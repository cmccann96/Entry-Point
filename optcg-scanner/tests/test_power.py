"""Tests for sample sizing and the direct failure-rate measurement."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from optcg.backtest import BacktestConfig
from optcg.power import (
    collection_plan,
    measure_failure_rate,
    required_sample_size,
    wilson_interval,
)
from optcg.sources.base import SoldSale
from optcg.variants import Channel, Region, Treatment, Variant

CARD = "OP06-118"


def _variants():
    return [
        Variant(CARD, "SEC", Treatment.BASE, Channel.BOOSTER, Region.JP, 100.0, "plain"),
        Variant(CARD, "SEC", Treatment.MANGA, Channel.BOOSTER, Region.JP, 8.0, "manga"),
    ]


def _sale(day, price, title):
    return SoldSale(CARD, date(2026, 1, 1) + timedelta(days=day), price, title)


class TestWilsonInterval:
    def test_contains_point_estimate(self):
        low, high = wilson_interval(1, 13)
        assert low < 1 / 13 < high

    def test_reproduces_the_verdict_interval(self):
        # The interval that made n=13 inconclusive: 20.4% required sits inside.
        low, high = wilson_interval(1, 13)
        assert low < 0.204 < high

    def test_narrows_with_n(self):
        narrow = wilson_interval(8, 100)
        wide = wilson_interval(1, 13)
        assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])

    def test_stays_within_bounds_at_zero_successes(self):
        low, high = wilson_interval(0, 30)
        assert low == 0.0 and 0.0 < high < 1.0

    def test_rejects_bad_inputs(self):
        with pytest.raises(ValueError):
            wilson_interval(5, 0)
        with pytest.raises(ValueError):
            wilson_interval(11, 10)


class TestSampleSizing:
    def test_separating_close_rates_needs_more_data(self):
        assert required_sample_size(0.077, 0.204) < required_sample_size(0.18, 0.204)

    def test_planning_number_is_a_few_dozen_judged_sales(self):
        # The headline planning number: ~40 judged undescribed sales separate
        # 7.7% from 20.4%. Far fewer than a normal approximation suggests.
        n = required_sample_size(0.077, 0.204)
        assert 20 <= n <= 120

    def test_returned_size_actually_separates(self):
        n = required_sample_size(0.077, 0.204)
        low, high = wilson_interval(round(0.077 * n), n)
        assert high < 0.204

    def test_identical_rates_are_unresolvable(self):
        assert required_sample_size(0.2, 0.2, max_n=500) == 500

    def test_rejects_out_of_range(self):
        with pytest.raises(ValueError):
            required_sample_size(1.5, 0.2)


class TestCollectionPlan:
    def test_current_data_is_not_decisive(self):
        assert collection_plan(1, 13, 0.204)["currently_decisive"] is False

    def test_strong_evidence_is_decisive(self):
        assert collection_plan(2, 200, 0.204)["currently_decisive"] is True

    def test_offers_scenarios_for_both_readings(self):
        assert len(collection_plan(1, 13, 0.204)["scenarios"]) == 2


class TestMeasureFailureRate:
    CFG = BacktestConfig(min_comparables=3)

    def test_cheap_against_the_only_benchmark_is_unambiguous(self):
        sales = [_sale(d, 1200.0, "OP06-118 manga rare") for d in range(6)]
        sales.append(_sale(7, 100.0, "One Piece OP06-118"))  # no descriptor
        result = measure_failure_rate(sales, _variants(), self.CFG)
        assert result.undescribed_sales == 1
        assert result.unambiguous == 1
        assert result.lower_rate == pytest.approx(1.0)

    def test_the_real_ambiguity_is_reported_as_a_candidate_not_an_event(self):
        # The $30.64 case in full. With both a cheap and a dear variant selling,
        # an undescribed cheap sale is genuinely undecidable from the record:
        # a correctly-priced plain SEC and a missed manga rare look identical.
        sales = [_sale(d, 1200.0, "OP06-118 manga rare") for d in range(6)]
        sales += [_sale(d, 30.0, "OP06-118 SEC") for d in range(6, 12)]
        sales.append(_sale(13, 30.64, "One Piece OP06-118"))
        result = measure_failure_rate(sales, _variants(), self.CFG)
        assert result.unambiguous == 0
        assert result.candidates == 1
        # The bound spans the whole identification gap -- honest, not decisive.
        assert result.lower_rate == 0.0 and result.upper_rate == pytest.approx(1.0)

    def test_described_cheap_sale_is_not_a_listing_failure(self):
        # A cheap sale carrying a full descriptor is ordinary dispersion.
        sales = [_sale(d, 1200.0, "OP06-118 manga rare") for d in range(6)]
        sales.append(_sale(7, 100.0, "OP06-118 manga rare cheap"))
        result = measure_failure_rate(sales, _variants(), self.CFG)
        assert result.undescribed_sales == 0
        assert result.unambiguous == 0

    def test_undescribed_but_fairly_priced_is_not_an_event(self):
        sales = [_sale(d, 1200.0, "OP06-118 manga rare") for d in range(6)]
        sales.append(_sale(7, 1150.0, "One Piece OP06-118"))
        result = measure_failure_rate(sales, _variants(), self.CFG)
        assert result.undescribed_sales == 1
        assert result.unambiguous == 0 and result.candidates == 0

    def test_benchmarks_do_not_leak_the_future(self):
        # A sale must not be benchmarked against sales that happened after it.
        sales = [_sale(0, 30.0, "One Piece OP06-118")]
        sales += [_sale(d, 1200.0, "OP06-118 manga rare") for d in range(1, 8)]
        result = measure_failure_rate(sales, _variants(), self.CFG)
        assert result.total_judged == 0

    def test_insufficient_comparables_are_not_judged(self):
        sales = [_sale(0, 1200.0, "OP06-118 manga"), _sale(1, 50.0, "OP06-118")]
        result = measure_failure_rate(sales, _variants(), BacktestConfig(min_comparables=5))
        assert result.total_judged == 0
        assert result.decides_against(0.204) == "NO DATA"

    def test_verdict_decides_when_evidence_is_strong(self):
        sales = [_sale(d, 1200.0, "OP06-118 manga rare") for d in range(6)]
        sales += [_sale(10 + d, 1200.0, "One Piece OP06-118") for d in range(40)]
        result = measure_failure_rate(sales, _variants(), self.CFG)
        # Many undescribed sales, none discounted -> decisively below the bar.
        assert result.decides_against(0.204) == "DECIDES: NO-GO"

    def test_examples_carry_their_bucket_for_eyeballing(self):
        sales = [_sale(d, 1200.0, "OP06-118 manga rare") for d in range(6)]
        sales.append(_sale(7, 30.64, "One Piece OP06-118"))
        result = measure_failure_rate(sales, _variants(), self.CFG)
        title, price, benchmark, bucket = result.examples[0]
        assert price == pytest.approx(30.64)
        assert bucket == "unambiguous"
