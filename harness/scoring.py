"""
Scoring and evaluation logic for the Trading Council.

Implements the four proof-bar conditions from S6 of EVALUATION_FRAMEWORK.md.

Key design decisions
--------------------

1.  Two scoring paths:
      - long/short/buy/sell predictions -> feed hit rate AND Sharpe/drawdown/returns
      - hold/avoid predictions -> feed hit rate ONLY
    Folding avoided losses into the returns math would inflate apparent
    profitability because there is no marked-to-market position.
    Prediction.feeds_returns is the single gate controlling this split.

2.  Per-prediction matched baseline windows:
    For each resolved long/short/buy/sell council prediction, the baseline
    asset (BTC for crypto, SPY for equities) return is computed over the
    EXACT SAME time window -- from the council prediction's entry date to
    its resolution date.

    The baseline is ALWAYS unleveraged buy-and-hold.  For crypto, this
    means raw BTC spot return with NO leverage multiplier.  Leveraged
    buy-and-hold is a self-liquidating strategy, not a passive benchmark,
    and would silently lower the proof bar.

    Why matched windows matter:
      - Predictions have different horizons (7d, 30d, 90d).  An aggregate
        baseline Sharpe over the full period hides this.
      - If the council made many predictions during a bull run, the matched
        baseline covers that same bull run -- a fair fight.

3.  Sample size warning:
    Sharpe on small samples is statistically meaningless.  Any Sharpe
    output below 30 resolved predictions carries a prominent warning and
    the number is labeled "[UNRELIABLE -- n<30]".  The 30-prediction minimum
    comes from the locked proof bar in S6.

4.  Sharpe formula:
    mean(returns) / std(returns, ddof=1).  No annualization factor is
    applied because predictions have variable horizons and because the
    SAME formula is applied to both council returns and matched baseline
    returns -- so the comparison is still apples-to-apples.

5.  Two council return series (crypto track):

    SKILL returns (unleveraged): return_achieved / leverage
      - Strips the leverage multiplier to isolate directional skill.
      - Compared directly against the unleveraged baseline Sharpe (cond_1).
      - For a liquidated position at Lx, unleveraged loss = -1.0/L
        (the price moved exactly 1/L against you -- the definition of liq).

    REAL returns (leveraged): return_achieved as stored
      - What would actually be experienced on real capital.
      - Reported separately as "actual experience" -- clearly labelled.
      - Drives cond_4 (drawdown limit) since that is a real capital risk gate.
      - NEVER enters the skill-vs-baseline Sharpe comparison (cond_1).

    For equities: leverage = 1 always, so skill == real.
"""

import math
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .models import Prediction
from .prices import BadPriceData, get_price_source, sanity_check

MIN_SAMPLE_SIZE = 30  # proof-bar minimum, S6 EVALUATION_FRAMEWORK.md
BASELINE_ASSETS = {"crypto": "BTC", "equities": "SPY"}
DRAWDOWN_LIMITS = {"crypto": 0.40, "equities": 0.25}


# ------------------------------------------------------------------
# Individual metrics
# ------------------------------------------------------------------

def hit_rate(predictions: List[Prediction]) -> Dict[str, Any]:
    """
    Hit rate across ALL resolved predictions -- long, short, buy, sell, hold, avoid.
    hold/avoid are scored here and nowhere else in the returns math.
    """
    resolved = [p for p in predictions if p.is_resolved]
    if not resolved:
        return {"rate": None, "n": 0, "correct": 0, "wrong": 0,
                "buy_sell_n": 0, "hold_avoid_n": 0,
                "buy_sell_hit_rate": None, "hold_avoid_hit_rate": None}
    correct = sum(1 for p in resolved if p.outcome_correct)
    capital_exposed = [p for p in resolved if p.feeds_returns]
    hold_avoid = [p for p in resolved if not p.feeds_returns]
    return {
        "rate": correct / len(resolved),
        "n": len(resolved),
        "correct": correct,
        "wrong": len(resolved) - correct,
        "buy_sell_n": len(capital_exposed),    # long/short/buy/sell
        "hold_avoid_n": len(hold_avoid),
        "buy_sell_hit_rate": (
            sum(1 for p in capital_exposed if p.outcome_correct) / len(capital_exposed)
            if capital_exposed else None
        ),
        "hold_avoid_hit_rate": (
            sum(1 for p in hold_avoid if p.outcome_correct) / len(hold_avoid)
            if hold_avoid else None
        ),
    }


