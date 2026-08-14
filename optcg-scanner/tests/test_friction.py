"""Tests for the round-trip cost model."""

from __future__ import annotations

import pytest

from optcg.friction import (
    FrictionConfig,
    SaleChannel,
    breakeven_discount,
    evaluate_trade,
    landed_cost,
    net_proceeds,
)

CFG = FrictionConfig()


class TestLandedCost:
    def test_overseas_adds_postage_and_gst(self):
        # 500 + 12 postage + 10% GST on 512 = 563.20
        assert landed_cost(500.0, SaleChannel.OVERSEAS_TO_EBAY_AU, CFG) == pytest.approx(563.20)

    def test_customs_clearance_above_threshold(self):
        below = landed_cost(980.0, SaleChannel.OVERSEAS_TO_EBAY_AU, CFG)
        above = landed_cost(1000.0, SaleChannel.OVERSEAS_TO_EBAY_AU, CFG)
        # A A$20 higher ask costs far more than A$20 once clearance triggers.
        assert above - below > 70.0

    def test_gst_applies_at_all_values(self):
        # Low-value imports are not exempt; eBay collects at checkout.
        cost = landed_cost(50.0, SaleChannel.OVERSEAS_TO_LOCAL, CFG)
        assert cost == pytest.approx(50.0 + 12.0 + 6.2)

    def test_local_has_no_gst_or_customs(self):
        assert landed_cost(500.0, SaleChannel.LOCAL_TO_LOCAL, CFG) == pytest.approx(512.0)

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            landed_cost(-1.0, SaleChannel.LOCAL_TO_LOCAL, CFG)


class TestNetProceeds:
    def test_ebay_au_fee_and_postage(self):
        assert net_proceeds(1000.0, SaleChannel.OVERSEAS_TO_EBAY_AU, CFG) == pytest.approx(858.0)

    def test_local_sale_has_no_platform_fee(self):
        assert net_proceeds(1000.0, SaleChannel.LOCAL_TO_LOCAL, CFG) == pytest.approx(988.0)


class TestRoundTrip:
    def test_implied_rate_brackets_the_briefs_blended_figures(self):
        # The brief quotes ~30% / ~20% / ~8%. The component model should land in
        # the same neighbourhood at a typical price point; large divergence would
        # mean the components are miscalibrated.
        expected = {
            SaleChannel.OVERSEAS_TO_EBAY_AU: 0.30,
            SaleChannel.OVERSEAS_TO_LOCAL: 0.20,
            SaleChannel.LOCAL_TO_LOCAL: 0.08,
        }
        for channel, blended in expected.items():
            trade = evaluate_trade(1000.0, 1100.0, channel, CFG)
            assert trade.implied_round_trip_rate == pytest.approx(blended, abs=0.10)

    def test_the_disproven_trade_is_negative(self):
        # The brief's finding: buy at the 25th percentile ($960), sell at the
        # median ($1,098) -- +14% gross -- does not survive friction.
        trade = evaluate_trade(960.0, 1098.0, SaleChannel.OVERSEAS_TO_EBAY_AU, CFG)
        assert trade.net_ev_aud < 0

    def test_deep_tail_clears_where_the_quartile_trade_does_not(self):
        quartile = evaluate_trade(960.0, 1098.0, SaleChannel.OVERSEAS_TO_EBAY_AU, CFG)
        deep = evaluate_trade(1098.0 * 0.5, 1098.0, SaleChannel.OVERSEAS_TO_EBAY_AU, CFG)
        assert quartile.net_ev_aud < 0 < deep.net_ev_aud

    def test_local_channel_dominates(self):
        buy, sell = 700.0, 1098.0
        overseas = evaluate_trade(buy, sell, SaleChannel.OVERSEAS_TO_EBAY_AU, CFG)
        local = evaluate_trade(buy, sell, SaleChannel.LOCAL_TO_LOCAL, CFG)
        assert local.net_ev_aud > overseas.net_ev_aud


class TestBreakevenDiscount:
    def test_fixed_costs_dominate_at_low_prices(self):
        cheap = breakeven_discount(50.0, SaleChannel.OVERSEAS_TO_EBAY_AU, CFG)
        rich = breakeven_discount(3000.0, SaleChannel.OVERSEAS_TO_EBAY_AU, CFG)
        assert cheap > rich
        # A A$50 card needs an implausible discount to be worth shipping.
        assert cheap > 0.5

    def test_local_breakeven_is_easiest(self):
        price = 1000.0
        assert breakeven_discount(price, SaleChannel.LOCAL_TO_LOCAL, CFG) < breakeven_discount(
            price, SaleChannel.OVERSEAS_TO_EBAY_AU, CFG
        )

    def test_breakeven_is_actually_breakeven(self):
        # Buying at exactly the breakeven discount should yield ~zero EV.
        for price in (300.0, 1500.0, 3000.0):
            for channel in SaleChannel:
                discount = breakeven_discount(price, channel, CFG)
                trade = evaluate_trade(price * (1 - discount), price, channel, CFG)
                assert trade.net_ev_aud == pytest.approx(0.0, abs=0.01)

    def test_rejects_non_positive_median(self):
        with pytest.raises(ValueError):
            breakeven_discount(0.0, SaleChannel.LOCAL_TO_LOCAL, CFG)
