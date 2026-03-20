"""
GGG_LONG_V1 — Gap Give and Go, Long Only.

Gap up → opening flush that holds above support → mini-consolidation → breakout.

State machine phases:
  IDLE → FLUSH_DETECTED → CONSOL_ACTIVE → TRIGGERED (or EXPIRE)

1-minute native. Active 9:30-9:45 ET only.
Max 2 attempts per day (initial + 1 re-entry within 3 bars of stop).

V1 simplification: uses prior_day_high as support reference
(premarket low not available in RTH-only bar data).
"""

import math
from typing import Optional

from ...models import Bar, NaN
from ..shared.config import StrategyConfig
from ..shared.rejection_filters import RejectionFilters
from ..shared.quality_scoring import QualityScorer
from ..shared.signal_schema import QualityTier
from ..shared.helpers import trigger_bar_quality, bar_body_ratio
from .shared_indicators import IndicatorSnapshot
from .base import LiveStrategy, RawSignal

_isnan = math.isnan


class GapGiveGoLive(LiveStrategy):
    """Incremental GGG_LONG_V1 for live pipeline. Runs on 1-min bars."""

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True,
                 strategy_name: str = "GGG_LONG_V1"):
        super().__init__(name=strategy_name, direction=1, enabled=enabled,
                         skip_rejections=["distance", "maturity"],
                         timeframe=1)
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality

        self._time_start = cfg.get(cfg.ggg_time_start)
        self._time_end = cfg.get(cfg.ggg_time_end)
        self._consol_min = cfg.get(cfg.ggg_consol_min_bars)
        self._consol_max = cfg.get(cfg.ggg_consol_max_bars)

        self._init_state()

    def _init_state(self):
        # Phase tracking
        self._flush_detected: bool = False
        self._consol_active: bool = False
        self._triggered: bool = False

        # Flush state
        self._session_open: float = NaN
        self._prior_close: float = NaN
        self._gap_pct: float = NaN
        self._flush_low: float = NaN
        self._flush_size: float = NaN  # open - flush_low
        self._support_level: float = NaN  # PDH for V1
        self._bar_count: int = 0

        # Consolidation state
        self._consol_high: float = NaN
        self._consol_low: float = NaN
        self._consol_bars: int = 0
        self._consol_total_vol: float = 0.0
        self._failed_probes: int = 0

        # Attempt management
        self._attempts: int = 0
        self._bars_since_stop: int = 999
        self._awaiting_reentry: bool = False
        self._last_consol_high: float = NaN  # preserved for re-entry
        self._last_consol_low: float = NaN

    def reset_day(self):
        self._init_state()

    def step(self, snap: IndicatorSnapshot,
             market_ctx=None) -> Optional[RawSignal]:
        cfg = self.cfg
        hhmm = snap.hhmm
        bar = snap.bar
        i_atr = snap.atr
        vol_ma = snap.vol_ma20

        self._bar_count += 1

        # Only active 9:30-9:45
        if hhmm > self._time_end:
            return None
        if hhmm < self._time_start:
            return None

        # Need ATR and vol_ma
        if _isnan(i_atr) or i_atr <= 0:
            return None
        if _isnan(vol_ma) or vol_ma <= 0:
            return None

        # Max attempts reached
        if self._attempts >= cfg.ggg_max_attempts:
            return None

        # Track session open from first bar
        if _isnan(self._session_open) and not _isnan(snap.session_open):
            self._session_open = snap.session_open
        if _isnan(self._prior_close) and not _isnan(snap.prior_day_high):
            self._prior_close = getattr(snap, 'prior_close', NaN)
            # Fallback: compute from gap if prior_close available on snap
            if _isnan(self._prior_close):
                # Use prior_day data from indicators
                si = snap
                if hasattr(si, 'prior_day_low') and not _isnan(si.prior_day_low):
                    pass  # prior_close not directly on snapshot in all versions

        # Support level: prior day high for V1
        if _isnan(self._support_level) and not _isnan(snap.prior_day_high):
            self._support_level = snap.prior_day_high

        # ── RE-ENTRY WINDOW ──
        if self._awaiting_reentry:
            self._bars_since_stop += 1
            if self._bars_since_stop > cfg.ggg_reentry_window_bars:
                self._awaiting_reentry = False
                return None

            # Check if price breaks above the preserved consolidation high
            if bar.high > self._last_consol_high:
                # Re-entry trigger
                return self._build_signal(
                    snap, bar, i_atr, vol_ma,
                    self._last_consol_high, self._last_consol_low,
                    self._consol_bars, self._consol_total_vol,
                    attempt=self._attempts + 1,
                )
            return None

        # ── PHASE 1: FLUSH DETECTION ──
        if not self._flush_detected and not self._consol_active:
            if _isnan(self._session_open) or _isnan(self._support_level):
                return None

            # Need gap up
            gap_pct = 0.0
            if not _isnan(snap.prior_day_high):
                # Gap = (open - prior_close) / prior_close
                # Since we don't have prior_close directly, use: open must be above PDH
                if self._session_open <= self._support_level:
                    return None  # no gap above support
                gap_pct = ((self._session_open - self._support_level) / self._support_level * 100.0
                           if self._support_level > 0 else 0.0)

            if gap_pct < cfg.ggg_gap_min_pct:
                return None

            self._gap_pct = gap_pct

            # Check for flush: bar makes a new low below open
            if bar.low < self._session_open:
                flush_size = self._session_open - bar.low
                flush_pct = (flush_size / self._session_open * 100.0) if self._session_open > 0 else 0

                if flush_pct >= cfg.ggg_flush_min_pct:
                    # Check flush holds above support
                    if bar.low >= self._support_level:
                        # Check gap retracement: flush must not retrace > 50% of gap
                        gap_size = self._session_open - self._support_level
                        if gap_size > 0:
                            retrace_frac = flush_size / gap_size
                            if retrace_frac <= cfg.ggg_flush_max_gap_retrace_frac:
                                self._flush_detected = True
                                self._flush_low = bar.low
                                self._flush_size = flush_size
                                # Start consolidation tracking on next bar
                                return None

            return None

        # ── PHASE 2: CONSOLIDATION TRACKING ──
        if self._flush_detected and not self._consol_active:
            # First bar after flush — start consolidation
            self._consol_active = True
            self._consol_high = bar.high
            self._consol_low = bar.low
            self._consol_bars = 1
            self._consol_total_vol = bar.volume
            self._failed_probes = 0
            return None

        if self._consol_active and not self._triggered:
            # Check consolidation validity on each bar

            # Update bounds BEFORE breakout check
            new_high = max(self._consol_high, bar.high)
            new_low = min(self._consol_low, bar.low)
            consol_range = new_high - new_low

            # Consolidation low must stay above support
            if new_low < self._support_level:
                self._consol_active = False
                self._flush_detected = False
                return None

            # Consolidation too wide (> 50% of flush)
            if self._flush_size > 0 and consol_range > cfg.ggg_consol_max_flush_frac * self._flush_size:
                self._consol_active = False
                self._flush_detected = False
                return None

            # Too many bars
            if self._consol_bars >= self._consol_max:
                self._consol_active = False
                self._flush_detected = False
                return None

            # Track failed probes (bar high touches consol_high area but doesn't break out cleanly)
            if bar.high >= self._consol_high * 0.999 and bar.close < self._consol_high:
                self._failed_probes += 1

            if self._failed_probes > cfg.ggg_max_failed_probes:
                self._consol_active = False
                self._flush_detected = False
                return None

            # ── BREAKOUT CHECK ──
            # Only check breakout if min bars met
            if self._consol_bars >= self._consol_min:
                # Breakout: bar.high > consol_high (aggressive, don't wait for close)
                if bar.high > self._consol_high:
                    # Volume check
                    consol_avg_vol = self._consol_total_vol / self._consol_bars if self._consol_bars > 0 else 0
                    vol_ok = True
                    if consol_avg_vol > 0:
                        vol_ok = bar.volume >= cfg.ggg_break_vol_frac * consol_avg_vol

                    if vol_ok:
                        return self._build_signal(
                            snap, bar, i_atr, vol_ma,
                            self._consol_high, self._consol_low,
                            self._consol_bars, self._consol_total_vol,
                            attempt=self._attempts + 1,
                        )

            # Update consolidation state (no breakout this bar)
            self._consol_high = new_high
            self._consol_low = new_low
            self._consol_bars += 1
            self._consol_total_vol += bar.volume

        return None

    def _build_signal(self, snap, bar, i_atr, vol_ma,
                      consol_high, consol_low, consol_bars, consol_total_vol,
                      attempt: int) -> Optional[RawSignal]:
        """Construct the GGG signal with stop, target, quality pipeline."""
        cfg = self.cfg

        # Entry at breakout trigger price (aggressive: consol_high + tick)
        trigger_price = consol_high + 0.01
        entry_price = max(trigger_price, bar.open)

        # Stop: consolidation low - $0.02
        stop = consol_low - cfg.ggg_stop_buffer
        risk = entry_price - stop
        if risk <= 0:
            return None

        # Min risk floor: $0.50 or 0.15*ATR, whichever is larger
        # (Consolidation-low stop creates absurdly tight risk on early-session bars)
        min_risk = max(0.50, 0.15 * i_atr) if i_atr > 0 else 0.50
        if risk < min_risk:
            stop = entry_price - min_risk
            risk = min_risk

        # Target: structural (drive high, session high) — V1 uses structural like other strategies
        from ..shared.helpers import compute_structural_target_long
        _candidates = []
        if not _isnan(self._session_open) and self._session_open > entry_price:
            _candidates.append((self._session_open, "session_open"))
        if not _isnan(snap.session_high) and snap.session_high > entry_price:
            _candidates.append((snap.session_high, "session_high"))
        if not _isnan(snap.prior_day_high) and snap.prior_day_high > entry_price:
            _candidates.append((snap.prior_day_high, "pdh"))

        target, actual_rr, target_tag, skipped = compute_structural_target_long(
            entry_price, risk, _candidates,
            min_rr=0.0, max_rr=5.0,
            fallback_rr=1.5, mode="structural",
        )
        if skipped:
            # No structural target — use fixed 1.5R fallback
            target = entry_price + risk * 1.5
            actual_rr = 1.5
            target_tag = "fixed_rr_fallback"

        # Compute metadata
        consol_avg_vol = consol_total_vol / consol_bars if consol_bars > 0 else 0
        consol_size = consol_high - consol_low
        gap_size = self._session_open - self._support_level if not _isnan(self._support_level) else 0
        gap_retrace_frac = (self._flush_size / gap_size) if gap_size > 0 else 0
        consol_vs_flush = (consol_size / self._flush_size) if self._flush_size > 0 else 0

        # Quality pipeline
        quality_score = 3
        quality_tier = QualityTier.B_TIER
        reject_reasons = []

        e9 = snap.ema9_1m if hasattr(snap, 'ema9_1m') else snap.ema9
        vw = snap.vwap

        if self.rejection and self.quality:
            reject_reasons = self.rejection.check_all(
                bar, snap.recent_bars_1m if hasattr(snap, 'recent_bars_1m') else snap.recent_bars,
                len(snap.recent_bars_1m if hasattr(snap, 'recent_bars_1m') else snap.recent_bars) - 1,
                i_atr, e9, vw, vol_ma,
                skip_filters=self.skip_rejections
            )

            stock_factors = {
                "in_play_score": snap.in_play_score,
                "rs_market": snap.rs_market if hasattr(snap, 'rs_market') else 0.0,
                "rs_sector": 0.0,
                "volume_profile": min(bar.volume / vol_ma, 1.0) if vol_ma > 0 else 0.0,
            }
            market_factors = {
                "regime_score": snap.regime_score if hasattr(snap, 'regime_score') else 0.5,
                "alignment_score": snap.alignment_score if hasattr(snap, 'alignment_score') else 0.0,
            }
            setup_factors = {
                "trigger_quality": trigger_bar_quality(bar, i_atr, vol_ma),
                "structure_quality": 0.6,
                "confluence_count": 3,
            }

            if not reject_reasons:
                quality_tier, quality_score = self.quality.score(
                    stock_factors, market_factors, setup_factors)
            else:
                _, quality_score = self.quality.score(
                    stock_factors, market_factors, setup_factors)
                quality_tier = QualityTier.B_TIER if quality_score >= self.cfg.quality_b_min else QualityTier.C_TIER

        # Internal quality gate: force A-tier if IP and quality pass
        _ip_ok = snap.in_play_score >= cfg.ggg_min_ip_score
        _q_ok = quality_score >= cfg.ggg_min_quality_score if cfg.ggg_min_quality_score > 0 else True
        if _ip_ok and _q_ok:
            quality_tier = QualityTier.A_TIER

        # Update state
        self._attempts = attempt
        self._triggered = True
        self._last_consol_high = consol_high
        self._last_consol_low = consol_low

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
                "gap_pct": round(self._gap_pct, 3),
                "open_price": round(self._session_open, 2),
                "support_level": round(self._support_level, 2),
                "support_mode": cfg.ggg_support_mode,
                "flush_low": round(self._flush_low, 2),
                "flush_size": round(self._flush_size, 2),
                "flush_pct": round(self._flush_size / self._session_open * 100, 3) if self._session_open > 0 else 0,
                "gap_retrace_frac": round(gap_retrace_frac, 3),
                "consol_high": round(consol_high, 2),
                "consol_low": round(consol_low, 2),
                "consol_bars": consol_bars,
                "consol_size": round(consol_size, 3),
                "consol_vs_flush_frac": round(consol_vs_flush, 3),
                "breakout_vol_frac": round(bar.volume / consol_avg_vol, 2) if consol_avg_vol > 0 else 0,
                "attempt_number": attempt,
                "failed_probes": self._failed_probes,
                "actual_rr": round(actual_rr, 3),
                "target_tag": target_tag,
                "exit_mode": cfg.ggg_exit_mode,
                "quality_tier": quality_tier.value,
                "reject_reasons": reject_reasons,
                "entry_type": "breakout_trigger",
            },
        )

    def on_stop_hit(self):
        """Called externally if the trade is stopped out. Enables re-entry window."""
        if self._attempts < self.cfg.ggg_max_attempts:
            self._awaiting_reentry = True
            self._bars_since_stop = 0
            self._triggered = False
