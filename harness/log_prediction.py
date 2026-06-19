"""
Core prediction-logging function.

Entry price is fetched LIVE at call time via the price layer and stored
immediately in the database.  It is never back-filled from historical data
at resolution time -- this prevents hindsight bias in the price baseline.

For leveraged crypto positions (long/short with leverage > 1), the
liquidation price is also computed and stored at log time using the
simplified isolated-margin formula:
    LONG  Lx: entry * (1 - 1/L)
    SHORT Lx: entry * (1 + 1/L)

If the live price fetch fails the function raises rather than silently
logging a prediction without an entry price, because a prediction without
an entry price cannot be resolved or scored later.
Pass skip_price_fetch=True only during testing.
"""

from datetime import datetime, timedelta
from typing import List, Optional

from .db import insert_prediction
from .models import Prediction
from .prices import get_price_source, sanity_check, BadPriceData

VALID_TRACKS = ("crypto", "equities")

# Per-track valid directions.
TRACK_DIRECTIONS = {
    "crypto":   ("long", "short", "hold", "avoid"),
    "equities": ("buy",  "sell",  "hold", "avoid"),
}

# Union of all valid directions -- kept for backward-compat import in main.py
# until that file is updated to use TRACK_DIRECTIONS.
VALID_DIRECTIONS = tuple(sorted(set(d for dirs in TRACK_DIRECTIONS.values() for d in dirs)))


def log_prediction(
    track: str,
    asset: str,
    direction: str,
    conviction: int,
    horizon_days: int,
    thesis: str,
    agents: List[str],
    leverage: int = 1,
    skip_price_fetch: bool = False,
) -> Prediction:
    """
    Validate inputs, fetch the live entry price, compute the liquidation
    price for leveraged crypto positions, write to the database, and return
    the saved Prediction with its assigned id.

    Parameters
    ----------
    track         : "crypto" or "equities"
    asset         : ticker / symbol (e.g. "ETH", "BTC", "NVDA")
    direction     : crypto -> "long"/"short"/"hold"/"avoid"
                    equities -> "buy"/"sell"/"hold"/"avoid"
    conviction    : 1-10
    horizon_days  : days until this prediction is judged
    thesis        : specific, falsifiable claim -- what would make it wrong?
    agents        : list of agent names that produced/challenged this call
    leverage      : futures leverage multiplier for crypto long/short (>= 1).
                    Equities must always be 1 (spot, no leverage).
    skip_price_fetch : if True, entry_price left NULL (testing only)
    """
    # --- Validate track ---
    if track not in VALID_TRACKS:
        raise ValueError(f"track must be one of {VALID_TRACKS}, got {track!r}")

    # --- Validate direction against track ---
    valid_dirs = TRACK_DIRECTIONS[track]
    if direction not in valid_dirs:
        raise ValueError(
            f"direction {direction!r} is not valid for the {track} track.  "
            f"Valid options: {valid_dirs}"
        )

    # --- Validate leverage ---
    if leverage < 1:
        raise ValueError(f"leverage must be >= 1, got {leverage}")
    if track == "equities" and leverage != 1:
        raise ValueError(
            f"Equities track is spot/unleveraged only.  leverage must be 1, got {leverage}."
        )
    if track == "crypto" and direction in ("hold", "avoid") and leverage != 1:
        raise ValueError(
            f"hold/avoid positions carry no capital exposure; leverage must be 1, got {leverage}."
        )

    # --- Validate other fields ---
    if not (1 <= conviction <= 10):
        raise ValueError(f"conviction must be 1-10, got {conviction}")
    if horizon_days < 1:
        raise ValueError(f"horizon_days must be >= 1, got {horizon_days}")
    if not thesis.strip():
        raise ValueError("thesis cannot be empty")
    if not agents:
        raise ValueError("at least one agent name is required")

    now = datetime.utcnow()
    horizon_date = (now + timedelta(days=horizon_days)).date()

    # --- Fetch live entry price ---
    entry_price: Optional[float] = None
    if not skip_price_fetch:
        source = get_price_source(track)
        entry_price = source.get_current_price(asset)
        sanity_check(entry_price, asset, now.date())

    # --- Compute liquidation price for leveraged crypto long/short ---
    liquidation_price: Optional[float] = None
    if entry_price is not None:
        liquidation_price = _compute_liquidation_price(direction, entry_price, leverage)

    p = Prediction(
        track=track,
        asset=asset.upper(),
        direction=direction,
        conviction=conviction,
        horizon_days=horizon_days,
        horizon_date=horizon_date,
        thesis=thesis.strip(),
        agents=agents,
        leverage=leverage,
        timestamp=now,
        entry_price=entry_price,
        liquidation_price=liquidation_price,
    )

    p.id = insert_prediction(p)
    return p


def _compute_liquidation_price(
    direction: str, entry_price: float, leverage: int
) -> Optional[float]:
    """
    Simplified isolated-margin liquidation price.

    LONG  at Lx: price must drop 1/L from entry to wipe the margin.
                 liq = entry * (1 - 1/L)
    SHORT at Lx: price must rise 1/L from entry to wipe the margin.
                 liq = entry * (1 + 1/L)

    Returns None for:
      - leverage == 1  (no meaningful liquidation for spot-equivalent positions)
      - hold/avoid     (no open position to liquidate)
    """
    if leverage <= 1 or direction not in ("long", "short"):
        return None
    if direction == "long":
        return entry_price * (1.0 - 1.0 / leverage)
    return entry_price * (1.0 + 1.0 / leverage)
