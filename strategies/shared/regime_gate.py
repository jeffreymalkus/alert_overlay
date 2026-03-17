"""Shared regime gate logic — single source of truth for dashboard + replay.

Time-normalized hostile-tape thresholds derived from SPY 1-min data
(126 trading days, 2025-09-15 to 2026-03-12).  75th percentile of
absolute % from open at each time bucket.

Strategy → gate type mapping and the check function live here so both
dashboard.py and replay_live_path.py import the same code.
"""

import math

# ── Strategy → gate type mapping (all 13 strategies) ──
STRATEGY_REGIME_GATE = {
    # Long, GREEN-only (5m)
    "SC_SNIPER":   "GREEN",
    "FL_ANTICHOP": "GREEN",
    "SP_ATIER":    "GREEN",
    "HH_QUALITY":  "GREEN",
    "EMA_FPIP":    "GREEN",
    "BS_STRUCT":   "GREEN",
    # Long, GREEN-only (1m)
    "EMA9_FT":     "GREEN",
    # Long, failed-move (permissive)
    "ORL_FBD_LONG":    "FAILED_LONG",
    "FFT_NEWLOW_REV":  "FAILED_LONG",
    # Short, RED/FLAT
    "BDR_SHORT":       "RED/FLAT",
    # Short, failed-breakout (permissive)
    "ORH_FBO_V2_A":    "FAILED_SHORT",
    "ORH_FBO_V2_B":    "FAILED_SHORT",
    "PDH_FBO_B":       "FAILED_SHORT",
}


def hostile_threshold(hhmm: int) -> float:
    """Return the hostile-tape threshold (% from open) for a given time of day.

    Thresholds are the 75th percentile of SPY absolute % from open,
    computed from 126 sessions of 1-min data (2025-09-15 to 2026-03-12).

    Below these thresholds, the SPY move is within normal range and
    should NOT trigger a hostile-tape block.

    Args:
        hhmm: Time of day as integer, e.g. 945, 1030, 1300

    Returns:
        Threshold as percentage (e.g. 0.16 means 0.16%)
    """
    if hhmm < 945:
        return 0.16   # P75 = 0.16% (pre-market, tight)
    elif hhmm < 1030:
        return 0.32   # P75 = 0.32% (morning range expansion)
    else:
        return 0.63   # P75 = 0.63% (midday+, wide normal range)


def check_regime_gate(strategy_name, spy_snap, bar_time_hhmm=None):
    """Check if the current SPY regime allows this strategy to fire.

    Uses time-normalized hostile thresholds instead of fixed -0.15%.
    The hostile-block philosophy: only block when tape is CLEARLY hostile
    (move exceeds the 75th percentile normal range for this time of day).

    Args:
        strategy_name: Strategy identifier (e.g. "FL_ANTICHOP")
        spy_snap: MarketSnapshot with pct_from_open, above_vwap, ema9_above_ema20
        bar_time_hhmm: Optional time as integer (e.g. 1015).  If None, uses
                       the old fixed threshold as fallback.

    Returns:
        (passed: bool, reason: str)
    """
    gate_type = STRATEGY_REGIME_GATE.get(strategy_name)
    if gate_type is None:
        return True, "no_gate"

    if spy_snap is None:
        return False, "no_spy_data"

    # Handle both MarketSnapshot (has .ready) and bare snapshot objects
    if hasattr(spy_snap, 'ready') and not spy_snap.ready:
        return False, "no_spy_data"

    pct = spy_snap.pct_from_open
    if math.isnan(pct):
        return False, "spy_pct_nan"

    above_vwap = spy_snap.above_vwap
    ema9_gt_ema20 = spy_snap.ema9_above_ema20

    # Time-normalized threshold (or fixed fallback)
    thr = hostile_threshold(bar_time_hhmm) if bar_time_hhmm is not None else 0.15

    if gate_type == "GREEN":
        # Block longs only when clearly bearish:
        # below VWAP AND SPY down more than the time-normalized threshold.
        if not above_vwap and pct < -thr:
            return False, (f"regime_bearish_blocks_long"
                           f"(SPY={pct:+.2f}%,thr={thr:.2f}%,below_vwap)")
        return True, "GREEN"

    elif gate_type == "RED/FLAT":
        # Allow shorts only when clearly bearish:
        # below VWAP AND SPY down more than the time-normalized threshold.
        if above_vwap:
            return False, f"regime_SPY_above_VWAP(pct={pct:+.2f}%)"
        if pct > -thr:
            return False, f"regime_SPY_not_bearish(pct={pct:+.2f}%>-{thr:.2f}%)"
        return True, "RED/FLAT"

    elif gate_type == "FAILED_LONG":
        # Same logic as GREEN: block when clearly bearish.
        if not above_vwap and pct < -thr:
            return False, f"regime_bearish_blocks_failed_long(pct={pct:+.2f}%,thr={thr:.2f}%)"
        return True, "FAILED_LONG_OK"

    elif gate_type == "FAILED_SHORT":
        # Block failed-shorts when clearly bullish (symmetric mirror of FAILED_LONG):
        # above VWAP AND SPY up more than the time-normalized threshold.
        if above_vwap and pct > thr:
            return False, (f"regime_bullish_blocks_failed_short"
                           f"(SPY={pct:+.2f}%,thr={thr:.2f}%,above_vwap)")
        return True, "FAILED_SHORT_OK"

    return True, "unknown_gate"
