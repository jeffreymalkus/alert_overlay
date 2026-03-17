"""
ORH_FBO_SHORT v2 — Hybrid timeframe (5m context + 1m sequencing).

CORE THESIS: A quality breakout above ORH traps breakout longs.
If price fails quickly and reclaims back below ORH, those longs are trapped.
Short entry only after the failed breakout is structurally confirmed.

TIMEFRAME DESIGN:
  5-min bars: ORH definition, broader structure, market environment, VWAP context
  1-min bars: break detection, failure timing, retest/rejection, trigger bar

TWO ENTRY MODES (tracked separately):
  Mode A (premium): break → fail → retest from below → bearish 1m rejection
  Mode B (secondary): break → fail → no retest → downside continuation through fail-bar low

ORH-only for v2. No PDH, no PMH.
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
    """Extracts 5-min context for the hybrid strategy."""

    def __init__(self):
        self.or_high: float = NaN
        self.or_low: float = NaN
        self.ema9: float = NaN
        self.ema20: float = NaN
        self.vwap: float = NaN
        self.atr: float = NaN
        self.recent_highs: List[float] = []  # last 6 5m bar highs

    @staticmethod
    def build(bars_5m: List[Bar], day: date) -> '_Context5m':
        """Build 5-min context for a given day."""
        ctx = _Context5m()
        ema9 = EMA(9)
        ema20 = EMA(20)
        vwap = VWAPCalc()
        atr_buf: deque = deque(maxlen=14)

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
        # For efficiency in replay, we rebuild up to the timestamp
        ctx = _Context5m()
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
# Mode A goes to RETESTING → SIGNAL
RETESTING = 3
# Mode B goes directly from FAILED → SIGNAL (continuation)


class ORHFailedBOShortV2Strategy:
    """
    Hybrid-timeframe ORH Failed Breakout Short.

    Replay strategy: iterates 1-min bars for event detection,
    uses 5-min context for ORH, structure, environment.

    Pipeline:
    1. In-play check (day level, from 5-min data)
    2. Market regime (per-signal, is_aligned_failed_short)
    3. Raw detection: hybrid state machine on 1-min bars
    4. Rejection filters (skip trigger_weakness — 4-phase structure IS quality)
    5. Quality scoring (inverted for shorts: RED=1.0, GREEN=0.0)
    6. A-tier gating
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
            "blocked_leader": 0, "blocked_hh_trend": 0,
            "blocked_no_damage": 0, "blocked_regime": 0,
            "breakouts_detected": 0, "failures_detected": 0,
        }

    def scan_day(self, symbol: str, bars_5m: List[Bar], bars_1m: List[Bar],
                 day: date, spy_bars: Optional[List[Bar]] = None,
                 sector_bars: Optional[List[Bar]] = None,
                 **kwargs) -> List[StrategySignal]:
        """Run full hybrid pipeline for one symbol-day."""
        cfg = self.cfg
        self.stats["total_symbol_days"] += 1

        # ── Step 1: In-play check (uses 5-min precomputed data) ──
        ip_pass, ip_score = self.in_play.is_in_play(symbol, day)
        if not ip_pass:
            return []
        self.stats["passed_in_play"] += 1
        self.stats["passed_regime"] += 1

        # ── Get 1-min bars for this day ──
        day_bars_1m = [b for b in bars_1m if b.timestamp.date() == day]
        if len(day_bars_1m) < 30:  # need enough bars
            return []

        # ── Build 5-min context (ORH, structure) ──
        # We'll rebuild context at signal time for accuracy
        # But get ORH first from the OR period
        or_bars_1m = [b for b in day_bars_1m if get_hhmm(b) < 1000]
        if not or_bars_1m:
            return []
        or_high = max(b.high for b in or_bars_1m)

        # ── Initialize 1-min indicators ──
        ind = _Indicators1m()

        # ── State machine ──
        phase = IDLE
        bo_bar_idx = 0       # index of first breakout bar
        bo_high_water = 0.0  # highest price above ORH during breakout
        fail_bar_idx = 0     # index of failure bar
        fail_bar_low = 0.0   # low of the failure bar
        bars_since_bo = 0
        bars_since_fail = 0
        triggered_today_a = False
        triggered_today_b = False

        results = []

        for i, bar in enumerate(day_bars_1m):
            hhmm = get_hhmm(bar)
            ind.update(bar)

            # Skip OR period
            if hhmm < cfg.orh2_time_start or hhmm > cfg.orh2_time_end:
                continue

            # Need indicators ready
            atr_5m_proxy = ind.atr  # use 1m ATR as proxy until we refine
            if _isnan(atr_5m_proxy) or atr_5m_proxy <= 0:
                continue
            if _isnan(ind.vol_ma) or ind.vol_ma <= 0:
                continue

            # ── Phase tracking ──
            if phase == BROKE_OUT:
                bars_since_bo += 1
                # Track high water mark
                if bar.high > bo_high_water:
                    bo_high_water = bar.high
                # Failure window expired
                if bars_since_bo > cfg.orh2_failure_window:
                    phase = IDLE
                    continue

            elif phase == FAILED:
                bars_since_fail += 1

            elif phase == RETESTING:
                bars_since_fail += 1

            # ── Phase 0 → 1: Breakout detection ──
            if phase == IDLE:
                dist_above = bar.close - or_high
                if dist_above >= cfg.orh2_break_min_dist_atr * atr_5m_proxy:
                    body_r = bar_body_ratio(bar)
                    if body_r >= cfg.orh2_break_body_min and bar.close > bar.open:
                        vol_ok = bar.volume >= cfg.orh2_break_vol_frac * ind.vol_ma
                        if vol_ok:
                            phase = BROKE_OUT
                            bo_bar_idx = i
                            bars_since_bo = 0
                            bo_high_water = bar.high
                            self.stats["breakouts_detected"] += 1
                            continue

            # ── Phase 1 → 2: Failure detection ──
            if phase == BROKE_OUT:
                if bar.close < or_high:
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
                # Check for retest: bar high approaches ORH from below
                proximity = or_high - bar.high
                if abs(proximity) <= cfg.orh2_retest_proximity_atr * atr_5m_proxy and bar.high <= or_high + 0.05 * atr_5m_proxy:
                    # Retest detected — look for rejection on this bar or next few
                    phase = RETESTING
                    continue

            if phase == RETESTING and not triggered_today_a:
                # Check for bearish rejection
                rng = bar.high - bar.low
                if rng > 0:
                    upper_wick = (bar.high - max(bar.open, bar.close)) / rng
                    body_r = bar_body_ratio(bar)
                    if (bar.close < bar.open and
                            upper_wick >= cfg.orh2_rejection_wick_min and
                            body_r >= cfg.orh2_rejection_body_min):
                        # MODE A SIGNAL
                        triggered_today_a = True
                        sig = self._build_signal(
                            symbol, bar, or_high, atr_5m_proxy, ind, ip_score,
                            bars_5m, day, spy_bars, mode="A",
                            retest_high=bar.high,
                            bo_high_water=bo_high_water,
                        )
                        if sig is not None:
                            results.append(sig)
                        phase = IDLE
                        continue

                # Rejection window expired
                if bars_since_fail > cfg.orh2_retest_window + cfg.orh2_rejection_lookback:
                    phase = IDLE
                    continue

            # ── Phase 2 → Mode B: continuation without retest ──
            if phase == FAILED and not triggered_today_b:
                if bars_since_fail >= cfg.orh2_mode_b_no_retest_wait:
                    # No retest happened — check for downside continuation
                    if bars_since_fail <= cfg.orh2_mode_b_no_retest_wait + cfg.orh2_mode_b_window:
                        # Continuation: close below the failure bar's low with strong body
                        if bar.close < fail_bar_low:
                            body_r = bar_body_ratio(bar)
                            if body_r >= cfg.orh2_mode_b_body_min and bar.close < bar.open:
                                # MODE B SIGNAL
                                triggered_today_b = True
                                sig = self._build_signal(
                                    symbol, bar, or_high, atr_5m_proxy, ind, ip_score,
                                    bars_5m, day, spy_bars, mode="B",
                                    retest_high=or_high,
                                    bo_high_water=bo_high_water,
                                )
                                if sig is not None:
                                    results.append(sig)
                                phase = IDLE
                                continue
                    else:
                        # Mode B window expired
                        phase = IDLE
                        continue

            # Timeout: if in FAILED too long without retest or continuation
            if phase == FAILED and bars_since_fail > cfg.orh2_retest_window + cfg.orh2_mode_b_no_retest_wait + cfg.orh2_mode_b_window:
                phase = IDLE

        return results

    def _build_signal(self, symbol: str, bar: Bar, or_high: float,
                      atr: float, ind: _Indicators1m, ip_score: float,
                      bars_5m: List[Bar], day: date,
                      spy_bars: Optional[List[Bar]],
                      mode: str, retest_high: float,
                      bo_high_water: float) -> Optional[StrategySignal]:
        """Build and filter a signal. Returns None if blocked."""
        cfg = self.cfg

        # ── Get 5-min context at signal time ──
        ctx = _Context5m()
        ctx.or_high = or_high
        # Build 5m indicators up to this timestamp
        ctx_full = ctx.snapshot_at(bars_5m, bar.timestamp)

        # Use 5-min ATR if available, else fall back to 1m proxy
        atr_5m = ctx_full.atr if not _isnan(ctx_full.atr) and ctx_full.atr > 0 else atr
        vwap = ind.vwap.value if not _isnan(ind.vwap.value) else ctx_full.vwap

        self.stats["raw_signals"] += 1
        if mode == "A":
            self.stats["raw_mode_a"] += 1
        else:
            self.stats["raw_mode_b"] += 1

        # ── Step 2b: Regime gate — block GREEN days entirely ──
        # On GREEN days, "failed breakout" is just a pullback before continuation.
        # Every GREEN Mode A trade in testing was a stop-out.
        regime_snap = self.regime.get_nearest_regime(bar.timestamp)
        regime_label = regime_snap.day_label if regime_snap else "FLAT"
        if regime_label == "GREEN":
            self.stats["blocked_regime"] += 1
            return None

        # Also block strongly bullish intraday (SPY above VWAP + ema9>ema20 + pct > 0.3%)
        if regime_snap and (regime_snap.spy_above_vwap and
                regime_snap.ema9_above_ema20 and
                regime_snap.spy_pct_from_open > 0.003):
            self.stats["blocked_regime"] += 1
            return None

        # ── Structural filter: no damage after reclaim ──
        # If bar.close is still very close to ORH (within 0.10 ATR), nobody is truly trapped
        if abs(bar.close - or_high) < 0.10 * atr_5m and mode == "A":
            self.stats["blocked_no_damage"] += 1
            return None

        # ── Stop / Target ──
        if mode == "A":
            stop = retest_high + cfg.orh2_stop_buffer_atr * atr_5m
        else:
            stop = or_high + cfg.orh2_stop_buffer_atr * atr_5m

        # Enforce minimum stop distance
        min_stop = cfg.orh2_min_stop_atr * atr_5m
        if stop - bar.close < min_stop:
            stop = bar.close + min_stop

        # Target: structural or fixed RR
        risk = stop - bar.close
        if risk <= 0:
            return None

        # Get session low from context for structural targeting
        session_low = ctx_full.or_low if not _isnan(ctx_full.or_low) else NaN

        if cfg.orh2_target_mode == "structural":
            _candidates = []
            if not _isnan(vwap) and vwap < bar.close:
                _candidates.append((vwap, "vwap"))
            if not _isnan(or_high) and or_high < bar.close:
                _candidates.append((or_high, "orh"))
            if not _isnan(session_low) and session_low < bar.close:
                _candidates.append((session_low, "session_low"))

            target, actual_rr, target_tag, skipped = compute_structural_target_short(
                bar.close, risk, _candidates,
                min_rr=cfg.orh2_struct_min_rr, max_rr=cfg.orh2_struct_max_rr,
                fallback_rr=cfg.orh2_target_rr, mode="structural",
            )
            if skipped:
                return None
        else:
            target = bar.close - cfg.orh2_target_rr * risk
            actual_rr = cfg.orh2_target_rr
            target_tag = "fixed_rr"

        # ── Step 4: Rejection filters (skip trigger_weakness + distance + bigger_picture) ──
        # We don't have the standard 5-min bar context for rejection filters in hybrid mode
        # The 4-phase structure IS the quality filter for failed-move strategies
        # Only apply choppiness check if we have enough 1m context
        # For now: skip all standard rejection filters for v2 hybrid
        self.stats["passed_rejection"] += 1

        # ── Step 5: Quality scoring (inverted for shorts) ──
        # regime_snap and regime_label already set in regime gate above
        regime_score = {"RED": 1.0, "FLAT": 0.5, "GREEN": 0.0}.get(regime_label, 0.5)

        # Trap quality bonus from high water mark
        trap_depth = (bo_high_water - or_high) / atr_5m if atr_5m > 0 else 0
        trap_bonus = min(trap_depth * 0.2, 0.2)  # up to 0.2 bonus

        quality_score = (
            0.3 * regime_score +
            0.2 * (1.0 if mode == "A" else 0.5) +  # Mode A premium
            0.2 * min(actual_rr / 2.0, 1.0) +
            0.15 * min(ip_score / 6.0, 1.0) +
            0.15 + trap_bonus  # base + trap depth
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
        tags = [f"orh_fbo_v2_{mode.lower()}"]
        if not _isnan(vwap) and bar.close < vwap:
            tags.append("below_vwap")
        if not _isnan(ind.ema9.value) and bar.close < ind.ema9.value:
            tags.append("below_ema9")
        if trap_depth >= 0.20:
            tags.append("strong_trap")
        if mode == "A":
            tags.append("failed_retest")
        else:
            tags.append("continuation")

        sig = StrategySignal(
            strategy_name=f"ORH_FBO_V2_{mode}",
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
                "or_high": or_high,
                "vwap_at_signal": round(vwap, 2) if not _isnan(vwap) else None,
            },
        )

        if tier != QualityTier.A_TIER:
            sig.reject_reasons = ["quality_below_a"]

        return sig
