"""Tests for the reference-price corpus.

The theme: a missing benchmark is recoverable, a wrong one produces a
confident wrong EV. Every test here is about refusing the second.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from optcg.prices import PriceCorpus, PriceQuote, corpus_from_config, load_corpus
from optcg.sources.base import DataUnavailable
from optcg.variants import Channel, Region, Treatment, Variant

CARD = "OP06-118"
FX = {"AUD": 1.0, "USD": 1.5}


def _jp_variants():
    return [
        Variant(CARD, "SEC", Treatment.BASE, Channel.BOOSTER, Region.JP, 100.0, "plain"),
        Variant(CARD, "SEC", Treatment.MANGA, Channel.BOOSTER, Region.JP, 8.0, "manga"),
    ]


def _quote(variant, price=1098.0, market=Region.JP, as_of=None, derivation="marketplace_aggregate"):
    return PriceQuote(
        variant_key=variant.key,
        price_aud=price,
        source="test",
        as_of=as_of or date.today(),
        market=market,
        derivation=derivation,
    )


class TestQuoteValidation:
    def test_rejects_non_positive_price(self):
        with pytest.raises(ValueError):
            PriceQuote("k", 0.0, "s", date.today(), Region.JP)

    def test_rejects_unknown_derivation(self):
        with pytest.raises(ValueError, match="derivation must be"):
            PriceQuote("k", 10.0, "s", date.today(), Region.JP, derivation="vibes")

    def test_marketplace_quotes_are_flagged_as_inheriting_mislabelling(self):
        assert PriceQuote("k", 10.0, "s", date.today(), Region.JP).inherits_mislabelling
        assert not PriceQuote(
            "k", 10.0, "s", date.today(), Region.JP, derivation="operator"
        ).inherits_mislabelling


class TestRegionGuard:
    def test_en_quote_does_not_price_a_jp_card(self):
        # The failure this guard exists for: EN and JP markets price the same
        # card very differently, so a cross-market quote is a different number,
        # not an approximation.
        variant = _jp_variants()[1]
        corpus = PriceCorpus([_quote(variant, market=Region.EN)])
        assert corpus.get(variant) is None

    def test_matching_market_resolves(self):
        variant = _jp_variants()[1]
        corpus = PriceCorpus([_quote(variant, market=Region.JP)])
        assert corpus.get(variant).price_aud == pytest.approx(1098.0)

    def test_mismatch_can_be_opted_into_explicitly(self):
        variant = _jp_variants()[1]
        corpus = PriceCorpus([_quote(variant, market=Region.EN)], allow_region_mismatch=True)
        assert corpus.get(variant) is not None

    def test_mismatches_are_enumerable_for_reporting(self):
        variants = _jp_variants()
        corpus = PriceCorpus([_quote(variants[1], market=Region.EN)])
        found = corpus.region_mismatches({CARD: variants})
        assert len(found) == 1 and "EN quote for a JP card" in found[0][1]


class TestStaleness:
    def test_stale_quote_is_not_returned(self):
        variant = _jp_variants()[1]
        old = date.today() - timedelta(days=200)
        corpus = PriceCorpus([_quote(variant, as_of=old)], max_age_days=60)
        assert corpus.get(variant) is None
        assert len(corpus.stale_quotes()) == 1

    def test_fresh_quote_within_window_resolves(self):
        variant = _jp_variants()[1]
        recent = date.today() - timedelta(days=10)
        corpus = PriceCorpus([_quote(variant, as_of=recent)], max_age_days=60)
        assert corpus.get(variant) is not None

    def test_freshest_quote_wins_when_several_exist(self):
        variant = _jp_variants()[1]
        corpus = PriceCorpus([
            _quote(variant, price=900.0, as_of=date.today() - timedelta(days=30)),
            _quote(variant, price=1200.0, as_of=date.today()),
        ])
        assert corpus.get(variant).price_aud == pytest.approx(1200.0)


class TestCoverageAndWarnings:
    def test_coverage_splits_priced_from_unpriced(self):
        variants = _jp_variants()
        corpus = PriceCorpus([_quote(variants[1])])
        covered, missing = corpus.coverage({CARD: variants})
        assert covered == [variants[1].key]
        assert missing == [variants[0].key]

    def test_circularity_warning_fires_on_aggregate_quotes(self):
        corpus = PriceCorpus([_quote(_jp_variants()[1])])
        warning = corpus.circularity_warning()
        assert warning and "FLOOR" in warning

    def test_no_circularity_warning_for_operator_prices(self):
        corpus = PriceCorpus([_quote(_jp_variants()[1], derivation="operator")])
        assert corpus.circularity_warning() is None

    def test_config_prices_are_operator_derived_and_never_stale(self):
        variants = _jp_variants()
        corpus = corpus_from_config({variants[1].key: 1098.0}, {CARD: variants})
        assert corpus.circularity_warning() is None
        assert corpus.get(variants[1]) is not None

    def test_config_prices_take_market_from_the_variant(self):
        variants = _jp_variants()
        corpus = corpus_from_config({variants[1].key: 1098.0}, {CARD: variants})
        assert corpus.get(variants[1]).market is Region.JP


class TestLoadCorpus:
    def _write(self, tmp_path, body):
        path = tmp_path / "prices.csv"
        path.write_text(body, encoding="utf-8")
        return path

    def test_loads_and_converts_currency(self, tmp_path):
        path = self._write(
            tmp_path,
            "card_number,treatment,price,currency,market,as_of\n"
            f"OP06-118,manga,US $700.00,USD,JP,{date.today().isoformat()}\n",
        )
        corpus = load_corpus(path, {CARD: _jp_variants()}, FX)
        assert corpus.get(_jp_variants()[1]).price_aud == pytest.approx(1050.0)

    def test_untracked_cards_are_skipped_quietly(self, tmp_path):
        path = self._write(
            tmp_path,
            "card_number,treatment,price,currency,market,as_of\n"
            f"ST01-012,base,US $10.00,USD,EN,{date.today().isoformat()}\n"
            f"OP06-118,manga,US $700.00,USD,JP,{date.today().isoformat()}\n",
        )
        assert len(load_corpus(path, {CARD: _jp_variants()}, FX)) == 1

    def test_missing_fx_rate_is_an_error(self, tmp_path):
        path = self._write(
            tmp_path,
            "card_number,treatment,price,currency,market,as_of\n"
            f"OP06-118,manga,£700.00,GBP,JP,{date.today().isoformat()}\n",
        )
        with pytest.raises(DataUnavailable, match="no FX rate"):
            load_corpus(path, {CARD: _jp_variants()}, FX)

    def test_no_matching_cards_raises(self, tmp_path):
        path = self._write(
            tmp_path,
            "card_number,treatment,price,currency,market,as_of\n"
            f"ST01-012,base,US $10.00,USD,EN,{date.today().isoformat()}\n",
        )
        with pytest.raises(DataUnavailable, match="no quotes"):
            load_corpus(path, {CARD: _jp_variants()}, FX)
