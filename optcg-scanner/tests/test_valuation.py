"""Tests for valuation under variant uncertainty.

The headline test is ``test_cheapness_screen_would_discard_the_prize``: it
encodes the bug that motivated this module, so a future refactor that
reintroduces cheapness-first ranking fails loudly.
"""

from __future__ import annotations

import pytest

from optcg.friction import FrictionConfig, SaleChannel
from optcg.valuation import triage_rank, value_listing
from optcg.variants import Channel, Region, Treatment, Variant, infer_variant

CARD = "OP06-118"
CFG = FrictionConfig()


def _variants():
    return [
        Variant(CARD, "SEC", Treatment.BASE, Channel.BOOSTER, Region.JP, 100.0, "plain SEC"),
        Variant(CARD, "SEC", Treatment.PARALLEL, Channel.BOOSTER, Region.JP, 30.0, "parallel"),
        Variant(CARD, "SEC", Treatment.MANGA, Channel.BOOSTER, Region.JP, 8.0, "manga"),
        Variant(CARD, "SEC", Treatment.COMIC, Channel.BOOSTER, Region.JP, 2.0, "comic"),
    ]


def _prices():
    return {v.key: p for v, p in zip(_variants(), [25.0, 70.0, 1098.0, 1500.0])}


class TestValuation:
    def test_blind_ev_weights_the_whole_posterior(self):
        posterior = infer_variant("OP06-118 parallel", _variants())
        valuation = value_listing(posterior, _prices(), ask_aud=200.0, cfg=CFG)
        # Bounded by the best and worst live outcomes.
        assert valuation.downside_ev_aud < valuation.blind_ev_aud < valuation.upside_ev_aud

    def test_upside_uses_the_dearest_live_variant(self):
        posterior = infer_variant("OP06-118 parallel", _variants())
        valuation = value_listing(posterior, _prices(), ask_aud=200.0, cfg=CFG)
        assert "comic" in valuation.best_variant_label

    def test_price_dispersion_reported(self):
        posterior = infer_variant("One Piece OP06-118", _variants())
        valuation = value_listing(posterior, _prices(), ask_aud=100.0, cfg=CFG)
        assert valuation.price_dispersion == pytest.approx(1500.0 / 25.0)

    def test_missing_reference_price_raises_rather_than_guessing(self):
        posterior = infer_variant("OP06-118 comic parallel", _variants())
        with pytest.raises(ValueError, match="inventing a benchmark"):
            value_listing(posterior, {}, ask_aud=200.0, cfg=CFG)


class TestTheCheapnessScreenBug:
    def test_cheapness_screen_would_discard_the_prize(self):
        # A comic parallel (~$1,500) listed as "parallel" and asked at $200.
        # Against the standard-parallel median of $70 the ask is ~3x too dear,
        # so any cheapness screen drops it. It is the listing worth having.
        posterior = infer_variant("OP06-118 Zoro parallel", _variants())
        valuation = value_listing(posterior, _prices(), ask_aud=200.0, cfg=CFG)

        claimed_median = _prices()[posterior.most_likely.key]
        assert 200.0 > claimed_median  # looks expensive against the claim
        assert valuation.information_value_aud > 0  # yet worth checking
        assert valuation.is_hidden_by_cheapness_screen

    def test_correctly_labelled_cheap_card_is_still_valued(self):
        # The other opportunity type must not be lost by the fix.
        posterior = infer_variant("OP06-118 comic parallel red", _variants())
        valuation = value_listing(posterior, _prices(), ask_aud=300.0, cfg=CFG)
        assert valuation.blind_ev_aud > 0
        assert not valuation.is_hidden_by_cheapness_screen

    def test_information_is_worthless_when_the_variant_is_pinned(self):
        # A fully described listing at a fair price: nothing to learn.
        posterior = infer_variant("OP06-118 comic parallel red", _variants())
        valuation = value_listing(posterior, _prices(), ask_aud=1400.0, cfg=CFG)
        assert valuation.information_value_aud == pytest.approx(0.0, abs=1.0)

    def test_information_value_is_zero_when_upside_cannot_pay(self):
        # Ask above even the dearest variant: no photo changes the answer.
        posterior = infer_variant("One Piece OP06-118", _variants())
        valuation = value_listing(posterior, _prices(), ask_aud=5000.0, cfg=CFG)
        assert valuation.information_value_aud == 0.0


class TestTriage:
    def test_ranks_by_information_value_not_cheapness(self):
        prices = _prices()
        listings = [
            ("mislabelled comic at $200", infer_variant("OP06-118 parallel", _variants()), 200.0),
            ("plain sec at $5", infer_variant("OP06-118 SEC", _variants()), 5.0),
            ("fully described comic at fair price", infer_variant(
                "OP06-118 comic parallel red", _variants()), 1400.0),
        ]
        valuations = [
            (name, value_listing(p, prices, ask, cfg=CFG)) for name, p, ask in listings
        ]
        ranked = triage_rank(valuations)
        # The mislabelled dear card must come first -- it is the one a photo
        # check would change the decision on.
        assert ranked[0][0] == "mislabelled comic at $200"

    def test_limit_is_respected(self):
        prices = _prices()
        valuations = [
            (str(ask), value_listing(infer_variant("OP06-118 parallel", _variants()), prices, ask))
            for ask in (100.0, 200.0, 300.0)
        ]
        assert len(triage_rank(valuations, limit=2)) == 2

    def test_local_channel_raises_information_value(self):
        posterior = infer_variant("OP06-118 parallel", _variants())
        overseas = value_listing(
            posterior, _prices(), 200.0, SaleChannel.OVERSEAS_TO_EBAY_AU, CFG
        )
        local = value_listing(posterior, _prices(), 200.0, SaleChannel.LOCAL_TO_LOCAL, CFG)
        assert local.information_value_aud > overseas.information_value_aud
