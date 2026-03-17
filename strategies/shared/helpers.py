"""
Shared helper functions for strategy framework.
Trigger bar quality, volume patterns, structure location, RS, trade simulation.
"""

import math
from collections import defaultdict, deque
from datetime import date, datetime
from typing import Dict, List, Optional

from ...models import Bar, NaN

_isnan = math.isnan


def trigger_bar_quality(bar: Bar, atr: float, vol_ma: float) -> float:
    """
    Score 0.0-1.0: composite of body ratio, range vs ATR, volume vs average.
    """
    if atr <= 0 or _isnan(atr):
        return 0.0

    score = 0.0
    rng = bar.high - bar.low

    # Body ratio (0-0.4)
    body = abs(bar.close - bar.open)
    body_pct = body / rng if rng > 0 else 0.0
    score += min(body_pct, 1.0) * 0.4

    # Range vs ATR (0-0.3)
    range_atr = rng / atr
    if range_atr >= 1.0:
        score += 0.3
    elif range_atr >= 0.7:
        score += 0.2
    elif range_atr >= 0.5:
        score += 0.1

    # Volume vs average (0-0.3)
    if not _isnan(vol_ma) and vol_ma > 0:
        vol_ratio = bar.volume / vol_ma
        if vol_ratio >= 1.5:
            score += 0.3
        elif vol_ratio >= 1.0:
            score += 0.2
        elif vol_ratio >= 0.7:
            score += 0.1

    return min(score, 1.0)


def is_expansion_bar(bar: Bar, atr: float, mult: float = 0.7) -> bool:
    """Bar range > mult * ATR."""
    if atr <= 0 or _isnan(atr):
        return False
    rng = bar.high - bar.low
    return rng > mult * atr


def bar_body_ratio(bar: Bar) -> float:
    """abs(close-open) / (high-low). 1.0 = full body, 0.0 = doji."""
    rng = bar.high - bar.low
    if rng <= 0:
        return 0.0
    return abs(bar.close - bar.open) / rng


def compute_rs_from_open(stock_close: float, stock_open: float,
                          ref_close: float, ref_open: float) -> float:
    """Relative strength: stock pct_from_open - ref pct_from_open."""
    if stock_open <= 0 or ref_open <= 0:
        return 0.0
    stock_pct = (stock_close - stock_open) / stock_open
    ref_pct = (ref_close - ref_open) / ref_open
    return stock_pct - ref_pct


def bars_since_open(bar: Bar) -> int:
    """Number of bars since 9:30 open (5-min: bar 0 = 9:30, bar 1 = 9:35, etc.)."""
    h = bar.timestamp.hour
    m = bar.timestamp.minute
    minutes_since = (h - 9) * 60 + (m - 30)
    if minutes_since < 0:
        return 0
    return minutes_since // 5  # assumes 5-min bars; caller adjusts for 1-min


def is_in_time_window(bar: Bar, start_hhmm: int, end_hhmm: int) -> bool:
    """Check bar timestamp within time window (e.g., 1000-1400)."""
    hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute
    return start_hhmm <= hhmm <= end_hhmm


def get_hhmm(bar: Bar) -> int:
    """Get HHMM integer from bar timestamp."""
    return bar.timestamp.hour * 100 + bar.timestamp.minute


def compute_daily_atr(bars: List[Bar], period: int = 14) -> Dict[date, float]:
    """
    Compute per-day ATR from daily high-low-close ranges.
    Returns {date: atr}. Uses prior N days only (no look-ahead).
    First `period` days will have partial ATR (simple average of available days).
    """
    # Group bars by date to get daily OHLC
    day_data: Dict[date, list] = defaultdict(list)
    for b in bars:
        day_data[b.timestamp.date()].append(b)

    sorted_days = sorted(day_data.keys())
    daily_ranges = []  # (date, true_range)
    prev_close = NaN

    for d in sorted_days:
        day_bars = day_data[d]
        d_high = max(b.high for b in day_bars)
        d_low = min(b.low for b in day_bars)
        d_close = day_bars[-1].close

        if _isnan(prev_close):
            tr = d_high - d_low
        else:
            tr = max(d_high - d_low,
                     abs(d_high - prev_close),
                     abs(d_low - prev_close))
        daily_ranges.append((d, tr))
        prev_close = d_close

    # Compute rolling ATR
    result: Dict[date, float] = {}
    if not daily_ranges:
        return result

    # EMA-style ATR
    atr_val = NaN
    for i, (d, tr) in enumerate(daily_ranges):
        if i == 0:
            atr_val = tr
        elif i < period:
            # Simple average for warmup
            atr_val = sum(x[1] for x in daily_ranges[:i + 1]) / (i + 1)
        else:
            atr_val = (atr_val * (period - 1) + tr) / period
        result[d] = atr_val

    return result


def compute_structural_target_long(
    entry: float, risk: float, candidates: List[tuple],
    min_rr: float = 1.0, max_rr: float = 3.0,
    fallback_rr: float = 2.0, mode: str = "structural",
) -> tuple:
    """
    Universal structural target computation for LONG strategies.

    candidates: list of (price, tag) where price > entry = valid long target.
    Returns: (target_price, actual_rr, target_tag, skipped)
    """
    if mode == "fixed_rr":
        return entry + risk * fallback_rr, fallback_rr, "fixed_rr", False

    viable = []
    for price, tag in candidates:
        if _isnan(price) or price <= entry:
            continue
        rr = (price - entry) / risk
        if rr >= min_rr:
            capped_rr = min(rr, max_rr)
            capped_price = entry + capped_rr * risk
            viable.append((capped_price, capped_rr, tag))

    if not viable:
        # No structural target available — signal should be skipped.
        # Return fallback values but mark skipped=True so caller can reject.
        return entry + risk * fallback_rr, fallback_rr, "no_structural_target", True

    # Pick nearest viable (smallest R:R)
    viable.sort(key=lambda x: x[1])
    tp, rr, tag = viable[0]
    return tp, rr, tag, False


