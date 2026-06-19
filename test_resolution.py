"""
Resolution and scoring smoke test.

Inserts two synthetic past-dated predictions (one BUY NVDA, one SELL NVDA)
with a horizon that has already passed, then fires the full resolution and
scoring pipeline so you can watch every step without waiting for a real
prediction to mature.

ALL records are stamped with agent "RESOLUTION-TEST".
To remove them afterward:

    python test_resolution.py --cleanup

or manually:

    DELETE FROM predictions WHERE agents LIKE '%RESOLUTION-TEST%';

Never let test records sit in the DB when you start real evaluation --
they will contaminate your sample counts and Sharpe figures.
"""

import argparse
import sys
from datetime import date, datetime, timedelta

# Bootstrap path (script lives at project root, harness is a sub-package)
from harness.db import _conn, migrate, insert_prediction, get_prediction, get_predictions
from harness.models import Prediction
from harness.prices import YFinanceSource, BadPriceData
from harness.resolve import resolve_prediction, _is_correct, _direction_signed_return
from harness.scoring import full_report, MIN_SAMPLE_SIZE

TEST_AGENT = "RESOLUTION-TEST"
TRACK = "equities"
ASSET = "NVDA"       # real equity, different from baseline (SPY) -- cleaner comparison
BASELINE = "SPY"
ENTRY_DAYS_AGO = 14  # two weeks back -- safely past any weekend/holiday gaps
HORIZON_DAYS_AGO = 7 # one week back -- clearly overdue for resolution

LINE = "=" * 68


def section(title: str) -> None:
    print(f"\n{LINE}")
    print(f"  {title}")
    print(LINE)


def step(msg: str) -> None:
    print(f"\n  >> {msg}")


def result(label: str, value) -> None:
    print(f"     {label:<28} {value}")


# ------------------------------------------------------------------
# Cleanup mode
# ------------------------------------------------------------------

def do_cleanup() -> None:
    section("CLEANUP -- removing RESOLUTION-TEST records")
    with _conn() as con:
        rows = con.execute(
            "SELECT id, asset, direction FROM predictions WHERE agents LIKE ?",
            (f'%{TEST_AGENT}%',),
        ).fetchall()
        if not rows:
            print("  Nothing to clean up.")
            return
        for r in rows:
            print(f"  Deleting #{r['id']} {r['asset']} {r['direction']}")
        con.execute(
            "DELETE FROM predictions WHERE agents LIKE ?",
            (f'%{TEST_AGENT}%',),
        )
    print(f"\n  Deleted {len(rows)} record(s). DB is clean.")


# ------------------------------------------------------------------
# Main test
# ------------------------------------------------------------------

