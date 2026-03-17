"""
Universal rejection filters — 5 filters that prune weak signals before quality scoring.
Each filter returns (passed: bool, reason: str).
"""

import math
from typing import List, Tuple

from ...models import Bar, NaN

_isnan = math.isnan


class RejectionFilters:
    """Five universal rejection filters for the strategy pipeline."""

    def __init__(self, cfg):
        self.cfg = cfg

    def check_all(self, bar: Bar, bars: List[Bar], bar_idx: int,
                  atr: float, ema9: float, vwap: float,
                  vol_ma: float,
                  skip_filters: List[str] = None) -> List[str]:
        """
        Run all 5 filters. Return list of reject reasons (empty = all passed).
        skip_filters: list of filter names to skip (e.g. ["distance"]).
        """
        skip = set(skip_filters or [])
        reasons = []
        checks = [
            ("choppiness", self.choppiness),
            ("maturity", self.maturity),
            ("distance", self.distance),
            ("bigger_picture", self.bigger_picture),
            ("trigger_weakness", self.trigger_weakness),
        ]
        for name, check in checks:
            if name in skip:
                continue
            passed, reason = check(bar, bars, bar_idx, atr, ema9, vwap, vol_ma)
            if not passed:
                reasons.append(reason)
        return reasons

    def choppiness(self, bar: Bar, bars: List[Bar], bar_idx: int,
                   atr: float, ema9: float, vwap: float,
                   vol_ma: float) -> Tuple[bool, str]:
        """
        Reject if recent N bars have heavily overlapping ranges (no trend).
        Overlap = sum of bar-to-bar overlap / sum of ranges.
        """
        lookback = self.cfg.get(self.cfg.rej_chop_lookback)
        max_overlap = self.cfg.rej_chop_overlap_max

        if bar_idx < lookback + 1 or atr <= 0:
            return True, ""

        start = max(0, bar_idx - lookback)
        window = bars[start:bar_idx + 1]

        if len(window) < 3:
            return True, ""

        total_range = 0.0
        total_overlap = 0.0
        for j in range(1, len(window)):
            prev_b = window[j - 1]
            curr_b = window[j]
            prev_range = prev_b.high - prev_b.low
            curr_range = curr_b.high - curr_b.low
            total_range += prev_range + curr_range

            # Overlap between consecutive bars
            overlap_high = min(prev_b.high, curr_b.high)
            overlap_low = max(prev_b.low, curr_b.low)
            overlap = max(0, overlap_high - overlap_low)
            total_overlap += overlap

        if total_range <= 0:
            return True, ""

        overlap_ratio = total_overlap / (total_range / 2.0)  # normalize

        if overlap_ratio > max_overlap:
            return False, f"chop({overlap_ratio:.2f}>{max_overlap:.2f})"

        return True, ""

    def maturity(self, bar: Bar, bars: List[Bar], bar_idx: int,
                 atr: float, ema9: float, vwap: float,
                 vol_ma: float) -> Tuple[bool, str]:
        """
        Reject if the intraday move is too extended (too many bars since last
        meaningful pullback). Late entries in exhausted moves.
        """
        max_bars = self.cfg.get(self.cfg.rej_maturity_bars)

        if bar_idx < 3:
            return True, ""

        # Count bars since last pullback (bar that closed red / below prior low)
        bars_since_pullback = 0
        for j in range(bar_idx, max(0, bar_idx - max_bars * 2) - 1, -1):
            b = bars[j]
            if b.close < b.open:  # red bar = pullback
                break
            bars_since_pullback += 1

        if bars_since_pullback > max_bars:
            return False, f"mature({bars_since_pullback}bars)"

        return True, ""

    def distance(self, bar: Bar, bars: List[Bar], bar_idx: int,
                 atr: float, ema9: float, vwap: float,
                 vol_ma: float) -> Tuple[bool, str]:
        """
        Reject if price is too far from VWAP (extended, no room to run).
        """
        max_dist = self.cfg.rej_distance_atr_max

        if atr <= 0 or _isnan(atr) or _isnan(vwap) or vwap <= 0:
            return True, ""

        dist_atr = abs(bar.close - vwap) / atr

        if dist_atr > max_dist:
            return False, f"dist({dist_atr:.1f}ATR>{max_dist:.1f})"

        return True, ""

    def bigger_picture(self, bar: Bar, bars: List[Bar], bar_idx: int,
                       atr: float, ema9: float, vwap: float,
                       vol_ma: float) -> Tuple[bool, str]:
        """
        Reject if bigger-picture structure conflicts with long entry.
        For longs: reject if price is below session VWAP AND below EMA9
        (both structural bearish signals at once).
        """
        if not self.cfg.rej_bigger_picture_enabled:
            return True, ""

        if _isnan(vwap) or _isnan(ema9):
            return True, ""

        # For longs: below both VWAP and EMA9 is structurally weak
        below_vwap = bar.close < vwap
        below_ema9 = bar.close < ema9

        if below_vwap and below_ema9:
            return False, "bigger_picture(below_vwap+ema9)"

        return True, ""

    def trigger_weakness(self, bar: Bar, bars: List[Bar], bar_idx: int,
                         atr: float, ema9: float, vwap: float,
                         vol_ma: float) -> Tuple[bool, str]:
        """
        Reject if trigger bar has weak body ratio or very low volume.
        """
        min_body = self.cfg.rej_trigger_body_min_pct
        min_vol_frac = self.cfg.rej_trigger_vol_min_frac

        rng = bar.high - bar.low
        if rng > 0:
            body_pct = abs(bar.close - bar.open) / rng
            if body_pct < min_body:
                return False, f"trigger_weak_body({body_pct:.2f}<{min_body:.2f})"

        if not _isnan(vol_ma) and vol_ma > 0:
            vol_ratio = bar.volume / vol_ma
            if vol_ratio < min_vol_frac:
                return False, f"trigger_low_vol({vol_ratio:.2f}<{min_vol_frac:.2f})"

        return True, ""
