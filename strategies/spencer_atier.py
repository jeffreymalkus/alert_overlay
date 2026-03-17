"""
Spencer_A_Tier_Only — Tight consolidation → breakout with trend context.
Adapts _detect_spencer from engine.py with full 6-step pipeline.

Long-only. Uptrend → tight box → breakout above box high.

Usage (via replay.py):
    from .spencer_atier import SpencerATierStrategy
    strategy = SpencerATierStrategy(cfg, in_play, regime, rejection, quality)
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
    simulate_strategy_trade, compute_daily_atr, compute_structural_target_long,
)

_isnan = math.isnan


class SpencerATierStrategy:
    """
    Spencer A-Tier: uptrend → tight consolidation box → breakout.

    Pipeline:
    1. In-play check (day level)
    2. Market regime check (day level)
    3. Raw Spencer detection (bar-by-bar consolidation box scan)
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
                 sector_bars: Optional[List[Bar]] = None,
                 daily_atr: Optional[Dict[date, float]] = None) -> List[StrategySignal]:
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

        # ── Step 3: Raw Spencer detection ──
        raw_signals = self._detect_sp_signals(symbol, bars, day, ip_score, daily_atr)

        # ── Steps 4-6: Filter + score ──
        results = []
        for sig, bar_idx, bar, atr, ema9, vwap, vol_ma in raw_signals:
            self.stats["raw_signals"] += 1

            # Step 4: Universal rejection filters
            # Skip distance: consolidation breakouts push away from VWAP by design
            # Skip bigger_picture: trend preconditions already handle context
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

    def _detect_sp_signals(self, symbol: str, bars: List[Bar], day: date,
                           ip_score: float,
                           daily_atr: Optional[Dict[date, float]] = None) -> list:
        """
        Run Spencer state machine: trend context → box scan → breakout.
        Returns list of (StrategySignal, bar_idx, bar, atr, ema9, vwap, vol_ma).
        """
        cfg = self.cfg
        time_start = cfg.get(cfg.sp_time_start)
        time_end = cfg.get(cfg.sp_time_end)
        box_min = cfg.get(cfg.sp_box_min_bars)
        box_max = cfg.get(cfg.sp_box_max_bars)

        # Compute prior_day_high from previous day's bars
        prior_day_high = NaN
        prev_date = None
        for bar in bars:
            d = bar.timestamp.date()
            if d >= day:
                break
            prev_date = d

        if prev_date is not None:
            prior_bars = [b for b in bars if b.timestamp.date() == prev_date]
            if prior_bars:
                prior_day_high = max(b.high for b in prior_bars)

        # Indicators
        ema9 = EMA(9)
        ema20 = EMA(20)
        vwap_calc = VWAPCalc()
        atr_pair = ATRPair(14, use_completed=True)
        vol_buf = deque(maxlen=20)

        # Per-bar EMA9 tracking for slope detection
        prior_e9 = NaN

        # Session state
        session_high = NaN
        session_low = NaN
        session_open = NaN
        triggered = False

        # Recent bars for box scanning
        recent = deque(maxlen=box_max + 2)

        prev_date = None
        signals = []

        for i, bar in enumerate(bars):
            d = bar.timestamp.date()
            hhmm = get_hhmm(bar)

            # Day reset
            if d != prev_date:
                vwap_calc.reset()
                session_high = NaN
                session_low = NaN
                session_open = NaN
                triggered = False
                prior_e9 = NaN
                recent.clear()
                prev_date = d

            # Update indicators
            e9 = ema9.update(bar.close)
            e20 = ema20.update(bar.close)
            i_atr = atr_pair.update_intraday(bar.high, bar.low, bar.close)
            tp = (bar.high + bar.low + bar.close) / 3.0
            vw = vwap_calc.update(tp, bar.volume)
            vol_buf.append(bar.volume)
            vol_ma = sum(vol_buf) / len(vol_buf) if len(vol_buf) >= 5 else NaN

            # Track session extremes
            if _isnan(session_open):
                session_open = bar.open
            if _isnan(session_high) or bar.high > session_high:
                session_high = bar.high
            if _isnan(session_low) or bar.low < session_low:
                session_low = bar.low

            # Store EMA9 on bar for box scanning
            bar._e9 = e9  # monkey-patch for box below-EMA9 check

            recent.append(bar)

            if day is not None and d != day:
                prior_e9 = e9
                continue

            if _isnan(e9) or _isnan(e20) or _isnan(vw) or _isnan(i_atr) or i_atr <= 0:
                prior_e9 = e9
                continue
            if _isnan(vol_ma) or vol_ma <= 0:
                prior_e9 = e9
                continue
            if triggered:
                prior_e9 = e9
                continue

            if not (time_start <= hhmm <= time_end):
                prior_e9 = e9
                continue

            # ── Preconditions: trend context (long only) ──

            # EMA alignment: EMA9 > EMA20
            if not (ema9.ready and ema20.ready and e9 > e20):
                prior_e9 = e9
                continue

            # EMA9 slope (track for confluence, not hard gate)
            ema9_rising = not _isnan(prior_e9) and e9 > prior_e9

            # Price above VWAP
            if bar.close <= vw:
                prior_e9 = e9
                continue

            # Daily ATR for trend advance + extension checks
            d_atr = daily_atr.get(day, NaN) if daily_atr else NaN

            # Trend advance check
            if not _isnan(session_low) and not _isnan(d_atr) and d_atr > 0:
                advance = bar.close - session_low
                if advance < cfg.sp_trend_advance_atr * d_atr:
                    # Try VWAP alternative
                    vwap_advance = bar.close - vw
                    if vwap_advance < cfg.sp_trend_advance_vwap_atr * i_atr:
                        prior_e9 = e9
                        continue

            # Extension filter
            if not _isnan(session_open) and not _isnan(d_atr) and d_atr > 0:
                if (bar.close - session_open) > cfg.sp_extension_atr * d_atr:
                    prior_e9 = e9
                    continue

            # ── Scan for consolidation box ──
            if len(recent_list) < box_min + 1:
                prior_e9 = e9
                continue

            best_box = None
            for window in range(box_min, min(box_max + 1, len(recent_list))):
                box_bars = recent_list[-(window + 1):-1]  # N bars before current
                if len(box_bars) < box_min:
                    continue

                box_high = max(b.high for b in box_bars)
                box_low = min(b.low for b in box_bars)
                box_range = box_high - box_low

                # Range check
                if box_range > cfg.sp_box_max_range_atr * i_atr:
                    continue
                if box_range <= 0:
                    continue

                # Box tightness: <20% of day's range
                if not _isnan(session_high) and not _isnan(session_low):
                    day_range = session_high - session_low
                    if day_range > 0 and (box_range / day_range) > 0.20:
                        continue

                # Upper-half close check (long)
                box_mid = (box_high + box_low) / 2
                upper_closes = sum(1 for b in box_bars if b.close >= box_mid)
                if upper_closes / len(box_bars) < cfg.sp_box_upper_close_pct:
                    continue

                # Box midpoint in upper third of day's range
                if not _isnan(session_high) and not _isnan(session_low):
                    day_range = session_high - session_low
                    if day_range > 0:
                        box_position = (box_mid - session_low) / day_range
                        if box_position < 0.67:
                            continue

                # Max closes below EMA9
                below_ema9_count = 0
                for b in box_bars:
                    if hasattr(b, '_e9') and not _isnan(b._e9) and b.close < b._e9:
                        below_ema9_count += 1
                if below_ema9_count > cfg.sp_box_max_below_ema9:
                    continue

                # Volume check
                avg_vol = sum(b.volume for b in box_bars) / len(box_bars)
                if avg_vol < cfg.sp_box_min_vol_frac * vol_ma:
                    continue

                # Failed breakout filter
                failed_bo = 0
                threshold = cfg.sp_box_failed_bo_atr * i_atr
                for b in box_bars:
                    if b.high > box_high - threshold and b.close < box_high:
                        failed_bo += 1
                if failed_bo >= cfg.sp_box_failed_bo_limit:
                    continue

                best_box = (box_high, box_low, box_range, len(box_bars), avg_vol, failed_bo)
                break  # use first valid (shortest) window

            if best_box is None:
                prior_e9 = e9
                continue

            box_high, box_low, box_range, box_len, box_avg_vol, box_failed = best_box

            # ── Breakout trigger ──
            clearance = cfg.sp_break_clearance_atr * i_atr
            rng = bar.high - bar.low

            if bar.close <= box_high + clearance:
                prior_e9 = e9
                continue

            # Pre-break volume collapse detector (confluence bonus, not hard gate)
            pre_break_collapse = False
            if len(box_bars) >= 2:
                for k in range(-2, 0):
                    if k-1 >= -len(box_bars):
                        if box_bars[k].volume < 0.70 * box_bars[k-1].volume:
                            pre_break_collapse = True
                            break

            # Breakout volume
            if bar.volume < cfg.sp_break_vol_frac * vol_ma:
                prior_e9 = e9
                continue

            # Close position in bar (top 30%)
            if rng > 0:
                close_pct = (bar.close - bar.low) / rng
                if close_pct < cfg.sp_break_close_pct:
                    prior_e9 = e9
                    continue

            # Must remain above VWAP and EMA9
            if bar.close <= vw or bar.close <= e9:
                prior_e9 = e9
                continue

            # ── Entry price: breakout trigger ──
            trigger_price = box_high + 0.01
            entry_price = max(trigger_price, bar.open)

            # ── Stop ──
            stop = box_low - cfg.sp_stop_buffer
            min_stop = 0.15 * i_atr
            if abs(entry_price - stop) < min_stop:
                stop = entry_price - min_stop

            risk = entry_price - stop
            if risk <= 0:
                prior_e9 = e9
                continue

            # Structural target: measured moves only (SMB source: 1x, 2x, 3x box range)
            # Session_high/PDH removed — Spencer is a measured-move strategy,
            # not a structural-level strategy. Near-HOD targets produce tiny R:R.
            if cfg.sp_target_mode == "structural":
                _candidates = []
                mm1 = box_high + box_range  # 1x measured move
                mm2 = box_high + 2.0 * box_range  # 2x measured move
                mm3 = box_high + 3.0 * box_range  # 3x measured move
                if mm1 > entry_price:
                    _candidates.append((mm1, "measured_move_1x"))
                if mm2 > entry_price:
                    _candidates.append((mm2, "measured_move_2x"))
                if mm3 > entry_price:
                    _candidates.append((mm3, "measured_move_3x"))
                target, actual_rr, target_tag, skipped = compute_structural_target_long(
                    entry_price, risk, _candidates,
                    min_rr=cfg.sp_struct_min_rr, max_rr=cfg.sp_struct_max_rr,
                    fallback_rr=cfg.sp_target_rr, mode="structural",
                )
                if skipped:
                    prior_e9 = e9
                    continue
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

            sig = StrategySignal(
                strategy_name="SP_ATIER",
                symbol=symbol,
                timestamp=bar.timestamp,
                direction=1,
                entry_price=entry_price,
                stop_price=stop,
                target_price=target,
                in_play_score=ip_score,
                confluence_tags=confluence,
                metadata={
                    "box_high": box_high,
                    "box_low": box_low,
                    "box_range": box_range,
                    "box_len": box_len,
                    "box_avg_vol": box_avg_vol,
                    "box_failed_bo": box_failed,
                    "structure_quality": min(struct_q, 1.0),
                    "actual_rr": actual_rr,
                    "target_tag": target_tag,
                    "entry_type": "breakout_trigger",
                },
            )

            signals.append((sig, i, bar, i_atr, e9, vw, vol_ma))
            triggered = True

            prior_e9 = e9

        return signals
