# Stage 1 Verdict — OPTCG variant mispricing

**Date:** 2026-08-14
**Status:** ⛔ **NO-GO as specified.** Do not build Stage 2 yet.
**Confidence:** Medium-high on the friction arithmetic, low on the opportunity rate — for a reason that is itself the main finding.

---

## 0. The headline

Two independent things went wrong, and they compound:

1. **The specified backtest cannot be run at all.** No official API supplies per-sale realised history for eBay. Not PriceCharting, not eBay Browse, not TCGplayer. This is a structural fact about the data landscape, not a configuration problem.
2. **On your own measured numbers, the strategy fails its own gate** — by roughly 6× on event rate, and by more than that once the net-edge requirement is applied to the channel you actually intend to buy through.

The second finding does not depend on the first. I could not run the backtest you asked for, but I could interrogate the dispersion study you already completed, and it does not support the thesis.

---

## 1. Why the specified Stage 1 cannot be run

| Source | What it actually returns | Can it supply Stage 1? |
|---|---|---|
| **PriceCharting API** | Current values by grade/condition. Their docs state historic prices and historic sales are **not supported**. | ❌ No |
| **eBay Browse API** | Active listings only. Sold data is classified "Restricted". | ❌ No |
| **eBay Marketplace Insights API** | Sold prices — the right data | ❌ "Restricted and not open to new users" |
| **TCGplayer API** | Public developer access effectively closed; third-party resellers are scrapers | ❌ No |
| **Terapeak** (Seller Hub → Research) | ~3 years of sold history, free with any seller account | ⚠️ **Yes, but dashboard-only — manual CSV export** |

Every service that will sell you sold-comp data — SoldComps, TCGAPIs, Apify actors, Bright Data — is a scraper with a REST endpoint in front of it. Using one violates your own hard constraint. Renaming scraping "an API" does not change what it is.

**Consequence for the architecture, which is larger than it looks.** The trailing-median denominator is required by *both* the Stage 1 backtest *and* the Stage 2 `cheapness` score. Stage 2 was specified as a live scanner running on a 60-second alerting budget. It cannot compute cheapness live, because its price benchmark can only come from a manual export you refresh by hand. Stage 2 as designed is not implementable under the stated constraints — not because of latency, but because the benchmark is offline.

The harness therefore reads an operator-supplied Terapeak export (`optcg/sources/csv_source.py`). That is your own account's data, exported through a supported feature — neither scraping nor an unofficial API. It is the only compliant path I could find.

---

## 2. What your own data says

Inputs are yours, unmodified: OP06-118 raw, 2026 YTD, median $1,098, IQR $960–$1,475, CV 31.1%, n=13 over 7.5 months → **20.8 sales/year**.

### 2.1 The 31.1% CV overstates the opportunity

Fitting a lognormal σ three ways from your quartiles:

| Fit | σ | What it describes |
|---|---|---|
| Left tail (from Q1) | **0.199** | The side you buy on |
| CV-implied (symmetric) | 0.304 | The headline number |
| Right tail (from Q3) | 0.438 | The side you sell on |

**Right/left skew ratio: 2.20×.** Your distribution is strongly right-skewed in log space. The 31.1% CV is inflated by the upper tail — the $1,850 April sale and its kin. Buying happens exclusively in the left tail, which is **materially thinner than the headline CV suggests**.

Using the CV-implied σ for a left-tail question overstates the 30%-discount rate by 3.3× (12.0% vs 3.66%). This is a subtle trap and it is worth stating plainly: *a symmetric fit to an asymmetric distribution flatters the buy side.*

### 2.2 Event rate against the gate

Your gate: **5 events per card-year at >30% net edge**.

| Discount | Left-tail P | Events/card-yr | vs gate |
|---|---|---|---|
| 20% | 13.12% | 2.73 | ❌ |
| **30%** | **3.66%** | **0.76** | ❌ **6.6× short** |
| 40% | 0.52% | 0.11 | ❌ |
| 50% | 0.02% | 0.01 | ❌ |

