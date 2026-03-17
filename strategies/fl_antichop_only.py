"""
FL_AntiChop_Only — Decline → LOD → Turn → VWAP convergence → EMA9 cross.
Adapts FL_Momentum_Rebuild from engine.py with enhanced anti-chop filtering.

Long-only. Meaningful decline from session high → higher-low turn → EMA9 crosses above VWAP.

Usage (via replay.py):
    from .fl_antichop_only import FLAntiChopStrategy
    strategy = FLAntiChopStrategy(cfg, in_play, regime, rejection, quality)
    signals = strategy.scan_day(symbol, bars, day, spy_bars, sector_bars)
"""

import math
from collections import deque
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from ..models import Bar, NaN
from ..indicators import EMA, VWAPCalc, ATRPair
from .shared.signal_schema import StrategySignal, StrategyTrade, QualityTier
from .shared.config import StrategyConfig
from .shared.in_play_proxy import InPlayProxy
from .shared.market_regime import EnhancedMarketRegime
from .shared.rejection_filters import RejectionFilters
from .shared.quality_scoring import QualityScorer
from .shared.helpers import (
    trigger_bar_quality, is_expansion_bar, bar_body_ratio,
    compute_rs_from_open, is_in_time_window, get_hhmm,
    simulate_strategy_trade, compute_structural_target_long,
)

_isnan = math.isnan


