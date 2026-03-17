"""
Shared level helpers for the failed-move strategy family.
Used by ORH_FailedBreakout_Short and ORL_FailedBreakdown_Reclaim_Long.
Minimal, focused helpers — no bloated abstractions.
"""

import math
from typing import List
from ...models import Bar, NaN

_isnan = math.isnan


def is_near_level(price: float, level: float, atr: float, tol: float = 0.20) -> bool:
    """Check if price is within tol * ATR of a level."""
    if _isnan(price) or _isnan(level) or _isnan(atr) or atr <= 0:
        return False
    return abs(price - level) <= tol * atr


def bars_above_level(bars: List[Bar], level: float) -> int:
    """Count how many bars close above a level."""
    return sum(1 for b in bars if b.close > level)


def bars_below_level(bars: List[Bar], level: float) -> int:
    """Count how many bars close below a level."""
    return sum(1 for b in bars if b.close < level)


def breakout_quality(bar: Bar, level: float, atr: float, vol_ma: float,
                     direction: int) -> float:
    """
    Score 0.0-1.0 for how convincing a breakout/breakdown bar is.
    direction: +1 = breakout above, -1 = breakdown below.
    Measures: distance past level, body ratio, volume, close position.
    """
    if _isnan(atr) or atr <= 0:
        return 0.0

    rng = bar.high - bar.low
    if rng <= 0:
        return 0.0

    score = 0.0

    # Distance past level (0-0.30)
    if direction == 1:
        dist = (bar.close - level) / atr
    else:
        dist = (level - bar.close) / atr
    if dist >= 0.30:
        score += 0.30
    elif dist >= 0.15:
        score += 0.20
    elif dist >= 0.05:
        score += 0.10

    # Body ratio (0-0.25)
    body = abs(bar.close - bar.open)
    body_pct = body / rng
    score += min(body_pct, 1.0) * 0.25

    # Volume (0-0.25)
    if not _isnan(vol_ma) and vol_ma > 0:
        vol_ratio = bar.volume / vol_ma
        if vol_ratio >= 1.5:
            score += 0.25
        elif vol_ratio >= 1.0:
            score += 0.15
        elif vol_ratio >= 0.7:
            score += 0.05

    # Close position relative to direction (0-0.20)
    if direction == 1:
        close_pct = (bar.close - bar.low) / rng  # higher = better for longs
    else:
        close_pct = (bar.high - bar.close) / rng  # higher = better for shorts
    score += close_pct * 0.20

    return min(score, 1.0)


def acceptance_bars(bars_after_break: List[Bar], level: float,
                    direction: int) -> int:
    """
    Count consecutive bars that stay on the 'accepted' side of a level.
    direction: +1 = above level (breakout acceptance), -1 = below level (breakdown acceptance).
    Stops counting on first bar that crosses back.
    """
    count = 0
    for b in bars_after_break:
        if direction == 1 and b.close > level:
            count += 1
        elif direction == -1 and b.close < level:
            count += 1
        else:
            break
    return count
