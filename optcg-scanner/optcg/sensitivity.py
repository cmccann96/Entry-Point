"""Analytic feasibility study from measured dispersion statistics.

This is NOT the Stage 1 backtest and must never be reported as one. The Stage 1
backtest requires per-sale realised history across 20 card numbers. This module
requires only the summary statistics from the operator's existing dispersion
study (median, quartiles, CV, n, observation window) and asks a narrower
question that those statistics can actually answer:

    Given a distribution with these measured moments, how often should a sale
    land deep enough below the median to clear friction -- and is that rate
    above or below the go/no-go gate?

It is a projection from measured inputs, not a measurement. Its value is that it
can be run today, and that it fails the gate on the operator's own numbers,
which is worth knowing before commissioning a data pipeline.

The key modelling decision is that the left and right tails are fitted
*separately*. A single lognormal fitted to the CV is the obvious approach and it
is wrong here: the measured quartiles are strongly asymmetric in log space, so a
symmetric fit imports upper-tail fatness into the lower tail and overstates the
buyable opportunity. Buying happens in the left tail only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .friction import FrictionConfig, SaleChannel, breakeven_discount, evaluate_trade
from .stats import norm_cdf, norm_ppf

_Q1_Z = norm_ppf(0.25)  # ~ -0.6745
_Q3_Z = norm_ppf(0.75)  # ~ +0.6745


@dataclass(frozen=True)
class MeasuredDistribution:
    """Summary statistics from a completed dispersion study."""

    label: str
    median: float
    q1: float
    q3: float
    cv: float
    n: int
    window_months: float

    def __post_init__(self) -> None:
        if not 0 < self.q1 <= self.median <= self.q3:
            raise ValueError(f"{self.label}: need 0 < q1 <= median <= q3")
        if self.n < 2:
            raise ValueError(f"{self.label}: need n >= 2")
        if self.window_months <= 0:
            raise ValueError(f"{self.label}: window_months must be positive")

    @property
    def sales_per_year(self) -> float:
        """Observed sale frequency annualised from the study window."""
        return self.n * (12.0 / self.window_months)


@dataclass(frozen=True)
class LognormalFits:
    """Three sigma estimates from the same distribution.

    They disagree, and the disagreement is the finding: it quantifies how much
    of the headline CV is upper-tail skew rather than two-sided dispersion.
    """

    sigma_from_q1: float  # left tail -- the one that governs buying
    sigma_from_q3: float  # right tail
    sigma_from_cv: float  # symmetric fit implied by the headline CV

    @property
    def skew_ratio(self) -> float:
        """>1 means the right tail is fatter than the left."""
        return self.sigma_from_q3 / self.sigma_from_q1

    def as_dict(self) -> dict[str, float]:
        return {
            "left (q1)": self.sigma_from_q1,
            "cv-implied": self.sigma_from_cv,
            "right (q3)": self.sigma_from_q3,
        }


def fit_lognormal(dist: MeasuredDistribution) -> LognormalFits:
    """Fit sigma three ways: from Q1, from Q3, and from the CV.

    Under a lognormal, median = exp(mu), so log(q/median) = z_q * sigma.
    """
    sigma_q1 = math.log(dist.q1 / dist.median) / _Q1_Z
    sigma_q3 = math.log(dist.q3 / dist.median) / _Q3_Z
    sigma_cv = math.sqrt(math.log(1.0 + dist.cv**2))
    return LognormalFits(
        sigma_from_q1=sigma_q1,
        sigma_from_q3=sigma_q3,
        sigma_from_cv=sigma_cv,
    )


def prob_below_discount(sigma: float, discount: float) -> float:
    """P(sale price <= (1 - discount) * median) under a lognormal with this sigma."""
    if not 0.0 < discount < 1.0:
        raise ValueError("discount must be in (0, 1)")
    if sigma <= 0:
        return 0.0
    return norm_cdf(math.log(1.0 - discount) / sigma)


@dataclass
class OpportunityEstimate:
    """Projected deep-tail opportunity rate at one discount threshold."""

    discount: float
    sigma_label: str
    sigma: float
    probability: float
    events_per_year: float
    net_ev_by_channel: dict[SaleChannel, float] = field(default_factory=dict)
    net_margin_by_channel: dict[SaleChannel, float] = field(default_factory=dict)


def estimate_opportunities(
    dist: MeasuredDistribution,
    discounts: tuple[float, ...] = (0.20, 0.30, 0.40, 0.50),
    cfg: FrictionConfig | None = None,
) -> list[OpportunityEstimate]:
    """Project opportunity rate and net edge across discounts and sigma fits."""
    cfg = cfg or FrictionConfig()
    fits = fit_lognormal(dist)
    estimates: list[OpportunityEstimate] = []

    for discount in discounts:
        buy_price = dist.median * (1.0 - discount)
        ev: dict[SaleChannel, float] = {}
        margin: dict[SaleChannel, float] = {}
        for channel in SaleChannel:
            trade = evaluate_trade(buy_price, dist.median, channel, cfg)
            ev[channel] = trade.net_ev_aud
            margin[channel] = trade.net_margin_on_cost

        for label, sigma in fits.as_dict().items():
            probability = prob_below_discount(sigma, discount)
            estimates.append(
                OpportunityEstimate(
                    discount=discount,
                    sigma_label=label,
                    sigma=sigma,
                    probability=probability,
                    events_per_year=probability * dist.sales_per_year,
                    net_ev_by_channel=ev,
                    net_margin_by_channel=margin,
                )
            )
    return estimates


def required_probability_for_gate(
    dist: MeasuredDistribution, gate_events_per_year: float
) -> float:
    """Tail probability needed to hit the gate at this card's sale frequency."""
    if dist.sales_per_year <= 0:
        raise ValueError("sales_per_year must be positive")
    return gate_events_per_year / dist.sales_per_year


def required_listing_failure_rate(
    dist: MeasuredDistribution,
    gate_events_per_year: float,
    continuous_probability: float,
) -> float:
    """Extra 'listing failure' rate needed to close the gap to the gate.

    The continuous dispersion model cannot represent an event like a card
    selling for a 97% discount under a title carrying no variant descriptor.
    That is not a draw from the price distribution; it is a distinct failure
    mode -- the listing never reached the bidders who would have priced it. It
    is best modelled as a separate mixture component with its own rate.

    This returns the per-sale rate of that component required to reach the gate,
    given the continuous model already supplies ``continuous_probability``.
    Returns 0.0 if the continuous model alone already clears.
    """
    needed = required_probability_for_gate(dist, gate_events_per_year)
    return max(0.0, needed - continuous_probability)


def breakeven_table(
    price_points: tuple[float, ...],
    cfg: FrictionConfig | None = None,
) -> dict[float, dict[SaleChannel, float]]:
    """Breakeven discount by price point and channel.

    Demonstrates that fixed costs dominate at low price points: the discount
    required to break even rises steeply as the card gets cheaper.
    """
    cfg = cfg or FrictionConfig()
    return {
        price: {channel: breakeven_discount(price, channel, cfg) for channel in SaleChannel}
        for price in price_points
    }