class FLAntiChopStrategy:
    """
    FL AntiChop: decline → turn → EMA9 crosses VWAP with anti-chop gate.

    Pipeline:
    1. In-play check (day level)
    2. Market regime check (day level)
    3. Raw FL detection (bar-by-bar state tracking)
    4. Anti-chop rejection (enhanced universal + strategy-specific chop filter)
    5. Quality scoring → tier
    6. A-tier only returned as tradeable
    """

    def __init__(self, cfg: StrategyConfig, in_play: InPlayProxy,
                 regime: EnhancedMarketRegime, rejection: RejectionFilters,
                 quality: QualityScorer):
        self.cfg = cfg
        self.in_play = in_play
        self.regime = regime
        self.rejection = rejection
        self.quality = quality

        # Pipeline counters
        self.stats = {
            "total_symbol_days": 0,
            "passed_in_play": 0,
            "passed_regime": 0,
            "raw_signals": 0,
            "passed_rejection": 0,
            "passed_antichop": 0,
            "a_tier": 0,
            "b_tier": 0,
            "c_tier": 0,
            "reject_reasons": {},
        }

    def scan_day(self, symbol: str, bars: List[Bar], day: date,
                 spy_bars: Optional[List[Bar]] = None,
                 sector_bars: Optional[List[Bar]] = None) -> List[StrategySignal]:
        """
        Run full pipeline for one symbol-day.
        Returns list of StrategySignals (all tiers, caller filters A-tier).
        """
        cfg = self.cfg
        self.stats["total_symbol_days"] += 1

        # ── Step 1: In-play check ──
        ip_pass, ip_score = self.in_play.is_in_play(symbol, day)
        if not ip_pass:
            return []
        self.stats["passed_in_play"] += 1

        # ── Step 2: Market regime check ──
        day_bars = [b for b in bars if b.timestamp.date() == day]
        if not day_bars:
            return []

        first_bar_ts = day_bars[0].timestamp
        if not self.regime.is_aligned_long(first_bar_ts):
            return []
        self.stats["passed_regime"] += 1

        # ── Step 3: Raw FL detection ──
        raw_signals = self._detect_fl_signals(symbol, bars, day, ip_score)

        # ── Steps 4-6: Filter + score ──
        results = []
        for sig, bar_idx, bar, atr, ema9, vwap, vol_ma, rvol in raw_signals:
            self.stats["raw_signals"] += 1

            # Step 4a: Universal rejection filters
            # Skip trigger_weakness: FL cross bar naturally has lower volume than decline bars
            # Skip distance: FL signals after deep declines are inherently far from VWAP;
            #   distance filter was rejecting 45 good trades (ablation: PF 1.40→1.06)
            reasons = self.rejection.check_all(
                bar, bars, bar_idx, atr, ema9, vwap, vol_ma,
                skip_filters=["trigger_weakness", "distance"]
            )

            # Step 4b: Enhanced anti-chop filter (strategy-specific, stricter)
            chop_pass, chop_reason = self._antichop_filter(bars, bar_idx, atr)
            if not chop_pass:
                reasons.append(chop_reason)

            sig.reject_reasons = reasons

            if reasons:
                for r in reasons:
                    key = r.split("(")[0]
                    self.stats["reject_reasons"][key] = self.stats["reject_reasons"].get(key, 0) + 1
            else:
                self.stats["passed_rejection"] += 1
                self.stats["passed_antichop"] += 1

            # Step 5: Quality scoring
            spy_pct = self.regime.get_spy_pct_from_open(bar.timestamp)
            day_open = day_bars[0].open
            rs_mkt = (bar.close - day_open) / day_open - spy_pct if day_open > 0 else 0.0
            sig.rs_market = rs_mkt

            regime_snap = self.regime.get_nearest_regime(bar.timestamp)
            regime_score = {"GREEN": 1.0, "FLAT": 0.5, "RED": 0.0}.get(
                regime_snap.day_label if regime_snap else "", 0.5
            )
            alignment_score = 0.0
            if regime_snap:
                if regime_snap.spy_above_vwap:
                    alignment_score += 0.5
                if regime_snap.ema9_above_ema20:
                    alignment_score += 0.5

            stock_factors = {
                "in_play_score": ip_score,
                "rs_market": rs_mkt,
                "rs_sector": 0.0,
                "volume_profile": min(rvol / 2.0, 1.0) if not _isnan(rvol) else 0.0,
            }
            market_factors = {
                "regime_score": regime_score,
                "alignment_score": alignment_score,
            }
            setup_factors = {
                "trigger_quality": trigger_bar_quality(bar, atr, vol_ma),
                "structure_quality": sig.metadata.get("structure_quality", 0.5),
                "confluence_count": len(sig.confluence_tags),
            }

            tier, score = self.quality.score(stock_factors, market_factors, setup_factors)

            if reasons:
                tier = QualityTier.B_TIER if score >= cfg.quality_b_min else QualityTier.C_TIER

            sig.quality_tier = tier
            sig.quality_score = score
            sig.market_regime = regime_snap.day_label if regime_snap else ""

            if tier == QualityTier.A_TIER:
                self.stats["a_tier"] += 1
            elif tier == QualityTier.B_TIER:
                self.stats["b_tier"] += 1
            else:
                self.stats["c_tier"] += 1

            results.append(sig)

        return results

    def _detect_fl_signals(self, symbol: str, bars: List[Bar], day: date,
                           ip_score: float) -> list:
        """
        Run FL state machine: decline → turn → cross.
        Returns list of (StrategySignal, bar_idx, bar, atr, ema9, vwap, vol_ma, rvol).
        """
        cfg = self.cfg
        time_start = cfg.get(cfg.fl_time_start)
        time_end = cfg.get(cfg.fl_time_end)
        turn_confirm_n = cfg.get(cfg.fl_turn_confirm_bars)
        max_base_bars = cfg.get(cfg.fl_max_base_bars)

        # Indicators
        ema9 = EMA(9)
        vwap_calc = VWAPCalc()
        atr_pair = ATRPair(14, use_completed=True)
        vol_buf = deque(maxlen=20)
        rvol_buf = deque(maxlen=20)  # for RVOL computation

        # State
        session_high = NaN
        prior_day_high = NaN
        decline_active = False
        decline_high = NaN
        decline_low = NaN
        decline_atr = 0.0
        turn_detected = False
        turn_confirm_bars = 0
        turn_low = NaN
        base_bars = 0
        cross_fired = False
        triggered = False
        prior_e9 = NaN
        prior_prior_e9 = NaN
        prior_vwap = NaN

        prev_date = None
        signals = []

        for i, bar in enumerate(bars):
            d = bar.timestamp.date()
            hhmm = get_hhmm(bar)

            # Day reset
            if d != prev_date:
                vwap_calc.reset()
                if not _isnan(session_high):
                    prior_day_high = session_high
                session_high = NaN
                decline_active = False
                decline_high = NaN
                decline_low = NaN
                decline_atr = 0.0
                turn_detected = False
                turn_confirm_bars = 0
                turn_low = NaN
                base_bars = 0
                cross_fired = False
                triggered = False
                prior_e9 = NaN
                prior_prior_e9 = NaN
                prior_vwap = NaN
                prev_date = d

            # Update indicators
            e9 = ema9.update(bar.close)
            i_atr = atr_pair.update_intraday(bar.high, bar.low, bar.close)
            tp = (bar.high + bar.low + bar.close) / 3.0
            vw = vwap_calc.update(tp, bar.volume)
            vol_buf.append(bar.volume)
            vol_ma = sum(vol_buf) / len(vol_buf) if len(vol_buf) >= 5 else NaN

            # RVOL for the day
            rvol_buf.append(bar.volume)
            rvol = bar.volume / vol_ma if (not _isnan(vol_ma) and vol_ma > 0) else NaN

            if day is not None and d != day:
                # Warmup: still update state tracking but don't search for signals
                prior_prior_e9 = prior_e9
                prior_e9 = e9
                prior_vwap = vw
                continue

            if _isnan(e9) or _isnan(vw) or _isnan(i_atr) or i_atr <= 0:
                prior_prior_e9 = prior_e9
                prior_e9 = e9
                prior_vwap = vw
                continue

            # ── Track session high ──
            if _isnan(session_high) or bar.high > session_high:
                session_high = bar.high

            # ── Phase 1: Decline detection ──
            if not _isnan(session_high):
                decline_dist = session_high - bar.low
                curr_decline_atr = decline_dist / i_atr if i_atr > 0 else 0.0

                if curr_decline_atr >= cfg.fl_min_decline_atr and not decline_active:
                    decline_active = True
                    decline_high = session_high
                    decline_low = bar.low
                    decline_atr = curr_decline_atr

                if decline_active and bar.low < decline_low:
                    decline_low = bar.low
                    decline_atr = (decline_high - bar.low) / i_atr

            # ── Phase 2: Turn detection ──
            if decline_active and not turn_detected:
                hl_threshold = decline_low + cfg.fl_hl_tolerance_atr * i_atr
                if bar.low > hl_threshold and bar.close > bar.open:
                    turn_confirm_bars += 1
                    if turn_confirm_bars >= turn_confirm_n:
                        turn_detected = True
                        turn_low = bar.low
                        base_bars = 0
                else:
                    turn_confirm_bars = 0

            # Base bar counting
            if turn_detected and not cross_fired:
                base_bars += 1
                if base_bars > max_base_bars:
                    turn_detected = False
                    base_bars = 0

            # ── Phase 3: Cross detection ──
            if not _isnan(prior_e9) and not _isnan(prior_vwap):
                if prior_e9 <= prior_vwap and e9 > vw:
                    cross_fired = True

            # ── Signal: cross fired + turn detected + decline active + in time window ──
            if (cross_fired and turn_detected and decline_active and
                    not triggered and time_start <= hhmm <= time_end):

                # Gate: cross bar close above VWAP
                if cfg.fl_cross_close_above_vwap and bar.close <= vw:
                    prior_prior_e9 = prior_e9
                    prior_e9 = e9
                    prior_vwap = vw
                    continue

                # Gate: clean cross bar body
                bar_range = bar.high - bar.low
                if bar_range > 0:
                    body_pct = abs(bar.close - bar.open) / bar_range
                    if body_pct < cfg.fl_cross_body_pct:
                        prior_prior_e9 = prior_e9
                        prior_e9 = e9
                        prior_vwap = vw
                        continue

                # Gate: cross bar volume
                if not _isnan(rvol) and rvol < cfg.fl_cross_vol_min_rvol:
                    prior_prior_e9 = prior_e9
                    prior_e9 = e9
                    prior_vwap = vw
                    continue

                # Compute stop — selectable mode (aligned with live version)
                _fl_stop_mode = getattr(cfg, "fl_stop_mode", "current_hybrid")
                if _fl_stop_mode == "source_faithful":
                    # Source material: stop = VWAP - (VWAP - LOD) / 3
                    _vwap_for_stop = vw if not _isnan(vw) else bar.close
                    _lod = decline_low if not _isnan(decline_low) else bar.low
                    stop = _vwap_for_stop - (_vwap_for_stop - _lod) / 3.0
                    # No ATR floor in source-faithful mode
                else:
                    # Current hybrid: turn_low - ATR buffer, with ATR floor
                    if not _isnan(turn_low):
                        stop = turn_low - cfg.fl_stop_buffer_atr * i_atr
                    else:
                        measured_move = decline_high - decline_low
                        stop_dist = measured_move * cfg.fl_stop_frac
                        stop = bar.close - stop_dist
                    min_stop = 0.15 * i_atr
                    if bar.close - stop < min_stop:
                        stop = bar.close - min_stop

                if stop >= bar.close:
                    prior_prior_e9 = prior_e9
                    prior_e9 = e9
                    prior_vwap = vw
                    continue

                risk = bar.close - stop

                # Structural target: VWAP (primary for mean-reversion), decline_high, session_high, PDH
                if cfg.fl_target_mode == "structural":
                    _candidates = []
                    # VWAP is the natural mean-reversion target (primary per spec)
                    if not _isnan(vw) and vw > bar.close:
                        _candidates.append((vw, "vwap"))
                    if not _isnan(decline_high) and decline_high > bar.close:
                        _candidates.append((decline_high, "decline_high"))
                    if not _isnan(session_high) and session_high > bar.close:
                        _candidates.append((session_high, "session_high"))
                    if not _isnan(prior_day_high) and prior_day_high > bar.close:
                        _candidates.append((prior_day_high, "pdh"))
                    target, actual_rr, target_tag, skipped = compute_structural_target_long(
                        bar.close, risk, _candidates,
                        min_rr=cfg.fl_struct_min_rr, max_rr=cfg.fl_struct_max_rr,
                        fallback_rr=cfg.fl_target_rr, mode="structural",
                    )
                    if skipped:
                        prior_prior_e9 = prior_e9
                        prior_e9 = e9
                        prior_vwap = vw
                        continue
                else:
                    target = bar.close + risk * cfg.fl_target_rr
                    actual_rr = cfg.fl_target_rr
                    target_tag = "fixed_rr"

                # Structure quality
                struct_q = 0.5
                if decline_atr > cfg.fl_min_decline_atr + 1.0:
                    struct_q += 0.15  # deeper decline = stronger
                if turn_confirm_bars >= turn_confirm_n + 1:
                    struct_q += 0.15  # extra turn bars = more conviction
                if bar.close > vw:
                    struct_q += 0.1
                if not _isnan(prior_prior_e9):
                    slope_cur = (e9 - prior_e9) / i_atr
                    slope_prev = (prior_e9 - prior_prior_e9) / i_atr
                    if slope_cur > slope_prev:
                        struct_q += 0.1  # accelerating EMA

                confluence = []
                if bar.close > vw:
                    confluence.append("above_vwap")
                if bar.close > e9:
                    confluence.append("above_ema9")
                if decline_atr > 4.0:
                    confluence.append("deep_decline")
                if not _isnan(rvol) and rvol >= 1.5:
                    confluence.append("strong_rvol")

                sig = StrategySignal(
                    strategy_name="FL_ANTICHOP",
                    symbol=symbol,
                    timestamp=bar.timestamp,
                    direction=1,
                    entry_price=bar.close,
                    stop_price=stop,
                    target_price=target,
                    in_play_score=ip_score,
                    confluence_tags=confluence,
                    metadata={
                        "decline_atr": decline_atr,
                        "decline_high": decline_high,
                        "decline_low": decline_low,
                        "turn_bars": turn_confirm_bars,
                        "base_bars": base_bars,
                        "actual_rr": actual_rr,
                        "target_tag": target_tag,
                        "structure_quality": min(struct_q, 1.0),
                    },
                )

                signals.append((sig, i, bar, i_atr, e9, vw, vol_ma, rvol))
                triggered = True

            prior_prior_e9 = prior_e9
            prior_e9 = e9
            prior_vwap = vw

        return signals

    def _antichop_filter(self, bars: List[Bar], bar_idx: int,
                         atr: float) -> Tuple[bool, str]:
        """
        Enhanced anti-chop filter — stricter than universal choppiness.
        Looks at recent bars for excessive range overlap.
        """
        cfg = self.cfg
        lookback = cfg.get(cfg.fl_chop_lookback)
        max_overlap = cfg.fl_chop_overlap_max

        if bar_idx < lookback + 1 or atr <= 0:
            return True, ""

        start = max(0, bar_idx - lookback)
        window = bars[start:bar_idx + 1]

        if len(window) < 3:
            return True, ""

        total_range = 0.0
        total_overlap = 0.0
        for j in range(1, len(window)):
            prev_b = window[j - 1]
            curr_b = window[j]
            prev_range = prev_b.high - prev_b.low
            curr_range = curr_b.high - curr_b.low
            total_range += prev_range + curr_range

            overlap_high = min(prev_b.high, curr_b.high)
            overlap_low = max(prev_b.low, curr_b.low)
            overlap = max(0, overlap_high - overlap_low)
            total_overlap += overlap

        if total_range <= 0:
            return True, ""

        overlap_ratio = total_overlap / (total_range / 2.0)

        if overlap_ratio > max_overlap:
            return False, f"antichop({overlap_ratio:.2f}>{max_overlap:.2f})"

        return True, ""
