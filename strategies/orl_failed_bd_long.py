"""
ORL_FBD_LONG — Opening Range Low Failed Breakdown Reclaim Long.

4-phase state machine:
  Phase 1 (Breakdown):  Bar closes decisively below ORL with volume — traps shorts / weak longs
  Phase 2 (Reclaim):    Price reclaims back above ORL within tight window
  Phase 3 (Structure):  Price makes a higher high above the reclaim bar — confirms structure
  Phase 4 (Entry):      The HH bar IS the entry — no additional confirmation needed

LONG ONLY. ORL-only for v1 (no PDL, no PML).
Environment: allows GREEN, FLAT, mildly RED. Blocks strongly bearish tape.
Time window: 10:00-13:00 (after OR forms).
Target: VWAP (room-to-VWAP is a POSITIVE — provides squeeze room).

Tightening vs design spec draft:
  - ORL only (no PDL/PML)
  - Tighter reclaim: must reclaim within 5 bars of breakdown
  - Reclaim + HH confirmation: not just first close above ORL
  - Room-to-VWAP used as positive (farther from VWAP = more room to run)
  - Stop below lowest low since breakdown (not just ORL)
  - Regime: permissive (blocks only strongly bearish)

Usage (via replay.py):
    from .orl_failed_bd_long import ORLFailedBDLongStrategy
    strategy = ORLFailedBDLongStrategy(cfg, in_play, regime, rejection, quality)
    signals = strategy.scan_day(symbol, bars, day, spy_bars, sector_bars)
"""

import math
from collections import deque
from datetime import date, datetime
from statistics import median
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
from .shared.level_helpers import breakout_quality

_isnan = math.isnan


