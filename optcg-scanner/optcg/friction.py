"""Round-trip cost model, in AUD.

The brief supplies blended round-trip figures (~30% / ~20% / ~8%). Those are
useful as a sanity check but they are the wrong thing to compute with, because
a flat percentage hides the fact that fixed costs dominate at low price points.
A A$12 postage leg is 24% of a A$50 card and 0.4% of a A$3,000 one. So this
module models the components explicitly and exposes
``implied_round_trip_rate`` to cross-check against the blended numbers.

Components modelled:
  * fixed postage each way
  * import GST at 10%, applied at all values (eBay collects at checkout below
    A$1,000; charged at the border above)
  * formal customs clearance above A$1,000
  * marketplace selling fees
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SaleChannel(str, Enum):
    OVERSEAS_TO_EBAY_AU = "overseas_to_ebay_au"
    OVERSEAS_TO_LOCAL = "overseas_to_local"
    LOCAL_TO_LOCAL = "local_to_local"


@dataclass(frozen=True)
class FrictionConfig:
    inbound_postage_aud: float = 12.0
    outbound_postage_aud: float = 12.0
    import_gst_rate: float = 0.10
    customs_threshold_aud: float = 1000.0
    # Formal clearance runs ~A$50-90 above the threshold; midpoint by default.
    customs_clearance_aud: float = 70.0
    # eBay AU final value fee incl. payment processing.
    ebay_au_fee_rate: float = 0.13
    # Fairs / FB groups: no platform fee, but assume a small cash discount.
    local_fee_rate: float = 0.0

    def is_overseas(self, channel: SaleChannel) -> bool:
        return channel in (
            SaleChannel.OVERSEAS_TO_EBAY_AU,
            SaleChannel.OVERSEAS_TO_LOCAL,
        )


@dataclass(frozen=True)
class TradeEconomics:
    ask_aud: float
    expected_sale_aud: float
    channel: SaleChannel
    landed_cost_aud: float
    net_proceeds_aud: float

    @property
    def net_ev_aud(self) -> float:
        return self.net_proceeds_aud - self.landed_cost_aud

    @property
    def net_margin_on_cost(self) -> float:
        if self.landed_cost_aud <= 0:
            return 0.0
        return self.net_ev_aud / self.landed_cost_aud

    @property
    def implied_round_trip_rate(self) -> float:
        """Total friction as a fraction of the expected sale price.

        Comparable to the brief's blended ~30% / ~20% / ~8% figures.
        """
        if self.expected_sale_aud <= 0:
            return 0.0
        friction = (self.landed_cost_aud - self.ask_aud) + (
            self.expected_sale_aud - self.net_proceeds_aud
        )
        return friction / self.expected_sale_aud


def landed_cost(ask_aud: float, channel: SaleChannel, cfg: FrictionConfig) -> float:
    """Total cash out to acquire the card and get it in hand."""
    if ask_aud < 0:
        raise ValueError("ask_aud must be non-negative")

    cost = ask_aud
    if cfg.is_overseas(channel):
        cost += cfg.inbound_postage_aud
        # GST applies to the value of the taxable importation: goods + transport.
        cost += cfg.import_gst_rate * (ask_aud + cfg.inbound_postage_aud)
        if ask_aud + cfg.inbound_postage_aud > cfg.customs_threshold_aud:
            cost += cfg.customs_clearance_aud
    else:
        # Domestic purchase still has to reach you unless collected in person.
        cost += cfg.inbound_postage_aud
    return cost


def net_proceeds(sale_aud: float, channel: SaleChannel, cfg: FrictionConfig) -> float:
    """Cash in after selling fees and outbound postage."""
    if sale_aud < 0:
        raise ValueError("sale_aud must be non-negative")

    if channel is SaleChannel.OVERSEAS_TO_EBAY_AU:
        fee_rate = cfg.ebay_au_fee_rate
    else:
        fee_rate = cfg.local_fee_rate

    proceeds = sale_aud * (1.0 - fee_rate)
    proceeds -= cfg.outbound_postage_aud
    return proceeds


def evaluate_trade(
    ask_aud: float,
    expected_sale_aud: float,
    channel: SaleChannel,
    cfg: FrictionConfig | None = None,
) -> TradeEconomics:
    """Full economics of buying at ``ask_aud`` and selling at ``expected_sale_aud``."""
    cfg = cfg or FrictionConfig()
    return TradeEconomics(
        ask_aud=ask_aud,
        expected_sale_aud=expected_sale_aud,
        channel=channel,
        landed_cost_aud=landed_cost(ask_aud, channel, cfg),
        net_proceeds_aud=net_proceeds(expected_sale_aud, channel, cfg),
    )


def breakeven_value_ratio(
    ask_aud: float,
    channel: SaleChannel,
    cfg: FrictionConfig | None = None,
) -> float:
    """How many times the ask a card must truly be worth to break even.

    This is the right friction test for a CROSS-VARIANT trade -- buying at the
    price of the variant the seller claims, and selling at the price of the
    variant the card actually is. ``breakeven_discount`` answers a different
    question (buying below a variant's own median) and is far more pessimistic
    at low prices, because it assumes the upside is capped by that same
    variant's value.

    The distinction matters in the expensive direction. A $25 ask on a card
    truly worth $1,500 nets over $1,200; judged by ``breakeven_discount`` the
    same price point looks arithmetically dead. Using the discount test to set a
    minimum ask would filter out the best trades this strategy can find.

    Returns the multiple of ``ask_aud`` at which net EV is exactly zero. Lower
    is better. Note it is not monotonic: it falls as fixed costs amortise, then
    rises again once the ask crosses the customs threshold.
    """
    cfg = cfg or FrictionConfig()
    if ask_aud <= 0:
        raise ValueError("ask_aud must be positive")

    cost = landed_cost(ask_aud, channel, cfg)
    fee_rate = (
        cfg.ebay_au_fee_rate
        if channel is SaleChannel.OVERSEAS_TO_EBAY_AU
        else cfg.local_fee_rate
    )
    # cost = sale * (1 - fee_rate) - outbound_postage  =>  solve for sale
    sale = (cost + cfg.outbound_postage_aud) / (1.0 - fee_rate)
    return sale / ask_aud


def breakeven_discount(
    median_aud: float,
    channel: SaleChannel,
    cfg: FrictionConfig | None = None,
) -> float:
    """Discount to median at which a trade exactly breaks even.

    Returns a fraction in [0, 1]; 0.25 means "must buy 25% below median". This
    is the number that decides whether the strategy is viable at a given price
    point, and it rises sharply as the price falls because fixed costs stop
    amortising.
    """
    cfg = cfg or FrictionConfig()
    if median_aud <= 0:
        raise ValueError("median_aud must be positive")

    target_proceeds = net_proceeds(median_aud, channel, cfg)

    # Invert landed_cost analytically rather than searching.
    if cfg.is_overseas(channel):
        gst = cfg.import_gst_rate
        # target = ask + post + gst*(ask + post) [+ clearance]
        # Solve without clearance first, then re-check the threshold.
        ask = (target_proceeds - cfg.inbound_postage_aud * (1 + gst)) / (1 + gst)
        if ask + cfg.inbound_postage_aud > cfg.customs_threshold_aud:
            ask = (
                target_proceeds
                - cfg.customs_clearance_aud
                - cfg.inbound_postage_aud * (1 + gst)
            ) / (1 + gst)
    else:
        ask = target_proceeds - cfg.inbound_postage_aud

    if ask <= 0:
        return 1.0
    return max(0.0, 1.0 - ask / median_aud)
