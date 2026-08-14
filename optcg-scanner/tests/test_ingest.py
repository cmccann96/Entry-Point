"""Tests for forgiving ingestion of hand-collected data.

The goal is that whatever shape the export arrives in, it either loads or fails
with an actionable message. Silent misreads are the failure mode that matters:
a mis-parsed currency or a dropped row biases the measurement it feeds.
"""

from __future__ import annotations

from datetime import date

import pytest

from optcg.ingest import (
    ManualSource,
    load_sales,
    map_columns,
    parse_date,
    parse_price,
    read_table,
)
from optcg.sources.base import DataUnavailable

FX = {"AUD": 1.0, "USD": 1.5, "JPY": 0.01}


class TestColumnMapping:
    @pytest.mark.parametrize(
        "headers",
        [
            ["card_number", "sold_date", "price", "title"],
            ["Card Number", "Sold Date", "Price", "Title"],
            ["cardNumber", "dateSold", "soldPrice", "itemTitle"],
            ["Item", "Date of sale", "Avg sold price", "Listing Title"],
        ],
    )
    def test_auto_detects_common_variants(self, headers):
        mapping = map_columns(headers)
        assert {"sold_date", "price", "title"} <= mapping.keys()

    def test_overrides_win(self):
        mapping = map_columns(
            ["weird_a", "weird_b", "weird_c"],
            {"sold_date": "weird_a", "price": "weird_b", "title": "weird_c"},
        )
        assert mapping["price"] == "weird_b"

    def test_unmappable_reports_actual_headers_and_the_fix(self):
        with pytest.raises(DataUnavailable) as excinfo:
            map_columns(["alpha", "beta"])
        message = str(excinfo.value)
        assert "alpha" in message and "beta" in message
        assert "[ingest.columns]" in message

    def test_override_to_absent_column_is_an_error(self):
        with pytest.raises(DataUnavailable, match="not in the file"):
            map_columns(["a", "b", "c"], {"price": "nonexistent"})


class TestPriceParsing:
    @pytest.mark.parametrize(
        "raw,amount,currency",
        [
            ("US $1,098.00", 1098.0, "USD"),
            ("AU $860", 860.0, "AUD"),
            ("£450.50", 450.5, "GBP"),
            ("¥150000", 150000.0, "JPY"),
            ("1098.00", 1098.0, "AUD"),  # via default
        ],
    )
    def test_parses_symbols_and_separators(self, raw, amount, currency):
        assert parse_price(raw, "AUD") == (amount, currency)

    def test_bare_dollar_without_default_is_an_error(self):
        # AUD and USD share '$'. Guessing would understate overseas sales ~50%.
        with pytest.raises(ValueError, match="ambiguous"):
            parse_price("$1,098.00")

    def test_bare_dollar_with_default_resolves(self):
        assert parse_price("$1,098.00", "USD") == (1098.0, "USD")

    def test_rejects_blank_and_non_numeric(self):
        for raw in ("", "   ", "n/a"):
            with pytest.raises(ValueError):
                parse_price(raw, "AUD")

    def test_rejects_non_positive(self):
        with pytest.raises(ValueError):
            parse_price("$0.00", "AUD")


class TestDateParsing:
    @pytest.mark.parametrize(
        "raw",
        ["2026-03-14", "14/03/2026", "Mar 14, 2026", "14 Mar 2026", "March 14, 2026"],
    )
    def test_parses_common_formats(self, raw):
        assert parse_date(raw) == date(2026, 3, 14)

    def test_strips_weekday_and_time_noise(self):
        assert parse_date("Sat, Mar 14, 2026 at 14:32") == date(2026, 3, 14)

    def test_unrecognised_date_raises(self):
        with pytest.raises(ValueError, match="unrecognised date"):
            parse_date("last Tuesday")


