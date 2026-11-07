from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class SaleItem:
    store: str  # "Woolworths" or "Coles"
    name: str
    price: float
    was_price: Optional[float] = None
    size: Optional[str] = None
    unit_price: Optional[str] = None  # e.g. "$12.50 / 1KG"
    promo: Optional[str] = None  # e.g. "Half Price", "Down Down"
    url: Optional[str] = None
    search_term: Optional[str] = None

    @property
    def savings(self) -> float:
        if self.was_price and self.was_price > self.price:
            return round(self.was_price - self.price, 2)
        return 0.0

    @property
    def discount_pct(self) -> float:
        if self.was_price and self.was_price > 0 and self.was_price > self.price:
            return round(100 * (self.was_price - self.price) / self.was_price, 1)
        return 0.0

    def key(self) -> tuple:
        return (self.store, self.name.lower())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["savings"] = self.savings
        d["discount_pct"] = self.discount_pct
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SaleItem":
        fields = {k: d.get(k) for k in cls.__dataclass_fields__}
        return cls(**fields)

    def describe(self) -> str:
        bits = [f"[{self.store}] {self.name}"]
        if self.size:
            bits.append(f"({self.size})")
        price = f"${self.price:.2f}"
        if self.was_price and self.was_price > self.price:
            price += f" (was ${self.was_price:.2f}, {self.discount_pct:.0f}% off)"
        bits.append(price)
        if self.promo:
            bits.append(f"- {self.promo}")
        return " ".join(bits)


def dedupe(items: list[SaleItem]) -> list[SaleItem]:
    seen: dict[tuple, SaleItem] = {}
    for item in items:
        seen.setdefault(item.key(), item)
    return list(seen.values())
