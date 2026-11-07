"""Coles specials via the Next.js data routes behind coles.com.au search.

Coles serves search results as JSON at /_next/data/<buildId>/en/search/products.json.
The buildId changes with every Coles deploy, so it is read from the home page.
Unofficial and undocumented: update `parse_search_response` if the shape changes.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterable, Optional

from ..fetchers import Fetcher, FetchError
from ..models import SaleItem

log = logging.getLogger(__name__)

BASE = "https://www.coles.com.au"


def extract_build_id(html: str) -> Optional[str]:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            return json.loads(m.group(1)).get("buildId")
        except ValueError:
            pass
    m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
    return m.group(1) if m else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_search_response(data: dict, search_term: str | None = None) -> list[SaleItem]:
    page_props = data.get("pageProps") or data
    results = (page_props.get("searchResults") or {}).get("results") or []
    items: list[SaleItem] = []
    for r in results:
        if r.get("_type") != "PRODUCT":
            continue
        pricing = r.get("pricing") or {}
        price = pricing.get("now")
        was = pricing.get("was") or None
        promo_type = pricing.get("promotionType")
        on_special = (was and price and was > price) or promo_type == "SPECIAL" or pricing.get("onlineSpecial")
        if price is None or not on_special:
            continue
        brand = (r.get("brand") or "").strip()
        name = (r.get("name") or "").strip()
        full = f"{brand} {name}".strip() if brand and not name.lower().startswith(brand.lower()) else name
        size = r.get("size")
        unit = (pricing.get("comparable") or None)
        price, was = float(price), (float(was) if was and was > price else None)
        mb = pricing.get("multiBuyPromotion") or {}
        reward = mb.get("reward")
        if reward and (mb.get("minQuantity") or 0) > 1 and reward < price:
            # "Buy 2 for $20": reward is the per-item price when buying the multibuy quantity
            was, price = price, float(reward)
        promo = pricing.get("offerDescription")
        if not promo:
            special = pricing.get("specialType")
            promo = special.replace("_", " ").title() if special else "Special"
        pid = r.get("id")
        items.append(
            SaleItem(
                store="Coles",
                name=full,
                price=price,
                was_price=was,
                size=size,
                unit_price=unit,
                promo=promo,
                url=f"{BASE}/product/{_slug(full + ' ' + (size or ''))}-{pid}" if pid else None,
                search_term=search_term,
            )
        )
    return items


class ColesScraper:
    name = "Coles"

    def __init__(self, fetcher: Fetcher, pages: int = 1):
        self.fetcher = fetcher
        self.pages = pages
        self.build_id: Optional[str] = None

    def _ensure_build_id(self) -> str:
        if self.build_id:
            return self.build_id
        html = self.fetcher.warmup(BASE)
        self.build_id = extract_build_id(html)
        if not self.build_id:
            raise FetchError("Could not find the Coles buildId (blocked? try --browser)")
        return self.build_id

    def scrape(self, terms: Iterable[str]) -> list[SaleItem]:
        build_id = self._ensure_build_id()
        url = f"{BASE}/_next/data/{build_id}/en/search/products.json"
        results: list[SaleItem] = []
        for term in terms:
            for page in range(1, self.pages + 1):
                params = {"q": term, "page": page, "filter_Special": "all"}
                try:
                    data = self.fetcher.get_json(url, params=params, headers={"x-nextjs-data": "1"})
                except FetchError as e:
                    log.warning("Coles '%s' page %d failed: %s", term, page, e)
                    break
                found = parse_search_response(data, term)
                log.info("Coles '%s' p%d: %d specials", term, page, len(found))
                results.extend(found)
                total = ((data.get("pageProps") or {}).get("searchResults") or {}).get("noOfResults") or 0
                if page * 48 >= total:
                    break
        return results
