"""Tests for the Stage 1 harness.

Sales constructed here are synthetic *test fixtures* exercising harness
mechanics -- leak-freedom, variant separation, gate enforcement. They are not
market data and produce no market conclusions. The harness refuses to run
against anything but a real operator-supplied export; see test_sources.py.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from optcg.backtest import BacktestConfig, backtest_card, run_backtest
from optcg.friction import FrictionConfig, SaleChannel
from optcg.sources.base import SoldSale
from optcg.variants import Channel, Region, Treatment, Variant

CARD = "OP06-118"


def _variants() -> list[Variant]:
    return [
        Variant(CARD, "SEC", Treatment.BASE, Channel.BOOSTER, Region.JP, 100.0, "plain"),
        Variant(CARD, "SEC", Treatment.MANGA, Channel.BOOSTER, Region.JP, 8.0, "manga"),
    ]


def _sale(day: int, price: float, title: str = "OP06-118 manga rare", graded: bool = False):
    return SoldSale(
        card_number=CARD,
        sold_date=date(2026, 1, 1) + timedelta(days=day),
        price_aud=price,
        title=title,
        is_graded=graded,
    )


class TestTrailingMedian:
    def test_requires_minimum_comparables(self):
        cfg = BacktestConfig(min_comparables=5)
        # Only 3 prior sales -- nothing should be judged.
        sales = [_sale(d, 1000.0) for d in range(3)] + [_sale(5, 100.0)]
        result = backtest_card(CARD, sales, _variants(), cfg)
        assert result.events == []
        assert result.qualified_sales == 0
        assert result.skipped_insufficient_comps == 4

    def test_does_not_leak_the_evaluated_sale_into_its_benchmark(self):
        # A deep discount must be measured against PRIOR sales only. If the sale
        # itself entered the median, the discount would be understated.
        cfg = BacktestConfig(min_comparables=3)
        sales = [_sale(d, 1000.0) for d in range(6)] + [_sale(7, 500.0)]
        result = backtest_card(CARD, sales, _variants(), cfg)
        assert len(result.events) == 1
        assert result.events[0].trailing_median_aud == pytest.approx(1000.0)
        assert result.events[0].discount == pytest.approx(0.50)

    def test_window_excludes_stale_sales(self):
        cfg = BacktestConfig(min_comparables=3, trailing_days=30)
        # Six sales at day 0-5, then a cheap sale 200 days later: comparables
        # have aged out of the window, so it cannot be judged.
        sales = [_sale(d, 1000.0) for d in range(6)] + [_sale(200, 300.0)]
        result = backtest_card(CARD, sales, _variants(), cfg)
        assert result.events == []


class TestVariantSeparation:
    def test_medians_are_per_variant_not_per_card(self):
        # The classic false positive: a cheap plain printing looks like a 97%
        # discount if benchmarked against the manga variant's median.
        cfg = BacktestConfig(min_comparables=3)
        sales = [_sale(d, 1200.0, "OP06-118 manga rare") for d in range(6)]
        sales += [_sale(d, 30.0, "OP06-118 SEC") for d in range(6, 12)]
        result = backtest_card(CARD, sales, _variants(), cfg)
        # Each variant is internally consistent, so there is no tail event.
        assert result.events == []

    def test_genuine_within_variant_discount_is_caught(self):
        cfg = BacktestConfig(min_comparables=3)
        sales = [_sale(d, 1200.0, "OP06-118 manga rare") for d in range(6)]
        sales += [_sale(7, 400.0, "OP06-118 manga rare")]
        result = backtest_card(CARD, sales, _variants(), cfg)
        assert len(result.events) == 1
        assert result.events[0].discount == pytest.approx(1 - 400 / 1200)


class TestGradedExclusion:
    def test_graded_sales_excluded_by_flag(self):
        cfg = BacktestConfig(min_comparables=2)
        sales = [_sale(d, 1000.0) for d in range(4)]
        sales += [_sale(5, 3000.0, graded=True)]
        result = backtest_card(CARD, sales, _variants(), cfg)
        assert result.skipped_graded == 1

    def test_graded_detected_from_title_even_without_flag(self):
        cfg = BacktestConfig(min_comparables=2)
        sales = [_sale(d, 1000.0) for d in range(4)]
        sales += [_sale(5, 3000.0, title="OP06-118 manga PSA 10")]
        result = backtest_card(CARD, sales, _variants(), cfg)
        assert result.skipped_graded == 1


class TestVerdict:
    def _report(self, events_per_card_year: float):
        cfg = BacktestConfig(min_comparables=3, gate_events_per_card_year=5.0)
        # Build one year of history with a controllable number of tail events.
        sales = []
        for month in range(12):
            for k in range(4):
                sales.append(_sale(month * 30 + k, 1200.0))
        for i in range(int(events_per_card_year)):
            sales.append(_sale(i * 30 + 10, 300.0))
        source = type("S", (), {"fetch_sold": lambda self, c, s, e: sales})()
        return run_backtest(
            source, {CARD: _variants()}, date(2026, 1, 1), date(2026, 12, 31), cfg, FrictionConfig()
        )

    def test_gate_fails_below_threshold(self):
        passed, message = self._report(2).verdict()
        assert not passed
        assert "FAIL" in message and "Do not build Stage 2" in message

    def test_verdict_reports_rate_honestly(self):
        report = self._report(2)
        assert report.mean_events_per_card_year < 5.0

    def test_empty_history_does_not_pass(self):
        source = type("S", (), {"fetch_sold": lambda self, c, s, e: []})()
        report = run_backtest(source, {CARD: _variants()}, date(2026, 1, 1), date(2026, 12, 31))
        passed, message = report.verdict()
        assert not passed
        assert report.all_events == []


class TestEdgeAccounting:
    def test_net_edge_differs_by_channel(self):
        cfg = BacktestConfig(min_comparables=3)
        sales = [_sale(d, 1200.0) for d in range(6)] + [_sale(7, 400.0)]
        result = backtest_card(CARD, sales, _variants(), cfg)
        event = result.events[0]
        friction = FrictionConfig()
        overseas = event.net_edge(SaleChannel.OVERSEAS_TO_EBAY_AU, friction)
        local = event.net_edge(SaleChannel.LOCAL_TO_LOCAL, friction)
        assert local > overseas
