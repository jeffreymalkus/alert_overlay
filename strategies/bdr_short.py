"""
BDR_SHORT — Breakdown-Retest Short with full 6-step pipeline.

3-phase state machine:
  Phase 1 (Breakdown): Bar closes decisively below support level (VWAP, OR low, swing low)
  Phase 2 (Retest):    Price bounces back toward broken level but fails to reclaim
  Phase 3 (Rejection): Bearish bar with big upper wick confirms rejection

SHORT ONLY. Uses RED+TREND regime gate (not GREEN).
AM-only entries (before 11:00) per validated research.

Usage (via replay.py):
    from .bdr_short import BDRShortStrategy
    strategy = BDRShortStrategy(cfg, in_play, regime, rejection, quality)
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
)

_isnan = math.isnan


class BDRShortStrategy:
    """
    Breakdown-Retest Short: breakdown → retest → rejection with full pipeline.

    Pipeline:
    1. In-play check (day level)
    2. Market regime check (RED+TREND day — is_aligned_short)
    3. Raw BDR detection (bar-by-bar state machine)
    4. Universal rejection filters (adapted for shorts)
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

        # ── Step 2: Market regime — deferred to per-signal (BDR needs real-time check) ──
        day_bars = [b for b in bars if b.timestamp.date() == day]
        if not day_bars:
            return []
        # Don't gate at day level — BDR regime is checked per-signal below
        self.stats["passed_regime"] += 1  # counted at signal level

        # ── Compute prior-day low for structural targets ──
        pdl = self._compute_pdl(bars, day)

        # ── Step 3: Raw BDR detection ──
        raw_signals = self._detect_bdr_signals(symbol, bars, day, ip_score, pdl=pdl)

        # ── Steps 4-6: Filter + score (with per-signal regime check) ──
        results = []
        for sig, bar_idx, bar, atr, ema9, vwap, vol_ma in raw_signals:
            self.stats["raw_signals"] += 1

            # Step 2b: Per-signal regime check (real-time SPY position)
            if not self.regime.is_aligned_short(sig.timestamp):
                continue

            # Step 4: Rejection filters
            # Skip bigger_picture (long-only check, irrelevant for shorts)
            # Skip distance (shorts happen when price is extended below VWAP)
            reasons = self.rejection.check_all(
                bar, bars, bar_idx, atr, ema9, vwap, vol_ma,
                skip_filters=["bigger_picture", "distance"]
            )
            sig.reject_reasons = reasons

            if reasons:
                for r in reasons:
                    key = r.split("(")[0]
                    self.stats["reject_reasons"][key] = self.stats["reject_reasons"].get(key, 0) + 1
            else:
                self.stats["passed_rejection"] += 1

            # Step 5: Quality scoring
            # For shorts, regime score is inverted: RED=1.0, GREEN=0.0
            spy_pct = self.regime.get_spy_pct_from_open(bar.timestamp)
            day_open = day_bars[0].open
            # RS for shorts: negative RS is good (stock weaker than market)
            rs_mkt = (bar.close - day_open) / day_open - spy_pct if day_open > 0 else 0.0

            regime_snap = self.regime.get_nearest_regime(bar.timestamp)
            # Inverted regime scoring for shorts
            regime_score = {"RED": 1.0, "FLAT": 0.5, "GREEN": 0.0}.get(
                regime_snap.day_label if regime_snap else "", 0.5
            )
            alignment_score = 0.0
            if regime_snap:
                # For shorts: below VWAP = good, EMA9 < EMA20 = good
                if not regime_snap.spy_above_vwap:
                    alignment_score += 0.5
                if not regime_snap.ema9_above_ema20:
                    alignment_score += 0.5

            stock_factors = {
                "in_play_score": ip_score,
                "rs_market": -rs_mkt,  # negative RS = good for shorts
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

    def _detect_bdr_signals(self, symbol: str, bars: List[Bar], day: date,
                             ip_score: float, pdl: float = NaN) -> list:
        """
        Run BDR state machine across all bars for the day.
        Returns list of (StrategySignal, bar_idx, bar, atr, ema9, vwap, vol_ma).

        3-phase state machine:
          Phase 1: Breakdown detection (close below support level with volume)
          Phase 2: Retest tracking (price approaches level but fails to reclaim)
          Phase 3: Rejection confirmation (bearish bar with big upper wick)
        """
        cfg = self.cfg
        time_start = cfg.get(cfg.bdr_time_start)
        time_end = cfg.get(cfg.bdr_time_end)
        retest_window = cfg.get(cfg.bdr_retest_window)
        confirm_window = cfg.get(cfg.bdr_confirm_window)

        # Indicators
        ema9 = EMA(9)
        vwap_calc = VWAPCalc()
        atr_pair = ATRPair(14, use_completed=True)
        vol_buf = deque(maxlen=20)
        range_buf = deque(maxlen=10)

        # OR tracking
        or_high = NaN
        or_low = NaN
        or_ready = False

        # State machine
        bd_active = False
        bd_level = NaN
        bd_level_tag = ""
        bd_bar_vol = 0.0
        bars_since_bd = 999
        retested = False
        bars_since_retest = 999
        retest_bar_high = NaN
        retest_bar_low = NaN
        retest_vol = 0.0

        recent_bars = deque(maxlen=10)
        prev_date = None
        triggered_today = False
        session_low = NaN  # running session low for structural target
        signals = []

        for i, bar in enumerate(bars):
            d = bar.timestamp.date()
            hhmm = get_hhmm(bar)

            # Day reset
            if d != prev_date:
                vwap_calc.reset()
                bd_active = False
                retested = False
                triggered_today = False
                or_high = NaN
                or_low = NaN
                or_ready = False
                session_low = NaN
                recent_bars.clear()
                prev_date = d

            # Track running session low
            if d == day:
                session_low = min(session_low, bar.low) if not _isnan(session_low) else bar.low

            # Update indicators
            e9 = ema9.update(bar.close)
            i_atr = atr_pair.update_intraday(bar.high, bar.low, bar.close)
            tp = (bar.high + bar.low + bar.close) / 3.0
            vw = vwap_calc.update(tp, bar.volume)
            vol_buf.append(bar.volume)
            vol_ma = sum(vol_buf) / len(vol_buf) if len(vol_buf) >= 5 else NaN
            rng = bar.high - bar.low
            range_buf.append(rng)

            # OR tracking (first 30 min)
            if d == day and not or_ready:
                if hhmm <= 959:
                    or_high = max(or_high, bar.high) if not _isnan(or_high) else bar.high
                    or_low = min(or_low, bar.low) if not _isnan(or_low) else bar.low
                else:
                    or_ready = True

            recent_bars.append(bar)

            if d != day:
                continue

            # Must be in time window for new detections
            if not (time_start <= hhmm <= time_end) and not bd_active:
                continue

            if triggered_today or not ema9.ready or _isnan(i_atr) or i_atr <= 0:
                continue
            if _isnan(vol_ma) or vol_ma <= 0:
                continue

            # Tick state counters
            if bd_active:
                bars_since_bd += 1
                # Hard expiry
                if bars_since_bd > retest_window + confirm_window + 2:
                    bd_active = False
                    continue

            if retested:
                bars_since_retest += 1

            # ══════════════════════════════════════════════════
            # Phase 1: BREAKDOWN DETECTION
            # ══════════════════════════════════════════════════
            if not bd_active and time_start <= hhmm <= time_end:
                level, tag = self._find_support_level(
                    or_high, or_low, or_ready, vw, list(recent_bars)
                )
                if _isnan(level):
                    continue

                # Check breakdown below level
                broke = bar.close < level - cfg.bdr_break_atr_min * i_atr
                bearish = bar.close < bar.open
                if not (broke and bearish):
                    continue

                # Range check
                if len(range_buf) >= 5:
                    med_rng = median(list(range_buf))
                    if rng < cfg.bdr_break_bar_range_frac * med_rng:
                        continue

                # Volume check
                if bar.volume < cfg.bdr_break_vol_frac * vol_ma:
                    continue

                # Close position (must close in bottom portion of bar)
                if rng > 0:
                    close_pct = (bar.high - bar.close) / rng
                    if close_pct < cfg.bdr_break_close_pct:
                        continue

                # Latch breakdown
                bd_active = True
                bd_level = level
                bd_level_tag = tag
                bd_bar_vol = bar.volume
                bars_since_bd = 0
                retested = False
                bars_since_retest = 999
                retest_bar_high = NaN
                retest_bar_low = NaN
                retest_vol = 0.0
                continue

            # ══════════════════════════════════════════════════
            # Phase 2: RETEST DETECTION
            # ══════════════════════════════════════════════════
            if bd_active and not retested and bars_since_bd <= retest_window:
                proximity = cfg.bdr_retest_proximity_atr * i_atr
                max_reclaim = cfg.bdr_retest_max_reclaim_atr * i_atr

                approaches = bar.high >= bd_level - proximity
                fails_reclaim = bar.close < bd_level
                not_blasted = bar.high <= bd_level + max_reclaim

                if approaches and fails_reclaim and not_blasted:
                    retested = True
                    retest_bar_high = bar.high
                    retest_bar_low = bar.low
                    retest_vol = bar.volume
                    bars_since_retest = 0
                    continue

            # ══════════════════════════════════════════════════
            # Phase 3: REJECTION CONFIRMATION
            # ══════════════════════════════════════════════════
            if retested and bars_since_retest <= confirm_window:
                # Update retest high/low for multi-bar retests
                if bar.high > retest_bar_high:
                    retest_bar_high = bar.high
                retest_vol += bar.volume

                bearish = bar.close < bar.open
                closes_below_retest_low = bar.close < retest_bar_low

                # Big rejection wick check (KEY filter)
                upper_wick = bar.high - max(bar.close, bar.open)
                wick_pct = upper_wick / rng if rng > 0 else 0.0

                if (bearish and closes_below_retest_low and
                        wick_pct >= cfg.bdr_min_rejection_wick_pct):

                    # ── REJECTION FIRES ──
                    # Stop above retest high
                    stop = retest_bar_high + cfg.bdr_stop_buffer_atr * i_atr
                    min_stop = cfg.bdr_min_stop_atr * i_atr
                    risk = stop - bar.close
                    if risk < min_stop:
                        stop = bar.close + min_stop
                        risk = min_stop
                    if risk <= 0:
                        bd_active = False
                        retested = False
                        continue

                    # ── Structural target computation ──
                    target, target_rr, target_tag, skipped = self._compute_structural_target(
                        bar.close, risk, session_low, pdl, bd_level, cfg,
                    )
                    if skipped:
                        bd_active = False
                        retested = False
                        continue

                    # Structure quality
                    struct_q = 0.50
                    if bd_level_tag == "ORL":
                        struct_q += 0.15  # structural level
                    if bd_bar_vol >= 1.25 * vol_ma:
                        struct_q += 0.10  # strong BD volume
                    retest_avg_vol = retest_vol / max(bars_since_retest, 1)
                    if retest_avg_vol < bd_bar_vol:
                        struct_q += 0.10  # weak retest
                    if bar.close < vw and bar.close < e9:
                        struct_q += 0.15  # bearish confluence

                    confluence = []
                    if bar.close < vw:
                        confluence.append("below_vwap")
                    if bar.close < e9:
                        confluence.append("below_ema9")
                    if bd_level_tag == "ORL":
                        confluence.append("or_level")
                    if wick_pct >= 0.40:
                        confluence.append("strong_wick")
                    if bd_bar_vol >= 1.5 * vol_ma:
                        confluence.append("strong_bd_vol")

                    sig = StrategySignal(
                        strategy_name="BDR_SHORT",
                        symbol=symbol,
                        timestamp=bar.timestamp,
                        direction=-1,  # SHORT
                        entry_price=bar.close,
                        stop_price=stop,
                        target_price=target,
                        in_play_score=ip_score,
                        confluence_tags=confluence,
                        metadata={
                            "bd_level": bd_level,
                            "bd_level_tag": bd_level_tag,
                            "bd_bar_vol_ratio": bd_bar_vol / vol_ma if vol_ma > 0 else 0,
                            "retest_bar_high": retest_bar_high,
                            "wick_pct": wick_pct,
                            "bars_since_bd": bars_since_bd,
                            "structure_quality": min(struct_q, 1.0),
                            "target_tag": target_tag,
                            "actual_rr": target_rr,
                            "session_low": session_low,
                            "pdl": pdl,
                        },
                    )

                    signals.append((sig, i, bar, i_atr, e9, vw, vol_ma))
                    bd_active = False
                    retested = False
                    triggered_today = True

        return signals

    @staticmethod
    def _compute_pdl(bars: List[Bar], day: date) -> float:
        """Compute prior day low from bar data. Scans backwards for efficiency."""
        prev_day = None
        pdl = NaN
        # Walk backwards to find bars from the most recent prior day
        for i in range(len(bars) - 1, -1, -1):
            d = bars[i].timestamp.date()
            if d >= day:
                continue
            if prev_day is None:
                prev_day = d
                pdl = bars[i].low
            elif d == prev_day:
                if bars[i].low < pdl:
                    pdl = bars[i].low
            else:
                break  # moved to an even earlier day, done
        return pdl

    @staticmethod
    def _compute_structural_target(
        entry: float, risk: float, session_low: float, pdl: float,
        bd_level: float, cfg: 'StrategyConfig',
    ) -> tuple:
        """
        Compute structural short target for BDR_SHORT.

        Candidates (all must be BELOW entry for a short):
          1. Session low — lowest low from open to signal time
          2. PDL (prior day low) — key downside reference
          3. Breakdown extension — bd_level - 1.5 * (bd_level - entry)

        Selection: nearest viable candidate (>= min_rr), capped at max_rr.
        Returns: (target_price, actual_rr, target_tag, skipped)
        """
        if cfg.bdr_target_mode == "fixed_rr":
            target = entry - risk * cfg.bdr_target_rr
            return target, cfg.bdr_target_rr, "fixed_rr", False

        min_rr = cfg.bdr_struct_min_rr
        max_rr = cfg.bdr_struct_max_rr

        candidates = []  # (target_price, tag)

        # 1. Session low
        if not _isnan(session_low) and session_low < entry:
            candidates.append((session_low, "session_low"))

        # 2. Prior day low
        if not _isnan(pdl) and pdl < entry:
            candidates.append((pdl, "pdl"))

        # 3. Breakdown extension: price broke below bd_level, project continuation
        #    extension = bd_level - 1.5 * (bd_level - entry)
        #    Since entry < bd_level, (bd_level - entry) > 0, so extension < entry
        if not _isnan(bd_level) and bd_level > entry:
            ext = bd_level - 1.5 * (bd_level - entry)
            candidates.append((ext, "bd_extension"))

        if not candidates:
            # No structural target available — skip this signal
            return NaN, 0.0, "none", True

        # Compute R:R for each candidate
        viable = []
        for price, tag in candidates:
            rr = (entry - price) / risk
            if rr >= min_rr:
                # Cap at max_rr
                capped_rr = min(rr, max_rr)
                capped_price = entry - capped_rr * risk
                viable.append((capped_price, capped_rr, tag))

        if not viable:
            # All structural targets too close (< min_rr) — skip
            return NaN, 0.0, "too_close", True

        # Pick nearest viable target (smallest R:R — most conservative)
        viable.sort(key=lambda x: x[1])
        target_price, actual_rr, tag = viable[0]
        return target_price, actual_rr, tag, False

    @staticmethod
    def _find_support_level(or_high, or_low, or_ready, vwap, recent_bars):
        """Find support level for breakdown. Short: OR low, VWAP, or swing low."""
        candidates = []

        if or_ready and not _isnan(or_low):
            candidates.append((or_low, "ORL"))

        if not _isnan(vwap) and vwap > 0:
            candidates.append((vwap, "VWAP"))

        # Swing low from recent bars (exclude current)
        prior_bars = recent_bars[:-1] if len(recent_bars) > 1 else recent_bars
        if len(prior_bars) >= 5:
            swing_low = min(b.low for b in prior_bars)
            candidates.append((swing_low, "SWING"))

        if not candidates:
            return NaN, ""

        # For shorts, pick the HIGHEST support level (most likely to break)
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0]