To clear 5 events/card-year at 20.8 sales/year you need **24.0% of all sales** to land 30%+ below trailing median. The measured left tail delivers **3.66%**.

Even on the most generous fit — the right-tail σ, which is the *wrong side* for this question — the 30% threshold yields 4.32 events/card-year. **Still below the gate.** The gate fails under every fit of your own data.

### 2.3 The net-edge requirement is the real killer

Event rate is only half the gate. Applying the >30% *net edge* requirement, with the full friction model (A$12 postage each way, 10% GST at all values, formal clearance above A$1,000, 13% eBay AU FVF):

| Channel | Discount needed for +30% net | Left-tail frequency |
|---|---|---|
| **Overseas → eBay AU** | **41.1%** | **once per 12 years per card** |
| Overseas → local | 32.0% | once per 2 years per card |
| Local → local | 25.1% | 1.5 per card-year |

Read the top row again. **The channel your brief is built around — buying from JP/US — needs a 41% discount to clear your edge bar, and that occurs about once per twelve years per card.** Across 20 cards that is 1.7 qualifying events per year. That is not a system; it is a lottery ticket with a maintenance burden.

At the 30% discount you specified, overseas → eBay AU returns **+9.9% net**. That is a real profit and it is nowhere near your 30% bar — and it is thin enough that a single grading dispute, return, or FX move erases it.

### 2.4 The one place the thesis survives

`local → local` needs only a 25.1% discount and fires ~1.5×/card-year. Across 20 cards that is ~30 events/year at >30% net edge. **That is the only configuration in this analysis that resembles a working strategy**, and your brief treats it as an afterthought.

The catch is that it inverts the sourcing assumption: you would be hunting mispriced cards on eBay AU from Australian sellers, not importing. Smaller pool, more domestic competition, and I have no data on the AU-only sale frequency — the 20.8 sales/year figure is global. Local sale frequency could easily be 3–5× lower, which would drag it back under the gate. **Unmeasured, and the single most valuable thing to measure next.**

---

## 3. The parameter the whole thesis rests on

Your strongest evidence is not the dispersion — it is the **$30.64 June sale under a title with no variant descriptor**. That is not a draw from the price distribution. It is a distinct failure mode: the listing never reached the people who would have bid it up. No continuous distribution models it, which is exactly why the CV analysis above cannot settle the question.

Modelled as a separate mixture component, here is what it must deliver:

| Fit | Continuous P(30% disc.) | Listing-failure rate needed to reach the gate |
|---|---|---|
| Left (q1) | 3.66% | **≥ 20.4%** of all sales |
| CV-implied | 12.0% | ≥ 12.0% |
| Right (q3) | 20.8% | ≥ 3.3% |

You observed roughly **one** such event in 13 sales — a point estimate of 7.7%, with a **95% confidence interval of [0.2%, 36.0%]**.

The required 20.4% **sits inside that interval.**

This is the honest bottom line: **the viability of this strategy turns on a parameter you have estimated from a single observation, and n=13 cannot distinguish "this happens 8% of the time" from "this happens 30% of the time" from "I got lucky once."** Everything else in the analysis is comparatively well-determined. This one number decides it, and it is the one number no available data source can pin down without manual work.

---

## 4. The friction model has a trap in it

Breakeven discount by price point (overseas → eBay AU):

| Card price | Breakeven discount |
|---|---|
| $50 | 66.7% |
| $100 | 43.8% |
| $300 | 28.5% |
| **$1,000** | **23.2%** |
| **$1,500** | **26.7%** ← *rises* |
| $3,000 | 23.8% |

Two things worth internalising:

