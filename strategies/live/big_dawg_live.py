"""
BIG_DAWG_LONG_V1 — Big Dawg Consolidation, Long Only.

Strong opening drive → midday compact consolidation (wedge/flag/pennant) → breakout.

State machine:
  IDLE → DRIVE_CONFIRMED → PATTERN_ACTIVE → TRIGGERED

5-minute native. Active 11:00-13:30 ET. One-and-done (max 1 attempt).

V1 simplifications:
  - Unified consolidation detector (no wedge vs flag vs pennant classification)
  - Prior day high as higher-timeframe resistance proxy
  - Structural target fallback (move2move_dbb deferred to V2)
  - No fresh catalyst or sector trend gating
"""

import math
from collections import deque
from typing import Optional

from ...models import Bar, NaN
from ..shared.config import StrategyConfig
from ..shared.rejection_filters import RejectionFilters
from ..shared.quality_scoring import QualityScorer
from ..shared.signal_schema import QualityTier
from ..shared.helpers import trigger_bar_quality, compute_structural_target_long
from .shared_indicators import IndicatorSnapshot
from .base import LiveStrategy, RawSignal

_isnan = math.isnan


class BigDawgLive(LiveStrategy):
    """Incremental BIG_DAWG_LONG_V1 for live pipeline. 5-min bars."""

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True,
                 strategy_name: str = "BIG_DAWG_LONG_V1"):
        super().__init__(name=strategy_name, direction=1, enabled=enabled,
                         skip_rejections=["distance", "maturity"])
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality

        self._time_start = cfg.get(cfg.bd_time_start)
        self._time_end = cfg.get(cfg.bd_time_end)
        self._pattern_min = cfg.get(cfg.bd_pattern_min_bars)
        self._pattern_max = cfg.get(cfg.bd_pattern_max_bars)

        self._init_state()

    def _init_state(self):
        self._drive_confirmed: bool = False
        self._drive_high: float = NaN
        self._pattern_active: bool = False
        self._pattern_high: float = NaN
        self._pattern_low: float = NaN
        self._pattern_bars: int = 0
        self._pattern_total_vol: float = 0.0
        self._pattern_first_bar_vol: float = 0.0
        self._triggered: bool = False

        # Session tracking
        self._bars_above_open: int = 0
        self._total_bars: int = 0
        self._pre_pattern_vol_buf: deque = deque(maxlen=20)

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

        if self._triggered:
            return None
        if _isnan(i_atr) or i_atr <= 0 or _isnan(vol_ma) or vol_ma <= 0:
            return None

        # Track bars above open for the 75% filter
        self._total_bars += 1
        if not _isnan(snap.session_open) and bar.close > snap.session_open:
            self._bars_above_open += 1

        # Track pre-pattern volume
        self._pre_pattern_vol_buf.append(bar.volume)

        # ── PHASE 1: Confirm strong prior drive ──
        if not self._drive_confirmed:
            if _isnan(snap.session_open) or snap.session_open <= 0:
                return None
            if not _isnan(snap.session_high):
                drive_pct = (snap.session_high - snap.session_open) / snap.session_open * 100.0
                if drive_pct >= cfg.bd_min_prior_drive_pct:
                    self._drive_confirmed = True
                    self._drive_high = snap.session_high
            return None

        # Update drive high if price makes new highs
        if not _isnan(snap.session_high) and snap.session_high > self._drive_high:
            self._drive_high = snap.session_high

        # Only look for patterns in the time window
        if hhmm < self._time_start:
            return None
        if hhmm > self._time_end:
            self._pattern_active = False
            return None

        # ── PHASE 2: Pattern detection ──
        if not self._pattern_active:
            # Check if this bar starts a consolidation (price pulls back from drive high)
            if bar.high < self._drive_high:
                self._pattern_active = True
                self._pattern_high = bar.high
                self._pattern_low = bar.low
                self._pattern_bars = 1
                self._pattern_total_vol = bar.volume
                self._pattern_first_bar_vol = bar.volume
            return None

        if self._pattern_active and not self._triggered:
            # ── BREAKOUT CHECK (before updating bounds) ──
            if self._pattern_bars >= self._pattern_min:
                if bar.high > self._pattern_high:
                    # Validate all conditions before triggering

                    # Location checks
                    if cfg.bd_require_above_vwap and (_isnan(vw) or bar.close <= vw):
                        self._pattern_active = False
                        return None

                    pdh = snap.prior_day_high if hasattr(snap, 'prior_day_high') else NaN
                    if cfg.bd_require_above_pdh and (_isnan(pdh) or self._pattern_low <= pdh):
                        self._pattern_active = False
                        return None

                    # Upper third of day range
                    if cfg.bd_require_upper_third:
                        day_range = snap.session_high - snap.session_low if not _isnan(snap.session_high) and not _isnan(snap.session_low) else 0
                        if day_range > 0:
                            threshold = snap.session_low + day_range * 0.67
                            if self._pattern_low < threshold:
                                self._pattern_active = False
                                return None

                    # 75% of day above open
                    if self._total_bars > 0:
                        above_frac = self._bars_above_open / self._total_bars
                        if above_frac < cfg.bd_min_day_above_open_frac:
                            self._pattern_active = False
                            return None

                    # Pattern size <= 50% of day range
                    pattern_size = self._pattern_high - self._pattern_low
                    day_range = snap.session_high - snap.session_low if not _isnan(snap.session_high) and not _isnan(snap.session_low) else 0
                    if day_range > 0 and pattern_size / day_range > cfg.bd_pattern_max_dayrange_frac:
                        self._pattern_active = False
                        return None

                    # Volume declining during pattern
                    pattern_avg_vol = self._pattern_total_vol / self._pattern_bars if self._pattern_bars > 0 else 0
                    pre_pattern_avg_vol = sum(self._pre_pattern_vol_buf) / len(self._pre_pattern_vol_buf) if len(self._pre_pattern_vol_buf) > 0 else vol_ma

                    if cfg.bd_require_declining_vol and pre_pattern_avg_vol > 0:
                        if pattern_avg_vol > cfg.bd_pattern_vol_frac_max * pre_pattern_avg_vol:
                            self._pattern_active = False
                            return None

                    # Reject crescendo: first bar of pattern shouldn't be loudest
                    if cfg.bd_reject_crescendo and self._pattern_bars >= 3:
                        if self._pattern_first_bar_vol > 0:
                            later_avg = (self._pattern_total_vol - self._pattern_first_bar_vol) / max(self._pattern_bars - 1, 1)
                            if self._pattern_first_bar_vol > 1.5 * later_avg and self._pattern_first_bar_vol > pattern_avg_vol * 1.3:
                                self._pattern_active = False
                                return None

                    # Breakout volume check
                    if pattern_avg_vol > 0 and bar.volume < cfg.bd_break_vol_frac * pattern_avg_vol:
                        # Weak breakout volume — don't trigger but keep pattern alive
                        pass
                    else:
                        # ── TRIGGER ──
                        return self._build_signal(snap, bar, i_atr, vol_ma, vw, e9, pdh,
                                                  pattern_avg_vol, pre_pattern_avg_vol, day_range)

            # ── BOUNDED PATTERN: reject bars that expand beyond initial range ──
            # A real consolidation/flag should NOT expand. If price breaks
            # outside the range established by the first 2 bars, reset.
            expansion_tolerance = 0.15 * i_atr if i_atr > 0 else 0.05  # small tolerance

            # Bar breaks above pattern high (but not enough for breakout)
            if bar.high > self._pattern_high and bar.high <= self._pattern_high + expansion_tolerance:
                pass  # minor overshoot, tolerate but don't expand
            elif bar.high > self._pattern_high:
                # Already checked as breakout above — if we're here, breakout conditions failed
                self._pattern_active = False
                return None

            # Bar breaks below pattern low — pattern is broken
            if bar.low < self._pattern_low - expansion_tolerance:
                self._pattern_active = False
                return None

            # If pattern gets too long, reset
            if self._pattern_bars >= self._pattern_max:
                self._pattern_active = False
                return None

            # Pattern bounds stay fixed (no expansion)
            self._pattern_bars += 1
            self._pattern_total_vol += bar.volume

        return None

    def _build_signal(self, snap, bar, i_atr, vol_ma, vw, e9, pdh,
                      pattern_avg_vol, pre_pattern_avg_vol, day_range):
        cfg = self.cfg

        # Entry: aggressive breakout trigger
        trigger_price = self._pattern_high + 0.01
        entry_price = max(trigger_price, bar.open)

        # Stop: pattern base low - $0.02
        stop = self._pattern_low - cfg.bd_stop_buffer
        risk = entry_price - stop
        if risk <= 0:
            return None

        # Min risk floor
        min_risk = max(0.30, 0.10 * i_atr) if i_atr > 0 else 0.30
        if risk < min_risk:
            stop = entry_price - min_risk
            risk = min_risk

        # Target: structural
        _candidates = []
        # Measured move: pattern_high + prior_drive_size
        drive_size = self._drive_high - snap.session_open if not _isnan(snap.session_open) else 0
        if drive_size > 0:
            mm = self._pattern_high + drive_size
            if mm > entry_price:
                _candidates.append((mm, "measured_move"))
        if not _isnan(snap.session_high) and snap.session_high > entry_price:
            _candidates.append((snap.session_high, "session_high"))

        target, actual_rr, target_tag, skipped = compute_structural_target_long(
            entry_price, risk, _candidates,
            min_rr=0.0, max_rr=1.5,
            fallback_rr=1.5, mode="structural",
        )
        if skipped:
            target = entry_price + risk * 2.0
            actual_rr = 2.0
            target_tag = "fixed_rr_fallback"

        # ── BIG_DAWG admissibility filters ──

        # 1. Projected RR band: reject if outside [min, max]
        if (_isnan(actual_rr) or actual_rr < cfg.bd_min_actual_rr
                or actual_rr > cfg.bd_max_actual_rr):
            return None

        # 2. Bullish trigger-bar anatomy: counter_wick_fraction = (open - low) / (high - low)
        _bar_range = bar.high - bar.low
        if _bar_range <= 0:
            return None  # degenerate bar — reject safely
        _counter_wick_fraction = (bar.open - bar.low) / _bar_range
        if _counter_wick_fraction > cfg.bd_max_counter_wick_fraction:
            return None

        # Metadata
        pattern_size = self._pattern_high - self._pattern_low
        above_frac = self._bars_above_open / max(self._total_bars, 1)
        drive_pct = ((self._drive_high - snap.session_open) / snap.session_open * 100.0
                     if not _isnan(snap.session_open) and snap.session_open > 0 else 0)
        pattern_vol_frac = pattern_avg_vol / pre_pattern_avg_vol if pre_pattern_avg_vol > 0 else 0

        # Quality pipeline
        quality_score = 3
        quality_tier = QualityTier.B_TIER
        reject_reasons = []

        if self.rejection and self.quality:
            reject_reasons = self.rejection.check_all(
                bar, snap.recent_bars, len(snap.recent_bars) - 1,
                i_atr, e9, vw, vol_ma,
                skip_filters=self.skip_rejections
            )
            stock_factors = {
                "in_play_score": snap.in_play_score,
                "rs_market": getattr(snap, 'rs_market', 0.0),
                "rs_sector": 0.0,
                "volume_profile": min(bar.volume / vol_ma, 1.0) if vol_ma > 0 else 0.0,
            }
            market_factors = {
                "regime_score": getattr(snap, 'regime_score', 0.5),
                "alignment_score": getattr(snap, 'alignment_score', 0.0),
            }
            setup_factors = {
                "trigger_quality": trigger_bar_quality(bar, i_atr, vol_ma),
                "structure_quality": 0.7,
                "confluence_count": 4,
            }
            if not reject_reasons:
                quality_tier, quality_score = self.quality.score(
                    stock_factors, market_factors, setup_factors)
            else:
                _, quality_score = self.quality.score(
                    stock_factors, market_factors, setup_factors)
                quality_tier = QualityTier.B_TIER

        # Internal gate + A-tier bypass
        _ip_ok = snap.in_play_score >= cfg.bd_min_ip_score
        _q_ok = quality_score >= cfg.bd_min_quality_score if cfg.bd_min_quality_score > 0 else True
        if _ip_ok and _q_ok:
            quality_tier = QualityTier.A_TIER

        self._triggered = True

        return RawSignal(
            strategy_name=self.name,
            direction=1,
            entry_price=entry_price,
            stop_price=stop,
            target_price=target,
            bar_idx=snap.bar_idx,
            hhmm=snap.hhmm,
            quality=quality_score,
            metadata={
                "session_open": round(snap.session_open, 2) if not _isnan(snap.session_open) else None,
                "prior_day_high": round(pdh, 2) if not _isnan(pdh) else None,
                "vwap_at_entry": round(vw, 2) if not _isnan(vw) else None,
                "drive_high": round(self._drive_high, 2),
                "drive_pct": round(drive_pct, 2),
                "day_range": round(day_range, 2),
                "above_open_frac": round(above_frac, 3),
                "pattern_high": round(self._pattern_high, 2),
                "pattern_low": round(self._pattern_low, 2),
                "pattern_bars": self._pattern_bars,
                "pattern_size": round(pattern_size, 3),
                "pattern_vs_dayrange": round(pattern_size / day_range, 3) if day_range > 0 else 0,
                "pattern_vol_frac": round(pattern_vol_frac, 3),
                "breakout_vol_frac": round(bar.volume / pattern_avg_vol, 2) if pattern_avg_vol > 0 else 0,
                "actual_rr": round(actual_rr, 3),
                "counter_wick_fraction": round(_counter_wick_fraction, 3),
                "target_tag": target_tag,
                "exit_mode": cfg.bd_exit_mode,
                "quality_tier": quality_tier.value,
                "reject_reasons": reject_reasons,
                "in_play_score": snap.in_play_score,
                "entry_type": "breakout_trigger",
            },
        )
