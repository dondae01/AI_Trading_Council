"""
Liquidation smoke test.

Proves the key futures invariant end-to-end:

  A LONG position that is liquidated mid-window MUST score as a LOSS
  (-1.0, outcome_correct=False) even if the closing price at horizon
  is ABOVE the entry price.  Without the liquidation check, such a
  position would be recorded as a WIN.

The test searches recent price history for a real window satisfying:
  1. Hourly price dropped to/below  entry * (1 - 1/leverage)  (liq triggered)
  2. Daily close at horizon is ABOVE entry                     (would be a WIN)

It tries a ranked list of (asset, leverage, horizon_days) configurations
in order, stopping at the first qualifying window found.  This makes the
test robust to periods where one asset is unusually stable.

CoinGecko granularity note: market_chart/range returns hourly data for any
window span <= 90 days regardless of how far back it is.  All windows
searched here are <= 30 days, so liq checks always use hourly resolution.

ALL records are tagged with agent "LIQ-TEST".
To remove them: python test_liquidation.py --cleanup
"""

import argparse
import sys
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

from harness.db import _conn, migrate, insert_prediction, get_prediction
from harness.models import Prediction
from harness.prices import CoinGeckoSource, BadPriceData
from harness.resolve import resolve_prediction

TEST_AGENT = "LIQ-TEST"

# Configurations tried in order.  First hit wins.
# (asset, leverage, horizon_days)
#   leverage L => liq at entry * (1 - 1/L) => needs (1/L)% drop
SEARCH_CONFIGS: List[Tuple[str, int, int]] = [
    ("ETH",  20, 14),   # 5.0% drop threshold  -- try most volatile first
    ("SOL",  20, 14),
    ("BTC",  20, 14),
    ("ETH",  20, 21),   # wider window catches more volatility
    ("SOL",  20, 21),
    ("BTC",  20, 21),
    ("ETH",  20, 30),
    ("SOL",  20, 30),
    ("BTC",  20, 30),
    ("ETH",  50, 14),   # 2.0% drop threshold  -- higher leverage, easier to liq
    ("SOL",  50, 14),
    ("BTC",  50, 14),
    ("ETH",  50, 21),
    ("BTC",  50, 21),
]

LOOKBACK     = 88   # days of daily history to search (hourly liq data valid up to 90d)
MAX_HOURLY   = 8    # max hourly fetches per (asset, leverage, horizon_days) config

LINE = "=" * 72


def section(title: str) -> None:
    print(f"\n{LINE}")
    print(f"  {title}")
    print(LINE)


def step(msg: str) -> None:
    print(f"\n  >> {msg}")


def ok(label: str, value) -> None:
    print(f"     {label:<36} {value}")


# ------------------------------------------------------------------
# Window search
# ------------------------------------------------------------------

def find_qualifying_window(
    source: CoinGeckoSource,
) -> Tuple[str, int, date, date, float, float, float, float]:
    """
    Iterate SEARCH_CONFIGS until a qualifying window is found.

    A window qualifies when:
      - close[horizon] > entry_price     (recovery: naive close = WIN)
      - hourly min <= liq_price          (liq triggered: must score as LOSS)

    Returns:
      (asset, leverage, entry_date, horizon_date,
       entry_price, horizon_price, liq_price, window_low)

    Raises RuntimeError if no qualifying window is found across all configs.
    """
    today = date.today()
    search_start = today - timedelta(days=LOOKBACK)
    search_end   = today - timedelta(days=1)

    tried: List[str] = []

    # Cache daily prices per asset to avoid redundant API calls
    daily_cache: dict = {}

    for asset, leverage, horizon_days in SEARCH_CONFIGS:
        liq_fraction = 1.0 - 1.0 / leverage
        config_label = f"{asset} {leverage}x {horizon_days}d"

        # Fetch daily prices for this asset (cached)
        if asset not in daily_cache:
            step(f"Fetching {asset} daily closes ({LOOKBACK}-day lookback) ...")
            try:
                daily = source.get_price_range(asset, search_start, search_end)
            except Exception as exc:
                print(f"     [skip] could not fetch {asset} daily data: {exc}")
                tried.append(f"{config_label}: daily fetch failed")
                continue
            daily_cache[asset] = daily
            price_map_key = asset
        daily = daily_cache[asset]
        price_map = {d: p for d, p in daily}
        print(f"\n  Trying {config_label} "
              f"(liq threshold: {1/leverage:.1%} drop) ...")

        # Find candidate windows: entry -> horizon where close[horizon] > entry
        candidates = []
        for entry_date, entry_price in daily:
            target = entry_date + timedelta(days=horizon_days)
            if target >= today:
                continue
            # Accept first trading day on/after target
            horizon_date = None
            horizon_price = None
            for offset in range(5):
                hd = target + timedelta(days=offset)
                if hd in price_map:
                    horizon_date = hd
                    horizon_price = price_map[hd]
                    break
            if horizon_price is None or horizon_price <= entry_price:
                continue
            recovery = (horizon_price - entry_price) / entry_price
            candidates.append(
                (entry_date, horizon_date, entry_price, horizon_price,
                 entry_price * liq_fraction, recovery)
            )

        if not candidates:
            tried.append(f"{config_label}: no recovery windows found")
            continue

        # Sort by strongest recovery so the demo is most unambiguous
        candidates.sort(key=lambda x: x[5], reverse=True)
        print(f"     {len(candidates)} recovery windows found; "
              f"checking top {min(len(candidates), MAX_HOURLY)} for hourly liq touch ...")

        for (entry_date, horizon_date, entry_price,
             horizon_price, liq_price, _) in candidates[:MAX_HOURLY]:
            print(f"     {entry_date} -> {horizon_date}  "
                  f"entry ${entry_price:,.2f}  liq ${liq_price:,.2f} ...",
                  end=" ", flush=True)
            try:
                prices = source.get_prices_in_window(asset, entry_date, horizon_date)
            except Exception as exc:
                print(f"fetch error: {exc}")
                continue
            if not prices:
                print("empty window")
                continue
            window_low = min(prices)
            if window_low <= liq_price:
                print(f"LIQ TOUCHED  (low ${window_low:,.2f})")
                return (asset, leverage, entry_date, horizon_date,
                        entry_price, horizon_price, liq_price, window_low)
            print(f"no liq  (low ${window_low:,.2f}, need <= ${liq_price:,.2f})")

        tried.append(f"{config_label}: hourly low never reached liq threshold")

    raise RuntimeError(
        "No qualifying window found across all configurations:\n"
        + "\n".join(f"  - {t}" for t in tried)
    )


