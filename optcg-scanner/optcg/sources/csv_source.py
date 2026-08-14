"""Sold-sale history from an operator-supplied CSV export.

This is the **only compliant Stage 1 input** available as of 2026-08.

Rationale: eBay's Browse API returns active listings only; the Marketplace
Insights API (the sole official sold-price API) is restricted and closed to new
users; PriceCharting's API returns current prices only. Terapeak -- free with
any eBay seller account, ~3 years of sold history -- has no API but does
support manual CSV export. Exporting your own account's research data is
neither scraping nor an unofficial API, so it satisfies the brief's hard
constraint while actually containing the required data.

Expected columns (case-insensitive, extra columns ignored):
    card_number, sold_date, price, currency, title
Optional:
    graded, grade, grader

``sold_date`` must be ISO ``YYYY-MM-DD``. Rows that cannot be parsed are
reported, not silently dropped -- a backtest that quietly discards half its
input is a backtest that lies.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

from .base import DataUnavailable, SoldSale

_GRADED_TRUE = {"1", "true", "yes", "y", "graded"}


class CsvSoldSource:
    """Reads realised sales from a CSV export.

    ``fx_rates`` maps currency code -> AUD multiplier. Any currency present in
    the file but missing from the mapping is a hard error: silently treating
    USD as AUD would understate every overseas price by ~50%.
    """

    name = "csv-export"

    def __init__(self, path: str | Path, fx_rates: dict[str, float] | None = None) -> None:
        self.path = Path(path)
        self.fx_rates = {"AUD": 1.0, **{k.upper(): v for k, v in (fx_rates or {}).items()}}
        self._cache: dict[str, list[SoldSale]] | None = None

    def _load(self) -> dict[str, list[SoldSale]]:
        if self._cache is not None:
            return self._cache

        if not self.path.exists():
            raise DataUnavailable(
                self.name,
                f"no sold-sales export at {self.path}",
                "Export sold history from eBay Seller Hub > Research (Terapeak) "
                "and save it to this path. See README.md 'Data sources'.",
            )

        by_card: dict[str, list[SoldSale]] = {}
        problems: list[str] = []

        with self.path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise DataUnavailable(self.name, f"{self.path} is empty")

            headers = {name.strip().lower(): name for name in reader.fieldnames}
            required = {"card_number", "sold_date", "price", "title"}
            missing = required - headers.keys()
            if missing:
                raise DataUnavailable(
                    self.name,
                    f"{self.path} missing required columns: {sorted(missing)}",
                    f"Found columns: {sorted(headers)}",
                )

            for lineno, row in enumerate(reader, start=2):
                try:
                    sale = self._parse_row(row, headers)
                except (ValueError, KeyError) as exc:
                    problems.append(f"  line {lineno}: {exc}")
                    continue
                by_card.setdefault(sale.card_number, []).append(sale)

        if problems:
            raise DataUnavailable(
                self.name,
                f"{len(problems)} unparseable row(s) in {self.path}:\n"
                + "\n".join(problems[:20]),
                "Fix or remove these rows. They are not being skipped, because "
                "silently dropping input would bias the measured distribution.",
            )

        if not by_card:
            raise DataUnavailable(self.name, f"{self.path} contained no usable sales")

        self._cache = by_card
        return by_card

    def _parse_row(self, row: dict[str, str], headers: dict[str, str]) -> SoldSale:
        def field(key: str, default: str = "") -> str:
            column = headers.get(key)
            if column is None:
                return default
            return (row.get(column) or default).strip()

        card_number = field("card_number").upper()
        if not card_number:
            raise ValueError("blank card_number")

        raw_date = field("sold_date")
        try:
            sold_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"bad sold_date {raw_date!r} (want YYYY-MM-DD)") from exc

        raw_price = field("price").replace("$", "").replace(",", "")
        try:
            price = float(raw_price)
        except ValueError as exc:
            raise ValueError(f"bad price {raw_price!r}") from exc

        currency = (field("currency") or "AUD").upper()
        if currency not in self.fx_rates:
            raise ValueError(
                f"no FX rate configured for {currency!r}; add it to [fx] in config.toml"
            )

        graded_raw = field("graded").lower()
        return SoldSale(
            card_number=card_number,
            sold_date=sold_date,
            price_aud=price * self.fx_rates[currency],
            title=field("title"),
            currency_original=currency,
            price_original=price,
            is_graded=graded_raw in _GRADED_TRUE,
            grade=field("grade") or None,
            grader=field("grader") or None,
            source=self.name,
        )

    def fetch_sold(self, card_number: str, start: date, end: date) -> list[SoldSale]:
        sales = self._load().get(card_number.upper(), [])
        window = [s for s in sales if start <= s.sold_date <= end]
        return sorted(window, key=lambda s: s.sold_date)

    def card_numbers(self) -> list[str]:
        return sorted(self._load())
