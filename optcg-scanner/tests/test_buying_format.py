"""Tests for buying format -- whether a mispricing is actually capturable.

The load-bearing case is ``test_open_auction_is_not_treated_as_a_bargain``: a
fresh auction's low current bid is not a price, and valuing one as if it were
would flood the output with listings destined to close at fair value.
"""

from __future__ import annotations

import pytest

from optcg.friction import FrictionConfig, SaleChannel
from optcg.scan import run_scan
from optcg.sources.base import ActiveListing
from optcg.variants import Channel, Region, Treatment, Variant
from optcg.vision import CardRead, VisionResult

CARD = "OP06-118"
CFG = FrictionConfig()


def _variants():
    return [
        Variant(CARD, "SEC", Treatment.BASE, Channel.BOOSTER, Region.JP, 100.0, "plain"),
        Variant(CARD, "SEC", Treatment.PARALLEL, Channel.BOOSTER, Region.JP, 30.0, "parallel"),
        Variant(CARD, "SEC", Treatment.COMIC, Channel.BOOSTER, Region.JP, 2.0, "comic"),
    ]


def _prices():
    return {v.key: p for v, p in zip(_variants(), [25.0, 70.0, 1500.0])}


def _listing(listing_id="1", ask=200.0, options=("FIXED_PRICE",), bids=None):
    return ActiveListing(
        listing_id=listing_id,
        card_number=CARD,
        title="OP06-118 Zoro parallel",
        ask_aud=ask,
        url="https://ebay/1",
        image_urls=("https://img/1.jpg",),
        buying_options=options,
        bid_count=bids,
    )


class _Reader:
    def read_listing(self, urls):
        return VisionResult(
            read=CardRead(treatment="comic", confidence=0.9),
            image_url=urls[0],
            from_cache=False,
        )


class TestBuyingOptions:
    def test_fixed_price_is_immediately_buyable(self):
        assert _listing(options=("FIXED_PRICE",)).is_immediately_buyable

    def test_pure_auction_is_not(self):
        assert not _listing(options=("AUCTION",)).is_immediately_buyable

    def test_auction_with_buy_now_is_buyable_until_someone_bids(self):
        both = ("AUCTION", "FIXED_PRICE")
        assert _listing(options=both, bids=0).is_immediately_buyable
        # eBay withdraws the buy-now option once bidding starts.
        assert not _listing(options=both, bids=3).is_immediately_buyable

    def test_unknown_format_is_not_assumed_takeable(self):
        # An unfamiliar enum value should read as un-takeable, not as a bargain.
        assert not _listing(options=("SOMETHING_NEW",)).is_immediately_buyable
        assert not _listing(options=()).is_immediately_buyable

    def test_best_offer_means_the_ask_is_a_ceiling(self):
        listing = _listing(options=("FIXED_PRICE", "BEST_OFFER"))
        assert listing.accepts_best_offer
        assert listing.is_immediately_buyable
        assert not listing.price_is_firm

    def test_plain_fixed_price_is_firm(self):
        assert _listing(options=("FIXED_PRICE",)).price_is_firm


class TestOpportunityFiltering:
    def _report(self, listings):
        source = type("S", (), {"fetch_active": lambda self, card: listings})()
        return run_scan(source, {CARD: _variants()}, _prices(), _Reader())

    def test_fixed_price_mispricing_is_an_opportunity(self):
        report = self._report([_listing(options=("FIXED_PRICE",))])
        found = report.opportunities(
            {CARD: _variants()}, _prices(), SaleChannel.OVERSEAS_TO_EBAY_AU, CFG
        )
        assert len(found) == 1

    def test_open_auction_is_not_treated_as_a_bargain(self):
        # THE TRAP: a comic parallel sitting at $20 with days to run is not a
        # $20 card. Treating the current bid as the ask marks every fresh
        # auction as a windfall.
        report = self._report([_listing(ask=20.0, options=("AUCTION",), bids=2)])
        found = report.opportunities(
            {CARD: _variants()}, _prices(), SaleChannel.OVERSEAS_TO_EBAY_AU, CFG
        )
        assert found == []

    def test_auctions_can_be_included_explicitly(self):
        report = self._report([_listing(ask=20.0, options=("AUCTION",), bids=2)])
        found = report.opportunities(
            {CARD: _variants()},
            _prices(),
            SaleChannel.OVERSEAS_TO_EBAY_AU,
            CFG,
            include_auctions=True,
        )
        assert len(found) == 1

    def test_auction_watchlist_returns_value_not_ev(self):
        report = self._report([_listing(ask=20.0, options=("AUCTION",), bids=2)])
        watch = report.auction_watchlist({CARD: _variants()}, _prices())
        assert len(watch) == 1
        # The true variant's reference value -- a bid ceiling, not a profit.
        assert watch[0][1] == pytest.approx(1500.0)

    def test_fixed_price_listings_are_not_on_the_auction_watchlist(self):
        report = self._report([_listing(options=("FIXED_PRICE",))])
        assert report.auction_watchlist({CARD: _variants()}, _prices()) == []

    def test_correctly_labelled_auction_is_not_watched(self):
        listings = [
            ActiveListing(
                listing_id="2",
                card_number=CARD,
                title="OP06-118 Zoro comic parallel red",
                ask_aud=20.0,
                url="https://ebay/2",
                image_urls=("https://img/2.jpg",),
                buying_options=("AUCTION",),
                bid_count=1,
            )
        ]
        report = self._report(listings)
        assert report.auction_watchlist({CARD: _variants()}, _prices()) == []