def compute_structural_target_short(
    entry: float, risk: float, candidates: List[tuple],
    min_rr: float = 1.0, max_rr: float = 3.0,
    fallback_rr: float = 2.0, mode: str = "structural",
) -> tuple:
    """
    Universal structural target computation for SHORT strategies.

    candidates: list of (price, tag) where price < entry = valid short target.
    Returns: (target_price, actual_rr, target_tag, skipped)
    """
    if mode == "fixed_rr":
        return entry - risk * fallback_rr, fallback_rr, "fixed_rr", False

    viable = []
    for price, tag in candidates:
        if _isnan(price) or price >= entry:
            continue
        rr = (entry - price) / risk
        if rr >= min_rr:
            capped_rr = min(rr, max_rr)
            capped_price = entry - capped_rr * risk
            viable.append((capped_price, capped_rr, tag))

    if not viable:
        # No structural target available — signal should be skipped.
        return entry - risk * fallback_rr, fallback_rr, "no_structural_target", True

    viable.sort(key=lambda x: x[1])
    tp, rr, tag = viable[0]
    return tp, rr, tag, False


def simulate_strategy_trade(signal, bars: List[Bar], bar_idx: int,
                             max_bars: int = 78, target_rr: float = 3.0,
                             trail_ema9: bool = False,
                             ema9_values: Optional[List[float]] = None,
                             slip_per_side_bps: float = 8.0):
    """
    Simulate trade from entry bar forward. Returns updated StrategyTrade.

    Exit priority: EOD > stop > trail > target.
    signal: StrategySignal with entry_price, stop_price, target_price, direction.

    Transaction cost model (applied as R-multiple friction):
      slip_per_side_bps: slippage+commission per side in basis points (default 8).
        Round-trip cost = 2 × slip_per_side_bps.
        Converted to R-fraction: cost_rr = (entry_price × 2 × bps/10000) / risk.
        Applied as a flat deduction from every trade's P&L.
    """
    from .signal_schema import StrategyTrade

    trade = StrategyTrade(signal=signal)
    risk = signal.risk
    if risk <= 0:
        trade.pnl_rr = 0.0
        trade.exit_reason = "invalid"
        return trade

    entry = signal.entry_price
    stop = signal.stop_price
    direction = signal.direction
    target = signal.target_price
    trail_active = False

    # ── Transaction cost as R-multiple ──
    # Round-trip cost in dollars: entry_price × 2 × bps / 10000
    # Convert to R: cost_dollars / risk
    if slip_per_side_bps > 0 and entry > 0:
        cost_dollars = entry * 2.0 * slip_per_side_bps / 10000.0
        cost_rr = cost_dollars / risk
    else:
        cost_rr = 0.0

    for i in range(bar_idx + 1, min(bar_idx + max_bars + 1, len(bars))):
        b = bars[i]
        trade.bars_held += 1
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute

        # EOD exit
        if hhmm >= 1555 or i == len(bars) - 1:
            pnl = (b.close - entry) * direction
            trade.pnl_rr = pnl / risk - cost_rr
            trade.exit_reason = "eod"
            trade.exit_price = b.close
            trade.exit_time = b.timestamp
            return trade

        # Stop check (long: low <= stop)
        if direction == 1 and b.low <= stop:
            trade.pnl_rr = (stop - entry) / risk - cost_rr
            trade.exit_reason = "stop"
            trade.exit_price = stop
            trade.exit_time = b.timestamp
            return trade
        elif direction == -1 and b.high >= stop:
            trade.pnl_rr = (entry - stop) / risk - cost_rr
            trade.exit_reason = "stop"
            trade.exit_price = stop
            trade.exit_time = b.timestamp
            return trade

        # Trail EMA9 (after +1R, exit on close below EMA9)
        if trail_ema9 and ema9_values and i < len(ema9_values):
            current_pnl = (b.close - entry) * direction / risk
            if current_pnl >= 1.0:
                trail_active = True
            if trail_active and not _isnan(ema9_values[i]):
                if direction == 1 and b.close < ema9_values[i]:
                    pnl = (b.close - entry) / risk
                    trade.pnl_rr = pnl - cost_rr
                    trade.exit_reason = "trail"
                    trade.exit_price = b.close
                    trade.exit_time = b.timestamp
                    return trade

        # Target check
        if not _isnan(target):
            if direction == 1 and b.high >= target:
                trade.pnl_rr = target_rr - cost_rr
                trade.exit_reason = "target"
                trade.exit_price = target
                trade.exit_time = b.timestamp
                return trade
            elif direction == -1 and b.low <= target:
                trade.pnl_rr = target_rr - cost_rr
                trade.exit_reason = "target"
                trade.exit_price = target
                trade.exit_time = b.timestamp
                return trade

    # Fell through max_bars — exit at actual bar close, not 0.0R
    last_idx = min(bar_idx + max_bars, len(bars) - 1)
    last_bar = bars[last_idx]
    pnl = (last_bar.close - entry) * direction
    trade.exit_reason = "eod"
    trade.pnl_rr = pnl / risk - cost_rr
    trade.exit_price = last_bar.close
    trade.exit_time = last_bar.timestamp
    return trade
