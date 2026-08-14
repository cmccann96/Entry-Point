"""Forward-test journal.

The scan tells you what a listing looks like *now*. It cannot tell you whether
your read was right, or whether the market agreed. Recording every flagged
listing and revisiting it later converts the scanner into a forward test --
which, given that no compliant source of historical sold data exists, is the
only honest way to validate the thesis end to end.

The loop:

  1. ``record`` every listing the scan flags, with its title claim, the photo's
     read, the ask, and the modelled EV
  2. wait
  3. ``resolve`` each one with what actually happened -- sold price, or that it
     ended unsold

Two things fall out that nothing else in this project can measure:

  * **read accuracy** -- when the photo said "comic parallel" and it later sold
    at comic-parallel money, the read was right. Sold at plain-SEC money, and
    the vision step is not reliable enough to trade on.
  * **competition** -- a deep-tail listing that vanishes in ninety seconds is
    not an opportunity you can capture on a poll-based scanner, however
    correctly you identified it. Time-to-sale is the measurement that decides
    whether latency, rather than detection, is the binding constraint.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flagged (
    listing_id        TEXT PRIMARY KEY,
    card_number       TEXT NOT NULL,
    title             TEXT NOT NULL,
    url               TEXT NOT NULL,
    ask_aud           REAL NOT NULL,
    title_claim       TEXT NOT NULL,
    vision_treatment  TEXT,
    vision_confidence REAL,
    modelled_ev_aud   REAL,
    flagged_at        TEXT NOT NULL,
    outcome           TEXT,
    sold_price_aud    REAL,
    resolved_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_flagged_card ON flagged (card_number);
CREATE INDEX IF NOT EXISTS idx_flagged_open ON flagged (outcome) WHERE outcome IS NULL;
"""


@dataclass(frozen=True)
class JournalEntry:
    listing_id: str
    card_number: str
    title: str
    url: str
    ask_aud: float
    title_claim: str
    vision_treatment: str | None
    vision_confidence: float | None
    modelled_ev_aud: float | None
    flagged_at: date
    outcome: str | None = None
    sold_price_aud: float | None = None
    resolved_at: date | None = None

    @property
    def days_open(self) -> int:
        end = self.resolved_at or date.today()
        return (end - self.flagged_at).days

    @property
    def read_was_right(self) -> bool | None:
        """Did the market price it as the photo said it was?

        Only answerable once resolved with a sold price, and only ever a signal
        rather than proof -- a correctly-identified card can still sell cheap.
        """
        if self.outcome != "sold" or self.sold_price_aud is None:
            return None
        if self.modelled_ev_aud is None:
            return None
        return self.sold_price_aud >= self.ask_aud


class Journal:
    """SQLite-backed record of flagged listings and their outcomes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record(
        self,
        listing_id: str,
        card_number: str,
        title: str,
        url: str,
        ask_aud: float,
        title_claim: str,
        vision_treatment: str | None = None,
        vision_confidence: float | None = None,
        modelled_ev_aud: float | None = None,
        flagged_at: date | None = None,
    ) -> bool:
        """Record a flagged listing. Returns False if already present.

        Idempotent on ``listing_id`` so repeated scans of the same watchlist
        don't double-count the same listing into the forward test.
        """
        existing = self._conn.execute(
            "SELECT 1 FROM flagged WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        if existing:
            return False

        self._conn.execute(
            "INSERT INTO flagged (listing_id, card_number, title, url, ask_aud,"
            " title_claim, vision_treatment, vision_confidence, modelled_ev_aud,"
            " flagged_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                listing_id,
                card_number.upper(),
                title,
                url,
                ask_aud,
                title_claim,
                vision_treatment,
                vision_confidence,
                modelled_ev_aud,
                (flagged_at or date.today()).isoformat(),
            ),
        )
        self._conn.commit()
        return True

    def resolve(
        self,
        listing_id: str,
        outcome: str,
        sold_price_aud: float | None = None,
        resolved_at: date | None = None,
    ) -> None:
        """Record what happened. ``outcome`` is 'sold', 'unsold', or 'delisted'."""
        if outcome not in {"sold", "unsold", "delisted"}:
            raise ValueError(f"outcome must be sold/unsold/delisted, got {outcome!r}")
        if outcome == "sold" and sold_price_aud is None:
            raise ValueError("a sold outcome requires sold_price_aud")

        cursor = self._conn.execute(
            "UPDATE flagged SET outcome = ?, sold_price_aud = ?, resolved_at = ?"
            " WHERE listing_id = ?",
            (
                outcome,
                sold_price_aud,
                (resolved_at or date.today()).isoformat(),
                listing_id,
            ),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"no flagged listing with id {listing_id!r}")
        self._conn.commit()

    def _row_to_entry(self, row: sqlite3.Row) -> JournalEntry:
        return JournalEntry(
            listing_id=row["listing_id"],
            card_number=row["card_number"],
            title=row["title"],
            url=row["url"],
            ask_aud=row["ask_aud"],
            title_claim=row["title_claim"],
            vision_treatment=row["vision_treatment"],
            vision_confidence=row["vision_confidence"],
            modelled_ev_aud=row["modelled_ev_aud"],
            flagged_at=datetime.strptime(row["flagged_at"], "%Y-%m-%d").date(),
            outcome=row["outcome"],
            sold_price_aud=row["sold_price_aud"],
            resolved_at=(
                datetime.strptime(row["resolved_at"], "%Y-%m-%d").date()
                if row["resolved_at"]
                else None
            ),
        )

    def open_entries(self) -> list[JournalEntry]:
        """Flagged listings with no recorded outcome -- the ones to go check."""
        rows = self._conn.execute(
            "SELECT * FROM flagged WHERE outcome IS NULL ORDER BY flagged_at"
        ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def resolved_entries(self) -> list[JournalEntry]:
        rows = self._conn.execute(
            "SELECT * FROM flagged WHERE outcome IS NOT NULL ORDER BY resolved_at"
        ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def summary(self) -> dict[str, object]:
        """What the forward test says so far."""
        resolved = self.resolved_entries()
        sold = [e for e in resolved if e.outcome == "sold"]
        reads = [e.read_was_right for e in sold if e.read_was_right is not None]

        summary: dict[str, object] = {
            "open": len(self.open_entries()),
            "resolved": len(resolved),
            "sold": len(sold),
            "unsold": sum(1 for e in resolved if e.outcome != "sold"),
        }
        if sold:
            days = sorted(e.days_open for e in sold)
            summary["median_days_to_sale"] = days[len(days) // 2]
            # A deep-tail listing that clears in under a day is a latency
            # problem, not a detection problem.
            summary["sold_within_1_day"] = sum(1 for d in days if d <= 1)
        if reads:
            summary["read_accuracy"] = sum(reads) / len(reads)
            summary["read_accuracy_n"] = len(reads)
        return summary

    def close(self) -> None:
        self._conn.close()
