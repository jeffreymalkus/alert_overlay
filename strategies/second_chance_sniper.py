"""
SecondChance_Sniper — Breakout → Retest → Confirm with full 6-step pipeline.
Adapts SC state machine from engine.py but adds in-play, regime, rejection, quality gates.

Long-only. Breakout above key level → pullback to level → hold → expansion confirm.

Usage (via replay.py):
    from .second_chance_sniper import SecondChanceSniperStrategy
    strategy = SecondChanceSniperStrategy(cfg, in_play, regime, rejection, quality)
    signals = strategy.scan_day(symbol, bars, day, spy_bars, sector_bars)
"""

import math
from collections import deque
from datetime import date, datetime
from statistics import median
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


class SecondChanceSniperStrategy:
    """
    Second Chance Sniper: breakout-retest-confirm with full pipeline.

    Pipeline:
    1. In-play check (day level)
    2. Market regime check (day level)
    3. Raw SC detection (bar-by-bar state machine)
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

        # Pipeline counters for diagnostics
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
        # Use first bar of day as approximate timestamp for regime
        day_bars = [b for b in bars if b.timestamp.date() == day]
        if not day_bars:
            return []

        first_bar_ts = day_bars[0].timestamp
        if not self.regime.is_aligned_long(first_bar_ts):
            return []
        self.stats["passed_regime"] += 1

        # ── Step 3: Raw SC detection (state machine) ──
        raw_signals = self._detect_sc_signals(symbol, bars, day, ip_score)

        # ── Steps 4-6: Filter + score each raw signal ──
        results = []
        for sig, bar_idx, bar, atr, ema9, vwap, vol_ma in raw_signals:
            self.stats["raw_signals"] += 1

            # Step 4: Rejection filters
            # Skip distance + bigger_picture: breakout entries are inherently extended from VWAP
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
            # Compute RS
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
                "rs_sector": 0.0,  # TODO: compute if sector bars available
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

            # If signal was rejected, force B/C tier
            if reasons:
                tier = QualityTier.B_TIER if score >= cfg.quality_b_min else QualityTier.C_TIER

            sig.quality_tier = tier
            sig.quality_score = score
            sig.market_regime = regime_snap.day_label if regime_snap else ""

            # Step 6: Track tiers
            if tier == QualityTier.A_TIER:
                self.stats["a_tier"] += 1
            elif tier == QualityTier.B_TIER:
                self.stats["b_tier"] += 1
            else:
                self.stats["c_tier"] += 1

            results.append(sig)

        return results

    def _detect_sc_signals(self, symbol: str, bars: List[Bar], day: date,
                           ip_score: float) -> list:
        """
        Run SC state machine across all bars for the day.
        Returns list of (StrategySignal, bar_idx, bar, atr, ema9, vwap, vol_ma).
        """
        cfg = self.cfg
        time_start = cfg.get(cfg.sc_time_start)
        time_end = cfg.get(cfg.sc_time_end)
        retest_window = cfg.get(cfg.sc_retest_window)
        confirm_window = cfg.get(cfg.sc_confirm_window)

        # Indicators
        ema9 = EMA(9)
        vwap_calc = VWAPCalc()
        atr_pair = ATRPair(14, use_completed=True)
        vol_buf = deque(maxlen=20)
        range_buf = deque(maxlen=10)

        # State machine state
        active = False
        level = NaN
        level_tag = ""
        bo_high = NaN
        bo_low = NaN
        bo_vol = 0.0
        bars_since_bo = 0
        retested = False
        bars_since_retest = 0
        retest_bar_high = NaN
        retest_bar_low = NaN
        lowest_since_retest = NaN
        triggered_today = False

        # OR tracking
        or_high = NaN
        or_low = NaN
        or_ready = False
        recent_bars = deque(maxlen=10)

        # Session high tracking
        session_high = NaN
        prior_day_high = NaN

        prev_date = None
        signals = []

        for i, bar in enumerate(bars):
            d = bar.timestamp.date()
            hhmm = get_hhmm(bar)

            # Day reset
            if d != prev_date:
                vwap_calc.reset()
                active = False
                triggered_today = False
                or_high = NaN
                or_low = NaN
                or_ready = False
                recent_bars.clear()
                # Track prior day high before resetting session high
                prior_day_high = session_high
                session_high = NaN
                prev_date = d

            # Skip non-target days (day=None means detect on all days)
            if day is not None and d != day:
                # Still update indicators for warmup
                e9 = ema9.update(bar.close)
                atr_pair.update_intraday(bar.high, bar.low, bar.close)
                tp = (bar.high + bar.low + bar.close) / 3.0
                vwap_calc.update(tp, bar.volume)
                vol_buf.append(bar.volume)
                range_buf.append(bar.high - bar.low)
                recent_bars.append(bar)
                # Track prior day high
                prior_day_high = max(prior_day_high, bar.high) if not _isnan(prior_day_high) else bar.high
                continue

            # Update indicators
            e9 = ema9.update(bar.close)
            i_atr = atr_pair.update_intraday(bar.high, bar.low, bar.close)
            tp = (bar.high + bar.low + bar.close) / 3.0
            vw = vwap_calc.update(tp, bar.volume)

            vol_buf.append(bar.volume)
            vol_ma = sum(vol_buf) / len(vol_buf) if len(vol_buf) >= 5 else NaN

            rng = bar.high - bar.low
            range_buf.append(rng)

            # Track session high for target day
            session_high = max(session_high, bar.high) if not _isnan(session_high) else bar.high

            # OR tracking (first 30 min = first 6 bars for 5-min)
            if not or_ready:
                if hhmm <= 959:
                    or_high = max(or_high, bar.high) if not _isnan(or_high) else bar.high
                    or_low = min(or_low, bar.low) if not _isnan(or_low) else bar.low
                else:
                    or_ready = True

            recent_bars.append(bar)

            # Only detect in time window
            if not (time_start <= hhmm <= time_end):
                # Tick state if active
                if active:
                    bars_since_bo += 1
                    if bars_since_bo > 12:
                        active = False
                continue

            if triggered_today or not ema9.ready or _isnan(i_atr) or i_atr <= 0:
                continue
            if _isnan(vol_ma) or vol_ma <= 0:
                continue

            bar_bullish = bar.close > bar.open
            direction = 1  # long only

            # ── Step 1: Detect breakout ──
            if not active:
                key_level, tag = self._find_key_level(
                    or_high, or_low, or_ready, list(recent_bars), direction
                )
                if _isnan(key_level):
                    continue

                broke = bar.close > key_level + cfg.sc_break_atr_min * i_atr
                if not broke:
                    continue

                # Range check
                if len(range_buf) >= 5:
                    med_rng = median(list(range_buf))
                    if rng < cfg.sc_break_bar_range_frac * med_rng:
                        continue

                # Volume check
                if bar.volume < cfg.sc_break_vol_frac * vol_ma:
                    continue
                if bar.volume < cfg.sc_strong_bo_vol_mult * vol_ma:
                    continue

                # Close position check
                if rng > 0:
                    close_pct = (bar.close - bar.low) / rng
                    if close_pct < cfg.sc_break_close_pct:
                        continue

                # Latch breakout
                active = True
                level = key_level
                level_tag = tag
                bo_high = bar.high
                bo_low = bar.low
                bo_vol = bar.volume
                bars_since_bo = 0
                retested = False
                bars_since_retest = 999
                retest_bar_high = NaN
                retest_bar_low = NaN
                lowest_since_retest = NaN
                continue

            # ── Active breakout ──
            bars_since_bo += 1

            # Expire
            if bars_since_bo > retest_window + confirm_window + 1:
                active = False
                continue

            # ── Step 2: Detect retest ──
            if not retested and bars_since_bo <= retest_window:
                proximity = cfg.sc_retest_proximity_atr * i_atr
                max_depth = cfg.sc_retest_max_depth_atr * i_atr

                touches = bar.low <= level + proximity
                holds = bar.close > level
                not_deep = bar.low >= level - max_depth
                not_bearish_exp = not (bar.close < bar.open and rng > 1.5 * i_atr)
                vol_ok = bar.volume <= cfg.sc_retest_max_vol_frac * vol_ma

                if touches and holds and not_deep and not_bearish_exp and vol_ok:
                    retested = True
                    retest_bar_high = bar.high
                    retest_bar_low = bar.low
                    bars_since_retest = 0
                    lowest_since_retest = bar.low
                    continue

            # ── Step 3: Confirmation ──
            if retested:
                bars_since_retest += 1

                # Track lowest since retest
                if _isnan(lowest_since_retest) or bar.low < lowest_since_retest:
                    lowest_since_retest = bar.low

                if bars_since_retest > confirm_window:
                    active = False
                    continue

                confirmed = (bar.close > retest_bar_high and
                             bar_bullish and
                             bar.volume >= cfg.sc_confirm_vol_frac * vol_ma and
                             bar.close > vw and bar.close > e9)

                if confirmed:
                    # Compute stop
                    raw_stop = min(retest_bar_low,
                                  lowest_since_retest if not _isnan(lowest_since_retest) else retest_bar_low)
                    stop = raw_stop - cfg.sc_stop_buffer
                    min_stop = 0.15 * i_atr
                    if abs(bar.close - stop) < min_stop:
                        stop = bar.close - min_stop

                    risk = bar.close - stop
                    if risk <= 0:
                        active = False
                        continue

                    # Structural target: measured move, session high, PDH
                    if cfg.sc_target_mode == "structural":
                        _candidates = []
                        # Measured move: entry + (breakout high - retest low)
                        retest_ref = retest_bar_low if not _isnan(retest_bar_low) else level
                        mm = bar.close + (bo_high - retest_ref)
                        if mm > bar.close:
                            _candidates.append((mm, "measured_move"))
                        if not _isnan(session_high) and session_high > bar.close:
                            _candidates.append((session_high, "session_high"))
                        if not _isnan(prior_day_high) and prior_day_high > bar.close:
                            _candidates.append((prior_day_high, "pdh"))
                        target, actual_rr, target_tag, skipped = compute_structural_target_long(
                            bar.close, risk, _candidates,
                            min_rr=cfg.sc_struct_min_rr, max_rr=cfg.sc_struct_max_rr,
                            fallback_rr=cfg.sc_target_rr, mode="structural",
                        )
                        if skipped:
                            active = False
                            continue
                    else:
                        target = bar.close + risk * cfg.sc_target_rr
                        actual_rr = cfg.sc_target_rr
                        target_tag = "fixed_rr"

                    # Structure quality for scoring
                    struct_q = 0.5
                    if level_tag == "ORH":
                        struct_q += 0.2
                    if bo_vol >= 1.25 * vol_ma:
                        struct_q += 0.15
                    if bar.volume < bo_vol:
                        struct_q += 0.15  # declining vol into retest = good

                    confluence = []
                    if bar.close > vw:
                        confluence.append("above_vwap")
                    if bar.close > e9:
                        confluence.append("above_ema9")
                    if level_tag == "ORH":
                        confluence.append("or_level")
                    if bo_vol >= 1.5 * vol_ma:
                        confluence.append("strong_bo_vol")

                    sig = StrategySignal(
                        strategy_name="SC_SNIPER",
                        symbol=symbol,
                        timestamp=bar.timestamp,
                        direction=1,
                        entry_price=bar.close,
                        stop_price=stop,
                        target_price=target,
                        in_play_score=ip_score,
                        confluence_tags=confluence,
                        metadata={
                            "level": level,
                            "level_tag": level_tag,
                            "bo_vol": bo_vol,
                            "retest_low": retest_bar_low,
                            "bars_since_bo": bars_since_bo,
                            "actual_rr": actual_rr,
                            "target_tag": target_tag,
                            "structure_quality": min(struct_q, 1.0),
                        },
                    )

                    signals.append((sig, i, bar, i_atr, e9, vw, vol_ma))
                    active = False
                    triggered_today = True

        return signals

    @staticmethod
    def _find_key_level(or_high, or_low, or_ready, recent_bars, direction):
        """Find breakout level. Long only: OR high or swing high.
        recent_bars includes current bar as last element — exclude it for swing."""
        candidates = []

        if or_ready and not _isnan(or_high):
            candidates.append((or_high, "ORH"))

        # Exclude last bar (current) from swing calculation
        prior_bars = recent_bars[:-1] if len(recent_bars) > 1 else recent_bars
        if len(prior_bars) >= 5:
            swing_high = max(b.high for b in prior_bars)
            candidates.append((swing_high, "SWING"))

        if not candidates:
            return NaN, ""

        # For longs, pick the highest level
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0]
