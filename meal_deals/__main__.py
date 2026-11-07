"""Command line entry point: python -m meal_deals --help"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from .fetchers import BrowserFetcher, FetchError, HttpFetcher
from .models import SaleItem, dedupe
from .stores import SCRAPERS

DEFAULT_TERMS = [
    # proteins
    "chicken", "beef mince", "steak", "pork", "lamb", "sausages", "salmon", "fish fillets",
    "prawns", "tofu", "eggs",
    # veg
    "broccoli", "capsicum", "zucchini", "mushrooms", "potatoes", "sweet potato", "carrots",
    "spinach", "tomatoes", "onions", "cauliflower", "pumpkin",
    # carbs and basics
    "pasta", "rice", "noodles", "tortillas", "cheese", "cream", "coconut milk",
]


def scrape(stores: list[str], terms: list[str], use_browser: bool, headful: bool, pages: int) -> list[SaleItem]:
    items: list[SaleItem] = []
    for store in stores:
        fetcher = BrowserFetcher(headless=not headful) if use_browser else HttpFetcher()
        with fetcher:
            scraper = SCRAPERS[store](fetcher, pages=pages)
            try:
                found = scraper.scrape(terms)
            except FetchError as e:
                logging.error("%s: %s", scraper.name, e)
                continue
        logging.info("%s: %d specials found", scraper.name, len(found))
        items.extend(found)
    return dedupe(items)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="meal_deals",
        description="Find Woolworths/Coles specials and plan dinners around them.",
    )
    ap.add_argument("--stores", nargs="+", choices=sorted(SCRAPERS), default=sorted(SCRAPERS))
    ap.add_argument("--terms", nargs="+", default=DEFAULT_TERMS, help="Ingredients to search for specials")
    ap.add_argument("--pages", type=int, default=1, help="Result pages per search term")
    ap.add_argument("--browser", action="store_true", help="Scrape through a real browser (Playwright)")
    ap.add_argument("--headful", action="store_true", help="Show the browser window (with --browser)")
    ap.add_argument("--deals-file", type=Path, help="Load specials from a saved JSON file instead of scraping")
    ap.add_argument("--save-deals", type=Path, help="Save scraped specials to this JSON file")
    ap.add_argument("--min-discount", type=float, default=0, help="Only keep items at least this %% off")
    ap.add_argument("--deals-only", action="store_true", help="List specials, skip recipe generation")
    ap.add_argument("--dinners", type=int, default=5)
    ap.add_argument("--servings", type=int, default=4)
    ap.add_argument("--max-minutes", type=int, help="Max total time per dinner")
    ap.add_argument("--prefs", help='Dietary needs / likes, e.g. "no pork, kid friendly, one vegetarian night"')
    ap.add_argument("--out", type=Path, help="Write the meal plan markdown here (default: stdout)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    if args.deals_file:
        items = [SaleItem.from_dict(d) for d in json.loads(args.deals_file.read_text())]
    else:
        items = scrape(args.stores, args.terms, args.browser, args.headful, args.pages)
        if args.save_deals:
            args.save_deals.write_text(json.dumps([i.to_dict() for i in items], indent=2))
            print(f"Saved {len(items)} specials to {args.save_deals}", file=sys.stderr)

    if args.min_discount:
        items = [i for i in items if i.discount_pct >= args.min_discount]

    if not items:
        print("No specials found. If the sites blocked the request, try --browser (or --headful).", file=sys.stderr)
        return 1

    if args.deals_only:
        for i in sorted(items, key=lambda i: (i.store, -i.discount_pct)):
            print(i.describe())
        return 0

    from .recipes import generate_meal_plan, render_markdown

    print(f"Planning {args.dinners} dinners from {len(items)} specials...", file=sys.stderr)
    plan = generate_meal_plan(
        items, dinners=args.dinners, servings=args.servings, preferences=args.prefs, max_minutes=args.max_minutes
    )
    md = render_markdown(plan).replace(
        "# Dinner plan from this week's specials", f"# Dinner plan from specials ({date.today():%d %b %Y})", 1
    )
    if args.out:
        args.out.write_text(md)
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
