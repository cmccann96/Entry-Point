"""Live scan: measure the mislabel rate on real listings, and find live trades.

This is the answer to "how do we test it on real listings". The historical
sold-data problem was a problem for *backtesting*; it is not a problem here.

  * the eBay Browse API returns active listings freely -- no login wall, no
    restricted-data gate, free tier, fully within the project's constraints
  * active listings carry titles AND image URLs, which is everything the
    mislabel measurement needs
  * a mislabelled active listing is not a data point. It is a trade.

So the measurement and the strategy are the same activity, which is why this
runs before any historical pipeline is worth building.

The benchmark still has to come from somewhere. Reference prices are operator-
supplied per variant (config.toml), or from PriceCharting's current-price API,
which is exactly what that API does return. Historical sold history was never
needed for this test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .friction import FrictionConfig, SaleChannel
from .power import wilson_interval
from .sources.base import ActiveListing
from .valuation import ListingValuation, triage_rank, value_listing
from .variants import Variant, infer_variant
from .vision import VisionReader, VisionResult


@dataclass
class ScannedListing:
    """One live listing, after title inference and (optionally) a photo check."""

    listing: ActiveListing
    valuation: ListingValuation
    claimed_variant: Variant
    ambiguity: float
    information_gain: float
    vision: VisionResult | None = None

    @property
    def title_claim(self) -> str:
        return self.claimed_variant.describe()

    @property
    def verified_treatment(self) -> str | None:
        if self.vision is None:
            return None
        treatment = self.vision.implied_treatment()
        return treatment.value if treatment else None

    @property
    def is_mislabelled(self) -> bool | None:
        """True / False / None, where None means the photo could not decide.

        The three-state return matters. Collapsing "unreadable" into "correctly
        labelled" would bias the measured mislabel rate downward -- and that
        rate is the number the whole go/no-go decision rests on.
        """
        if self.vision is None:
            return None
        match = self.vision.matches(self.claimed_variant)
        return None if match is None else not match

    def true_variant(self, candidates: list[Variant]) -> Variant | None:
        """The variant the photo actually shows, matched against the catalog."""
        treatment = self.vision.implied_treatment() if self.vision else None
        if treatment is None:
            return None
        for variant in candidates:
            if variant.treatment is treatment:
                return variant
        return None

    def realised_ev(
        self,
        candidates: list[Variant],
        reference_prices: dict[str, float],
        channel: SaleChannel,
        cfg: FrictionConfig,
    ) -> float | None:
        """Net EV of buying this listing, priced at the variant the PHOTO shows.

        This is the number that matters, and it is only computable after the
        photo check -- which is the whole argument for doing the photo check.
        """
        from .friction import evaluate_trade

        variant = self.true_variant(candidates)
        if variant is None or variant.key not in reference_prices:
            return None
        return evaluate_trade(
            self.listing.ask_aud, reference_prices[variant.key], channel, cfg
        ).net_ev_aud


@dataclass
class ScanReport:
    """Outcome of one scan pass."""

    scanned: list[ScannedListing] = field(default_factory=list)
    verified: int = 0
    mislabelled: int = 0
    undeterminable: int = 0

    @property
    def mislabel_rate(self) -> float:
        return self.mislabelled / self.verified if self.verified else 0.0

    @property
    def mislabel_interval(self) -> tuple[float, float]:
        if self.verified == 0:
            return (0.0, 1.0)
        return wilson_interval(self.mislabelled, self.verified)

    def opportunities(
        self,
        catalog: dict[str, list[Variant]],
        reference_prices: dict[str, float],
        channel: SaleChannel,
        cfg: FrictionConfig,
        min_ev: float = 0.0,
        include_auctions: bool = False,
    ) -> list[tuple[ScannedListing, float]]:
        """Live listings whose photo-verified value clears friction, and which
        can actually be bought at the ask.

        **Auctions are excluded by default, and that is not a preference.** An
        auction's current bid is not a price -- it is a partial state of an
        ongoing price discovery that every other interested buyer can also see.
        Valuing one off its current bid marks every freshly-opened auction as a
        huge bargain and would swamp the output with listings that will close at
        fair value. ``auction_watchlist`` handles those separately, where the
        mechanics are different (bid at the close; latency is irrelevant).
        """
        found: list[tuple[ScannedListing, float]] = []
        for item in self.scanned:
            if not include_auctions and not item.listing.is_immediately_buyable:
                continue
            candidates = catalog.get(item.listing.card_number, [])
            ev = item.realised_ev(candidates, reference_prices, channel, cfg)
            if ev is not None and ev > min_ev:
                found.append((item, ev))
        return sorted(found, key=lambda pair: pair[1], reverse=True)

    def auction_watchlist(
        self,
        catalog: dict[str, list[Variant]],
        reference_prices: dict[str, float],
    ) -> list[tuple[ScannedListing, float]]:
        """Auctions whose photo shows a variant dearer than the title claims.

        Returned with the TRUE VARIANT'S REFERENCE VALUE, not an EV -- because
        what you would pay is unknown until the auction closes. This is a
        maximum-bid ceiling to work back from, not a profit figure.

        Worth far less than the fixed-price list, and for a structural reason:
        an auction runs for days, so every interested party has time to see the
        same photo you did. A mislabelled auction does not stay cheap, it gets
        found -- unless the title is bad enough that the listing never surfaces
        in searches at all, which is the separate listing-failure mode.
        """
        found: list[tuple[ScannedListing, float]] = []
        for item in self.scanned:
            if not item.listing.is_auction:
                continue
            variant = item.true_variant(catalog.get(item.listing.card_number, []))
            if variant is None:
                continue
            value = reference_prices.get(variant.key)
            if value is None or item.is_mislabelled is not True:
                continue
            found.append((item, value))
        return sorted(found, key=lambda pair: pair[1], reverse=True)

    def decides_against(self, required_rate: float) -> str:
        low, high = self.mislabel_interval
        if self.verified == 0:
            return "NO DATA"
        if high < required_rate:
            return "DECIDES: NO-GO"
        if low > required_rate:
            return "DECIDES: GO"
        return "INCONCLUSIVE"


def scan_card(
    card_number: str,
    listings: list[ActiveListing],
    variants: list[Variant],
    reference_prices: dict[str, float],
    reader: VisionReader | None = None,
    channel: SaleChannel = SaleChannel.OVERSEAS_TO_EBAY_AU,
    cfg: FrictionConfig | None = None,
    vision_budget: int = 20,
) -> list[ScannedListing]:
    """Score live listings, then photo-check the ones worth checking.

    Triage is on information value, never cheapness -- a mislabelled dear card
    looks expensive against the variant its title claims, so a cheapness screen
    would spend the vision budget on everything except the listings that matter.
    """
    cfg = cfg or FrictionConfig()
    scanned: list[ScannedListing] = []

    for listing in listings:
        posterior = infer_variant(listing.title, variants)
        if posterior.is_graded():
            # Graded CV is 10.8% vs 31.1% raw -- the slab removes the ambiguity
            # that creates the edge. Skip, don't spend vision budget.
            continue
        try:
            valuation = value_listing(
                posterior, reference_prices, listing.ask_aud, channel, cfg
            )
        except ValueError:
            # No live variant has a reference price. Skip rather than invent one.
            continue
        scanned.append(
            ScannedListing(
                listing=listing,
                valuation=valuation,
                claimed_variant=posterior.most_likely,
                ambiguity=posterior.ambiguity,
                information_gain=posterior.information_gain,
            )
        )

    if reader is None:
        return scanned

    ranked = triage_rank(
        [(item.listing.listing_id, item.valuation) for item in scanned], limit=vision_budget
    )
    priority = {listing_id for listing_id, _ in ranked}
    by_id = {item.listing.listing_id: item for item in scanned}

    for listing_id in priority:
        item = by_id[listing_id]
        if not item.listing.image_urls:
            continue
        item.vision = reader.read_listing(item.listing.image_urls)

    return scanned


def run_scan(
    source,
    catalog: dict[str, list[Variant]],
    reference_prices: dict[str, float],
    reader: VisionReader | None = None,
    channel: SaleChannel = SaleChannel.OVERSEAS_TO_EBAY_AU,
    cfg: FrictionConfig | None = None,
    vision_budget: int = 20,
) -> ScanReport:
    """Scan every card number in the catalog."""
    report = ScanReport()

    for card_number, variants in catalog.items():
        prices = {
            key: price
            for key, price in reference_prices.items()
            if key.startswith(card_number)
        }
        if not prices:
            continue

        scanned = scan_card(
            card_number,
            source.fetch_active(card_number),
            variants,
            prices,
            reader,
            channel,
            cfg,
            vision_budget,
        )
        report.scanned.extend(scanned)

        for item in scanned:
            if item.vision is None:
                continue
            if item.is_mislabelled is None:
                report.undeterminable += 1
            else:
                report.verified += 1
                if item.is_mislabelled:
                    report.mislabelled += 1

    return report
