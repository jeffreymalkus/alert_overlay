"""
HitchHiker_Program_Quality — Opening drive → consolidation → breakout.
Faithful to SMB HitchHiker Scalp cheat sheet.

Long-only. Strong opening drive → tight consolidation near highs → breakout.

Key fidelity changes vs prior build:
  - Entry on breakout trigger price (consol_high + buffer), not bar.close
  - Consolidation in upper 1/3 of day range (0.66), not upper 1/2
  - Consolidation 1-4 bars on 5m (5-20 min), not 3-24
  - Breakout deadline 10:05, not 12:00
  - Prior-bar volume acceleration check (1.3x)
  - Failed attempt filter (max 1 upper-bound probe before breakout)
  - Drive must be multi-bar (>= 2 bars) and no single bar > 60% of drive
  - Wave-based exit: 50% on first stall, 50% on second wave / EMA9 loss

Usage (via replay.py):
    from .hitchhiker_quality import HitchHikerQualityStrategy
    strategy = HitchHikerQualityStrategy(cfg, in_play, regime, rejection, quality)
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


class HitchHikerQualityStrategy:
    """
    HitchHiker Program Quality: opening drive → tight consolidation → breakout.

    Pipeline:
    1. In-play check (day level)
    2. Market regime check (day level)
    3. Raw HitchHiker detection (3-phase state machine)
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

        # Pipeline counters
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

        # ── Step 3: Raw HH detection ──
        raw_signals = self._detect_hh_signals(symbol, bars, day, ip_score)

        # ── Steps 4-6: Filter + score ──
        results = []
        for sig, bar_idx, bar, atr, ema9, vwap, vol_ma in raw_signals:
            self.stats["raw_signals"] += 1

            # Step 4: Universal rejection filters
            # Skip distance: opening drive pushes price far from VWAP by design
            # Skip maturity: opening drive creates extension that maturity would flag
            reasons = self.rejection.check_all(
                bar, bars, bar_idx, atr, ema9, vwap, vol_ma,
                skip_filters=["distance", "maturity"]
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

    def _detect_hh_signals(self, symbol: str, bars: List[Bar], day: date,
                           ip_score: float) -> list:
        """
        Run HitchHiker 3-phase state machine (SMB-faithful):
        Phase 1: Opening drive detection (multi-bar, not overextended)
        Phase 2: Consolidation tracking (1-4 bars on 5m, upper 1/3, no failed attempts)
        Phase 3: Breakout detection (trigger price entry, prior-bar vol check)

        Returns list of (StrategySignal, bar_idx, bar, atr, ema9, vwap, vol_ma).
        """
        cfg = self.cfg
        time_start = cfg.get(cfg.hh_time_start)
        time_end = cfg.get(cfg.hh_time_end)
        consol_min = cfg.get(cfg.hh_consol_min_bars)
        consol_max = cfg.get(cfg.hh_consol_max_bars)

        # Indicators
        ema9 = EMA(9)
        vwap_calc = VWAPCalc()
        atr_pair = ATRPair(14, use_completed=True)
        vol_buf = deque(maxlen=20)

        # Day state
        session_open = NaN
        session_high = NaN
        session_low = NaN
        drive_confirmed = False
        drive_high = NaN
        drive_bar_count = 0        # NEW: track number of bars in the drive
        drive_max_single_bar = 0.0  # NEW: largest single bar contribution to drive
        drive_total_dist = 0.0      # NEW: total drive distance
        consol_active = False
        consol_high = NaN
        consol_low = NaN
        consol_bars = 0
        consol_total_vol = 0.0
        consol_total_wick = 0.0
        consol_attempt_count = 0   # NEW: failed upper-bound probes
        triggered = False
        prev_bar = None            # NEW: track previous bar for volume comparison
        prev_date = None
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
                drive_bar_count = 0
                drive_max_single_bar = 0.0
                drive_total_dist = 0.0
                consol_active = False
                consol_high = NaN
                consol_low = NaN
                consol_bars = 0
                consol_total_vol = 0.0
                consol_total_wick = 0.0
                consol_attempt_count = 0
                triggered = False
                prev_bar = None
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

            if day is not None and d != day:
                prev_bar = bar
                continue

            if _isnan(i_atr) or i_atr <= 0 or triggered:
                prev_bar = bar
                continue

            # ── Phase 1: Opening drive detection ──
            if not drive_confirmed and not consol_active:
                if not _isnan(session_open) and i_atr > 0:
                    # Track drive progression
                    bar_contribution = bar.high - bar.open if drive_bar_count == 0 else max(0, bar.high - (bars[i-1].high if i > 0 else bar.open))
                    drive_bar_count += 1
                    if bar_contribution > drive_max_single_bar:
                        drive_max_single_bar = bar_contribution

                    drive_dist = bar.high - session_open
                    if drive_dist >= cfg.hh_drive_min_atr * i_atr:
                        drive_total_dist = drive_dist

                        # NEW: Drive must be multi-bar (>= 2 bars)
                        if drive_bar_count < 2:
                            prev_bar = bar
                            continue

                        # NEW: No single bar > 60% of total drive distance
                        if drive_total_dist > 0 and drive_max_single_bar / drive_total_dist > 0.60:
                            prev_bar = bar
                            continue

                        drive_confirmed = True
                        drive_high = bar.high
                prev_bar = bar
                continue

            # ── Phase 2 start: detect transition to consolidation ──
            if drive_confirmed and not consol_active:
                if bar.close < drive_high:
                    consol_active = True
                    consol_high = bar.high
                    consol_low = bar.low
                    consol_bars = 1
                    consol_total_vol = bar.volume
                    bar_range = bar.high - bar.low
                    wick = (bar_range - abs(bar.close - bar.open)) / bar_range if bar_range > 0 else 0
                    consol_total_wick = wick
                    consol_attempt_count = 0
                else:
                    drive_high = max(drive_high, bar.high)
                prev_bar = bar
                continue

            # ── Phase 2/3: consolidation active ──
            if consol_active and not triggered:
                # Check breakout BEFORE updating bounds
                breakout_fired = False

                if consol_bars >= consol_min and time_start <= hhmm <= time_end:
                    consol_avg_vol = consol_total_vol / consol_bars if consol_bars > 0 else 0
                    avg_wick = consol_total_wick / consol_bars if consol_bars > 0 else 0

                    # Wick check
                    wick_ok = avg_wick <= cfg.hh_max_wick_pct

                    # Position check: consol low in upper 1/3 of day range
                    day_range = session_high - session_low if not _isnan(session_high) else 0
                    pos_ok = True
                    if day_range > 0:
                        threshold = session_low + day_range * cfg.hh_consol_upper_pct
                        if consol_low < threshold:
                            pos_ok = False

                    # NEW: Failed attempt filter — max 1 prior upper-bound probe
                    attempt_ok = consol_attempt_count <= 1

                    # Breakout: bar breaks above consol_high
                    if (bar.high > consol_high and wick_ok and pos_ok and attempt_ok and
                            not _isnan(vol_ma) and vol_ma > 0):
                        # Volume confirmation: vs consolidation average
                        vol_vs_consol_ok = consol_avg_vol > 0 and bar.volume >= cfg.hh_break_vol_frac * consol_avg_vol

                        # NEW: Prior-bar volume acceleration (30% more than prior candle)
                        vol_vs_prev_ok = True
                        if prev_bar is not None and prev_bar.volume > 0:
                            vol_vs_prev_ok = bar.volume >= 1.30 * prev_bar.volume

                        if vol_vs_consol_ok and vol_vs_prev_ok:
                            breakout_fired = True

                if breakout_fired:
                    # NEW: Entry on breakout trigger price, not bar.close
                    # SMB says: "buy on the break higher of the bar range"
                    trigger_price = consol_high + 0.01  # $0.01 above consol high
                    # In replay, model the fill at trigger price (better than close on expansion bars)
                    entry_price = max(trigger_price, bar.open)  # can't fill below open

                    stop = consol_low - cfg.hh_stop_buffer
                    risk = entry_price - stop
                    if risk <= 0:
                        consol_active = False
                        drive_confirmed = False
                        prev_bar = bar
                        continue

                    min_stop_dist = 0.15 * i_atr
                    if risk < min_stop_dist:
                        stop = entry_price - min_stop_dist
                        risk = min_stop_dist

                    # Structural target
                    if cfg.hh_target_mode == "structural":
                        _candidates = []
                        # Measured move 1: consol breakout + consol range (textbook box)
                        consol_range_mm = consol_high - consol_low
                        mm_box = consol_high + consol_range_mm
                        if mm_box > entry_price:
                            _candidates.append((mm_box, "measured_move_box"))
                        # Measured move 2: consol breakout + drive distance
                        if not _isnan(session_open) and not _isnan(drive_high):
                            drive_dist_mm = drive_high - session_open
                            mm_drive = consol_high + drive_dist_mm
                            if mm_drive > entry_price:
                                _candidates.append((mm_drive, "measured_move_drive"))
                        if not _isnan(drive_high) and drive_high > entry_price:
                            _candidates.append((drive_high, "drive_high"))
                        if not _isnan(session_high) and session_high > entry_price:
                            _candidates.append((session_high, "session_high"))
                        target, actual_rr, target_tag, skipped = compute_structural_target_long(
                            entry_price, risk, _candidates,
                            min_rr=cfg.hh_struct_min_rr, max_rr=cfg.hh_struct_max_rr,
                            fallback_rr=cfg.hh_target_rr, mode="structural",
                        )
                        if skipped:
                            consol_active = False
                            drive_confirmed = False
                            prev_bar = bar
                            continue
                    else:
                        target = entry_price + risk * cfg.hh_target_rr
                        actual_rr = cfg.hh_target_rr
                        target_tag = "fixed_rr"

                    drive_dist = drive_high - session_open if not _isnan(session_open) else 0.0

                    # Structure quality
                    struct_q = 0.30
                    consol_avg_vol_calc = consol_total_vol / consol_bars if consol_bars > 0 else 0
                    avg_wick_calc = consol_total_wick / consol_bars if consol_bars > 0 else 1.0

                    if consol_avg_vol_calc > 0 and bar.volume >= 1.5 * consol_avg_vol_calc:
                        struct_q += 0.15
                    if avg_wick_calc <= 0.40:
                        struct_q += 0.15
                    if not _isnan(vw) and entry_price > vw:
                        struct_q += 0.10
                    if not _isnan(e9) and entry_price > e9:
                        struct_q += 0.10
                    # NEW: Bonus for tight consolidation (1-2 bars)
                    if consol_bars <= 2:
                        struct_q += 0.10
                    # NEW: Bonus for zero failed attempts
                    if consol_attempt_count == 0:
                        struct_q += 0.10

                    confluence = []
                    if not _isnan(vw) and entry_price > vw:
                        confluence.append("above_vwap")
                    if not _isnan(e9) and entry_price > e9:
                        confluence.append("above_ema9")
                    if i_atr > 0 and drive_dist >= 1.5 * i_atr:
                        confluence.append("strong_drive")
                    consol_range = consol_high - consol_low
                    if i_atr > 0 and consol_range <= 1.0 * i_atr:
                        confluence.append("tight_consol")
                    if consol_avg_vol_calc > 0 and bar.volume >= 1.5 * consol_avg_vol_calc:
                        confluence.append("strong_bo_vol")
                    # NEW: Multi-bar drive confluence
                    if drive_bar_count >= 3:
                        confluence.append("multi_bar_drive")

                    sig = StrategySignal(
                        strategy_name="HH_QUALITY",
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
                            "drive_bar_count": drive_bar_count,
                            "consol_high": consol_high,
                            "consol_low": consol_low,
                            "consol_bars": consol_bars,
                            "consol_avg_vol": consol_avg_vol_calc,
                            "consol_attempt_count": consol_attempt_count,
                            "avg_wick": avg_wick_calc,
                            "structure_quality": min(struct_q, 1.0),
                            "actual_rr": actual_rr,
                            "target_tag": target_tag,
                            "entry_type": "breakout_trigger",
                        },
                    )

                    signals.append((sig, i, bar, i_atr, e9, vw, vol_ma))
                    triggered = True
                    prev_bar = bar
                    continue

                # No breakout — update consolidation bounds
                # NEW: Track failed upper-bound probes before updating
                if not _isnan(consol_high) and i_atr > 0:
                    # A bar whose high comes within 0.03 ATR of consol_high is a "probe"
                    probe_threshold = consol_high - 0.03 * i_atr
                    if bar.high >= probe_threshold and bar.close < consol_high:
                        consol_attempt_count += 1

                new_high = max(consol_high, bar.high)
                new_low = min(consol_low, bar.low)
                consol_range = new_high - new_low

                # If consolidation gets too wide, reset
                if i_atr > 0 and consol_range > cfg.hh_consol_max_range_atr * i_atr:
                    consol_active = False
                    drive_confirmed = False
                    prev_bar = bar
                    continue

                # If too many bars, reset
                if consol_bars >= consol_max:
                    consol_active = False
                    drive_confirmed = False
                    prev_bar = bar
                    continue

                consol_high = new_high
                consol_low = new_low
                consol_bars += 1
                consol_total_vol += bar.volume
                bar_range = bar.high - bar.low
                wick = (bar_range - abs(bar.close - bar.open)) / bar_range if bar_range > 0 else 0
                consol_total_wick += wick

            prev_bar = bar

        return signals
