"""Tests for the live scan path.

Listings and vision reads here are fakes exercising scan mechanics. No network
call is made and no market conclusion is drawn.
"""

from __future__ import annotations

import pytest

from optcg.friction import FrictionConfig, SaleChannel
from optcg.scan import ScanReport, run_scan, scan_card
from optcg.sources.base import ActiveListing
from optcg.variants import Channel, Region, Treatment, Variant
from optcg.vision import CardRead, VisionResult

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


def _listing(listing_id, title, ask, images=("https://img/1.jpg",), options=("FIXED_PRICE",)):
    # Buying format is specified because an unknown format is deliberately not
    # treated as takeable -- see tests/test_buying_format.py.
    return ActiveListing(
        listing_id=listing_id,
        card_number=CARD,
        title=title,
        ask_aud=ask,
        url=f"https://ebay/{listing_id}",
        image_urls=images,
        buying_options=options,
    )


class _FakeReader:
    """Returns a scripted treatment per image URL."""

    def __init__(self, treatment: str | None, confidence: float = 0.9):
        self.treatment = treatment
        self.confidence = confidence
        self.calls = 0

    def read_listing(self, image_urls):
        self.calls += 1
        read = CardRead(treatment=self.treatment, confidence=self.confidence)
        return VisionResult(read=read, image_url=image_urls[0], from_cache=False)


class TestScanCard:
    def test_graded_listings_are_skipped(self):
        listings = [_listing("1", "OP06-118 Zoro PSA 10", 3000.0)]
        assert scan_card(CARD, listings, _variants(), _prices()) == []

    def test_listing_without_reference_price_is_skipped(self):
        listings = [_listing("1", "OP06-118 Zoro", 100.0)]
        assert scan_card(CARD, listings, _variants(), {}) == []

    def test_vision_runs_within_budget(self):
        listings = [_listing(str(i), "OP06-118 Zoro parallel", 200.0) for i in range(5)]
        reader = _FakeReader("comic")
        scan_card(CARD, listings, _variants(), _prices(), reader, vision_budget=2)
        assert reader.calls == 2

    def test_no_reader_means_no_vision(self):
        listings = [_listing("1", "OP06-118 Zoro parallel", 200.0)]
        scanned = scan_card(CARD, listings, _variants(), _prices())
        assert scanned[0].vision is None
        assert scanned[0].is_mislabelled is None


class TestMislabelDetection:
    def _scan(self, title, ask, treatment):
        listings = [_listing("1", title, ask)]
        return scan_card(
            CARD, listings, _variants(), _prices(), _FakeReader(treatment)
        )[0]

    def test_photo_disagreeing_with_title_is_mislabelled(self):
        item = self._scan("OP06-118 Zoro parallel", 200.0, "comic")
        assert item.title_claim == "parallel"
        assert item.verified_treatment == "comic"
        assert item.is_mislabelled is True

    def test_photo_agreeing_with_title_is_not_mislabelled(self):
        item = self._scan("OP06-118 Zoro comic parallel red", 1400.0, "comic")
        assert item.is_mislabelled is False

    def test_undeterminable_photo_yields_none(self):
        item = self._scan("OP06-118 Zoro parallel", 200.0, None)
        assert item.is_mislabelled is None
        assert item.verified_treatment is None

    def test_realised_ev_prices_at_what_the_photo_shows(self):
        # THE TRADE: listed as "parallel" at $200, photo shows a comic parallel
        # worth ~$1,500. Only computable after the photo check.
        item = self._scan("OP06-118 Zoro parallel", 200.0, "comic")
        ev = item.realised_ev(
            _variants(), _prices(), SaleChannel.OVERSEAS_TO_EBAY_AU, CFG
        )
        assert ev is not None and ev > 800.0

    def test_realised_ev_is_negative_when_photo_shows_the_cheap_variant(self):
        # The trap: title says manga, photo says plain SEC. Buying loses money.
        item = self._scan("OP06-118 Zoro manga rare", 900.0, "base")
        ev = item.realised_ev(
            _variants(), _prices(), SaleChannel.OVERSEAS_TO_EBAY_AU, CFG
        )
        assert ev is not None and ev < 0

    def test_realised_ev_is_none_without_vision(self):
        listings = [_listing("1", "OP06-118 Zoro parallel", 200.0)]
        item = scan_card(CARD, listings, _variants(), _prices())[0]
        assert item.realised_ev(_variants(), _prices(), SaleChannel.LOCAL_TO_LOCAL, CFG) is None


