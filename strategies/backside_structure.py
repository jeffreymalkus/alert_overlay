"""
Backside_Structure_Only — Rising structure after LOD, range above 9 EMA, target VWAP.

Multi-phase state machine:
  Phase 1 (Decline):   Detect meaningful extension below VWAP to LOD
  Phase 2 (Structure): At least 1 HH + 1 HL after LOD, above rising 9 EMA
  Phase 3 (Range):     Tight range forms above 9 EMA
  Phase 4 (Breakout):  Range breakout, target = VWAP

Long-only. Time window: 10:00-13:30 ET. One-and-done (single attempt).

Key requirements from spec:
  - Stock extended below VWAP, distinct LOD
  - At least one HH and one HL after LOD
  - Majority of bars above rising 9 EMA, range forms above 9 EMA
  - Range midpoint above configurable fraction of LOD→VWAP path
  - Stop $0.02 below most recent HL, target = VWAP
  - One and done (single attempt only)

Usage (via replay.py):
    from .backside_structure import BacksideStructureStrategy
    strategy = BacksideStructureStrategy(cfg, in_play, regime, rejection, quality)
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


class BacksideStructureStrategy:
    """
    Backside Structure Only: decline → HH/HL structure → range above E9 → breakout → VWAP.

    Pipeline:
    1. In-play check (day level)
    2. Market regime check (GREEN day)
    3. Raw Backside detection (multi-phase state machine)
    4. Universal rejection filters
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

        # ── Step 3: Raw Backside detection ──
        raw_signals = self._detect_bs_signals(symbol, bars, day, ip_score)

        # ── Steps 4-6: Filter + score ──
        results = []
        for sig, bar_idx, bar, atr, ema9, vwap, vol_ma in raw_signals:
            self.stats["raw_signals"] += 1

            # Step 4: Rejection filters
            # Skip distance: backside trades start extended below VWAP by design
            # Skip bigger_picture: HH/HL structure IS the bigger picture assessment
            reasons = self.rejection.check_all(
                bar, bars, bar_idx, atr, ema9, vwap, vol_ma,
                skip_filters=["distance", "bigger_picture"]
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

    def _detect_bs_signals(self, symbol: str, bars: List[Bar], day: date,
                            ip_score: float) -> list:
        """
        Run Backside Structure multi-phase state machine.

        Phase 1: Decline detection (price drops below VWAP, makes LOD)
        Phase 2: Structure building (HH+HL after LOD, E9 starts rising)
        Phase 3: Range detection (tight range above E9)
        Phase 4: Breakout (close above range high, target = VWAP)

        Returns list of (StrategySignal, bar_idx, bar, atr, ema9, vwap, vol_ma).
        """
        cfg = self.cfg
        time_start = cfg.get(cfg.bs_time_start)
        time_end = cfg.get(cfg.bs_time_end)
        range_min = cfg.get(cfg.bs_range_min_bars)
        range_max = cfg.get(cfg.bs_range_max_bars)

        # Indicators
        ema9 = EMA(9)
        vwap_calc = VWAPCalc()
        atr_pair = ATRPair(14, use_completed=True)
        vol_buf = deque(maxlen=20)

        # Day state
        session_open = NaN
        session_high = NaN
        session_low = NaN  # LOD candidate
        lod_price = NaN    # confirmed LOD
        lod_bar_idx = -1
        decline_confirmed = False
        prev_date = None
        triggered = False

        # Structure tracking (after LOD)
        swing_highs = []    # list of (bar_idx, price)
        swing_lows = []     # list of (bar_idx, price)
        hh_count = 0
        hl_count = 0
        structure_confirmed = False

        # Range tracking
        range_active = False
        range_high = NaN
        range_low = NaN
        range_bars = 0
        range_bars_above_e9 = 0
        range_start_idx = -1
        most_recent_hl = NaN

        # E9 history for slope check
        e9_history = deque(maxlen=10)

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
                lod_price = NaN
                lod_bar_idx = -1
                decline_confirmed = False
                triggered = False
                swing_highs = []
                swing_lows = []
                hh_count = 0
                hl_count = 0
                structure_confirmed = False
                range_active = False
                range_high = NaN
                range_low = NaN
                range_bars = 0
                range_bars_above_e9 = 0
                range_start_idx = -1
                most_recent_hl = NaN
                e9_history.clear()
                prev_date = d

            # Update indicators
            e9 = ema9.update(bar.close)
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
            if not ema9.ready:
                continue
            if _isnan(vol_ma) or vol_ma <= 0:
                continue

            e9_history.append(e9)

            # ══════════════════════════════════════════════════
            # Phase 1: DECLINE DETECTION
            # ══════════════════════════════════════════════════
            if not decline_confirmed:
                # Need price below VWAP with meaningful decline
                if not _isnan(vw) and bar.close < vw:
                    decline_dist = session_high - bar.low
                    if decline_dist >= cfg.bs_min_decline_atr * i_atr:
                        decline_confirmed = True
                        lod_price = bar.low
                        lod_bar_idx = i
                continue

            # Track LOD (update if we make a new low)
            if bar.low < lod_price:
                lod_price = bar.low
                lod_bar_idx = i
                # Reset structure if we make a new low — start over
                swing_highs = []
                swing_lows = []
                hh_count = 0
                hl_count = 0
                structure_confirmed = False
                range_active = False
                continue

            # ══════════════════════════════════════════════════
            # Phase 2: STRUCTURE BUILDING (HH + HL detection)
            # ══════════════════════════════════════════════════
            if not structure_confirmed:
                # Track swing points using 3-bar lookback
                # Swing high: bar[i-1].high > bar[i-2].high and bar[i-1].high > bar[i].high
                if i >= 2:
                    prev1_idx = i - 1
                    prev2_idx = i - 2
                    if prev1_idx < len(bars) and prev2_idx < len(bars):
                        prev1 = bars[prev1_idx]
                        prev2 = bars[prev2_idx]

                        # Swing high at prev1
                        if (prev1.high > prev2.high and prev1.high > bar.high and
                                prev1.timestamp.date() == day and prev2.timestamp.date() == day):
                            sh = (prev1_idx, prev1.high)
                            if not swing_highs or sh[1] != swing_highs[-1][1]:
                                swing_highs.append(sh)
                                # Check HH
                                if len(swing_highs) >= 2:
                                    if swing_highs[-1][1] > swing_highs[-2][1]:
                                        hh_count += 1

                        # Swing low at prev1
                        if (prev1.low < prev2.low and prev1.low < bar.low and
                                prev1.timestamp.date() == day and prev2.timestamp.date() == day):
                            sl = (prev1_idx, prev1.low)
                            if not swing_lows or sl[1] != swing_lows[-1][1]:
                                swing_lows.append(sl)
                                most_recent_hl = sl[1]
                                # Check HL
                                if len(swing_lows) >= 2:
                                    if swing_lows[-1][1] > swing_lows[-2][1]:
                                        hl_count += 1

                # Check if structure is confirmed
                if (hh_count >= cfg.bs_min_hh_count and
                        hl_count >= cfg.bs_min_hl_count):
                    # Check E9 rising
                    if len(e9_history) >= cfg.bs_ema9_rising_bars:
                        rising = all(
                            e9_history[j] > e9_history[j - 1]
                            for j in range(len(e9_history) - cfg.bs_ema9_rising_bars + 1,
                                           len(e9_history))
                        )
                        if rising and bar.close > e9:
                            structure_confirmed = True
                            # Start range tracking
                            range_active = True
                            range_high = bar.high
                            range_low = bar.low
                            range_bars = 1
                            range_bars_above_e9 = 1 if bar.low > e9 else 0
                            range_start_idx = i
                continue

            # ══════════════════════════════════════════════════
            # Phase 3: RANGE TRACKING
            # ══════════════════════════════════════════════════
            if range_active and not triggered:
                # Check breakout BEFORE updating range
                breakout_fired = False

                if range_bars >= range_min and time_start <= hhmm <= time_end:
                    # Check range quality
                    above_e9_pct = range_bars_above_e9 / range_bars if range_bars > 0 else 0

                    if above_e9_pct >= cfg.bs_range_above_ema9_pct:
                        # Check range midpoint position vs LOD→VWAP path
                        range_mid = (range_high + range_low) / 2.0
                        if not _isnan(vw) and not _isnan(lod_price):
                            vwap_path = vw - lod_price
                            if vwap_path > 0:
                                midpoint_frac = (range_mid - lod_price) / vwap_path
                            else:
                                midpoint_frac = 0.0
                        else:
                            midpoint_frac = 0.0

                        if midpoint_frac >= cfg.bs_range_midpoint_vwap_frac:
                            # Breakout: close > range_high
                            if bar.high > range_high and bar.close > range_high:
                                # Volume check
                                vol_ok = bar.volume >= cfg.bs_break_vol_frac * vol_ma

                                # Close in upper portion
                                rng = bar.high - bar.low
                                close_pct = (bar.close - bar.low) / rng if rng > 0 else 0
                                close_ok = close_pct >= cfg.bs_break_close_pct

                                if vol_ok and close_ok:
                                    breakout_fired = True

                if breakout_fired:
                    # Compute stop: below most recent HL
                    if not _isnan(most_recent_hl):
                        stop = most_recent_hl - cfg.bs_stop_buffer
                    else:
                        stop = range_low - cfg.bs_stop_buffer

                    min_stop = 0.30 * i_atr  # FIX: spec says 0.30 ATR min stop
                    if abs(bar.close - stop) < min_stop:
                        stop = bar.close - min_stop

                    risk = bar.close - stop
                    if risk <= 0:
                        range_active = False
                        continue

                    # Structural target computation
                    if cfg.bs_target_mode == "structural":
                        _candidates = []
                        if not _isnan(vw) and vw > bar.close:
                            _candidates.append((vw, "vwap"))
                        if not _isnan(session_high) and session_high > bar.close:
                            _candidates.append((session_high, "session_high"))
                        target, actual_rr, target_tag, skipped = compute_structural_target_long(
                            bar.close, risk, _candidates,
                            min_rr=cfg.bs_struct_min_rr, max_rr=cfg.bs_struct_max_rr,
                            fallback_rr=cfg.bs_target_rr, mode="structural",
                        )
                        if skipped:
                            range_active = False
                            continue
                    else:
                        target = bar.close + risk * cfg.bs_target_rr
                        actual_rr = cfg.bs_target_rr
                        target_tag = "fixed_rr"

                    # Structure quality
                    struct_q = 0.30
                    # HH/HL count bonus
                    if hh_count >= 2 and hl_count >= 2:
                        struct_q += 0.20
                    elif hh_count >= 1 and hl_count >= 1:
                        struct_q += 0.10
                    # Tight range bonus
                    range_width = range_high - range_low
                    if i_atr > 0 and range_width <= 1.0 * i_atr:
                        struct_q += 0.15
                    # Strong vol on breakout
                    if bar.volume >= 1.5 * vol_ma:
                        struct_q += 0.10
                    # Above E9 pct in range
                    above_e9_pct = range_bars_above_e9 / range_bars if range_bars > 0 else 0
                    if above_e9_pct >= 0.85:
                        struct_q += 0.10
                    # Midpoint position
                    if midpoint_frac >= 0.60:
                        struct_q += 0.10

                    confluence = []
                    if not _isnan(e9) and bar.close > e9:
                        confluence.append("above_ema9")
                    if above_e9_pct >= 0.85:
                        confluence.append("strong_e9_structure")
                    if hh_count >= 2:
                        confluence.append("multi_hh")
                    if range_width <= 1.0 * i_atr:
                        confluence.append("tight_range")
                    if bar.volume >= 1.5 * vol_ma:
                        confluence.append("strong_bo_vol")

                    sig = StrategySignal(
                        strategy_name="BS_STRUCT",
                        symbol=symbol,
                        timestamp=bar.timestamp,
                        direction=1,
                        entry_price=bar.close,
                        stop_price=stop,
                        target_price=target,
                        in_play_score=ip_score,
                        confluence_tags=confluence,
                        metadata={
                            "lod_price": lod_price,
                            "decline_atr": (session_high - lod_price) / i_atr if i_atr > 0 else 0,
                            "hh_count": hh_count,
                            "hl_count": hl_count,
                            "range_bars": range_bars,
                            "range_width_atr": range_width / i_atr if i_atr > 0 else 0,
                            "above_e9_pct": above_e9_pct,
                            "midpoint_frac": midpoint_frac,
                            "target_is_vwap": cfg.bs_target_mode == "vwap",
                            "actual_rr": actual_rr,
                            "target_tag": target_tag,
                            "structure_quality": min(struct_q, 1.0),
                        },
                    )

                    signals.append((sig, i, bar, i_atr, e9, vw, vol_ma))
                    triggered = True

                    if cfg.bs_one_and_done:
                        continue

                # No breakout — update range bounds
                if not triggered:
                    new_high = max(range_high, bar.high)
                    new_low = min(range_low, bar.low)
                    range_width = new_high - new_low

                    # If range gets too wide, invalidate
                    if i_atr > 0 and range_width > cfg.bs_range_max_atr * i_atr:
                        range_active = False
                        if cfg.bs_one_and_done:
                            triggered = True  # prevent retry
                        continue

                    # If too many bars, invalidate
                    if range_bars >= range_max:
                        range_active = False
                        if cfg.bs_one_and_done:
                            triggered = True
                        continue

                    range_high = new_high
                    range_low = new_low
                    range_bars += 1
                    if bar.low > e9:
                        range_bars_above_e9 += 1

                    # Update most recent HL from swing lows
                    if i >= 2:
                        prev1 = bars[i - 1]
                        prev2 = bars[i - 2]
                        if (prev1.low < prev2.low and prev1.low < bar.low and
                                prev1.timestamp.date() == day and prev2.timestamp.date() == day):
                            if prev1.low > lod_price:
                                most_recent_hl = prev1.low

        return signals
