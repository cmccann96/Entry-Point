"""Command line entry points.

    optcg-backtest feasibility   analytic study from measured dispersion stats
    optcg-backtest run           Stage 1 backtest against a real sold-sales export
    optcg-backtest friction      breakeven discount by price point and channel
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from .backtest import run_backtest
from .config import AppConfig, load_config
from .friction import SaleChannel
from .sensitivity import (
    breakeven_table,
    estimate_opportunities,
    fit_lognormal,
    required_listing_failure_rate,
    required_probability_for_gate,
)
from .sources.base import DataUnavailable
from .sources.csv_source import CsvSoldSource

_CHANNEL_LABEL = {
    SaleChannel.OVERSEAS_TO_EBAY_AU: "overseas -> eBay AU",
    SaleChannel.OVERSEAS_TO_LOCAL: "overseas -> local",
    SaleChannel.LOCAL_TO_LOCAL: "local -> local",
}


def _rule(title: str) -> None:
    print(f"\n{title}\n{'=' * len(title)}")


def cmd_feasibility(cfg: AppConfig) -> int:
    if not cfg.dispersions:
        print("No ungraded [[dispersion]] blocks in config.toml. Nothing to analyse.")
        return 1

    print("ANALYTIC FEASIBILITY STUDY")
    print("Projection from measured summary statistics. NOT a backtest.")
    print("Answers 'is the gate reachable in principle', not 'what happened'.")

    gate = cfg.backtest.gate_events_per_card_year

    for dist in cfg.dispersions:
        _rule(dist.label)
        fits = fit_lognormal(dist)
        print(f"  n = {dist.n} over {dist.window_months:.1f} months "
              f"-> {dist.sales_per_year:.1f} sales/year")
        print(f"  median ${dist.median:,.0f}   IQR ${dist.q1:,.0f}-${dist.q3:,.0f}   "
              f"CV {dist.cv:.1%}")
        print("\n  Lognormal sigma, fitted three ways:")
        for label, sigma in fits.as_dict().items():
            print(f"    {label:<12} sigma = {sigma:.4f}")
        print(f"    right/left skew ratio = {fits.skew_ratio:.2f}x")
        if fits.skew_ratio > 1.5:
            print("    -> Strongly right-skewed. The headline CV is inflated by the")
            print("       upper tail. Buying happens in the LEFT tail, which is")
            print("       materially thinner than the CV implies.")

        needed = required_probability_for_gate(dist, gate)
        print(f"\n  To clear {gate:.0f} events/card-year at {dist.sales_per_year:.1f} "
              f"sales/year, need {needed:.1%} of sales in the tail.")

        _rule(f"  Opportunity rate -- {dist.label}")
        print(f"  {'disc':>5} {'sigma fit':<12} {'P(tail)':>8} {'ev/yr':>7}   net margin by channel")
        for est in estimate_opportunities(dist, cfg=cfg.friction):
            margins = "  ".join(
                f"{_CHANNEL_LABEL[c][:12]}:{est.net_margin_by_channel[c]:+6.1%}"
                for c in SaleChannel
            )
            flag = "" if est.events_per_year >= gate else "  BELOW GATE"
            print(f"  {est.discount:>5.0%} {est.sigma_label:<12} {est.probability:>8.2%} "
                  f"{est.events_per_year:>7.2f}   {margins}{flag}")

        threshold = cfg.backtest.discount_threshold
        _rule(f"  Listing-failure component -- {dist.label}")
        print("  The continuous model cannot represent a sale at a 97% discount under")
        print("  a title with no variant descriptor. That is a distinct failure mode:")
        print("  the listing never reached the bidders who would have priced it.")
        for label, sigma in fits.as_dict().items():
            from .sensitivity import prob_below_discount

            continuous = prob_below_discount(sigma, threshold)
            extra = required_listing_failure_rate(dist, gate, continuous)
            verdict = "gate unreachable from dispersion alone" if extra > 0 else "gate clears"
            print(f"    {label:<12} continuous P={continuous:>6.2%}  "
                  f"needs listing-failure rate >= {extra:>6.2%}  ({verdict})")

    _rule("Breakeven discount by price point")
    print("  Fixed costs (A$12 postage each way, 10% GST, customs above A$1,000)")
    print("  do not amortise at low price points.")
    header = "  " + f"{'price':>9}" + "".join(f"{_CHANNEL_LABEL[c]:>22}" for c in SaleChannel)
    print(header)
    for price, by_channel in breakeven_table((50, 100, 300, 1000, 1500, 3000), cfg.friction).items():
        row = "  " + f"${price:>8,.0f}"
        for channel in SaleChannel:
            row += f"{by_channel[channel]:>21.1%} "
        print(row)

    return 0


def cmd_run(cfg: AppConfig) -> int:
    source = _build_source(cfg)
    report = run_backtest(
        source,
        cfg.catalog,
        start=date(2020, 1, 1),
        end=date.today(),
        config=cfg.backtest,
        friction=cfg.friction,
    )

    _rule("STAGE 1 BACKTEST")
    print(f"  cards analysed: {report.cards_analysed}")
    for result in report.results:
        print(f"\n  {result.card_number}")
        print(f"    sales: {result.total_sales}  qualified: {result.qualified_sales}  "
              f"skipped(graded): {result.skipped_graded}  "
              f"skipped(no comps): {result.skipped_insufficient_comps}")
        print(f"    tail events: {len(result.events)}  "
              f"({result.tail_fraction:.1%} of qualified)  "
              f"{result.events_per_year:.2f}/year")

    _rule("Net edge by channel")
    for channel in SaleChannel:
        summary = report.edge_summary(channel)
        print(f"  {_CHANNEL_LABEL[channel]:<22} n={summary['n']:<4} "
              f"mean={summary['mean']:+.1%}  median={summary['median']:+.1%}  "
              f"above gate={summary['share_above_gate']:.0%}")

    _rule("Breakdown")
    print(f"  by set:          {report.by_set()}")
    print(f"  by price band:   {report.by_price_band()}")
    print(f"  by variant tier: {report.by_variant_tier()}")

    passed, message = report.verdict()
    _rule("VERDICT")
    print(f"  {message}")
    return 0 if passed else 2


def cmd_power(cfg: AppConfig) -> int:
    """How much data is needed to settle the question."""
    from .power import collection_plan, required_sample_size, wilson_interval

    _rule("HOW MUCH DATA SETTLES THIS")
    print("  The verdict turns on one number: how often an undescribed listing")
    print("  sells at a deep discount. Everything else is well determined.\n")

    required = 0.204  # from VERDICT.md, left-tail fit
    plan = collection_plan(observed_successes=1, observed_n=13, required_rate=required)
    low, high = plan["interval"]
    print(f"  What you have now: 1 event in 13 sales")
    print(f"    point estimate {plan['point_estimate']:.1%}   95% CI "
          f"[{low:.1%}, {high:.1%}]")
    print(f"    rate needed to clear the gate: {required:.1%}")
    decisive = (
        "yes" if plan["currently_decisive"]
        else "NO -- the required rate sits inside the interval"
    )
    print(f"    decisive? {decisive}")

    _rule("Sample size required")
    for label, n in plan["scenarios"].items():
        print(f"  {label:<42} n = {n:,} sales")

    print("\n  Interval width by sample size (assuming the 7.7% point estimate holds):")
    print(f"  {'n':>6}  {'95% CI':>22}   verdict vs 20.4%")
    for n in (13, 20, 30, 40, 60, 100, 200):
        successes = round(0.077 * n)
        lo, hi = wilson_interval(successes, n)
        if hi < required:
            call = "DECIDES: no-go"
        elif lo > required:
            call = "DECIDES: go"
        else:
            call = "inconclusive"
        print(f"  {n:>6}  [{lo:>6.1%}, {hi:>6.1%}]   {call}")

    _rule("Collection plan")
    n_needed = required_sample_size(0.077, required)
    print(f"  ~{n_needed} JUDGED UNDESCRIBED sales separate 7.7% from 20.4%.")
    print("  That is the binding number, and it is much smaller than it sounds --")
    print("  but note what has to be true for a sale to count toward it:")
    print("    * its title carries no variant descriptor, AND")
    print(f"    * at least {cfg.backtest.min_comparables} described sales of some variant")
    print(f"      occurred in the {cfg.backtest.trailing_days} days before it")
    print("\n  Undescribed listings are a minority of sales, so budget roughly")
    print(f"  {n_needed * 4}-{n_needed * 6} total sales. At ~21 sales/card-year that is")
    print("  about 8-12 cards with 1-2 years of history each -- a morning of")
    print("  exports, not a data pipeline.")
    print("\n  Collect, in priority order:")
    print("    1. Sale DATE, price, currency, and the FULL untruncated title")
    print("    2. Both descriptive and bare titles -- the described sales are the")
    print("       benchmark, so an export of only the weird ones measures nothing")
    print("    3. Cards with a wide variant price range; a card whose variants all")
    print("       cost the same cannot exhibit the failure mode at all")
    print("\n  Titles matter more than prices here. A truncated title reads as")
    print("  'undescribed' and will inflate the failure rate you measure.")
    print("\n  Then: uv run optcg-backtest measure")
    return 0


def cmd_measure(cfg: AppConfig) -> int:
    """Direct measurement of the listing-failure rate. The actual validity test."""
    from .power import measure_failure_rate

    source = _build_source(cfg)
    required = 0.204

    _rule("LISTING-FAILURE RATE -- DIRECT MEASUREMENT")
    print("  Reported as BOUNDS, not a point estimate. An undescribed listing")
    print("  that sold cheap is either a correctly-priced plain printing or a")
    print("  missed high-value variant, and the sale record cannot tell you")
    print("  which -- that is exactly what the missing description would have")
    print("  said. Photos settle it; nothing in the price history can.\n")

    total_judged = 0
    total_unambiguous = 0
    total_candidates = 0
    shortlist: list[tuple[str, float, float, str]] = []

    for card_number, variants in cfg.catalog.items():
        sales = source.fetch_sold(card_number, date(2020, 1, 1), date.today())
        if not sales:
            print(f"  {card_number}: no sales in export")
            continue
        result = measure_failure_rate(sales, variants, cfg.backtest)
        total_judged += result.total_judged
        total_unambiguous += result.unambiguous
        total_candidates += result.candidates
        shortlist.extend(result.examples)

        print(f"\n  {card_number}: {len(sales)} sales, "
              f"{result.undescribed_sales} undescribed, {result.total_judged} judged")
        print(f"    unambiguously cheap: {result.unambiguous}   "
              f"needs photo check: {result.candidates}")
        print(f"    rate in [{result.lower_rate:.1%}, {result.upper_rate:.1%}]")

    _rule("POOLED")
    if total_judged == 0:
        print("  No undescribed sales had enough comparables to judge.")
        print("  Collect more history -- 'optcg-backtest power' sizes it.")
        return 3

    from .power import wilson_interval

    low = wilson_interval(total_unambiguous, total_judged)[0]
    high = wilson_interval(total_unambiguous + total_candidates, total_judged)[1]
    print(f"  {total_judged} judged undescribed sales")
    print(f"    {total_unambiguous} unambiguously cheap, {total_candidates} candidates")
    print(f"  failure rate in [{low:.1%}, {high:.1%}]  "
          f"(identification gap + sampling error)")
    print(f"  required to clear the gate: {required:.1%}")

    if shortlist:
        _rule("Shortlist -- check these photos")
        print("  Each is a listing whose variant the title did not determine.")
        print("  Confirming or dismissing these is what narrows the bound.\n")
        for title, price, benchmark, bucket in sorted(shortlist, key=lambda r: r[1])[:20]:
            print(f"    [{bucket:<11}] ${price:>9,.2f} vs ${benchmark:>9,.2f}  "
                  f"\"{title[:52]}\"")

    if high < required:
        print("\n  DECIDES: NO-GO. Even the optimistic bound is below what the")
        print("  strategy needs. This is a real answer -- stop here.")
        return 2
    if low > required:
        print("\n  DECIDES: GO. Even the pessimistic bound clears the gate.")
        return 0
    print("\n  INCONCLUSIVE. The required rate is inside the bound.")
    if total_candidates:
        print(f"  Fastest way to narrow it: check the {total_candidates} candidate "
              "photo(s) above.")
        print("  Each one resolved moves it out of the gap and into a count.")
    print("  Otherwise collect more sales -- 'optcg-backtest power' sizes it.")
    return 3


def cmd_verify(cfg: AppConfig) -> int:
    """Mislabel rate measured against image ground truth. The real Stage 1 test."""
    from .ingest import load_truth_column
    from .verification import measure_mislabel_rate, parse_verified

    if not cfg.manual_files:
        print("No input files. Put annotated exports in data/ -- see README.md.")
        return 3

    _rule("MISLABEL RATE -- MEASURED AGAINST IMAGE GROUND TRUTH")
    print("  Requires a 'true_variant' column, filled in by looking at each")
    print("  listing's photo. Titles alone cannot measure how often titles lie.\n")

    source = _build_source(cfg)
    truth: dict[str, str] = {}
    for path in cfg.manual_files:
        truth.update(load_truth_column(path, cfg.column_overrides))

    if not truth:
        print("  No 'true_variant' column found in any input file.")
        print("\n  To produce it: open each sold listing, look at the card, and")
        print("  record which variant it actually is. The four tells:")
        print("    * star above the rarity code   -> parallel or above")
        print("    * manga panels / full-bleed / inside-frame art")
        print("    * gold stamp, WINNER stamp, or printed serial")
        print("    * slabbed? grade and grader")
        print("\n  50 listings by hand is enough for a first read. No API needed.")
        return 3

    total = 0
    exploitable = 0
    gaps: list[float] = []
    for card_number, variants in cfg.catalog.items():
        sales = source.fetch_sold(card_number, date(2020, 1, 1), date.today())
        verified = parse_verified(sales, truth, variants)
        if not verified:
            continue
        prices = {
            key: price for key, price in cfg.reference_prices.items()
            if key.startswith(card_number)
        }
        report = measure_mislabel_rate(
            verified, variants, prices, SaleChannel.OVERSEAS_TO_EBAY_AU, cfg.friction
        )
        total += report.total
        exploitable += report.exploitable
        gaps.extend(report.under_claim_gaps)

        print(f"  {card_number}: {report.total} verified")
        print(f"    mislabelled:      {report.mislabel_rate:>6.1%}")
        print(f"    under-claimed:    {report.under_claim_rate:>6.1%}  (the edge)")
        print(f"    over-claimed:     {report.over_claimed:>6}     (the trap)")
        print(f"    EXPLOITABLE:      {report.exploitable_rate:>6.1%}  "
              f"(under-claimed AND clears friction)")

        # Exploitability is channel-dependent: a gap too thin for an overseas
        # round trip can still pay locally.
        by_channel = {
            channel: measure_mislabel_rate(
                verified, variants, prices, channel, cfg.friction
            ).exploitable
            for channel in SaleChannel
        }
        print("    by channel:       " + "   ".join(
            f"{_CHANNEL_LABEL[c]}: {n}" for c, n in by_channel.items()
        ))

        for title, claimed, actual, net_ev in report.examples[:3]:
            print(f"      net +${net_ev:>9,.0f}  \"{title[:42]}\"")
            print(f"                     claimed {claimed} -> actually {actual}")

    _rule("POOLED")
    if total == 0:
        print("  No annotated sales matched the catalog.")
        return 3

    from .power import wilson_interval
    from .stats import median as _med

    low, high = wilson_interval(exploitable, total)
    required = 0.204
    print(f"  {exploitable} exploitable in {total} verified sales")
    print(f"  rate {exploitable / total:.1%}   95% CI [{low:.1%}, {high:.1%}]")
    if gaps:
        print(f"  median net EV per exploitable sale: ${_med(gaps):,.0f}")
        print(f"  total that would have been banked:  ${sum(gaps):,.0f}")
    print(f"  required to clear the gate: {required:.1%}")

    if high < required:
        print("\n  DECIDES: NO-GO. Sellers do not misdescribe often enough.")
        return 2
    if low > required:
        print("\n  DECIDES: GO. The mislabel edge is real and large enough.")
        return 0
    print("\n  INCONCLUSIVE. Verify more listings -- 'optcg-backtest power' sizes it.")
    return 3


def cmd_title(cfg: AppConfig, title: str, ask: float | None = None) -> int:
    """Score one listing title. No credentials, no network -- pure inference.

    The quickest way to sanity-check the variant engine against a real listing
    before spending anything on vision.
    """
    from .valuation import value_listing
    from .variants import detect_features, extract_card_numbers, infer_variant

    numbers = extract_card_numbers(title)
    _rule("TITLE ANALYSIS")
    print(f'  "{title}"\n')

    if not numbers:
        print("  No card number found. Search and keying are by card number, so")
        print("  this listing would not be matched to a catalog entry at all.")
        return 1

    print(f"  card number(s): {', '.join(numbers)}")
    print(f"  features fired: {sorted(detect_features(title)) or '(none)'}")

    card_number = numbers[0]
    variants = cfg.catalog.get(card_number)
    if not variants:
        print(f"\n  {card_number} is not in the catalog -- add it to config.toml")
        print("  to score the variant posterior.")
        return 1

    posterior = infer_variant(title, variants)
    _rule("Variant posterior")
    for key, probability in sorted(
        posterior.probabilities.items(), key=lambda kv: -kv[1]
    ):
        bar = "#" * round(probability * 40)
        price = cfg.reference_prices.get(key)
        tag = f"  (ref ${price:,.0f})" if price else ""
        print(f"  {probability:>6.1%} {posterior.variants[key].describe():<28}{tag}")
        print(f"         {bar}")

    _rule("Signals")
    print(f"  ambiguity (posterior entropy):   {posterior.ambiguity:.2f}")
    print(f"  information gain (KL vs prior):  {posterior.information_gain:.2f}", end="")
    print("   <- 0.00 means the title told us nothing"
          if posterior.information_gain < 0.01 else "")
    print(f"  undescribed (no variant tell):   {posterior.is_undescribed}")
    if cfg.reference_prices:
        dispersion = posterior.price_dispersion(cfg.reference_prices)
        print(f"  live price dispersion:           {dispersion:.1f}x")
        if dispersion > 5:
            print("    -> The title leaves variants spanning a wide price range live.")
            print("       This is a photo-check candidate: what it is worth depends")
            print("       on which variant it turns out to be.")

    if ask is not None and cfg.reference_prices:
        _rule(f"Valuation at ask ${ask:,.0f}")
        for channel in SaleChannel:
            try:
                valuation = value_listing(
                    posterior, cfg.reference_prices, ask, channel, cfg.friction
                )
            except ValueError:
                print("  No live variant has a reference price -- cannot value.")
                break
            print(f"  {_CHANNEL_LABEL[channel]:<22} blind EV {valuation.blind_ev_aud:>+9,.0f}"
                  f"   info value {valuation.information_value_aud:>+8,.0f}"
                  f"{'   <- cheapness screen DISCARDS' if valuation.is_hidden_by_cheapness_screen else ''}")
        print(f"\n  best case if photo confirms {valuation.best_variant_label}:"
              f" {valuation.upside_ev_aud:+,.0f}")
        print(f"  worst case if it is {valuation.worst_variant_label}:"
              f" {valuation.downside_ev_aud:+,.0f}")
    return 0


def cmd_scan(cfg: AppConfig) -> int:
    """Scan LIVE eBay listings, photo-check the top ones, log to the journal.

    This is the real test: measures the mislabel rate on actual listings and
    surfaces live trades at the same time.
    """
    import os

    from .journal import Journal
    from .scan import run_scan
    from .sources.ebay_browse import EbayBrowseSource
    from .vision import VisionCache, VisionReader

    if not cfg.reference_prices:
        print("No reference_price_aud values in config.toml. Nothing to value against.")
        return 3

    source = EbayBrowseSource(
        os.environ.get("EBAY_CLIENT_ID"),
        os.environ.get("EBAY_CLIENT_SECRET"),
        os.environ.get("EBAY_MARKETPLACE_ID", "EBAY_AU"),
    )
    cache = VisionCache(cfg.vision_cache_path)
    reader = VisionReader(cache)
    journal = Journal(cfg.journal_path)

    _rule("LIVE SCAN")
    print("  Active listings via the eBay Browse API (free, no login wall).")
    print("  Photo-checking the top listings by information value -- never by")
    print("  cheapness, which discards the mislabelled dear cards.\n")

    report = run_scan(
        source,
        cfg.catalog,
        cfg.reference_prices,
        reader,
        SaleChannel.OVERSEAS_TO_EBAY_AU,
        cfg.friction,
        cfg.vision_budget,
    )

    _rule("MISLABEL RATE (measured on real listings)")
    low, high = report.mislabel_interval
    print(f"  scanned:        {len(report.scanned)}")
    print(f"  photo-verified: {report.verified}")
    print(f"  mislabelled:    {report.mislabelled}")
    print(f"  unreadable:     {report.undeterminable}  (counted as neither)")
    if report.verified:
        print(f"  rate {report.mislabel_rate:.1%}   95% CI [{low:.1%}, {high:.1%}]")
    print(f"  verdict vs 20.4%: {report.decides_against(0.204)}")

    _rule("LIVE OPPORTUNITIES")
    found = report.opportunities(
        cfg.catalog, cfg.reference_prices, SaleChannel.OVERSEAS_TO_EBAY_AU, cfg.friction
    )
    if not found:
        print("  None clearing friction this pass.")
    for item, ev in found[:10]:
        print(f"\n  +${ev:>9,.0f} net   ask ${item.listing.ask_aud:,.0f}")
        print(f"    title claims:  {item.title_claim}")
        print(f"    photo shows:   {item.verified_treatment}")
        print(f"    \"{item.listing.title[:64]}\"")
        print(f"    {item.listing.url}")
        journal.record(
            listing_id=item.listing.listing_id,
            card_number=item.listing.card_number,
            title=item.listing.title,
            url=item.listing.url,
            ask_aud=item.listing.ask_aud,
            title_claim=item.title_claim,
            vision_treatment=item.verified_treatment,
            vision_confidence=item.vision.read.confidence if item.vision else None,
            modelled_ev_aud=ev,
        )

    _rule("FORWARD TEST")
    for key, value in journal.summary().items():
        print(f"  {key}: {value}")
    print("\n  Resolve open entries once they sell: optcg-backtest resolve")
    journal.close()
    cache.close()
    return 0


def cmd_resolve(cfg: AppConfig) -> int:
    """Show flagged listings awaiting an outcome."""
    from .journal import Journal

    journal = Journal(cfg.journal_path)
    entries = journal.open_entries()

    _rule("OPEN FORWARD-TEST ENTRIES")
    if not entries:
        print("  Nothing awaiting resolution.")
    for entry in entries:
        print(f"\n  [{entry.listing_id}] {entry.days_open}d open  "
              f"ask ${entry.ask_aud:,.0f}  modelled +${entry.modelled_ev_aud or 0:,.0f}")
        print(f"    claimed {entry.title_claim} -> photo said {entry.vision_treatment}")
        print(f"    {entry.url}")

    print("\n  Record outcomes in Python:")
    print("    from optcg.journal import Journal")
    print(f"    j = Journal({str(cfg.journal_path)!r})")
    print("    j.resolve('<listing_id>', 'sold', 1450.0)   # or 'unsold' / 'delisted'")
    journal.close()
    return 0


def cmd_triage(cfg: AppConfig) -> int:
    """Show why ranking on cheapness discards the listings worth having."""
    from .valuation import triage_rank, value_listing
    from .variants import infer_variant

    _rule("TRIAGE -- WHY CHEAPNESS RANKING FAILS")
    print("  Illustrative, using the reference prices in config.toml (operator-")
    print("  supplied, approximate). Shows ranking behaviour, not market data.\n")

    for card_number, variants in cfg.catalog.items():
        prices = {
            key: price for key, price in cfg.reference_prices.items()
            if key.startswith(card_number)
        }
        if len(prices) < 2:
            continue

        scenarios = [
            (f"{card_number} 'parallel' @ $200", f"{card_number} Zoro parallel", 200.0),
            (f"{card_number} 'SEC' @ $60", f"{card_number} Zoro SEC", 60.0),
            (f"{card_number} bare title @ $150", f"One Piece {card_number}", 150.0),
            (f"{card_number} 'comic parallel' @ $1400", f"{card_number} comic parallel red", 1400.0),
        ]
        valuations = []
        for name, title, ask in scenarios:
            posterior = infer_variant(title, variants)
            try:
                valuations.append((name, value_listing(posterior, prices, ask, cfg=cfg.friction)))
            except ValueError:
                continue

        print(f"  {'listing':<36} {'blind EV':>10} {'info value':>11}  cheapness screen")
        for name, valuation in triage_rank(valuations):
            verdict = "DISCARDS IT" if valuation.is_hidden_by_cheapness_screen else "keeps it"
            print(f"  {name:<36} {valuation.blind_ev_aud:>+10,.0f} "
                  f"{valuation.information_value_aud:>+11,.0f}  {verdict}")
        print()
        print("  Ranked by information value -- what a photo check is worth.")
        print("  Listings marked DISCARDS IT look expensive against the variant")
        print("  the seller claimed, and are exactly the mislabelled dear cards.")
    return 0


def _build_source(cfg: AppConfig):
    """Manual multi-file ingestion if configured, else the single CSV."""
    if cfg.manual_files:
        from .ingest import ManualSource

        return ManualSource(
            cfg.manual_files, cfg.fx_rates, cfg.column_overrides, cfg.default_currency
        )
    return CsvSoldSource(cfg.sold_sales_csv, cfg.fx_rates)


def cmd_friction(cfg: AppConfig) -> int:
    _rule("Breakeven discount by price point")
    for price, by_channel in breakeven_table(
        (25, 50, 100, 300, 700, 1000, 1500, 3000), cfg.friction
    ).items():
        print(f"  ${price:>8,.0f}  " + "  ".join(
            f"{_CHANNEL_LABEL[c]}: {by_channel[c]:>6.1%}" for c in SaleChannel
        ))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="optcg-backtest")
    parser.add_argument("--config", default="config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("feasibility", help="analytic study from measured dispersion stats")
    sub.add_parser("power", help="how much data is needed to settle the question")
    sub.add_parser("measure", help="listing-failure-rate bounds from titles alone")
    sub.add_parser("verify", help="mislabel rate vs image ground truth (the real test)")
    sub.add_parser("triage", help="why cheapness ranking discards the good listings")
    sub.add_parser("scan", help="scan LIVE eBay listings + photo-check (the real test)")
    sub.add_parser("resolve", help="list flagged listings awaiting an outcome")
    title_p = sub.add_parser("title", help="score one listing title (no credentials needed)")
    title_p.add_argument("text", help="the listing title, quoted")
    title_p.add_argument("--ask", type=float, help="ask price in AUD, to value it")
    sub.add_parser("run", help="Stage 1 backtest against a real sold-sales export")
    sub.add_parser("friction", help="breakeven discount by price point and channel")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    handlers = {
        "feasibility": cmd_feasibility,
        "power": cmd_power,
        "measure": cmd_measure,
        "verify": cmd_verify,
        "triage": cmd_triage,
        "scan": cmd_scan,
        "resolve": cmd_resolve,
        "run": cmd_run,
        "friction": cmd_friction,
    }
    try:
        if args.command == "title":
            return cmd_title(cfg, args.text, args.ask)
        return handlers[args.command](cfg)
    except DataUnavailable as exc:
        print(f"\nDATA UNAVAILABLE\n{exc}\n", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