def run_test() -> None:
    migrate()

    entry_dt = datetime.utcnow() - timedelta(days=ENTRY_DAYS_AGO)
    horizon_dt = (datetime.utcnow() - timedelta(days=HORIZON_DAYS_AGO)).date()
    entry_date = entry_dt.date()

    # ------------------------------------------------------------------
    section(f"STEP 1 -- Fetch real historical entry price for {ASSET}")
    # ------------------------------------------------------------------

    step(f"Calling YFinanceSource.get_price({ASSET!r}, {entry_date}) ...")
    source = YFinanceSource()
    try:
        entry_price = source.get_price(ASSET, entry_date)
    except BadPriceData as exc:
        print(f"\n  ERROR: could not fetch entry price: {exc}")
        sys.exit(1)

    result("Asset", ASSET)
    result("Entry date", entry_date)
    result("Horizon date (past)", horizon_dt)
    result("Entry price fetched", f"${entry_price:,.4f}")
    result("Days since entry", ENTRY_DAYS_AGO)
    result("Days since horizon", HORIZON_DAYS_AGO)

    # ------------------------------------------------------------------
    section("STEP 2 -- Insert two past-dated TEST predictions")
    # ------------------------------------------------------------------
    # One BUY, one SELL with identical parameters.  Only one can be correct.
    # This lets us verify both the correct and wrong paths in a single run.

    step("Inserting BUY prediction ...")
    p_buy = Prediction(
        track=TRACK,
        asset=ASSET,
        direction="buy",
        conviction=7,
        horizon_days=ENTRY_DAYS_AGO - HORIZON_DAYS_AGO,
        thesis=(
            f"[RESOLUTION TEST -- DELETE ME]  {ASSET} will rise over the test "
            f"window. Entry {entry_date}, horizon {horizon_dt}."
        ),
        agents=[TEST_AGENT, "SynthesisAgent"],
        timestamp=entry_dt,
        horizon_date=horizon_dt,
        entry_price=entry_price,
    )
    p_buy.id = insert_prediction(p_buy)
    result("BUY id", f"#{p_buy.id}")
    result("  timestamp", entry_dt.isoformat(timespec="seconds"))
    result("  horizon_date", horizon_dt.isoformat())
    result("  entry_price", f"${entry_price:,.4f}")
    result("  feeds_returns", p_buy.feeds_returns)

    step("Inserting SELL prediction ...")
    p_sell = Prediction(
        track=TRACK,
        asset=ASSET,
        direction="sell",
        conviction=4,
        horizon_days=ENTRY_DAYS_AGO - HORIZON_DAYS_AGO,
        thesis=(
            f"[RESOLUTION TEST -- DELETE ME]  {ASSET} will fall over the test "
            f"window. Entry {entry_date}, horizon {horizon_dt}."
        ),
        agents=[TEST_AGENT],
        timestamp=entry_dt,
        horizon_date=horizon_dt,
        entry_price=entry_price,
    )
    p_sell.id = insert_prediction(p_sell)
    result("SELL id", f"#{p_sell.id}")

    # ------------------------------------------------------------------
    section("STEP 3 -- Run resolve_prediction() on BUY")
    # ------------------------------------------------------------------

    step(f"Fetching resolution price for {ASSET} on {horizon_dt} ...")
    r_buy = resolve_prediction(p_buy.id)

    resolution_price = r_buy.resolution_price
    raw_return = (resolution_price - entry_price) / entry_price
    direction_signed = _direction_signed_return("buy", raw_return)

    result("Resolution price", f"${resolution_price:,.4f}")
    result("Raw price change", f"{raw_return:+.4%}")
    result("Sanity check", "PASSED  (price is positive, <90% move)")
    result("Correct?", f"{'YES' if r_buy.outcome_correct else 'NO'}  (buy correct when raw_return > 0)")
    result("return_achieved stored", f"{r_buy.return_achieved:+.4%}" if r_buy.return_achieved is not None else "None")
    result("Resolution notes", r_buy.resolution_notes)

    # ------------------------------------------------------------------
    section("STEP 4 -- Run resolve_prediction() on SELL")
    # ------------------------------------------------------------------

    step(f"Fetching resolution price for {ASSET} on {horizon_dt} ...")
    r_sell = resolve_prediction(p_sell.id)

    sell_signed = _direction_signed_return("sell", raw_return)
    result("Resolution price", f"${resolution_price:,.4f}  (same date, same asset)")
    result("Raw price change", f"{raw_return:+.4%}  (same underlying move)")
    result("Correct?", f"{'YES' if r_sell.outcome_correct else 'NO'}  (sell correct when raw_return < 0)")
    result("return_achieved stored", f"{r_sell.return_achieved:+.4%}" if r_sell.return_achieved is not None else "None")
    print()
    print(f"     Note: BUY and SELL on the same asset over the same window")
    print(f"     must produce opposite correctness -- exactly one wins.")
    buy_ok = "CORRECT" if r_buy.outcome_correct else "wrong"
    sell_ok = "CORRECT" if r_sell.outcome_correct else "wrong"
    print(f"     BUY:  {buy_ok}   SELL: {sell_ok}")

    # ------------------------------------------------------------------
    section("STEP 5 -- Scoring path split: buy/sell vs hold/avoid")
    # ------------------------------------------------------------------

    step("Demonstrating the two scoring paths ...")
    print()
    print(f"     buy/sell predictions -> feeds_returns = True")
    print(f"     BUY #{p_buy.id}:  return_achieved = {r_buy.return_achieved:+.4%}" if r_buy.return_achieved is not None else f"     BUY #{p_buy.id}:  return_achieved = None")
    print(f"     SELL #{p_sell.id}: return_achieved = {r_sell.return_achieved:+.4%}" if r_sell.return_achieved is not None else f"     SELL #{p_sell.id}: return_achieved = None")
    print()
    print(f"     Both feed into Sharpe and drawdown.  A hold/avoid prediction")
    print(f"     would have return_achieved = None and be excluded from those metrics.")

    # ------------------------------------------------------------------
    section(f"STEP 6 -- full_report() for {TRACK} track")
    # ------------------------------------------------------------------

    step("Loading all equities predictions (test records only at this point) ...")
    all_preds = get_predictions(track=TRACK)
    test_preds = [p for p in all_preds if TEST_AGENT in p.agents]
    print(f"     Total equities predictions in DB : {len(all_preds)}")
    print(f"     Of which tagged RESOLUTION-TEST  : {len(test_preds)}")

    step("Calling full_report() ...")
    report = full_report(TRACK, all_preds)

    s = report["sample"]
    hr = report["hit_rate"]
    c = report["council"]
    b = report["baseline"]
    pb = report["proof_bar"]

    print()
    print(f"  -- SAMPLE SIZE --")
    print(f"     n_resolved_total  : {s['n_resolved_total']}")
    print(f"     n_buy_sell        : {s['n_buy_sell']}")
    print(f"     n_hold_avoid      : {s['n_hold_avoid']}")
    print(f"     below minimum?    : {s['below_proof_bar_minimum']}  (minimum = {MIN_SAMPLE_SIZE})")
    if s["warning"]:
        print(f"\n     {s['warning']}")

    print()
    print(f"  -- HIT RATE (all directions) --")
    print(f"     Overall   : {hr['rate']:.1%}  ({hr['correct']}/{hr['n']})")
    if hr["buy_sell_hit_rate"] is not None:
        print(f"     Buy/sell  : {hr['buy_sell_hit_rate']:.1%}")

    print()
    print(f"  -- SHARPE (buy/sell only) --")
    print(f"     Council  : {c['sharpe_labeled']}")
    print(f"     {b['asset']} baseline : {b['sharpe_labeled']}")
    print(f"     Alignment: {b['alignment_method']}")
    if b["skipped_details"]:
        print(f"     Baseline skips: {b['skipped_details']}")

    print()
    print(f"  -- MAX DRAWDOWN (buy/sell) --")
    print(f"     Council  : {c['drawdown']:.2%}  (limit: {pb['drawdown_limit']:.0%})")

    print()
    print(f"  -- PROOF BAR --")
    for key, val in pb.items():
        if key.startswith("cond_") or key == "all_four_cleared":
            icon = "[PASS]" if val is True else "[FAIL]" if val is False else "[ ?  ]"
            print(f"     {icon}  {key}")

    # ------------------------------------------------------------------
    section("CLEANUP REMINDER")
    # ------------------------------------------------------------------

    ids = [p_buy.id, p_sell.id]
    print(f"\n  Test records inserted: #{', #'.join(str(i) for i in ids)}")
    print(f"  Agent tag: {TEST_AGENT!r}\n")
    print(f"  To delete them, run:")
    print(f"      python test_resolution.py --cleanup")
    print(f"\n  Or in SQLite directly:")
    print(f"      DELETE FROM predictions WHERE agents LIKE '%{TEST_AGENT}%';")
    print()


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolution smoke test")
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help=f"Delete all records tagged with agent '{TEST_AGENT}'",
    )
    args = parser.parse_args()

    if args.cleanup:
        do_cleanup()
    else:
        run_test()
