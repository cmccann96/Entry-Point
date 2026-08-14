"""Tests that sources fail loudly rather than inventing data.

This is the most important test module in the project. A source that quietly
returns something plausible when it has nothing produces a backtest that reads
as evidence while being fiction.
"""

from __future__ import annotations

from datetime import date

import pytest

from optcg.sources.base import DataUnavailable, SoldSale, SourceCapabilityError
from optcg.sources.csv_source import CsvSoldSource
from optcg.sources.ebay_browse import EbayBrowseSource
from optcg.sources.pricecharting import PriceChartingSource


class TestCapabilityLimits:
    def test_pricecharting_refuses_to_supply_sold_history(self):
        # PriceCharting's API exposes current prices only. Asking it for Stage 1
        # data must raise, not return current prices dressed up as history.
        source = PriceChartingSource(token="dummy", aud_per_usd=1.5)
        with pytest.raises(SourceCapabilityError, match="historic"):
            source.fetch_sold("OP06-118", date(2026, 1, 1), date(2026, 8, 1))

    def test_ebay_browse_refuses_to_supply_sold_history(self):
        source = EbayBrowseSource(client_id="a", client_secret="b")
        with pytest.raises(SourceCapabilityError, match="active listings only"):
            source.fetch_sold("OP06-118", date(2026, 1, 1), date(2026, 8, 1))

    def test_missing_credentials_raise_immediately(self):
        with pytest.raises(DataUnavailable, match="PRICECHARTING_TOKEN"):
            PriceChartingSource(token=None, aud_per_usd=1.5)
        with pytest.raises(DataUnavailable, match="EBAY_CLIENT_ID"):
            EbayBrowseSource(client_id=None, client_secret=None)


class TestCsvSource:
    def test_missing_file_raises_with_remedy(self, tmp_path):
        source = CsvSoldSource(tmp_path / "nope.csv")
        with pytest.raises(DataUnavailable) as excinfo:
            source.fetch_sold("OP06-118", date(2026, 1, 1), date(2026, 8, 1))
        assert "Terapeak" in str(excinfo.value)

    def test_missing_columns_raise(self, tmp_path):
        path = tmp_path / "bad.csv"
        path.write_text("card_number,price\nOP06-118,100\n", encoding="utf-8")
        with pytest.raises(DataUnavailable, match="missing required columns"):
            CsvSoldSource(path).fetch_sold("OP06-118", date(2026, 1, 1), date(2026, 8, 1))

    def test_unknown_currency_is_an_error_not_a_silent_passthrough(self, tmp_path):
        # Treating USD as AUD would understate every overseas price by ~50%.
        path = tmp_path / "fx.csv"
        path.write_text(
            "card_number,sold_date,price,currency,title\n"
            "OP06-118,2026-03-01,1000,USD,Zoro manga\n",
            encoding="utf-8",
        )
        with pytest.raises(DataUnavailable, match="no FX rate"):
            CsvSoldSource(path).fetch_sold("OP06-118", date(2026, 1, 1), date(2026, 8, 1))

    def test_bad_rows_are_reported_not_dropped(self, tmp_path):
        path = tmp_path / "rows.csv"
        path.write_text(
            "card_number,sold_date,price,currency,title\n"
            "OP06-118,2026-03-01,1000,AUD,good\n"
            "OP06-118,not-a-date,900,AUD,bad date\n",
            encoding="utf-8",
        )
        with pytest.raises(DataUnavailable, match="unparseable"):
            CsvSoldSource(path).fetch_sold("OP06-118", date(2026, 1, 1), date(2026, 8, 1))

    def test_valid_file_parses_and_converts(self, tmp_path):
        path = tmp_path / "ok.csv"
        path.write_text(
            "card_number,sold_date,price,currency,title,graded\n"
            "OP06-118,2026-03-01,1000,USD,Zoro manga,\n"
            "OP06-118,2026-04-01,500,AUD,Zoro SEC,yes\n",
            encoding="utf-8",
        )
        source = CsvSoldSource(path, {"USD": 1.5})
        sales = source.fetch_sold("OP06-118", date(2026, 1, 1), date(2026, 12, 31))
        assert len(sales) == 2
        assert sales[0].price_aud == pytest.approx(1500.0)
        assert sales[0].currency_original == "USD"
        assert sales[1].is_graded

    def test_window_filtering(self, tmp_path):
        path = tmp_path / "win.csv"
        path.write_text(
            "card_number,sold_date,price,currency,title\n"
            "OP06-118,2026-01-15,1000,AUD,a\n"
            "OP06-118,2026-06-15,1000,AUD,b\n",
            encoding="utf-8",
        )
        source = CsvSoldSource(path)
        assert len(source.fetch_sold("OP06-118", date(2026, 1, 1), date(2026, 3, 1))) == 1

    def test_empty_file_raises(self, tmp_path):
        path = tmp_path / "empty.csv"
        path.write_text("", encoding="utf-8")
        with pytest.raises(DataUnavailable):
            CsvSoldSource(path).fetch_sold("OP06-118", date(2026, 1, 1), date(2026, 8, 1))


class TestSoldSaleValidation:
    def test_rejects_non_positive_price(self):
        with pytest.raises(ValueError):
            SoldSale("OP06-118", date(2026, 1, 1), 0.0, "free card")
