"""
PDH_FBO_SHORT — Live incremental version (hybrid timeframe).

Prior Day High Failed Breakout Short:
  break above PDH → failure back below → continuation → short entry.

SHORT only. Runs on 1-min bars (timeframe=1). Uses BOTH 1-min and 5-min
indicator snapshots from SharedIndicators.

FAMILY CORE MEMBER: PDH_B (continuation mode).
Mode A is held back (N=9, PF=1.11). Mode B is the active paper candidate
(N=11, PF=3.31). Both modes are implemented here but Mode A defaults off.

Regime gate: GREEN days blocked entirely. Strongly bullish intraday blocked.

State machine phases:
  IDLE → BROKE_OUT → FAILED → RETESTING → SIGNAL(A) | → SIGNAL(B) | EXPIRE
"""

import math
from typing import Optional

from ...models import Bar, NaN
from ...market_context import MarketContext
from ..shared.config import StrategyConfig
from ..shared.helpers import bar_body_ratio, trigger_bar_quality
from ..shared.rejection_filters import RejectionFilters
from ..shared.quality_scoring import QualityScorer
from ..shared.signal_schema import QualityTier
from .shared_indicators import IndicatorSnapshot
from .base import LiveStrategy, RawSignal

_isnan = math.isnan

IDLE, BROKE_OUT, FAILED, RETESTING = 0, 1, 2, 3


