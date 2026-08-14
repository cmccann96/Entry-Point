"""Data source contracts.

Every source in this package obeys one rule: **if the data is not there, raise.**
No source may return synthesised, interpolated, or placeholder sales. A backtest
run against invented data is worse than no backtest, because it produces a
verdict that reads as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable


class DataUnavailable(RuntimeError):
    """Raised when a source cannot supply the requested data.

    Carries a remediation hint so failures are actionable rather than merely
    loud.
    """

    def __init__(self, source: str, reason: str, remedy: str = "") -> None:
        message = f"[{source}] {reason}"
        if remedy:
            message += f"\n  -> {remedy}"
        super().__init__(message)
        self.source = source
        self.reason = reason
        self.remedy = remedy


class SourceCapabilityError(DataUnavailable):
    """Raised when a source is asked for data it structurally cannot provide.

    Distinct from a missing credential or a network failure: no amount of
    configuration will make this call succeed.
    """


@dataclass(frozen=True)
class SoldSale:
    """One realised sale.

    ``price_aud`` is the realised price converted to AUD. ``title`` is retained
    verbatim because listing text is the signal this whole thesis rests on.
    """

    card_number: str
    sold_date: date
    price_aud: float
    title: str
    currency_original: str = "AUD"
    price_original: float | None = None
    is_graded: bool = False
    grade: str | None = None
    grader: str | None = None
    source: str = "unknown"

    def __post_init__(self) -> None:
        if self.price_aud <= 0:
            raise ValueError(f"non-positive price for {self.card_number}: {self.price_aud}")


@dataclass(frozen=True)
class ActiveListing:
    """One live ask. Used by Stage 2 only."""

    listing_id: str
    card_number: str
    title: str
    ask_aud: float
    url: str
    seller_country: str | None = None
    image_urls: tuple[str, ...] = ()
    source: str = "unknown"


@runtime_checkable
class SoldSalesSource(Protocol):
    """A source of realised sale history.

    Stage 1 requires this. As of 2026-08 no official *API* implements it -- see
    README.md "Data sources". The only compliant implementation is a manual
    export from a source the operator is entitled to (Terapeak).
    """

    name: str

    def fetch_sold(
        self, card_number: str, start: date, end: date
    ) -> list[SoldSale]:  # pragma: no cover - protocol
        ...


@runtime_checkable
class ActiveListingSource(Protocol):
    """A source of live asks. Stage 2."""

    name: str

    def fetch_active(self, card_number: str) -> list[ActiveListing]:  # pragma: no cover
        ...
