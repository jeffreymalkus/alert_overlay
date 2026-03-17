"""
HH_QUALITY — Live incremental version (SMB-faithful v2).

HitchHiker Program Quality: Opening drive → consolidation → breakout.
Long only. Strong opening drive → tight consolidation near highs → breakout.

Key fidelity features:
  - Entry on breakout trigger price (consol_high + $0.01), not bar.close
  - Multi-bar drive required (>= 2 bars, no single bar > 60% of drive)
  - Consolidation in upper 1/3 of day range
  - Failed attempt filter (max 1 upper-bound probe)
  - Prior-bar volume acceleration (1.3x)
  - Breakout deadline ~10:05 (opening drive trade)

State machine phases:
  IDLE → DRIVE_CONFIRMED → CONSOL_ACTIVE → SIGNAL (or EXPIRE)
"""

import math
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


class HitchHikerLive(LiveStrategy):
    """Incremental HH_QUALITY for live pipeline (SMB-faithful v2)."""

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True):
        super().__init__(name="HH_QUALITY", direction=1, enabled=enabled,
                         skip_rejections=["distance", "maturity"])
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality

        self._time_start = cfg.get(cfg.hh_time_start)
        self._time_end = cfg.get(cfg.hh_time_end)
        self._consol_min = cfg.get(cfg.hh_consol_min_bars)
        self._consol_max = cfg.get(cfg.hh_consol_max_bars)

        self._init_state()

    def _init_state(self):
        self._drive_confirmed: bool = False
        self._drive_high: float = NaN
        self._drive_bar_count: int = 0
        self._drive_max_single_bar: float = 0.0
        self._drive_total_dist: float = 0.0
        self._consol_active: bool = False
        self._consol_high: float = NaN
        self._consol_low: float = NaN
        self._consol_bars: int = 0
        self._consol_total_vol: float = 0.0
        self._consol_total_wick: float = 0.0
        self._consol_attempt_count: int = 0
        self._triggered: bool = False
        self._prev_bar_vol: float = 0.0
        self._prev_bar_high: float = NaN

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
            self._prev_bar_vol = bar.volume
            self._prev_bar_high = bar.high
            return None

        # ── Phase 1: Opening drive detection ──
        if not self._drive_confirmed and not self._consol_active:
            if not _isnan(snap.session_open) and i_atr > 0:
                # Track drive progression
                if self._drive_bar_count == 0:
                    bar_contribution = bar.high - bar.open
                else:
                    bar_contribution = max(0, bar.high - self._prev_bar_high) if not _isnan(self._prev_bar_high) else 0
                self._drive_bar_count += 1
                if bar_contribution > self._drive_max_single_bar:
                    self._drive_max_single_bar = bar_contribution

                drive_dist = bar.high - snap.session_open
                if drive_dist >= cfg.hh_drive_min_atr * i_atr:
                    self._drive_total_dist = drive_dist

                    # Drive must be multi-bar (>= 2 bars)
                    if self._drive_bar_count < 2:
                        self._prev_bar_vol = bar.volume
                        self._prev_bar_high = bar.high
                        return None

                    # No single bar > 60% of total drive distance
                    if self._drive_total_dist > 0 and self._drive_max_single_bar / self._drive_total_dist > 0.60:
                        self._prev_bar_vol = bar.volume
                        self._prev_bar_high = bar.high
                        return None

                    self._drive_confirmed = True
                    self._drive_high = bar.high
            self._prev_bar_vol = bar.volume
            self._prev_bar_high = bar.high
            return None

        # ── Phase 2 start: transition to consolidation ──
        if self._drive_confirmed and not self._consol_active:
            if bar.close < self._drive_high:
                self._consol_active = True
                self._consol_high = bar.high
                self._consol_low = bar.low
                self._consol_bars = 1
                self._consol_total_vol = bar.volume
                bar_range = bar.high - bar.low
                wick = (bar_range - abs(bar.close - bar.open)) / bar_range if bar_range > 0 else 0
                self._consol_total_wick = wick
                self._consol_attempt_count = 0
            else:
                self._drive_high = max(self._drive_high, bar.high)
            self._prev_bar_vol = bar.volume
            self._prev_bar_high = bar.high
            return None

        # ── Phase 2/3: consolidation active ──
        if self._consol_active and not self._triggered:
            # Check breakout BEFORE updating bounds
            breakout_fired = False

            if self._consol_bars >= self._consol_min and self._time_start <= hhmm <= self._time_end:
                consol_avg_vol = self._consol_total_vol / self._consol_bars if self._consol_bars > 0 else 0
                avg_wick = self._consol_total_wick / self._consol_bars if self._consol_bars > 0 else 0

                wick_ok = avg_wick <= cfg.hh_max_wick_pct

                day_range = snap.session_high - snap.session_low if not _isnan(snap.session_high) else 0
                pos_ok = True
                if day_range > 0:
                    threshold = snap.session_low + day_range * cfg.hh_consol_upper_pct
                    if self._consol_low < threshold:
                        pos_ok = False

                # Failed attempt filter
                attempt_ok = self._consol_attempt_count <= 1

                if (bar.high > self._consol_high and wick_ok and pos_ok and attempt_ok and
                        not _isnan(vol_ma) and vol_ma > 0):
                    # Volume vs consolidation average
                    vol_vs_consol_ok = consol_avg_vol > 0 and bar.volume >= cfg.hh_break_vol_frac * consol_avg_vol

                    # Prior-bar volume acceleration (30%)
                    vol_vs_prev_ok = True
                    if self._prev_bar_vol > 0:
                        vol_vs_prev_ok = bar.volume >= 1.30 * self._prev_bar_vol

                    if vol_vs_consol_ok and vol_vs_prev_ok:
                        breakout_fired = True

            if breakout_fired:
                # Entry on breakout trigger price
                trigger_price = self._consol_high + 0.01
                entry_price = max(trigger_price, bar.open)

                stop = self._consol_low - cfg.hh_stop_buffer
                risk = entry_price - stop
                if risk <= 0:
                    self._consol_active = False
                    self._drive_confirmed = False
                    self._prev_bar_vol = bar.volume
                    self._prev_bar_high = bar.high
                    return None

                min_stop_dist = 0.15 * i_atr
                min_stop_rule_applied = False
                if risk < min_stop_dist:
                    stop = entry_price - min_stop_dist
                    risk = min_stop_dist
                    min_stop_rule_applied = True

                _stop_meta = {
                    "stop_ref_type": "consol_low",
                    "stop_ref_price": self._consol_low,
                    "raw_stop": self._consol_low - cfg.hh_stop_buffer,
                    "buffer_type": "dollar",
                    "buffer_value": cfg.hh_stop_buffer,
                    "min_stop_rule_applied": min_stop_rule_applied,
                    "min_stop_distance": min_stop_dist,
                    "final_stop": stop,
                }

                # Structural target
                if cfg.hh_target_mode == "structural":
                    _candidates = []
                    consol_range = self._consol_high - self._consol_low
                    mm_box = self._consol_high + consol_range
                    if mm_box > entry_price:
                        _candidates.append((mm_box, "measured_move_box"))
                    if not _isnan(snap.session_open) and not _isnan(self._drive_high):
                        drive_dist = self._drive_high - snap.session_open
                        mm_drive = self._consol_high + drive_dist
                        if mm_drive > entry_price:
                            _candidates.append((mm_drive, "measured_move_drive"))
                    if not _isnan(self._drive_high) and self._drive_high > entry_price:
                        _candidates.append((self._drive_high, "drive_high"))
                    if not _isnan(snap.session_high) and snap.session_high > entry_price:
                        _candidates.append((snap.session_high, "session_high"))
                    from ..shared.helpers import compute_structural_target_long
                    target, actual_rr, target_tag, skipped = compute_structural_target_long(
                        entry_price, risk, _candidates,
                        min_rr=cfg.hh_struct_min_rr, max_rr=cfg.hh_struct_max_rr,
                        fallback_rr=cfg.hh_target_rr, mode="structural",
                    )
                    if skipped:
                        self._consol_active = False
                        self._drive_confirmed = False
                        self._prev_bar_vol = bar.volume
                        self._prev_bar_high = bar.high
                        return None
                else:
                    target = entry_price + risk * cfg.hh_target_rr
                    actual_rr = cfg.hh_target_rr
                    target_tag = "fixed_rr"

                drive_dist = self._drive_high - snap.session_open if not _isnan(snap.session_open) else 0.0
                consol_avg_vol_calc = self._consol_total_vol / self._consol_bars if self._consol_bars > 0 else 0
                avg_wick_calc = self._consol_total_wick / self._consol_bars if self._consol_bars > 0 else 1.0

                struct_q = 0.30
                if consol_avg_vol_calc > 0 and bar.volume >= 1.5 * consol_avg_vol_calc:
                    struct_q += 0.15
                if avg_wick_calc <= 0.40:
                    struct_q += 0.15
                if not _isnan(vw) and entry_price > vw:
                    struct_q += 0.10
                if not _isnan(e9) and entry_price > e9:
                    struct_q += 0.10
                if self._consol_bars <= 2:
                    struct_q += 0.10
                if self._consol_attempt_count == 0:
                    struct_q += 0.10

                confluence = []
                if not _isnan(vw) and entry_price > vw:
                    confluence.append("above_vwap")
                if not _isnan(e9) and entry_price > e9:
                    confluence.append("above_ema9")
                if i_atr > 0 and drive_dist >= 1.5 * i_atr:
                    confluence.append("strong_drive")
                consol_range = self._consol_high - self._consol_low
                if i_atr > 0 and consol_range <= 1.0 * i_atr:
                    confluence.append("tight_consol")
                if consol_avg_vol_calc > 0 and bar.volume >= 1.5 * consol_avg_vol_calc:
                    confluence.append("strong_bo_vol")
                if self._drive_bar_count >= 3:
                    confluence.append("multi_bar_drive")

                # ── Quality pipeline ──
                quality_score = 3  # default
                quality_tier = QualityTier.B_TIER
                reject_reasons = []

                if self.rejection and self.quality:
                    reject_reasons = self.rejection.check_all(
                        bar, snap.recent_bars, len(snap.recent_bars) - 1, i_atr, e9, vw, vol_ma,
                        skip_filters=self.skip_rejections
                    )

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
                self._prev_bar_vol = bar.volume
                self._prev_bar_high = bar.high
                return RawSignal(
                    strategy_name="HH_QUALITY",
                    direction=1,
                    entry_price=entry_price,
                    stop_price=stop,
                    target_price=target,
                    bar_idx=snap.bar_idx,
                    hhmm=hhmm,
                    quality=quality_score,
                    metadata={
                        "drive_high": self._drive_high,
                        "drive_dist": drive_dist,
                        "drive_bar_count": self._drive_bar_count,
                        "consol_high": self._consol_high,
                        "consol_low": self._consol_low,
                        "consol_bars": self._consol_bars,
                        "consol_avg_vol": consol_avg_vol_calc,
                        "consol_attempt_count": self._consol_attempt_count,
                        "avg_wick": avg_wick_calc,
                        "actual_rr": actual_rr,
                        "target_tag": target_tag,
                        "structure_quality": min(struct_q, 1.0),
                        "confluence": confluence,
                        "in_play_score": snap.in_play_score,
                        "quality_tier": quality_tier.value,
                        "reject_reasons": reject_reasons,
                        "entry_type": "breakout_trigger",
                        **_stop_meta,
                    },
                )

            # No breakout — update consolidation bounds
            # Track failed upper-bound probes
            if not _isnan(self._consol_high) and i_atr > 0:
                probe_threshold = self._consol_high - 0.03 * i_atr
                if bar.high >= probe_threshold and bar.close < self._consol_high:
                    self._consol_attempt_count += 1

            new_high = max(self._consol_high, bar.high)
            new_low = min(self._consol_low, bar.low)
            consol_range = new_high - new_low

            if i_atr > 0 and consol_range > cfg.hh_consol_max_range_atr * i_atr:
                self._consol_active = False
                self._drive_confirmed = False
                self._prev_bar_vol = bar.volume
                self._prev_bar_high = bar.high
                return None

            if self._consol_bars >= self._consol_max:
                self._consol_active = False
                self._drive_confirmed = False
                self._prev_bar_vol = bar.volume
                self._prev_bar_high = bar.high
                return None

            self._consol_high = new_high
            self._consol_low = new_low
            self._consol_bars += 1
            self._consol_total_vol += bar.volume
            bar_range = bar.high - bar.low
            wick = (bar_range - abs(bar.close - bar.open)) / bar_range if bar_range > 0 else 0
            self._consol_total_wick += wick

        self._prev_bar_vol = bar.volume
        self._prev_bar_high = bar.high
        return None
