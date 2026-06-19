"""
Resolution logic.

When a prediction's horizon_date has passed, resolve_prediction():
  1. Fetches the resolution price via the price layer.
  2. Runs a sanity check against the prior day (90% move guard).
  3. For crypto futures positions (long/short, leverage > 1):
       a. Fetches all available price points in the window (hourly via CoinGecko).
       b. If any price touched the liquidation level -> total loss (-1.0), done.
  4. Determines correctness:
       long  -> correct if price rose    (raw_return > 0)
       short -> correct if price fell    (raw_return < 0)
       buy   -> correct if price rose    (raw_return > 0)
       sell  -> correct if price fell    (raw_return < 0)
       hold  -> correct if no decline    (raw_return >= 0)
       avoid -> correct if price fell    (raw_return < 0)
  5. Computes return_achieved for long/short/buy/sell only:
       long/buy:   (resolution - entry) / entry * leverage  (positive = profit)
       short/sell: (entry - resolution) / entry * leverage  (positive = profit)
     return_achieved is NULL for hold/avoid (no capital exposure).
  6. Writes everything back to the database.

resolve_all_due() finds every overdue unresolved prediction and attempts
to resolve them in a batch, collecting per-prediction results and errors
rather than aborting the whole batch on one bad price.

Liquidation note
----------------
Liquidation check uses CoinGecko market_chart/range which provides hourly
data for windows <= 90 days.  Sub-hourly wicks may not be captured.  This
is the finest granularity available on the free CoinGecko tier and is noted
in resolution_notes when a liq check is performed.
"""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .db import get_prediction, get_due_predictions, update_resolution
from .models import Prediction
from .prices import get_price_source, sanity_check, BadPriceData


def resolve_prediction(prediction_id: int) -> Prediction:
    """
    Resolve a single prediction by ID.  Raises if:
      - the prediction does not exist
      - the prediction is already resolved
      - the horizon date has not passed yet
      - the fetched price fails the sanity check
      - the prediction has no entry_price (cannot compute a return)
    """
    p = get_prediction(prediction_id)
    if p is None:
        raise ValueError(f"No prediction found with id={prediction_id}")
    if p.is_resolved:
        raise ValueError(f"Prediction {prediction_id} is already resolved.")
    if p.horizon_date > date.today():
        raise ValueError(
            f"Prediction {prediction_id} horizon ({p.horizon_date}) has not passed yet."
        )
    if p.entry_price is None:
        raise ValueError(
            f"Prediction {prediction_id} has no entry_price -- cannot compute a return.  "
            f"Was it logged with skip_price_fetch=True?"
        )

    source = get_price_source(p.track)

    # Fetch resolution price on the exact horizon date
    resolution_price = source.get_price(p.asset, p.horizon_date)

    # Prior-day price for the sanity glitch guard (best-effort)
    prior_price: Optional[float] = None
    try:
        prior_price = source.get_price(p.asset, p.horizon_date - timedelta(days=1))
    except Exception:
        pass

    sanity_check(resolution_price, p.asset, p.horizon_date, prior_price=prior_price)

    # ------------------------------------------------------------------
    # Liquidation check (crypto futures only)
    # ------------------------------------------------------------------
    liquidated: Optional[bool] = None
    outcome_correct: bool
    return_achieved: Optional[float]
    notes: str

    liq_applicable = (
        p.track == "crypto"
        and p.is_leveraged
        and p.direction in ("long", "short")
        and p.liquidation_price is not None
    )

    if liq_applicable:
        liquidated, liq_note = _check_liquidation(p, source)
    else:
        liq_note = ""

    if liquidated:
        # Total margin loss -- closing price is irrelevant
        outcome_correct = False
        return_achieved = -1.0
        notes = (
            f"Entry ${p.entry_price:.6g} | Liq level ${p.liquidation_price:.6g} | "
            f"Resolution ${resolution_price:.6g} (horizon close, irrelevant) | "
            f"{liq_note} | [hourly granularity -- sub-hourly wicks not captured]"
        )
    else:
        raw_return = (resolution_price - p.entry_price) / p.entry_price
        outcome_correct = _is_correct(p.direction, raw_return)
        return_achieved = (
            _direction_signed_return(p.direction, raw_return, p.leverage)
            if p.feeds_returns
            else None
        )
        lev_note = f" | Leverage {p.leverage}x" if p.leverage > 1 else ""
        hold_note = " | not in Sharpe (hold/avoid)" if not p.feeds_returns else ""
        skipped_note = f" | {liq_note}" if liq_note else ""
        notes = (
            f"Entry ${p.entry_price:.6g} | Resolution ${resolution_price:.6g} | "
            f"Raw Delta {raw_return:+.2%}{lev_note}{hold_note}{skipped_note}"
        )

    update_resolution(
        prediction_id=prediction_id,
        resolution_date=datetime.utcnow(),
        resolution_price=resolution_price,
        liquidated=liquidated,
        outcome_correct=outcome_correct,
        return_achieved=return_achieved,
        resolution_notes=notes,
    )

    return get_prediction(prediction_id)


