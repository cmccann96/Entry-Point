"""How much data is needed to settle the question, and the direct measurement.

The Stage 1 verdict established that the whole thesis turns on one quantity:
the rate at which sales go off at a deep discount because the listing failed to
describe the variant -- the $30.64 case. Everything else is comparatively well
determined.

That quantity was estimated from one observation in 13 sales: 7.7%, 95% CI
[0.2%, 36.0%]. The rate required to clear the gate, 20.4%, sits inside that
interval, so n=13 cannot distinguish "worth doing" from "got lucky once".

This module answers two practical questions:

  * ``required_sample_size`` -- how many sales must be collected before the
    interval is narrow enough to separate those cases
  * ``measure_failure_rate`` -- given real sales, the direct measurement, with
    an interval rather than a point estimate

The second is the actual validity test. It is cheap: it needs undescribed
listings and their trailing medians, not a full backtest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .backtest import BacktestConfig
from .sources.base import SoldSale
from .stats import median as _median
from .variants import Variant, infer_variant


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Preferred over the normal approximation here because the counts are small
    and the proportion is near zero, where the normal interval misbehaves badly
    (it can even go negative).
    """
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be in [0, n]")

    p = successes / n
    denominator = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denominator
    spread = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denominator
    return max(0.0, centre - spread), min(1.0, centre + spread)


def required_sample_size(
    assumed_rate: float,
    threshold: float,
    z: float = 1.96,
    max_n: int = 20000,
) -> int:
    """Sales needed before the interval separates ``assumed_rate`` from ``threshold``.

    "Separates" means: if the true rate really is ``assumed_rate``, the Wilson
    interval around the observed count excludes ``threshold`` -- so the data
    would actually decide the question rather than shrug at it.

    Returns the smallest such n, or ``max_n`` if the two are too close to
    separate at a practical sample size.
    """
    if not 0 < assumed_rate < 1 or not 0 < threshold < 1:
        raise ValueError("rates must be in (0, 1)")
    if abs(assumed_rate - threshold) < 1e-6:
        return max_n

    for n in range(10, max_n + 1, 5):
        successes = round(assumed_rate * n)
        low, high = wilson_interval(successes, n, z)
        if (assumed_rate < threshold and high < threshold) or (
            assumed_rate > threshold and low > threshold
        ):
            return n
    return max_n


@dataclass
class FailureRateMeasurement:
    """Bounds on the listing-failure rate.

    Bounds rather than a point estimate, because the quantity is **not fully
    identifiable from sale records alone.** An undescribed listing that sold for
    $30.64 is either a plain SEC priced correctly or a manga rare that nobody
    saw. The sale record cannot tell you which -- that is precisely what the
    missing description would have told you.

    So sales are sorted into three buckets:

      * ``unambiguous`` -- below threshold against even the *cheapest* variant
        the listing could have been. Cheap however you read it.
      * ``candidates`` -- below threshold against the dearest plausible variant
        but not the cheapest. Either a bargain or a correctly-priced cheap
        variant, and the photos decide which.
      * everything else -- not cheap on any reading.

    The true failure rate lies between ``lower_rate`` and ``upper_rate``.
    Narrowing it means looking at photos, which is what Stage 3 is for.
    """

    undescribed_sales: int
    unambiguous: int
    candidates: int
    total_judged: int
    threshold_discount: float
    examples: list[tuple[str, float, float, str]]  # title, price, benchmark, bucket

    @property
    def lower_rate(self) -> float:
        if self.total_judged == 0:
            return 0.0
        return self.unambiguous / self.total_judged

    @property
    def upper_rate(self) -> float:
        if self.total_judged == 0:
            return 1.0
        return (self.unambiguous + self.candidates) / self.total_judged

    @property
    def interval(self) -> tuple[float, float]:
        """Widest defensible interval: sampling error on top of the identification gap."""
        if self.total_judged == 0:
            return (0.0, 1.0)
        low = wilson_interval(self.unambiguous, self.total_judged)[0]
        high = wilson_interval(self.unambiguous + self.candidates, self.total_judged)[1]
        return low, high

    def decides_against(self, required_rate: float) -> str:
        """Whether this measurement actually settles anything."""
        low, high = self.interval
        if self.total_judged == 0:
            return "NO DATA"
        if high < required_rate:
            return "DECIDES: NO-GO"
        if low > required_rate:
            return "DECIDES: GO"
        return "INCONCLUSIVE"


