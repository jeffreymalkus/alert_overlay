"""
ORH_FBO_SHORT_V2 — Live incremental version (hybrid timeframe).

Opening Range High Failed Breakout Short:
  break above ORH → failure back below → retest/continuation → short entry.

SHORT only. Runs on 1-min bars (timeframe=1). Uses BOTH 1-min and 5-min
indicator snapshots from SharedIndicators.

TWO ENTRY MODES (tracked separately):
  Mode A (premium): break → fail → retest from below → bearish rejection
  Mode B (secondary): break → fail → no retest → continuation through fail-bar low

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

# State constants
IDLE, BROKE_OUT, FAILED, RETESTING = 0, 1, 2, 3


class ORHFBOShortV2Live(LiveStrategy):
    """Incremental ORH_FBO_SHORT_V2 for live pipeline.

    Runs on 1-min bars. Reads ORH from snap.or_high (computed by
    SharedIndicators on 1-min precision). Uses 5-min ATR/EMA context
    from the snapshot for structure checks.
    """

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True):
        super().__init__(name="ORH_FBO_V2", direction=-1, enabled=enabled,
                         skip_rejections=["bigger_picture", "distance", "trigger_weakness"],
                         timeframe=1)  # runs on 1-min bars
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality
        self._init_state()

    def _init_state(self):
        self._phase: int = IDLE
        # Breakout tracking
        self._bo_high_water: float = NaN
        self._bars_since_bo: int = 0
        # Failure tracking
        self._fail_bar_low: float = NaN
        self._bars_since_fail: int = 0
        # Retest tracking (Mode A)
        self._retest_high: float = NaN
        # Daily triggers (one of each per day)
        self._triggered_a: bool = False
        self._triggered_b: bool = False

    def reset_day(self):
        self._init_state()

    def step(self, snap: IndicatorSnapshot,
             market_ctx=None) -> Optional[RawSignal]:
        cfg = self.cfg
        hhmm = snap.hhmm
        bar = snap.bar

        # ── Guards ──
        if self._triggered_a and self._triggered_b:
            return None
        if not snap.or_ready or _isnan(snap.or_high):
            return None

        # Use 1-min ATR for event detection, 5-min ATR for structural sizing
        atr_1m = snap.atr_1m
        atr_5m = snap.atr_5m
        # Prefer 5m ATR for sizing; fallback to 1m or daily
        atr = atr_5m if (not _isnan(atr_5m) and atr_5m > 0) else (
            atr_1m if (not _isnan(atr_1m) and atr_1m > 0) else snap.daily_atr)
        if _isnan(atr) or atr <= 0:
            return None

        vol_ma = snap.vol_ma_1m
        if _isnan(vol_ma) or vol_ma <= 0:
            return None

        or_high = snap.or_high
        vwap = snap.vwap

        # ── Tick state counters ──
        if self._phase == BROKE_OUT:
            self._bars_since_bo += 1
            if bar.high > self._bo_high_water:
                self._bo_high_water = bar.high
            if self._bars_since_bo > cfg.orh2_failure_window:
                self._phase = IDLE
                return None

        elif self._phase in (FAILED, RETESTING):
            self._bars_since_fail += 1

        # ── Phase 0 → 1: Breakout detection ──
        if self._phase == IDLE:
            if hhmm < cfg.orh2_time_start or hhmm > cfg.orh2_time_end:
                return None

            dist_above = bar.close - or_high
            if dist_above >= cfg.orh2_break_min_dist_atr * atr:
                body_r = bar_body_ratio(bar)
                if body_r >= cfg.orh2_break_body_min and bar.close > bar.open:
                    vol_ok = bar.volume >= cfg.orh2_break_vol_frac * vol_ma
                    if vol_ok:
                        self._phase = BROKE_OUT
                        self._bars_since_bo = 0
                        self._bo_high_water = bar.high
                        return None
            return None

        # ── Phase 1 → 2: Failure detection ──
        if self._phase == BROKE_OUT:
            if bar.close < or_high:
                body_r = bar_body_ratio(bar)
                if body_r >= 0.25:
                    self._phase = FAILED
                    self._fail_bar_low = bar.low
                    self._bars_since_fail = 0
                    return None
            return None

        # ── Phase 2 → Mode A: retest from below ──
        if self._phase == FAILED and not self._triggered_a:
            proximity = or_high - bar.high
            if (abs(proximity) <= cfg.orh2_retest_proximity_atr * atr and
                    bar.high <= or_high + 0.05 * atr):
                self._phase = RETESTING
                self._retest_high = bar.high
                return None

        if self._phase == RETESTING and not self._triggered_a:
            # Track retest high
            if bar.high > self._retest_high:
                self._retest_high = bar.high

            rng = bar.high - bar.low
            if rng > 0:
                upper_wick = (bar.high - max(bar.open, bar.close)) / rng
                body_r = bar_body_ratio(bar)
                if (bar.close < bar.open and
                        upper_wick >= cfg.orh2_rejection_wick_min and
                        body_r >= cfg.orh2_rejection_body_min):
                    # MODE A: attempt signal
                    sig = self._try_signal(
                        snap, bar, or_high, atr, vol_ma, vwap,
                        market_ctx, mode="A",
                        retest_high=self._retest_high,
                    )
                    if sig is not None:
                        self._triggered_a = True
                        self._phase = IDLE
                        return sig
                    # Signal blocked (regime etc) — reset
                    self._phase = IDLE
                    return None

            # Rejection window expired
            if self._bars_since_fail > cfg.orh2_retest_window + cfg.orh2_rejection_lookback:
                self._phase = IDLE
                return None

        # ── Phase 2 → Mode B: continuation without retest ──
        if self._phase == FAILED and not self._triggered_b:
            if self._bars_since_fail >= cfg.orh2_mode_b_no_retest_wait:
                if self._bars_since_fail <= cfg.orh2_mode_b_no_retest_wait + cfg.orh2_mode_b_window:
                    if bar.close < self._fail_bar_low:
                        body_r = bar_body_ratio(bar)
                        if body_r >= cfg.orh2_mode_b_body_min and bar.close < bar.open:
                            sig = self._try_signal(
                                snap, bar, or_high, atr, vol_ma, vwap,
                                market_ctx, mode="B",
                                retest_high=or_high,  # Mode B: stop above ORH
                            )
                            if sig is not None:
                                self._triggered_b = True
                                self._phase = IDLE
                                return sig
                            self._phase = IDLE
                            return None
                else:
                    # Mode B window expired
                    self._phase = IDLE
                    return None

        # Timeout: overall expiration
        if self._phase == FAILED:
            max_wait = cfg.orh2_retest_window + cfg.orh2_mode_b_no_retest_wait + cfg.orh2_mode_b_window
            if self._bars_since_fail > max_wait:
                self._phase = IDLE

        return None

    def _try_signal(self, snap: IndicatorSnapshot, bar: Bar,
                    or_high: float, atr: float, vol_ma: float,
                    vwap: float, market_ctx, mode: str,
                    retest_high: float) -> Optional[RawSignal]:
        """Build signal with regime gate + structural filters. Returns None if blocked."""
        cfg = self.cfg

        # ── Regime gate: block GREEN days entirely ──
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
        if mode == "A" and abs(bar.close - or_high) < 0.10 * atr:
            return None

        # ── Stop / Target ──
        if mode == "A":
            stop_ref_type = "retest_high"
            stop = retest_high + cfg.orh2_stop_buffer_atr * atr
            stop_ref_price = retest_high
        else:
            stop_ref_type = "or_high"
            stop = or_high + cfg.orh2_stop_buffer_atr * atr
            stop_ref_price = or_high

        raw_stop = stop
        min_stop = cfg.orh2_min_stop_atr * atr
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
            "buffer_value": cfg.orh2_stop_buffer_atr,
            "min_stop_rule_applied": min_stop_rule_applied,
            "min_stop_distance": round(min_stop, 2),
            "final_stop": round(stop, 2),
        }

        # Target: structural (VWAP, ORL, session low) or skip
        if cfg.orh2_target_mode == "structural":
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
                min_rr=cfg.orh2_struct_min_rr, max_rr=cfg.orh2_struct_max_rr,
                fallback_rr=cfg.orh2_target_rr, mode="structural",
            )
            if skipped:
                return None
        else:
            if not _isnan(vwap) and bar.close - vwap >= cfg.orh2_min_vwap_dist_atr * atr:
                target = vwap
                actual_rr = (bar.close - target) / risk
            else:
                target = bar.close - cfg.orh2_target_rr * risk
                actual_rr = cfg.orh2_target_rr
            target_tag = "vwap" if target == vwap else "fixed_rr"

        # ── Confluence tags ──
        confluence = [f"orh_fbo_v2_{mode.lower()}"]
        if not _isnan(vwap) and bar.close < vwap:
            confluence.append("below_vwap")
        e9_1m = snap.ema9_1m
        if not _isnan(e9_1m) and bar.close < e9_1m:
            confluence.append("below_ema9")
        trap_depth = (self._bo_high_water - or_high) / atr if atr > 0 and not _isnan(self._bo_high_water) else 0
        if trap_depth >= 0.20:
            confluence.append("strong_trap")
        if mode == "A":
            confluence.append("failed_retest")
        else:
            confluence.append("continuation")

        # ── Quality pipeline ──
        quality_score = 3  # default
        quality_tier = QualityTier.B_TIER
        reject_reasons = []

        if self.rejection and self.quality:
            i_atr = atr
            e9 = e9_1m
            vw = vwap
            struct_q = 0.5 + (0.2 if mode == "A" else 0.0) + min(trap_depth * 0.2, 0.2)

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

        # ── ORH_B sleeve classification ──
        # Compute trigger bar metrics for sleeve admission
        _bar_range = max(bar.high - bar.low, 1e-6)
        _body_fraction = abs(bar.close - bar.open) / _bar_range
        _counter_wick = (bar.high - max(bar.open, bar.close)) / _bar_range  # upper wick for bearish bar
        _bar_return_pct = ((bar.close - bar.open) / bar.open * 100.0) if bar.open > 0 else 0.0
        _rel_impulse = getattr(snap, 'rs_market', 0.0)  # relative impulse vs SPY
        _hhmm = snap.hhmm

        _is_elite = (quality_tier == QualityTier.A_TIER)
        _is_pm_continuation = False
        _is_pm_premium = False
        _confluence_premium = (len(confluence) >= 5)

        # Primary PM continuation sleeve: hour>=12, body>=0.60, counter_wick<=0.30, return<=-0.20
        if mode == "B" and _hhmm >= 1200:
            if (_body_fraction >= 0.60 and _counter_wick <= 0.30 and _bar_return_pct <= -0.20):
                _is_pm_continuation = True
                quality_tier = QualityTier.A_TIER  # bypass external gate

        # Premium late-day sleeve: hour>=13, body>=0.55, return<=-0.15, rel_impulse<=-0.10
        if mode == "B" and _hhmm >= 1300:
            if (_body_fraction >= 0.55 and _bar_return_pct <= -0.15 and _rel_impulse <= -0.10):
                _is_pm_premium = True
                quality_tier = QualityTier.A_TIER  # bypass external gate

        return RawSignal(
            strategy_name=f"ORH_FBO_V2_{mode}",
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
                "or_high": round(or_high, 2),
                "vwap_at_signal": round(vwap, 2) if not _isnan(vwap) else None,
                "regime": regime_label,
                "structure_quality": round(struct_q, 3) if self.rejection and self.quality else None,
                "confluence": confluence,
                "quality_tier": quality_tier.value,
                "body_fraction": round(_body_fraction, 3),
                "counter_wick_fraction": round(_counter_wick, 3),
                "bar_return_pct": round(_bar_return_pct, 3),
                "relative_impulse_vs_spy": round(_rel_impulse, 4),
                "orh_b_is_elite": _is_elite,
                "orh_b_is_pm_continuation": _is_pm_continuation,
                "orh_b_is_pm_premium": _is_pm_premium,
                "orh_b_confluence_premium": _confluence_premium,
                "reject_reasons": reject_reasons,
                "in_play_score": snap.in_play_score,
                **_stop_meta,
            },
        )

    @staticmethod
    def _get_regime_label(market_ctx) -> str:
        """Derive day regime label from market context."""
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
