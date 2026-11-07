"""Woolworths specials via the JSON search API that powers woolworths.com.au.

This is an unofficial, undocumented endpoint: if Woolworths changes it, the
parsing in `parse_search_response` is the place to update.
"""
from __future__ import annotations

import logging
from typing import Iterable

from ..fetchers import Fetcher, FetchError
from ..models import SaleItem

log = logging.getLogger(__name__)

BASE = "https://www.woolworths.com.au"
SEARCH_URL = f"{BASE}/apis/ui/Search/products"


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_search_response(data: dict, search_term: str | None = None) -> list[SaleItem]:
    items: list[SaleItem] = []
    for bundle in data.get("Products") or []:
        for p in bundle.get("Products") or []:
            price = _num(p.get("Price"))
            was = _num(p.get("WasPrice"))
            on_special = bool(p.get("IsOnSpecial")) or (was is not None and price is not None and was > price)
            if price is None or not on_special or p.get("IsAvailable") is False:
                continue
            promo = "Half Price" if p.get("IsHalfPrice") else "Special"
            stockcode = p.get("Stockcode")
            slug = p.get("UrlFriendlyName") or ""
            items.append(
                SaleItem(
                    store="Woolworths",
                    name=(p.get("DisplayName") or p.get("Name") or "").strip(),
                    price=price,
                    was_price=was if was and was > price else None,
                    size=p.get("PackageSize"),
                    unit_price=p.get("CupString"),
                    promo=promo,
                    url=f"{BASE}/shop/productdetails/{stockcode}/{slug}" if stockcode else None,
                    search_term=search_term,
                )
            )
    return items


class WoolworthsScraper:
    name = "Woolworths"

    def __init__(self, fetcher: Fetcher, pages: int = 1, page_size: int = 36):
        self.fetcher = fetcher
        self.pages = pages
        self.page_size = page_size
        self._warm = False

    def _search(self, term: str, page: int) -> dict:
        body = {
            "Filters": [],
            "IsSpecial": True,
            "Location": f"/shop/search/products?searchTerm={term}",
            "PageNumber": page,
            "PageSize": self.page_size,
            "SearchTerm": term,
            "SortType": "TraderRelevance",
        }
        return self.fetcher.post_json(SEARCH_URL, body, headers={"Origin": BASE, "Referer": f"{BASE}/"})

    def scrape(self, terms: Iterable[str]) -> list[SaleItem]:
        if not self._warm:
            self.fetcher.warmup(BASE)  # sets the cookies the API requires
            self._warm = True
        results: list[SaleItem] = []
        for term in terms:
            for page in range(1, self.pages + 1):
                try:
                    data = self._search(term, page)
                except FetchError as e:
                    log.warning("Woolworths '%s' page %d failed: %s", term, page, e)
                    break
                found = parse_search_response(data, term)
                log.info("Woolworths '%s' p%d: %d specials", term, page, len(found))
                results.extend(found)
                if not data.get("Products"):
                    break
        return results
