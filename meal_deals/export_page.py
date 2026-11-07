"""Turn saved specials into the data document the Dinner Deals page reads.

    python -m meal_deals.export_page deals.json page_data.json

Drops non-food items (pet food, snacks, toiletries...), tags each item with a
category and sub-type from its name (see categories.py), keeps its unit price,
and writes a compact JSON object (well under the page database's 256 KiB document limit).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .categories import CATEGORY_ORDER, classify
from .models import SaleItem

JUNK = re.compile(
    r"\b(cat|cats|dog|dogs|pet|kitten|puppy|whiskas|felix|sheba|dine|schmackos|pedigree|applaws|"
    r"diffuser|air ?wick|botanica|candle|shampoo|conditioner|body wash|soap|deodorant|"
    r"ice cream|gelato|frosting|dip|crackers?|biscuits?|cookies?|chocolate|lolly|lollies|cake|"
    r"croutons|puffs|chips?|crisps|popcorn|bars?|snacks?|drink|juice|soft drink|cola|"
    r"protein shake|smoothie|yoghurt pouch|baby food|toddler|custard|dessert|pudding|"
    r"lip balm|sunscreen|toothpaste|nappies|wipes|cakes|rings|pringles|nudie|\d+ ?months|on the go|hommus|hummus|"
    r"strawberries|creamy rice|creamed rice|rice pudding|jatz|herbal essences|leave in|hair|cocktail|moisturi[sz]er|serum|cleanser|lotion|nachips|corn chips|furikake)\b",
    re.I,
)
# frozen potato chips are a fine dinner side, so keep those
KEEP = re.compile(r"\b(frozen\b.*\bchips|oven chips|wedges)\b", re.I)


def is_dinner_food(item: SaleItem) -> bool:
    return bool(KEEP.search(item.name)) or not JUNK.search(item.name)


def build_doc(items: list[SaleItem]) -> dict:
    rows = []
    for i in items:
        if not is_dinner_food(i):
            continue
        cat, sub = classify(i.name)
        rows.append({
            "s": i.store,
            "n": i.name,
            "p": i.price,
            "w": i.was_price,
            "z": i.size,
            "o": i.promo,
            "c": cat,
            "t": sub,
            "k": i.unit_price,
            "u": i.url,
        })
    rows.sort(key=lambda r: -((r["w"] - r["p"]) / r["w"] if r["w"] else 0))
    return {
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "stores": sorted({r["s"] for r in rows}),
        "count": len(rows),
        "categories": CATEGORY_ORDER,
        "items": rows,
    }


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) != 2:
        print(__doc__)
        return 2
    src, dst = map(Path, argv)
    items = [SaleItem.from_dict(d) for d in json.loads(src.read_text())]
    doc = build_doc(items)
    dst.write_text(json.dumps(doc, separators=(",", ":")))
    print(f"{doc['count']} dinner items of {len(items)} specials -> {dst} ({dst.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
