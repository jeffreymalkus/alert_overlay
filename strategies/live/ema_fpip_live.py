"""
EMA_FPIP — Live incremental version.

EMA First Pullback In Play: Expansion → pullback to E9 → trigger.
Long only. 3-phase state machine.

State machine phases:
  IDLE → EXP_ACTIVE → QUAL_EXPANSION → PB_STARTED → SIGNAL (or EXPIRE)
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


class EmaFpipLive(LiveStrategy):
    """Incremental EMA_FPIP for live pipeline."""

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True,
                 strategy_name: str = "EMA_FPIP"):
        super().__init__(name=strategy_name, direction=1, enabled=enabled,
                         skip_rejections=["maturity", "bigger_picture", "distance"])
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality

        self._time_start = cfg.get(cfg.fpip_time_start)
        self._time_end = cfg.get(cfg.fpip_time_end)
        self._max_pb_bars = cfg.get(cfg.fpip_max_pullback_bars)

        self._init_state()

    def _init_state(self):
        # Expansion state
        self._exp_active: bool = False
        self._exp_bars: int = 0
        self._exp_high: float = NaN
        self._exp_low: float = NaN
        self._exp_total_vol: float = 0.0
        self._exp_overlap_count: int = 0
        self._exp_distance: float = 0.0
        self._exp_avg_vol: float = 0.0
        self._qual_expansion: bool = False

        # Pullback state
        self._pb_started: bool = False
        self._pb_bars: int = 0
        self._pb_low: float = NaN
        self._pb_total_vol: float = 0.0
        self._pb_heavy_bars: int = 0

        self._prev_bar_high: float = NaN
        self._prev_bar_low: float = NaN
        self._prev_bar_open: float = NaN
        self._prev_bar_close: float = NaN
        self._prev_e20: float = NaN
        self._triggered: bool = False

    def reset_day(self):
        self._init_state()

    def step(self, snap: IndicatorSnapshot,
             market_ctx=None) -> Optional[RawSignal]:
        cfg = self.cfg
        hhmm = snap.hhmm
        bar = snap.bar
        e9 = snap.ema9
        e20 = snap.ema20
        vw = snap.vwap
        i_atr = snap.atr
        vol_ma = snap.vol_ma20

        if not snap.ema9_ready or not snap.ema20_ready:
            self._store_prev(bar, e20)
            return None
        if _isnan(i_atr) or i_atr <= 0 or _isnan(vol_ma) or vol_ma <= 0:
            self._store_prev(bar, e20)
            return None
        if self._triggered:
            self._store_prev(bar, e20)
            return None

        in_window = self._time_start <= hhmm <= self._time_end
        signal = None

        # ══ Phase 1: EXPANSION TRACKING ══
        if not self._exp_active and not self._qual_expansion:
            is_impulse = (bar.close > bar.open and
                          (bar.close - e9) > 0.10 * i_atr and
                          e9 > e20)
            if is_impulse:
                self._exp_active = True
                self._exp_bars = 1
                self._exp_high = bar.high
                self._exp_low = bar.low
                self._exp_total_vol = bar.volume
                self._exp_overlap_count = 0
                self._exp_distance = bar.high - bar.low

        elif self._exp_active:
            if bar.close > e9 and e9 > e20 and self._exp_bars < cfg.fpip_expansion_max_bars:
                self._exp_bars += 1
                self._exp_high = max(self._exp_high, bar.high)
                self._exp_low = min(self._exp_low, bar.low)  # FIX: track true expansion low
                self._exp_total_vol += bar.volume
                self._exp_distance = self._exp_high - self._exp_low

                # Check body overlap with prev bar
                if not _isnan(self._prev_bar_close):
                    prev_body_top = max(self._prev_bar_close, self._prev_bar_open)
                    prev_body_bot = min(self._prev_bar_close, self._prev_bar_open)
                    cur_body_top = max(bar.close, bar.open)
                    cur_body_bot = min(bar.close, bar.open)
                    overlap = max(0, min(prev_body_top, cur_body_top) -
                                  max(prev_body_bot, cur_body_bot))
                    cur_body = abs(bar.close - bar.open)
                    if cur_body > 0 and overlap / cur_body > 0.5:
                        self._exp_overlap_count += 1
            else:
                # Expansion ended
                self._exp_active = False
                self._exp_avg_vol = self._exp_total_vol / self._exp_bars if self._exp_bars > 0 else 0
                overlap_ratio = self._exp_overlap_count / max(self._exp_bars - 1, 1)

                if (self._exp_bars >= cfg.fpip_expansion_min_bars and
                        self._exp_distance >= cfg.fpip_min_expansion_atr * i_atr and
                        self._exp_avg_vol >= cfg.fpip_min_expansion_avg_vol * vol_ma and
                        overlap_ratio <= cfg.fpip_max_impulse_overlap):
                    self._qual_expansion = True
                    self._pb_started = False
                    self._pb_bars = 0
                    self._pb_low = NaN
                    self._pb_total_vol = 0.0
                    self._pb_heavy_bars = 0

        # ══ Phase 2: PULLBACK TRACKING ══
        if self._qual_expansion and not self._pb_started:
            near_e9 = bar.low <= e9 + 0.15 * i_atr
            holds_e9 = bar.close > e9 * 0.97
            if near_e9 and holds_e9:
                self._pb_started = True
                self._pb_bars = 1
                self._pb_low = bar.low
                self._pb_total_vol = bar.volume
                self._pb_heavy_bars = 1 if bar.volume >= self._exp_avg_vol else 0

        elif self._qual_expansion and self._pb_started:
            pb_depth = self._exp_high - self._pb_low if not _isnan(self._pb_low) else 0
            max_depth = cfg.fpip_max_pullback_depth * self._exp_distance

            # Volume decline check: PB avg vol should be lighter than expansion
            pb_avg_vol_check = self._pb_total_vol / self._pb_bars if self._pb_bars > 0 else 0
            vol_declining = (pb_avg_vol_check <= cfg.fpip_max_pullback_vol_ratio * self._exp_avg_vol
                             if self._exp_avg_vol > 0 else True)

            if (pb_depth > max_depth or
                    self._pb_bars > self._max_pb_bars or
                    e9 < e20 or
                    not vol_declining):
                self._qual_expansion = False
                self._pb_started = False
                self._store_prev(bar, e20)
                return None

            # ══ Phase 3: TRIGGER CHECK ══
            if in_window:
                reclaims = bar.close > e9 and bar.close > bar.open
                prev_dipped = (not _isnan(self._prev_bar_low) and
                               self._prev_bar_low <= e9 + 0.15 * i_atr)

                # V3: require close above prior bar high
                prev_high_reclaimed = True
                if cfg.fpip_trigger_require_close_above_prev_high:
                    prev_high_reclaimed = (not _isnan(self._prev_bar_high) and
                                           bar.close > self._prev_bar_high)

                if reclaims and prev_dipped and prev_high_reclaimed:
                    rng = bar.high - bar.low
                    close_pct = (bar.close - bar.low) / rng if rng > 0 else 0.0
                    body_pct = abs(bar.close - bar.open) / rng if rng > 0 else 0.0

                    pb_avg_vol = self._pb_total_vol / self._pb_bars if self._pb_bars > 0 else vol_ma
                    vol_expansion = (bar.volume >= cfg.fpip_trigger_vol_vs_pb * pb_avg_vol
                                     if pb_avg_vol > 0 else True)

                    if (close_pct >= cfg.fpip_min_trigger_close_pct and
                            body_pct >= cfg.fpip_min_trigger_body_pct and
                            vol_expansion):

                        # V3: entry on prior-bar-high break
                        if cfg.fpip_entry_mode == "prev_high_break":
                            if _isnan(self._prev_bar_high):
                                self._store_prev(bar, e20)
                                return None
                            entry_price = max(self._prev_bar_high + cfg.fpip_entry_buffer, bar.open)
                        else:
                            entry_price = bar.close

                        stop = self._pb_low - cfg.fpip_stop_buffer
                        min_stop = 0.20 * i_atr
                        min_stop_rule_applied = False
                        if abs(entry_price - stop) < min_stop:
                            stop = entry_price - min_stop
                            min_stop_rule_applied = True

                        risk = entry_price - stop
                        if risk > 0:
                            _stop_meta = {
                                "stop_ref_type": "pb_low",
                                "stop_ref_price": self._pb_low,
                                "raw_stop": self._pb_low - cfg.fpip_stop_buffer,
                                "buffer_type": "dollar",
                                "buffer_value": cfg.fpip_stop_buffer,
                                "min_stop_rule_applied": min_stop_rule_applied,
                                "min_stop_distance": min_stop,
                                "final_stop": stop,
                            }
                            # Structural target: expansion high, session high
                            if cfg.fpip_target_mode == "structural":
                                _candidates = []
                                if not _isnan(self._exp_high) and self._exp_high > entry_price:
                                    _candidates.append((self._exp_high, "exp_high"))
                                if not _isnan(snap.session_high) and snap.session_high > entry_price:
                                    _candidates.append((snap.session_high, "session_high"))
                                if not _isnan(snap.prior_day_high) and snap.prior_day_high > entry_price:
                                    _candidates.append((snap.prior_day_high, "pdh"))
                                from ..shared.helpers import compute_structural_target_long
                                target, actual_rr, target_tag, skipped = compute_structural_target_long(
                                    entry_price, risk, _candidates,
                                    min_rr=cfg.fpip_struct_min_rr, max_rr=cfg.fpip_struct_max_rr,
                                    fallback_rr=cfg.fpip_target_rr, mode="structural",
                                )
                                if skipped:
                                    self._store_prev(bar, e20)
                                    return None
                            else:
                                target = entry_price + risk * cfg.fpip_target_rr
                                actual_rr = cfg.fpip_target_rr
                                target_tag = "fixed_rr"

                            struct_q = 0.50
                            pb_depth_calc = self._exp_high - self._pb_low if not _isnan(self._pb_low) else 0
                            if pb_depth_calc < 0.25 * self._exp_distance:
                                struct_q += 0.15
                            if bar.volume >= 1.2 * self._exp_avg_vol:
                                struct_q += 0.15
                            if pb_avg_vol < 0.60 * self._exp_avg_vol:
                                struct_q += 0.10
                            if not _isnan(self._prev_e20) and e20 > self._prev_e20:
                                struct_q += 0.10

                            confluence = []
                            if bar.close > vw:
                                confluence.append("above_vwap")
                            if bar.close > e9:
                                confluence.append("above_ema9")
                            if e9 > e20:
                                confluence.append("ema_aligned")
                            if self._exp_avg_vol >= 1.5 * vol_ma:
                                confluence.append("strong_exp_vol")

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

                            signal = RawSignal(
                                strategy_name=self.name,
                                direction=1,
                                entry_price=entry_price,
                                stop_price=stop,
                                target_price=target,
                                bar_idx=snap.bar_idx,
                                hhmm=hhmm,
                                quality=quality_score,
                                metadata={
                                    "exp_bars": self._exp_bars,
                                    "exp_distance_atr": self._exp_distance / i_atr,
                                    "pb_bars": self._pb_bars,
                                    "pb_depth_pct": pb_depth_calc / self._exp_distance if self._exp_distance > 0 else 0,
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
                            self._qual_expansion = False
                            self._pb_started = False
                            self._triggered = True

            # Update pullback tracking (if not triggered)
            if self._pb_started and not self._triggered:
                self._pb_bars += 1
                if bar.low < self._pb_low or _isnan(self._pb_low):
                    self._pb_low = bar.low
                self._pb_total_vol += bar.volume
                if bar.volume >= self._exp_avg_vol:
                    self._pb_heavy_bars += 1
                if self._pb_heavy_bars > cfg.fpip_max_heavy_pb_bars:
                    self._qual_expansion = False
                    self._pb_started = False

        self._store_prev(bar, e20)
        return signal

    def _store_prev(self, bar: 'Bar', e20: float):
        self._prev_bar_high = bar.high
        self._prev_bar_low = bar.low
        self._prev_bar_open = bar.open
        self._prev_bar_close = bar.close
        self._prev_e20 = e20
