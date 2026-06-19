from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional


@dataclass
class Prediction:
    # --- Required at log time ---
    track: str          # "crypto" or "equities"
    asset: str
    # crypto directions: "long" | "short" | "hold" | "avoid"
    # equities directions: "buy"  | "sell"  | "hold" | "avoid"
    direction: str
    conviction: int     # 1-10
    horizon_days: int
    thesis: str
    agents: List[str]
    leverage: int = 1   # 1 = unleveraged (equities/spot); Nx for crypto futures

    # --- Set automatically at log time ---
    id: Optional[int] = None
    timestamp: Optional[datetime] = None
    horizon_date: Optional[date] = None
    entry_price: Optional[float] = None       # live price captured at log time
    # Isolated-margin liquidation level stored at log time.
    # LONG Lx: entry * (1 - 1/L)   SHORT Lx: entry * (1 + 1/L)
    # NULL for equities, hold/avoid, or leverage == 1.
    liquidation_price: Optional[float] = None

    # --- Filled in at resolution time ---
    resolution_date: Optional[datetime] = None
    resolution_price: Optional[float] = None
    liquidated: Optional[bool] = None         # True if price hit liq level during window
    outcome_correct: Optional[bool] = None
    # NULL for hold/avoid (no capital exposure).
    # -1.0 if liquidated (total margin loss).
    # Otherwise: direction-signed, leverage-scaled return on margin.
    #   long/buy:   (resolution - entry) / entry * leverage
    #   short/sell: (entry - resolution) / entry * leverage
    return_achieved: Optional[float] = None
    resolution_notes: Optional[str] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_resolved(self) -> bool:
        return self.outcome_correct is not None

    @property
    def feeds_returns(self) -> bool:
        """
        True for directions with capital exposure (long/short for crypto,
        buy/sell for equities).  hold/avoid count only toward hit rate.
        """
        return self.direction in ("buy", "sell", "long", "short")

    @property
    def is_leveraged(self) -> bool:
        return self.leverage > 1

    @property
    def status_label(self) -> str:
        if not self.is_resolved:
            if self.horizon_date is None:
                return "pending"
            delta = (self.horizon_date - date.today()).days
            if delta < 0:
                return f"OVERDUE ({abs(delta)}d)"
            if delta == 0:
                return "DUE TODAY"
            return f"pending ({delta}d)"
        if self.liquidated:
            return "LIQUIDATED"
        return "CORRECT" if self.outcome_correct else "WRONG"
