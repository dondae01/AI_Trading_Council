"""
Human-readable display of predictions from the database.

Uses `tabulate` if installed (listed in requirements.txt) and falls back
to a plain fixed-width formatter if it is missing.
"""

from datetime import date
from typing import List, Optional

from .db import get_predictions
from .models import Prediction

try:
    from tabulate import tabulate as _tabulate
    _HAS_TABULATE = True
except ImportError:
    _HAS_TABULATE = False

_SEP = "=" * 88


def print_predictions(
    track: Optional[str] = None,
    status: Optional[str] = None,
    verbose: bool = False,
) -> None:
    """
    Print predictions to stdout in a readable table format.

    Parameters
    ----------
    track   : filter to "crypto" or "equities"
    status  : filter to "pending" or "resolved"
    verbose : if True, also print thesis and resolution notes for each row
    """
    resolved_only = status == "resolved"
    pending_only = status == "pending"

    predictions = get_predictions(
        track=track,
        resolved_only=resolved_only,
        pending_only=pending_only,
    )

    if not predictions:
        print("No predictions found.")
        return

    pending = [p for p in predictions if not p.is_resolved]
    resolved = [p for p in predictions if p.is_resolved]

    if pending and status != "resolved":
        _print_section("PENDING PREDICTIONS", pending, resolved=False, verbose=verbose)
    if resolved and status != "pending":
        _print_section("RESOLVED PREDICTIONS", resolved, resolved=True, verbose=verbose)

    total = len(predictions)
    res_count = len(resolved)
    pend_count = len(pending)
    print(f"\n  Total: {total}  |  Resolved: {res_count}  |  Pending: {pend_count}")
    if not resolved_only and not pending_only:
        print(f"  (Use --status resolved or --status pending to filter.)")


def _print_section(
    title: str,
    predictions: List[Prediction],
    resolved: bool,
    verbose: bool,
) -> None:
    print(f"\n{_SEP}")
    print(f"  {title}  ({len(predictions)})")
    print(_SEP)

    if resolved:
        headers = [
            "ID", "Track", "Asset", "Dir", "Lev", "Conv",
            "Horizon", "Entry", "Exit", "Chg%", "Return*", "Status", "Agents",
        ]
        rows = [_resolved_row(p) for p in predictions]
        liquidated_n = sum(1 for p in predictions if p.liquidated)
        note = (
            "* Return = direction-signed P&L (leverage-scaled) for long/short/buy/sell; "
            "'--' for hold/avoid (not in Sharpe)."
        )
        if liquidated_n:
            note += f"  {liquidated_n} LIQUIDATED = -1.0 (total margin loss)."
    else:
        headers = [
            "ID", "Track", "Asset", "Dir", "Lev", "Conv",
            "Horizon Date", "Entry Price", "Liq Price", "Status", "Agents",
        ]
        rows = [_pending_row(p) for p in predictions]
        note = None

    _print_table(headers, rows)

    if note:
        print(f"\n  {note}")

    if verbose:
        print()
        for p in predictions:
            _print_thesis(p)


def _resolved_row(p: Prediction) -> list:
    entry = p.entry_price or 0.0
    res = p.resolution_price or 0.0
    raw_pct = ((res - entry) / entry) if entry else None

    return [
        p.id,
        p.track,
        p.asset,
        p.direction.upper(),
        _fmt_leverage(p),
        f"{p.conviction}/10",
        p.horizon_date.isoformat() if p.horizon_date else "--",
        _fmt_price(p.entry_price),
        _fmt_price(p.resolution_price),
        _fmt_pct(raw_pct),
        _fmt_pct(p.return_achieved) if p.feeds_returns else "--",
        _outcome_label(p),
        _fmt_agents(p.agents),
    ]


def _pending_row(p: Prediction) -> list:
    return [
        p.id,
        p.track,
        p.asset,
        p.direction.upper(),
        _fmt_leverage(p),
        f"{p.conviction}/10",
        p.horizon_date.isoformat() if p.horizon_date else "--",
        _fmt_price(p.entry_price),
        _fmt_price(p.liquidation_price) if p.liquidation_price is not None else "--",
        p.status_label,
        _fmt_agents(p.agents),
    ]


def _print_thesis(p: Prediction) -> None:
    print(f"  #{p.id} [{p.asset} {p.direction.upper()}]  Thesis: {p.thesis}")
    if p.resolution_notes:
        print(f"         Resolution: {p.resolution_notes}")
    print()


# ------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------

def _outcome_label(p: Prediction) -> str:
    if p.liquidated:
        return "LIQUIDATED"
    return "CORRECT" if p.outcome_correct else "WRONG"


def _fmt_leverage(p: Prediction) -> str:
    return f"{p.leverage}x" if p.leverage > 1 else "--"


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


def _fmt_agents(agents: list) -> str:
    return ", ".join(agents) if agents else "--"


def _print_table(headers: list, rows: list) -> None:
    if _HAS_TABULATE:
        print(_tabulate(rows, headers=headers, tablefmt="simple"))
        return

    # Plain fallback: pad columns manually
    all_rows = [headers] + [[str(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in all_rows) for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    sep = "  ".join("-" * w for w in widths)
    print(fmt.format(*headers))
    print(sep)
    for row in all_rows[1:]:
        print(fmt.format(*row))
