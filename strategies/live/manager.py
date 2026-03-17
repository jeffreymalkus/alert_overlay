"""
StrategyManager — orchestrates all live strategies for one symbol.

One StrategyManager per symbol. Owns:
  - SharedIndicators (computed once per bar per timeframe)
  - List of LiveStrategy instances (each with private state)
  - Performance instrumentation (per-bar, per-strategy timing)
  - Signal collection and filtering

Dual-timeframe support:
  - on_1min_bar(bar) → updates 1-min indicators, runs 1-min strategies
  - on_5min_bar(bar) → updates 5-min indicators, runs 5-min strategies
  - on_bar(bar) → backward-compatible (updates both, runs all strategies)

Usage:
    manager = StrategyManager(strategies=[SC_Sniper(), EMA9_FT(), ...])
    manager.warm_up(daily_atr, prior_high, prior_low)

    # On each completed 1-min bar:
    signals_1m = manager.on_1min_bar(bar_1m, market_ctx)

    # On each completed 5-min bar:
    signals_5m = manager.on_5min_bar(bar_5m, market_ctx)
"""

import logging
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Deque, Set

from ...models import Bar, Signal, NaN
from ..shared.config import StrategyConfig
from ..shared.rejection_filters import RejectionFilters
from ..shared.quality_scoring import QualityScorer
from .shared_indicators import SharedIndicators, IndicatorSnapshot
from .base import LiveStrategy, RawSignal

log = logging.getLogger("strategy_manager")

_isnan = math.isnan


# ═══════════════════════════════════════════════════════════════════
#  Live Rejection Filters — works with IndicatorSnapshot (no bar list)
# ═══════════════════════════════════════════════════════════════════

def _check_choppiness(snap: IndicatorSnapshot, max_overlap: float = 0.70,
                      lookback: int = 6) -> Optional[str]:
    """Reject if recent bars have heavily overlapping ranges (no trend).

    Uses snap.recent_bars (5-min) or snap.recent_bars_1m (1-min).
    """
    bars = snap.recent_bars_5m if snap.timeframe == 5 else snap.recent_bars_1m
    lb = lookback if snap.timeframe == 5 else lookback * 5  # scale for 1-min
    if len(bars) < lb + 1:
        return None  # not enough data → pass

    window = bars[-(lb + 1):]
    total_range = 0.0
    total_overlap = 0.0
    for j in range(1, len(window)):
        prev_b = window[j - 1]
        curr_b = window[j]
        prev_range = prev_b.high - prev_b.low
        curr_range = curr_b.high - curr_b.low
        total_range += prev_range + curr_range
        overlap_high = min(prev_b.high, curr_b.high)
        overlap_low = max(prev_b.low, curr_b.low)
        overlap = max(0, overlap_high - overlap_low)
        total_overlap += overlap

    if total_range <= 0:
        return None
    overlap_ratio = total_overlap / (total_range / 2.0)
    if overlap_ratio > max_overlap:
        return f"chop({overlap_ratio:.2f}>{max_overlap:.2f})"
    return None


def _check_maturity(snap: IndicatorSnapshot, max_bars: int = 24) -> Optional[str]:
    """Reject if move is too extended (no pullback in N bars)."""
    bars = snap.recent_bars_5m if snap.timeframe == 5 else snap.recent_bars_1m
    mb = max_bars if snap.timeframe == 5 else max_bars * 5
    if len(bars) < 3:
        return None

    bars_since_pullback = 0
    for b in reversed(bars):
        if b.close < b.open:  # red bar = pullback
            break
        bars_since_pullback += 1

    if bars_since_pullback > mb:
        return f"mature({bars_since_pullback}bars)"
    return None


def _check_bigger_picture(snap: IndicatorSnapshot) -> Optional[str]:
    """Reject longs if price below both VWAP and EMA9."""
    bar = snap.bar
    vwap = snap.vwap
    ema9 = snap.ema9 if snap.timeframe == 5 else snap.ema9_1m

    if _isnan(vwap) or _isnan(ema9):
        return None

    if bar.close < vwap and bar.close < ema9:
        return "bigger_picture(below_vwap+ema9)"
    return None


def _check_trigger_weakness(snap: IndicatorSnapshot,
                             min_body_pct: float = 0.40,
                             min_vol_frac: float = 0.80) -> Optional[str]:
    """Reject if trigger bar has weak body or low volume."""
    bar = snap.bar
    rng = bar.high - bar.low
    if rng > 0:
        body_pct = abs(bar.close - bar.open) / rng
        if body_pct < min_body_pct:
            return f"trigger_weak_body({body_pct:.2f}<{min_body_pct:.2f})"

    vol_ma = snap.vol_ma_5m if snap.timeframe == 5 else snap.vol_ma_1m
    if not _isnan(vol_ma) and vol_ma > 0:
        vol_ratio = bar.volume / vol_ma
        if vol_ratio < min_vol_frac:
            return f"trigger_low_vol({vol_ratio:.2f}<{min_vol_frac:.2f})"

    return None


