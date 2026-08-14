"""eBay Browse API adapter -- ACTIVE LISTINGS ONLY.

The Browse API has no access to sold-item data; eBay classifies that as
restricted. The Marketplace Insights API, which does expose sold prices, is a
limited-release API described in eBay's own documentation as restricted and not
open to new users.

Consequence for this project: the Browse API supplies the *ask* side of Stage 2
and can never supply the trailing-median denominator that the cheapness score
compares against. That denominator has to come from the operator's own Terapeak
export. This is not a limitation of the adapter; it is the central constraint on
the whole strategy.

This module is Stage 2 infrastructure. Stage 2 is gated on the Stage 1 verdict
and is not wired up.
"""

from __future__ import annotations

import base64
from datetime import date

import httpx

from .base import ActiveListing, DataUnavailable, SourceCapabilityError

_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_SCOPE = "https://api.ebay.com/oauth/api_scope"


class EbayBrowseSource:
    name = "ebay-browse"

    def __init__(
        self,
        client_id: str | None,
        client_secret: str | None,
        marketplace_id: str = "EBAY_AU",
        timeout: float = 20.0,
    ) -> None:
        if not client_id or not client_secret:
            raise DataUnavailable(
                self.name,
                "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not set",
                "Create an application keyset at developer.ebay.com and add both to .env.",
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.marketplace_id = marketplace_id
        self.timeout = timeout
        self._token: str | None = None

    def fetch_sold(self, card_number: str, start: date, end: date):
        raise SourceCapabilityError(
            self.name,
            "the eBay Browse API returns active listings only and has no access "
            "to sold-item data",
            "Sold prices require the Marketplace Insights API, which is "
            "restricted and closed to new users. Use a Terapeak CSV export.",
        )

    def _access_token(self) -> str:
        if self._token:
            return self._token

        credential = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        try:
            response = httpx.post(
                _OAUTH_URL,
                headers={
                    "Authorization": f"Basic {credential}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials", "scope": _SCOPE},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise DataUnavailable(self.name, f"OAuth request failed: {exc}") from exc

        if response.status_code != 200:
            raise DataUnavailable(
                self.name,
                f"OAuth failed: HTTP {response.status_code} {response.text[:200]}",
                "Verify the keyset is a production keyset and the secret is current.",
            )

        self._token = response.json()["access_token"]
        return self._token

    def fetch_active(self, card_number: str, limit: int = 100) -> list[ActiveListing]:
        """Live asks matching a card number.

        Searching by card number rather than card name is deliberate: sellers
        transcribe the number off the card, so it is the one reliable key.
        """
        try:
            response = httpx.get(
                _BROWSE_URL,
                headers={
                    "Authorization": f"Bearer {self._access_token()}",
                    "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id,
                },
                params={"q": card_number, "limit": limit},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise DataUnavailable(self.name, f"search failed for {card_number}: {exc}") from exc

        if response.status_code != 200:
            raise DataUnavailable(
                self.name,
                f"HTTP {response.status_code} for {card_number}: {response.text[:200]}",
            )

        listings: list[ActiveListing] = []
        for item in response.json().get("itemSummaries", []) or []:
            price = item.get("price") or {}
            if price.get("currency") != "AUD":
                # Converting here would need an FX rate; Stage 2 should pass one
                # in explicitly rather than guess.
                continue
            images = tuple(
                img["imageUrl"]
                for img in ([item.get("image")] + (item.get("additionalImages") or []))
                if img and img.get("imageUrl")
            )
            listings.append(
                ActiveListing(
                    listing_id=item.get("itemId", ""),
                    card_number=card_number,
                    title=item.get("title", ""),
                    ask_aud=float(price.get("value", 0.0)),
                    url=item.get("itemWebUrl", ""),
                    seller_country=(item.get("itemLocation") or {}).get("country"),
                    image_urls=images,
                    source=self.name,
                )
            )
        return listings
