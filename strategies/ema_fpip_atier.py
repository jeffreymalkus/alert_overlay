"""
EMA_FPIP_ATier — 9EMA First Pullback In Play with full 6-step pipeline.

3-phase state machine:
  Phase 1 (Expansion): Clean directional impulse, E9>E20, limited body overlap
  Phase 2 (Pullback):  First retracement to E9 zone, volume contraction
  Phase 3 (Trigger):   Bar reclaims E9 with strong body/volume

Long-only. Relaxed thresholds vs engine.py original for 5-min viability.

Usage (via replay.py):
    from .ema_fpip_atier import EmaFpipATierStrategy
    strategy = EmaFpipATierStrategy(cfg, in_play, regime, rejection, quality)
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


class EmaFpipATierStrategy:
    """
    EMA First Pullback In Play: expansion → pullback → trigger with full pipeline.

    Pipeline:
    1. In-play check (day level)
    2. Market regime check (GREEN day)
    3. Raw FPIP detection (bar-by-bar state machine)
    4. Universal rejection filters
    5. Quality scoring → tier
    6. A-tier only returned as tradeable

    Supports V3 variants via config:
      fpip_trigger_require_close_above_prev_high, fpip_entry_mode, fpip_entry_buffer
    """

    def __init__(self, cfg: StrategyConfig, in_play: InPlayProxy,
                 regime: EnhancedMarketRegime, rejection: RejectionFilters,
                 quality: QualityScorer, strategy_name: str = "EMA_FPIP"):
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

        # ── Step 3: Raw FPIP detection ──
        raw_signals = self._detect_fpip_signals(symbol, bars, day, ip_score)

        # ── Steps 4-6: Filter + score ──
        results = []
        for sig, bar_idx, bar, atr, ema9, vwap, vol_ma in raw_signals:
            self.stats["raw_signals"] += 1

            # Step 4: Rejection filters
            # Skip maturity (pullback IS the entry, not a continuation)
            # Skip bigger_picture (EMA alignment already checked in detection)
            # Skip distance (FPIP triggers AFTER expansion, price is inherently extended from VWAP)
            reasons = self.rejection.check_all(
                bar, bars, bar_idx, atr, ema9, vwap, vol_ma,
                skip_filters=["maturity", "bigger_picture", "distance"]
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

    def _detect_fpip_signals(self, symbol: str, bars: List[Bar], day: date,
                              ip_score: float) -> list:
        """
        Run FPIP state machine across all bars for the day.
        Returns list of (StrategySignal, bar_idx, bar, atr, ema9, vwap, vol_ma).

        3-phase state machine:
          Phase 1: Expansion detection (clean impulse, E9>E20, limited overlap)
          Phase 2: Pullback tracking (first retrace to E9 zone, vol contraction)
          Phase 3: Trigger detection (reclaim E9 with strong candle)
        """
        cfg = self.cfg
        time_start = cfg.get(cfg.fpip_time_start)
        time_end = cfg.get(cfg.fpip_time_end)
        max_pb_bars = cfg.get(cfg.fpip_max_pullback_bars)

        # Indicators
        ema9 = EMA(9)
        ema20 = EMA(20)
        vwap_calc = VWAPCalc()
        atr_pair = ATRPair(14, use_completed=True)
        vol_buf = deque(maxlen=20)

        # Expansion state
        exp_active = False
        exp_bars = 0
        exp_high = NaN
        exp_low = NaN
        exp_total_vol = 0.0
        exp_overlap_count = 0
        exp_distance = 0.0
        exp_avg_vol = 0.0
        qual_expansion = False  # expansion has qualified

        # Pullback state
        pb_started = False
        pb_bars = 0
        pb_low = NaN
        pb_total_vol = 0.0
        pb_heavy_bars = 0  # bars at expansion-level volume
        pb_avg_vol = 0.0

        prev_bar = None
        prev_date = None
        prev_e20 = NaN
        triggered_today = False
        session_high = NaN
        prior_day_high = NaN
        signals = []

        for i, bar in enumerate(bars):
            d = bar.timestamp.date()
            hhmm = get_hhmm(bar)

            # Day reset
            if d != prev_date:
                vwap_calc.reset()
                exp_active = False
                qual_expansion = False
                pb_started = False
                triggered_today = False
                prev_bar = None
                prev_e20 = NaN
                if not _isnan(session_high):
                    prior_day_high = session_high
                session_high = NaN
                prev_date = d

            # Update indicators (all days for warmup)
            e9 = ema9.update(bar.close)
            e20 = ema20.update(bar.close)
            i_atr = atr_pair.update_intraday(bar.high, bar.low, bar.close)
            tp = (bar.high + bar.low + bar.close) / 3.0
            vw = vwap_calc.update(tp, bar.volume)
            vol_buf.append(bar.volume)
            vol_ma = sum(vol_buf) / len(vol_buf) if len(vol_buf) >= 5 else NaN

            # Track session high
            session_high = max(session_high, bar.high) if not _isnan(session_high) else bar.high

            if d != day:
                prev_bar = bar
                prev_e20 = e20
                continue

            # Skip if not ready
            if not ema9.ready or not ema20.ready or _isnan(i_atr) or i_atr <= 0:
                prev_bar = bar
                prev_e20 = e20
                continue
            if _isnan(vol_ma) or vol_ma <= 0:
                prev_bar = bar
                prev_e20 = e20
                continue
            if triggered_today:
                prev_bar = bar
                prev_e20 = e20
                continue

            in_window = time_start <= hhmm <= time_end

            # ══════════════════════════════════════════════════
            # Phase 1: EXPANSION TRACKING (always runs)
            # ══════════════════════════════════════════════════
            if not exp_active and not qual_expansion:
                # Try to start a new expansion
                is_impulse = (bar.close > bar.open and
                              (bar.close - e9) > 0.10 * i_atr and
                              e9 > e20)
                if is_impulse:
                    exp_active = True
                    exp_bars = 1
                    exp_high = bar.high
                    exp_low = bar.low
                    exp_total_vol = bar.volume
                    exp_overlap_count = 0
                    exp_distance = bar.high - bar.low

            elif exp_active:
                # Continue or end expansion
                if bar.close > e9 and e9 > e20 and exp_bars < cfg.fpip_expansion_max_bars:
                    exp_bars += 1
                    exp_high = max(exp_high, bar.high)
                    exp_low = min(exp_low, bar.low)  # FIX: track true expansion low
                    exp_total_vol += bar.volume
                    exp_distance = exp_high - exp_low

                    # Check body overlap with prev bar
                    if prev_bar is not None:
                        prev_body_top = max(prev_bar.close, prev_bar.open)
                        prev_body_bot = min(prev_bar.close, prev_bar.open)
                        cur_body_top = max(bar.close, bar.open)
                        cur_body_bot = min(bar.close, bar.open)
                        overlap = max(0, min(prev_body_top, cur_body_top) -
                                      max(prev_body_bot, cur_body_bot))
                        cur_body = abs(bar.close - bar.open)
                        if cur_body > 0 and overlap / cur_body > 0.5:
                            exp_overlap_count += 1
                else:
                    # Expansion ended — check if it qualifies
                    exp_active = False
                    exp_avg_vol = exp_total_vol / exp_bars if exp_bars > 0 else 0
                    overlap_ratio = exp_overlap_count / max(exp_bars - 1, 1)

                    if (exp_bars >= cfg.fpip_expansion_min_bars and
                            exp_distance >= cfg.fpip_min_expansion_atr * i_atr and
                            exp_avg_vol >= cfg.fpip_min_expansion_avg_vol * vol_ma and
                            overlap_ratio <= cfg.fpip_max_impulse_overlap):
                        qual_expansion = True
                        # Reset pullback state for tracking
                        pb_started = False
                        pb_bars = 0
                        pb_low = NaN
                        pb_total_vol = 0.0
                        pb_heavy_bars = 0

            # ══════════════════════════════════════════════════
            # Phase 2: PULLBACK TRACKING
            # ══════════════════════════════════════════════════
            if qual_expansion and not pb_started:
                # Check if bar dips into E9 zone
                near_e9 = bar.low <= e9 + 0.15 * i_atr
                holds_e9 = bar.close > e9 * 0.97
                if near_e9 and holds_e9:
                    pb_started = True
                    pb_bars = 1
                    pb_low = bar.low
                    pb_total_vol = bar.volume
                    pb_heavy_bars = 1 if bar.volume >= exp_avg_vol else 0

            elif qual_expansion and pb_started:
                pb_depth = exp_high - pb_low if not _isnan(pb_low) else 0
                max_depth = cfg.fpip_max_pullback_depth * exp_distance

                # Check expiration
                # Volume decline check: PB avg vol should be lighter than expansion
                pb_avg_vol_check = pb_total_vol / pb_bars if pb_bars > 0 else 0
                vol_declining = (pb_avg_vol_check <= cfg.fpip_max_pullback_vol_ratio * exp_avg_vol
                                 if exp_avg_vol > 0 else True)

                if (pb_depth > max_depth or
                        pb_bars > max_pb_bars or
                        e9 < e20 or
                        not vol_declining):
                    # Pullback failed — reset everything
                    qual_expansion = False
                    pb_started = False
                    prev_bar = bar
                    prev_e20 = e20
                    continue

                # ══════════════════════════════════════════════════
                # Phase 3: TRIGGER CHECK (must be in time window)
                # ══════════════════════════════════════════════════
                if in_window:
                    # Check if this bar is a trigger (reclaims E9)
                    reclaims = bar.close > e9 and bar.close > bar.open
                    prev_dipped = (prev_bar is not None and
                                   prev_bar.low <= e9 + 0.15 * i_atr)

                    # V3: require close above prior bar high
                    prev_high_reclaimed = True
                    if cfg.fpip_trigger_require_close_above_prev_high:
                        prev_high_reclaimed = (prev_bar is not None and
                                               bar.close > prev_bar.high)

                    if reclaims and prev_dipped and prev_high_reclaimed:
                        rng = bar.high - bar.low
                        if rng > 0:
                            close_pct = (bar.close - bar.low) / rng
                            body_pct = abs(bar.close - bar.open) / rng
                        else:
                            close_pct = 0.0
                            body_pct = 0.0

                        pb_avg_vol = pb_total_vol / pb_bars if pb_bars > 0 else vol_ma
                        vol_expansion = (bar.volume >= cfg.fpip_trigger_vol_vs_pb * pb_avg_vol
                                         if pb_avg_vol > 0 else True)

                        if (close_pct >= cfg.fpip_min_trigger_close_pct and
                                body_pct >= cfg.fpip_min_trigger_body_pct and
                                vol_expansion):
                            # ── TRIGGER FIRES ──
                            # V3: entry on prior-bar-high break
                            if cfg.fpip_entry_mode == "prev_high_break":
                                if prev_bar is None:
                                    qual_expansion = False
                                    pb_started = False
                                    prev_bar = bar
                                    prev_e20 = e20
                                    continue
                                entry_price = max(prev_bar.high + cfg.fpip_entry_buffer, bar.open)
                            else:
                                entry_price = bar.close

                            stop = pb_low - cfg.fpip_stop_buffer
                            min_stop = 0.20 * i_atr
                            if abs(entry_price - stop) < min_stop:
                                stop = entry_price - min_stop

                            risk = entry_price - stop
                            if risk <= 0:
                                qual_expansion = False
                                pb_started = False
                                prev_bar = bar
                                prev_e20 = e20
                                continue

                            # Structural target: expansion high, session high, PDH
                            if cfg.fpip_target_mode == "structural":
                                _candidates = []
                                if not _isnan(exp_high) and exp_high > entry_price:
                                    _candidates.append((exp_high, "exp_high"))
                                if not _isnan(session_high) and session_high > entry_price:
                                    _candidates.append((session_high, "session_high"))
                                if not _isnan(prior_day_high) and prior_day_high > entry_price:
                                    _candidates.append((prior_day_high, "pdh"))
                                target, actual_rr, target_tag, skipped = compute_structural_target_long(
                                    entry_price, risk, _candidates,
                                    min_rr=cfg.fpip_struct_min_rr, max_rr=cfg.fpip_struct_max_rr,
                                    fallback_rr=cfg.fpip_target_rr, mode="structural",
                                )
                                if skipped:
                                    qual_expansion = False
                                    pb_started = False
                                    prev_bar = bar
                                    prev_e20 = e20
                                    continue
                            else:
                                target = entry_price + risk * cfg.fpip_target_rr
                                actual_rr = cfg.fpip_target_rr
                                target_tag = "fixed_rr"

                            # Structure quality
                            struct_q = 0.50
                            if pb_depth < 0.25 * exp_distance:
                                struct_q += 0.15  # shallow PB
                            if bar.volume >= 1.2 * exp_avg_vol:
                                struct_q += 0.15  # strong trigger vol
                            if pb_avg_vol < 0.60 * exp_avg_vol:
                                struct_q += 0.10  # light PB volume
                            if not _isnan(prev_e20) and e20 > prev_e20:
                                struct_q += 0.10  # E20 sloping up

                            confluence = []
                            if bar.close > vw:
                                confluence.append("above_vwap")
                            if bar.close > e9:
                                confluence.append("above_ema9")
                            if e9 > e20:
                                confluence.append("ema_aligned")
                            if exp_avg_vol >= 1.5 * vol_ma:
                                confluence.append("strong_exp_vol")

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
                                    "exp_bars": exp_bars,
                                    "exp_distance_atr": exp_distance / i_atr,
                                    "exp_avg_vol_ratio": exp_avg_vol / vol_ma if vol_ma > 0 else 0,
                                    "pb_bars": pb_bars,
                                    "pb_depth_pct": pb_depth / exp_distance if exp_distance > 0 else 0,
                                    "pb_vol_ratio": pb_avg_vol / exp_avg_vol if exp_avg_vol > 0 else 0,
                                    "trigger_vol_ratio": bar.volume / pb_avg_vol if pb_avg_vol > 0 else 0,
                                    "structure_quality": min(struct_q, 1.0),
                                    "actual_rr": actual_rr,
                                    "target_tag": target_tag,
                                    "entry_mode": cfg.fpip_entry_mode,
                                    "entry_type": "prev_high_break" if cfg.fpip_entry_mode == "prev_high_break" else "close",
                                    "entry_ref_price": prev_bar.high if prev_bar is not None else NaN,
                                },
                            )

                            signals.append((sig, i, bar, i_atr, e9, vw, vol_ma))
                            qual_expansion = False
                            pb_started = False
                            triggered_today = True
                            if cfg.fpip_first_pullback_only:
                                prev_bar = bar
                                prev_e20 = e20
                                continue

                # Update pullback tracking (for bars that weren't triggers)
                if pb_started and not triggered_today:
                    pb_bars += 1
                    if bar.low < pb_low or _isnan(pb_low):
                        pb_low = bar.low
                    pb_total_vol += bar.volume
                    if bar.volume >= exp_avg_vol:
                        pb_heavy_bars += 1

                    # Check heavy PB bars limit
                    if pb_heavy_bars > cfg.fpip_max_heavy_pb_bars:
                        qual_expansion = False
                        pb_started = False

            prev_bar = bar
            prev_e20 = e20

        return signals
