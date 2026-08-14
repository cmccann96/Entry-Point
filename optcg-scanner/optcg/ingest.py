"""Forgiving ingestion for hand-collected sales data.

The point of this module is to make manual collection cheap. You should be able
to export from Terapeak, or select rows in a browser and paste into a
spreadsheet, save whatever columns you happen to get, and have it load without
hand-editing the file.

It does NOT fetch anything. Every function here reads a local file that you
supplied. There is no network code in this module by design.

Two things it deliberately refuses to do:

  * guess a column it cannot confidently identify -- it reports the headers it
    actually found and asks you to map them, rather than silently picking one
  * guess a currency -- an unlabelled price column is an error, because reading
    USD as AUD understates every overseas sale by ~50%
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

from .sources.base import DataUnavailable, SoldSale

# Column aliases seen across Terapeak exports, spreadsheet pastes, and manual
# transcription. Matching is case- and punctuation-insensitive.
_ALIASES: dict[str, tuple[str, ...]] = {
    "card_number": ("card_number", "cardnumber", "card", "number", "cardno", "sku", "item"),
    "sold_date": (
        "sold_date", "solddate", "date", "datesold", "dateofsale", "saledate",
        "enddate", "dateended", "lastsold",
    ),
    "price": (
        "price", "soldprice", "saleprice", "totalprice", "avgsaleprice",
        "avgsoldprice", "averagesaleprice", "averagesoldprice", "itemprice",
        "amount", "soldfor", "finalprice",
    ),
    "currency": ("currency", "ccy", "curr"),
    "title": ("title", "itemtitle", "name", "listingtitle", "description", "product"),
    "graded": ("graded", "isgraded", "slabbed"),
    # Filled in by looking at the listing photo. This is the ground-truth column
    # that turns an export into a mislabel measurement.
    "true_variant": ("truevariant", "actualvariant", "confirmedvariant", "verified"),
    "grade": ("grade", "gradevalue"),
    "grader": ("grader", "gradingcompany", "company"),
}

_DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d",
    "%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%d %B %Y",
    "%b %d %Y", "%Y-%m-%d %H:%M:%S", "%d/%m/%y", "%m/%d/%y",
)

_CURRENCY_SYMBOLS = {
    "US $": "USD", "AU $": "AUD", "C $": "CAD", "£": "GBP", "€": "EUR",
    "¥": "JPY", "US$": "USD", "AU$": "AUD", "A$": "AUD", "$": None,
}

_GRADED_TRUE = {"1", "true", "yes", "y", "graded", "slabbed"}


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.strip().lower())


def map_columns(
    headers: list[str], overrides: dict[str, str] | None = None
) -> dict[str, str]:
    """Map canonical field names to the actual column names in the file.

    ``overrides`` (from ``[ingest.columns]`` in config.toml) wins over
    auto-detection, so an unusual export can always be forced through.
    """
    overrides = {k: v for k, v in (overrides or {}).items()}
    normalised = {_normalise(h): h for h in headers}
    mapping: dict[str, str] = {}

    for field, aliases in _ALIASES.items():
        if field in overrides:
            wanted = overrides[field]
            if wanted not in headers:
                raise DataUnavailable(
                    "ingest",
                    f"config maps {field!r} to column {wanted!r}, which is not in the file",
                    f"Columns present: {headers}",
                )
            mapping[field] = wanted
            continue
        for alias in aliases:
            if alias in normalised:
                mapping[field] = normalised[alias]
                break

    missing = {"sold_date", "price", "title"} - mapping.keys()
    if missing:
        raise DataUnavailable(
            "ingest",
            f"could not identify required column(s): {sorted(missing)}",
            "Columns found in your file:\n    "
            + "\n    ".join(headers)
            + "\n  Add explicit mappings under [ingest.columns] in config.toml, e.g.\n"
            + "".join(f'    {f} = "<your column>"\n' for f in sorted(missing)),
        )
    return mapping


def parse_price(raw: str, default_currency: str | None = None) -> tuple[float, str]:
    """Parse a price cell into (amount, currency).

    Currency is taken from a symbol prefix when present, else from
    ``default_currency``. A bare ``$`` is ambiguous and is an error unless a
    default is supplied -- AUD and USD both use it.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("blank price")

    currency: str | None = None
    for symbol, code in sorted(_CURRENCY_SYMBOLS.items(), key=lambda kv: -len(kv[0])):
        if text.upper().startswith(symbol.upper()):
            currency = code
            text = text[len(symbol):].strip()
            break

    amount_text = re.sub(r"[^0-9.\-]", "", text)
    if not amount_text:
        raise ValueError(f"no numeric value in price {raw!r}")
    amount = float(amount_text)
    if amount <= 0:
        raise ValueError(f"non-positive price {raw!r}")

    currency = currency or default_currency
    if not currency:
        raise ValueError(
            f"cannot determine currency for {raw!r} -- '$' is ambiguous. "
            "Add a currency column, or set ingest.default_currency in config.toml"
        )
    return amount, currency.upper()


