"""
PDH_FBO_SHORT — Prior Day High Failed Breakout Short (hybrid 5m+1m).

CORE THESIS: A quality breakout above PDH traps breakout longs.
If price fails quickly and reclaims back below PDH, those longs are trapped.
Short entry only after the failed breakout is structurally confirmed.

This is the first family expansion of the failed-breakout-short family.
Architecture is identical to ORH_FBO_SHORT v2 — only the level source changes.

LEVEL: Prior Day High — objective, universally watched, plotted on every
institutional chart. The most-watched resistance after ORH. Breakout above
PDH traps the same participant class (breakout longs).

KEY DIFFERENCE FROM ORH:
  - PDH is further from the opening range, so tests happen later in the day
  - PDH > ORH on ~60% of trading days (distinct level on the majority of days)
  - Trapped-participant dynamics may be weaker (less congestion around PDH)
  - Time window extended (1000-1400 vs 1000-1300 for ORH)

TIMEFRAME DESIGN:
  5-min bars: PDH computation (prior day max high), broader structure, VWAP
  1-min bars: break detection, failure timing, retest/rejection, trigger bar

TWO ENTRY MODES (tracked separately, same as ORH v2):
  Mode A (premium): break → fail → retest from below → bearish 1m rejection
  Mode B (secondary): break → fail → no retest → downside continuation through fail-bar low

CONFLUENCE BONUS: When ORH ≈ PDH (within pdh_orh_confluence_atr * ATR),
the signal gets a quality boost — two independent resistance levels at the same
price means more participants watching and more longs trapped.
"""

import math
from collections import deque
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from ..backtest import load_bars_from_csv
from ..models import Bar, NaN
from ..indicators import EMA, VWAPCalc

from .shared.signal_schema import StrategySignal, StrategyTrade, QualityTier
from .shared.config import StrategyConfig
from .shared.in_play_proxy import InPlayProxy
from .shared.market_regime import EnhancedMarketRegime
from .shared.rejection_filters import RejectionFilters
from .shared.quality_scoring import QualityScorer
from .shared.helpers import (
    bar_body_ratio, compute_rs_from_open, get_hhmm,
    compute_structural_target_short,
)
from .shared.level_helpers import breakout_quality

_isnan = math.isnan


# ── Lightweight 1-min indicator set ──────────────────────────────

class _Indicators1m:
    """Minimal indicators for 1-min bar processing within a day."""
    __slots__ = ('ema9', 'ema20', 'vwap', 'vol_buf', 'vol_ma', '_atr_buf', 'atr')

    def __init__(self):
        self.ema9 = EMA(9)
        self.ema20 = EMA(20)
        self.vwap = VWAPCalc()
        self.vol_buf: deque = deque(maxlen=20)
        self.vol_ma: float = NaN
        self._atr_buf: deque = deque(maxlen=14)
        self.atr: float = NaN

    def update(self, bar: Bar):
        self.ema9.update(bar.close)
        self.ema20.update(bar.close)
        tp = (bar.high + bar.low + bar.close) / 3.0
        self.vwap.update(tp, bar.volume)
        # Volume MA
        self.vol_buf.append(bar.volume)
        if len(self.vol_buf) >= 5:
            self.vol_ma = sum(self.vol_buf) / len(self.vol_buf)
        # ATR (simple TR average for 1-min)
        tr = bar.high - bar.low
        self._atr_buf.append(tr)
        if len(self._atr_buf) >= 5:
            self.atr = sum(self._atr_buf) / len(self._atr_buf)


# ── 5-min context builder ────────────────────────────────────────

