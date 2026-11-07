# meal_deals: dinners from Woolworths and Coles specials

This tool searches Woolworths and Coles for discounted dinner ingredients. It then asks
Claude to plan dinners around those specials, with recipes, estimated costs and a
shopping list for each store.

## Setup

```bash
pip install -r meal_deals/requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...        # only needed for recipe generation
# optional, for --browser mode:
playwright install chromium
```

## Usage (run from the repo root)

```bash
# 1. See what's on special
python -m meal_deals --deals-only -v

# 2. Plan 5 dinners for 4 people
python -m meal_deals --dinners 5 --servings 4 --out dinners.md

# Preferences, time limit, only deals that are at least 25% off
python -m meal_deals --prefs "no pork, kid friendly, one vegetarian night" \
    --max-minutes 40 --min-discount 25 --out dinners.md

# Scrape once and reuse the results (saves time and avoids hitting the sites again)
python -m meal_deals --deals-only --save-deals deals.json
python -m meal_deals --deals-file deals.json --dinners 4

# Only one store, or your own search terms
python -m meal_deals --stores coles --terms chicken salmon broccoli rice
```

If you get 403 errors or "non-JSON" warnings, the sites' bot protection blocked the
plain HTTP client. Add `--browser` to scrape through a real Chromium, and add
`--headful` as well if headless mode is still blocked.

## How it works

| File | What it does |
|---|---|
| `stores/woolworths.py` | Calls the JSON search API behind woolworths.com.au (`/apis/ui/Search/products`) for each search term and keeps the specials |
| `stores/coles.py` | Reads the current Next.js `buildId` from the Coles home page, then calls `/_next/data/<buildId>/en/search/products.json?q=…&filter_Special=all` |
| `fetchers.py` | `HttpFetcher` uses `requests` and `BrowserFetcher` uses Playwright. Both are polite: there's a delay between requests |
| `recipes.py` | Sends the specials, sorted biggest discount first, to Claude (`claude-opus-5-5`) and gets back a validated `MealPlan` |
| `__main__.py` | The command-line interface |

The default search terms are common dinner proteins, vegetables and staples. Pass
`--terms` to change them. Searching by ingredient rather than crawling every special
keeps the list focused on things you can cook with.

## Caveats

- Neither store has a public API. These endpoints are the ones their websites use, and
  they can change without warning. If parsing breaks, update `parse_search_response` in
  the relevant store file. The tests in `tests/` show the expected response shapes.
- Prices are the online prices for the store's default location and may differ in store.
- Use it for personal use at a modest rate, and check each site's terms of use.

## Tests

```bash
python -m pytest meal_deals/tests
```
