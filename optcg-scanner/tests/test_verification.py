"""Tests for mislabel-rate measurement against image ground truth."""

from __future__ import annotations

from datetime import date

import pytest

from optcg.sources.base import SoldSale
from optcg.variants import Channel, Region, Treatment, Variant
from optcg.verification import VerifiedSale, measure_mislabel_rate, parse_verified

CARD = "OP06-118"


def _variants():
    return [
        Variant(CARD, "SEC", Treatment.BASE, Channel.BOOSTER, Region.JP, 100.0, "plain SEC"),
        Variant(CARD, "SEC", Treatment.PARALLEL, Channel.BOOSTER, Region.JP, 30.0, "parallel"),
        Variant(CARD, "SEC", Treatment.MANGA, Channel.BOOSTER, Region.JP, 8.0, "manga"),
        Variant(CARD, "SEC", Treatment.COMIC, Channel.BOOSTER, Region.JP, 2.0, "comic"),
    ]


def _prices():
    return {v.key: p for v, p in zip(_variants(), [25.0, 70.0, 1098.0, 1500.0])}


def _sale(price, title):
    return SoldSale(CARD, date(2026, 3, 1), price, title)


def _key(index):
    return _variants()[index].key


class TestMislabelDirection:
    def test_under_claimed_and_underpriced_is_exploitable(self):
        # Comic parallel described as "parallel", sold for $200 against a
        # $1,500 true median. This is the trade.
        verified = [VerifiedSale(_sale(200.0, "OP06-118 Zoro parallel"), _key(3))]
        report = measure_mislabel_rate(verified, _variants(), _prices())
        assert report.under_claimed == 1
        assert report.exploitable == 1
        assert report.median_gap_aud == pytest.approx(1300.0)

    def test_under_claimed_but_fully_priced_is_not_exploitable(self):
        # Bad title, but the bidders saw the photo and paid up anyway. This is
        # why the raw mislabel rate overstates the opportunity.
        verified = [VerifiedSale(_sale(1600.0, "OP06-118 Zoro parallel"), _key(3))]
        report = measure_mislabel_rate(verified, _variants(), _prices())
        assert report.under_claimed == 1
        assert report.exploitable == 0

    def test_over_claimed_is_the_trap_not_the_edge(self):
        # Title says manga; it is actually a plain SEC. Buying this loses money.
        verified = [VerifiedSale(_sale(900.0, "OP06-118 manga rare"), _key(0))]
        report = measure_mislabel_rate(verified, _variants(), _prices())
        assert report.over_claimed == 1
        assert report.exploitable == 0

    def test_correct_label_counted_as_correct(self):
        verified = [VerifiedSale(_sale(1500.0, "OP06-118 comic parallel red"), _key(3))]
        report = measure_mislabel_rate(verified, _variants(), _prices())
        assert report.correct == 1
        assert report.mislabel_rate == 0.0


class TestRates:
    def _mixed(self):
        return [
            VerifiedSale(_sale(200.0, "OP06-118 Zoro parallel"), _key(3)),  # exploitable
            VerifiedSale(_sale(1600.0, "OP06-118 parallel"), _key(3)),  # under, priced
            VerifiedSale(_sale(900.0, "OP06-118 manga rare"), _key(0)),  # over-claimed
            VerifiedSale(_sale(1100.0, "OP06-118 manga rare"), _key(2)),  # correct
        ]

    def test_rates_are_distinguished(self):
        report = measure_mislabel_rate(self._mixed(), _variants(), _prices())
        assert report.total == 4
        assert report.mislabel_rate == pytest.approx(0.75)
        assert report.under_claim_rate == pytest.approx(0.5)
        # Only one of four is actually tradeable -- the number that matters.
        assert report.exploitable_rate == pytest.approx(0.25)

    def test_exploitable_rate_is_below_mislabel_rate(self):
        report = measure_mislabel_rate(self._mixed(), _variants(), _prices())
        assert report.exploitable_rate < report.mislabel_rate

    def test_interval_widens_with_small_samples(self):
        report = measure_mislabel_rate(self._mixed(), _variants(), _prices())
        low, high = report.exploitable_interval
        assert low < report.exploitable_rate < high

    def test_empty_input_is_no_data(self):
        report = measure_mislabel_rate([], _variants(), _prices())
        assert report.decides_against(0.204) == "NO DATA"

    def test_decides_no_go_on_strong_negative_evidence(self):
        verified = [
            VerifiedSale(_sale(1100.0, "OP06-118 manga rare"), _key(2)) for _ in range(60)
        ]
        report = measure_mislabel_rate(verified, _variants(), _prices())
        assert report.decides_against(0.204) == "DECIDES: NO-GO"

    def test_missing_reference_price_does_not_pick_a_direction(self):
        verified = [VerifiedSale(_sale(200.0, "OP06-118 parallel"), _key(3))]
        report = measure_mislabel_rate(verified, _variants(), {})
        assert report.total == 1
        assert report.under_claimed == 0 and report.over_claimed == 0


class TestParseVerified:
    def test_matches_by_label(self):
        sales = [_sale(200.0, "OP06-118 Zoro parallel")]
        verified = parse_verified(sales, {"OP06-118 Zoro parallel": "comic"}, _variants())
        assert verified[0].true_variant_key == _key(3)

    def test_matches_by_key(self):
        sales = [_sale(200.0, "OP06-118 Zoro parallel")]
        verified = parse_verified(sales, {"OP06-118 Zoro parallel": _key(3)}, _variants())
        assert verified[0].true_variant_key == _key(3)

    def test_unannotated_sales_are_skipped(self):
        sales = [_sale(200.0, "a"), _sale(300.0, "b")]
        assert len(parse_verified(sales, {"a": "comic"}, _variants())) == 1

    def test_typo_raises_rather_than_silently_dropping(self):
        sales = [_sale(200.0, "a")]
        with pytest.raises(ValueError, match="unrecognised variant label"):
            parse_verified(sales, {"a": "comik"}, _variants())