def sharpe_ratio(returns: List[float]) -> Optional[float]:
    """
    Sharpe ratio: mean(r) / std(r, ddof=1).
    Returns None for fewer than 2 data points or zero standard deviation.

    No annualization is applied (see module docstring S4).
    """
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(variance)
    if std == 0:
        return None
    return mean / std


def max_drawdown(returns: List[float]) -> float:
    """
    Maximum peak-to-trough drawdown from a sequence of per-prediction returns.
    Predictions are ordered chronologically (by resolution date) before calling.
    Returns 0.0 for an empty or single-element series.
    """
    if len(returns) < 2:
        return 0.0
    equity = [1.0]
    for r in returns:
        equity.append(equity[-1] * (1 + r))
    peak = equity[0]
    worst = 0.0
    for val in equity:
        peak = max(peak, val)
        dd = (peak - val) / peak
        worst = max(worst, dd)
    return worst


# ------------------------------------------------------------------
# Baseline: per-prediction matched windows (always unleveraged spot)
# ------------------------------------------------------------------

def fetch_matched_baseline_returns(
    track: str,
    capital_predictions: List[Prediction],
) -> Tuple[List[float], List[str]]:
    """
    For each resolved long/short/buy/sell prediction, fetch the baseline
    asset's UNLEVERAGED buy-and-hold return over the EXACT SAME window.

    The baseline is always raw spot return -- no leverage multiplier, ever.
    Leveraged buy-and-hold is a self-liquidating strategy, not a passive
    benchmark; applying leverage to the baseline silently lowers the bar.

    Window alignment:
      council prediction logged 2026-07-01, resolved 2026-07-31
      -> baseline = (BTC[2026-07-31] - BTC[2026-07-01]) / BTC[2026-07-01]

    Returns
    -------
    (baseline_returns, skipped_reasons)
      baseline_returns : one float per matched prediction (in the same order)
      skipped_reasons  : human-readable strings for predictions skipped due
                         to missing/bad baseline price data
    """
    if track not in BASELINE_ASSETS:
        raise ValueError(f"Unknown track: {track!r}")

    baseline_asset = BASELINE_ASSETS[track]
    source = get_price_source(track)
    returns: List[float] = []
    skipped: List[str] = []

    for p in capital_predictions:
        if not p.is_resolved or p.resolution_date is None:
            continue
        entry_date = p.timestamp.date()
        resolution_date = p.resolution_date.date()
        try:
            entry_px = source.get_price(baseline_asset, entry_date)
            sanity_check(entry_px, baseline_asset, entry_date)
            res_px = source.get_price(baseline_asset, resolution_date)
            sanity_check(res_px, baseline_asset, resolution_date, prior_price=entry_px)
            # Unleveraged spot return -- no leverage multiplication.
            # The baseline is always unleveraged buy-and-hold; leveraged
            # buy-and-hold is not a passive benchmark.
            returns.append((res_px - entry_px) / entry_px)
        except BadPriceData as exc:
            skipped.append(f"Prediction #{p.id} ({p.asset}): {exc}")
        except Exception as exc:
            skipped.append(
                f"Prediction #{p.id} ({p.asset}): unexpected error fetching "
                f"{baseline_asset}: {exc}"
            )

    return returns, skipped


# ------------------------------------------------------------------
# Full proof-bar report
# ------------------------------------------------------------------

