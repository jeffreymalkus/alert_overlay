"""
ORH_FBO_SHORT — Opening Range High Failed Breakout Short.

4-phase state machine:
  Phase 1 (Breakout): Bar closes decisively above ORH with volume — traps breakout longs
  Phase 2 (Failure):  Price fails to hold above ORH, closes back below within tight window
  Phase 3 (Retest):   Price retests ORH from below (approaches but fails to reclaim)
  Phase 4 (Confirm):  Bearish rejection bar with upper wick confirms trapped longs

SHORT ONLY. ORH-only for v1 (no PDH, no PMH).
Environment: allows RED, FLAT, mildly GREEN. Blocks strongly bullish tape.
Time window: 10:00-13:00 (after OR forms, before afternoon noise).
Target: VWAP (with fixed RR fallback if VWAP too close).

Tightening vs design spec draft:
  - ORH only (no PDH/PMH)
  - Tight failure window: 4 bars (20 min) — real failures fail fast
  - 4-phase instead of 3-phase: breakout → failure → retest-from-below → confirm
  - Breakout must be meaningful: close ≥0.15 ATR above ORH with body ≥40%
  - Regime: permissive (blocks only strongly bullish). Not the old GREEN-only gate.
  - VWAP target always, fixed RR only if VWAP < 0.5 ATR away

Usage (via replay.py):
    from .orh_failed_bo_short import ORHFailedBOShortStrategy
    strategy = ORHFailedBOShortStrategy(cfg, in_play, regime, rejection, quality)
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
    compute_structural_target_short,
)
from .shared.level_helpers import breakout_quality

_isnan = math.isnan


class ORHFailedBOShortStrategy:
    """
    ORH Failed Breakout Short: breakout above ORH → failure → retest from below → rejection.

    Pipeline:
    1. In-play check (day level)
    2. Market regime check (per-signal, failed-short gate)
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

        # ── Step 2: Regime — deferred to per-signal (real-time check) ──
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
            if not self.regime.is_aligned_failed_short(sig.timestamp):
                continue

            # Step 4: Rejection filters
            # Skip bigger_picture (long-only), skip distance (we want price near ORH, not extended from VWAP)
            # Skip trigger_weakness — 4-phase structure IS the quality filter, not the trigger bar itself
            reasons = self.rejection.check_all(
                bar, bars, bar_idx, atr, ema9, vwap, vol_ma,
                skip_filters=["bigger_picture", "distance", "trigger_weakness"]
            )
            sig.reject_reasons = reasons

            if reasons:
                for r in reasons:
                    key = r.split("(")[0]
                    self.stats["reject_reasons"][key] = self.stats["reject_reasons"].get(key, 0) + 1
            else:
                self.stats["passed_rejection"] += 1

            # Step 5: Quality scoring (inverted for shorts)
            spy_pct = self.regime.get_spy_pct_from_open(bar.timestamp)
            day_open = day_bars[0].open
            rs_mkt = (bar.close - day_open) / day_open - spy_pct if day_open > 0 else 0.0

            regime_snap = self.regime.get_nearest_regime(bar.timestamp)
            # Inverted: RED=1.0, FLAT=0.5, GREEN=0.0
            regime_score = {"RED": 1.0, "FLAT": 0.5, "GREEN": 0.0}.get(
                regime_snap.day_label if regime_snap else "", 0.5
            )
            alignment_score = 0.0
            if regime_snap:
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

    def _detect_signals(self, symbol: str, bars: List[Bar], day: date,
                        ip_score: float) -> list:
        """
        4-phase state machine:
          Phase 1: Breakout above ORH (meaningful, with volume)
          Phase 2: Failure — price closes back below ORH within tight window
          Phase 3: Retest from below — approaches ORH but fails to reclaim
          Phase 4: Rejection — bearish bar with upper wick confirms
        """
        cfg = self.cfg
        time_start = cfg.get(cfg.orh_time_start)
        time_end = cfg.get(cfg.orh_time_end)
        failure_window = cfg.get(cfg.orh_failure_window)
        retest_window = cfg.get(cfg.orh_retest_window)

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
        IDLE, BROKE_OUT, FAILED, RETESTED = 0, 1, 2, 3
        phase = IDLE
        bo_bar_idx = 0
        bo_bar_vol = 0.0
        bo_bar_close = NaN
        bo_quality = 0.0
        bars_since_bo = 0
        fail_bar_idx = 0
        bars_since_fail = 0
        retest_high = NaN
        retest_low = NaN
        retest_vol = 0.0
        bars_since_retest = 0

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
                prev_date = d

            # Update indicators
            e9 = ema9.update(bar.close)
            e20 = ema20.update(bar.close)
            i_atr = atr_pair.update_intraday(bar.high, bar.low, bar.close)
            tp = (bar.high + bar.low + bar.close) / 3.0
            vw = vwap_calc.update(tp, bar.volume)
            vol_buf.append(bar.volume)
            vol_ma = sum(vol_buf) / len(vol_buf) if len(vol_buf) >= 5 else NaN

            # OR tracking (first 30 min)
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
            if not or_ready or _isnan(or_high):
                continue

            rng = bar.high - bar.low

            # Tick state counters
            if phase == BROKE_OUT:
                bars_since_bo += 1
                if bars_since_bo > failure_window:
                    phase = IDLE  # breakout held = no trade (accepted)
                    continue
            elif phase == FAILED:
                bars_since_fail += 1
                if bars_since_fail > retest_window:
                    phase = IDLE  # no retest = no trade
                    continue
            elif phase == RETESTED:
                bars_since_retest += 1
                if bars_since_retest > 3:  # tight confirm window: 3 bars
                    phase = IDLE
                    continue

            # ════════════════════════════════════════
            # Phase 1: BREAKOUT above ORH
            # ════════════════════════════════════════
            if phase == IDLE and time_start <= hhmm <= time_end:
                # Must close above ORH by meaningful distance
                dist_above = bar.close - or_high
                if dist_above < cfg.orh_break_min_dist_atr * i_atr:
                    continue

                # Body check
                if bar_body_ratio(bar) < cfg.orh_break_body_min:
                    continue

                # Volume check
                if bar.volume < cfg.orh_break_vol_frac * vol_ma:
                    continue

                # Must be bullish (close > open) — a real breakout
                if bar.close <= bar.open:
                    continue

                # Latch breakout
                phase = BROKE_OUT
                bo_bar_idx = i
                bo_bar_vol = bar.volume
                bo_bar_close = bar.close
                bo_quality = breakout_quality(bar, or_high, i_atr, vol_ma, 1)
                bars_since_bo = 0
                continue

            # ════════════════════════════════════════
            # Phase 2: FAILURE — closes back below ORH
            # ════════════════════════════════════════
            if phase == BROKE_OUT:
                if bar.close < or_high:
                    # Failed! Price gave back the breakout within the tight window
                    phase = FAILED
                    fail_bar_idx = i
                    bars_since_fail = 0
                    retest_high = NaN
                    retest_low = NaN
                    retest_vol = 0.0
                continue

            # ════════════════════════════════════════
            # Phase 3: RETEST from below
            # ════════════════════════════════════════
            if phase == FAILED:
                proximity = cfg.orh_retest_proximity_atr * i_atr
                max_reclaim = cfg.orh_retest_max_reclaim_atr * i_atr

                # High approaches ORH from below
                approaches = bar.high >= or_high - proximity
                # But closes below ORH (fails to reclaim)
                fails_reclaim = bar.close < or_high
                # Doesn't blast through ORH
                not_blasted = bar.high <= or_high + max_reclaim

                if approaches and fails_reclaim and not_blasted:
                    phase = RETESTED
                    retest_high = bar.high
                    retest_low = bar.low
                    retest_vol = bar.volume
                    bars_since_retest = 0
                continue

            # ════════════════════════════════════════
            # Phase 4: REJECTION CONFIRMATION
            # ════════════════════════════════════════
            if phase == RETESTED:
                # Track retest high
                if bar.high > retest_high:
                    retest_high = bar.high
                retest_vol += bar.volume

                bearish = bar.close < bar.open
                # Upper wick check
                upper_wick = bar.high - max(bar.close, bar.open)
                wick_pct = upper_wick / rng if rng > 0 else 0.0

                # Body check
                body_ratio = bar_body_ratio(bar)

                if (bearish and
                        body_ratio >= cfg.orh_confirm_body_min and
                        wick_pct >= cfg.orh_confirm_wick_min):

                    # ── SIGNAL FIRES ──
                    # Stop above retest high
                    stop = retest_high + cfg.orh_stop_buffer_atr * i_atr
                    min_stop = cfg.orh_min_stop_atr * i_atr
                    risk = stop - bar.close
                    if risk < min_stop:
                        stop = bar.close + min_stop
                        risk = min_stop
                    if risk <= 0:
                        phase = IDLE
                        continue

                    # Target: structural targets (VWAP, OR Low, Session Low)
                    entry = bar.close
                    target_tag = "fixed_rr"

                    if cfg.orh_target_mode == "structural":
                        _candidates = []
                        if not _isnan(vw) and vw < entry:
                            _candidates.append((vw, "vwap"))
                        if not _isnan(or_low) and or_low < entry:
                            _candidates.append((or_low, "orl"))
                        # session_low would be tracked separately; skipping for now

                        target, actual_rr, target_tag, skipped = compute_structural_target_short(
                            entry, risk, _candidates,
                            min_rr=cfg.orh_struct_min_rr, max_rr=cfg.orh_struct_max_rr,
                            fallback_rr=cfg.orh_target_rr, mode="structural",
                        )
                        if skipped:
                            phase = IDLE
                            continue
                    else:
                        target = entry - risk * cfg.orh_target_rr
                        actual_rr = cfg.orh_target_rr
                        target_tag = "fixed_rr"

                    # Structure quality
                    struct_q = 0.50
                    if bo_quality >= 0.60:
                        struct_q += 0.15  # strong breakout = more trapped longs
                    if bars_since_bo <= 2:
                        struct_q += 0.10  # very fast failure = more trapped
                    retest_avg_vol = retest_vol / max(bars_since_retest + 1, 1)
                    if retest_avg_vol < bo_bar_vol:
                        struct_q += 0.10  # weak retest = bullish exhaustion
                    if bar.close < e9:
                        struct_q += 0.10  # below EMA9 = bearish
                    if wick_pct >= 0.40:
                        struct_q += 0.05  # strong wick

                    confluence = ["orh_failed_bo"]
                    if bar.close < vw:
                        confluence.append("below_vwap")
                    if bar.close < e9:
                        confluence.append("below_ema9")
                    if wick_pct >= 0.40:
                        confluence.append("strong_wick")
                    if bo_quality >= 0.60:
                        confluence.append("strong_trap")
                    if bars_since_bo <= 2:
                        confluence.append("fast_failure")

                    sig = StrategySignal(
                        strategy_name="ORH_FBO_SHORT",
                        symbol=symbol,
                        timestamp=bar.timestamp,
                        direction=-1,  # SHORT
                        entry_price=bar.close,
                        stop_price=stop,
                        target_price=target,
                        in_play_score=ip_score,
                        confluence_tags=confluence,
                        metadata={
                            "or_high": or_high,
                            "bo_quality": bo_quality,
                            "bo_bar_vol_ratio": bo_bar_vol / vol_ma if vol_ma > 0 else 0,
                            "bars_to_failure": bars_since_bo,
                            "retest_high": retest_high,
                            "wick_pct": wick_pct,
                            "actual_rr": actual_rr,
                            "target_tag": target_tag,
                            "structure_quality": min(struct_q, 1.0),
                        },
                    )

                    signals.append((sig, i, bar, i_atr, e9, vw, vol_ma))
                    phase = IDLE
                    triggered_today = True

        return signals
