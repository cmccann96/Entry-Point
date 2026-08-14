"""Reference-price corpus, with provenance.

Reference prices drive every EV number in this project, so where each one came
from matters as much as its value. This module keeps that provenance attached
and refuses the two silent failures that would otherwise corrupt the output.

**Region mismatch.** Published OPTCG price corpora (TCGplayer-derived,
PriceCharting, the community APIs) overwhelmingly cover the ENGLISH release.
The Japanese market prices differently, sometimes by multiples. Valuing a JP
variant off an EN quote is not an approximation, it is a different number --
so a mismatch raises rather than passing a plausible-looking figure through.

**Staleness.** A quote from six months ago is not a current benchmark for a
market that moves on set releases and tournament results. Quotes carry a date
and expire.

There is a third problem this module can flag but cannot fix, and it is the
serious one:

**Circularity.** Aggregators derive prices from marketplace sold data, and
assign those sales to variants using listing titles and seller-selected SKUs.
This project exists because those titles are wrong. Mislabelled cheap sales
therefore land in the wrong variant's bucket and depress its published
average -- biasing the benchmark in the same direction as the error being
hunted, from a pipeline whose variant assignment cannot be inspected.

The practical consequence: a corpus-derived benchmark is a floor on a dear
variant's value, not a fair estimate. ``PriceQuote.derivation`` records whether
a quote came from such a pipeline so the distinction survives into the output,
and ``PriceCorpus.circularity_warning`` surfaces it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .sources.base import DataUnavailable
from .variants import Region, Variant

# How a quote's number was arrived at. The distinction is not cosmetic:
# marketplace-derived quotes inherit the mislabelling this project exploits.
DERIVATIONS = {
    "marketplace_aggregate",  # TCGplayer/PriceCharting-style. Circularity applies.
    "operator",  # hand-entered from the operator's own knowledge.
    "observed_sale",  # a specific sale the operator verified themselves.
}


@dataclass(frozen=True)
class PriceQuote:
    """One reference price for one variant, with where it came from."""

    variant_key: str
    price_aud: float
    source: str
    as_of: date
    market: Region
    derivation: str = "marketplace_aggregate"
    currency_original: str = "AUD"
    price_original: float | None = None

    def __post_init__(self) -> None:
        if self.price_aud <= 0:
            raise ValueError(f"non-positive price for {self.variant_key}")
        if self.derivation not in DERIVATIONS:
            raise ValueError(
                f"derivation must be one of {sorted(DERIVATIONS)}, got {self.derivation!r}"
            )

    def age_days(self, today: date | None = None) -> int:
        return ((today or date.today()) - self.as_of).days

    def is_stale(self, max_age_days: int, today: date | None = None) -> bool:
        return self.age_days(today) > max_age_days

    @property
    def inherits_mislabelling(self) -> bool:
        """True when this quote came from a pipeline that buckets by seller labels."""
        return self.derivation == "marketplace_aggregate"


class PriceCorpus:
    """Reference prices keyed by variant, with provenance enforced on lookup."""

    def __init__(
        self,
        quotes: list[PriceQuote],
        max_age_days: int = 60,
        allow_region_mismatch: bool = False,
    ) -> None:
        self.max_age_days = max_age_days
        self.allow_region_mismatch = allow_region_mismatch
        self._quotes: dict[str, PriceQuote] = {}
        # Keep the freshest quote per variant when a corpus carries several.
        for quote in sorted(quotes, key=lambda q: q.as_of):
            self._quotes[quote.variant_key] = quote

    def __len__(self) -> int:
        return len(self._quotes)

    def get(self, variant: Variant, today: date | None = None) -> PriceQuote | None:
        """Return the quote for ``variant``, or None if unusable.

        Unusable means missing, stale, or priced in a different regional market.
        Returning None rather than a wrong number is the point: a missing
        benchmark makes a listing unscoreable, which is recoverable. A benchmark
        from the wrong market produces a confident, wrong EV.
        """
        quote = self._quotes.get(variant.key)
        if quote is None:
            return None
        if quote.is_stale(self.max_age_days, today):
            return None
        if quote.market is not variant.region and not self.allow_region_mismatch:
            return None
        return quote

    def as_reference_prices(self, today: date | None = None) -> dict[str, float]:
        """Flat variant_key -> AUD mapping, for the valuation and scan paths."""
        return {
            key: quote.price_aud
            for key, quote in self._quotes.items()
            if not quote.is_stale(self.max_age_days, today)
        }

    def coverage(self, catalog: dict[str, list[Variant]], today: date | None = None):
        """Which catalog variants have a usable price, and which do not."""
        covered: list[str] = []
        missing: list[str] = []
        for variants in catalog.values():
            for variant in variants:
                target = covered if self.get(variant, today) else missing
                target.append(variant.key)
        return covered, missing

    def circularity_warning(self) -> str | None:
        """Warn when quotes inherit the mislabelling this project exploits."""
        affected = [q for q in self._quotes.values() if q.inherits_mislabelling]
        if not affected:
            return None
        return (
            f"{len(affected)} of {len(self._quotes)} quotes are marketplace "
            "aggregates. Those pipelines bucket sales by seller-supplied labels, "
            "which is the error this project exploits -- mislabelled cheap sales "
            "depress the dear variant's published average. Treat these as a FLOOR "
            "on a dear variant's value, not a fair estimate: a listing that clears "
            "friction against a floor clears it against the truth, but a marginal "
            "one may be better than it looks."
        )

    def stale_quotes(self, today: date | None = None) -> list[PriceQuote]:
        return [q for q in self._quotes.values() if q.is_stale(self.max_age_days, today)]

    def region_mismatches(self, catalog: dict[str, list[Variant]]) -> list[tuple[str, str]]:
        """Variants whose only quote comes from a different regional market."""
        found: list[tuple[str, str]] = []
        for variants in catalog.values():
            for variant in variants:
                quote = self._quotes.get(variant.key)
                if quote and quote.market is not variant.region:
                    found.append((variant.key, f"{quote.market.value} quote for a "
                                               f"{variant.region.value} card"))
        return found


def load_corpus(
    path: str | Path,
    catalog: dict[str, list[Variant]],
    fx_rates: dict[str, float],
    default_source: str = "import",
    default_derivation: str = "marketplace_aggregate",
    max_age_days: int = 60,
) -> PriceCorpus:
    """Load a price corpus from a local CSV/TSV/HTML export.

    Deliberately reuses the forgiving ingestion path, because price corpora
    arrive in whatever shape the source emits. Matching is by card number plus
    treatment, since that is what every published corpus actually keys on --
    variant key strings are this project's internal convention and no external
    source will use them.

    Recognised columns (auto-detected, case-insensitive):
        card_number, treatment, price, currency, market, as_of, source
    """
    # NOTE: deliberately does NOT reuse ingest.map_columns -- that mapper is for
    # sales rows and requires sold_date/title, which a price corpus has neither
    # of. Only the field-level parsers are shared.
    from .ingest import parse_date, parse_price, read_table

    headers, rows = read_table(Path(path))
    number_col = _find(headers, "card_number") or _find(headers, "card") or _find(headers, "number")
    price_col = _find(headers, "price") or _find(headers, "market_price") or _find(headers, "value")
    if not number_col or not price_col:
        raise DataUnavailable(
            "prices",
            f"need a card-number column and a price column; found: {headers}",
            "Rename the columns, or export with headers named card_number and price.",
        )

    treatment_col = _find(headers, "treatment") or _find(headers, "variant")
    currency_col = _find(headers, "currency")
    market_col = _find(headers, "market") or _find(headers, "region")
    asof_col = _find(headers, "as_of") or _find(headers, "date")
    source_col = _find(headers, "source")

    by_number: dict[str, list[Variant]] = {}
    for variants in catalog.values():
        for variant in variants:
            by_number.setdefault(variant.card_number, []).append(variant)

    quotes: list[PriceQuote] = []
    problems: list[str] = []

    for lineno, row in enumerate(rows, start=2):
        try:
            number = (row.get(number_col, "") or "").strip().upper()
            if not number:
                raise ValueError("no card_number")
            treatment = (row.get(treatment_col, "") or "").strip().lower() if treatment_col else ""
            candidates = [
                v for v in by_number.get(number, [])
                if not treatment or v.treatment.value == treatment
            ]
            if not candidates:
                continue  # not a card we track; skip quietly
            if len(candidates) > 1:
                raise ValueError(
                    f"{number} treatment {treatment!r} matches {len(candidates)} variants"
                )

            declared = (row.get(currency_col, "") or "").strip().upper() if currency_col else ""
            amount, currency = parse_price(row.get(price_col, ""), declared or None)
            if currency not in fx_rates:
                raise ValueError(f"no FX rate for {currency}")

            market = Region((row.get(market_col, "") or "EN").strip().upper()) \
                if market_col else Region.EN
            as_of = parse_date(row.get(asof_col, "")) if asof_col else date.today()

            quotes.append(
                PriceQuote(
                    variant_key=candidates[0].key,
                    price_aud=amount * fx_rates[currency],
                    source=(row.get(source_col, "") if source_col else "") or default_source,
                    as_of=as_of,
                    market=market,
                    derivation=default_derivation,
                    currency_original=currency,
                    price_original=amount,
                )
            )
        except (ValueError, KeyError) as exc:
            problems.append(f"  line {lineno}: {exc}")

    if problems:
        raise DataUnavailable(
            "prices",
            f"{len(problems)} unusable row(s) in {path}:\n" + "\n".join(problems[:20]),
            "Fix or remove these rows. Prices drive every EV, so a bad one is "
            "worse than a missing one.",
        )
    if not quotes:
        raise DataUnavailable("prices", f"{path} yielded no quotes for catalog cards")

    return PriceCorpus(quotes, max_age_days=max_age_days)


def _find(headers: list[str], wanted: str) -> str | None:
    """Case- and punctuation-insensitive header lookup."""
    import re

    target = re.sub(r"[^a-z0-9]", "", wanted.lower())
    for header in headers:
        if re.sub(r"[^a-z0-9]", "", header.lower()) == target:
            return header
    return None


def corpus_from_config(reference_prices: dict[str, float], catalog) -> PriceCorpus:
    """Wrap operator-supplied config prices as a corpus.

    Config prices are hand-entered, so they carry ``derivation="operator"`` and
    no circularity warning -- and their market is taken from the variant itself,
    since the operator priced that specific card.
    """
    quotes: list[PriceQuote] = []
    by_key = {v.key: v for variants in catalog.values() for v in variants}
    for key, price in reference_prices.items():
        variant = by_key.get(key)
        if variant is None:
            continue
        quotes.append(
            PriceQuote(
                variant_key=key,
                price_aud=price,
                source="config.toml",
                as_of=date.today(),
                market=variant.region,
                derivation="operator",
            )
        )
    return PriceCorpus(quotes, max_age_days=10_000)