def _check_distance(snap: IndicatorSnapshot,
                    max_dist_atr: float = 2.5) -> Optional[str]:
    """Reject if price is too far from VWAP in ATR units."""
    atr = snap.atr if snap.timeframe == 5 else snap.atr_1m
    vwap = snap.vwap

    if _isnan(atr) or atr <= 0 or _isnan(vwap) or vwap <= 0:
        return None

    dist = abs(snap.bar.close - vwap) / atr
    if dist > max_dist_atr:
        return f"dist({dist:.1f}ATR>{max_dist_atr:.1f})"
    return None


# Map filter names to functions
_REJECTION_CHECKS = {
    "choppiness": _check_choppiness,
    "maturity": _check_maturity,
    "distance": _check_distance,
    "bigger_picture": _check_bigger_picture,
    "trigger_weakness": _check_trigger_weakness,
}


def apply_live_rejections(snap: IndicatorSnapshot,
                          skip: Set[str]) -> List[str]:
    """Run all live rejection filters. Returns list of reject reasons (empty = pass).

    Args:
        snap: Current indicator snapshot
        skip: Set of filter names to skip for this strategy
    """
    reasons = []
    for name, check_fn in _REJECTION_CHECKS.items():
        if name in skip:
            continue
        reason = check_fn(snap)
        if reason is not None:
            reasons.append(reason)
    return reasons


@dataclass
class StrategyTiming:
    """Per-strategy timing stats."""
    total_us: float = 0.0      # total microseconds
    calls: int = 0
    max_us: float = 0.0        # worst-case single call
    signals: int = 0

    @property
    def avg_us(self) -> float:
        return self.total_us / self.calls if self.calls > 0 else 0.0


@dataclass
class BarTiming:
    """Per-bar timing breakdown."""
    indicator_us: float = 0.0
    strategy_us: float = 0.0
    total_us: float = 0.0
    strategy_count: int = 0
    signal_count: int = 0
    timeframe: int = 5         # which timeframe triggered this bar


