"""
FL_ANTICHOP — Live incremental version.

Decline → Turn → EMA9 crosses above VWAP with anti-chop filtering.
Long only. Meaningful decline from session high → higher-low turn → EMA9 cross.

State machine phases:
  IDLE → DECLINE_ACTIVE → TURN_DETECTED → CROSS_FIRED (SIGNAL or EXPIRE)
"""

import math
from collections import deque
from typing import Optional

from ...models import Bar, NaN
from ..shared.config import StrategyConfig
from ..shared.rejection_filters import RejectionFilters
from ..shared.quality_scoring import QualityScorer
from ..shared.signal_schema import QualityTier
from ..shared.helpers import trigger_bar_quality
from .shared_indicators import IndicatorSnapshot
from .base import LiveStrategy, RawSignal

_isnan = math.isnan


class FLAntiChopLive(LiveStrategy):
    """Incremental FL_ANTICHOP for live pipeline."""

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True):
        super().__init__(name="FL_ANTICHOP", direction=1, enabled=enabled,
                         skip_rejections=["trigger_weakness", "distance"])
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality

        self._time_start = cfg.get(cfg.fl_time_start)
        self._time_end = cfg.get(cfg.fl_time_end)
        self._turn_confirm_n = cfg.get(cfg.fl_turn_confirm_bars)
        self._max_base_bars = cfg.get(cfg.fl_max_base_bars)

        self._init_state()

    def _init_state(self):
        """Initialize/reset all private state machine fields."""
        self._session_high: float = NaN
        self._decline_active: bool = False
        self._decline_high: float = NaN
        self._decline_low: float = NaN
        self._decline_atr: float = 0.0
        self._turn_detected: bool = False
        self._turn_confirm_bars: int = 0
        self._turn_low: float = NaN
        self._base_bars: int = 0
        self._cross_fired: bool = False
        self._triggered: bool = False
        self._prior_e9: float = NaN
        self._prior_prior_e9: float = NaN
        self._prior_vwap: float = NaN
        self._rvol_buf: deque = deque(maxlen=20)

    def reset_day(self):
        """Reset for new trading day."""
        self._init_state()

    def step(self, snap: IndicatorSnapshot,
             market_ctx=None) -> Optional[RawSignal]:
        """Process one bar through FL_ANTICHOP state machine."""
        cfg = self.cfg
        hhmm = snap.hhmm
        bar = snap.bar
        e9 = snap.ema9
        vw = snap.vwap
        i_atr = snap.atr
        vol_ma = snap.vol_ma20

        # Track RVOL
        self._rvol_buf.append(bar.volume)
        rvol = bar.volume / vol_ma if (not _isnan(vol_ma) and vol_ma > 0) else NaN

        # Gate checks
        if self._triggered:
            self._update_trailing(e9, vw)
            return None
        if not snap.ema9_ready or _isnan(i_atr) or i_atr <= 0:
            self._update_trailing(e9, vw)
            return None
        if _isnan(vol_ma) or vol_ma <= 0:
            self._update_trailing(e9, vw)
            return None

        # Track session high
        if _isnan(self._session_high) or bar.high > self._session_high:
            self._session_high = bar.high

        # ── Phase 1: Decline detection ──
        if not _isnan(self._session_high):
            decline_dist = self._session_high - bar.low
            curr_decline_atr = decline_dist / i_atr if i_atr > 0 else 0.0

            if curr_decline_atr >= cfg.fl_min_decline_atr and not self._decline_active:
                self._decline_active = True
                self._decline_high = self._session_high
                self._decline_low = bar.low
                self._decline_atr = curr_decline_atr

            if self._decline_active and bar.low < self._decline_low:
                self._decline_low = bar.low
                self._decline_atr = (self._decline_high - bar.low) / i_atr

        # ── Phase 2: Turn detection ──
        if self._decline_active and not self._turn_detected:
            hl_threshold = self._decline_low + cfg.fl_hl_tolerance_atr * i_atr
            if bar.low > hl_threshold and bar.close > bar.open:
                self._turn_confirm_bars += 1
                if self._turn_confirm_bars >= self._turn_confirm_n:
                    self._turn_detected = True
                    self._turn_low = bar.low
                    self._base_bars = 0
            else:
                self._turn_confirm_bars = 0

        # Base bar counting
        if self._turn_detected and not self._cross_fired:
            self._base_bars += 1
            if self._base_bars > self._max_base_bars:
                self._turn_detected = False
                self._base_bars = 0

        # ── Phase 3: Cross detection ──
        if not _isnan(self._prior_e9) and not _isnan(self._prior_vwap):
            if self._prior_e9 <= self._prior_vwap and e9 > vw:
                self._cross_fired = True

        # ── Signal check ──
        signal = None
        if (self._cross_fired and self._turn_detected and self._decline_active and
                not self._triggered and self._time_start <= hhmm <= self._time_end):

            # Gate: cross bar close above VWAP
            if cfg.fl_cross_close_above_vwap and bar.close <= vw:
                pass
            else:
                # Gate: clean cross bar body
                bar_range = bar.high - bar.low
                body_pct = abs(bar.close - bar.open) / bar_range if bar_range > 0 else 0
                if body_pct < cfg.fl_cross_body_pct:
                    pass
                elif not _isnan(rvol) and rvol < cfg.fl_cross_vol_min_rvol:
                    pass
                else:
                    # Compute stop — selectable mode
                    _fl_stop_mode = getattr(cfg, "fl_stop_mode", "current_hybrid")
                    if _fl_stop_mode == "source_faithful":
                        # Source material: stop = 1/3 distance from VWAP to LOD
                        # i.e. VWAP - (VWAP - LOD) / 3
                        _vwap_for_stop = vw if not _isnan(vw) else bar.close
                        _lod = self._decline_low if not _isnan(self._decline_low) else bar.low
                        _stop_ref_type = "source_faithful_measured_move"
                        _stop_ref_price = _vwap_for_stop
                        _raw_stop = _vwap_for_stop - (_vwap_for_stop - _lod) / 3.0
                        _buffer_type = "measured_move_vwap_lod_third"
                        _buffer_value = (_vwap_for_stop - _lod) / 3.0
                        stop = _raw_stop
                        # No ATR floor in source-faithful mode
                        _floor_applied = False
                        min_stop = 0.0
                    else:
                        # Current hybrid: turn_low - ATR buffer, with ATR floor
                        if not _isnan(self._turn_low):
                            _stop_ref_type = "turn_low"
                            _stop_ref_price = self._turn_low
                            _raw_stop = self._turn_low - cfg.fl_stop_buffer_atr * i_atr
                            _buffer_type = "atr"
                            _buffer_value = cfg.fl_stop_buffer_atr
                            stop = _raw_stop
                        else:
                            measured_move = self._decline_high - self._decline_low
                            _stop_ref_type = "measured_move_fallback"
                            _stop_ref_price = bar.close
                            _raw_stop = bar.close - measured_move * cfg.fl_stop_frac
                            _buffer_type = "measured_move_frac"
                            _buffer_value = cfg.fl_stop_frac
                            stop = _raw_stop
                        min_stop = 0.15 * i_atr
                        _floor_applied = False
                        if bar.close - stop < min_stop:
                            _floor_applied = True
                            stop = bar.close - min_stop
                    _stop_meta = {
                        "stop_ref_type": _stop_ref_type,
                        "stop_ref_price": _stop_ref_price,
                        "raw_stop": _raw_stop,
                        "buffer_type": _buffer_type,
                        "buffer_value": _buffer_value,
                        "min_stop_rule_applied": _floor_applied,
                        "min_stop_distance": min_stop,
                        "final_stop": stop,
                        "fl_stop_mode": _fl_stop_mode,
                    }

                    if stop < bar.close and self._antichop_ok(snap, i_atr):
                        risk = bar.close - stop

                        # Structural target: VWAP (primary for mean-reversion), decline high, session high, PDH
                        if cfg.fl_target_mode == "structural":
                            _candidates = []
                            # VWAP is the natural mean-reversion target (primary per spec)
                            if not _isnan(vw) and vw > bar.close:
                                _candidates.append((vw, "vwap"))
                            if not _isnan(self._decline_high) and self._decline_high > bar.close:
                                _candidates.append((self._decline_high, "decline_high"))
                            if not _isnan(snap.session_high) and snap.session_high > bar.close:
                                _candidates.append((snap.session_high, "session_high"))
                            if not _isnan(snap.prior_day_high) and snap.prior_day_high > bar.close:
                                _candidates.append((snap.prior_day_high, "pdh"))
                            from ..shared.helpers import compute_structural_target_long
                            target, actual_rr, target_tag, skipped = compute_structural_target_long(
                                bar.close, risk, _candidates,
                                min_rr=cfg.fl_struct_min_rr, max_rr=cfg.fl_struct_max_rr,
                                fallback_rr=cfg.fl_target_rr, mode="structural",
                            )
                            if skipped:
                                self._update_trailing(e9, vw)
                                return None
                        else:
                            target = bar.close + risk * cfg.fl_target_rr
                            actual_rr = cfg.fl_target_rr
                            target_tag = "fixed_rr"

                        # Structure quality
                        struct_q = 0.5
                        if self._decline_atr > cfg.fl_min_decline_atr + 1.0:
                            struct_q += 0.15
                        if self._turn_confirm_bars >= self._turn_confirm_n + 1:
                            struct_q += 0.15
                        if bar.close > vw:
                            struct_q += 0.1
                        if not _isnan(self._prior_prior_e9) and not _isnan(self._prior_e9):
                            slope_cur = (e9 - self._prior_e9) / i_atr
                            slope_prev = (self._prior_e9 - self._prior_prior_e9) / i_atr
                            if slope_cur > slope_prev:
                                struct_q += 0.1

                        confluence = []
                        if bar.close > vw:
                            confluence.append("above_vwap")
                        if bar.close > e9:
                            confluence.append("above_ema9")
                        if self._decline_atr > 4.0:
                            confluence.append("deep_decline")
                        if not _isnan(rvol) and rvol >= 1.5:
                            confluence.append("strong_rvol")

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
                                    "volume_profile": min(rvol / 2.0, 1.0) if not _isnan(rvol) else 0.0,
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
                                    "volume_profile": min(rvol / 2.0, 1.0) if not _isnan(rvol) else 0.0,
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

                        signal = RawSignal(
                            strategy_name="FL_ANTICHOP",
                            direction=1,
                            entry_price=bar.close,
                            stop_price=stop,
                            target_price=target,
                            bar_idx=snap.bar_idx,
                            hhmm=hhmm,
                            quality=quality_score,
                            metadata={
                                "decline_atr": self._decline_atr,
                                "decline_high": self._decline_high,
                                "decline_low": self._decline_low,
                                "turn_bars": self._turn_confirm_bars,
                                "base_bars": self._base_bars,
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
                        self._triggered = True

        self._update_trailing(e9, vw)
        return signal

    def _update_trailing(self, e9: float, vw: float):
        """Update trailing EMA/VWAP values."""
        self._prior_prior_e9 = self._prior_e9
        self._prior_e9 = e9
        self._prior_vwap = vw

    def _antichop_ok(self, snap: IndicatorSnapshot, atr: float) -> bool:
        """Custom anti-chop filter — matches replay _antichop_filter().

        Stricter than universal choppiness: uses fl_chop_lookback / fl_chop_overlap_max.
        Returns True if NOT choppy (signal OK), False if choppy (reject).
        """
        cfg = self.cfg
        lookback = cfg.get(cfg.fl_chop_lookback)
        max_overlap = cfg.fl_chop_overlap_max

        bars = snap.recent_bars_5m if snap.timeframe == 5 else snap.recent_bars_1m
        if len(bars) < lookback + 1 or atr <= 0:
            return True  # not enough data → pass

        window = bars[-(lookback + 1):]
        if len(window) < 3:
            return True

        total_range = 0.0
        total_overlap = 0.0
        for j in range(1, len(window)):
            prev_b = window[j - 1]
            curr_b = window[j]
            total_range += (prev_b.high - prev_b.low) + (curr_b.high - curr_b.low)
            overlap = max(0, min(prev_b.high, curr_b.high) - max(prev_b.low, curr_b.low))
            total_overlap += overlap

        if total_range <= 0:
            return True
        overlap_ratio = total_overlap / (total_range / 2.0)
        return overlap_ratio <= max_overlap
