#!/usr/bin/env python3
"""
Trading Council Evaluation Harness -- CLI

Commands
--------
  python main.py log                      Interactive prompt to log a prediction
  python main.py show [--track] [--status] [--verbose]
  python main.py resolve --id N | --all
  python main.py score --track crypto|equities
"""

import argparse
import sys
import textwrap
from typing import Optional

from harness.db import migrate, get_prediction, get_predictions
from harness.log_prediction import log_prediction, VALID_TRACKS, TRACK_DIRECTIONS
from harness.resolve import resolve_prediction, resolve_all_due
from harness.display import print_predictions
from harness.scoring import full_report
from harness.prices import BadPriceData

MIN_SAMPLE_SIZE = 30  # mirrored from scoring.py for the score printer


# ------------------------------------------------------------------
# log subcommand -- interactive prompt
# ------------------------------------------------------------------

def cmd_log(_args) -> None:
    print("\n=== Log a New Prediction ===\n")

    track = _prompt_choice("Track", list(VALID_TRACKS))
    asset = _prompt_str("Asset (ticker/symbol, e.g. ETH, NVDA, BTC)").upper()

    # Direction choices are track-specific
    valid_dirs = list(TRACK_DIRECTIONS[track])
    direction = _prompt_choice("Direction", valid_dirs)

    # Leverage: only for crypto long/short; all other cases force 1
    leverage = 1
    if track == "crypto" and direction in ("long", "short"):
        leverage = _prompt_int(
            "Leverage (e.g. 1=spot-equivalent, 5=5x, 10=10x)",
            lo=1,
        )

    conviction = _prompt_int("Conviction (1-10)", lo=1, hi=10)
    horizon_days = _prompt_int("Time horizon in days (e.g. 7, 30, 90, 365)", lo=1)

    print(
        "\nThesis -- be specific and falsifiable.  What is the claim, and what "
        "would prove it wrong?"
    )
    thesis = _prompt_str("Thesis")

    agents_raw = _prompt_str(
        "Agents (comma-separated, e.g. AnalystAgent, ChallengerAgent)"
    )
    agents = [a.strip() for a in agents_raw.split(",") if a.strip()]

    print(f"\nFetching live entry price for {asset} ...")
    try:
        p = log_prediction(
            track=track,
            asset=asset,
            direction=direction,
            conviction=conviction,
            horizon_days=horizon_days,
            thesis=thesis,
            agents=agents,
            leverage=leverage,
        )
    except BadPriceData as exc:
        print(f"\n[ERROR] Price fetch failed: {exc}")
        print(
            "The prediction was NOT saved.  "
            "Check the asset name or try again when the API is available."
        )
        sys.exit(1)

    print(f"\n[LOGGED] Prediction #{p.id}")
    print(f"  {p.asset} {p.direction.upper()}"
          + (f"  {p.leverage}x leverage" if p.leverage > 1 else ""))
    print(f"  Conviction  : {p.conviction}/10")
    print(f"  Entry price : {_fmt_price(p.entry_price)}")
    if p.liquidation_price is not None:
        print(f"  Liq price   : {_fmt_price(p.liquidation_price)}"
              f"  ({'long: price must fall to' if p.direction == 'long' else 'short: price must rise to'} this level)")
    print(f"  Horizon     : {p.horizon_date}  ({p.horizon_days} days)")
    print(f"  Agents      : {', '.join(p.agents)}")
    print(f"  Thesis      : {p.thesis[:120]}{'...' if len(p.thesis) > 120 else ''}")


# ------------------------------------------------------------------
# show subcommand
# ------------------------------------------------------------------

def cmd_show(args) -> None:
    print_predictions(
        track=args.track,
        status=args.status,
        verbose=args.verbose,
    )


# ------------------------------------------------------------------
# resolve subcommand
# ------------------------------------------------------------------

def cmd_resolve(args) -> None:
    if args.id is not None:
        print(f"\nResolving prediction #{args.id} ...")
        try:
            p = resolve_prediction(args.id)
        except (ValueError, BadPriceData) as exc:
            print(f"[ERROR] {exc}")
            sys.exit(1)

        print(f"\n[RESOLVED] #{p.id} {p.asset} {p.direction.upper()}"
              + (f"  {p.leverage}x" if p.leverage > 1 else ""))

        if p.liquidated:
            print(f"  Outcome    : LIQUIDATED (total margin loss)")
            print(f"  Liq level  : {_fmt_price(p.liquidation_price)}")
            print(f"  Return     : -100.00% (margin wiped)")
        elif p.outcome_correct:
            print(f"  Outcome    : CORRECT")
        else:
            print(f"  Outcome    : WRONG")

        print(f"  Entry      : {_fmt_price(p.entry_price)}")
        print(f"  Exit       : {_fmt_price(p.resolution_price)}")
        if p.feeds_returns and not p.liquidated:
            lev_note = f"  ({p.leverage}x leverage applied)" if p.leverage > 1 else ""
            print(f"  Return     : {_fmt_pct(p.return_achieved)}{lev_note}")
        elif not p.feeds_returns:
            raw = (
                (p.resolution_price - p.entry_price) / p.entry_price
                if p.entry_price and p.resolution_price
                else None
            )
            print(f"  Price Delta: {_fmt_pct(raw)}  (hold/avoid -- not in Sharpe)")
        print(f"  Notes      : {p.resolution_notes}")

    elif args.all:
        print("\nScanning for due predictions ...")
        results = resolve_all_due()
        if not results:
            print("No predictions are due yet.")
            return
        print(f"\nProcessed {len(results)} prediction(s):\n")
        for r in results:
            if r["status"] == "resolved":
                if r.get("liquidated"):
                    outcome = "LIQUIDATED"
                elif r["correct"]:
                    outcome = "CORRECT"
                else:
                    outcome = "WRONG"
                lev_str = f"  {r['leverage']}x" if r.get("leverage", 1) > 1 else ""
                ret_str = f"  return {_fmt_pct(r['return'])}" if r["return"] is not None else ""
                print(f"  #{r['id']} {r['asset']:8s}{lev_str} -> {outcome}{ret_str}")
            else:
                print(f"  #{r['id']} {r['asset']:8s} -> ERROR: {r['error']}")


