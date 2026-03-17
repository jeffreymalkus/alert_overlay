"""
SP_ATIER — Live incremental version.

Spencer A-Tier: Uptrend → tight consolidation box → breakout above box high.
Long only. Requires EMA9 > EMA20, price above VWAP, box scan on each bar.

State machine: On each bar, check preconditions then scan recent_bars for
a valid consolidation box. If box found + breakout triggers, emit signal.
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


class SpencerATierLive(LiveStrategy):
    """Incremental SP_ATIER for live pipeline."""

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True):
        super().__init__(name="SP_ATIER", direction=1, enabled=enabled,
                         skip_rejections=["distance", "bigger_picture"])
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality

        self._time_start = cfg.get(cfg.sp_time_start)
        self._time_end = cfg.get(cfg.sp_time_end)
        self._box_min = cfg.get(cfg.sp_box_min_bars)
        self._box_max = cfg.get(cfg.sp_box_max_bars)

        self._init_state()

    def _init_state(self):
        self._triggered: bool = False
        self._prior_e9: float = NaN
        # Per-bar EMA9 values stored on bars for box below-EMA9 check
        self._e9_by_idx: deque = deque(maxlen=self._box_max + 5)

    def reset_day(self):
        self._init_state()

    def step(self, snap: IndicatorSnapshot,
             market_ctx=None) -> Optional[RawSignal]:
        cfg = self.cfg
        hhmm = snap.hhmm
        bar = snap.bar
        e9 = snap.ema9
        e20 = snap.ema20
        vw = snap.vwap
        i_atr = snap.atr
        vol_ma = snap.vol_ma20

        # Store EMA9 for this bar index
        self._e9_by_idx.append(e9)

        if self._triggered:
            self._prior_e9 = e9
            return None
        if not snap.ema9_ready or not snap.ema20_ready:
            self._prior_e9 = e9
            return None
        if _isnan(i_atr) or i_atr <= 0 or _isnan(vol_ma) or vol_ma <= 0:
            self._prior_e9 = e9
            return None
        if not (self._time_start <= hhmm <= self._time_end):
            self._prior_e9 = e9
            return None

        # ── Preconditions ──
        # EMA alignment
        if not (e9 > e20):
            self._prior_e9 = e9
            return None

        # EMA9 slope positive (track but don't block — confluence bonus later)
        ema9_rising = not _isnan(self._prior_e9) and e9 > self._prior_e9

        # Price above VWAP
        if bar.close <= vw:
            self._prior_e9 = e9
            return None

        # Daily ATR for trend advance + extension (matches replay's d_atr usage)
        d_atr = snap.daily_atr if not _isnan(snap.daily_atr) and snap.daily_atr > 0 else i_atr

        # Extension filter (uses daily ATR like replay)
        if not _isnan(snap.session_open) and d_atr > 0:
            if (bar.close - snap.session_open) > cfg.sp_extension_atr * d_atr:
                self._prior_e9 = e9
                return None

        # Trend advance check (uses daily ATR for main check, intra ATR for VWAP alt)
        if not _isnan(snap.session_low) and d_atr > 0:
            advance = bar.close - snap.session_low
            if advance < cfg.sp_trend_advance_atr * d_atr:
                vwap_advance = bar.close - vw
                if vwap_advance < cfg.sp_trend_advance_vwap_atr * i_atr:
                    self._prior_e9 = e9
                    return None

        recent = snap.recent_bars

        # ── Box scan ──
        if len(recent) < self._box_min + 1:
            self._prior_e9 = e9
            return None

        best_box = None
        for window in range(self._box_min, min(self._box_max + 1, len(recent))):
            box_bars = recent[-(window + 1):-1]
            if len(box_bars) < self._box_min:
                continue

            box_high = max(b.high for b in box_bars)
            box_low = min(b.low for b in box_bars)
            box_range = box_high - box_low

            if box_range > cfg.sp_box_max_range_atr * i_atr:
                continue
            if box_range <= 0:
                continue

            # BOX TIGHTNESS: add day-range % check
            if not _isnan(snap.session_high) and not _isnan(snap.session_low):
                day_range_val = snap.session_high - snap.session_low
                if day_range_val > 0 and (box_range / day_range_val) > 0.20:
                    continue

            box_mid = (box_high + box_low) / 2
            upper_closes = sum(1 for b in box_bars if b.close >= box_mid)
            if upper_closes / len(box_bars) < cfg.sp_box_upper_close_pct:
                continue

            # Box midpoint in upper third of day range
            if not _isnan(snap.session_high) and not _isnan(snap.session_low):
                day_range = snap.session_high - snap.session_low
                if day_range > 0:
                    box_position = (box_mid - snap.session_low) / day_range
                    if box_position < 0.67:
                        continue

            # Max closes below EMA9 — use stored per-bar EMA9 values
            # _e9_by_idx aligns with recent_bars from SharedIndicators
            e9_list = list(self._e9_by_idx)
            # Box bars correspond to recent[-(window+1):-1], which maps to
            # e9_list[-(window+1):-1] if both deques are aligned
            if len(e9_list) >= window + 1:
                box_e9s = e9_list[-(window + 1):-1]
                below_ema9_count = sum(
                    1 for b, be9 in zip(box_bars, box_e9s)
                    if not _isnan(be9) and b.close < be9
                )
            else:
                below_ema9_count = sum(1 for b in box_bars if b.close < e9)
            if below_ema9_count > cfg.sp_box_max_below_ema9:
                continue

            avg_vol = sum(b.volume for b in box_bars) / len(box_bars)
            if avg_vol < cfg.sp_box_min_vol_frac * vol_ma:
                continue

            # PRE-BREAK VOLUME COLLAPSE: add confluence check
            # Detect if 1-2 bars before break have <70% of prior bar volume
            pre_break_collapse = False
            if len(box_bars) >= 2:
                for k in range(-2, 0):
                    if k-1 >= -len(box_bars):
                        if box_bars[k].volume < 0.70 * box_bars[k-1].volume:
                            pre_break_collapse = True
                            break

            # Failed breakout filter
            failed_bo = 0
            threshold = cfg.sp_box_failed_bo_atr * i_atr
            for b in box_bars:
                if b.high > box_high - threshold and b.close < box_high:
                    failed_bo += 1
            if failed_bo >= cfg.sp_box_failed_bo_limit:
                continue

            best_box = (box_high, box_low, box_range, len(box_bars), avg_vol, failed_bo, pre_break_collapse)
            break

        if best_box is None:
            self._prior_e9 = e9
            return None

        box_high, box_low, box_range, box_len, box_avg_vol, box_failed, pre_break_collapse = best_box

        # ── Breakout trigger ──
        # ENTRY: breakout trigger price instead of bar.close
        trigger_price = box_high + 0.01
        entry_price = max(trigger_price, bar.open)

        clearance = cfg.sp_break_clearance_atr * i_atr
        rng = bar.high - bar.low

        if bar.close <= box_high + clearance:
            self._prior_e9 = e9
            return None

        if bar.volume < cfg.sp_break_vol_frac * vol_ma:
            self._prior_e9 = e9
            return None

        if rng > 0:
            close_pct = (bar.close - bar.low) / rng
            if close_pct < cfg.sp_break_close_pct:
                self._prior_e9 = e9
                return None

        if bar.close <= vw or bar.close <= e9:
            self._prior_e9 = e9
            return None

        # ── Stop ──
        stop = box_low - cfg.sp_stop_buffer
        min_stop = 0.15 * i_atr
        min_stop_rule_applied = False
        if abs(entry_price - stop) < min_stop:
            stop = entry_price - min_stop
            min_stop_rule_applied = True

        _stop_meta = {
            "stop_ref_type": "box_low",
            "stop_ref_price": box_low,
            "raw_stop": box_low - cfg.sp_stop_buffer,
            "buffer_type": "dollar",
            "buffer_value": cfg.sp_stop_buffer,
            "min_stop_rule_applied": min_stop_rule_applied,
            "min_stop_distance": min_stop,
            "final_stop": stop,
        }

        risk = entry_price - stop
        if risk <= 0:
            self._prior_e9 = e9
            return None

        # Structural target: measured move (box_high + box_range), session high, PDH
        if cfg.sp_target_mode == "structural":
            _candidates = []
            # SMB source: measured moves only (1x, 2x, 3x box range)
            mm1 = box_high + box_range
            mm2 = box_high + 2.0 * box_range
            mm3 = box_high + 3.0 * box_range
            if mm1 > entry_price:
                _candidates.append((mm1, "measured_move_1x"))
            if mm2 > entry_price:
                _candidates.append((mm2, "measured_move_2x"))
            if mm3 > entry_price:
                _candidates.append((mm3, "measured_move_3x"))
            from ..shared.helpers import compute_structural_target_long
            target, actual_rr, target_tag, skipped = compute_structural_target_long(
                entry_price, risk, _candidates,
                min_rr=cfg.sp_struct_min_rr, max_rr=cfg.sp_struct_max_rr,
                fallback_rr=cfg.sp_target_rr, mode="structural",
            )
            if skipped:
                self._prior_e9 = e9
                return None
        else:
            target = entry_price + risk * cfg.sp_target_rr
            actual_rr = cfg.sp_target_rr
            target_tag = "fixed_rr"

        # ── Structure quality ──
        struct_q = 0.5
        if box_len >= 6:
            struct_q += 0.20
        elif box_len >= 5:
            struct_q += 0.10
        if i_atr > 0:
            if box_range <= 0.75 * i_atr:
                struct_q += 0.20
            elif box_range <= 1.25 * i_atr:
                struct_q += 0.10
        if vol_ma > 0 and bar.volume >= 1.25 * vol_ma:
            struct_q += 0.10
        if box_failed == 0:
            struct_q += 0.10

        confluence = []
        if bar.close > vw:
            confluence.append("above_vwap")
        if bar.close > e9:
            confluence.append("above_ema9")
        if i_atr > 0 and box_range < 1.0 * i_atr:
            confluence.append("tight_box")
        if vol_ma > 0 and bar.volume >= 1.5 * vol_ma:
            confluence.append("strong_bo_vol")
        if ema9_rising:
            confluence.append("ema9_rising")
        if pre_break_collapse:
            confluence.append("pre_break_vol_collapse")

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

        signal = RawSignal(
            strategy_name="SP_ATIER",
            direction=1,
            entry_price=entry_price,
            stop_price=stop,
            target_price=target,
            bar_idx=snap.bar_idx,
            hhmm=hhmm,
            quality=quality_score,
            metadata={
                "entry_type": "breakout_trigger",
                "trigger_price": trigger_price,
                "bar_open": bar.open,
                "box_high": box_high,
                "box_low": box_low,
                "box_range": box_range,
                "box_len": box_len,
                "box_avg_vol": box_avg_vol,
                "box_failed_bo": box_failed,
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

        self._triggered = True
        self._prior_e9 = e9
        return signal
