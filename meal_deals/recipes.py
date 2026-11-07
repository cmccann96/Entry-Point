"""Turn a list of sale items into dinner recipes using Claude."""
from __future__ import annotations

from typing import List, Optional

import anthropic
from pydantic import BaseModel, Field

from .models import SaleItem

MODEL = "claude-opus-5-5"
MAX_ITEMS_IN_PROMPT = 200


class SaleIngredient(BaseModel):
    item: str = Field(description="Product name exactly as listed in the specials")
    store: str
    sale_price: float
    quantity: str = Field(description="How many packs / how much is needed")


class OtherIngredient(BaseModel):
    item: str
    quantity: str
    pantry_staple: bool = Field(description="True if most households already have it")


class Recipe(BaseModel):
    title: str
    description: str
    servings: int
    prep_minutes: int
    cook_minutes: int
    sale_ingredients: List[SaleIngredient]
    other_ingredients: List[OtherIngredient]
    steps: List[str]
    estimated_total_cost_aud: float
    estimated_savings_aud: float


class MealPlan(BaseModel):
    recipes: List[Recipe]
    shopping_list_woolworths: List[str]
    shopping_list_coles: List[str]
    notes: str


SYSTEM = """You are a practical home cook in Australia helping a household plan weeknight \
dinners around this week's supermarket specials at Woolworths and Coles.

Build each recipe around one or more discounted items from the specials list. Prefer the \
biggest genuine savings and ingredients that work across several meals so nothing goes to \
waste. Ignore specials that are not useful for cooking dinner (snacks, drinks, cleaning \
products, toiletries and the like). Only list an item under sale_ingredients if it appears \
in the specials list; anything else goes under other_ingredients. Use Australian ingredient \
names and metric measures. Keep recipes realistic for a weeknight unless told otherwise. \
Costs are estimates in AUD; base them on the listed sale prices plus typical supermarket \
prices for everything else."""


def _format_items(items: list[SaleItem]) -> str:
    ranked = sorted(items, key=lambda i: (i.discount_pct, i.savings), reverse=True)
    return "\n".join(f"- {i.describe()}" for i in ranked[:MAX_ITEMS_IN_PROMPT])


def build_prompt(items, dinners, servings, preferences, max_minutes) -> str:
    parts = [
        f"Here are this week's specials ({len(items)} items):",
        _format_items(items),
        "",
        f"Plan {dinners} different dinners, each serving {servings}.",
    ]
    if max_minutes:
        parts.append(f"Each dinner should take no more than {max_minutes} minutes in total.")
    if preferences:
        parts.append(f"Household preferences / dietary needs: {preferences}")
    parts.append(
        "Then give a combined shopping list per store, listing only the discounted items to "
        "buy (with quantities), and short notes with any tips (e.g. what to freeze, leftovers)."
    )
    return "\n".join(parts)


def generate_meal_plan(
    items: list[SaleItem],
    dinners: int = 5,
    servings: int = 4,
    preferences: Optional[str] = None,
    max_minutes: Optional[int] = None,
    model: str = MODEL,
    client: Optional[anthropic.Anthropic] = None,
) -> MealPlan:
    if not items:
        raise ValueError("No sale items to plan with")
    client = client or anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": build_prompt(items, dinners, servings, preferences, max_minutes)}],
        output_format=MealPlan,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to generate a meal plan for this request.")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError("The meal plan response was cut off; try fewer dinners.")
    return response.parsed_output


def render_markdown(plan: MealPlan) -> str:
    out: list[str] = ["# Dinner plan from this week's specials", ""]
    for n, r in enumerate(plan.recipes, 1):
        out += [
            f"## {n}. {r.title}",
            "",
            r.description,
            "",
            f"Serves {r.servings} · prep {r.prep_minutes} min · cook {r.cook_minutes} min · "
            f"about ${r.estimated_total_cost_aud:.2f} (saving ~${r.estimated_savings_aud:.2f})",
            "",
            "**On special:**",
        ]
        out += [f"- {i.quantity} {i.item} ({i.store}, ${i.sale_price:.2f})" for i in r.sale_ingredients]
        out += ["", "**You'll also need:**"]
        out += [
            f"- {i.quantity} {i.item}" + (" (pantry)" if i.pantry_staple else "")
            for i in r.other_ingredients
        ]
        out += ["", "**Method:**"]
        out += [f"{k}. {s}" for k, s in enumerate(r.steps, 1)]
        out.append("")
    out += ["## Shopping list", ""]
    for store, lst in (("Woolworths", plan.shopping_list_woolworths), ("Coles", plan.shopping_list_coles)):
        if lst:
            out += [f"**{store}**", ""] + [f"- [ ] {x}" for x in lst] + [""]
    if plan.notes:
        out += ["## Notes", "", plan.notes, ""]
    return "\n".join(out)