# ------------------------------------------------------------------
# score subcommand
# ------------------------------------------------------------------

def cmd_score(args) -> None:
    track = args.track
    predictions = get_predictions(track=track)

    print(f"\n{'=' * 70}")
    print(f"  EVALUATION REPORT -- {track.upper()} TRACK")
    print(f"{'=' * 70}")

    if not predictions:
        print("  No predictions logged for this track yet.")
        return

    report = full_report(track, predictions)

    # --- Sample size (always shown first) ---
    s = report["sample"]
    print(f"\n  SAMPLE SIZE")
    print(f"  -----------")
    print(f"  Total resolved             : {s['n_resolved_total']}")
    print(f"  Long/short/buy/sell        : {s['n_buy_sell']}")
    print(f"  Hold/avoid (hit rate only) : {s['n_hold_avoid']}")
    if s.get("n_liquidated", 0):
        print(f"  Liquidated                 : {s['n_liquidated']}")
    print(f"  Proof-bar minimum          : {MIN_SAMPLE_SIZE} resolved")
    if s["warning"]:
        print(f"\n  {s['warning']}")
    else:
        print(f"  Status                     : sample size OK [PASS]")

    # --- Hit rate ---
    hr = report["hit_rate"]
    print(f"\n  HIT RATE  (all directions)")
    print(f"  --------------------------")
    if hr["rate"] is None:
        print("  No resolved predictions yet.")
    else:
        print(f"  Overall          : {hr['rate']:.1%}  ({hr['correct']}/{hr['n']})")
        if hr["buy_sell_hit_rate"] is not None:
            print(f"  Long/Short/Buy/Sell : {hr['buy_sell_hit_rate']:.1%}  ({hr['buy_sell_n']} predictions)")
        if hr["hold_avoid_hit_rate"] is not None:
            print(f"  Hold/Avoid       : {hr['hold_avoid_hit_rate']:.1%}  ({hr['hold_avoid_n']} predictions)")

    # --- Returns & Sharpe ---
    c = report["council"]
    b = report["baseline"]
    pb = report["proof_bar"]

    print(f"\n  SHARPE RATIO  (long/short/buy/sell vs unleveraged {b['asset']} baseline)")
    print(f"  {'-' * 65}")
    print(f"  Council skill    : {c['sharpe_labeled']}")
    if track == "crypto":
        print(f"  Council real     : {c['sharpe_real_labeled']}")
        print(f"  (skill = unleveraged returns; real = leveraged returns -- never mixed)")
    print(f"  {b['asset']} baseline      : {b['sharpe_labeled']}  [unleveraged]")
    if c["sharpe_raw"] is not None and b["sharpe_raw"] is not None:
        delta = c["sharpe_raw"] - b["sharpe_raw"]
        sign = "+" if delta > 0 else ""
        print(f"  Skill delta      : {sign}{delta:.4f}  ({'BEATING' if delta > 0 else 'TRAILING'} baseline)")
    print(f"\n  Note: {b['alignment_method']}")
    if b["n_skipped"]:
        print(f"\n  Skipped {b['n_skipped']} prediction(s) due to bad baseline price data:")
        for msg in b["skipped_details"]:
            print(f"    - {msg}")

    # --- Total return ---
    print(f"\n  TOTAL RETURN  (long/short/buy/sell, cumulative product)")
    print(f"  -------------------------------------------------------")
    if track == "crypto":
        print(f"  Council skill    : {_fmt_pct(c['total_return'])}  [unleveraged]")
        print(f"  Council real     : {_fmt_pct(c['total_return_real'])}  [leveraged, actual P&L]")
    else:
        print(f"  Council          : {_fmt_pct(c['total_return'])}")
    print(f"  {b['asset']} baseline      : {_fmt_pct(b['total_return'])}  [unleveraged]")

    # --- Drawdown ---
    print(f"\n  MAX DRAWDOWN  (long/short/buy/sell, chronological equity curve)")
    print(f"  ---------------------------------------------------------------")
    limit_str = f"{pb['drawdown_limit']:.0%}"
    real_dd = c.get("drawdown_real", c["drawdown"])
    within = real_dd <= pb["drawdown_limit"]
    if track == "crypto":
        print(f"  Council real     : {real_dd:.2%}  [leveraged, actual risk -- proof bar gate]")
        print(f"  Council skill    : {c['drawdown']:.2%}  [unleveraged, directional risk]")
        print(f"  Limit            : {limit_str}  (applied to real/leveraged) {'[PASS]' if within else '[BREACH]'}")
    else:
        print(f"  Council  : {c['drawdown']:.2%}  (limit: {limit_str}) {'[PASS]' if within else '[BREACH]'}")

    # --- Proof bar ---
    print(f"\n{'=' * 70}")
    print(f"  PROOF BAR  (S6 EVALUATION_FRAMEWORK.md -- ALL FOUR required)")
    print(f"{'=' * 70}")
    _print_condition(
        "1. Beat baseline on Sharpe",
        pb["cond_1_beats_baseline_on_sharpe"],
        "(Sharpe data insufficient)" if c["sharpe_raw"] is None else None,
    )
    _print_condition(
        "2. Beat manual picks on Sharpe",
        pb["cond_2_beats_manual_picks"],
        pb["note_cond_2"],
    )
    _print_condition(
        "3. Min 30 resolved predictions",
        pb["cond_3_min_30_resolved"],
        f"({s['n_resolved_total']}/{MIN_SAMPLE_SIZE})",
    )
    _print_condition(
        f"4. Max drawdown <= {pb['drawdown_limit']:.0%}",
        pb["cond_4_max_drawdown_within_limit"],
        f"(current: {c['drawdown']:.2%})",
    )

    cleared = pb["all_four_cleared"]

    banner = "[ PROOF BAR: ALL CONDITIONS MET ]" if cleared else "[ PROOF BAR: NOT CLEARED ]"
    print(f"\n  {banner}")
    if not pb["cond_2_beats_manual_picks"]:
        print("  (Condition 2 cannot be checked until manual picks are logged.)")
    print()


