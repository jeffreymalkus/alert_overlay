"""
BS_STRUCT — Live incremental version.

Backside Structure: Decline → HH/HL structure → range above E9 → breakout → VWAP.
Long only. Time window: 10:00-13:30. One-and-done.

State machine phases:
  IDLE → DECLINE_CONFIRMED → STRUCTURE_BUILDING → RANGE_ACTIVE → SIGNAL (or EXPIRE)
"""

import math
from collections import deque
from typing import Optional, List, Tuple

from ...models import Bar, NaN
from ..shared.config import StrategyConfig
from ..shared.rejection_filters import RejectionFilters
from ..shared.quality_scoring import QualityScorer
from ..shared.signal_schema import QualityTier
from ..shared.helpers import trigger_bar_quality
from .shared_indicators import IndicatorSnapshot
from .base import LiveStrategy, RawSignal

_isnan = math.isnan


class BacksideStructureLive(LiveStrategy):
    """Incremental BS_STRUCT for live pipeline."""

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True):
        super().__init__(name="BS_STRUCT", direction=1, enabled=enabled,
                         skip_rejections=["distance", "bigger_picture"])
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality

        self._time_start = cfg.get(cfg.bs_time_start)
        self._time_end = cfg.get(cfg.bs_time_end)
        self._range_min = cfg.get(cfg.bs_range_min_bars)
        self._range_max = cfg.get(cfg.bs_range_max_bars)

        self._init_state()

    def _init_state(self):
        self._lod_price: float = NaN
        self._decline_confirmed: bool = False
        self._triggered: bool = False

        # Structure tracking
        self._swing_highs: List[Tuple[int, float]] = []
        self._swing_lows: List[Tuple[int, float]] = []
        self._hh_count: int = 0
        self._hl_count: int = 0
        self._structure_confirmed: bool = False

        # Range tracking
        self._range_active: bool = False
        self._range_high: float = NaN
        self._range_low: float = NaN
        self._range_bars: int = 0
        self._range_bars_above_e9: int = 0
        self._most_recent_hl: float = NaN

        # E9 history for slope
        self._e9_history: deque = deque(maxlen=10)

        # Previous bars for swing detection
        self._prev2_bar: Optional['Bar'] = None
        self._prev1_bar: Optional['Bar'] = None

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

        if _isnan(i_atr) or i_atr <= 0 or self._triggered:
            self._shift_bars(bar)
            return None
        if not snap.ema9_ready:
            self._shift_bars(bar)
            return None
        if _isnan(vol_ma) or vol_ma <= 0:
            self._shift_bars(bar)
            return None

        self._e9_history.append(e9)

        # ── Phase 1: Decline detection ──
        if not self._decline_confirmed:
            if not _isnan(vw) and bar.close < vw:
                decline_dist = snap.session_high - bar.low
                if decline_dist >= cfg.bs_min_decline_atr * i_atr:
                    self._decline_confirmed = True
                    self._lod_price = bar.low
            self._shift_bars(bar)
            return None

        # Track LOD — if new low, reset structure
        if bar.low < self._lod_price:
            self._lod_price = bar.low
            self._swing_highs = []
            self._swing_lows = []
            self._hh_count = 0
            self._hl_count = 0
            self._structure_confirmed = False
            self._range_active = False
            self._shift_bars(bar)
            return None

        # ── Phase 2: Structure building (HH + HL) ──
        if not self._structure_confirmed:
            # Swing point detection using 3-bar pattern
            # Date boundary check: all 3 bars must be same day
            if (self._prev1_bar is not None and self._prev2_bar is not None
                    and hasattr(self._prev1_bar, 'timestamp')
                    and hasattr(self._prev2_bar, 'timestamp')
                    and self._prev1_bar.timestamp.date() == bar.timestamp.date()
                    and self._prev2_bar.timestamp.date() == bar.timestamp.date()):
                # Swing high at prev1
                if (self._prev1_bar.high > self._prev2_bar.high and
                        self._prev1_bar.high > bar.high):
                    sh = (snap.bar_idx - 1, self._prev1_bar.high)
                    if not self._swing_highs or sh[1] != self._swing_highs[-1][1]:
                        self._swing_highs.append(sh)
                        if len(self._swing_highs) >= 2:
                            if self._swing_highs[-1][1] > self._swing_highs[-2][1]:
                                self._hh_count += 1

                # Swing low at prev1
                if (self._prev1_bar.low < self._prev2_bar.low and
                        self._prev1_bar.low < bar.low):
                    sl = (snap.bar_idx - 1, self._prev1_bar.low)
                    if not self._swing_lows or sl[1] != self._swing_lows[-1][1]:
                        self._swing_lows.append(sl)
                        self._most_recent_hl = sl[1]
                        if len(self._swing_lows) >= 2:
                            if self._swing_lows[-1][1] > self._swing_lows[-2][1]:
                                self._hl_count += 1

            # Check structure confirmation
            if (self._hh_count >= cfg.bs_min_hh_count and
                    self._hl_count >= cfg.bs_min_hl_count):
                if len(self._e9_history) >= cfg.bs_ema9_rising_bars:
                    rising = all(
                        self._e9_history[j] > self._e9_history[j - 1]
                        for j in range(len(self._e9_history) - cfg.bs_ema9_rising_bars + 1,
                                       len(self._e9_history))
                    )
                    if rising and bar.close > e9:
                        self._structure_confirmed = True
                        self._range_active = True
                        self._range_high = bar.high
                        self._range_low = bar.low
                        self._range_bars = 1
                        self._range_bars_above_e9 = 1 if bar.low > e9 else 0

            self._shift_bars(bar)
            return None

        # ── Phase 3: Range tracking + breakout ──
        if self._range_active and not self._triggered:
            breakout_fired = False
            midpoint_frac = 0.0

            if self._range_bars >= self._range_min and self._time_start <= hhmm <= self._time_end:
                above_e9_pct = self._range_bars_above_e9 / self._range_bars if self._range_bars > 0 else 0

                if above_e9_pct >= cfg.bs_range_above_ema9_pct:
                    range_mid = (self._range_high + self._range_low) / 2.0
                    if not _isnan(vw) and not _isnan(self._lod_price):
                        vwap_path = vw - self._lod_price
                        if vwap_path > 0:
                            midpoint_frac = (range_mid - self._lod_price) / vwap_path

                    if midpoint_frac >= cfg.bs_range_midpoint_vwap_frac:
                        if bar.high > self._range_high and bar.close > self._range_high:
                            rng = bar.high - bar.low
                            vol_ok = bar.volume >= cfg.bs_break_vol_frac * vol_ma
                            close_pct = (bar.close - bar.low) / rng if rng > 0 else 0
                            close_ok = close_pct >= cfg.bs_break_close_pct

                            if vol_ok and close_ok:
                                breakout_fired = True

            if breakout_fired:
                if not _isnan(self._most_recent_hl):
                    stop = self._most_recent_hl - cfg.bs_stop_buffer
                    stop_ref_type = "most_recent_hl"
                    stop_ref_price = self._most_recent_hl
                else:
                    stop = self._range_low - cfg.bs_stop_buffer
                    stop_ref_type = "range_low"
                    stop_ref_price = self._range_low

                min_stop = 0.30 * i_atr  # FIX: spec says 0.30 ATR min stop
                min_stop_rule_applied = False
                if abs(bar.close - stop) < min_stop:
                    stop = bar.close - min_stop
                    min_stop_rule_applied = True

                _stop_meta = {
                    "stop_ref_type": stop_ref_type,
                    "stop_ref_price": stop_ref_price,
                    "raw_stop": stop_ref_price - cfg.bs_stop_buffer,
                    "buffer_type": "dollar",
                    "buffer_value": cfg.bs_stop_buffer,
                    "min_stop_rule_applied": min_stop_rule_applied,
                    "min_stop_distance": min_stop,
                    "final_stop": stop,
                }

                risk = bar.close - stop
                if risk <= 0:
                    self._range_active = False
                    if cfg.bs_one_and_done:
                        self._triggered = True
                    self._shift_bars(bar)
                    return None

                # Structural target: VWAP, session high
                if cfg.bs_target_mode == "structural":
                    _candidates = []
                    if not _isnan(vw) and vw > bar.close:
                        _candidates.append((vw, "vwap"))
                    if not _isnan(snap.session_high) and snap.session_high > bar.close:
                        _candidates.append((snap.session_high, "session_high"))
                    from ..shared.helpers import compute_structural_target_long
                    target, actual_rr, target_tag, skipped = compute_structural_target_long(
                        bar.close, risk, _candidates,
                        min_rr=cfg.bs_struct_min_rr, max_rr=cfg.bs_struct_max_rr,
                        fallback_rr=cfg.bs_target_rr, mode="structural",
                    )
                    if skipped:
                        self._range_active = False
                        if cfg.bs_one_and_done:
                            self._triggered = True
                        self._shift_bars(bar)
                        return None
                else:
                    target = bar.close + risk * cfg.bs_target_rr
                    actual_rr = cfg.bs_target_rr
                    target_tag = "fixed_rr"
                range_width = self._range_high - self._range_low
                above_e9_pct = self._range_bars_above_e9 / self._range_bars if self._range_bars > 0 else 0

                struct_q = 0.30
                if self._hh_count >= 2 and self._hl_count >= 2:
                    struct_q += 0.20
                elif self._hh_count >= 1 and self._hl_count >= 1:
                    struct_q += 0.10
                if i_atr > 0 and range_width <= 1.0 * i_atr:
                    struct_q += 0.15
                if bar.volume >= 1.5 * vol_ma:
                    struct_q += 0.10
                if above_e9_pct >= 0.85:
                    struct_q += 0.10
                if midpoint_frac >= 0.60:
                    struct_q += 0.10

                confluence = []
                if not _isnan(e9) and bar.close > e9:
                    confluence.append("above_ema9")
                if above_e9_pct >= 0.85:
                    confluence.append("strong_e9_structure")
                if self._hh_count >= 2:
                    confluence.append("multi_hh")
                if range_width <= 1.0 * i_atr:
                    confluence.append("tight_range")
                if bar.volume >= 1.5 * vol_ma:
                    confluence.append("strong_bo_vol")

                # ── Quality pipeline ──
                quality_score = 3  # default
                quality_tier = QualityTier.B_TIER
                reject_reasons = []

                if self.rejection and self.quality:
                    # Rejection filters
                    reject_reasons = self.rejection.check_all(
                        bar, snap.recent_bars, len(snap.recent_bars) - 1, i_atr, e9, vw, vol_ma,
                        skip_filters=self.skip_rejections
                    )

                    # Quality scoring
                    if not reject_reasons:
                        stock_factors = {
                            "in_play_score": snap.in_play_score,
                            "rs_market": snap.rs_market,
                            "rs_sector": 0.0,
                            "volume_profile": min(bar.volume / vol_ma, 1.0) if vol_ma > 0 else 0.0,
                        }
                        market_factors = {
                            "regime_score": snap.regime_score,
                            "alignment_score": snap.alignment_score,
                        }
                        setup_factors = {
                            "trigger_quality": trigger_bar_quality(bar, i_atr, vol_ma),
                            "structure_quality": min(struct_q, 1.0),
                            "confluence_count": len(confluence),
                        }
                        quality_tier, quality_score = self.quality.score(
                            stock_factors, market_factors, setup_factors
                        )
                    else:
                        # Rejected signals get B or C tier
                        stock_factors = {
                            "in_play_score": snap.in_play_score,
                            "rs_market": snap.rs_market,
                            "rs_sector": 0.0,
                            "volume_profile": min(bar.volume / vol_ma, 1.0) if vol_ma > 0 else 0.0,
                        }
                        market_factors = {
                            "regime_score": snap.regime_score,
                            "alignment_score": snap.alignment_score,
                        }
                        setup_factors = {
                            "trigger_quality": trigger_bar_quality(bar, i_atr, vol_ma),
                            "structure_quality": min(struct_q, 1.0),
                            "confluence_count": len(confluence),
                        }
                        _, quality_score = self.quality.score(
                            stock_factors, market_factors, setup_factors
                        )
                        quality_tier = QualityTier.B_TIER if quality_score >= self.cfg.quality_b_min else QualityTier.C_TIER

                self._triggered = True

                self._shift_bars(bar)
                return RawSignal(
                    strategy_name="BS_STRUCT",
                    direction=1,
                    entry_price=bar.close,
                    stop_price=stop,
                    target_price=target,
                    bar_idx=snap.bar_idx,
                    hhmm=hhmm,
                    quality=quality_score,
                    metadata={
                        "lod_price": self._lod_price,
                        "decline_atr": (snap.session_high - self._lod_price) / i_atr if i_atr > 0 else 0,
                        "hh_count": self._hh_count,
                        "hl_count": self._hl_count,
                        "range_bars": self._range_bars,
                        "range_width_atr": range_width / i_atr if i_atr > 0 else 0,
                        "above_e9_pct": above_e9_pct,
                        "midpoint_frac": midpoint_frac,
                        "actual_rr": actual_rr,
                        "target_tag": target_tag,
                        "structure_quality": min(struct_q, 1.0),
                        "confluence": confluence,
                        "in_play_score": snap.in_play_score,
                        "quality_tier": quality_tier.value,
                        "reject_reasons": reject_reasons,
                        **_stop_meta,
                    },
                )

            # No breakout — update range
            if not self._triggered:
                new_high = max(self._range_high, bar.high)
                new_low = min(self._range_low, bar.low)
                range_width = new_high - new_low

                if i_atr > 0 and range_width > cfg.bs_range_max_atr * i_atr:
                    self._range_active = False
                    if cfg.bs_one_and_done:
                        self._triggered = True
                    self._shift_bars(bar)
                    return None

                if self._range_bars >= self._range_max:
                    self._range_active = False
                    if cfg.bs_one_and_done:
                        self._triggered = True
                    self._shift_bars(bar)
                    return None

                self._range_high = new_high
                self._range_low = new_low
                self._range_bars += 1
                if bar.low > e9:
                    self._range_bars_above_e9 += 1

                # Update most recent HL from swing detection
                if self._prev1_bar is not None and self._prev2_bar is not None:
                    if (self._prev1_bar.low < self._prev2_bar.low and
                            self._prev1_bar.low < bar.low):
                        if self._prev1_bar.low > self._lod_price:
                            self._most_recent_hl = self._prev1_bar.low

        self._shift_bars(bar)
        return None

    def _shift_bars(self, bar: 'Bar'):
        """Shift previous bars for swing detection."""
        self._prev2_bar = self._prev1_bar
        self._prev1_bar = bar