def resolve_all_due() -> List[Dict[str, Any]]:
    """
    Find all overdue unresolved predictions and attempt to resolve each.
    Errors on individual predictions are captured, not raised, so one bad
    price feed does not abort the whole batch.
    """
    due = get_due_predictions()
    if not due:
        return []

    results = []
    for p in due:
        try:
            resolved = resolve_prediction(p.id)
            results.append({
                "id": p.id,
                "asset": p.asset,
                "direction": p.direction,
                "leverage": p.leverage,
                "status": "resolved",
                "liquidated": resolved.liquidated,
                "correct": resolved.outcome_correct,
                "return": resolved.return_achieved,
            })
        except (BadPriceData, ValueError) as exc:
            results.append({
                "id": p.id,
                "asset": p.asset,
                "direction": p.direction,
                "status": "error",
                "error": str(exc),
            })
    return results


# ------------------------------------------------------------------
# Liquidation check helper
# ------------------------------------------------------------------

def _check_liquidation(p: Prediction, source) -> Tuple[bool, str]:
    """
    Fetch all available price points for the prediction window and check
    whether the liquidation level was touched.

    Returns (liquidated, note_string) on success.

    Raises BadPriceData on any failure -- liquidation status is binary and
    cannot be assumed either way.  The caller lets this propagate so the
    prediction stays unresolved in the DB rather than being falsely recorded
    as a survivor.  An honest unresolved trade beats a falsely-survived one.
    """
    if not hasattr(source, "get_prices_in_window"):
        raise BadPriceData(
            f"Prediction #{p.id} ({p.asset} {p.direction} {p.leverage}x) requires a "
            f"liquidation check but the price source does not support intraday windows. "
            f"Leaving prediction unresolved."
        )

    entry_date = p.timestamp.date()
    horizon_date = p.horizon_date
    liq = p.liquidation_price

    try:
        prices = source.get_prices_in_window(p.asset, entry_date, horizon_date)
    except Exception as exc:
        raise BadPriceData(
            f"Liquidation check failed for prediction #{p.id} ({p.asset} {p.direction} "
            f"{p.leverage}x): {exc}. Leaving prediction unresolved -- cannot assume survival."
        ) from exc

    if not prices:
        raise BadPriceData(
            f"Liquidation check returned an empty price window for prediction #{p.id} "
            f"({p.asset} {entry_date} -> {horizon_date}). "
            f"Leaving prediction unresolved -- cannot assume survival."
        )

    if p.direction == "long":
        hit = any(price <= liq for price in prices)
        if hit:
            low = min(prices)
            return True, f"LIQUIDATED: low of ${low:.6g} touched liq ${liq:.6g} (LONG {p.leverage}x)"
    else:  # short
        hit = any(price >= liq for price in prices)
        if hit:
            high = max(prices)
            return True, f"LIQUIDATED: high of ${high:.6g} touched liq ${liq:.6g} (SHORT {p.leverage}x)"

    n = len(prices)
    return False, f"liq check passed ({n} price points, entry {entry_date} -> horizon {horizon_date}, hourly)"


# ------------------------------------------------------------------
# Correctness and return helpers
# ------------------------------------------------------------------

def _is_correct(direction: str, raw_return: float) -> bool:
    """
    Whether the direction call was right given the raw price return.

    long/buy  : correct if price rose    (raw_return > 0)
    short/sell: correct if price fell    (raw_return < 0)
    hold      : correct if no decline    (raw_return >= 0)
    avoid     : correct if price fell    (raw_return < 0)
    """
    if direction in ("long", "buy"):
        return raw_return > 0
    if direction in ("short", "sell"):
        return raw_return < 0
    if direction == "hold":
        return raw_return >= 0
    if direction == "avoid":
        return raw_return < 0
    raise ValueError(f"Unknown direction: {direction!r}")


def _direction_signed_return(
    direction: str, raw_return: float, leverage: int = 1
) -> float:
    """
    Convert raw price return into a direction-signed, leverage-scaled return.
    Only called for long/short/buy/sell (capital-exposed) predictions.

    long/buy  : profit = price rise  -> raw_return * leverage
    short/sell: profit = price fall  -> -raw_return * leverage

    Result is positive when the prediction was correct.
    Not called when liquidated (caller uses -1.0 directly).
    """
    if direction in ("long", "buy"):
        return raw_return * leverage
    if direction in ("short", "sell"):
        return -raw_return * leverage
    raise ValueError(
        f"_direction_signed_return called for non-capital direction: {direction!r}"
    )
