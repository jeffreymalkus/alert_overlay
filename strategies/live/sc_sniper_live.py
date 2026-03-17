"""
SC_SNIPER — Live incremental version.

Second Chance Sniper: Breakout → Retest → Confirmation.
Long only. Detects clean breakout above key level (OR high or swing high),
waits for orderly retest, then fires on confirmation bar.

State machine phases:
  IDLE → BREAKOUT_DETECTED → RETEST_CONFIRMED → SIGNAL (or EXPIRE)
"""

import math
from collections import deque
from typing import Optional, List

from ...models import Bar, NaN
from ..shared.config import StrategyConfig
from ..shared.rejection_filters import RejectionFilters
from ..shared.quality_scoring import QualityScorer
from ..shared.signal_schema import QualityTier
from ..shared.helpers import trigger_bar_quality
from .shared_indicators import IndicatorSnapshot
from .base import LiveStrategy, RawSignal

_isnan = math.isnan


def _median(vals: list) -> float:
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


class SCSniperLive(LiveStrategy):
    """Incremental SC_SNIPER for live pipeline."""

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True):
        super().__init__(name="SC_SNIPER", direction=1, enabled=enabled,
                         skip_rejections=["distance", "bigger_picture"])
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality

        # Config params (resolved for timeframe)
        self._time_start = cfg.get(cfg.sc_time_start)
        self._time_end = cfg.get(cfg.sc_time_end)
        self._retest_window = cfg.get(cfg.sc_retest_window)
        self._confirm_window = cfg.get(cfg.sc_confirm_window)

        # Private state machine — reset each day
        self._init_state()

    def _init_state(self):
        """Initialize/reset all private state machine fields."""
        self._active = False
        self._level: float = NaN
        self._level_tag: str = ""
        self._bo_high: float = NaN
        self._bo_low: float = NaN
        self._bo_vol: float = 0.0
        self._bars_since_bo: int = 0
        self._retested: bool = False
        self._bars_since_retest: int = 999
        self._retest_bar_high: float = NaN
        self._retest_bar_low: float = NaN
        self._lowest_since_retest: float = NaN
        self._triggered_today: bool = False
        self._range_buf: deque = deque(maxlen=10)

    def reset_day(self):
        """Reset for new trading day."""
        self._init_state()

    def step(self, snap: IndicatorSnapshot,
             market_ctx=None) -> Optional[RawSignal]:
        """Process one bar through SC_SNIPER state machine.

        Reads shared indicators from snap (read-only).
        Updates only self._ fields (private state).
        Returns RawSignal if confirmation fires, else None.
        """
        cfg = self.cfg
        hhmm = snap.hhmm
        bar = snap.bar
        e9 = snap.ema9
        vw = snap.vwap
        i_atr = snap.atr
        vol_ma = snap.vol_ma20

        # Track range for median range check
        rng = bar.high - bar.low
        self._range_buf.append(rng)

        # ── Gate checks ──
        if self._triggered_today:
            return None
        if not snap.ema9_ready or _isnan(i_atr) or i_atr <= 0:
            return None
        if _isnan(vol_ma) or vol_ma <= 0:
            return None

        # Tick state even outside time window
        if not (self._time_start <= hhmm <= self._time_end):
            if self._active:
                self._bars_since_bo += 1
                if self._bars_since_bo > 12:
                    self._active = False
            return None

        bar_bullish = bar.close > bar.open

        # ── Step 1: Detect breakout ──
        if not self._active:
            key_level, tag = self._find_key_level(snap)
            if _isnan(key_level):
                return None

            broke = bar.close > key_level + cfg.sc_break_atr_min * i_atr
            if not broke:
                return None

            # Range check
            if len(self._range_buf) >= 5:
                med_rng = _median(list(self._range_buf))
                if rng < cfg.sc_break_bar_range_frac * med_rng:
                    return None

            # Volume checks
            if bar.volume < cfg.sc_break_vol_frac * vol_ma:
                return None
            if bar.volume < cfg.sc_strong_bo_vol_mult * vol_ma:
                return None

            # Close position
            if rng > 0:
                close_pct = (bar.close - bar.low) / rng
                if close_pct < cfg.sc_break_close_pct:
                    return None

            # Latch breakout
            self._active = True
            self._level = key_level
            self._level_tag = tag
            self._bo_high = bar.high
            self._bo_low = bar.low
            self._bo_vol = bar.volume
            self._bars_since_bo = 0
            self._retested = False
            self._bars_since_retest = 999
            self._retest_bar_high = NaN
            self._retest_bar_low = NaN
            self._lowest_since_retest = NaN
            return None

        # ── Active breakout ──
        self._bars_since_bo += 1

        # Expire
        if self._bars_since_bo > self._retest_window + self._confirm_window + 1:
            self._active = False
            return None

        # ── Step 2: Detect retest ──
        if not self._retested and self._bars_since_bo <= self._retest_window:
            proximity = cfg.sc_retest_proximity_atr * i_atr
            max_depth = cfg.sc_retest_max_depth_atr * i_atr

            touches = bar.low <= self._level + proximity
            holds = bar.close > self._level
            not_deep = bar.low >= self._level - max_depth
            not_bearish_exp = not (bar.close < bar.open and rng > 1.5 * i_atr)
            vol_ok = bar.volume <= cfg.sc_retest_max_vol_frac * vol_ma

            if touches and holds and not_deep and not_bearish_exp and vol_ok:
                self._retested = True
                self._retest_bar_high = bar.high
                self._retest_bar_low = bar.low
                self._bars_since_retest = 0
                self._lowest_since_retest = bar.low
                return None

        # ── Step 3: Confirmation ──
        if self._retested:
            self._bars_since_retest += 1

            # Track lowest since retest
            if _isnan(self._lowest_since_retest) or bar.low < self._lowest_since_retest:
                self._lowest_since_retest = bar.low

            if self._bars_since_retest > self._confirm_window:
                self._active = False
                return None

            confirmed = (bar.close > self._retest_bar_high and
                         bar_bullish and
                         bar.volume >= cfg.sc_confirm_vol_frac * vol_ma and
                         bar.close > vw and bar.close > e9)

            if confirmed:
                # Compute stop
                raw_stop = min(self._retest_bar_low,
                               self._lowest_since_retest if not _isnan(self._lowest_since_retest)
                               else self._retest_bar_low)
                stop = raw_stop - cfg.sc_stop_buffer
                min_stop = 0.15 * i_atr
                _floor_applied = False
                if abs(bar.close - stop) < min_stop:
                    _floor_applied = True
                    stop = bar.close - min_stop
                # Stop source-truth
                _stop_meta = {
                    "stop_ref_type": "retest_bar_low/lowest_since_retest",
                    "stop_ref_price": raw_stop,
                    "raw_stop": raw_stop - cfg.sc_stop_buffer,
                    "buffer_type": "dollar",
                    "buffer_value": cfg.sc_stop_buffer,
                    "min_stop_rule_applied": _floor_applied,
                    "min_stop_distance": min_stop,
                    "final_stop": stop,
                }

                risk = bar.close - stop
                if risk <= 0:
                    self._active = False
                    return None

                # Structural target: measured move, session high, PDH
                # Measured move = entry + (breakout high - retest low)
                # Primary candidate for breakout-to-new-highs entries
                if cfg.sc_target_mode == "structural":
                    _candidates = []
                    retest_ref = self._retest_bar_low if not _isnan(self._retest_bar_low) else self._level
                    mm = bar.close + (self._bo_high - retest_ref)
                    if mm > bar.close:
                        _candidates.append((mm, "measured_move"))
                    if not _isnan(snap.session_high) and snap.session_high > bar.close:
                        _candidates.append((snap.session_high, "session_high"))
                    if not _isnan(snap.prior_day_high) and snap.prior_day_high > bar.close:
                        _candidates.append((snap.prior_day_high, "pdh"))
                    from ..shared.helpers import compute_structural_target_long
                    target, actual_rr, target_tag, skipped = compute_structural_target_long(
                        bar.close, risk, _candidates,
                        min_rr=cfg.sc_struct_min_rr, max_rr=cfg.sc_struct_max_rr,
                        fallback_rr=cfg.sc_target_rr, mode="structural",
                    )
                    if skipped:
                        self._active = False
                        return None
                else:
                    target = bar.close + risk * cfg.sc_target_rr
                    actual_rr = cfg.sc_target_rr
                    target_tag = "fixed_rr"

                # Structure quality
                struct_q = 0.5
                if self._level_tag == "ORH":
                    struct_q += 0.2
                if self._bo_vol >= 1.25 * vol_ma:
                    struct_q += 0.15
                if bar.volume < self._bo_vol:
                    struct_q += 0.15

                confluence = []
                if bar.close > vw:
                    confluence.append("above_vwap")
                if bar.close > e9:
                    confluence.append("above_ema9")
                if self._level_tag == "ORH":
                    confluence.append("or_level")
                if self._bo_vol >= 1.5 * vol_ma:
                    confluence.append("strong_bo_vol")

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
                            "rs_market": snap.rs_market,  # computed at manager level
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
                    strategy_name="SC_SNIPER",
                    direction=1,
                    entry_price=bar.close,
                    stop_price=stop,
                    target_price=target,
                    bar_idx=snap.bar_idx,
                    hhmm=hhmm,
                    quality=quality_score,
                    metadata={
                        "level": self._level,
                        "level_tag": self._level_tag,
                        "bo_vol": self._bo_vol,
                        "retest_low": self._retest_bar_low,
                        "bars_since_bo": self._bars_since_bo,
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

                self._active = False
                self._triggered_today = True
                return signal

        return None

    def _find_key_level(self, snap: IndicatorSnapshot) -> tuple:
        """Find breakout level from OR high or swing high.

        Uses shared indicators' recent_bars and OR state.
        """
        candidates = []

        if snap.or_ready and not _isnan(snap.or_high):
            candidates.append((snap.or_high, "ORH"))

        # Swing high from recent bars (exclude current bar)
        recent = snap.recent_bars
        if len(recent) >= 6:
            # Find highest high in prior bars (exclude last = current)
            prior_bars = recent[:-1]
            swing_high = max(b.high for b in prior_bars)
            # Only use if different from OR high
            if not snap.or_ready or _isnan(snap.or_high) or abs(swing_high - snap.or_high) > 0.01:
                candidates.append((swing_high, "SWING"))

        if not candidates:
            return NaN, ""

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0]
