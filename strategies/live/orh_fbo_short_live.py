"""
ORH_FBO_SHORT — Live incremental version.

Opening Range High Failed Breakout Short: breakout → failure → retest from below → rejection.
SHORT only. ORH-only for v1.

State machine phases:
  IDLE → BROKE_OUT → FAILED → RETESTED → SIGNAL (or EXPIRE)
"""

import math
from typing import Optional

from ...models import Bar, NaN
from ..shared.config import StrategyConfig
from ..shared.level_helpers import breakout_quality
from ..shared.helpers import bar_body_ratio
from .shared_indicators import IndicatorSnapshot
from .base import LiveStrategy, RawSignal

_isnan = math.isnan

# State constants
IDLE, BROKE_OUT, FAILED, RETESTED = 0, 1, 2, 3


class ORHFBOShortLive(LiveStrategy):
    """Incremental ORH_FBO_SHORT for live pipeline."""

    def __init__(self, cfg: StrategyConfig, enabled: bool = True):
        super().__init__(name="ORH_FBO_SHORT", direction=-1, enabled=enabled)
        self.cfg = cfg

        self._time_start = cfg.get(cfg.orh_time_start)
        self._time_end = cfg.get(cfg.orh_time_end)
        self._failure_window = cfg.get(cfg.orh_failure_window)
        self._retest_window = cfg.get(cfg.orh_retest_window)

        self._init_state()

    def _init_state(self):
        self._phase: int = IDLE
        self._bo_bar_vol: float = 0.0
        self._bo_bar_close: float = NaN
        self._bo_quality: float = 0.0
        self._bars_since_bo: int = 0
        self._bars_since_fail: int = 0
        self._retest_high: float = NaN
        self._retest_low: float = NaN
        self._retest_vol: float = 0.0
        self._bars_since_retest: int = 0
        self._triggered_today: bool = False

    def reset_day(self):
        self._init_state()

    def step(self, snap: IndicatorSnapshot,
             market_ctx=None) -> Optional[RawSignal]:
        cfg = self.cfg
        hhmm = snap.hhmm
        bar = snap.bar
        e9 = snap.ema9
        vw = snap.vwap
        i_atr = snap.atr
        vol_ma = snap.vol_ma20

        if self._triggered_today:
            return None
        if not snap.ema9_ready or _isnan(i_atr) or i_atr <= 0:
            return None
        if _isnan(vol_ma) or vol_ma <= 0:
            return None
        if not snap.or_ready or _isnan(snap.or_high):
            return None

        or_high = snap.or_high
        rng = bar.high - bar.low

        # Tick state counters
        if self._phase == BROKE_OUT:
            self._bars_since_bo += 1
            if self._bars_since_bo > self._failure_window:
                self._phase = IDLE
                return None
        elif self._phase == FAILED:
            self._bars_since_fail += 1
            if self._bars_since_fail > self._retest_window:
                self._phase = IDLE
                return None
        elif self._phase == RETESTED:
            self._bars_since_retest += 1
            if self._bars_since_retest > 3:
                self._phase = IDLE
                return None

        # ── Phase 1: Breakout ──
        if self._phase == IDLE and self._time_start <= hhmm <= self._time_end:
            dist_above = bar.close - or_high
            if dist_above < cfg.orh_break_min_dist_atr * i_atr:
                return None
            if bar_body_ratio(bar) < cfg.orh_break_body_min:
                return None
            if bar.volume < cfg.orh_break_vol_frac * vol_ma:
                return None
            if bar.close <= bar.open:
                return None

            self._phase = BROKE_OUT
            self._bo_bar_vol = bar.volume
            self._bo_bar_close = bar.close
            self._bo_quality = breakout_quality(bar, or_high, i_atr, vol_ma, 1)
            self._bars_since_bo = 0
            return None

        # ── Phase 2: Failure ──
        if self._phase == BROKE_OUT:
            if bar.close < or_high:
                self._phase = FAILED
                self._bars_since_fail = 0
                self._retest_high = NaN
                self._retest_low = NaN
                self._retest_vol = 0.0
            return None

        # ── Phase 3: Retest from below ──
        if self._phase == FAILED:
            proximity = cfg.orh_retest_proximity_atr * i_atr
            max_reclaim = cfg.orh_retest_max_reclaim_atr * i_atr

            approaches = bar.high >= or_high - proximity
            fails_reclaim = bar.close < or_high
            not_blasted = bar.high <= or_high + max_reclaim

            if approaches and fails_reclaim and not_blasted:
                self._phase = RETESTED
                self._retest_high = bar.high
                self._retest_low = bar.low
                self._retest_vol = bar.volume
                self._bars_since_retest = 0
            return None

        # ── Phase 4: Rejection confirmation ──
        if self._phase == RETESTED:
            if bar.high > self._retest_high:
                self._retest_high = bar.high
            self._retest_vol += bar.volume

            bearish = bar.close < bar.open
            upper_wick = bar.high - max(bar.close, bar.open)
            wick_pct = upper_wick / rng if rng > 0 else 0.0
            body_ratio = abs(bar.close - bar.open) / rng if rng > 0 else 0.0

            if (bearish and
                    body_ratio >= cfg.orh_confirm_body_min and
                    wick_pct >= cfg.orh_confirm_wick_min):

                stop = self._retest_high + cfg.orh_stop_buffer_atr * i_atr
                min_stop = cfg.orh_min_stop_atr * i_atr
                risk = stop - bar.close
                if risk < min_stop:
                    stop = bar.close + min_stop
                    risk = min_stop
                if risk <= 0:
                    self._phase = IDLE
                    return None

                vwap_dist = bar.close - vw if not _isnan(vw) else 0.0
                if vwap_dist >= cfg.orh_min_vwap_dist_atr * i_atr and not _isnan(vw):
                    target = vw
                    actual_rr = vwap_dist / risk
                else:
                    target = bar.close - risk * cfg.orh_target_rr
                    actual_rr = cfg.orh_target_rr

                struct_q = 0.50
                if self._bo_quality >= 0.60:
                    struct_q += 0.15
                if self._bars_since_bo <= 2:
                    struct_q += 0.10
                retest_avg_vol = self._retest_vol / max(self._bars_since_retest + 1, 1)
                if retest_avg_vol < self._bo_bar_vol:
                    struct_q += 0.10
                if bar.close < e9:
                    struct_q += 0.10
                if wick_pct >= 0.40:
                    struct_q += 0.05

                confluence = ["orh_failed_bo"]
                if bar.close < vw:
                    confluence.append("below_vwap")
                if bar.close < e9:
                    confluence.append("below_ema9")
                if wick_pct >= 0.40:
                    confluence.append("strong_wick")

                self._phase = IDLE
                self._triggered_today = True

                return RawSignal(
                    strategy_name="ORH_FBO_SHORT",
                    direction=-1,
                    entry_price=bar.close,
                    stop_price=stop,
                    target_price=target,
                    bar_idx=snap.bar_idx,
                    hhmm=hhmm,
                    quality=3,
                    metadata={
                        "or_high": or_high,
                        "bo_quality": self._bo_quality,
                        "bo_bar_vol_ratio": self._bo_bar_vol / vol_ma if vol_ma > 0 else 0,
                        "bars_to_failure": self._bars_since_bo,
                        "retest_high": self._retest_high,
                        "wick_pct": wick_pct,
                        "actual_rr": actual_rr,
                        "structure_quality": min(struct_q, 1.0),
                        "confluence": confluence,
                        "in_play_score": snap.in_play_score,
                    },
                )

        return None
