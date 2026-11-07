"""HTTP access to the supermarket sites.

Both Woolworths and Coles sit behind bot protection (Akamai / Imperva). The
plain `requests` fetcher works from many home connections, but if you get 403s
or HTML challenge pages instead of JSON, use the Playwright browser fetcher
(`--browser`), which loads the real site first and then calls the JSON
endpoints from inside the page so cookies and headers look like a normal visit.
"""
from __future__ import annotations

import json
import random
import time
from typing import Any, Optional
from urllib.parse import urlencode

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


class FetchError(RuntimeError):
    pass


class Fetcher:
    def warmup(self, url: str) -> str:
        """Visit a page (to pick up cookies) and return its HTML."""
        raise NotImplementedError

    def get_json(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> Any:
        raise NotImplementedError

    def post_json(self, url: str, body: dict, headers: Optional[dict] = None) -> Any:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class HttpFetcher(Fetcher):
    def __init__(self, delay: float = 1.0, timeout: float = 30.0):
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-AU,en;q=0.9",
                "Accept": "application/json, text/plain, */*",
            }
        )

    def _pause(self) -> None:
        if self.delay:
            time.sleep(self.delay + random.uniform(0, self.delay / 2))

    def _json(self, resp: requests.Response) -> Any:
        if resp.status_code >= 400:
            raise FetchError(f"{resp.request.method} {resp.url} -> HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as e:
            raise FetchError(
                f"{resp.url} returned non-JSON (probably a bot challenge page). "
                "Try again with --browser."
            ) from e

    def warmup(self, url: str) -> str:
        resp = self.session.get(url, timeout=self.timeout, headers={"Accept": "text/html"})
        if resp.status_code >= 400:
            raise FetchError(f"GET {url} -> HTTP {resp.status_code}. Try again with --browser.")
        self._pause()
        return resp.text

    def get_json(self, url, params=None, headers=None):
        resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        self._pause()
        return self._json(resp)

    def post_json(self, url, body, headers=None):
        resp = self.session.post(url, json=body, headers=headers, timeout=self.timeout)
        self._pause()
        return self._json(resp)

    def close(self):
        self.session.close()


class BrowserFetcher(Fetcher):
    """Drives a real Chromium via Playwright (pip install playwright && playwright install chromium)."""

    def __init__(self, headless: bool = True, delay: float = 1.0):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            raise FetchError(
                "Playwright is not installed. Run: pip install playwright && playwright install chromium"
            ) from e
        self.delay = delay
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless)
        self._context = self._browser.new_context(user_agent=USER_AGENT, locale="en-AU")
        self.page = self._context.new_page()

    def warmup(self, url: str) -> str:
        self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self.page.wait_for_timeout(3000)
        return self.page.content()

    def _fetch(self, url: str, method: str, body: Optional[dict], headers: Optional[dict]) -> Any:
        result = self.page.evaluate(
            """async ([url, method, body, headers]) => {
                const opts = {method, credentials: 'include',
                              headers: Object.assign({'Accept': 'application/json'}, headers || {})};
                if (body !== null) {
                    opts.body = JSON.stringify(body);
                    opts.headers['Content-Type'] = 'application/json';
                }
                const r = await fetch(url, opts);
                return {status: r.status, text: await r.text()};
            }""",
            [url, method, body, headers],
        )
        time.sleep(self.delay)
        if result["status"] >= 400:
            raise FetchError(f"{method} {url} -> HTTP {result['status']}")
        try:
            return json.loads(result["text"])
        except ValueError as e:
            raise FetchError(f"{url} returned non-JSON content") from e

    def get_json(self, url, params=None, headers=None):
        if params:
            url = f"{url}?{urlencode(params)}"
        return self._fetch(url, "GET", None, headers)

    def post_json(self, url, body, headers=None):
        return self._fetch(url, "POST", body, headers)

    def close(self):
        self._context.close()
        self._browser.close()
        self._pw.stop()
