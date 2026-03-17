"""
ORL_FBD_LONG — Live incremental version.

Opening Range Low Failed Breakdown Reclaim Long: breakdown → reclaim → HH confirm.
LONG only. ORL-only for v1. GREEN regime days only.

State machine phases:
  IDLE → BROKE_DOWN → RECLAIMED → SIGNAL (or EXPIRE)

NOTE: GREEN-only regime gate enforced downstream (alert pipeline checks
market_ctx.day_label == "GREEN" before promoting signals to alerts).
The live step() emits raw signals; the pipeline filters by regime.
"""

import math
from typing import Optional

from ...models import Bar, NaN
from ..shared.config import StrategyConfig
from ..shared.level_helpers import breakout_quality
from ..shared.helpers import bar_body_ratio, trigger_bar_quality
from ..shared.rejection_filters import RejectionFilters
from ..shared.quality_scoring import QualityScorer
from ..shared.signal_schema import QualityTier
from .shared_indicators import IndicatorSnapshot
from .base import LiveStrategy, RawSignal

_isnan = math.isnan

# State constants
IDLE, BROKE_DOWN, RECLAIMED = 0, 1, 2


class ORLFBDLongLive(LiveStrategy):
    """Incremental ORL_FBD_LONG for live pipeline."""

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True):
        super().__init__(name="ORL_FBD_LONG", direction=1, enabled=enabled,
                         skip_rejections=["distance", "bigger_picture", "trigger_weakness"])
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality

        self._time_start = cfg.get(cfg.orl_time_start)
        self._time_end = cfg.get(cfg.orl_time_end)
        self._failure_window = cfg.get(cfg.orl_failure_window)
        self._confirm_bars = cfg.get(cfg.orl_reclaim_confirm_bars)

        self._init_state()

    def _init_state(self):
        self._phase: int = IDLE
        self._bd_bar_vol: float = 0.0
        self._bd_quality: float = 0.0
        self._bars_since_bd: int = 0
        self._lowest_since_bd: float = NaN
        self._reclaim_bar_high: float = NaN
        self._reclaim_bar_low: float = NaN
        self._bars_since_reclaim: int = 0
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
        if not snap.or_ready or _isnan(snap.or_low):
            return None

        or_low = snap.or_low
        rng = bar.high - bar.low

        # Track lowest since breakdown
        if self._phase in (BROKE_DOWN, RECLAIMED):
            if _isnan(self._lowest_since_bd) or bar.low < self._lowest_since_bd:
                self._lowest_since_bd = bar.low

        # Tick state counters
        if self._phase == BROKE_DOWN:
            self._bars_since_bd += 1
            if self._bars_since_bd > self._failure_window:
                self._phase = IDLE
                return None
        elif self._phase == RECLAIMED:
            self._bars_since_reclaim += 1
            if self._bars_since_reclaim > self._confirm_bars:
                self._phase = IDLE
                return None

        # ── Phase 1: Breakdown ──
        if self._phase == IDLE and self._time_start <= hhmm <= self._time_end:
            dist_below = or_low - bar.close
            if dist_below < cfg.orl_break_min_dist_atr * i_atr:
                return None
            if bar_body_ratio(bar) < cfg.orl_break_body_min:
                return None
            if bar.volume < cfg.orl_break_vol_frac * vol_ma:
                return None
            if bar.close >= bar.open:
                return None

            self._phase = BROKE_DOWN
            self._bd_bar_vol = bar.volume
            self._bd_quality = breakout_quality(bar, or_low, i_atr, vol_ma, -1)
            self._bars_since_bd = 0
            self._lowest_since_bd = bar.low
            return None

        # ── Phase 2: Reclaim ──
        if self._phase == BROKE_DOWN:
            if bar.close > or_low:
                body_r = abs(bar.close - bar.open) / rng if rng > 0 else 0.0
                if body_r >= cfg.orl_reclaim_body_min and bar.close > bar.open:
                    self._phase = RECLAIMED
                    self._reclaim_bar_high = bar.high
                    self._reclaim_bar_low = bar.low
                    self._bars_since_reclaim = 0
            return None

        # ── Phase 3: HH confirmation ──
        if self._phase == RECLAIMED:
            clearance = cfg.orl_hh_clearance_atr * i_atr
            hh_made = bar.high > self._reclaim_bar_high + clearance

            if hh_made and bar.close > bar.open:
                # Determine stop reference and type
                if not _isnan(self._lowest_since_bd):
                    stop_ref_type = "lowest_since_bd"
                    stop_ref_price = self._lowest_since_bd
                else:
                    stop_ref_type = "or_low"
                    stop_ref_price = or_low

                raw_stop = stop_ref_price - cfg.orl_stop_buffer_atr * i_atr
                stop = raw_stop
                min_stop = cfg.orl_min_stop_atr * i_atr
                risk = bar.close - stop
                min_stop_rule_applied = False
                if risk < min_stop:
                    stop = bar.close - min_stop
                    risk = min_stop
                    min_stop_rule_applied = True
                if risk <= 0:
                    self._phase = IDLE
                    return None

                # Capture stop metadata
                _stop_meta = {
                    "stop_ref_type": stop_ref_type,
                    "stop_ref_price": round(stop_ref_price, 2),
                    "raw_stop": round(raw_stop, 2),
                    "buffer_type": "atr",
                    "buffer_value": cfg.orl_stop_buffer_atr,
                    "min_stop_rule_applied": min_stop_rule_applied,
                    "min_stop_distance": round(min_stop, 2),
                    "final_stop": round(stop, 2),
                }

                # Structural target: VWAP, ORH, session high
                if cfg.orl_target_mode == "structural":
                    _candidates = []
                    if not _isnan(vw) and vw > bar.close:
                        _candidates.append((vw, "vwap"))
                    if snap.or_ready and not _isnan(snap.or_high) and snap.or_high > bar.close:
                        _candidates.append((snap.or_high, "orh"))
                    if not _isnan(snap.session_high) and snap.session_high > bar.close:
                        _candidates.append((snap.session_high, "session_high"))
                    from ..shared.helpers import compute_structural_target_long
                    target, actual_rr, target_tag, skipped = compute_structural_target_long(
                        bar.close, risk, _candidates,
                        min_rr=cfg.orl_struct_min_rr, max_rr=cfg.orl_struct_max_rr,
                        fallback_rr=cfg.orl_target_rr, mode="structural",
                    )
                    if skipped:
                        self._phase = IDLE
                        return None
                else:
                    vwap_dist = vw - bar.close if not _isnan(vw) else 0.0
                    if vwap_dist >= cfg.orl_min_vwap_dist_atr * i_atr and not _isnan(vw):
                        target = vw
                        actual_rr = vwap_dist / risk
                    else:
                        target = bar.close + risk * cfg.orl_target_rr
                        actual_rr = cfg.orl_target_rr
                    target_tag = "vwap" if not _isnan(vw) and target == vw else "fixed_rr"
                vwap_dist = vw - bar.close if not _isnan(vw) else 0.0

                struct_q = 0.50
                if self._bd_quality >= 0.60:
                    struct_q += 0.15
                if self._bars_since_bd <= 3:
                    struct_q += 0.10
                if vwap_dist > 1.0 * i_atr:
                    struct_q += 0.10
                if bar.close > e9:
                    struct_q += 0.10
                if bar.volume >= 1.0 * vol_ma:
                    struct_q += 0.05

                confluence = ["orl_failed_bd"]
                if bar.close > e9:
                    confluence.append("above_ema9")
                if vwap_dist > 1.0 * i_atr:
                    confluence.append("room_to_vwap")

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

                self._phase = IDLE
                self._triggered_today = True

                return RawSignal(
                    strategy_name="ORL_FBD_LONG",
                    direction=1,
                    entry_price=bar.close,
                    stop_price=stop,
                    target_price=target,
                    bar_idx=snap.bar_idx,
                    hhmm=hhmm,
                    quality=quality_score,
                    metadata={
                        "or_low": or_low,
                        "bd_quality": self._bd_quality,
                        "bd_bar_vol_ratio": self._bd_bar_vol / vol_ma if vol_ma > 0 else 0,
                        "bars_to_reclaim": self._bars_since_bd,
                        "lowest_since_bd": self._lowest_since_bd,
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

            # Update reclaim high
            if bar.high > self._reclaim_bar_high:
                self._reclaim_bar_high = bar.high

        return None