class ORLFailedBDLongStrategy:
    """
    ORL Failed Breakdown Reclaim Long: breakdown below ORL → reclaim → HH confirm.

    Pipeline:
    1. In-play check (day level)
    2. Market regime check (per-signal, failed-long gate)
    3. Raw detection (4-phase state machine)
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
                 sector_bars: Optional[List[Bar]] = None,
                 **kwargs) -> List[StrategySignal]:
        """Run full pipeline for one symbol-day."""
        cfg = self.cfg
        self.stats["total_symbol_days"] += 1

        # ── Step 1: In-play check ──
        ip_pass, ip_score = self.in_play.is_in_play(symbol, day)
        if not ip_pass:
            return []
        self.stats["passed_in_play"] += 1

        # ── Step 2: Regime — GREEN-only day gate ──
        day_label = self.regime.get_day_label(day)
        if day_label != "GREEN":
            return []

        day_bars = [b for b in bars if b.timestamp.date() == day]
        if not day_bars:
            return []
        self.stats["passed_regime"] += 1

        # ── Step 3: Raw detection ──
        raw_signals = self._detect_signals(symbol, bars, day, ip_score)

        # ── Steps 4-6: Filter + score ──
        results = []
        for sig, bar_idx, bar, atr, ema9, vwap, vol_ma in raw_signals:
            self.stats["raw_signals"] += 1

            # Step 2b: Per-signal regime check
            if not self.regime.is_aligned_failed_long(sig.timestamp):
                continue

            # Step 4: Rejection filters
            # Skip distance (we want price below VWAP — that's the setup)
            # Skip bigger_picture (complex interaction with failed-move thesis — below VWAP is expected)
            # Skip trigger_weakness — 4-phase structure IS the quality filter, not the HH bar itself
            reasons = self.rejection.check_all(
                bar, bars, bar_idx, atr, ema9, vwap, vol_ma,
                skip_filters=["distance", "bigger_picture", "trigger_weakness"]
            )
            sig.reject_reasons = reasons

            if reasons:
                for r in reasons:
                    key = r.split("(")[0]
                    self.stats["reject_reasons"][key] = self.stats["reject_reasons"].get(key, 0) + 1
            else:
                self.stats["passed_rejection"] += 1

            # Step 5: Quality scoring (standard long scoring)
            spy_pct = self.regime.get_spy_pct_from_open(bar.timestamp)
            day_open = day_bars[0].open
            rs_mkt = (bar.close - day_open) / day_open - spy_pct if day_open > 0 else 0.0

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

    def _detect_signals(self, symbol: str, bars: List[Bar], day: date,
                        ip_score: float) -> list:
        """
        4-phase state machine:
          Phase 1: Breakdown below ORL (meaningful, with volume)
          Phase 2: Reclaim — price closes back above ORL within tight window
          Phase 3: Structure — higher high confirms reclaim is structural
          Phase 4: Entry on HH bar (the HH bar IS the trigger)
        """
        cfg = self.cfg
        time_start = cfg.get(cfg.orl_time_start)
        time_end = cfg.get(cfg.orl_time_end)
        failure_window = cfg.get(cfg.orl_failure_window)
        confirm_bars = cfg.get(cfg.orl_reclaim_confirm_bars)

        # Indicators
        ema9 = EMA(9)
        ema20 = EMA(20)
        vwap_calc = VWAPCalc()
        atr_pair = ATRPair(14, use_completed=True)
        vol_buf = deque(maxlen=20)

        # OR tracking
        or_high = NaN
        or_low = NaN
        or_ready = False

        # State machine
        IDLE, BROKE_DOWN, RECLAIMED, CONFIRMED = 0, 1, 2, 3
        phase = IDLE
        bd_bar_idx = 0
        bd_bar_vol = 0.0
        bd_quality = 0.0
        bars_since_bd = 0
        lowest_since_bd = NaN  # for stop placement
        reclaim_bar_high = NaN
        reclaim_bar_low = NaN
        bars_since_reclaim = 0

        prev_date = None
        triggered_today = False
        signals = []

        for i, bar in enumerate(bars):
            d = bar.timestamp.date()
            hhmm = get_hhmm(bar)

            # Day reset
            if d != prev_date:
                vwap_calc.reset()
                phase = IDLE
                triggered_today = False
                or_high = NaN
                or_low = NaN
                or_ready = False
                lowest_since_bd = NaN
                prev_date = d

            # Update indicators
            e9 = ema9.update(bar.close)
            e20 = ema20.update(bar.close)
            i_atr = atr_pair.update_intraday(bar.high, bar.low, bar.close)
            tp = (bar.high + bar.low + bar.close) / 3.0
            vw = vwap_calc.update(tp, bar.volume)
            vol_buf.append(bar.volume)
            vol_ma = sum(vol_buf) / len(vol_buf) if len(vol_buf) >= 5 else NaN

            # OR tracking
            if d == day and not or_ready:
                if hhmm <= 959:
                    or_high = max(or_high, bar.high) if not _isnan(or_high) else bar.high
                    or_low = min(or_low, bar.low) if not _isnan(or_low) else bar.low
                else:
                    or_ready = True

            if d != day:
                continue

            # Skip if not ready
            if triggered_today or not ema9.ready or _isnan(i_atr) or i_atr <= 0:
                continue
            if _isnan(vol_ma) or vol_ma <= 0:
                continue
            if not or_ready or _isnan(or_low):
                continue

            rng = bar.high - bar.low

            # Track lowest since breakdown
            if phase in (1, 2):  # BROKE_DOWN or RECLAIMED
                if _isnan(lowest_since_bd) or bar.low < lowest_since_bd:
                    lowest_since_bd = bar.low

            # Tick state counters
            if phase == 1:  # BROKE_DOWN
                bars_since_bd += 1
                if bars_since_bd > failure_window:
                    phase = IDLE
                    continue
            elif phase == 2:  # RECLAIMED
                bars_since_reclaim += 1
                if bars_since_reclaim > confirm_bars:
                    phase = IDLE
                    continue

            # ════════════════════════════════════════
            # Phase 1: BREAKDOWN below ORL
            # ════════════════════════════════════════
            if phase == IDLE and time_start <= hhmm <= time_end:
                # Must close below ORL by meaningful distance
                dist_below = or_low - bar.close
                if dist_below < cfg.orl_break_min_dist_atr * i_atr:
                    continue

                # Body check
                if bar_body_ratio(bar) < cfg.orl_break_body_min:
                    continue

                # Volume check
                if bar.volume < cfg.orl_break_vol_frac * vol_ma:
                    continue

                # Must be bearish (close < open) — a real breakdown
                if bar.close >= bar.open:
                    continue

                # Latch breakdown
                phase = 1  # BROKE_DOWN
                bd_bar_idx = i
                bd_bar_vol = bar.volume
                bd_quality = breakout_quality(bar, or_low, i_atr, vol_ma, -1)
                bars_since_bd = 0
                lowest_since_bd = bar.low
                continue

            # ════════════════════════════════════════
            # Phase 2: RECLAIM — closes back above ORL
            # ════════════════════════════════════════
            if phase == 1:  # BROKE_DOWN
                if bar.close > or_low:
                    # Reclaimed! Price gave back the breakdown
                    # Body check on reclaim bar
                    if bar_body_ratio(bar) >= cfg.orl_reclaim_body_min and bar.close > bar.open:
                        phase = 2  # RECLAIMED
                        reclaim_bar_high = bar.high
                        reclaim_bar_low = bar.low
                        bars_since_reclaim = 0
                continue

            # ════════════════════════════════════════
            # Phase 3: STRUCTURE — Higher High confirms
            # ════════════════════════════════════════
            if phase == 2:  # RECLAIMED
                clearance = cfg.orl_hh_clearance_atr * i_atr
                hh_made = bar.high > reclaim_bar_high + clearance

                if hh_made and bar.close > bar.open:
                    # ── SIGNAL FIRES on HH bar ──
                    # Stop below lowest since breakdown
                    stop_ref = lowest_since_bd if not _isnan(lowest_since_bd) else or_low
                    stop = stop_ref - cfg.orl_stop_buffer_atr * i_atr
                    min_stop = cfg.orl_min_stop_atr * i_atr
                    risk = bar.close - stop
                    if risk < min_stop:
                        stop = bar.close - min_stop
                        risk = min_stop
                    if risk <= 0:
                        phase = IDLE
                        continue

                    # Compute vwap distance for quality and confluence
                    vwap_dist = vw - bar.close if not _isnan(vw) else 0.0

                    # Target: Structural targets or fixed RR
                    if cfg.orl_target_mode == "structural":
                        _candidates = []
                        if not _isnan(vw) and vw > bar.close:
                            _candidates.append((vw, "vwap"))
                        if not _isnan(or_high) and or_high > bar.close:
                            _candidates.append((or_high, "orh"))
                        target, actual_rr, target_tag, skipped = compute_structural_target_long(
                            bar.close, risk, _candidates,
                            min_rr=cfg.orl_struct_min_rr, max_rr=cfg.orl_struct_max_rr,
                            fallback_rr=cfg.orl_target_rr, mode="structural",
                        )
                        if skipped:
                            phase = IDLE
                            continue
                    else:
                        target = bar.close + risk * cfg.orl_target_rr
                        actual_rr = cfg.orl_target_rr
                        target_tag = "fixed_rr"

                    # Structure quality
                    struct_q = 0.50
                    if bd_quality >= 0.60:
                        struct_q += 0.15  # strong breakdown = more trapped shorts
                    if bars_since_bd <= 3:
                        struct_q += 0.10  # fast reclaim = more trapped
                    if vwap_dist > 1.0 * i_atr:
                        struct_q += 0.10  # lots of room to squeeze
                    if bar.close > e9:
                        struct_q += 0.10  # above EMA9 = bullish
                    if bar.volume >= 1.0 * vol_ma:
                        struct_q += 0.05  # volume on HH bar

                    confluence = ["orl_failed_bd"]
                    if bar.close > e9:
                        confluence.append("above_ema9")
                    if vwap_dist > 1.0 * i_atr:
                        confluence.append("room_to_vwap")
                    if bd_quality >= 0.60:
                        confluence.append("strong_trap")
                    if bars_since_bd <= 3:
                        confluence.append("fast_reclaim")
                    if bar.volume >= 1.2 * vol_ma:
                        confluence.append("vol_confirm")

                    sig = StrategySignal(
                        strategy_name="ORL_FBD_LONG",
                        symbol=symbol,
                        timestamp=bar.timestamp,
                        direction=1,  # LONG
                        entry_price=bar.close,
                        stop_price=stop,
                        target_price=target,
                        in_play_score=ip_score,
                        confluence_tags=confluence,
                        metadata={
                            "or_low": or_low,
                            "bd_quality": bd_quality,
                            "bd_bar_vol_ratio": bd_bar_vol / vol_ma if vol_ma > 0 else 0,
                            "bars_to_reclaim": bars_since_bd,
                            "lowest_since_bd": lowest_since_bd,
                            "actual_rr": actual_rr,
                            "target_tag": target_tag,
                            "structure_quality": min(struct_q, 1.0),
                        },
                    )

                    signals.append((sig, i, bar, i_atr, e9, vw, vol_ma))
                    phase = IDLE
                    triggered_today = True

                # Update reclaim high for next bars
                if bar.high > reclaim_bar_high:
                    reclaim_bar_high = bar.high

        return signals