class TestTableReading:
    def test_reads_csv(self, tmp_path):
        path = tmp_path / "a.csv"
        path.write_text("h1,h2\nv1,v2\n", encoding="utf-8")
        headers, rows = read_table(path)
        assert headers == ["h1", "h2"] and rows[0]["h2"] == "v2"

    def test_reads_tsv(self, tmp_path):
        path = tmp_path / "a.tsv"
        path.write_text("h1\th2\nv1\tv2\n", encoding="utf-8")
        headers, _ = read_table(path)
        assert headers == ["h1", "h2"]

    def test_reads_largest_table_from_saved_html(self, tmp_path):
        path = tmp_path / "saved.html"
        path.write_text(
            "<html><body>"
            "<table><tr><th>nav</th></tr><tr><td>junk</td></tr></table>"
            "<table>"
            "<tr><th>Title</th><th>Price</th><th>Date</th></tr>"
            "<tr><td>OP06-118 Zoro</td><td>US $1,098.00</td><td>2026-03-14</td></tr>"
            "<tr><td>OP06-118 manga</td><td>US $980.00</td><td>2026-03-20</td></tr>"
            "</table></body></html>",
            encoding="utf-8",
        )
        headers, rows = read_table(path)
        assert headers == ["Title", "Price", "Date"]
        assert len(rows) == 2

    def test_html_without_table_gives_actionable_advice(self, tmp_path):
        path = tmp_path / "empty.html"
        path.write_text("<html><body><p>nothing</p></body></html>", encoding="utf-8")
        with pytest.raises(DataUnavailable, match="paste into a spreadsheet"):
            read_table(path)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(DataUnavailable, match="no such file"):
            read_table(tmp_path / "absent.csv")


class TestLoadSales:
    def test_card_number_inferred_from_title(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text(
            "sold_date,price,currency,title\n"
            "2026-03-14,1098,USD,One Piece OP06-118 Zoro manga\n",
            encoding="utf-8",
        )
        sales = load_sales(path, FX)
        assert sales[0].card_number == "OP06-118"
        assert sales[0].price_aud == pytest.approx(1647.0)

    def test_card_number_falls_back_to_argument(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text(
            "sold_date,price,currency,title\n2026-03-14,1098,AUD,Zoro manga rare\n",
            encoding="utf-8",
        )
        assert load_sales(path, FX, card_number="OP06-118")[0].card_number == "OP06-118"

    def test_no_card_number_anywhere_is_an_error(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text(
            "sold_date,price,currency,title\n2026-03-14,1098,AUD,Zoro manga\n",
            encoding="utf-8",
        )
        with pytest.raises(DataUnavailable, match="no card number"):
            load_sales(path, FX)

    def test_missing_fx_rate_is_an_error(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text(
            "sold_date,price,currency,title\n2026-03-14,1098,GBP,OP06-118 Zoro\n",
            encoding="utf-8",
        )
        with pytest.raises(DataUnavailable, match="no FX rate"):
            load_sales(path, FX)

    def test_bad_rows_reported_with_line_numbers(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text(
            "sold_date,price,currency,title\n"
            "2026-03-14,1098,AUD,OP06-118 good\n"
            "whenever,900,AUD,OP06-118 bad date\n",
            encoding="utf-8",
        )
        with pytest.raises(DataUnavailable) as excinfo:
            load_sales(path, FX)
        assert "line 3" in str(excinfo.value)


class TestManualSource:
    def test_infers_card_from_filename(self, tmp_path):
        path = tmp_path / "OP06-118.csv"
        path.write_text(
            "sold_date,price,currency,title\n"
            "2026-03-14,1098,AUD,Zoro manga rare\n"
            "2026-04-01,860,AUD,Zoro\n",
            encoding="utf-8",
        )
        source = ManualSource([path], FX)
        sales = source.fetch_sold("OP06-118", date(2026, 1, 1), date(2026, 12, 31))
        assert len(sales) == 2
        assert source.card_numbers() == ["OP06-118"]

    def test_merges_multiple_files(self, tmp_path):
        for name, number in (("OP06-118.csv", "OP06-118"), ("ST01-001.csv", "ST01-001")):
            (tmp_path / name).write_text(
                f"sold_date,price,currency,title\n2026-03-14,100,AUD,{number} card\n",
                encoding="utf-8",
            )
        source = ManualSource(list(tmp_path.iterdir()), FX)
        assert source.card_numbers() == ["OP06-118", "ST01-001"]

    def test_no_files_configured_raises(self):
        with pytest.raises(DataUnavailable, match="no input files"):
            ManualSource([], FX).fetch_sold("OP06-118", date(2026, 1, 1), date(2026, 2, 1))
