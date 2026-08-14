"""Stage 1 backtest harness.

Answers the gate questions from the brief, against real realised-sale history:

  * what fraction of raw sales clear a 30% discount to the trailing 30-day
    median for that variant?
  * what is the mean and median edge on those, net of each friction channel?
  * how many such events occur per card per year?
  * does the opportunity rate differ by set, price band, or variant tier?

Design notes that matter for the validity of the answer:

  * The trailing median is computed per *variant*, not per card number. Pooling
    variants that span a 50x price range would make almost every cheap variant
    look like a screaming discount on the expensive one. This is the single
    easiest way to produce a spuriously positive backtest.
  * The trailing window is strictly backward-looking and excludes the sale being
    evaluated. Including it leaks the answer into the benchmark.
  * A minimum number of prior comparable sales is required before a sale is
    judged at all. A "median" over two observations is noise, and unqualified
    sales are reported as skipped rather than silently dropped.
  * Graded sales are excluded from the tradeable distribution by default. The
    measured CV of 10.8% graded versus 31.1% raw says the slab label removes the
    ambiguity that creates the edge.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .friction import FrictionConfig, SaleChannel, evaluate_trade
from .sources.base import SoldSale
from .stats import median as _median
from .variants import Variant, infer_variant


@dataclass(frozen=True)
class BacktestConfig:
    trailing_days: int = 30
    discount_threshold: float = 0.30
    min_comparables: int = 5
    exclude_graded: bool = True
    gate_events_per_card_year: float = 5.0
    gate_net_edge: float = 0.30


@dataclass
class TailEvent:
    """One historical sale that would have been a qualifying buy."""

    card_number: str
    variant_key: str
    variant_label: str
    sold_date: date
    price_aud: float
    trailing_median_aud: float
    n_comparables: int
    title: str
    ambiguity: float

    @property
    def discount(self) -> float:
        return 1.0 - (self.price_aud / self.trailing_median_aud)

    def net_edge(self, channel: SaleChannel, cfg: FrictionConfig) -> float:
        """Net margin on landed cost, reselling at the trailing median."""
        return evaluate_trade(
            self.price_aud, self.trailing_median_aud, channel, cfg
        ).net_margin_on_cost


@dataclass
class CardResult:
    card_number: str
    total_sales: int = 0
    qualified_sales: int = 0  # had enough comparables to judge
    skipped_insufficient_comps: int = 0
    skipped_graded: int = 0
    events: list[TailEvent] = field(default_factory=list)
    observation_years: float = 0.0

    @property
    def tail_fraction(self) -> float:
        if self.qualified_sales == 0:
            return 0.0
        return len(self.events) / self.qualified_sales

    @property
    def events_per_year(self) -> float:
        if self.observation_years <= 0:
            return 0.0
        return len(self.events) / self.observation_years


@dataclass
class BacktestReport:
    results: list[CardResult]
    config: BacktestConfig
    friction: FrictionConfig

    @property
    def all_events(self) -> list[TailEvent]:
        return [event for result in self.results for event in result.events]

    @property
    def cards_analysed(self) -> int:
        return len(self.results)

    @property
    def mean_events_per_card_year(self) -> float:
        rates = [r.events_per_year for r in self.results if r.observation_years > 0]
        return sum(rates) / len(rates) if rates else 0.0

    def net_edges(self, channel: SaleChannel) -> list[float]:
        return [event.net_edge(channel, self.friction) for event in self.all_events]

    def edge_summary(self, channel: SaleChannel) -> dict[str, float]:
        edges = self.net_edges(channel)
        if not edges:
            return {"n": 0, "mean": 0.0, "median": 0.0, "share_above_gate": 0.0}
        above = sum(1 for e in edges if e >= self.config.gate_net_edge)
        return {
            "n": len(edges),
            "mean": sum(edges) / len(edges),
            "median": _median(edges),
            "share_above_gate": above / len(edges),
        }

    def by_price_band(self) -> dict[str, int]:
        bands: dict[str, int] = defaultdict(int)
        for event in self.all_events:
            price = event.trailing_median_aud
            if price < 100:
                bands["<$100"] += 1
            elif price < 500:
                bands["$100-499"] += 1
            elif price < 1500:
                bands["$500-1499"] += 1
            else:
                bands["$1500+"] += 1
        return dict(bands)

    def by_set(self) -> dict[str, int]:
        sets: dict[str, int] = defaultdict(int)
        for event in self.all_events:
            sets[event.card_number.split("-")[0]] += 1
        return dict(sets)

    def by_variant_tier(self) -> dict[str, int]:
        tiers: dict[str, int] = defaultdict(int)
        for event in self.all_events:
            tiers[event.variant_label] += 1
        return dict(tiers)

    def verdict(self) -> tuple[bool, str]:
        """Go/no-go against the configured gate.

        Deliberately blunt. The gate exists to be enforced, not negotiated.
        """
        rate = self.mean_events_per_card_year
        gate = self.config.gate_events_per_card_year
        if rate < gate:
            return False, (
                f"FAIL: {rate:.2f} qualifying events per card-year across "
                f"{self.cards_analysed} cards, below the gate of {gate:.1f}. "
                "The tail opportunity does not scale. Do not build Stage 2."
            )

        best = max(
            (self.edge_summary(c)["share_above_gate"] for c in SaleChannel),
            default=0.0,
        )
        if best <= 0.5:
            return False, (
                f"FAIL: event rate clears ({rate:.2f}/card-year) but only "
                f"{best:.0%} of events beat {self.config.gate_net_edge:.0%} net "
                "edge on the best channel. Edge too thin. Do not build Stage 2."
            )
        return True, (
            f"PASS: {rate:.2f} events per card-year, {best:.0%} of events above "
            f"{self.config.gate_net_edge:.0%} net edge. Stage 2 is justified."
        )


def _trailing_median(
    history: list[SoldSale], as_of: date, window_days: int
) -> tuple[float | None, int]:
    """Median of sales strictly before ``as_of`` within the window."""
    start = as_of - timedelta(days=window_days)
    prices = [s.price_aud for s in history if start <= s.sold_date < as_of]
    if not prices:
        return None, 0
    return _median(prices), len(prices)


def backtest_card(
    card_number: str,
    sales: list[SoldSale],
    variants: list[Variant],
    config: BacktestConfig | None = None,
) -> CardResult:
    """Run the tail-event backtest for one card number."""
    config = config or BacktestConfig()
    result = CardResult(card_number=card_number)

    if not sales:
        return result

    result.total_sales = len(sales)
    ordered = sorted(sales, key=lambda s: s.sold_date)
    span_days = (ordered[-1].sold_date - ordered[0].sold_date).days
    result.observation_years = max(span_days / 365.25, 0.0)

    # Assign every sale to its most likely variant, then evaluate each sale
    # against the trailing median of its own variant.
    by_variant: dict[str, list[SoldSale]] = defaultdict(list)
    posteriors = {}
    for sale in ordered:
        if config.exclude_graded and sale.is_graded:
            result.skipped_graded += 1
            continue
        posterior = infer_variant(sale.title, variants)
        if posterior.is_graded():
            result.skipped_graded += 1
            continue
        key = posterior.most_likely.key
        posteriors[id(sale)] = posterior
        by_variant[key].append(sale)

    for key, variant_sales in by_variant.items():
        for sale in variant_sales:
            trailing, n_comps = _trailing_median(
                variant_sales, sale.sold_date, config.trailing_days
            )
            if trailing is None or n_comps < config.min_comparables:
                result.skipped_insufficient_comps += 1
                continue

            result.qualified_sales += 1
            discount = 1.0 - (sale.price_aud / trailing)
            if discount >= config.discount_threshold:
                posterior = posteriors[id(sale)]
                result.events.append(
                    TailEvent(
                        card_number=card_number,
                        variant_key=key,
                        variant_label=posterior.most_likely.describe(),
                        sold_date=sale.sold_date,
                        price_aud=sale.price_aud,
                        trailing_median_aud=trailing,
                        n_comparables=n_comps,
                        title=sale.title,
                        ambiguity=posterior.ambiguity,
                    )
                )
    return result


def run_backtest(
    source,
    catalog: dict[str, list[Variant]],
    start: date,
    end: date,
    config: BacktestConfig | None = None,
    friction: FrictionConfig | None = None,
) -> BacktestReport:
    """Run Stage 1 across every card number in ``catalog``.

    ``source`` must implement ``fetch_sold``. Any source failure propagates --
    a partial backtest reported as complete would be worse than no backtest.
    """
    config = config or BacktestConfig()
    results = [
        backtest_card(card_number, source.fetch_sold(card_number, start, end), variants, config)
        for card_number, variants in catalog.items()
    ]
    return BacktestReport(
        results=results, config=config, friction=friction or FrictionConfig()
    )
