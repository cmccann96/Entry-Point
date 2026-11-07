import json
from pathlib import Path
from types import SimpleNamespace

from meal_deals.models import SaleItem, dedupe
from meal_deals.recipes import MealPlan, build_prompt, generate_meal_plan, render_markdown
from meal_deals.stores import coles, woolworths

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def test_woolworths_parse_keeps_only_specials():
    items = woolworths.parse_search_response(load("woolworths_search.json"), "chicken")
    assert [i.name for i in items] == [
        "Woolworths RSPCA Chicken Breast Fillets Large Pack",
        "San Remo Spaghetti No 5",
    ]
    chicken, pasta = items
    assert chicken.was_price == 16.5 and chicken.discount_pct == 33.3
    assert chicken.url.endswith("/shop/productdetails/123/woolworths-rspca-chicken-breast-fillets")
    assert pasta.promo == "Half Price" and pasta.discount_pct == 50.0


def test_coles_parse_keeps_only_specials():
    items = coles.parse_search_response(load("coles_search.json"), "beef mince")
    assert len(items) == 2
    mince = items[0]
    assert mince.name == "Coles Beef Mince 3 Star"
    assert (mince.price, mince.was_price, mince.savings) == (6.0, 8.0, 2.0)
    assert mince.promo == "Percent Off"
    assert mince.url.endswith("-5551")
    kofta = items[1]
    assert (kofta.price, kofta.was_price, kofta.promo) == (10.0, 11.0, "Buy 2 for $20")


def test_coles_build_id_extraction():
    html = '<html><script id="__NEXT_DATA__" type="application/json">{"buildId":"abc123","page":"/"}</script></html>'
    assert coles.extract_build_id(html) == "abc123"
    assert coles.extract_build_id('..."buildId":"xyz"...') == "xyz"
    assert coles.extract_build_id("nothing") is None


class FakeFetcher:
    def __init__(self):
        self.calls = []

    def warmup(self, url):
        self.calls.append(("warmup", url))
        return '<script id="__NEXT_DATA__">{"buildId":"B1"}</script>'

    def get_json(self, url, params=None, headers=None):
        self.calls.append(("get", url, params))
        return load("coles_search.json")

    def post_json(self, url, body, headers=None):
        self.calls.append(("post", url, body["SearchTerm"]))
        return load("woolworths_search.json")


def test_scrapers_with_fake_fetcher():
    f = FakeFetcher()
    ww = woolworths.WoolworthsScraper(f).scrape(["chicken", "pasta"])
    cl = coles.ColesScraper(f).scrape(["mince"])
    assert len(ww) == 4 and len(dedupe(ww)) == 2
    assert len(cl) == 2
    assert ("get", "https://www.coles.com.au/_next/data/B1/en/search/products.json",
            {"q": "mince", "page": 1, "filter_Special": "all"}) in f.calls


def test_sale_item_roundtrip():
    item = SaleItem("Coles", "Thing", 2.0, 4.0)
    assert SaleItem.from_dict(item.to_dict()) == item


PLAN = {
    "recipes": [{
        "title": "Chicken Spaghetti", "description": "Quick pasta.", "servings": 4,
        "prep_minutes": 10, "cook_minutes": 20,
        "sale_ingredients": [{"item": "San Remo Spaghetti No 5", "store": "Woolworths", "sale_price": 1.6, "quantity": "1 pack"}],
        "other_ingredients": [{"item": "garlic", "quantity": "2 cloves", "pantry_staple": True}],
        "steps": ["Boil pasta.", "Toss."], "estimated_total_cost_aud": 12.5, "estimated_savings_aud": 7.1,
    }],
    "shopping_list_woolworths": ["1 x San Remo Spaghetti"], "shopping_list_coles": [], "notes": "Freeze leftovers.",
}


class FakeClient:
    def __init__(self):
        self.kwargs = None
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(stop_reason="end_turn", parsed_output=MealPlan.model_validate(PLAN))


def test_generate_and_render():
    items = woolworths.parse_search_response(load("woolworths_search.json"))
    client = FakeClient()
    plan = generate_meal_plan(items, dinners=1, servings=4, preferences="no pork", client=client)
    prompt = client.kwargs["messages"][0]["content"]
    assert "Half Price" in prompt and "no pork" in prompt and "Plan 1 different dinners" in prompt
    assert client.kwargs["output_format"] is MealPlan
    md = render_markdown(plan)
    assert "## 1. Chicken Spaghetti" in md and "- [ ] 1 x San Remo Spaghetti" in md


def test_prompt_ranks_biggest_discount_first():
    items = [SaleItem("Coles", "Small", 9.0, 10.0), SaleItem("Coles", "Big", 5.0, 10.0)]
    prompt = build_prompt(items, 3, 2, None, 30)
    assert prompt.index("Big") < prompt.index("Small")
    assert "no more than 30 minutes" in prompt
