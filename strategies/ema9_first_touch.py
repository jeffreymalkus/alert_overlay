"""
EMA9_FirstTouch_Only — First meaningful pullback to 9 EMA after opening drive.

3-phase state machine:
  Phase 1 (Drive):    Detect opening drive (price extends from open, above VWAP)
  Phase 2 (Pullback): First pullback touches/dips to 9 EMA zone
  Phase 3 (Trigger):  First close back above 9 EMA with strong bar

Long-only. Time window: 09:35-11:15 ET.

Key requirements from spec:
  - Stock above VWAP, above/supported by 5m 9 EMA, positive RS vs SPY
  - FIRST meaningful pullback only, pullback must not lose VWAP
  - Stop below pullback low, target via fixed R

Usage (via replay.py):
    from .ema9_first_touch import EMA9FirstTouchStrategy
    strategy = EMA9FirstTouchStrategy(cfg, in_play, regime, rejection, quality)
    signals = strategy.scan_day(symbol, bars, day, spy_bars, sector_bars)
"""

import math
from collections import deque
from datetime import date, datetime
from typing import Dict, List, Optional

from ..models import Bar, NaN
from ..indicators import EMA, VWAPCalc, ATRPair
from .shared.signal_schema import StrategySignal, StrategyTrade, QualityTier
from .shared.config import StrategyConfig
from .shared.in_play_proxy import InPlayProxy
from .shared.market_regime import EnhancedMarketRegime
from .shared.rejection_filters import RejectionFilters
from .shared.quality_scoring import QualityScorer
from .shared.helpers import (
    trigger_bar_quality, bar_body_ratio,
    compute_rs_from_open, is_in_time_window, get_hhmm,
    compute_structural_target_long,
)

_isnan = math.isnan


