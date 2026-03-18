"""
BDR_SHORT — Live incremental version.

Breakdown-Retest Short: Breakdown → retest → rejection wick confirmation.
SHORT only. Uses RED+TREND regime gate. AM-only entries.

State machine phases:
  IDLE → BD_ACTIVE → RETESTED → SIGNAL (or EXPIRE)
"""

import math
from collections import deque
from statistics import median
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


class BDRShortLive(LiveStrategy):
    """Incremental BDR_SHORT for live pipeline."""

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True,
                 strategy_name: str = "BDR_SHORT"):
        super().__init__(name=strategy_name, direction=-1, enabled=enabled,
                         skip_rejections=["bigger_picture", "distance"])
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality

        self._v3 = cfg.bdr_v3_enabled
        self._time_start = cfg.bdr_v3_time_start if self._v3 else cfg.get(cfg.bdr_time_start)
        self._time_end = cfg.bdr_v3_time_end if self._v3 else cfg.get(cfg.bdr_time_end)
        self._retest_window = cfg.get(cfg.bdr_retest_window)
        self._confirm_window = cfg.get(cfg.bdr_confirm_window)

        self._init_state()

    def _init_state(self):
        self._bd_active: bool = False
        self._bd_level: float = NaN
        self._bd_level_tag: str = ""
        self._bd_bar_vol: float = 0.0
        self._bars_since_bd: int = 999
        self._retested: bool = False
        self._bars_since_retest: int = 999
        self._retest_bar_high: float = NaN
        self._retest_bar_low: float = NaN
        self._retest_vol: float = 0.0
        self._triggered_today: bool = False
        self._range_buf: deque = deque(maxlen=10)
        # V3 additional state
        self._v3_retest_found: bool = False
        self._v3_retest_close: float = NaN
        self._v3_waiting_trigger: bool = False
        self._v3_trigger_countdown: int = 0

    def reset_day(self):
        self._init_state()

    def step(self, snap: IndicatorSnapshot,
             market_ctx=None) -> Optional[RawSignal]:
        if self._v3:
            return self._step_v3(snap, market_ctx)
        return self._step_legacy(snap, market_ctx)

    def _step_v3(self, snap: IndicatorSnapshot,
                 market_ctx=None) -> Optional[RawSignal]:
        """V3: breakdown → weak retest → retest-low-break entry."""
        cfg = self.cfg
        hhmm = snap.hhmm
        bar = snap.bar
        e9 = snap.ema9
        vw = snap.vwap
        i_atr = snap.atr
        vol_ma = snap.vol_ma20
        rng = bar.high - bar.low
        self._range_buf.append(rng)

        if self._triggered_today:
            return None
        if not snap.ema9_ready or _isnan(i_atr) or i_atr <= 0:
            return None
        if _isnan(vol_ma) or vol_ma <= 0:
            return None

        # Regime gate
        if cfg.bdr_require_red_trend and market_ctx is not None:
            spy_label = getattr(market_ctx, 'day_label', None)
            if spy_label and spy_label != "RED":
                return None
            spy_close_pct = getattr(market_ctx, 'spy_close_pct_of_range', None)
            if spy_close_pct is not None and spy_close_pct > cfg.bdr_spy_trend_pct:
                return None

        # ── TRIGGER PHASE ──
        if self._v3_waiting_trigger:
            self._v3_trigger_countdown -= 1
            if self._v3_trigger_countdown < 0:
                self._v3_waiting_trigger = False
                self._bd_active = False
                self._v3_retest_found = False
                return None

            trigger_price = self._retest_bar_low - cfg.bdr_entry_buffer
            if bar.low < trigger_price:
                if cfg.bdr_require_trigger_below_vwap and not (bar.close < vw):
                    return None
                if cfg.bdr_require_trigger_below_ema9 and not (bar.close < e9):
                    return None

                entry_price = min(trigger_price, bar.open)
                stop = self._retest_bar_high + cfg.bdr_v3_stop_buffer
                min_stop = cfg.bdr_min_stop_atr * i_atr
                risk = stop - entry_price
                if risk < min_stop:
                    stop = entry_price + min_stop
                    risk = min_stop
                if risk <= 0:
                    self._v3_waiting_trigger = False
                    self._bd_active = False
                    return None

                target = entry_price - risk * cfg.bdr_target_rr_v3
                actual_rr = cfg.bdr_target_rr_v3

                struct_q = 0.50
                if self._bd_level_tag == "ORL":
                    struct_q += 0.15
                if self._bd_bar_vol >= 1.25 * vol_ma:
                    struct_q += 0.10
                if self._retest_vol < self._bd_bar_vol:
                    struct_q += 0.10
                if bar.close < vw and bar.close < e9:
                    struct_q += 0.15

                confluence = []
                if bar.close < vw:
                    confluence.append("below_vwap")
                if bar.close < e9:
                    confluence.append("below_ema9")
                if self._bd_level_tag == "ORL":
                    confluence.append("or_level")
                if self._bd_bar_vol >= 1.5 * vol_ma:
                    confluence.append("strong_bd_vol")

                _stop_meta = {
                    "stop_ref_type": "retest_high",
                    "stop_ref_price": self._retest_bar_high,
                    "raw_stop": self._retest_bar_high + cfg.bdr_v3_stop_buffer,
                    "buffer_type": "dollar",
                    "buffer_value": cfg.bdr_v3_stop_buffer,
                    "min_stop_rule_applied": risk == cfg.bdr_min_stop_atr * i_atr,
                    "min_stop_distance": cfg.bdr_min_stop_atr * i_atr,
                    "final_stop": stop,
                }

                # ── Quality pipeline (same as legacy BDR) ──
                quality_score = 3
                quality_tier = QualityTier.B_TIER
                reject_reasons = []

                if self.rejection and self.quality:
                    # Build skip list — V3 skips trigger body/vol checks
                    v3_skips = list(self.skip_rejections)
                    if cfg.bdr_skip_generic_trigger_body_filter:
                        v3_skips.append("trigger_weakness")
                    if cfg.bdr_skip_generic_trigger_vol_filter:
                        v3_skips.append("trigger_low_vol")

                    reject_reasons = self.rejection.check_all(
                        bar, snap.recent_bars, len(snap.recent_bars) - 1,
                        i_atr, e9, vw, vol_ma,
                        skip_filters=v3_skips
                    )

                    # Inverted scoring for shorts
                    stock_factors = {
                        "in_play_score": snap.in_play_score,
                        "rs_market": -snap.rs_market,
                        "rs_sector": 0.0,
                        "volume_profile": min(bar.volume / vol_ma, 1.0) if vol_ma > 0 else 0.0,
                    }
                    market_factors = {
                        "regime_score": 1.0 - snap.regime_score,
                        "alignment_score": 1.0 - snap.alignment_score,
                    }
                    setup_factors = {
                        "trigger_quality": trigger_bar_quality(bar, i_atr, vol_ma),
                        "structure_quality": min(struct_q, 1.0),
                        "confluence_count": len(confluence),
                    }

                    if not reject_reasons:
                        quality_tier, quality_score = self.quality.score(
                            stock_factors, market_factors, setup_factors
                        )
                    else:
                        _, quality_score = self.quality.score(
                            stock_factors, market_factors, setup_factors
                        )
                        quality_tier = QualityTier.B_TIER if quality_score >= self.cfg.quality_b_min else QualityTier.C_TIER

                self._v3_waiting_trigger = False
                self._bd_active = False
                self._triggered_today = True

                return RawSignal(
                    strategy_name=self.name,
                    direction=-1,
                    entry_price=entry_price,
                    stop_price=stop,
                    target_price=target,
                    bar_idx=snap.bar_idx,
                    hhmm=hhmm,
                    quality=quality_score,
                    metadata={
                        "level_type": self._bd_level_tag,
                        "level_price": self._bd_level,
                        "entry_mode": cfg.bdr_entry_mode,
                        "setup_mode": cfg.bdr_setup_mode,
                        "actual_rr": actual_rr,
                        "target_tag": "fixed_rr",
                        "structure_quality": min(struct_q, 1.0),
                        "confluence": confluence,
                        "in_play_score": snap.in_play_score,
                        "quality_tier": quality_tier.value,
                        "reject_reasons": reject_reasons,
                        "entry_type": "retest_low_break",
                        **_stop_meta,
                    },
                )
            return None

        # ── RETEST PHASE ──
        if self._bd_active and not self._v3_retest_found:
            proximity = cfg.bdr_retest_proximity_atr * i_atr
            approaches = bar.high >= self._bd_level - proximity
            reclaim_above = max(0.0, bar.high - self._bd_level)
            reclaim_atr = reclaim_above / max(i_atr, 1e-6)

            if approaches and reclaim_atr <= cfg.bdr_max_reclaim_above_level_atr:
                bar_range = max(bar.high - bar.low, 1e-6)
                close_pos = (bar.close - bar.low) / bar_range
                body_pct = abs(bar.close - bar.open) / bar_range
                upper_wick_pct = (bar.high - max(bar.open, bar.close)) / bar_range

                weak_enough = close_pos <= cfg.bdr_retest_close_max_pos
                if cfg.bdr_setup_mode == "failed_reclaim_break":
                    weak_enough = (weak_enough and
                                   body_pct <= cfg.bdr_retest_body_max_pct and
                                   upper_wick_pct >= cfg.bdr_retest_min_upper_wick_pct)
                if cfg.bdr_require_retest_below_vwap and not (bar.close < vw):
                    weak_enough = False
                if cfg.bdr_require_retest_below_ema9 and not (bar.close < e9):
                    weak_enough = False
                if cfg.bdr_require_retest_vol_not_stronger_than_breakdown:
                    if bar.volume > self._bd_bar_vol:
                        weak_enough = False

                if weak_enough:
                    self._v3_retest_found = True
                    self._retest_bar_high = bar.high
                    self._retest_bar_low = bar.low
                    self._v3_retest_close = bar.close
                    self._retest_vol = bar.volume
                    self._v3_waiting_trigger = True
                    self._v3_trigger_countdown = cfg.bdr_trigger_bars_after_retest
            return None

        # ── BREAKDOWN PHASE ──
        if not self._bd_active and self._time_start <= hhmm <= self._time_end:
            level, tag = self._find_support_level(snap)
            if _isnan(level):
                return None

            broke = bar.close < level - cfg.bdr_break_atr_min * i_atr
            bearish = bar.close < bar.open
            if not (broke and bearish):
                return None

            if len(self._range_buf) >= 10:
                med_rng = median(list(self._range_buf))
                if rng < cfg.bdr_break_bar_range_frac * med_rng:
                    return None
            if bar.volume < cfg.bdr_break_vol_frac * vol_ma:
                return None
            if rng > 0:
                close_pct = (bar.high - bar.close) / rng
                if close_pct < cfg.bdr_break_close_pct:
                    return None

            self._bd_active = True
            self._bd_level = level
            self._bd_level_tag = tag
            self._bd_bar_vol = bar.volume
            self._v3_retest_found = False
            self._v3_waiting_trigger = False

        return None

    def _step_legacy(self, snap: IndicatorSnapshot,
                     market_ctx=None) -> Optional[RawSignal]:
        """Legacy BDR path (unchanged)."""
        cfg = self.cfg
        hhmm = snap.hhmm
        bar = snap.bar
        e9 = snap.ema9
        vw = snap.vwap
        i_atr = snap.atr
        vol_ma = snap.vol_ma20

        rng = bar.high - bar.low
        self._range_buf.append(rng)

        if self._triggered_today:
            return None
        if not snap.ema9_ready or _isnan(i_atr) or i_atr <= 0:
            return None
        if _isnan(vol_ma) or vol_ma <= 0:
            return None

        # RED+TREND regime gate: SPY must be red and close in bottom 25% of day range
        if cfg.bdr_require_red_trend and market_ctx is not None:
            spy_label = getattr(market_ctx, 'day_label', None)
            if spy_label and spy_label != "RED":
                return None
            spy_close_pct = getattr(market_ctx, 'spy_close_pct_of_range', None)
            if spy_close_pct is not None and spy_close_pct > cfg.bdr_spy_trend_pct:
                return None

        # Tick state counters
        if self._bd_active:
            self._bars_since_bd += 1
            if self._bars_since_bd > self._retest_window + self._confirm_window + 2:
                self._bd_active = False
                return None

        if self._retested:
            self._bars_since_retest += 1

        # ── Phase 1: Breakdown detection ──
        if not self._bd_active and self._time_start <= hhmm <= self._time_end:
            level, tag = self._find_support_level(snap)
            if _isnan(level):
                return None

            broke = bar.close < level - cfg.bdr_break_atr_min * i_atr
            bearish = bar.close < bar.open
            if not (broke and bearish):
                return None

            if len(self._range_buf) >= 10:
                med_rng = median(list(self._range_buf))
                if rng < cfg.bdr_break_bar_range_frac * med_rng:
                    return None

            if bar.volume < cfg.bdr_break_vol_frac * vol_ma:
                return None

            if rng > 0:
                close_pct = (bar.high - bar.close) / rng
                if close_pct < cfg.bdr_break_close_pct:
                    return None

            self._bd_active = True
            self._bd_level = level
            self._bd_level_tag = tag
            self._bd_bar_vol = bar.volume
            self._bars_since_bd = 0
            self._retested = False
            self._bars_since_retest = 999
            self._retest_bar_high = NaN
            self._retest_bar_low = NaN
            self._retest_vol = 0.0
            return None

        # Time gate for active states (allow continuation outside window)
        if not self._bd_active:
            return None

        # ── Phase 2: Retest detection ──
        if self._bd_active and not self._retested and self._bars_since_bd <= self._retest_window:
            proximity = cfg.bdr_retest_proximity_atr * i_atr
            max_reclaim = cfg.bdr_retest_max_reclaim_atr * i_atr

            approaches = bar.high >= self._bd_level - proximity
            fails_reclaim = bar.close < self._bd_level
            not_blasted = bar.high <= self._bd_level + max_reclaim

            if approaches and fails_reclaim and not_blasted:
                self._retested = True
                self._retest_bar_high = bar.high
                self._retest_bar_low = bar.low
                self._retest_vol = bar.volume
                self._bars_since_retest = 0
                return None

        # ── Phase 3: Rejection confirmation ──
        # AM-only enforcement at signal time (spec: entries before 11:00 AM)
        if self._retested and self._bars_since_retest <= self._confirm_window and hhmm <= self._time_end:
            if bar.high > self._retest_bar_high:
                self._retest_bar_high = bar.high
            self._retest_vol += bar.volume

            bearish = bar.close < bar.open
            closes_below = bar.close < self._retest_bar_low
            upper_wick = bar.high - max(bar.close, bar.open)
            wick_pct = upper_wick / rng if rng > 0 else 0.0

            if (bearish and closes_below and
                    wick_pct >= cfg.bdr_min_rejection_wick_pct):

                stop = self._retest_bar_high + cfg.bdr_stop_buffer_atr * i_atr
                min_stop = cfg.bdr_min_stop_atr * i_atr
                min_stop_rule_applied = False
                risk = stop - bar.close
                if risk < min_stop:
                    stop = bar.close + min_stop
                    risk = min_stop
                    min_stop_rule_applied = True
                if risk <= 0:
                    self._bd_active = False
                    self._retested = False
                    return None

                _stop_meta = {
                    "stop_ref_type": "retest_bar_high",
                    "stop_ref_price": self._retest_bar_high,
                    "raw_stop": self._retest_bar_high + cfg.bdr_stop_buffer_atr * i_atr,
                    "buffer_type": "atr",
                    "buffer_value": cfg.bdr_stop_buffer_atr,
                    "min_stop_rule_applied": min_stop_rule_applied,
                    "min_stop_distance": min_stop,
                    "final_stop": stop,
                }

                # ── Structural target computation ──
                target, actual_rr, target_tag = self._compute_structural_target(
                    bar.close, risk, snap.session_low, snap.prior_day_low,
                    self._bd_level, cfg,
                )
                if _isnan(target):
                    # No viable structural target — skip signal
                    self._bd_active = False
                    self._retested = False
                    return None

                struct_q = 0.50
                if self._bd_level_tag == "ORL":
                    struct_q += 0.15
                if self._bd_bar_vol >= 1.25 * vol_ma:
                    struct_q += 0.10
                retest_avg_vol = self._retest_vol / max(self._bars_since_retest, 1)
                if retest_avg_vol < self._bd_bar_vol:
                    struct_q += 0.10
                if bar.close < vw and bar.close < e9:
                    struct_q += 0.15

                confluence = []
                if bar.close < vw:
                    confluence.append("below_vwap")
                if bar.close < e9:
                    confluence.append("below_ema9")
                if self._bd_level_tag == "ORL":
                    confluence.append("or_level")
                if wick_pct >= 0.40:
                    confluence.append("strong_wick")
                if self._bd_bar_vol >= 1.5 * vol_ma:
                    confluence.append("strong_bd_vol")

                # ── Quality pipeline (with inverted scoring for shorts) ──
                quality_score = 3  # default
                quality_tier = QualityTier.B_TIER
                reject_reasons = []

                if self.rejection and self.quality:
                    # Rejection filters
                    reject_reasons = self.rejection.check_all(
                        bar, snap.recent_bars, len(snap.recent_bars) - 1, i_atr, e9, vw, vol_ma,
                        skip_filters=self.skip_rejections
                    )

                    # Quality scoring (inverted for shorts)
                    if not reject_reasons:
                        stock_factors = {
                            "in_play_score": snap.in_play_score,
                            "rs_market": -snap.rs_market,  # inverted: underperforming SPY = positive for shorts
                            "rs_sector": 0.0,
                            "volume_profile": min(bar.volume / vol_ma, 1.0) if vol_ma > 0 else 0.0,
                        }
                        # Inverted for shorts: RED regime = high score, SPY below VWAP = high alignment
                        market_factors = {
                            "regime_score": 1.0 - snap.regime_score,
                            "alignment_score": 1.0 - snap.alignment_score,
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
                            "rs_market": -snap.rs_market,  # inverted: underperforming SPY = positive for shorts
                            "rs_sector": 0.0,
                            "volume_profile": min(bar.volume / vol_ma, 1.0) if vol_ma > 0 else 0.0,
                        }
                        # Inverted for shorts: RED regime = high score, SPY below VWAP = high alignment
                        market_factors = {
                            "regime_score": 1.0 - snap.regime_score,
                            "alignment_score": 1.0 - snap.alignment_score,
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

                self._bd_active = False
                self._retested = False
                self._triggered_today = True

                return RawSignal(
                    strategy_name="BDR_SHORT",
                    direction=-1,
                    entry_price=bar.close,
                    stop_price=stop,
                    target_price=target,
                    bar_idx=snap.bar_idx,
                    hhmm=hhmm,
                    quality=quality_score,
                    metadata={
                        "bd_level": self._bd_level,
                        "bd_level_tag": self._bd_level_tag,
                        "bd_bar_vol_ratio": self._bd_bar_vol / vol_ma if vol_ma > 0 else 0,
                        "retest_bar_high": self._retest_bar_high,
                        "wick_pct": wick_pct,
                        "bars_since_bd": self._bars_since_bd,
                        "structure_quality": min(struct_q, 1.0),
                        "confluence": confluence,
                        "in_play_score": snap.in_play_score,
                        "quality_tier": quality_tier.value,
                        "reject_reasons": reject_reasons,
                        "target_tag": target_tag,
                        "actual_rr": actual_rr,
                        **_stop_meta,
                    },
                )

        return None

    @staticmethod
    def _compute_structural_target(
        entry: float, risk: float, session_low: float, pdl: float,
        bd_level: float, cfg,
    ) -> tuple:
        """
        Compute structural short target for BDR_SHORT.

        Candidates (all must be BELOW entry):
          1. Session low — lowest low from open to signal time
          2. PDL (prior day low) — key downside reference
          3. Breakdown extension — bd_level - 1.5 * (bd_level - entry)

        Selection: nearest viable candidate (>= min_rr), capped at max_rr.
        Returns: (target_price, actual_rr, target_tag)
                 target_price=NaN if no viable candidate (signal should be skipped).
        """
        if cfg.bdr_target_mode == "fixed_rr":
            target = entry - risk * cfg.bdr_target_rr
            return target, cfg.bdr_target_rr, "fixed_rr"

        min_rr = cfg.bdr_struct_min_rr
        max_rr = cfg.bdr_struct_max_rr

        candidates = []

        if not _isnan(session_low) and session_low < entry:
            candidates.append((session_low, "session_low"))

        if not _isnan(pdl) and pdl < entry:
            candidates.append((pdl, "pdl"))

        if not _isnan(bd_level) and bd_level > entry:
            ext = bd_level - 1.5 * (bd_level - entry)
            candidates.append((ext, "bd_extension"))

        if not candidates:
            # No structural target — skip the trade
            return float('nan'), 0.0, "no_structural_target"

        viable = []
        for price, tag in candidates:
            rr = (entry - price) / risk
            if rr >= min_rr:
                capped_rr = min(rr, max_rr)
                capped_price = entry - capped_rr * risk
                viable.append((capped_price, capped_rr, tag))

        if not viable:
            # All candidates too close — skip the trade
            return float('nan'), 0.0, "no_structural_target"

        viable.sort(key=lambda x: x[1])
        target_price, actual_rr, tag = viable[0]
        return target_price, actual_rr, tag

    def _find_support_level(self, snap: IndicatorSnapshot) -> tuple:
        """Find support level for breakdown.

        Filters candidates by enabled level flags (V3 disables VWAP).
        Picks highest allowed support level (most likely to break for shorts).
        """
        cfg = self.cfg
        candidates = []

        if snap.or_ready and not _isnan(snap.or_low) and cfg.bdr_use_orl_level:
            candidates.append((snap.or_low, "ORL"))

        if not _isnan(snap.vwap) and snap.vwap > 0 and cfg.bdr_use_vwap_level:
            candidates.append((snap.vwap, "VWAP"))

        # Swing low from recent bars (10-bar lookback per spec)
        if cfg.bdr_use_swing_low_level:
            recent = snap.recent_bars
            if len(recent) >= 10:
                prior_bars = recent[:-1]
                swing_low = min(b.low for b in prior_bars)
                candidates.append((swing_low, "SWING"))

        if not candidates:
            return NaN, ""

        # For shorts, pick highest support level
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0]