# ------------------------------------------------------------------
# Cleanup
# ------------------------------------------------------------------

def do_cleanup() -> None:
    section("CLEANUP -- removing LIQ-TEST records")
    with _conn() as con:
        rows = con.execute(
            "SELECT id, asset, direction, leverage FROM predictions "
            "WHERE agents LIKE ?",
            (f"%{TEST_AGENT}%",),
        ).fetchall()
        if not rows:
            print("  Nothing to clean up.")
            return
        for r in rows:
            print(f"  Deleting #{r['id']} {r['asset']} "
                  f"{r['direction']} {r['leverage']}x")
        con.execute(
            "DELETE FROM predictions WHERE agents LIKE ?",
            (f"%{TEST_AGENT}%",),
        )
    print(f"\n  Deleted {len(rows)} record(s). DB is clean.")


# ------------------------------------------------------------------
# Main test
# ------------------------------------------------------------------

def run_test() -> None:
    migrate()
    source = CoinGeckoSource()

    # ------------------------------------------------------------------
    section("STEP 1 -- Search for qualifying historical window")
    # ------------------------------------------------------------------

    print(f"\n  Strategy: find a window where a LONG was liquidated mid-window")
    print(f"  but the closing price recovered above entry.")
    print(f"  Trying assets in order: "
          + ", ".join(sorted({a for a, _, _ in SEARCH_CONFIGS})))

    try:
        (asset, leverage, entry_date, horizon_date,
         entry_price, horizon_price, liq_price, window_low) = find_qualifying_window(source)
    except RuntimeError as exc:
        print(f"\n  [SKIP] {exc}")
        sys.exit(0)

    # ------------------------------------------------------------------
    section("STEP 2 -- Qualifying window confirmed")
    # ------------------------------------------------------------------

    drop_pct           = (entry_price - window_low) / entry_price
    recovery_pct       = (horizon_price - entry_price) / entry_price
    counterfactual_ret = recovery_pct * leverage   # what return would be without liq

    ok("Asset",                 f"{asset}  ({leverage}x LONG)")
    ok("Entry date",            entry_date)
    ok("Horizon date",          horizon_date)
    ok("",                      "")
    ok("Entry price",           f"${entry_price:,.2f}")
    ok("Liquidation level",     f"${liq_price:,.2f}  "
                                f"(entry x {1 - 1/leverage:.4f}  --  "
                                f"needs {1/leverage:.1%} drop to liquidate)")
    ok("Window low (hourly)",   f"${window_low:,.2f}  "
                                f"[{drop_pct:.2%} below entry  --  LIQ LEVEL TOUCHED]")
    ok("Closing price",         f"${horizon_price:,.2f}  "
                                f"[{recovery_pct:+.2%} vs entry  --  ABOVE ENTRY]")
    ok("",                      "")
    ok("Counterfactual return", f"{counterfactual_ret:+.2%}  "
                                f"(leveraged, if liq check did not exist)")
    ok("Correct resolution",    "LIQUIDATED  =>  return = -1.0  (margin wiped)")

    # ------------------------------------------------------------------
    section("STEP 3 -- Insert past-dated LONG prediction")
    # ------------------------------------------------------------------

    timestamp    = datetime.combine(entry_date, datetime.min.time())
    horizon_days = (horizon_date - entry_date).days

    p = Prediction(
        track="crypto",
        asset=asset,
        direction="long",
        conviction=8,
        horizon_days=horizon_days,
        horizon_date=horizon_date,
        thesis=(
            f"[LIQ-TEST -- DELETE ME]  {asset} LONG {leverage}x.  "
            f"Entry {entry_date}, horizon {horizon_date}.  "
            f"Hourly low ${window_low:,.2f} < liq ${liq_price:,.2f}; "
            f"close ${horizon_price:,.2f} > entry ${entry_price:,.2f}.  "
            f"Expected: LIQUIDATED, return=-1.0, NOT a win."
        ),
        agents=[TEST_AGENT],
        leverage=leverage,
        timestamp=timestamp,
        entry_price=entry_price,
        liquidation_price=liq_price,
    )
    p.id = insert_prediction(p)

    ok("Prediction ID",         f"#{p.id}")
    ok("Asset / direction",     f"{p.asset} LONG  {p.leverage}x")
    ok("Entry price",           f"${p.entry_price:,.2f}")
    ok("Liquidation price",     f"${p.liquidation_price:,.2f}")
    ok("Horizon date",          p.horizon_date)

    # ------------------------------------------------------------------
    section("STEP 4 -- Resolve (liq check runs inside resolve_prediction)")
    # ------------------------------------------------------------------

    step(f"Calling resolve_prediction(#{p.id}) ...")
    print(f"\n     resolve_prediction() will:")
    print(f"       1. Fetch closing price at {horizon_date}")
    print(f"       2. Call _check_liquidation(): fetch ALL hourly prices "
          f"{entry_date} -> {horizon_date}")
    print(f"       3. Check: any price <= ${liq_price:,.2f}  (LONG liq level)")
    print(f"       4. If yes: set return=-1.0, liquidated=True, skip closing-price path")

    try:
        resolved = resolve_prediction(p.id)
    except (ValueError, BadPriceData) as exc:
        print(f"\n  [ERROR] resolve_prediction failed: {exc}")
        sys.exit(1)

    # ------------------------------------------------------------------
    section("STEP 5 -- Three prices: verify the logic with your own eyes")
    # ------------------------------------------------------------------

    print(f"\n  {'Liquidation level':<36}  ${resolved.liquidation_price:,.2f}")
    print(f"  {'Window low (hourly, from liq check)':<36}  ${window_low:,.2f}  "
          f"<-- BELOW liq level  [triggered]")
    print(f"  {'Closing price at horizon':<36}  ${resolved.resolution_price:,.2f}  "
          f"<-- ABOVE entry ${entry_price:,.2f}  [would be a win]")

    print(f"\n  Resolution notes:")
    print(f"    {resolved.resolution_notes}")

    # ------------------------------------------------------------------
    section("STEP 6 -- Assertions: liq must override recovery")
    # ------------------------------------------------------------------

    print()
    _assert("liquidated",      resolved.liquidated,      True)
    _assert("outcome_correct", resolved.outcome_correct, False)
    _assert("return_achieved", resolved.return_achieved, -1.0)

    print(f"\n  KEY INVARIANT CONFIRMED:")
    print(f"  Closing price ${resolved.resolution_price:,.2f} is above entry "
          f"${entry_price:,.2f} ({recovery_pct:+.2%}).")
    print(f"  Without liq check: leveraged return = {counterfactual_ret:+.2%}  "
          f"[would have been scored as a WIN].")
    print(f"  With liq check:    return = -100.00%  [liquidated mid-window, "
          f"recovery is irrelevant].")

    # ------------------------------------------------------------------
    section("CLEANUP REMINDER")
    # ------------------------------------------------------------------

    print(f"\n  Test record: #{p.id}  ({asset} LONG {leverage}x)")
    print(f"  Agent tag: {TEST_AGENT!r}\n")
    print(f"  To delete it, run:")
    print(f"      python test_liquidation.py --cleanup")
    print(f"\n  Or in SQLite directly:")
    print(f"      DELETE FROM predictions WHERE agents LIKE '%{TEST_AGENT}%';")
    print()


def _assert(label: str, actual, expected) -> None:
    passed = actual == expected
    icon = "[PASS]" if passed else "[FAIL]"
    print(f"  {icon}  {label:<22}  expected={expected!r:<8}  got={actual!r}")
    if not passed:
        print(f"\n  ASSERTION FAILED on '{label}'.")
        print(f"  The liquidation invariant is broken -- investigate resolve.py immediately.")
        sys.exit(1)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Liquidation invariant smoke test"
    )
    parser.add_argument(
        "--cleanup", action="store_true",
        help=f"Delete all records tagged with agent '{TEST_AGENT}'",
    )
    args = parser.parse_args()

    if args.cleanup:
        do_cleanup()
    else:
        run_test()
