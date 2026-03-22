"""
EMA9_V6_A — Live incremental version (5-MINUTE TIMEFRAME).

EMA9 FirstTouch redesign: Opening drive → first pullback to 5m E9 → reclaim entry.
Long only. 5-min bars fix the R:R geometry problem from V5 (1-min).

Key V5→V6 changes:
  - 5-min bars: E9 ready at bar 9 (~10:15), E20 at bar 20 (~11:10)
  - Drive uses session_high (not current bar high)
  - Pullback depth measured in 5-min ATR (much wider than 1-min)
  - No E9>E20 requirement (meaningless noise on intraday bars)
  - Minimum 1:1 structural RR required (V5 had no floor → median 0.29 RR)
  - Wider price band ($20-500 vs $25-150)
  - No fatal state resets on VWAP undercut (skip bar instead)
  - Time window 10:15-12:30 (after E9 ready, wider to compensate for 5m cadence)

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


class EMA9V6ALive(LiveStrategy):
    """Incremental EMA9_V6_A for live pipeline. Runs on 5-min bars."""

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True,
                 strategy_name: str = "EMA9_V6_A"):
        super().__init__(name=strategy_name, direction=1, enabled=enabled,
                         skip_rejections=["distance", "maturity", "trigger_weakness"],
                         timeframe=5)  # ← 5-min timeframe (key V6 change)
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality
        self._init_state()

    def _init_state(self):
        self._drive_confirmed: bool = False
        self._drive_high: float = NaN
        self._pullback_started: bool = False
        self._pullback_low: float = NaN
        self._pullback_touched_e9: bool = False
        self._triggered: bool = False
        self._prev_bar_low: float = NaN
        self._prev_prev_bar_low: float = NaN

    def reset_day(self):
        self._init_state()

    def step(self, snap: IndicatorSnapshot,
             market_ctx=None) -> Optional[RawSignal]:
        cfg = self.cfg
        hhmm = snap.hhmm
        bar = snap.bar

        # ── Read 5-min indicators (this strategy's native timeframe) ──
        e9 = snap.ema9_5m
        e20 = snap.ema20_5m
        vw = snap.vwap
        i_atr = snap.atr_5m
        vol_ma = snap.vol_ma_5m

        # Fall back to daily ATR if 5-min ATR not yet ready
        if _isnan(i_atr) or i_atr <= 0:
            i_atr = snap.daily_atr
        if _isnan(i_atr) or i_atr <= 0 or self._triggered:
            return None
        if not snap.ema9_5m_ready:
            return None
        if _isnan(vol_ma) or vol_ma <= 0:
            return None

        # ── Phase 1: Opening drive detection ──
        # Uses session_high (not bar.high) — detects drives that already happened
        if not self._drive_confirmed:
            if not _isnan(snap.session_open) and i_atr > 0:
                drive_dist = snap.session_high - snap.session_open
                if drive_dist >= cfg.ema9_v6a_drive_min_atr * i_atr:
                    if not _isnan(vw) and bar.close > vw:
                        self._drive_confirmed = True
                        self._drive_high = snap.session_high
            return None

        # Update drive high while price still extending
        if not self._pullback_started:
            if bar.high > self._drive_high:
                self._drive_high = bar.high

            # Detect start of pullback: price pulls back toward E9
            near_e9 = bar.low <= e9 + 0.25 * i_atr
            pulling_back = bar.close < self._drive_high - 0.20 * i_atr

            if near_e9:
                self._pullback_started = True
                self._pullback_low = bar.low
                self._pullback_touched_e9 = True
            elif pulling_back:
                self._pullback_started = True
                self._pullback_low = bar.low
                self._pullback_touched_e9 = False
            else:
                return None

        # ── Phase 2: Pullback tracking ──
        if self._pullback_started and not self._triggered:
            if bar.low < self._pullback_low or _isnan(self._pullback_low):
                self._pullback_low = bar.low

            if bar.low <= e9 + 0.25 * i_atr:
                self._pullback_touched_e9 = True

            pb_depth = self._drive_high - self._pullback_low
            max_depth = cfg.ema9_v6a_max_pb_depth_atr * i_atr

            # Depth exceeded — reset drive (fatal, keeps quality high)
            if pb_depth > max_depth:
                self._drive_confirmed = False
                self._pullback_started = False
                return None

            # VWAP undercut — skip this bar but don't kill pattern
            if cfg.ema9_v6a_above_vwap and not _isnan(vw):
                if self._pullback_low < vw - cfg.ema9_v6a_pb_vwap_buffer_atr * i_atr:
                    return None

            # ── Phase 3: Trigger check ──
            if not (cfg.ema9_v6a_time_start <= hhmm <= cfg.ema9_v6a_time_end):
                return None

            if not self._pullback_touched_e9:
                return None

            # Trigger: bullish close above E9
            if bar.close > e9 and bar.close > bar.open:
                rng = bar.high - bar.low
                body_pct = abs(bar.close - bar.open) / rng if rng > 0 else 0.0
                close_pct = (bar.close - bar.low) / rng if rng > 0 else 0.0

                if (body_pct >= cfg.ema9_v6a_trigger_body_min and
                        close_pct >= cfg.ema9_v6a_trigger_close_min):

                    # VWAP filter on trigger bar
                    if cfg.ema9_v6a_above_vwap and not _isnan(vw) and bar.close <= vw:
                        return None

                    # RS check
                    stock_pct = ((bar.close - snap.session_open) / snap.session_open * 100.0
                                 if not _isnan(snap.session_open) and snap.session_open > 0 else 0.0)
                    spy_pct = 0.0
                    if market_ctx and hasattr(market_ctx, 'spy_pct_from_open'):
                        spy_pct = market_ctx.spy_pct_from_open
                    rs_pct = stock_pct - spy_pct

                    if rs_pct / 100.0 < cfg.ema9_v6a_min_rs_vs_spy:
                        self._prev_prev_bar_low = self._prev_bar_low
                        self._prev_bar_low = bar.low
                        return None

                    # Price band filter
                    entry_price = bar.close
                    if entry_price < cfg.ema9_v6a_price_min or entry_price > cfg.ema9_v6a_price_max:
                        self._prev_prev_bar_low = self._prev_bar_low
                        self._prev_bar_low = bar.low
                        return None

                    # ── Stop computation ──
                    stop = self._pullback_low - cfg.ema9_v6a_stop_buffer
                    risk = entry_price - stop

                    # Enforce minimum dollar risk floor
                    v6_floor_applied = False
                    if risk < cfg.ema9_v6a_min_stop_dollar:
                        stop = entry_price - cfg.ema9_v6a_min_stop_dollar
                        risk = cfg.ema9_v6a_min_stop_dollar
                        v6_floor_applied = True

                    if risk <= 0:
                        self._prev_prev_bar_low = self._prev_bar_low
                        self._prev_bar_low = bar.low
                        return None

                    _stop_meta = {
                        "stop_ref_type": "v6_floor" if v6_floor_applied else "pullback_low",
                        "stop_ref_price": self._pullback_low,
                        "raw_stop": self._pullback_low - cfg.ema9_v6a_stop_buffer,
                        "buffer_type": "dollar",
                        "buffer_value": cfg.ema9_v6a_min_stop_dollar if v6_floor_applied else cfg.ema9_v6a_stop_buffer,
                        "min_stop_rule_applied": v6_floor_applied,
                        "v5_floor_applied": False,
                        "final_stop": stop,
                    }

                    # ── Target computation (structural) ──
                    _candidates = []
                    if not _isnan(self._drive_high) and self._drive_high > entry_price:
                        _candidates.append((self._drive_high, "drive_high"))
                    if not _isnan(snap.session_high) and snap.session_high > entry_price:
                        _candidates.append((snap.session_high, "session_high"))

                    from ..shared.helpers import compute_structural_target_long
                    target, actual_rr, target_tag, skipped = compute_structural_target_long(
                        entry_price, risk, _candidates,
                        min_rr=cfg.ema9_v6a_struct_min_rr,
                        max_rr=cfg.ema9_v6a_struct_max_rr,
                        fallback_rr=cfg.ema9_v6a_fallback_rr,
                        mode="structural",
                    )
                    if skipped:
                        self._prev_prev_bar_low = self._prev_bar_low
                        self._prev_bar_low = bar.low
                        return None

                    # ── Quality scoring ──
                    drive_dist = self._drive_high - snap.session_open if not _isnan(snap.session_open) else 0

                    struct_q = 0.40
                    if pb_depth < 0.30 * i_atr:
                        struct_q += 0.15
                    elif pb_depth < 0.50 * i_atr:
                        struct_q += 0.10
                    if drive_dist >= 1.5 * i_atr:
                        struct_q += 0.15
                    if bar.volume >= 1.2 * vol_ma:
                        struct_q += 0.10
                    if not _isnan(e20) and e9 > e20:
                        struct_q += 0.10

                    confluence = []
                    if not _isnan(vw) and bar.close > vw:
                        confluence.append("above_vwap")
                    if bar.close > e9:
                        confluence.append("above_ema9")
                    if not _isnan(e20) and e9 > e20:
                        confluence.append("ema_aligned")
                    if rs_pct > 0.5:
                        confluence.append("strong_rs")
                    if drive_dist >= 1.5 * i_atr:
                        confluence.append("strong_drive")

                    quality_score = 3
                    quality_tier = QualityTier.B_TIER
                    reject_reasons = []

                    if self.rejection and self.quality:
                        reject_reasons = self.rejection.check_all(
                            bar, snap.recent_bars_5m if hasattr(snap, 'recent_bars_5m') else snap.recent_bars_1m,
                            len(snap.recent_bars_5m if hasattr(snap, 'recent_bars_5m') else snap.recent_bars_1m) - 1,
                            i_atr, e9, vw, vol_ma,
                            skip_filters=self.skip_rejections
                        )

                        if not reject_reasons:
                            stock_factors = {
                                "in_play_score": snap.in_play_score,
                                "rs_market": rs_pct,
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
                                "rs_market": rs_pct,
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

                    # ── TRIGGER FIRES ──
                    self._triggered = True
                    self._prev_prev_bar_low = self._prev_bar_low
                    self._prev_bar_low = bar.low
                    return RawSignal(
                        strategy_name=self.name,
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
                            "pullback_low": self._pullback_low,
                            "pb_depth": pb_depth,
                            "pb_depth_atr": pb_depth / i_atr if i_atr > 0 else 0,
                            "rs_vs_spy": rs_pct,
                            "actual_rr": actual_rr,
                            "target_tag": target_tag,
                            "structure_quality": min(struct_q, 1.0),
                            "confluence": confluence,
                            "in_play_score": snap.in_play_score,
                            "quality_tier": quality_tier.value,
                            "reject_reasons": reject_reasons,
                            "v6_floor_applied": v6_floor_applied,
                            "timeframe": "5m",
                            **_stop_meta,
                        },
                    )

        self._prev_prev_bar_low = self._prev_bar_low
        self._prev_bar_low = bar.low
        return None
