"""
EnhancedMarketRegime — Wraps existing MarketEngine + day classification.
Provides per-bar regime snapshots and day-level GREEN/RED/FLAT labels.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional

from ...indicators import EMA, VWAPCalc
from ...market_context import MarketEngine, MarketTrend
from ...models import Bar, NaN

_isnan = math.isnan


@dataclass
class RegimeSnapshot:
    """Per-bar market regime state."""
    day_label: str = ""          # GREEN/RED/FLAT
    spy_above_vwap: bool = False
    ema9_above_ema20: bool = False
    trend: MarketTrend = MarketTrend.NEUTRAL
    chop_score: float = 0.0      # 0-1, how choppy recent bars are
    spy_pct_from_open: float = 0.0  # for RS computation


class EnhancedMarketRegime:
    """Day-level + bar-level market regime for strategy pipeline."""

    def __init__(self, spy_bars: List[Bar], cfg):
        self.cfg = cfg
        self._spy_bars = spy_bars
        self._day_labels: Dict[date, str] = {}
        self._bar_snapshots: Dict[datetime, RegimeSnapshot] = {}
        self._day_opens: Dict[date, float] = {}  # for pct_from_open

    def precompute(self):
        """Classify days and build per-bar snapshots."""
        if not self._spy_bars:
            return

        # ── Day classification (GREEN/RED/FLAT) ──
        daily_bars: Dict[date, List[Bar]] = defaultdict(list)
        for b in self._spy_bars:
            daily_bars[b.timestamp.date()].append(b)

        for d in sorted(daily_bars.keys()):
            bars = daily_bars[d]
            if not bars:
                continue
            o = bars[0].open
            c = bars[-1].close
            chg = (c - o) / o * 100 if o > 0 else 0.0
            if chg > 0.05:
                self._day_labels[d] = "GREEN"
            elif chg < -0.05:
                self._day_labels[d] = "RED"
            else:
                self._day_labels[d] = "FLAT"
            self._day_opens[d] = o

        # ── Per-bar snapshots ──
        me = MarketEngine()
        ema9 = EMA(9)
        ema20 = EMA(20)
        vwap = VWAPCalc()
        prev_date = None

        for b in self._spy_bars:
            d = b.timestamp.date()

            # Day reset for VWAP
            if d != prev_date:
                vwap.reset()
                prev_date = d

            # Update indicators
            snap = me.process_bar(b)
            e9 = ema9.update(b.close)
            e20 = ema20.update(b.close)
            tp = (b.high + b.low + b.close) / 3.0
            vw = vwap.update(tp, b.volume)

            # SPY pct from open
            day_open = self._day_opens.get(d, b.open)
            pct_from_open = (b.close - day_open) / day_open if day_open > 0 else 0.0

            above_vwap = b.close > vw if vwap.ready else False
            e9_above_e20 = e9 > e20 if (ema9.ready and ema20.ready) else False

            # Trend from MarketEngine snapshot
            trend = MarketTrend.NEUTRAL
            if snap and snap.ready:
                trend = snap.trend

            self._bar_snapshots[b.timestamp] = RegimeSnapshot(
                day_label=self._day_labels.get(d, "FLAT"),
                spy_above_vwap=above_vwap,
                ema9_above_ema20=e9_above_e20,
                trend=trend,
                chop_score=0.0,  # computed on demand if needed
                spy_pct_from_open=pct_from_open,
            )

    def get_regime(self, dt: datetime) -> Optional[RegimeSnapshot]:
        """Get regime snapshot at exact timestamp."""
        return self._bar_snapshots.get(dt)

    def get_nearest_regime(self, dt: datetime) -> Optional[RegimeSnapshot]:
        """Get regime snapshot at or before the given timestamp."""
        snap = self._bar_snapshots.get(dt)
        if snap:
            return snap
        # Find the closest bar before dt
        candidates = [ts for ts in self._bar_snapshots if ts <= dt]
        if candidates:
            return self._bar_snapshots[max(candidates)]
        return None

    def get_day_label(self, d: date) -> str:
        """Get day-level label."""
        return self._day_labels.get(d, "FLAT")

    def is_aligned_long(self, dt: datetime) -> bool:
        """Check if market supports long entry at this timestamp."""
        cfg = self.cfg
        d = dt.date() if isinstance(dt, datetime) else dt

        # Day must be GREEN
        if cfg.regime_require_green and self._day_labels.get(d) != "GREEN":
            return False

        # Optional: SPY above VWAP at this bar
        if cfg.regime_require_spy_above_vwap:
            snap = self.get_nearest_regime(dt) if isinstance(dt, datetime) else None
            if snap and not snap.spy_above_vwap:
                return False

        # Optional: EMA9 > EMA20 on SPY
        if cfg.regime_require_ema_aligned:
            snap = self.get_nearest_regime(dt) if isinstance(dt, datetime) else None
            if snap and not snap.ema9_above_ema20:
                return False

        return True

    def is_aligned_short(self, dt: datetime) -> bool:
        """Check if market supports short entry at this timestamp.
        Uses REAL-TIME bar-level assessment (not end-of-day label).
        Requires: SPY below VWAP AND SPY pct_from_open < -0.15% (bearish tape).
        """
        if not isinstance(dt, datetime):
            # Day-level fallback: use RED label
            d = dt
            return self._day_labels.get(d) in ("RED",)

        snap = self.get_nearest_regime(dt)
        if not snap:
            return False

        # Real-time: SPY must be below VWAP (bearish intraday structure)
        if snap.spy_above_vwap:
            return False

        # Real-time: SPY must be meaningfully down from open
        if snap.spy_pct_from_open > -0.0015:  # must be down at least -0.15%
            return False

        return True

    def is_aligned_failed_short(self, dt: datetime) -> bool:
        """Check if market supports a failed-breakout SHORT at this timestamp.
        Allows: RED, FLAT, mildly GREEN. Blocks: strongly bullish tape.
        Strongly bullish = SPY above VWAP AND EMA9>EMA20 AND pct_from_open > +0.50%.
        """
        if not isinstance(dt, datetime):
            return True  # day-level: always allow

        snap = self.get_nearest_regime(dt)
        if not snap:
            return True  # no data = allow

        # Block if all three bullish signals align (strong trend up)
        if (snap.spy_above_vwap and
                snap.ema9_above_ema20 and
                snap.spy_pct_from_open > 0.005):
            return False

        return True

    def is_aligned_failed_long(self, dt: datetime) -> bool:
        """Check if market supports a failed-breakdown LONG at this timestamp.
        Allows: GREEN, FLAT, mildly positive. Blocks: bearish tape.
        Tightened: block when SPY below VWAP AND pct_from_open < -0.15%
        (even mildly bearish is bad for buying failed breakdowns).
        """
        if not isinstance(dt, datetime):
            return True

        snap = self.get_nearest_regime(dt)
        if not snap:
            return True

        # Block if SPY below VWAP and meaningfully negative
        # (failed breakdown longs need at least neutral market support)
        if not snap.spy_above_vwap and snap.spy_pct_from_open < -0.0015:
            return False

        return True

    def get_spy_pct_from_open(self, dt: datetime) -> float:
        """Get SPY pct from open at given timestamp (for RS computation)."""
        snap = self.get_nearest_regime(dt)
        if snap:
            return snap.spy_pct_from_open
        return 0.0

    def day_label_distribution(self) -> Dict[str, int]:
        """Diagnostic: count of GREEN/RED/FLAT days."""
        dist: Dict[str, int] = defaultdict(int)
        for label in self._day_labels.values():
            dist[label] += 1
        return dict(dist)
