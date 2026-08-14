# OPTCG variant mispricing scanner

Research harness for finding mispriced One Piece TCG cards. **Stage 1 only** — the live scanner is gated on a backtest verdict that has not been cleared.

👉 **Read [`VERDICT.md`](VERDICT.md) first.** It contains the go/no-go analysis. Short version: on the operator's own measured dispersion the strategy misses its gate by ~6×, and the specified data sources cannot supply the backtest at all.

---

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"

uv run optcg-backtest feasibility   # analytic study — runs today, needs no credentials
uv run optcg-backtest friction      # breakeven discount by price point and channel
uv run optcg-backtest run           # Stage 1 backtest — needs a real sold-sales export
uv run pytest                       # 81 tests
```

`run` exits `3` with a remediation message until you supply real data. That is intended behaviour, not a bug.

---

## Data sources — read this before wiring anything up

The brief assumed PriceCharting could supply historical sold prices and that eBay's Browse API could supply the trailing median. **Neither is true.** Verified 2026-08:

| Source | Returns | Stage 1? |
|---|---|---|
| PriceCharting API | Current values only; historic sales explicitly unsupported | ❌ |
| eBay Browse API | Active listings only; sold data is "Restricted" | ❌ |
| eBay Marketplace Insights API | Sold prices — but "restricted and not open to new users" | ❌ |
| TCGplayer API | Public developer access effectively closed | ❌ |
| **Terapeak** (Seller Hub → Research) | ~3yr sold history, free with a seller account, **manual CSV export** | ✅ |

Every third-party service selling "eBay sold data APIs" is a scraper behind a REST endpoint. Using one violates the project's own hard constraint, so none are integrated.

**Terapeak export is the only compliant path.** It is your own account's data via a supported feature.

### Export format

Save to `data/sold_sales.csv` (path configurable in `config.toml`):

```csv
card_number,sold_date,price,currency,title,graded,grade,grader
OP06-118,2026-03-14,1098.00,AUD,"One Piece OP06-118 Zoro Manga Rare",,,
OP06-118,2026-04-02,860.00,USD,"OP06-118 Zoro SEC parallel",,,
```

- `sold_date` must be ISO `YYYY-MM-DD`.
- Every currency present must have a rate under `[fx]` in `config.toml`. A missing rate is a **hard error** — silently treating USD as AUD would understate overseas prices by ~50%.
- Unparseable rows abort the load with line numbers. They are not skipped: quietly discarding input biases the measured distribution.

---

## Design decisions that affect whether the answer is true

**Trailing medians are per-variant, never per card number.** One card number spans a 50×+ price range. Benchmarking a plain SEC against the manga variant's median makes every cheap printing look like a 97% discount. This is the single easiest way to manufacture a spuriously positive backtest, and there is a regression test for it.

**Benchmarks are strictly backward-looking and exclude the sale being judged.** Including it leaks the answer into its own benchmark.

**Minimum comparables before a sale is judged** (default 5). A median over two observations is noise. Unqualified sales are counted and reported, not silently dropped.

**Graded sales excluded from the tradeable distribution.** Measured CV 10.8% graded vs 31.1% raw — the slab label removes the ambiguity that creates the edge. Detected from both the `graded` column and the title (`PSA`, `BGS`, `CGC`, …).

**Friction is modelled component-wise, not as a blended percentage.** The blended ~30/20/8% figures hide that a A$12 postage leg is 24% of a A$50 card and 0.4% of a A$3,000 one. The component model reproduces the blended figures at typical price points (there is a test asserting this) while exposing two effects a flat rate cannot: sub-A$300 cards are arithmetically dead overseas, and there is a **non-monotonic notch at A$1,000** where customs clearance makes a dearer card a worse trade.

**Three ambiguity signals, not one.** Posterior entropy alone is insufficient — with a concentrated population prior, a title carrying no information yields a *concentrated* posterior and so scores as low-entropy, i.e. confidently identified. See `VERDICT.md` §7. The module exposes `ambiguity` (entropy), `information_gain` (KL from prior — did the seller disambiguate anything), `is_undescribed`, and `price_dispersion` (what the ambiguity is worth in dollars).

---

## Layout

```
optcg/
  variants.py     card-number extraction, 4-axis taxonomy, Bayesian posterior
  friction.py     AUD round-trip costs: GST, customs, postage, platform fees
  backtest.py     Stage 1 harness + gate enforcement
  sensitivity.py  analytic feasibility study from measured summary stats
  stats.py        stdlib-only quantiles, normal CDF/PPF
  config.py       TOML settings + .env secrets
  cli.py          feasibility / run / friction
  sources/
    base.py           contracts; DataUnavailable, SourceCapabilityError
    csv_source.py     Terapeak export — the only compliant Stage 1 input
    pricecharting.py  current-price reference only; fetch_sold() raises
    ebay_browse.py    active listings only; fetch_sold() raises
```

`sensitivity.py` is a **projection from measured summary statistics, not a measurement.** It never reports itself as a backtest.

---

## Configuration

`config.toml` — backtest thresholds, friction parameters, FX rates, and the card catalog.
`.env` (from `.env.example`) — secrets only. Never committed.

The catalog's `prior_weight` values are **relative population estimates supplied by the operator**, not prices and not inferred from market data. The shipped defaults encode only an ordering (plain > parallel > manga > comic) and are unvalidated placeholders — calibrate before trusting any posterior.

Only the OP06-118 worked example is populated. The brief calls for 20 card numbers with high max/min variant ratios; the remaining 19 must be chosen from real variant-ratio data rather than guessed.

---

## Not implemented, deliberately

Stages 2 (live scanner), 3 (vision verification) and 4 (alerting) are **not built**. The Stage 1 gate did not clear, and the brief was explicit that Stage 2 should not be built to be agreeable.

Beyond the gate, Stage 2 has an unresolved design problem worth knowing about: its `cheapness` score needs a trailing median that, under the stated compliance constraints, is only obtainable via manual export. A 60-second live alerting loop cannot depend on a benchmark you refresh by hand. That needs an answer before Stage 2 is worth writing.

`ebay_browse.py` is present because Stage 1 needs to demonstrate what the Browse API can and cannot do, and it supplies the ask side if Stage 2 is ever justified.

---

## Testing

```bash
uv run pytest -v
```

81 tests. The highest-value module is `tests/test_sources.py`, which asserts that every source **fails loudly rather than inventing data** — a source that quietly returns something plausible produces a backtest that reads as evidence while being fiction.

Synthetic sales appear only in `tests/`, as fixtures exercising harness mechanics (leak-freedom, variant separation, gate enforcement). No market conclusion is ever drawn from them, and the harness refuses to run against anything but a real operator-supplied export.
