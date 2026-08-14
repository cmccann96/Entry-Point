"""Valuation under variant uncertainty, and what it is worth to look at a photo.

The correction this module implements: a listing's value must not be scored
against the variant its title claims, because the failure mode being exploited
is sellers claiming the wrong thing.

Concretely. A comic parallel worth ~$1,500 is listed as "parallel" and priced at
$200. Scored against the standard-parallel median of ~$70 that ask is nearly 3x
too *expensive*, and any cheapness screen discards it. It is also precisely the
listing you want. Cheapness relative to the claim is not merely a weak signal
here -- it is anti-correlated with the opportunity.

So value is computed as an expectation over the whole posterior, and listings
are triaged on **value of information**: how much would resolving the variant
change the decision? That is what earns a photo check, and it is high exactly
where blind EV is unattractive but the upside is large.
"""

from __future__ import annotations

from dataclasses import dataclass

from .friction import FrictionConfig, SaleChannel, evaluate_trade, landed_cost, net_proceeds
from .variants import VariantPosterior


@dataclass(frozen=True)
class ListingValuation:
    """What a listing is worth, and what it is worth to check it."""

    ask_aud: float
    channel: SaleChannel
    blind_ev_aud: float
    upside_ev_aud: float
    upside_probability: float
    downside_ev_aud: float
    best_variant_label: str
    worst_variant_label: str
    price_dispersion: float

    @property
    def information_value_aud(self) -> float:
        """Expected gain from resolving the variant before committing.

        Blind, you take ``blind_ev`` (and only if positive). Informed, you buy
        only in the states where buying pays. The difference is what the photo
        is worth, and it is the correct triage key.
        """
        informed = self.upside_probability * max(0.0, self.upside_ev_aud)
        blind = max(0.0, self.blind_ev_aud)
        return max(0.0, informed - blind)

    @property
    def is_hidden_by_cheapness_screen(self) -> bool:
        """True when a cheapness screen would discard a listing worth checking.

        The diagnostic for the bug this module exists to fix: blind EV is
        negative -- so the ask looks dear against what the seller claims -- yet
        the upside is large. These are the mislabelled dear cards.
        """
        return self.blind_ev_aud <= 0 < self.information_value_aud


def value_listing(
    posterior: VariantPosterior,
    reference_prices: dict[str, float],
    ask_aud: float,
    channel: SaleChannel = SaleChannel.OVERSEAS_TO_EBAY_AU,
    cfg: FrictionConfig | None = None,
    live_threshold: float = 0.01,
) -> ListingValuation:
    """Value a listing across every variant its description leaves live.

    ``reference_prices`` maps variant key -> trailing median in AUD. Variants
    with no reference price are skipped: guessing one would invent the number
    that drives the whole decision.
    """
    cfg = cfg or FrictionConfig()

    live = {
        key: probability
        for key, probability in posterior.probabilities.items()
        if probability > live_threshold
        and key in reference_prices
        and reference_prices[key] > 0
    }
    if not live:
        raise ValueError(
            "no live variant has a reference price; cannot value this listing "
            "without inventing a benchmark"
        )

    cost = landed_cost(ask_aud, channel, cfg)

    # Blind EV: buy now, take whatever variant it turns out to be.
    blind = sum(
        probability * net_proceeds(reference_prices[key], channel, cfg)
        for key, probability in live.items()
    ) / sum(live.values()) - cost

    best_key = max(live, key=lambda k: reference_prices[k])
    worst_key = min(live, key=lambda k: reference_prices[k])

    upside = evaluate_trade(ask_aud, reference_prices[best_key], channel, cfg)
    downside = evaluate_trade(ask_aud, reference_prices[worst_key], channel, cfg)

    prices = [reference_prices[k] for k in live]
    return ListingValuation(
        ask_aud=ask_aud,
        channel=channel,
        blind_ev_aud=blind,
        upside_ev_aud=upside.net_ev_aud,
        upside_probability=live[best_key] / sum(live.values()),
        downside_ev_aud=downside.net_ev_aud,
        best_variant_label=posterior.variants[best_key].describe(),
        worst_variant_label=posterior.variants[worst_key].describe(),
        price_dispersion=max(prices) / min(prices),
    )


def triage_rank(
    valuations: list[tuple[str, ListingValuation]],
    limit: int | None = None,
) -> list[tuple[str, ListingValuation]]:
    """Order listings by what a photo check is worth, best first.

    Ranked on ``information_value_aud`` rather than on cheapness or on blind EV,
    so mislabelled dear cards -- which look expensive against their claimed
    variant -- are not filtered out before anyone looks at them.
    """
    ordered = sorted(
        valuations, key=lambda item: item[1].information_value_aud, reverse=True
    )
    return ordered[:limit] if limit else ordered
