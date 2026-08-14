"""Configuration loading. TOML for settings, .env for secrets."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .backtest import BacktestConfig
from .friction import FrictionConfig
from .sensitivity import MeasuredDistribution
from .variants import Channel, Region, Treatment, Variant


@dataclass
class AppConfig:
    backtest: BacktestConfig
    friction: FrictionConfig
    fx_rates: dict[str, float]
    catalog: dict[str, list[Variant]]
    dispersions: list[MeasuredDistribution]
    sold_sales_csv: Path
    reference_prices: dict[str, float]


def _load_secrets(root: Path) -> None:
    """Load .env into the environment if present. Never fails on absence."""
    env_path = root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def load_config(path: str | Path = "config.toml") -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"config not found: {config_path.resolve()}")

    raw = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    root = config_path.parent
    _load_secrets(root)

    backtest = BacktestConfig(**raw.get("backtest", {}))
    friction = FrictionConfig(**raw.get("friction", {}))
    fx_rates = {k.upper(): float(v) for k, v in raw.get("fx", {}).items()}

    catalog: dict[str, list[Variant]] = {}
    reference_prices: dict[str, float] = {}
    for card in raw.get("catalog", {}).get("cards", []):
        card_number = card["card_number"].upper()
        variants: list[Variant] = []
        for entry in card.get("variants", []):
            variant = Variant(
                card_number=card_number,
                base_rarity=entry["base_rarity"].upper(),
                treatment=Treatment(entry["treatment"]),
                channel=Channel(entry["channel"]),
                region=Region(entry["region"].upper()),
                prior_weight=float(entry.get("prior_weight", 1.0)),
                label=entry.get("label", ""),
            )
            variants.append(variant)
            if "reference_price_aud" in entry:
                reference_prices[variant.key] = float(entry["reference_price_aud"])
        if not variants:
            raise ValueError(f"{card_number}: catalog entry has no variants")
        catalog[card_number] = variants

    dispersions = [
        MeasuredDistribution(
            label=entry["label"],
            median=float(entry["median"]),
            q1=float(entry["q1"]),
            q3=float(entry["q3"]),
            cv=float(entry["cv"]),
            n=int(entry["n"]),
            window_months=float(entry["window_months"]),
        )
        for entry in raw.get("dispersion", [])
        if not entry.get("graded", False)
    ]

    sources = raw.get("sources", {})
    return AppConfig(
        backtest=backtest,
        friction=friction,
        fx_rates=fx_rates,
        catalog=catalog,
        dispersions=dispersions,
        sold_sales_csv=root / sources.get("sold_sales_csv", "data/sold_sales.csv"),
        reference_prices=reference_prices,
    )
