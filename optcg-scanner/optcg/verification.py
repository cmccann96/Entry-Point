"""Measuring the mislabel rate against image ground truth.

This is the real Stage 1 measurement. The strategy does not rest on how often a
title is *blank* -- that was one observation. It rests on how often a title is
*wrong*, and specifically on how often it is wrong in the profitable direction:
the seller claims a lesser variant than the card actually is.

Only an image settles that. So the input here is a sale annotated with a
confirmed variant, established by looking at the photo -- by eye, or later by a
vision model. The four observations that resolve a variant, per the brief:

  * is there a star above the rarity code (parallel or above)
  * is the art manga panels, painted full-bleed, or painted inside a frame
  * is there a gold stamp, WINNER stamp, or printed serial
  * is it slabbed, and if so what grade and grader

Crucially this can be done by hand. Fifty listings eyeballed in an afternoon
gives a usable estimate, with no vision API and no live scanner. That is the
cheapest path to a real answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .friction import FrictionConfig, SaleChannel, evaluate_trade
from .power import wilson_interval
from .sources.base import SoldSale
from .stats import median as _median
from .variants import Variant, infer_variant


@dataclass(frozen=True)
class VerifiedSale:
    """A sale whose true variant was confirmed from its image."""

    sale: SoldSale
    true_variant_key: str
    notes: str = ""


@dataclass
class MislabelReport:
    """How often, and in which direction, titles misdescribe the card."""

    total: int = 0
    correct: int = 0
    under_claimed: int = 0  # card is dearer than the title implies -- the edge
    over_claimed: int = 0  # card is cheaper than the title implies -- the trap
    exploitable: int = 0  # under-claimed AND sold below the true variant's median
    under_claim_gaps: list[float] = field(default_factory=list)  # realised net EV, AUD
    examples: list[tuple[str, str, str, float]] = field(default_factory=list)

    @property
    def mislabel_rate(self) -> float:
        return 0.0 if self.total == 0 else (self.total - self.correct) / self.total

    @property
    def under_claim_rate(self) -> float:
        return 0.0 if self.total == 0 else self.under_claimed / self.total

    @property
    def exploitable_rate(self) -> float:
        """The parameter the strategy actually depends on."""
        return 0.0 if self.total == 0 else self.exploitable / self.total

    @property
    def exploitable_interval(self) -> tuple[float, float]:
        if self.total == 0:
            return (0.0, 1.0)
        return wilson_interval(self.exploitable, self.total)

    @property
    def median_gap_aud(self) -> float:
        """Median net EV, in AUD, of an exploitable listing after friction."""
        return _median(self.under_claim_gaps) if self.under_claim_gaps else 0.0

    @property
    def total_net_ev_aud(self) -> float:
        """Total that would have been banked across every exploitable sale."""
        return sum(self.under_claim_gaps)

    def decides_against(self, required_rate: float) -> str:
        low, high = self.exploitable_interval
        if self.total == 0:
            return "NO DATA"
        if high < required_rate:
            return "DECIDES: NO-GO"
        if low > required_rate:
            return "DECIDES: GO"
        return "INCONCLUSIVE"


def measure_mislabel_rate(
    verified: list[VerifiedSale],
    variants: list[Variant],
    reference_prices: dict[str, float],
    channel: SaleChannel = SaleChannel.OVERSEAS_TO_EBAY_AU,
    cfg: FrictionConfig | None = None,
    min_net_ev_aud: float = 0.0,
) -> MislabelReport:
    """Compare what titles claimed against what the images showed.

    ``reference_prices`` maps variant key -> trailing median AUD, and decides
    the *direction* of a mislabel: a card is under-claimed when its true variant
    is dearer than the one its title implied.

    A mislabel is only counted as exploitable when the card additionally sold
    cheap enough for the trade to clear friction on ``channel``. Two separate
    reasons the raw mislabel rate overstates the opportunity:

      * a comic parallel described as "parallel" that still fetched full
        comic-parallel money was priced correctly by bidders who saw the photo
      * a card that sold a little under its true median still loses money once
        a ~30% round trip is applied

    ``under_claim_gaps`` therefore holds realised net EV in AUD, not raw price
    gaps -- the number you would actually have banked.
    """
    cfg = cfg or FrictionConfig()
    report = MislabelReport()

    for entry in verified:
        posterior = infer_variant(entry.sale.title, variants)
        claimed_key = posterior.most_likely.key
        true_key = entry.true_variant_key

        report.total += 1
        if claimed_key == true_key:
            report.correct += 1
            continue

        claimed_price = reference_prices.get(claimed_key)
        true_price = reference_prices.get(true_key)
        if claimed_price is None or true_price is None:
            # Direction is undecidable without both benchmarks. Counted as a
            # mislabel, but not attributed to either direction.
            continue

        if true_price > claimed_price:
            report.under_claimed += 1
            # "Could be sold for more" has to mean more NET OF FRICTION. A card
            # that went at $1,499 against a $1,500 median is under-claimed and
            # underpriced and still loses money after a ~30% round trip. Only
            # count it when the trade actually pays.
            trade = evaluate_trade(entry.sale.price_aud, true_price, channel, cfg)
            if trade.net_ev_aud > min_net_ev_aud:
                report.exploitable += 1
                report.under_claim_gaps.append(trade.net_ev_aud)
                report.examples.append(
                    (
                        entry.sale.title,
                        posterior.most_likely.describe(),
                        next(
                            (v.describe() for v in variants if v.key == true_key),
                            true_key,
                        ),
                        trade.net_ev_aud,
                    )
                )
        else:
            report.over_claimed += 1

    return report


def parse_verified(
    sales: list[SoldSale],
    truth: dict[str, str],
    variants: list[Variant],
) -> list[VerifiedSale]:
    """Attach confirmed variants to sales.

    ``truth`` maps a sale title to a variant, as recorded while looking at
    photos. Matching is deliberately forgiving, because this column is typed by
    hand fifty times and demanding the full label invites transcription errors:
    a full key, a full label, a bare treatment name ("comic", "manga"), or any
    substring that identifies exactly one variant will all resolve.

    Ambiguity and typos both raise. Silently dropping an annotation would bias
    the measurement toward whatever happened to be easy to label, and silently
    guessing between two variants would corrupt the direction of the mislabel --
    which is the entire quantity being measured.
    """
    verified: list[VerifiedSale] = []
    unknown: list[str] = []
    ambiguous: list[tuple[str, list[str]]] = []

    for sale in sales:
        raw = truth.get(sale.title)
        if raw is None:
            continue
        label = raw.strip()
        if not label:
            continue

        matches = _resolve_variant(label, variants)
        if not matches:
            unknown.append(label)
        elif len(matches) > 1:
            ambiguous.append((label, [v.describe() for v in matches]))
        else:
            verified.append(VerifiedSale(sale=sale, true_variant_key=matches[0].key))

    problems: list[str] = []
    if unknown:
        problems.append(f"unrecognised variant label(s): {sorted(set(unknown))}")
    for label, candidates in ambiguous:
        problems.append(f"{label!r} matches {len(candidates)} variants: {candidates}")
    if problems:
        known = sorted({v.label or v.describe() for v in variants})
        raise ValueError(
            "; ".join(problems)
            + f". Known variants: {known}"
            + ". A bare treatment name (e.g. 'comic') works when it is unambiguous."
        )
    return verified


def _resolve_variant(label: str, variants: list[Variant]) -> list[Variant]:
    """Resolve an annotation to variants, most specific match first."""
    needle = label.casefold()

    for matcher in (
        lambda v: v.key.casefold() == needle,
        lambda v: (v.label or "").casefold() == needle,
        lambda v: v.treatment.value.casefold() == needle,
        lambda v: needle in (v.label or "").casefold(),
    ):
        matches = [v for v in variants if matcher(v)]
        if matches:
            return matches
    return []