def parse_date(raw: str) -> date:
    """Parse a date cell, trying the formats these exports actually use."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("blank date")
    # Strip a leading weekday and any trailing time-of-day noise.
    text = re.sub(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s*", "", text, flags=re.I)
    text = re.sub(r"\s+at\s+.*$", "", text, flags=re.I).strip()

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {raw!r}")


class _TableExtractor(HTMLParser):
    """Pulls every <table> out of a locally saved HTML file.

    For pages you saved yourself from a browser. This parses a file on disk; it
    never performs a request.
    """

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self._table.append(self._row)  # type: ignore[union-attr]
            self._row = None
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))  # type: ignore[union-attr]
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def read_table(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read a local CSV/TSV/HTML file into (headers, rows).

    For HTML, the largest table in the document is used, on the assumption that
    a saved results page's biggest table is the results.
    """
    if not path.exists():
        raise DataUnavailable("ingest", f"no such file: {path}")

    suffix = path.suffix.lower()
    if suffix in (".html", ".htm"):
        parser = _TableExtractor()
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        if not parser.tables:
            raise DataUnavailable(
                "ingest",
                f"no <table> found in {path}",
                "Select the results in your browser, paste into a spreadsheet, "
                "and save as CSV instead.",
            )
        table = max(parser.tables, key=len)
        headers, *body = table
        rows = [dict(zip(headers, row)) for row in body if len(row) >= len(headers) // 2]
        return headers, rows

    delimiter = "\t" if suffix in (".tsv", ".tab") else ","
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if reader.fieldnames is None:
            raise DataUnavailable("ingest", f"{path} is empty")
        return list(reader.fieldnames), list(reader)


def load_truth_column(
    path: str | Path, column_overrides: dict[str, str] | None = None
) -> dict[str, str]:
    """Read title -> confirmed-variant annotations from a file, if present.

    Returns an empty mapping when the file carries no ``true_variant`` column,
    so an un-annotated export loads normally.
    """
    path = Path(path)
    headers, rows = read_table(path)
    try:
        mapping = map_columns(headers, column_overrides)
    except DataUnavailable:
        return {}
    if "true_variant" not in mapping or "title" not in mapping:
        return {}

    truth: dict[str, str] = {}
    for row in rows:
        label = (row.get(mapping["true_variant"]) or "").strip()
        title = (row.get(mapping["title"]) or "").strip()
        if label and title:
            truth[title] = label
    return truth


def load_sales(
    path: str | Path,
    fx_rates: dict[str, float],
    card_number: str | None = None,
    column_overrides: dict[str, str] | None = None,
    default_currency: str | None = None,
) -> list[SoldSale]:
    """Load sales from a local file, tolerating whatever shape it arrived in.

    ``card_number`` supplies the card for files that cover a single card and
    therefore have no card column -- the common case when you export one search
    at a time. Titles are still scanned for a card number, which wins when
    present.
    """
    from .variants import extract_card_numbers

    path = Path(path)
    headers, rows = read_table(path)
    mapping = map_columns(headers, column_overrides)

    sales: list[SoldSale] = []
    problems: list[str] = []

    for lineno, row in enumerate(rows, start=2):
        try:
            title = (row.get(mapping["title"]) or "").strip()

            number = None
            if "card_number" in mapping:
                number = (row.get(mapping["card_number"]) or "").strip().upper() or None
            if not number:
                found = extract_card_numbers(title)
                number = found[0] if found else card_number
            if not number:
                raise ValueError("no card number in row, title, or --card")

            amount, currency = parse_price(
                row.get(mapping["price"], ""),
                (row.get(mapping.get("currency", ""), "") or default_currency) or None,
            )
            if currency not in fx_rates:
                raise ValueError(
                    f"no FX rate for {currency}; add it under [fx] in config.toml"
                )

            graded_raw = (row.get(mapping.get("graded", ""), "") or "").strip().lower()
            sales.append(
                SoldSale(
                    card_number=number,
                    sold_date=parse_date(row.get(mapping["sold_date"], "")),
                    price_aud=amount * fx_rates[currency],
                    title=title,
                    currency_original=currency,
                    price_original=amount,
                    is_graded=graded_raw in _GRADED_TRUE,
                    grade=(row.get(mapping.get("grade", ""), "") or "").strip() or None,
                    grader=(row.get(mapping.get("grader", ""), "") or "").strip() or None,
                    source=f"manual:{path.name}",
                )
            )
        except (ValueError, KeyError) as exc:
            problems.append(f"  line {lineno}: {exc}")

    if problems:
        raise DataUnavailable(
            "ingest",
            f"{len(problems)} unparseable row(s) in {path}:\n" + "\n".join(problems[:20]),
            "Fix or delete these rows. They are not skipped, because silently "
            "dropping input biases the measured distribution.",
        )
    if not sales:
        raise DataUnavailable("ingest", f"{path} contained no usable sales")
    return sales


class ManualSource:
    """Sold-sales source backed by one or more local files you collected."""

    name = "manual"

    def __init__(
        self,
        paths: list[str | Path],
        fx_rates: dict[str, float],
        column_overrides: dict[str, str] | None = None,
        default_currency: str | None = None,
    ) -> None:
        self.paths = [Path(p) for p in paths]
        self.fx_rates = fx_rates
        self.column_overrides = column_overrides
        self.default_currency = default_currency
        self._cache: dict[str, list[SoldSale]] | None = None

    def _load(self) -> dict[str, list[SoldSale]]:
        if self._cache is not None:
            return self._cache
        if not self.paths:
            raise DataUnavailable(
                self.name,
                "no input files configured",
                "Put your exports in data/ and list them under [sources] in config.toml.",
            )

        by_card: dict[str, list[SoldSale]] = {}
        for path in self.paths:
            # A file named for its card lets single-card exports skip the column.
            stem_numbers = __import__(
                "optcg.variants", fromlist=["extract_card_numbers"]
            ).extract_card_numbers(path.stem)
            for sale in load_sales(
                path,
                self.fx_rates,
                card_number=stem_numbers[0] if stem_numbers else None,
                column_overrides=self.column_overrides,
                default_currency=self.default_currency,
            ):
                by_card.setdefault(sale.card_number, []).append(sale)

        self._cache = by_card
        return by_card

    def fetch_sold(self, card_number: str, start: date, end: date) -> list[SoldSale]:
        sales = self._load().get(card_number.upper(), [])
        return sorted(
            (s for s in sales if start <= s.sold_date <= end), key=lambda s: s.sold_date
        )

    def card_numbers(self) -> list[str]:
        return sorted(self._load())