class StrategyManager:
    """Orchestrates incremental strategy processing for one symbol.

    Design rules:
      1. SharedIndicators computed ONCE per bar per timeframe
      2. Each strategy.step() called with read-only snapshot
      3. No strategy can modify shared state
      4. Adding/removing strategies = list append/remove
      5. Per-bar and per-strategy timing logged automatically
      6. Strategies are routed by their declared timeframe field
    """

    def __init__(self, strategies: Optional[List[LiveStrategy]] = None,
                 symbol: str = "", config: Optional[StrategyConfig] = None):
        self.symbol = symbol
        self.strategies: List[LiveStrategy] = strategies or []
        self.indicators = SharedIndicators()
        self.config = config

        # ── Shared STAGE 4 pipeline objects (for Family A strategies) ──
        self._rejection_filters: Optional[RejectionFilters] = None
        self._quality_scorer: Optional[QualityScorer] = None
        if config is not None:
            self._rejection_filters = RejectionFilters(config)
            self._quality_scorer = QualityScorer(config)
            self._inject_pipeline_objects()

        # ── Performance instrumentation ──
        self._strategy_timing: Dict[str, StrategyTiming] = defaultdict(StrategyTiming)
        self._bar_timing_history: Deque[BarTiming] = deque(maxlen=100)
        self._total_bars_1m: int = 0
        self._total_bars_5m: int = 0
        self._total_bars: int = 0   # backward compat (counts 5-min bars)
        self._total_signals: int = 0
        self._total_rejections: int = 0

        # Worst-case tracking (for open-bell monitoring)
        self._worst_bar_us: float = 0.0
        self._worst_bar_idx: int = 0

        # Last indicator snapshot (for quality scoring at gate chain level)
        self.last_snap: Optional[IndicatorSnapshot] = None

        # Rejection filter enable/disable
        # Phase 1 result: choppiness/trigger_weakness/maturity filters
        # are net-negative in live pipeline due to calibration mismatch with
        # scan_day detection. Infrastructure preserved for future per-strategy
        # calibration. bigger_picture alone had zero impact at promotion level.
        self.enable_rejections: bool = True

    def warm_up(self, daily_atr: float, prior_high: float, prior_low: float):
        """Pass warm-up data to shared indicators."""
        self.indicators.warm_up_daily(daily_atr, prior_high, prior_low)

    def _inject_pipeline_objects(self):
        """Inject RejectionFilters and QualityScorer into ALL strategies.

        All 13 strategies (Family A + Family B) accept rejection= and quality=
        parameters and use the unified quality pipeline. Strategies that don't
        have these attributes are silently skipped.
        """
        if self._rejection_filters is None or self._quality_scorer is None:
            return

        for strategy in self.strategies:
            if hasattr(strategy, 'rejection') and hasattr(strategy, 'quality'):
                strategy.rejection = self._rejection_filters
                strategy.quality = self._quality_scorer
                log.debug(f"[{self.symbol}] {strategy.name}: injected rejection_filters and quality_scorer")

    def add_strategy(self, strategy: LiveStrategy):
        """Register a strategy at runtime."""
        self.strategies.append(strategy)
        # Inject pipeline objects into any strategy that accepts them
        if self._rejection_filters is not None and self._quality_scorer is not None:
            if hasattr(strategy, 'rejection') and hasattr(strategy, 'quality'):
                strategy.rejection = self._rejection_filters
                strategy.quality = self._quality_scorer
                log.debug(f"[{self.symbol}] {strategy.name}: injected rejection_filters and quality_scorer")
        log.info(f"[{self.symbol}] Strategy added: {strategy.name} (tf={strategy.timeframe}m)")

    def remove_strategy(self, name: str) -> bool:
        """Remove a strategy by name. Returns True if found."""
        for i, s in enumerate(self.strategies):
            if s.name == name:
                self.strategies.pop(i)
                log.info(f"[{self.symbol}] Strategy removed: {name}")
                return True
        return False

    def enable_strategy(self, name: str, enabled: bool = True):
        """Enable/disable a strategy by name."""
        for s in self.strategies:
            if s.name == name:
                s.enabled = enabled
                log.info(f"[{self.symbol}] {name} {'enabled' if enabled else 'disabled'}")
                return

    # ── Market context → SharedIndicators bridging ─────────────────

    def _update_market_scores(self, market_ctx, snap):
        """Extract regime/alignment/RS from MarketContext into SharedIndicators.

        Called once per bar before strategy execution so snap carries real
        market context values instead of hardcoded placeholders.
        """
        spy = getattr(market_ctx, 'spy', None)
        if spy is not None and getattr(spy, 'ready', False):
            # Regime score: GREEN=1.0, RED=0.0, FLAT=0.5
            trend = getattr(spy, 'trend', 0)  # MarketTrend enum: BULL=1, NEUTRAL=0, BEAR=-1
            if trend == 1:
                self.indicators.regime_score = 1.0
            elif trend == -1:
                self.indicators.regime_score = 0.0
            else:
                self.indicators.regime_score = 0.5

            # Alignment: SPY above VWAP + EMA9 above EMA20
            al = 0.0
            if getattr(spy, 'above_vwap', False):
                al += 0.5
            if getattr(spy, 'ema9_above_ema20', False):
                al += 0.5
            self.indicators.alignment_score = al

        # RS market: stock % from open - SPY % from open
        rs_mkt = getattr(market_ctx, 'rs_market', None)
        if rs_mkt is not None and not _isnan(rs_mkt):
            self.indicators.rs_market = rs_mkt

        # Update snap in-place (already constructed but not yet consumed by strategies)
        snap.regime_score = self.indicators.regime_score
        snap.alignment_score = self.indicators.alignment_score
        snap.rs_market = self.indicators.rs_market

    # ── Dual-timeframe entry points ──────────────────────────────────

    def on_1min_bar(self, bar: Bar, market_ctx=None) -> List[RawSignal]:
        """Process a completed 1-min bar.

        Updates 1-min indicators, then runs ONLY strategies with timeframe=1.
        Called on every completed 1-min bar from SymbolRunner.
        """
        t_total_start = time.perf_counter_ns()

        # Update 1-min indicators
        t_ind_start = time.perf_counter_ns()
        snap = self.indicators.update_1min(bar)
        t_ind_end = time.perf_counter_ns()
        indicator_us = (t_ind_end - t_ind_start) / 1000.0

        # Run 1-min strategies only
        self.last_snap = snap
        signals = self._run_strategies(snap, market_ctx, target_tf=1)

        self._total_bars_1m += 1

        # Record timing
        t_total_end = time.perf_counter_ns()
        total_us = (t_total_end - t_total_start) / 1000.0
        strategy_us = total_us - indicator_us

        bar_timing = BarTiming(
            indicator_us=indicator_us,
            strategy_us=strategy_us,
            total_us=total_us,
            strategy_count=sum(1 for s in self.strategies if s.enabled and s.timeframe == 1),
            signal_count=len(signals),
            timeframe=1,
        )
        self._bar_timing_history.append(bar_timing)
        self._total_signals += len(signals)

        if total_us > self._worst_bar_us:
            self._worst_bar_us = total_us
            self._worst_bar_idx = self._total_bars_1m

        # Log on signals (1-min bars are too frequent for periodic logging)
        if signals:
            self._log_timing(bar_timing, snap, signals)

        return signals

    def on_5min_bar(self, bar: Bar, market_ctx=None) -> List[RawSignal]:
        """Process a completed 5-min bar.

        Updates 5-min indicators, then runs ONLY strategies with timeframe=5.
        Called on every completed 5-min bar from SymbolRunner.
        """
        t_total_start = time.perf_counter_ns()

        # Update 5-min indicators
        t_ind_start = time.perf_counter_ns()
        snap = self.indicators.update_5min(bar)
        t_ind_end = time.perf_counter_ns()
        indicator_us = (t_ind_end - t_ind_start) / 1000.0

        # Run 5-min strategies only
        self.last_snap = snap
        signals = self._run_strategies(snap, market_ctx, target_tf=5)

        self._total_bars_5m += 1
        self._total_bars = self._total_bars_5m  # backward compat

        # Record timing
        t_total_end = time.perf_counter_ns()
        total_us = (t_total_end - t_total_start) / 1000.0
        strategy_us = total_us - indicator_us

        bar_timing = BarTiming(
            indicator_us=indicator_us,
            strategy_us=strategy_us,
            total_us=total_us,
            strategy_count=sum(1 for s in self.strategies if s.enabled and s.timeframe == 5),
            signal_count=len(signals),
            timeframe=5,
        )
        self._bar_timing_history.append(bar_timing)
        self._total_signals += len(signals)

        if total_us > self._worst_bar_us:
            self._worst_bar_us = total_us
            self._worst_bar_idx = self._total_bars_5m

        # Log periodically or on signals
        if self._total_bars_5m % 50 == 0 or signals:
            self._log_timing(bar_timing, snap, signals)

        return signals

    # ── Backward-compatible single entry point ──────────────────────

    def on_bar(self, bar: Bar, market_ctx=None) -> List[RawSignal]:
        """Process one bar through all strategies. Returns raw signals.

        BACKWARD COMPATIBLE: Updates both 1-min and 5-min indicators
        with the same bar, then runs ALL strategies regardless of timeframe.
        Used by equivalence tests and old code paths.

        For new code, use on_1min_bar() and on_5min_bar() separately.
        """
        t_total_start = time.perf_counter_ns()

        # Step 1: Shared indicators (both timeframes, via legacy update)
        t_ind_start = time.perf_counter_ns()
        snap = self.indicators.update(bar)
        t_ind_end = time.perf_counter_ns()
        indicator_us = (t_ind_end - t_ind_start) / 1000.0

        # Step 2-3: Run ALL strategies (no timeframe filter)
        self.last_snap = snap
        signals = self._run_strategies(snap, market_ctx, target_tf=None)

        self._total_bars += 1
        self._total_bars_5m = self._total_bars

        # Step 4: Record bar timing
        t_total_end = time.perf_counter_ns()
        total_us = (t_total_end - t_total_start) / 1000.0
        strategy_us = total_us - indicator_us

        bar_timing = BarTiming(
            indicator_us=indicator_us,
            strategy_us=strategy_us,
            total_us=total_us,
            strategy_count=sum(1 for s in self.strategies if s.enabled),
            signal_count=len(signals),
            timeframe=5,
        )
        self._bar_timing_history.append(bar_timing)
        self._total_signals += len(signals)

        if total_us > self._worst_bar_us:
            self._worst_bar_us = total_us
            self._worst_bar_idx = self._total_bars

        # Log timing periodically or on signals
        if self._total_bars % 50 == 0 or signals:
            self._log_timing(bar_timing, snap, signals)

        return signals

    # ── Internal strategy runner ─────────────────────────────────────

    def _run_strategies(self, snap: IndicatorSnapshot, market_ctx,
                        target_tf: Optional[int]) -> List[RawSignal]:
        """Run strategies matching the target timeframe. None = run all."""
        # Surface market context into SharedIndicators → snap for quality scoring
        if market_ctx is not None:
            self._update_market_scores(market_ctx, snap)

        signals: List[RawSignal] = []

        for strategy in self.strategies:
            if not strategy.enabled:
                continue
            # Filter by timeframe if specified
            if target_tf is not None and strategy.timeframe != target_tf:
                continue

            # Auto day-reset
            strategy._check_day_reset(snap)

            # Time each strategy individually
            t_s = time.perf_counter_ns()
            try:
                sig = strategy.step(snap, market_ctx)
            except Exception as e:
                log.error(f"[{self.symbol}] {strategy.name}.step() error: {e}",
                          exc_info=True)
                sig = None
            t_e = time.perf_counter_ns()

            elapsed_us = (t_e - t_s) / 1000.0

            # Record per-strategy timing
            timing = self._strategy_timing[strategy.name]
            timing.total_us += elapsed_us
            timing.calls += 1
            timing.max_us = max(timing.max_us, elapsed_us)

            if sig is not None:
                # Apply rejection filters (matching replay scan_day pipeline)
                if self.enable_rejections:
                    skip_set = set(strategy.skip_rejections)
                    reasons = apply_live_rejections(snap, skip_set)
                    if reasons:
                        self._total_rejections += 1
                        sig.metadata["reject_reasons"] = reasons
                        log.debug(
                            f"[{self.symbol}] {strategy.name} REJECTED: "
                            f"{', '.join(reasons)}"
                        )
                        # Don't skip — pass through with reject_reasons attached.
                        # Quality scoring will demote to B/C tier.
                        # Caller (replay_live_path) gates on A-tier.

                signals.append(sig)
                timing.signals += 1

        return signals

    # ── Logging & diagnostics ────────────────────────────────────────

    def _log_timing(self, bt: BarTiming, snap: IndicatorSnapshot,
                    signals: List[RawSignal]):
        """Log per-bar timing for monitoring."""
        sig_str = ""
        if signals:
            sig_str = " | SIGNALS: " + ", ".join(s.strategy_name for s in signals)
        tf_str = f"{bt.timeframe}m" if bt.timeframe else "?"
        bar_count = self._total_bars_1m if bt.timeframe == 1 else self._total_bars_5m
        log.info(
            f"[{self.symbol}] {tf_str} bar #{bar_count} "
            f"@{snap.hhmm:04d} "
            f"ind={bt.indicator_us:.0f}μs "
            f"strat={bt.strategy_us:.0f}μs "
            f"total={bt.total_us:.0f}μs "
            f"({bt.strategy_count} active)"
            f"{sig_str}"
        )

    def get_timing_report(self) -> dict:
        """Get comprehensive timing report for diagnostics."""
        # Per-strategy stats
        strat_stats = {}
        for name, t in self._strategy_timing.items():
            strat_stats[name] = {
                "avg_us": round(t.avg_us, 1),
                "max_us": round(t.max_us, 1),
                "calls": t.calls,
                "signals": t.signals,
            }

        # Aggregate bar stats (5-min bars only for backward compat)
        bars_5m = [b for b in self._bar_timing_history if b.timeframe == 5]
        if bars_5m:
            avg_total = sum(b.total_us for b in bars_5m) / len(bars_5m)
            avg_ind = sum(b.indicator_us for b in bars_5m) / len(bars_5m)
            avg_strat = sum(b.strategy_us for b in bars_5m) / len(bars_5m)
            p99_total = sorted(b.total_us for b in bars_5m)[
                int(len(bars_5m) * 0.99)
            ] if len(bars_5m) > 10 else 0
        else:
            avg_total = avg_ind = avg_strat = p99_total = 0

        return {
            "symbol": self.symbol,
            "total_bars_1m": self._total_bars_1m,
            "total_bars_5m": self._total_bars_5m,
            "total_bars": self._total_bars,  # backward compat
            "total_signals": self._total_signals,
            "total_rejections": self._total_rejections,
            "strategies": strat_stats,
            "bar_avg_us": round(avg_total, 1),
            "bar_avg_indicator_us": round(avg_ind, 1),
            "bar_avg_strategy_us": round(avg_strat, 1),
            "bar_p99_us": round(p99_total, 1),
            "bar_worst_us": round(self._worst_bar_us, 1),
            "bar_worst_idx": self._worst_bar_idx,
        }

    def __repr__(self):
        active = sum(1 for s in self.strategies if s.enabled)
        tf1 = sum(1 for s in self.strategies if s.enabled and s.timeframe == 1)
        tf5 = sum(1 for s in self.strategies if s.enabled and s.timeframe == 5)
        return (f"<StrategyManager [{self.symbol}] "
                f"{active}/{len(self.strategies)} active "
                f"(1m:{tf1} 5m:{tf5}), "
                f"{self._total_bars_5m} bars, {self._total_signals} signals>")
