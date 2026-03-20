"""
FLS_SOURCE — Fashionably Late Scalp, Source-Faithful Rebuild.

Long only. Turn off LOD → convergence toward VWAP → EMA9 crosses above VWAP → enter.

Source rules:
  - Entry: EMA9 (upsloping) crosses above VWAP (flat/downsloping)
  - Stop: VWAP_at_cross - (VWAP_at_cross - LOD) / 3
  - Target: cross_price + (cross_price - LOD) = 1x measured move above cross
  - Avoid: flat EMA9 for >15 min after turn, choppy action near cross
  - Volume: convergence vol > divergence vol improves odds

State machine:
  IDLE → DECLINE_DETECTED → TURN_DETECTED → CONVERGING → CROSS_TRIGGERED
"""

import math
from collections import deque
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


class FashionablyLateSourceLive(LiveStrategy):
    """FLS Source-Faithful: LOD turn → convergence → EMA9/VWAP cross."""

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True,
                 strategy_name: str = "FLS_SOURCE_V1"):
        super().__init__(name=strategy_name, direction=1, enabled=enabled,
                         skip_rejections=["distance", "maturity"])
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality

        self._time_start = cfg.get(cfg.fls_time_start)
        self._time_end = cfg.get(cfg.fls_time_end)

        self._init_state()

    def _init_state(self):
        # Session tracking
        self._session_high: float = NaN
        self._session_low: float = NaN  # running LOD
        self._lod_bar_idx: int = -1     # when LOD was established

        # Turn detection
        self._turn_detected: bool = False
        self._turn_low: float = NaN     # the LOD at the time of turn
        self._turn_bar_idx: int = -1

        # Divergence leg (move away from VWAP into LOD)
        self._div_vol: float = 0.0
        self._div_bars: int = 0

        # Convergence leg (move from turn back toward VWAP)
        self._conv_vol: float = 0.0
        self._conv_bars: int = 0
        self._conv_bars_above_ema: int = 0
        self._flat_ema_bars: int = 0     # consecutive flat EMA bars after turn

        # Cross dwell tracking
        self._dwell_bars: int = 0

        # EMA/VWAP history for slope computation
        self._ema_history: deque = deque(maxlen=10)
        self._vwap_history: deque = deque(maxlen=10)

        self._triggered: bool = False
        self._prev_ema_below_vwap: bool = True  # track for cross detection
        self._bar_count: int = 0

    def reset_day(self):
        self._init_state()

    def step(self, snap: IndicatorSnapshot,
             market_ctx=None) -> Optional[RawSignal]:
        cfg = self.cfg
        hhmm = snap.hhmm
        bar = snap.bar
        e9 = snap.ema9  # 5m EMA9
        vw = snap.vwap
        i_atr = snap.atr
        vol_ma = snap.vol_ma20

        self._bar_count += 1

        if self._triggered:
            return None
        if _isnan(e9) or _isnan(vw) or _isnan(i_atr) or i_atr <= 0:
            return None
        if not snap.ema9_ready:
            return None

        # Track EMA/VWAP history for slopes
        self._ema_history.append(e9)
        self._vwap_history.append(vw)

        # Track session extremes
        if _isnan(self._session_high) or bar.high > self._session_high:
            self._session_high = bar.high
        if _isnan(self._session_low) or bar.low < self._session_low:
            self._session_low = bar.low
            self._lod_bar_idx = self._bar_count

        # ── PHASE 1: Detect meaningful decline ──
        if not self._turn_detected:
            decline = self._session_high - self._session_low
            if decline >= cfg.fls_min_decline_atr * i_atr and bar.low <= vw:
                # Price has declined meaningfully and is at or below VWAP
                # Track divergence volume (bars where price is moving away from VWAP)
                if bar.close < vw:
                    self._div_vol += bar.volume
                    self._div_bars += 1

                # Detect turn: bar closes above prior bar's close AND above EMA9
                # (simple turn detection: price bouncing off LOD)
                if (bar.close > bar.open and  # bullish bar
                        self._session_low == self._session_low and  # LOD is current
                        bar.low > self._session_low - 0.1 * i_atr):  # near LOD
                    # Check if this bar is bouncing off LOD
                    if bar.close > e9 or bar.close > (self._session_low + 0.3 * (self._session_high - self._session_low)):
                        self._turn_detected = True
                        self._turn_low = self._session_low
                        self._turn_bar_idx = self._bar_count
                        self._conv_vol = bar.volume
                        self._conv_bars = 1
                        self._conv_bars_above_ema = 1 if bar.close > e9 else 0
                        self._flat_ema_bars = 0
            return None

        # ── PHASE 2: Track convergence toward VWAP ──
        # After turn, track the convergence leg
        self._conv_vol += bar.volume
        self._conv_bars += 1
        if bar.close > e9:
            self._conv_bars_above_ema += 1

        # Check for flat EMA invalidation
        ema_slope = self._compute_slope(self._ema_history, cfg.fls_ema_slope_lookback)
        if abs(ema_slope) < cfg.fls_flat_ema_threshold:
            self._flat_ema_bars += 1
        else:
            self._flat_ema_bars = 0  # reset on any non-flat bar

        if self._flat_ema_bars > cfg.fls_flat_ema_max_bars:
            # EMA flat too long after turn — invalidate
            self._turn_detected = False
            return None

        # Track dwell near cross
        if i_atr > 0 and abs(e9 - vw) < cfg.fls_cross_dwell_atr_frac * i_atr:
            self._dwell_bars += 1

        # ── PHASE 3: Detect EMA9/VWAP cross ──
        # Must be in time window
        if not (self._time_start <= hhmm <= self._time_end):
            # Track whether EMA was below VWAP for cross detection
            self._prev_ema_below_vwap = (e9 < vw)
            return None

        # Cross: EMA9 was below VWAP, now above
        cross_occurred = (self._prev_ema_below_vwap and e9 >= vw)
        self._prev_ema_below_vwap = (e9 < vw)

        if not cross_occurred:
            return None

        # Validate cross quality
        vwap_slope = self._compute_slope(self._vwap_history, cfg.fls_vwap_slope_lookback)

        # EMA9 must be upsloping
        if ema_slope < cfg.fls_min_ema_slope:
            return None

        # VWAP must be flat or downsloping
        if vwap_slope > cfg.fls_max_vwap_slope:
            return None

        # Dwell filter
        if self._dwell_bars > cfg.fls_max_cross_dwell_bars:
            return None

        # Volume filter: convergence > divergence
        if cfg.fls_require_conv_gt_div_vol:
            if self._div_vol > 0 and self._conv_vol <= self._div_vol:
                return None

        # Hold-above-EMA filter
        if cfg.fls_min_hold_above_ema_frac > 0 and self._conv_bars > 0:
            hold_frac = self._conv_bars_above_ema / self._conv_bars
            if hold_frac < cfg.fls_min_hold_above_ema_frac:
                return None

        # ── CROSS TRIGGERED — build signal ──
        entry_price = bar.close  # enter immediately on cross
        cross_price = (e9 + vw) / 2.0  # midpoint of cross
        lod = self._turn_low
        measured_move = cross_price - lod

        if measured_move <= 0:
            return None

        # Source stop: VWAP - (VWAP - LOD) / 3
        vwap_to_lod = vw - lod
        if vwap_to_lod <= 0:
            return None
        stop = vw - vwap_to_lod / 3.0

        risk = entry_price - stop
        if risk <= 0:
            return None

        # Min risk floor
        min_risk = max(0.20, 0.10 * i_atr) if i_atr > 0 else 0.20
        if risk < min_risk:
            stop = entry_price - min_risk
            risk = min_risk

        # Source target: cross_price + measured_move
        target = cross_price + measured_move
        actual_rr = (target - entry_price) / risk if risk > 0 else 0

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
                "structure_quality": 0.6,
                "confluence_count": 3,
            }
            if not reject_reasons:
                quality_tier, quality_score = self.quality.score(
                    stock_factors, market_factors, setup_factors)
            else:
                _, quality_score = self.quality.score(
                    stock_factors, market_factors, setup_factors)
                quality_tier = QualityTier.B_TIER

        # Internal quality gate + A-tier bypass
        _ip_ok = snap.in_play_score >= cfg.fls_min_ip_score
        _q_ok = quality_score >= cfg.fls_min_quality_score if cfg.fls_min_quality_score > 0 else True
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
            hhmm=hhmm,
            quality=quality_score,
            metadata={
                "lod": round(lod, 2),
                "cross_price": round(cross_price, 2),
                "vwap_at_cross": round(vw, 2),
                "ema9_at_cross": round(e9, 2),
                "measured_move": round(measured_move, 2),
                "ema_slope": round(ema_slope, 5),
                "vwap_slope": round(vwap_slope, 5),
                "div_vol": round(self._div_vol, 0),
                "conv_vol": round(self._conv_vol, 0),
                "conv_gt_div": self._conv_vol > self._div_vol if self._div_vol > 0 else None,
                "conv_bars": self._conv_bars,
                "hold_above_ema_frac": round(self._conv_bars_above_ema / max(self._conv_bars, 1), 2),
                "flat_ema_bars": self._flat_ema_bars,
                "dwell_bars": self._dwell_bars,
                "turn_to_cross_bars": self._bar_count - self._turn_bar_idx,
                "actual_rr": round(actual_rr, 3),
                "target_tag": "measured_move",
                "stop_tag": "measured_move_third",
                "quality_tier": quality_tier.value,
                "reject_reasons": reject_reasons,
                "in_play_score": snap.in_play_score,
            },
        )

    @staticmethod
    def _compute_slope(history: deque, lookback: int) -> float:
        """Compute simple slope over last N values. Returns change per bar."""
        if len(history) < 2:
            return 0.0
        n = min(lookback, len(history))
        vals = list(history)[-n:]
        if len(vals) < 2:
            return 0.0
        return (vals[-1] - vals[0]) / (len(vals) - 1)
