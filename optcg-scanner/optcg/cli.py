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
    source = CsvSoldSource(cfg.sold_sales_csv, cfg.fx_rates)
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
    sub.add_parser("run", help="Stage 1 backtest against a real sold-sales export")
    sub.add_parser("friction", help="breakeven discount by price point and channel")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    handlers = {
        "feasibility": cmd_feasibility,
        "run": cmd_run,
        "friction": cmd_friction,
    }
    try:
        return handlers[args.command](cfg)
    except DataUnavailable as exc:
        print(f"\nDATA UNAVAILABLE\n{exc}\n", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
