"""
EMA9_FT — Live incremental version (1-MINUTE TIMEFRAME).

EMA9 FirstTouch: Opening drive → first pullback to E9 → reclaim entry.
Long only. Time window: 09:35-11:15 ET.

This strategy runs on 1-min bars for early EMA readiness:
  - EMA9 ready at bar 9 (~9:39 ET) instead of bar 9×5 (~10:15 ET)
  - EMA20 ready at bar 20 (~9:50 ET) instead of bar 20×5 (~11:10 ET)
  - Full signal window usable: 09:50 - 11:15 (85 min vs 5 min on 5-min bars)

Reads: snap.ema9_1m, snap.ema20_1m, snap.atr_1m, snap.vol_ma_1m
Session state: snap.vwap, snap.session_open/high/low (always from 1-min)

State machine phases:
  IDLE → DRIVE_CONFIRMED → PULLBACK_STARTED → SIGNAL (or EXPIRE)
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


class EMA9FirstTouchLive(LiveStrategy):
    """Incremental EMA9_FT for live pipeline. Runs on 1-min bars."""

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True):
        super().__init__(name="EMA9_FT", direction=1, enabled=enabled,
                         skip_rejections=["distance", "maturity", "trigger_weakness"],
                         timeframe=1)  # ← 1-min timeframe
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality

        self._time_start = cfg.get(cfg.e9ft_time_start)
        self._time_end = cfg.get(cfg.e9ft_time_end)

        self._init_state()

    def _init_state(self):
        self._drive_confirmed: bool = False
        self._drive_high: float = NaN
        self._pullback_started: bool = False
        self._pullback_low: float = NaN
        self._pullback_touched_e9: bool = False
        self._triggered: bool = False

    def reset_day(self):
        self._init_state()

    def step(self, snap: IndicatorSnapshot,
             market_ctx=None) -> Optional[RawSignal]:
        cfg = self.cfg
        hhmm = snap.hhmm
        bar = snap.bar

        # ── Read 1-min indicators (this strategy's native timeframe) ──
        e9 = snap.ema9_1m
        e20 = snap.ema20_1m
        vw = snap.vwap              # VWAP is canonical (always from 1-min)
        i_atr = snap.atr_1m         # 1-min ATR (explicit, no ambiguity)
        vol_ma = snap.vol_ma_1m     # 1-min volume MA

        # Fall back to daily ATR if 1-min ATR not yet ready
        if _isnan(i_atr) or i_atr <= 0:
            i_atr = snap.daily_atr
        if _isnan(i_atr) or i_atr <= 0 or self._triggered:
            return None
        if not snap.ema9_1m_ready or not snap.ema20_1m_ready:
            return None
        if _isnan(vol_ma) or vol_ma <= 0:
            return None

        # ── Phase 1: Opening drive detection ──
        if not self._drive_confirmed:
            if not _isnan(snap.session_open) and i_atr > 0:
                drive_dist = bar.high - snap.session_open
                if drive_dist >= cfg.e9ft_drive_min_atr * i_atr:
                    if not _isnan(vw) and bar.close > vw:
                        if not cfg.e9ft_ema9_above_ema20 or e9 > e20:
                            self._drive_confirmed = True
                            self._drive_high = snap.session_high
            return None

        # Update drive high while price still extending
        if not self._pullback_started:
            if bar.high > self._drive_high:
                self._drive_high = bar.high

            # Detect start of pullback
            near_e9 = bar.low <= e9 + 0.20 * i_atr
            if near_e9:
                self._pullback_started = True
                self._pullback_low = bar.low
                self._pullback_touched_e9 = True
            elif bar.close < self._drive_high - 0.15 * i_atr:
                self._pullback_started = True
                self._pullback_low = bar.low
                self._pullback_touched_e9 = False
            else:
                return None

        # ── Phase 2: Pullback tracking ──
        if self._pullback_started and not self._triggered:
            if bar.low < self._pullback_low or _isnan(self._pullback_low):
                self._pullback_low = bar.low

            if bar.low <= e9 + 0.20 * i_atr:
                self._pullback_touched_e9 = True

            pb_depth = self._drive_high - self._pullback_low
            max_depth = cfg.e9ft_max_pullback_depth_atr * i_atr

            if pb_depth > max_depth:
                self._drive_confirmed = False
                self._pullback_started = False
                return None

            if cfg.e9ft_pullback_must_hold_vwap and not _isnan(vw):
                if self._pullback_low < vw - 0.10 * i_atr:
                    self._drive_confirmed = False
                    self._pullback_started = False
                    return None

            # ── Phase 3: Trigger check ──
            if not (self._time_start <= hhmm <= self._time_end):
                return None

            if not self._pullback_touched_e9:
                return None

            if bar.close > e9 and bar.close > bar.open:
                rng = bar.high - bar.low
                body_pct = abs(bar.close - bar.open) / rng if rng > 0 else 0.0
                close_pct = (bar.close - bar.low) / rng if rng > 0 else 0.0

                if (body_pct >= cfg.e9ft_trigger_body_min_pct and
                        close_pct >= cfg.e9ft_trigger_close_pct):

                    if cfg.e9ft_above_vwap and not _isnan(vw) and bar.close <= vw:
                        return None

                    # RS check (simplified for live — uses market_ctx if available)
                    stock_pct = (bar.close - snap.session_open) / snap.session_open if not _isnan(snap.session_open) and snap.session_open > 0 else 0
                    spy_pct = 0.0
                    if market_ctx and hasattr(market_ctx, 'spy_pct_from_open'):
                        spy_pct = market_ctx.spy_pct_from_open
                    rs = stock_pct - spy_pct

                    if rs < cfg.e9ft_min_rs_vs_spy:
                        return None

                    # ── TRIGGER FIRES ──
                    stop = self._pullback_low - cfg.e9ft_stop_buffer
                    min_stop = 0.15 * i_atr
                    min_stop_rule_applied = False
                    if abs(bar.close - stop) < min_stop:
                        stop = bar.close - min_stop
                        min_stop_rule_applied = True

                    risk = bar.close - stop
                    if risk <= 0:
                        self._drive_confirmed = False
                        self._pullback_started = False
                        return None

                    _stop_meta = {
                        "stop_ref_type": "pullback_low",
                        "stop_ref_price": self._pullback_low,
                        "raw_stop": self._pullback_low - cfg.e9ft_stop_buffer,
                        "buffer_type": "dollar",
                        "buffer_value": cfg.e9ft_stop_buffer,
                        "min_stop_rule_applied": min_stop_rule_applied,
                        "min_stop_distance": min_stop,
                        "final_stop": stop,
                    }

                    # Structural target: drive high, session high
                    if cfg.e9ft_target_mode == "structural":
                        _candidates = []
                        if not _isnan(self._drive_high) and self._drive_high > bar.close:
                            _candidates.append((self._drive_high, "drive_high"))
                        if not _isnan(snap.session_high) and snap.session_high > bar.close:
                            _candidates.append((snap.session_high, "session_high"))
                        from ..shared.helpers import compute_structural_target_long
                        target, actual_rr, target_tag, skipped = compute_structural_target_long(
                            bar.close, risk, _candidates,
                            min_rr=cfg.e9ft_struct_min_rr, max_rr=cfg.e9ft_struct_max_rr,
                            fallback_rr=cfg.e9ft_target_rr, mode="structural",
                        )
                        if skipped:
                            self._drive_confirmed = False
                            self._pullback_started = False
                            return None
                    else:
                        target = bar.close + risk * cfg.e9ft_target_rr
                        actual_rr = cfg.e9ft_target_rr
                        target_tag = "fixed_rr"

                    drive_dist = self._drive_high - snap.session_open if not _isnan(snap.session_open) else 0

                    struct_q = 0.40
                    if pb_depth < 0.30 * i_atr:
                        struct_q += 0.15
                    elif pb_depth < 0.45 * i_atr:
                        struct_q += 0.10
                    if drive_dist >= 1.5 * i_atr:
                        struct_q += 0.15
                    if bar.volume >= 1.2 * vol_ma:
                        struct_q += 0.10
                    if e9 > e20:
                        struct_q += 0.10

                    confluence = []
                    if not _isnan(vw) and bar.close > vw:
                        confluence.append("above_vwap")
                    if bar.close > e9:
                        confluence.append("above_ema9")
                    if e9 > e20:
                        confluence.append("ema_aligned")
                    if rs > 0.005:
                        confluence.append("strong_rs")
                    if drive_dist >= 1.5 * i_atr:
                        confluence.append("strong_drive")

                    # ── Quality pipeline ──
                    quality_score = 3  # default
                    quality_tier = QualityTier.B_TIER
                    reject_reasons = []

                    if self.rejection and self.quality:
                        # Rejection filters
                        reject_reasons = self.rejection.check_all(
                            bar, snap.recent_bars_1m, len(snap.recent_bars_1m) - 1, i_atr, e9, vw, vol_ma,
                            skip_filters=self.skip_rejections
                        )

                        # Quality scoring
                        if not reject_reasons:
                            stock_factors = {
                                "in_play_score": snap.in_play_score,
                                "rs_market": rs,
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
                                "rs_market": rs,
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
                    return RawSignal(
                        strategy_name="EMA9_FT",
                        direction=1,
                        entry_price=bar.close,
                        stop_price=stop,
                        target_price=target,
                        bar_idx=snap.bar_idx,
                        hhmm=hhmm,
                        quality=quality_score,
                        metadata={
                            "drive_high": self._drive_high,
                            "drive_dist": drive_dist,
                            "pullback_low": self._pullback_low,
                            "pb_depth": pb_depth,
                            "pb_depth_atr": pb_depth / i_atr if i_atr > 0 else 0,
                            "rs_vs_spy": rs,
                            "actual_rr": actual_rr,
                            "target_tag": target_tag,
                            "structure_quality": min(struct_q, 1.0),
                            "confluence": confluence,
                            "in_play_score": snap.in_play_score,
                            "quality_tier": quality_tier.value,
                            "reject_reasons": reject_reasons,
                            "timeframe": "1m",
                            **_stop_meta,
                        },
                    )

        return None