class _Context5m:
    """Extracts 5-min context for the hybrid strategy.
    Key difference from ORH version: computes PDH from prior day's bars."""

    def __init__(self):
        self.pdh: float = NaN          # Prior Day High — the level
        self.or_high: float = NaN      # Today's ORH (for confluence check)
        self.or_low: float = NaN
        self.ema9: float = NaN
        self.ema20: float = NaN
        self.vwap: float = NaN
        self.atr: float = NaN
        self.recent_highs: List[float] = []

    @staticmethod
    def build(bars_5m: List[Bar], day: date) -> '_Context5m':
        """Build 5-min context including PDH for a given day."""
        ctx = _Context5m()
        ema9 = EMA(9)
        ema20 = EMA(20)
        vwap = VWAPCalc()
        atr_buf: deque = deque(maxlen=14)

        # ── Compute PDH from prior day's bars ──
        prior_day_high = NaN
        prev_date = None
        for bar in bars_5m:
            d = bar.timestamp.date()
            if d >= day:
                break
            if prev_date is not None and d != prev_date:
                # New day in history — if the previous day was the day before target,
                # we'll keep updating. We want the LAST complete day before target day.
                pass
            prev_date = d

        # Get the actual prior day's bars
        if prev_date is not None:
            prior_bars = [b for b in bars_5m if b.timestamp.date() == prev_date]
            if prior_bars:
                prior_day_high = max(b.high for b in prior_bars)

        ctx.pdh = prior_day_high

        # ── Build today's context ──
        day_bars = [b for b in bars_5m if b.timestamp.date() == day]
        if not day_bars:
            return ctx

        or_bars = []
        recent_highs: deque = deque(maxlen=6)

        for bar in day_bars:
            hhmm = get_hhmm(bar)
            e9 = ema9.update(bar.close)
            e20 = ema20.update(bar.close)
            tp = (bar.high + bar.low + bar.close) / 3.0
            vw = vwap.update(tp, bar.volume)
            tr = bar.high - bar.low
            atr_buf.append(tr)

            if hhmm < 1000:
                or_bars.append(bar)
            else:
                recent_highs.append(bar.high)

            ctx.ema9 = e9
            ctx.ema20 = e20
            ctx.vwap = vw
            if len(atr_buf) >= 5:
                ctx.atr = sum(atr_buf) / len(atr_buf)

        if or_bars:
            ctx.or_high = max(b.high for b in or_bars)
            ctx.or_low = min(b.low for b in or_bars)

        ctx.recent_highs = list(recent_highs)
        return ctx

    def snapshot_at(self, bars_5m: List[Bar], dt: datetime) -> '_Context5m':
        """Build context up to a specific timestamp (for per-signal accuracy)."""
        ctx = _Context5m()
        ctx.pdh = self.pdh  # PDH doesn't change intraday
        ema9 = EMA(9)
        ema20 = EMA(20)
        vwap = VWAPCalc()
        atr_buf: deque = deque(maxlen=14)
        recent_highs: deque = deque(maxlen=6)

        day = dt.date()
        for bar in bars_5m:
            if bar.timestamp.date() != day:
                if bar.timestamp.date() > day:
                    break
                # Warm up EMAs from prior days
                ema9.update(bar.close)
                ema20.update(bar.close)
                tr = bar.high - bar.low
                atr_buf.append(tr)
                continue

            if bar.timestamp > dt:
                break

            hhmm = get_hhmm(bar)
            e9 = ema9.update(bar.close)
            e20 = ema20.update(bar.close)
            tp = (bar.high + bar.low + bar.close) / 3.0
            vw = vwap.update(tp, bar.volume)
            tr = bar.high - bar.low
            atr_buf.append(tr)

            if hhmm < 1000:
                if _isnan(ctx.or_high):
                    ctx.or_high = bar.high
                    ctx.or_low = bar.low
                else:
                    ctx.or_high = max(ctx.or_high, bar.high)
                    ctx.or_low = min(ctx.or_low, bar.low)
            else:
                recent_highs.append(bar.high)

            ctx.ema9 = e9
            ctx.ema20 = e20
            ctx.vwap = vw
            if len(atr_buf) >= 5:
                ctx.atr = sum(atr_buf) / len(atr_buf)

        ctx.recent_highs = list(recent_highs)
        return ctx


# ── State machine phases ─────────────────────────────────────────

IDLE = 0
BROKE_OUT = 1
FAILED = 2
RETESTING = 3