class PDHFBOShortLive(LiveStrategy):
    """Incremental PDH_FBO_SHORT for live pipeline.

    Runs on 1-min bars. Uses snap.prior_day_high (computed by
    SharedIndicators). Uses 5-min ATR/EMA context from the snapshot
    for structure checks. Also reads snap.or_high for confluence tagging.
    """

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True,
                 enable_mode_a: bool = False, enable_mode_b: bool = True):
        super().__init__(name="PDH_FBO", direction=-1, enabled=enabled,
                         skip_rejections=["choppiness", "maturity", "distance", "bigger_picture", "trigger_weakness"],
                         timeframe=1)
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality
        self._enable_mode_a = enable_mode_a
        self._enable_mode_b = enable_mode_b
        self._init_state()

    def _init_state(self):
        self._phase: int = IDLE
        self._bo_high_water: float = NaN
        self._bars_since_bo: int = 0
        self._fail_bar_low: float = NaN
        self._bars_since_fail: int = 0
        self._retest_high: float = NaN
        self._triggered_a: bool = False
        self._triggered_b: bool = False

    def reset_day(self):
        self._init_state()

    def step(self, snap: IndicatorSnapshot,
             market_ctx=None) -> Optional[RawSignal]:
        cfg = self.cfg
        hhmm = snap.hhmm
        bar = snap.bar

        if self._triggered_a and self._triggered_b:
            return None

        # PDH must be available
        pdh = getattr(snap, 'prior_day_high', NaN)
        if _isnan(pdh) or pdh <= 0:
            return None

        # Need OR to be established (for time gating and confluence)
        if not snap.or_ready:
            return None

        # ATR: prefer 5m, fallback to 1m, then daily
        atr_1m = snap.atr_1m
        atr_5m = snap.atr_5m
        atr = atr_5m if (not _isnan(atr_5m) and atr_5m > 0) else (
            atr_1m if (not _isnan(atr_1m) and atr_1m > 0) else snap.daily_atr)
        if _isnan(atr) or atr <= 0:
            return None

        vol_ma = snap.vol_ma_1m
        if _isnan(vol_ma) or vol_ma <= 0:
            return None

        vwap = snap.vwap
        or_high = snap.or_high

        # ── Tick state counters ──
        if self._phase == BROKE_OUT:
            self._bars_since_bo += 1
            if bar.high > self._bo_high_water:
                self._bo_high_water = bar.high
            if self._bars_since_bo > cfg.pdh_failure_window:
                self._phase = IDLE
                return None

        elif self._phase in (FAILED, RETESTING):
            self._bars_since_fail += 1

        # ── Phase 0 → 1: Breakout above PDH ──
        if self._phase == IDLE:
            if hhmm < cfg.pdh_time_start or hhmm > cfg.pdh_time_end:
                return None

            dist_above = bar.close - pdh
            if dist_above >= cfg.pdh_break_min_dist_atr * atr:
                body_r = bar_body_ratio(bar)
                if body_r >= cfg.pdh_break_body_min and bar.close > bar.open:
                    vol_ok = bar.volume >= cfg.pdh_break_vol_frac * vol_ma
                    if vol_ok:
                        self._phase = BROKE_OUT
                        self._bars_since_bo = 0
                        self._bo_high_water = bar.high
                        return None
            return None

        # ── Phase 1 → 2: Failure — close back below PDH ──
        if self._phase == BROKE_OUT:
            if bar.close < pdh:
                body_r = bar_body_ratio(bar)
                if body_r >= 0.25:
                    self._phase = FAILED
                    self._fail_bar_low = bar.low
                    self._bars_since_fail = 0
                    return None
            return None

        # ── Phase 2 → Mode A: retest from below ──
        if self._phase == FAILED and not self._triggered_a and self._enable_mode_a:
            proximity = pdh - bar.high
            if (abs(proximity) <= cfg.pdh_retest_proximity_atr * atr and
                    bar.high <= pdh + 0.05 * atr):
                self._phase = RETESTING
                self._retest_high = bar.high
                return None

        if self._phase == RETESTING and not self._triggered_a and self._enable_mode_a:
            if bar.high > self._retest_high:
                self._retest_high = bar.high

            rng = bar.high - bar.low
            if rng > 0:
                upper_wick = (bar.high - max(bar.open, bar.close)) / rng
                body_r = bar_body_ratio(bar)
                if (bar.close < bar.open and
                        upper_wick >= cfg.pdh_rejection_wick_min and
                        body_r >= cfg.pdh_rejection_body_min):
                    sig = self._try_signal(
                        snap, bar, pdh, or_high, atr, vol_ma, vwap,
                        market_ctx, mode="A",
                        retest_high=self._retest_high,
                    )
                    if sig is not None:
                        self._triggered_a = True
                        self._phase = IDLE
                        return sig
                    self._phase = IDLE
                    return None

            if self._bars_since_fail > cfg.pdh_retest_window + cfg.pdh_rejection_lookback:
                self._phase = IDLE
                return None

        # ── Phase 2 → Mode B: continuation without retest ──
        if self._phase == FAILED and not self._triggered_b and self._enable_mode_b:
            if self._bars_since_fail >= cfg.pdh_mode_b_no_retest_wait:
                if self._bars_since_fail <= cfg.pdh_mode_b_no_retest_wait + cfg.pdh_mode_b_window:
                    if bar.close < self._fail_bar_low:
                        body_r = bar_body_ratio(bar)
                        if body_r >= cfg.pdh_mode_b_body_min and bar.close < bar.open:
                            sig = self._try_signal(
                                snap, bar, pdh, or_high, atr, vol_ma, vwap,
                                market_ctx, mode="B",
                                retest_high=pdh,
                            )
                            if sig is not None:
                                self._triggered_b = True
                                self._phase = IDLE
                                return sig
                            self._phase = IDLE
                            return None
                else:
                    self._phase = IDLE
                    return None

        # Timeout
        if self._phase == FAILED:
            max_wait = cfg.pdh_retest_window + cfg.pdh_mode_b_no_retest_wait + cfg.pdh_mode_b_window
            if self._bars_since_fail > max_wait:
                self._phase = IDLE

        return None

    def _try_signal(self, snap: IndicatorSnapshot, bar: Bar,
                    pdh: float, or_high: float, atr: float, vol_ma: float,
                    vwap: float, market_ctx, mode: str,
                    retest_high: float) -> Optional[RawSignal]:
        """Build signal with regime gate + structural filters."""
        cfg = self.cfg

        # ── Regime gate ──
        regime_label = self._get_regime_label(market_ctx)
        if regime_label == "GREEN":
            return None

        # Block strongly bullish intraday
        if market_ctx is not None and hasattr(market_ctx, 'spy'):
            spy = market_ctx.spy
            if (spy.above_vwap and spy.ema9_above_ema20 and
                    not _isnan(spy.pct_from_open) and spy.pct_from_open > 0.3):
                return None

        # ── Structural filter: no damage (Mode A only) ──
        if mode == "A" and abs(bar.close - pdh) < 0.10 * atr:
            return None

        # ── Stop / Target ──
        if mode == "A":
            stop_ref_type = "retest_high"
            stop = retest_high + cfg.pdh_stop_buffer_atr * atr
            stop_ref_price = retest_high
        else:
            stop_ref_type = "pdh"
            stop = pdh + cfg.pdh_stop_buffer_atr * atr
            stop_ref_price = pdh

        raw_stop = stop
        min_stop = cfg.pdh_min_stop_atr * atr
        min_stop_rule_applied = False
        if stop - bar.close < min_stop:
            stop = bar.close + min_stop
            min_stop_rule_applied = True

        risk = stop - bar.close
        if risk <= 0:
            return None

        # Capture stop metadata
        _stop_meta = {
            "stop_ref_type": stop_ref_type,
            "stop_ref_price": round(stop_ref_price, 2),
            "raw_stop": round(raw_stop, 2),
            "buffer_type": "atr",
            "buffer_value": cfg.pdh_stop_buffer_atr,
            "min_stop_rule_applied": min_stop_rule_applied,
            "min_stop_distance": round(min_stop, 2),
            "final_stop": round(stop, 2),
        }

        # Target: structural (VWAP, ORL, session low) or skip
        if cfg.pdh_target_mode == "structural":
            _candidates = []
            if not _isnan(vwap) and vwap < bar.close:
                _candidates.append((vwap, "vwap"))
            if not _isnan(snap.or_low) and snap.or_low < bar.close:
                _candidates.append((snap.or_low, "orl"))
            if not _isnan(snap.session_low) and snap.session_low < bar.close:
                _candidates.append((snap.session_low, "session_low"))
            from ..shared.helpers import compute_structural_target_short
            target, actual_rr, target_tag, skipped = compute_structural_target_short(
                bar.close, risk, _candidates,
                min_rr=cfg.pdh_struct_min_rr, max_rr=cfg.pdh_struct_max_rr,
                fallback_rr=cfg.pdh_target_rr, mode="structural",
            )
            if skipped:
                return None
        else:
            if not _isnan(vwap) and bar.close - vwap >= cfg.pdh_min_vwap_dist_atr * atr:
                target = vwap
                actual_rr = (bar.close - target) / risk
            else:
                target = bar.close - cfg.pdh_target_rr * risk
                actual_rr = cfg.pdh_target_rr
            target_tag = "vwap" if target == vwap else "fixed_rr"

        # ── Confluence check ──
        has_confluence = False
        if not _isnan(or_high) and not _isnan(pdh):
            if abs(pdh - or_high) <= cfg.pdh_orh_confluence_atr * atr:
                has_confluence = True

        # ── Tags ──
        confluence_tags = [f"pdh_fbo_{mode.lower()}"]
        if not _isnan(vwap) and bar.close < vwap:
            confluence_tags.append("below_vwap")
        e9_1m = snap.ema9_1m
        if not _isnan(e9_1m) and bar.close < e9_1m:
            confluence_tags.append("below_ema9")
        trap_depth = (self._bo_high_water - pdh) / atr if atr > 0 and not _isnan(self._bo_high_water) else 0
        if trap_depth >= 0.20:
            confluence_tags.append("strong_trap")
        if has_confluence:
            confluence_tags.append("orh_pdh_confluence")
        if mode == "A":
            confluence_tags.append("failed_retest")
        else:
            confluence_tags.append("continuation")

        # ── Quality pipeline ──
        quality_score = 3  # default
        quality_tier = QualityTier.B_TIER
        reject_reasons = []

        if self.rejection and self.quality:
            i_atr = atr
            e9 = e9_1m
            vw = vwap
            struct_q = 0.5 + (0.2 if mode == "A" else 0.0) + (0.1 if has_confluence else 0.0) + min(trap_depth * 0.2, 0.2)

            reject_reasons = self.rejection.check_all(
                bar, snap.recent_bars_1m, len(snap.recent_bars_1m) - 1, i_atr, e9, vw, vol_ma,
                skip_filters=self.skip_rejections
            )

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
                "confluence_count": len(confluence_tags),
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

        return RawSignal(
            strategy_name=f"PDH_FBO_{mode}",
            direction=-1,
            entry_price=bar.close,
            stop_price=stop,
            target_price=target,
            bar_idx=snap.bar_idx,
            hhmm=snap.hhmm,
            quality=quality_score,
            metadata={
                "mode": mode,
                "actual_rr": round(actual_rr, 3),
                "target_tag": target_tag,
                "trap_depth_atr": round(trap_depth, 3),
                "pdh": round(pdh, 2),
                "or_high": round(or_high, 2) if not _isnan(or_high) else None,
                "has_confluence": has_confluence,
                "vwap_at_signal": round(vwap, 2) if not _isnan(vwap) else None,
                "regime": regime_label,
                "structure_quality": round(struct_q, 3) if self.rejection and self.quality else None,
                "confluence": confluence_tags,
                "quality_tier": quality_tier.value,
                "reject_reasons": reject_reasons,
                "in_play_score": snap.in_play_score,
                **_stop_meta,
            },
        )

    @staticmethod
    def _get_regime_label(market_ctx) -> str:
        if market_ctx is None:
            return "UNKNOWN"
        spy = getattr(market_ctx, 'spy', None)
        if spy is None:
            return "UNKNOWN"
        pct = spy.pct_from_open
        if _isnan(pct):
            return "UNKNOWN"
        if pct > 0.05:
            return "GREEN"
        elif pct < -0.05:
            return "RED"
        return "FLAT"