def measure_failure_rate(
    sales: list[SoldSale],
    variants: list[Variant],
    config: BacktestConfig | None = None,
    deep_discount: float = 0.50,
) -> FailureRateMeasurement:
    """Bound how often an undescribed listing sells at a deep discount.

    Restricted to sales whose title carries no variant-determining descriptor,
    since that is the failure mode being measured.

    The benchmark construction is the part that matters. An undescribed sale is
    NOT compared against the trailing median of its most likely variant -- doing
    so would assign it to the plain printing (which dominates the population
    prior) and then compare it against other plain printings, which makes the
    failure mode invisible by construction. Instead it is compared against every
    variant it could plausibly be, benchmarked on *described* sales of each:

      * below threshold vs the cheapest plausible variant -> unambiguously cheap
      * below threshold vs the dearest but not the cheapest -> candidate, and
        only the photos can settle it

    ``deep_discount`` defaults to 50% rather than the backtest's 30%: this is
    measuring listing failure, not ordinary dispersion, and a 50% floor keeps
    the two from blurring together.
    """
    config = config or BacktestConfig()
    ordered = sorted(sales, key=lambda s: s.sold_date)

    described_by_variant: dict[str, list[SoldSale]] = {}
    undescribed_sales: list[SoldSale] = []

    for sale in ordered:
        if config.exclude_graded and sale.is_graded:
            continue
        posterior = infer_variant(sale.title, variants)
        if posterior.is_graded():
            continue
        if posterior.is_undescribed:
            undescribed_sales.append(sale)
        else:
            described_by_variant.setdefault(posterior.most_likely.key, []).append(sale)

    unambiguous = 0
    candidates = 0
    judged = 0
    examples: list[tuple[str, float, float, str]] = []

    for sale in undescribed_sales:
        window_start = sale.sold_date.toordinal() - config.trailing_days

        benchmarks: list[float] = []
        for variant_sales in described_by_variant.values():
            comparables = [
                s.price_aud
                for s in variant_sales
                if s is not sale and window_start <= s.sold_date.toordinal() < sale.sold_date.toordinal()
            ]
            if len(comparables) >= config.min_comparables:
                benchmarks.append(_median(comparables))

        if not benchmarks:
            continue

        judged += 1
        cheapest, dearest = min(benchmarks), max(benchmarks)
        if sale.price_aud <= cheapest * (1 - deep_discount):
            unambiguous += 1
            examples.append((sale.title, sale.price_aud, cheapest, "unambiguous"))
        elif sale.price_aud <= dearest * (1 - deep_discount):
            candidates += 1
            examples.append((sale.title, sale.price_aud, dearest, "candidate"))

    return FailureRateMeasurement(
        undescribed_sales=len(undescribed_sales),
        unambiguous=unambiguous,
        candidates=candidates,
        total_judged=judged,
        threshold_discount=deep_discount,
        examples=examples,
    )


def collection_plan(
    observed_successes: int,
    observed_n: int,
    required_rate: float,
) -> dict[str, object]:
    """What it would take to turn the current guess into a decision."""
    point = observed_successes / observed_n if observed_n else 0.0
    low, high = wilson_interval(observed_successes, observed_n) if observed_n else (0.0, 1.0)

    # Size the collection for the optimistic and pessimistic readings of what
    # we have, so the plan is robust to which one is true.
    scenarios = {}
    for label, rate in (("if true rate is the point estimate", max(point, 0.01)),
                        ("if true rate is half the point estimate", max(point / 2, 0.005))):
        scenarios[label] = required_sample_size(rate, required_rate)

    return {
        "point_estimate": point,
        "interval": (low, high),
        "required_rate": required_rate,
        "currently_decisive": high < required_rate or low > required_rate,
        "scenarios": scenarios,
    }