class PDHFailedBOShortStrategy:
    """
    Hybrid-timeframe PDH Failed Breakout Short.

    Replay strategy: iterates 1-min bars for event detection,
    uses 5-min context for PDH, ORH (confluence), structure, environment.

    Pipeline:
    1. In-play check (day level, from 5-min data)
    2. Market regime (per-signal, RED/FLAT-only gate)
    3. Raw detection: hybrid state machine on 1-min bars
    4. Quality scoring (inverted for shorts: RED=1.0, GREEN=0.0)
    5. A-tier gating
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
            "total_symbol_days": 0, "passed_in_play": 0, "passed_regime": 0,
            "raw_signals": 0, "raw_mode_a": 0, "raw_mode_b": 0,
            "passed_rejection": 0, "reject_reasons": {},
            "a_tier": 0, "b_tier": 0, "c_tier": 0,
            "blocked_regime": 0, "blocked_no_damage": 0,
            "breakouts_detected": 0, "failures_detected": 0,
            "pdh_not_available": 0, "pdh_equals_orh": 0,
            "confluence_signals": 0,
        }

    def scan_day(self, symbol: str, bars_5m: List[Bar], bars_1m: List[Bar],
                 day: date, spy_bars: Optional[List[Bar]] = None,
                 sector_bars: Optional[List[Bar]] = None,
                 **kwargs) -> List[StrategySignal]:
        """Run full hybrid pipeline for one symbol-day."""
        cfg = self.cfg
        self.stats["total_symbol_days"] += 1

        # ── Step 1: In-play check ──
        ip_pass, ip_score = self.in_play.is_in_play(symbol, day)
        if not ip_pass:
            return []
        self.stats["passed_in_play"] += 1
        self.stats["passed_regime"] += 1

        # ── Get 1-min bars for this day ──
        day_bars_1m = [b for b in bars_1m if b.timestamp.date() == day]
        if len(day_bars_1m) < 30:
            return []

        # ── Build 5-min context (PDH, ORH for confluence) ──
        ctx = _Context5m.build(bars_5m, day)
        pdh = ctx.pdh
        or_high = ctx.or_high

        # PDH must be available
        if _isnan(pdh):
            self.stats["pdh_not_available"] += 1
            return []

        # Track PDH == ORH days (these fire on ORH strategy instead)
        if not _isnan(or_high) and abs(pdh - or_high) < 0.01:
            self.stats["pdh_equals_orh"] += 1
            # Still allow — the breakout dynamics are slightly different even
            # at the same price because PDH has prior-day context.
            # But if ORH v2 already fired, the trades won't overlap because
            # ORH v2 triggers on the ORH level which is the same price.
            # We'll track this for analysis but don't skip.

        # ── Check if ORH ≈ PDH (confluence) ──
        has_confluence = False
        if not _isnan(or_high) and not _isnan(ctx.atr) and ctx.atr > 0:
            if abs(pdh - or_high) <= cfg.pdh_orh_confluence_atr * ctx.atr:
                has_confluence = True

        # ── Initialize 1-min indicators ──
        ind = _Indicators1m()

        # ── State machine ──
        phase = IDLE
        bo_bar_idx = 0
        bo_high_water = 0.0
        fail_bar_idx = 0
        fail_bar_low = 0.0
        bars_since_bo = 0
        bars_since_fail = 0
        triggered_today_a = False
        triggered_today_b = False

        results = []

        for i, bar in enumerate(day_bars_1m):
            hhmm = get_hhmm(bar)
            ind.update(bar)

            # Skip OR period and after time window
            if hhmm < cfg.pdh_time_start or hhmm > cfg.pdh_time_end:
                continue

            # Need indicators ready
            atr_proxy = ind.atr
            if _isnan(atr_proxy) or atr_proxy <= 0:
                continue
            if _isnan(ind.vol_ma) or ind.vol_ma <= 0:
                continue

            # ── Phase tracking ──
            if phase == BROKE_OUT:
                bars_since_bo += 1
                if bar.high > bo_high_water:
                    bo_high_water = bar.high
                if bars_since_bo > cfg.pdh_failure_window:
                    phase = IDLE
                    continue

            elif phase == FAILED:
                bars_since_fail += 1

            elif phase == RETESTING:
                bars_since_fail += 1

            # ── Phase 0 → 1: Breakout above PDH ──
            if phase == IDLE:
                dist_above = bar.close - pdh
                if dist_above >= cfg.pdh_break_min_dist_atr * atr_proxy:
                    body_r = bar_body_ratio(bar)
                    if body_r >= cfg.pdh_break_body_min and bar.close > bar.open:
                        vol_ok = bar.volume >= cfg.pdh_break_vol_frac * ind.vol_ma
                        if vol_ok:
                            phase = BROKE_OUT
                            bo_bar_idx = i
                            bars_since_bo = 0
                            bo_high_water = bar.high
                            self.stats["breakouts_detected"] += 1
                            continue

            # ── Phase 1 → 2: Failure — close back below PDH ──
            if phase == BROKE_OUT:
                if bar.close < pdh:
                    body_r = bar_body_ratio(bar)
                    if body_r >= 0.25:
                        phase = FAILED
                        fail_bar_idx = i
                        fail_bar_low = bar.low
                        bars_since_fail = 0
                        self.stats["failures_detected"] += 1
                        continue

            # ── Phase 2 → Mode A: retest from below ──
            if phase == FAILED and not triggered_today_a:
                proximity = pdh - bar.high
                if abs(proximity) <= cfg.pdh_retest_proximity_atr * atr_proxy and bar.high <= pdh + 0.05 * atr_proxy:
                    phase = RETESTING
                    continue

            if phase == RETESTING and not triggered_today_a:
                rng = bar.high - bar.low
                if rng > 0:
                    upper_wick = (bar.high - max(bar.open, bar.close)) / rng
                    body_r = bar_body_ratio(bar)
                    if (bar.close < bar.open and
                            upper_wick >= cfg.pdh_rejection_wick_min and
                            body_r >= cfg.pdh_rejection_body_min):
                        # MODE A SIGNAL
                        triggered_today_a = True
                        sig = self._build_signal(
                            symbol, bar, pdh, or_high, atr_proxy, ind, ip_score,
                            bars_5m, day, spy_bars, ctx, mode="A",
                            retest_high=bar.high,
                            bo_high_water=bo_high_water,
                            has_confluence=has_confluence,
                        )
                        if sig is not None:
                            results.append(sig)
                        phase = IDLE
                        continue

                if bars_since_fail > cfg.pdh_retest_window + cfg.pdh_rejection_lookback:
                    phase = IDLE
                    continue

            # ── Phase 2 → Mode B: continuation without retest ──
            if phase == FAILED and not triggered_today_b:
                if bars_since_fail >= cfg.pdh_mode_b_no_retest_wait:
                    if bars_since_fail <= cfg.pdh_mode_b_no_retest_wait + cfg.pdh_mode_b_window:
                        if bar.close < fail_bar_low:
                            body_r = bar_body_ratio(bar)
                            if body_r >= cfg.pdh_mode_b_body_min and bar.close < bar.open:
                                # MODE B SIGNAL
                                triggered_today_b = True
                                sig = self._build_signal(
                                    symbol, bar, pdh, or_high, atr_proxy, ind, ip_score,
                                    bars_5m, day, spy_bars, ctx, mode="B",
                                    retest_high=pdh,
                                    bo_high_water=bo_high_water,
                                    has_confluence=has_confluence,
                                )
                                if sig is not None:
                                    results.append(sig)
                                phase = IDLE
                                continue
                    else:
                        phase = IDLE
                        continue

            # Timeout
            if phase == FAILED and bars_since_fail > cfg.pdh_retest_window + cfg.pdh_mode_b_no_retest_wait + cfg.pdh_mode_b_window:
                phase = IDLE

        return results

    def _build_signal(self, symbol: str, bar: Bar, pdh: float, or_high: float,
                      atr: float, ind: _Indicators1m, ip_score: float,
                      bars_5m: List[Bar], day: date,
                      spy_bars: Optional[List[Bar]],
                      ctx: _Context5m, mode: str, retest_high: float,
                      bo_high_water: float,
                      has_confluence: bool) -> Optional[StrategySignal]:
        """Build and filter a signal. Returns None if blocked."""
        cfg = self.cfg

        # ── Get 5-min context at signal time ──
        ctx_snap = ctx.snapshot_at(bars_5m, bar.timestamp)
        atr_5m = ctx_snap.atr if not _isnan(ctx_snap.atr) and ctx_snap.atr > 0 else atr
        vwap = ind.vwap.value if not _isnan(ind.vwap.value) else ctx_snap.vwap

        self.stats["raw_signals"] += 1
        if mode == "A":
            self.stats["raw_mode_a"] += 1
        else:
            self.stats["raw_mode_b"] += 1

        # ── Regime gate — block GREEN days ──
        regime_snap = self.regime.get_nearest_regime(bar.timestamp)
        regime_label = regime_snap.day_label if regime_snap else "FLAT"
        if regime_label == "GREEN":
            self.stats["blocked_regime"] += 1
            return None

        # Block strongly bullish intraday
        if regime_snap and (regime_snap.spy_above_vwap and
                regime_snap.ema9_above_ema20 and
                regime_snap.spy_pct_from_open > 0.003):
            self.stats["blocked_regime"] += 1
            return None

        # ── Structural filter: no damage after reclaim ──
        if abs(bar.close - pdh) < 0.10 * atr_5m and mode == "A":
            self.stats["blocked_no_damage"] += 1
            return None

        # ── Stop / Target ──
        if mode == "A":
            stop = retest_high + cfg.pdh_stop_buffer_atr * atr_5m
        else:
            stop = pdh + cfg.pdh_stop_buffer_atr * atr_5m

        min_stop = cfg.pdh_min_stop_atr * atr_5m
        if stop - bar.close < min_stop:
            stop = bar.close + min_stop

        risk = stop - bar.close
        if risk <= 0:
            return None

        # ── Structural or fixed targets ──
        if cfg.pdh_target_mode == "structural":
            _candidates = []
            or_low = ctx_snap.or_low if not _isnan(ctx_snap.or_low) else ind.ema9.value
            session_low = min([b.low for b in bars_5m if b.timestamp.date() == day], default=NaN)

            if not _isnan(vwap) and vwap < bar.close:
                _candidates.append((vwap, "vwap"))
            if not _isnan(or_low) and or_low < bar.close:
                _candidates.append((or_low, "orl"))
            if not _isnan(session_low) and session_low < bar.close:
                _candidates.append((session_low, "session_low"))

            target, actual_rr, target_tag, skipped = compute_structural_target_short(
                bar.close, risk, _candidates,
                min_rr=cfg.pdh_struct_min_rr, max_rr=cfg.pdh_struct_max_rr,
                fallback_rr=cfg.pdh_target_rr, mode="structural",
            )
            if skipped:
                return None
        else:
            target = bar.close - cfg.pdh_target_rr * risk
            actual_rr = cfg.pdh_target_rr
            target_tag = "fixed_rr"

        self.stats["passed_rejection"] += 1

        # ── Quality scoring (inverted for shorts) ──
        regime_score = {"RED": 1.0, "FLAT": 0.5, "GREEN": 0.0}.get(regime_label, 0.5)

        trap_depth = (bo_high_water - pdh) / atr_5m if atr_5m > 0 else 0
        trap_bonus = min(trap_depth * 0.2, 0.2)

        # Confluence bonus: ORH ≈ PDH → more trapped participants
        confluence_bonus = 0.10 if has_confluence else 0.0
        if has_confluence:
            self.stats["confluence_signals"] += 1

        quality_score = (
            0.25 * regime_score +
            0.20 * (1.0 if mode == "A" else 0.5) +
            0.20 * min(actual_rr / 2.0, 1.0) +
            0.10 * min(ip_score / 6.0, 1.0) +
            0.15 + trap_bonus + confluence_bonus
        )
        quality_score = min(quality_score, 1.0)

        if quality_score >= 0.55:
            tier = QualityTier.A_TIER
            self.stats["a_tier"] += 1
        elif quality_score >= 0.40:
            tier = QualityTier.B_TIER
            self.stats["b_tier"] += 1
        else:
            tier = QualityTier.C_TIER
            self.stats["c_tier"] += 1

        # ── Build signal ──
        tags = [f"pdh_fbo_{mode.lower()}"]
        if not _isnan(vwap) and bar.close < vwap:
            tags.append("below_vwap")
        if not _isnan(ind.ema9.value) and bar.close < ind.ema9.value:
            tags.append("below_ema9")
        if trap_depth >= 0.20:
            tags.append("strong_trap")
        if has_confluence:
            tags.append("orh_pdh_confluence")
        if mode == "A":
            tags.append("failed_retest")
        else:
            tags.append("continuation")

        sig = StrategySignal(
            strategy_name=f"PDH_FBO_{mode}",
            symbol=symbol,
            timestamp=bar.timestamp,
            direction=-1,  # SHORT
            entry_price=bar.close,
            stop_price=stop,
            target_price=target,
            quality_score=quality_score,
            quality_tier=tier,
            in_play_score=ip_score,
            market_regime=regime_label,
            confluence_tags=tags,
            metadata={
                "mode": mode,
                "actual_rr": actual_rr,
                "target_tag": target_tag,
                "trap_depth_atr": round(trap_depth, 3),
                "pdh": pdh,
                "or_high": round(or_high, 2) if not _isnan(or_high) else None,
                "has_confluence": has_confluence,
                "vwap_at_signal": round(vwap, 2) if not _isnan(vwap) else None,
            },
        )

        if tier != QualityTier.A_TIER:
            sig.reject_reasons = ["quality_below_a"]

        return sig
