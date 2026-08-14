"""PriceCharting adapter -- CURRENT PRICES ONLY.

Read this before wiring it into anything.

The brief assumed PriceCharting could supply historical sold data for Stage 1.
It cannot. PriceCharting's own API documentation states the API and CSV support
current item values in various grades and conditions, and that historic prices
and historic sales are **not supported**. The per-sale history visible on their
website is a UI feature with no API surface.

So this adapter deliberately implements ``fetch_price_reference`` and NOT
``fetch_sold``. Calling ``fetch_sold`` raises ``SourceCapabilityError`` rather
than returning something plausible-looking.

What it is still good for: a cross-check on the raw-vs-graded price levels used
as reference points for the raw distribution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx

from .base import DataUnavailable, SourceCapabilityError

_BASE_URL = "https://www.pricecharting.com/api/product"


@dataclass(frozen=True)
class PriceReference:
    """Current price points for one product, in AUD."""

    product_id: str
    product_name: str
    loose_aud: float | None
    graded_aud: float | None
    retrieved_at: date


class PriceChartingSource:
    name = "pricecharting"

    def __init__(self, token: str | None, aud_per_usd: float, timeout: float = 20.0) -> None:
        if not token:
            raise DataUnavailable(
                self.name,
                "PRICECHARTING_TOKEN is not set",
                "Add it to .env. Note a paid subscription is required, and it "
                "still will not provide Stage 1 sold history.",
            )
        self.token = token
        self.aud_per_usd = aud_per_usd
        self.timeout = timeout

    def fetch_sold(self, card_number: str, start: date, end: date):
        raise SourceCapabilityError(
            self.name,
            "PriceCharting's API does not expose historic sales or historic prices",
            "Stage 1 needs realised per-sale history. Use a Terapeak CSV export "
            "via optcg.sources.csv_source instead. See README.md 'Data sources'.",
        )

    def fetch_price_reference(self, product_id: str) -> PriceReference:
        """Current loose/graded price points. Reference only, never a backtest input."""
        try:
            response = httpx.get(
                _BASE_URL,
                params={"t": self.token, "id": product_id},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise DataUnavailable(
                self.name, f"request failed for {product_id}: {exc}"
            ) from exc

        if response.status_code == 401:
            raise DataUnavailable(
                self.name, "401 unauthorised", "Check PRICECHARTING_TOKEN and subscription status."
            )
        if response.status_code != 200:
            raise DataUnavailable(
                self.name, f"HTTP {response.status_code} for {product_id}: {response.text[:200]}"
            )

        payload = response.json()
        if payload.get("status") != "success":
            raise DataUnavailable(
                self.name, f"API reported failure for {product_id}: {payload.get('error-message')}"
            )

        def cents_to_aud(key: str) -> float | None:
            raw = payload.get(key)
            if raw in (None, "", 0):
                return None
            return (float(raw) / 100.0) * self.aud_per_usd

        return PriceReference(
            product_id=product_id,
            product_name=payload.get("product-name", ""),
            loose_aud=cents_to_aud("loose-price"),
            graded_aud=cents_to_aud("graded-price"),
            retrieved_at=date.today(),
        )