def full_report(track: str, predictions: List[Prediction]) -> Dict[str, Any]:
    """
    Full evaluation report for one track.  Checks all four proof-bar
    conditions from S6 of EVALUATION_FRAMEWORK.md:

      1. Council Sharpe > baseline Sharpe  (over matched prediction windows)
      2. Council Sharpe > manual picks Sharpe  [NOT YET TRACKED -- flagged N/A]
      3. >= 30 resolved predictions in total
      4. Max drawdown <= 40% (crypto) or 25% (equities)

    Sample size is reported prominently.  When n < 30, a warning is
    attached to every Sharpe figure because the ratio is statistically
    noisy on small samples.

    For crypto: also reports unleveraged council Sharpe to separate
    directional skill from leverage contribution.
    """
    if track not in BASELINE_ASSETS:
        raise ValueError(f"Unknown track {track!r}.  Must be 'crypto' or 'equities'.")

    resolved = [p for p in predictions if p.is_resolved]

    # Split into two scoring paths (chronological order for drawdown)
    capital_exposed = sorted(
        [p for p in resolved if p.feeds_returns],
        key=lambda p: p.resolution_date or date.min,
    )
    hold_avoid = [p for p in resolved if not p.feeds_returns]

    # Pair each prediction with its stored return_achieved so we can access
    # p.leverage alongside the return for the unleveraged calculation.
    # Liquidated positions store -1.0 -- that IS their return, include them.
    exposed_with_returns = [
        (p, p.return_achieved)
        for p in capital_exposed
        if p.return_achieved is not None
    ]

    # REAL returns: leverage-scaled, what would actually be experienced on capital.
    # Drives cond_4 (drawdown limit) and reported as "actual experience."
    # NEVER enters the skill-vs-baseline Sharpe comparison.
    real_returns = [r for _, r in exposed_with_returns]

    # SKILL returns: unleveraged (real / leverage), isolates directional accuracy.
    # Compared directly against the unleveraged BTC baseline (cond_1).
    # For a liquidated position at Lx: -1.0/L = the price moved 1/L against entry,
    # which is exactly what triggers liquidation -- so the formula is consistent.
    skill_returns = [r / p.leverage for p, r in exposed_with_returns]

    n_resolved = len(resolved)
    n_capital = len(capital_exposed)
    liquidation_count = sum(1 for p in capital_exposed if p.liquidated)
    below_minimum = n_resolved < MIN_SAMPLE_SIZE
    sharpe_unreliable = n_capital < MIN_SAMPLE_SIZE

    # Skill Sharpe (unleveraged) -- primary metric, used in cond_1
    council_sharpe_skill = sharpe_ratio(skill_returns)
    council_drawdown_skill = max_drawdown(skill_returns)
    council_total_skill = (
        math.prod(1 + r for r in skill_returns) - 1 if skill_returns else None
    )

    # Real Sharpe (leveraged) -- actual capital experience, informational only
    council_sharpe_real = sharpe_ratio(real_returns)
    council_drawdown_real = max_drawdown(real_returns)   # drives cond_4
    council_total_real = (
        math.prod(1 + r for r in real_returns) - 1 if real_returns else None
    )

    # Baseline: UNLEVERAGED BTC buy-and-hold per matched window.
    # fetch_matched_baseline_returns returns raw spot returns -- no leverage multiplier.
    baseline_rets, baseline_skipped = fetch_matched_baseline_returns(track, capital_exposed)
    baseline_sharpe = sharpe_ratio(baseline_rets)
    baseline_total = (
        math.prod(1 + r for r in baseline_rets) - 1 if baseline_rets else None
    )

    # Proof-bar conditions
    drawdown_limit = DRAWDOWN_LIMITS[track]
    # cond_1: unleveraged council skill vs unleveraged baseline -- like for like
    cond_1 = (
        council_sharpe_skill is not None
        and baseline_sharpe is not None
        and council_sharpe_skill > baseline_sharpe
    )
    cond_3 = n_resolved >= MIN_SAMPLE_SIZE
    # cond_4: leveraged drawdown -- real capital risk gate
    cond_4 = council_drawdown_real <= drawdown_limit

    def _labeled_sharpe(value: Optional[float], n: int, suffix: str = "") -> str:
        if value is None:
            return "N/A (insufficient data)"
        label = f"{value:.4f}{suffix}"
        if n < MIN_SAMPLE_SIZE:
            label += f"  [UNRELIABLE -- only {n} predictions, need {MIN_SAMPLE_SIZE}]"
        return label

    scoring_note = (
        "Sharpe and drawdown: long/short/buy/sell predictions only.  "
        "hold/avoid count only toward hit rate.  "
        "Skill Sharpe = unleveraged returns (directional accuracy).  "
        "Real Sharpe = leveraged returns (actual capital experience)."
    )
    if liquidation_count:
        scoring_note += (
            f"  {liquidation_count} liquidation(s) recorded as -1.0 real return "
            f"(total margin loss); unleveraged equivalent = -1.0/leverage."
        )

    return {
        "track": track,
        "sample": {
            "n_resolved_total": n_resolved,
            "n_buy_sell": n_capital,
            "n_hold_avoid": len(hold_avoid),
            "n_liquidated": liquidation_count,
            "below_proof_bar_minimum": below_minimum,
            "sharpe_unreliable": sharpe_unreliable,
            "warning": (
                f"WARNING: SAMPLE TOO SMALL -- {n_resolved}/{MIN_SAMPLE_SIZE} resolved "
                f"predictions.  Proof bar requires {MIN_SAMPLE_SIZE}.  "
                f"All Sharpe figures below are unreliable."
            ) if below_minimum else None,
        },
        "hit_rate": hit_rate(predictions),
        "council": {
            # Skill metrics (unleveraged) -- basis for proof bar cond_1
            "sharpe_raw": council_sharpe_skill,
            "sharpe_labeled": _labeled_sharpe(
                council_sharpe_skill, n_capital,
                suffix="  [skill: unleveraged]" if track == "crypto" else "",
            ),
            "drawdown": council_drawdown_skill,
            "total_return": council_total_skill,
            # Real metrics (leveraged) -- actual capital experience, informational
            "sharpe_real_raw": council_sharpe_real,
            "sharpe_real_labeled": _labeled_sharpe(
                council_sharpe_real, n_capital,
                suffix="  [real: leveraged, actual P&L]" if track == "crypto" else "",
            ),
            "drawdown_real": council_drawdown_real,     # used in cond_4
            "total_return_real": council_total_real,
            "n_predictions_in_returns": n_capital,
            "n_liquidated": liquidation_count,
            "scoring_note": scoring_note,
        },
        "baseline": {
            "asset": BASELINE_ASSETS[track],
            "sharpe_raw": baseline_sharpe,
            "sharpe_labeled": _labeled_sharpe(baseline_sharpe, len(baseline_rets)),
            "total_return": baseline_total,
            "n_matched": len(baseline_rets),
            "n_skipped": len(baseline_skipped),
            "skipped_details": baseline_skipped,
            "alignment_method": (
                "Per-prediction matched windows: for each council long/short/buy/sell "
                "prediction, the baseline (unleveraged buy-and-hold) return is computed "
                "over the SAME entry-date to resolution-date window.  "
                "Both series use the same Sharpe formula.  "
                + (
                    "Crypto baseline = unleveraged BTC spot; no leverage applied."
                    if track == "crypto"
                    else
                    "Equities baseline = unleveraged SPY spot."
                )
            ),
        },
        "proof_bar": {
            "drawdown_limit": drawdown_limit,
            "cond_1_beats_baseline_on_sharpe": cond_1,
            "cond_2_beats_manual_picks": None,  # manual picks not yet tracked
            "cond_3_min_30_resolved": cond_3,
            "cond_4_max_drawdown_within_limit": cond_4,
            "all_four_cleared": cond_1 and cond_3 and cond_4,
            "note_cond_2": (
                "Condition 2 (beat manual picks) is not yet tracked.  "
                "Log manual decisions to enable this check."
            ),
        },
    }