def _print_condition(label: str, result: Optional[bool], note: Optional[str]) -> None:
    if result is True:
        icon = "PASS"
    elif result is False:
        icon = "FAIL"
    else:
        icon = " ?  "
    note_str = f"  -- {note}" if note else ""
    print(f"  [{icon}] {label}{note_str}")


# ------------------------------------------------------------------
# Prompt helpers
# ------------------------------------------------------------------

def _prompt_str(label: str) -> str:
    while True:
        val = input(f"  {label}: ").strip()
        if val:
            return val
        print("    (cannot be empty)")


def _prompt_int(label: str, lo: int = 1, hi: Optional[int] = None) -> int:
    while True:
        raw = input(f"  {label}: ").strip()
        try:
            val = int(raw)
            if val < lo or (hi is not None and val > hi):
                hi_str = f"-{hi}" if hi is not None else "+"
                print(f"    (must be {lo}{hi_str})")
                continue
            return val
        except ValueError:
            print("    (enter a whole number)")


def _prompt_choice(label: str, choices: list) -> str:
    choices_str = " / ".join(choices)
    while True:
        val = input(f"  {label} [{choices_str}]: ").strip().lower()
        if val in choices:
            return val
        print(f"    (choose one of: {choices_str})")


def _fmt_price(v: Optional[float]) -> str:
    if v is None:
        return "--"
    if v >= 1000:
        return f"${v:,.2f}"
    if v >= 1:
        return f"${v:.4f}"
    return f"${v:.8f}"


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "--"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2%}"


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trading Council Evaluation Harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        examples:
          python main.py log
          python main.py show --track crypto --status pending
          python main.py show --verbose
          python main.py resolve --id 3
          python main.py resolve --all
          python main.py score --track crypto
        """),
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    # log
    sub.add_parser("log", help="Log a new prediction (interactive)")

    # show
    p_show = sub.add_parser("show", help="Display predictions")
    p_show.add_argument("--track", choices=["crypto", "equities"], help="Filter by track")
    p_show.add_argument(
        "--status", choices=["pending", "resolved"], help="Filter by status"
    )
    p_show.add_argument(
        "--verbose", "-v", action="store_true", help="Also print thesis and resolution notes"
    )

    # resolve
    p_resolve = sub.add_parser("resolve", help="Resolve predictions whose horizon has passed")
    g = p_resolve.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", type=int, metavar="N", help="Resolve a single prediction by ID")
    g.add_argument("--all", action="store_true", help="Resolve all overdue predictions")

    # score
    p_score = sub.add_parser("score", help="Print the proof-bar evaluation report")
    p_score.add_argument(
        "--track", required=True, choices=["crypto", "equities"], help="Track to evaluate"
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    # Bootstrap DB schema on every run (idempotent)
    migrate()

    dispatch = {
        "log": cmd_log,
        "show": cmd_show,
        "resolve": cmd_resolve,
        "score": cmd_score,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