class EMA9FirstTouchStrategy:
    """
    EMA9 FirstTouch Only: opening drive → first pullback to E9 → reclaim entry.

    Pipeline:
    1. In-play check (day level)
    2. Market regime check (GREEN day)
    3. Raw EMA9FT detection (3-phase state machine)
    4. Universal rejection filters
    5. Quality scoring → tier
    6. A-tier only returned as tradeable
    """

    def __init__(self, cfg: StrategyConfig, in_play: InPlayProxy,
                 regime: EnhancedMarketRegime, rejection: RejectionFilters,
                 quality: QualityScorer, strategy_name: str = "EMA9_FT"):
        self.cfg = cfg
        self._strategy_name = strategy_name
        self.in_play = in_play
        self.regime = regime
        self.rejection = rejection
        self.quality = quality

        self.stats = {
            "total_symbol_days": 0,
            "passed_in_play": 0,
            "passed_regime": 0,
            "raw_signals": 0,
            "passed_rejection": 0,
            "a_tier": 0,
            "b_tier": 0,
            "c_tier": 0,
            "reject_reasons": {},
        }

    def scan_day(self, symbol: str, bars: List[Bar], day: date,
                 spy_bars: Optional[List[Bar]] = None,
                 sector_bars: Optional[List[Bar]] = None) -> List[StrategySignal]:
        """Run full pipeline for one symbol-day."""
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

        # ── Step 3: Raw EMA9FT detection ──
        raw_signals = self._detect_e9ft_signals(symbol, bars, day, ip_score, spy_bars)

        # ── Steps 4-6: Filter + score ──
        results = []
        for sig, bar_idx, bar, atr, ema9, vwap, vol_ma in raw_signals:
            self.stats["raw_signals"] += 1

            # Step 4: Rejection filters
            # Skip distance: opening drive pushes price above VWAP by design
            # Skip maturity: early-day strategy, maturity would over-filter
            # Skip trigger_weakness: pullback vol contraction is a FEATURE of this setup;
            #   trigger bar after pullback often has lower vol (vol returns on continuation)
            reasons = self.rejection.check_all(
                bar, bars, bar_idx, atr, ema9, vwap, vol_ma,
                skip_filters=["distance", "maturity", "trigger_weakness"]
            )
            sig.reject_reasons = reasons

            if reasons:
                for r in reasons:
                    key = r.split("(")[0]
                    self.stats["reject_reasons"][key] = self.stats["reject_reasons"].get(key, 0) + 1
            else:
                self.stats["passed_rejection"] += 1

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
                "volume_profile": min(bar.volume / vol_ma, 1.0) if vol_ma > 0 else 0.0,
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

    def _detect_e9ft_signals(self, symbol: str, bars: List[Bar], day: date,
                              ip_score: float,
                              spy_bars: Optional[List[Bar]] = None) -> list:
        """
        Run EMA9 FirstTouch 3-phase state machine.

        Phase 1: Opening drive detection (price extends from open above VWAP)
        Phase 2: First pullback to 9 EMA zone (must hold VWAP)
        Phase 3: First close back above 9 EMA (trigger)

        Returns list of (StrategySignal, bar_idx, bar, atr, ema9, vwap, vol_ma).
        """
        cfg = self.cfg
        time_start = cfg.get(cfg.e9ft_time_start)
        time_end = cfg.get(cfg.e9ft_time_end)

        # Indicators
        ema9 = EMA(9)
        ema20 = EMA(20)
        vwap_calc = VWAPCalc()
        atr_pair = ATRPair(14, use_completed=True)
        vol_buf = deque(maxlen=20)

        # SPY tracking for RS computation
        spy_open_today = NaN
        spy_close_latest = NaN
        if spy_bars:
            for sb in spy_bars:
                if sb.timestamp.date() == day:
                    if _isnan(spy_open_today):
                        spy_open_today = sb.open
                    spy_close_latest = sb.close

        # Day state
        session_open = NaN
        session_high = NaN
        session_low = NaN
        drive_confirmed = False
        drive_high = NaN
        pullback_started = False
        pullback_low = NaN
        pullback_touched_e9 = False
        triggered = False
        prev_date = None
        prev_bar = None
        prev_prev_bar = None

        signals = []

        for i, bar in enumerate(bars):
            d = bar.timestamp.date()
            hhmm = get_hhmm(bar)

            # Day reset
            if d != prev_date:
                vwap_calc.reset()
                session_open = NaN
                session_high = NaN
                session_low = NaN
                drive_confirmed = False
                drive_high = NaN
                pullback_started = False
                pullback_low = NaN
                pullback_touched_e9 = False
                triggered = False
                prev_date = d

            # Update indicators
            e9 = ema9.update(bar.close)
            e20 = ema20.update(bar.close)
            i_atr = atr_pair.update_intraday(bar.high, bar.low, bar.close)
            tp = (bar.high + bar.low + bar.close) / 3.0
            vw = vwap_calc.update(tp, bar.volume)
            vol_buf.append(bar.volume)
            vol_ma = sum(vol_buf) / len(vol_buf) if len(vol_buf) >= 5 else NaN

            # Track session
            if _isnan(session_open):
                session_open = bar.open
            if _isnan(session_high) or bar.high > session_high:
                session_high = bar.high
            if _isnan(session_low) or bar.low < session_low:
                session_low = bar.low

            if d != day:
                continue

            if _isnan(i_atr) or i_atr <= 0 or triggered:
                continue
            if not ema9.ready or not ema20.ready:
                continue
            if _isnan(vol_ma) or vol_ma <= 0:
                continue

            # ── Phase 1: Opening drive detection ──
            if not drive_confirmed:
                # Need price to extend from open
                if not _isnan(session_open) and i_atr > 0:
                    drive_dist = bar.high - session_open
                    if drive_dist >= cfg.e9ft_drive_min_atr * i_atr:
                        # Check above VWAP
                        if not _isnan(vw) and bar.close > vw:
                            # Check E9 > E20 structure
                            if not cfg.e9ft_ema9_above_ema20 or e9 > e20:
                                drive_confirmed = True
                                drive_high = session_high
                continue

            # Update drive high while price still extending
            if not pullback_started:
                if bar.high > drive_high:
                    drive_high = bar.high

                # Detect start of pullback: bar dips toward E9
                # Pullback starts when bar.low comes within reach of E9
                # or when bar.close < prev bar.high (stops making new highs)
                near_e9 = bar.low <= e9 + 0.20 * i_atr
                if near_e9:
                    pullback_started = True
                    pullback_low = bar.low
                    pullback_touched_e9 = True
                elif bar.close < drive_high - 0.15 * i_atr:
                    # Price pulling back but hasn't reached E9 yet
                    pullback_started = True
                    pullback_low = bar.low
                    pullback_touched_e9 = False
                else:
                    continue

            # ── Phase 2: Pullback tracking ──
            if pullback_started and not triggered:
                # Update pullback low
                if bar.low < pullback_low or _isnan(pullback_low):
                    pullback_low = bar.low

                # Check if pullback touched E9 zone
                if bar.low <= e9 + 0.20 * i_atr:
                    pullback_touched_e9 = True

                # Check pullback validity
                pb_depth = drive_high - pullback_low
                max_depth = cfg.e9ft_max_pullback_depth_atr * i_atr

                # Pullback too deep — invalidate
                if pb_depth > max_depth:
                    drive_confirmed = False
                    pullback_started = False
                    continue

                # Pullback must hold VWAP
                if cfg.e9ft_pullback_must_hold_vwap and not _isnan(vw):
                    if pullback_low < vw - 0.10 * i_atr:  # small tolerance
                        drive_confirmed = False
                        pullback_started = False
                        continue

                # ── Phase 3: Trigger check (must be in time window) ──
                # V4 time override
                if cfg.ema9_v4_enabled:
                    if not (cfg.ema9_v4_time_start <= hhmm <= cfg.ema9_v4_time_end):
                        prev_prev_bar = prev_bar
                        prev_bar = bar
                        continue
                else:
                    if not (time_start <= hhmm <= time_end):
                        prev_prev_bar = prev_bar
                        prev_bar = bar
                        continue

                if not pullback_touched_e9:
                    prev_prev_bar = prev_bar
                    prev_bar = bar
                    continue

                # Trigger: first close back above E9
                if bar.close > e9 and bar.close > bar.open:
                    rng = bar.high - bar.low
                    if rng > 0:
                        body_pct = abs(bar.close - bar.open) / rng
                        close_pct = (bar.close - bar.low) / rng
                    else:
                        body_pct = 0.0
                        close_pct = 0.0

                    if (body_pct >= cfg.e9ft_trigger_body_min_pct and
                            close_pct >= cfg.e9ft_trigger_close_pct):

                        # Must be above VWAP at entry
                        if cfg.e9ft_above_vwap and not _isnan(vw) and bar.close <= vw:
                            prev_prev_bar = prev_bar
                            prev_bar = bar
                            continue

                        # RS check: stock pct_from_open > SPY pct_from_open
                        stock_pct = (bar.close - session_open) / session_open if session_open > 0 else 0
                        spy_pct = 0.0
                        if spy_bars:
                            for sb in spy_bars:
                                if sb.timestamp <= bar.timestamp and sb.timestamp.date() == day:
                                    spy_pct = (sb.close - spy_open_today) / spy_open_today if not _isnan(spy_open_today) and spy_open_today > 0 else 0
                        rs = stock_pct - spy_pct

                        if rs < cfg.e9ft_min_rs_vs_spy:
                            prev_prev_bar = prev_bar
                            prev_bar = bar
                            continue

                        # V4: relative impulse vs SPY filter
                        relative_impulse_vs_spy = rs  # same as RS for now
                        if cfg.ema9_require_relative_impulse_vs_spy:
                            if relative_impulse_vs_spy < cfg.ema9_min_relative_impulse_vs_spy:
                                prev_prev_bar = prev_bar
                                prev_bar = bar
                                continue

                        # V4: optional soft trigger bar filter
                        if cfg.ema9_require_soft_trigger_bar:
                            if (close_pct > cfg.ema9_max_trigger_close_location or
                                    body_pct > cfg.ema9_max_trigger_body_fraction):
                                prev_prev_bar = prev_bar
                                prev_bar = bar
                                continue

                        # ── TRIGGER FIRES ──
                        entry_price = bar.close

                        # V4: stop model
                        if cfg.ema9_v4_enabled and cfg.ema9_stop_mode_v4 == "two_bar_low":
                            pb_low_ref = bar.low
                            if prev_bar is not None:
                                pb_low_ref = min(bar.low, prev_bar.low)
                            stop = pb_low_ref - cfg.ema9_stop_buffer_v4
                        elif cfg.ema9_v4_enabled and cfg.ema9_stop_mode_v4 == "setup_bar_low":
                            stop = bar.low - cfg.ema9_stop_buffer_v4
                        elif cfg.ema9_v4_enabled and cfg.ema9_stop_mode_v4 == "three_bar_low":
                            lows = [bar.low]
                            if prev_bar is not None:
                                lows.append(prev_bar.low)
                            if prev_prev_bar is not None:
                                lows.append(prev_prev_bar.low)
                            stop = min(lows) - cfg.ema9_stop_buffer_v4
                        else:
                            # Legacy stop
                            stop = pullback_low - cfg.e9ft_stop_buffer

                        min_stop = 0.15 * i_atr
                        if abs(entry_price - stop) < min_stop:
                            stop = entry_price - min_stop

                        risk = entry_price - stop
                        if risk <= 0:
                            drive_confirmed = False
                            pullback_started = False
                            prev_prev_bar = prev_bar
                            prev_bar = bar
                            continue

                        # V4: fixed RR target
                        if cfg.ema9_v4_enabled and cfg.ema9_target_mode_v4 == "fixed_rr":
                            target = entry_price + risk * cfg.ema9_target_rr_v4
                            actual_rr = cfg.ema9_target_rr_v4
                            target_tag = "fixed_rr"
                        elif cfg.e9ft_target_mode == "structural":
                            _candidates = []
                            if not _isnan(drive_high) and drive_high > entry_price:
                                _candidates.append((drive_high, "drive_high"))
                            if not _isnan(session_high) and session_high > entry_price:
                                _candidates.append((session_high, "session_high"))
                            target, actual_rr, target_tag, skipped = compute_structural_target_long(
                                entry_price, risk, _candidates,
                                min_rr=cfg.e9ft_struct_min_rr, max_rr=cfg.e9ft_struct_max_rr,
                                fallback_rr=cfg.e9ft_target_rr, mode="structural",
                            )
                            if skipped:
                                drive_confirmed = False
                                pullback_started = False
                                prev_prev_bar = prev_bar
                                prev_bar = bar
                                continue
                        else:
                            target = entry_price + risk * cfg.e9ft_target_rr
                            actual_rr = cfg.e9ft_target_rr
                            target_tag = "fixed_rr"

                        # Structure quality
                        struct_q = 0.40
                        if pb_depth < 0.30 * i_atr:
                            struct_q += 0.15
                        elif pb_depth < 0.45 * i_atr:
                            struct_q += 0.10
                        drive_dist = drive_high - session_open
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

                        sig = StrategySignal(
                            strategy_name=self._strategy_name,
                            symbol=symbol,
                            timestamp=bar.timestamp,
                            direction=1,
                            entry_price=entry_price,
                            stop_price=stop,
                            target_price=target,
                            in_play_score=ip_score,
                            confluence_tags=confluence,
                            metadata={
                                "drive_high": drive_high,
                                "drive_dist": drive_dist,
                                "pullback_low": pullback_low,
                                "pb_depth": pb_depth,
                                "pb_depth_atr": pb_depth / i_atr if i_atr > 0 else 0,
                                "rs_vs_spy": rs,
                                "relative_impulse_vs_spy": relative_impulse_vs_spy if cfg.ema9_v4_enabled else rs,
                                "close_location": close_pct,
                                "body_fraction": body_pct,
                                "structure_quality": min(struct_q, 1.0),
                                "actual_rr": actual_rr,
                                "target_tag": target_tag,
                                "stop_mode": cfg.ema9_stop_mode_v4 if cfg.ema9_v4_enabled else "pullback_low",
                            },
                        )

                        signals.append((sig, i, bar, i_atr, e9, vw, vol_ma))
                        triggered = True

                        if cfg.e9ft_first_pullback_only:
                            prev_prev_bar = prev_bar
                            prev_bar = bar
                            continue

            prev_prev_bar = prev_bar
            prev_bar = bar

        return signals
