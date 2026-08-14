"""Tests for the forward-test journal."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from optcg.journal import Journal
from optcg.vision import CardRead, VisionCache


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


def _record(journal, listing_id="1", ask=200.0, ev=900.0, flagged_at=None, **kwargs):
    return journal.record(
        listing_id=listing_id,
        card_number="OP06-118",
        title="OP06-118 Zoro parallel",
        url="https://ebay/1",
        ask_aud=ask,
        title_claim="parallel",
        vision_treatment="comic",
        vision_confidence=0.92,
        modelled_ev_aud=ev,
        flagged_at=flagged_at,
        **kwargs,
    )


class TestRecording:
    def test_records_and_lists_open(self, journal):
        assert _record(journal) is True
        entries = journal.open_entries()
        assert len(entries) == 1
        assert entries[0].card_number == "OP06-118"
        assert entries[0].vision_treatment == "comic"

    def test_duplicate_listing_is_not_double_counted(self, journal):
        # Re-scanning the same watchlist must not inflate the forward test.
        assert _record(journal) is True
        assert _record(journal) is False
        assert len(journal.open_entries()) == 1

    def test_card_number_normalised(self, journal):
        journal.record(
            listing_id="x",
            card_number="op06-118",
            title="t",
            url="u",
            ask_aud=1.0,
            title_claim="parallel",
        )
        assert journal.open_entries()[0].card_number == "OP06-118"


class TestResolving:
    def test_resolve_moves_entry_out_of_open(self, journal):
        _record(journal)
        journal.resolve("1", "sold", 1450.0)
        assert journal.open_entries() == []
        assert len(journal.resolved_entries()) == 1

    def test_sold_requires_a_price(self, journal):
        _record(journal)
        with pytest.raises(ValueError, match="requires sold_price_aud"):
            journal.resolve("1", "sold")

    def test_rejects_unknown_outcome(self, journal):
        _record(journal)
        with pytest.raises(ValueError, match="sold/unsold/delisted"):
            journal.resolve("1", "vanished")

    def test_resolving_unknown_listing_raises(self, journal):
        with pytest.raises(KeyError):
            journal.resolve("nope", "unsold")

    def test_unsold_needs_no_price(self, journal):
        _record(journal)
        journal.resolve("1", "unsold")
        assert journal.resolved_entries()[0].outcome == "unsold"


class TestForwardTestSignals:
    def test_read_accuracy_when_market_agrees(self, journal):
        # Photo said comic parallel; it sold well above ask. The read held up.
        _record(journal, ask=200.0)
        journal.resolve("1", "sold", 1450.0)
        assert journal.resolved_entries()[0].read_was_right is True

    def test_read_accuracy_when_market_disagrees(self, journal):
        # Photo said comic parallel; it sold at plain-SEC money. The read didn't.
        _record(journal, ask=200.0)
        journal.resolve("1", "sold", 30.0)
        assert journal.resolved_entries()[0].read_was_right is False

    def test_unresolved_read_accuracy_is_none(self, journal):
        _record(journal)
        assert journal.open_entries()[0].read_was_right is None

    def test_days_open_tracks_time_to_sale(self, journal):
        flagged = date.today() - timedelta(days=5)
        _record(journal, flagged_at=flagged)
        journal.resolve("1", "sold", 1450.0, resolved_at=date.today())
        assert journal.resolved_entries()[0].days_open == 5

    def test_summary_flags_fast_sales_as_a_latency_problem(self, journal):
        today = date.today()
        for i in range(3):
            _record(journal, listing_id=str(i), flagged_at=today)
            journal.resolve(str(i), "sold", 1450.0, resolved_at=today)
        summary = journal.summary()
        assert summary["sold"] == 3
        assert summary["sold_within_1_day"] == 3
        assert summary["median_days_to_sale"] == 0

    def test_summary_reports_read_accuracy(self, journal):
        _record(journal, listing_id="a", ask=200.0)
        _record(journal, listing_id="b", ask=200.0)
        journal.resolve("a", "sold", 1450.0)
        journal.resolve("b", "sold", 30.0)
        summary = journal.summary()
        assert summary["read_accuracy"] == pytest.approx(0.5)
        assert summary["read_accuracy_n"] == 2

    def test_empty_summary_is_safe(self, journal):
        summary = journal.summary()
        assert summary["open"] == 0 and summary["resolved"] == 0
        assert "read_accuracy" not in summary


class TestVisionCache:
    def test_roundtrip(self, tmp_path):
        cache = VisionCache(tmp_path / "vision.db")
        read = CardRead(treatment="comic", confidence=0.9)
        cache.put("https://img/1.jpg", read)
        assert cache.get("https://img/1.jpg").treatment == "comic"
        cache.close()

    def test_miss_returns_none(self, tmp_path):
        cache = VisionCache(tmp_path / "vision.db")
        assert cache.get("https://img/absent.jpg") is None
        cache.close()

    def test_overwrite_updates(self, tmp_path):
        cache = VisionCache(tmp_path / "vision.db")
        cache.put("u", CardRead(treatment="parallel", confidence=0.5))
        cache.put("u", CardRead(treatment="comic", confidence=0.95))
        assert cache.get("u").treatment == "comic"
        cache.close()