class TestScanReport:
    def _source(self, listings):
        return type("S", (), {"fetch_active": lambda self, card: listings})()

    def test_counts_verified_and_mislabelled(self):
        listings = [_listing(str(i), "OP06-118 Zoro parallel", 200.0) for i in range(3)]
        report = run_scan(
            self._source(listings),
            {CARD: _variants()},
            _prices(),
            _FakeReader("comic"),
        )
        assert report.verified == 3
        assert report.mislabelled == 3
        assert report.mislabel_rate == pytest.approx(1.0)

    def test_undeterminable_reads_are_counted_separately(self):
        listings = [_listing("1", "OP06-118 Zoro parallel", 200.0)]
        report = run_scan(
            self._source(listings), {CARD: _variants()}, _prices(), _FakeReader(None)
        )
        assert report.undeterminable == 1
        assert report.verified == 0

    def test_opportunities_ranked_by_realised_ev(self):
        listings = [
            _listing("cheap", "OP06-118 Zoro parallel", 200.0),
            _listing("dear", "OP06-118 Zoro parallel", 900.0),
        ]
        report = run_scan(
            self._source(listings),
            {CARD: _variants()},
            _prices(),
            _FakeReader("comic"),
        )
        found = report.opportunities(
            {CARD: _variants()}, _prices(), SaleChannel.OVERSEAS_TO_EBAY_AU, CFG
        )
        assert found[0][0].listing.listing_id == "cheap"
        assert found[0][1] > found[1][1]

    def test_no_data_verdict_on_empty_scan(self):
        report = ScanReport()
        assert report.decides_against(0.204) == "NO DATA"
        assert report.mislabel_interval == (0.0, 1.0)

    def test_verdict_decides_on_strong_evidence(self):
        listings = [
            _listing(str(i), "OP06-118 Zoro comic parallel red", 1400.0) for i in range(60)
        ]
        report = run_scan(
            self._source(listings),
            {CARD: _variants()},
            _prices(),
            _FakeReader("comic"),
            vision_budget=60,
        )
        assert report.decides_against(0.204) == "DECIDES: NO-GO"

    def test_cards_without_reference_prices_are_skipped(self):
        listings = [_listing("1", "OP06-118 Zoro", 100.0)]
        report = run_scan(self._source(listings), {CARD: _variants()}, {})
        assert report.scanned == []


class TestPriceFreeTriage:
    """Scanning before a price corpus exists.

    Triage needs breadth and tolerates coarse numbers; valuation needs accuracy
    on a handful. Only the first has to be automated, so a scan must be able to
    run on a per-card variant spread alone.
    """

    def _source(self, listings):
        return type("S", (), {"fetch_active": lambda self, card: listings})()

    def test_scan_runs_with_no_reference_prices(self):
        listings = [_listing("1", "One Piece OP06-118 Zoro", 200.0)]
        report = run_scan(
            self._source(listings),
            {CARD: _variants()},
            {},
            _FakeReader("comic"),
            variant_spreads={CARD: 60.0},
        )
        assert len(report.scanned) == 1
        assert report.verified == 1
        assert report.mislabelled == 1

    def test_nothing_scans_without_prices_or_spread(self):
        listings = [_listing("1", "One Piece OP06-118 Zoro", 200.0)]
        report = run_scan(self._source(listings), {CARD: _variants()}, {})
        assert report.scanned == []

    def test_wide_spread_outranks_narrow_on_the_same_title(self):
        listings = [_listing("1", "One Piece OP06-118 Zoro", 200.0)]
        wide = run_scan(
            self._source(listings), {CARD: _variants()}, {}, variant_spreads={CARD: 60.0}
        ).scanned[0]
        narrow = run_scan(
            self._source(listings), {CARD: _variants()}, {}, variant_spreads={CARD: 1.5}
        ).scanned[0]
        assert wide.triage_score > narrow.triage_score

    def test_flat_spread_card_scores_zero_however_vague(self):
        # Resolving the variant cannot change the value, so no photo is worth it.
        listings = [_listing("1", "One Piece OP06-118", 200.0)]
        item = run_scan(
            self._source(listings), {CARD: _variants()}, {}, variant_spreads={CARD: 1.0}
        ).scanned[0]
        assert item.triage_score == 0.0

    def test_uninformative_title_outranks_a_precise_one(self):
        source = self._source([
            _listing("bare", "One Piece OP06-118 Zoro", 200.0),
            _listing("exact", "OP06-118 Zoro comic parallel red", 200.0),
        ])
        report = run_scan(source, {CARD: _variants()}, {}, variant_spreads={CARD: 60.0})
        by_id = {i.listing.listing_id: i for i in report.scanned}
        assert by_id["bare"].triage_score > by_id["exact"].triage_score

    def test_priced_listings_still_rank_on_information_value(self):
        listings = [_listing("1", "OP06-118 Zoro parallel", 200.0)]
        item = run_scan(
            self._source(listings), {CARD: _variants()}, _prices(), variant_spreads={CARD: 60.0}
        ).scanned[0]
        assert item.valuation is not None
        assert item.triage_score == item.valuation.information_value_aud