- **Below ~A$300 the strategy is arithmetically dead overseas.** A A$50 card needs a 67% discount to break even. Fixed costs do not amortise. Any variant priced under a few hundred dollars should be excluded from overseas hunting outright — which removes the plain SEC and standard parallel tiers of OP06-118 entirely.
- **There is a non-monotonic notch at A$1,000** where formal customs clearance (~A$50–90) kicks in. Breakeven *worsens* from 23.2% to 26.7% as the card gets more expensive. A card asking A$1,050 is a worse trade than the same card asking A$990. The scanner should treat the A$950–1,150 band as a dead zone rather than scoring it smoothly.

---

## 5. Verdict

**Do not build Stages 2–4.**

Not because the inefficiency isn't real — your edge-damaged card selling 34% above median while clean copies sold below is genuine evidence that listing quality dominates condition, and that is a real market failure. But:

- The measured dispersion does not clear your gate under any fit, on the channel you intend to use, by a wide margin.
- The tail events that *would* clear it depend on a failure rate estimated from **one observation**.
- The live scanner cannot compute its own cheapness score under your compliance constraints, because the benchmark is only obtainable by manual export.

You set the gate at 5 events/card-year. The analysis returns 0.76 on the left-tail fit and 0.08 once the net-edge bar is applied to overseas → eBay AU. I am not going to build Stage 2 against that.

---

## 6. What would falsify this — cheaply

In rough order of information per hour spent:

1. **Export Terapeak sold history for 20 card numbers and run `optcg-backtest run`.** This is a few hours of manual export. It replaces every projection above with measurement, and it directly estimates the listing-failure rate that decides the question. The harness is built and tested; it needs only the CSV.
2. **Measure the AU-domestic sale frequency specifically.** If `local → local` sustains >5 events/card-year, the strategy is alive in a form your brief didn't anticipate — and the friction advantage is large enough that this is the most likely path to a "go".
3. **Count listing-failure events directly rather than inferring them.** Filter your export for sales whose title lacks any variant descriptor, then check what fraction sold below 50% of trailing median. This is the 20.4% number. Ten minutes on real data, and it is decisive.
4. **Check whether the tail is competitive.** A card selling at $30.64 may have had three other snipers. If deep-tail listings are already contested within seconds, the measured historical rate overstates *your* realisable rate — and 60-second alerting is far too slow.

If (1) and (3) come back showing a listing-failure rate above ~20% with a defensible sample, I will build Stage 2 and say so. That is the number to go and get.

---

## 7. What I built anyway

Stage 1 harness, complete and tested (81 tests passing) — it runs the moment a real export exists:

- **Variant inference** — card-number extraction, four-axis taxonomy, naive-Bayes posterior, entropy + information gain. Handles the "Super Parallel" trap.
- **Friction model** — component-level, not blended. Reproduces your ~30/20/8% figures as a cross-check while exposing the fixed-cost and customs-notch effects a blended rate hides.
- **Backtest harness** — per-variant trailing medians, leak-free benchmarks, minimum-comparable gating, graded exclusion, and a `verdict()` that enforces the gate rather than negotiating with it.
- **Feasibility study** — everything in this document, reproducible via `optcg-backtest feasibility`.

**A note on Stage 2's design, for when you get there.** Your instinct to separate `ambiguity` from `cheapness` was right, and while building it I hit a related bug worth flagging: **normalised posterior entropy is not a sufficient ambiguity measure.** Because the population prior is concentrated (plain printings vastly outnumber comic parallels), a title carrying *no information at all* produces a concentrated posterior and therefore scores as **low** entropy — i.e. confidently identified. My first test caught a bare title scoring *less* ambiguous than an explicitly-labelled one. Entropy alone would have ranked your $30.64 case as unambiguous.

The fix is three separate signals, all implemented in `variants.py`: posterior entropy (`ambiguity`), KL divergence from prior (`information_gain` — did the seller actually disambiguate anything), and `price_dispersion` (what the ambiguity is worth in dollars, given which variants remain live). Rank on the max across those and cheapness, never a product.
