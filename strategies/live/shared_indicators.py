"""
SharedIndicators — dual-timeframe per-symbol indicator state.

Maintains indicators on BOTH 1-min and 5-min bars from a single instance.
1-min indicators update on every 1-min bar. 5-min indicators update only
when a completed 5-min bar arrives.

Session policy:
  - EMA9, EMA20: session-CONTINUOUS (carry across days, no reset)
  - ATR (intraday): session-CONTINUOUS (accumulates like replay)
  - Daily ATR: set from warmup, updated on day change
  - Volume MA: session-CONTINUOUS (deque ages out naturally)
  - VWAP: session-LOCAL (resets daily)
  - OR, session_open/high/low: session-LOCAL (resets daily)

Every strategy reads from this shared state. No strategy writes to it.

────────────────────────────────────────────────────────────────────
RESOLVED: Strategy warmup — seed 5-min indicators from prior history
────────────────────────────────────────────────────────────────────
Problem: 8 strategies use 5-min indicators (EMA9/20, ATR, VolMA) which
require 45-100 min to warm up from scratch. Cold start readiness:
  EMA9_5m: 10:15    EMA20_5m: 11:10    Vol MA: 9:55

FAILED APPROACH (2026-03-17): Switched strategies from 5-min to 1-min
indicators. Replay degraded N=24 PF=1.63 +5.0R → N=34 PF=0.83 -2.7R.
1-min ATR is a different volatility scale. REVERTED immediately.

FIX APPLIED: seed_5min() — feeds prior-day 5-min bars through EMA/ATR/
VolMA accumulators before the trading day starts. With seeding:
  EMA9_5m: 9:30     EMA20_5m: 9:30     Vol MA: 9:30
Replay baseline UNCHANGED: N=24, PF=1.63, +5.0R

Wired into: replay.py (seeds from first trading day's bars),
dashboard.py (both sync and async setup paths, seeds from prior-day
1-min bars upsampled to 5-min).

Key principle: separate indicator readiness from strategy eligibility
from structural earliest signal time. Seeding fixes readiness without
changing what the strategies are.
────────────────────────────────────────────────────────────────────
"""

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Optional, List, Deque

from ...models import Bar, NaN

_isnan = math.isnan


# ── Lightweight indicator implementations ────────────────────────────

class EMA:
    """Exponential Moving Average — O(1) per update.

    Session-continuous: carries across days, never resets.
    Matches replay EMA behavior exactly:
      - Returns NaN until period bars accumulated
      - SMA seed at bar == period
      - Standard EMA smoothing after that
    """
    __slots__ = ('period', 'k', 'value', '_count', '_sum')

    def __init__(self, period: int):
        self.period = period
        self.k = 2.0 / (period + 1)
        self.value: float = NaN
        self._count: int = 0
        self._sum: float = 0.0

    def update(self, price: float) -> float:
        if _isnan(price):
            return self.value
        self._count += 1
        if self._count <= self.period:
            self._sum += price
            if self._count == self.period:
                self.value = self._sum / self.period
        else:
            self.value = price * self.k + self.value * (1.0 - self.k)
        return self.value

    @property
    def ready(self) -> bool:
        return not _isnan(self.value)

    def reset(self):
        self.value = NaN
        self._count = 0
        self._sum = 0.0


class VWAPCalc:
    """VWAP — O(1) per update. Session-local: resets daily."""
    __slots__ = ('_cum_tpv', '_cum_vol', 'value', '_date')

    def __init__(self):
        self._cum_tpv: float = 0.0
        self._cum_vol: float = 0.0
        self.value: float = NaN
        self._date: Optional[date] = None

    def update(self, bar: Bar) -> float:
        bar_date = bar.timestamp.date()
        if self._date is None or bar_date != self._date:
            self._cum_tpv = 0.0
            self._cum_vol = 0.0
            self._date = bar_date

        tp = (bar.high + bar.low + bar.close) / 3.0
        vol = max(bar.volume, 1)  # avoid div by zero
        self._cum_tpv += tp * vol
        self._cum_vol += vol
        self.value = self._cum_tpv / self._cum_vol
        return self.value

    @property
    def ready(self) -> bool:
        return not _isnan(self.value) and self._cum_vol > 0

    def reset(self):
        self._cum_tpv = 0.0
        self._cum_vol = 0.0
        self.value = NaN
        self._date = None


class ATRPair:
    """Dual ATR: daily (from prior-day data) + intraday (from completed bars).

    Session-continuous: intraday ATR accumulates across days (matches replay).
    daily_atr: set via warm_up, updated on day change.

    Wilder's MA: SMA for first N values, then (prev*(N-1)+val)/N.
    use_completed=True: returns PREVIOUS bar's ATR (lag-1).
    """
    __slots__ = ('period', '_daily_value', '_intra_value', '_intra_count',
                 '_intra_sum', '_prev_close', '_use_completed',
                 '_completed_value')

    def __init__(self, period: int = 14, use_completed: bool = True):
        self.period = period
        self._daily_value: float = NaN
        self._intra_value: float = NaN
        self._intra_count: int = 0
        self._intra_sum: float = 0.0
        self._prev_close: float = NaN
        self._use_completed = use_completed
        self._completed_value: float = NaN

    def set_daily(self, atr_value: float):
        """Set daily ATR from warm-up data."""
        self._daily_value = atr_value

    @property
    def daily_value(self) -> float:
        return self._daily_value

    def update(self, bar: Bar) -> float:
        """Update intraday ATR with a completed bar."""
        if _isnan(self._prev_close):
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._prev_close = bar.close

        self._intra_count += 1

        if self._intra_count <= self.period:
            self._intra_sum += tr
            if self._intra_count == self.period:
                self._intra_value = self._intra_sum / self.period
        else:
            self._intra_value = (self._intra_value * (self.period - 1) + tr) / self.period

        if self._use_completed:
            result = self._completed_value
            self._completed_value = self._intra_value if self._intra_count >= self.period else NaN
            return result if not _isnan(result) else self._fallback
        return self.value

    @property
    def _fallback(self) -> float:
        if not _isnan(self._daily_value):
            return self._daily_value
        return NaN

    @property
    def value(self) -> float:
        """Current ATR value (explicit, no ambiguity)."""
        if self._use_completed:
            if not _isnan(self._completed_value):
                return self._completed_value
            return self._fallback
        if self._intra_count >= self.period and not _isnan(self._intra_value):
            return self._intra_value
        if not _isnan(self._daily_value):
            return self._daily_value
        if not _isnan(self._intra_value):
            return self._intra_value
        return NaN

    # Keep .atr as alias for backward compat (used in old code paths)
    @property
    def atr(self) -> float:
        return self.value

    @property
    def intra_ready(self) -> bool:
        return self._intra_count >= self.period

    def reset_intraday(self):
        self._intra_value = NaN
        self._intra_count = 0
        self._intra_sum = 0.0
        self._prev_close = NaN
        self._completed_value = NaN


# ── Dual-timeframe indicator snapshot ─────────────────────────────────

@dataclass
class IndicatorSnapshot:
    """Read-only snapshot of all shared indicators for one bar.

    Contains BOTH 1-min and 5-min indicator values. Each strategy reads
    the fields it needs based on its timeframe.

    Backward-compatible: snap.ema9 / snap.ema20 / snap.atr still work
    and return the 5-min values (matching prior behavior for existing
    5-min strategies). Explicit snap.ema9_1m / snap.ema9_5m are also
    available for strategies that need to be precise.
    """
    bar: Bar
    bar_idx: int              # bar index for THIS timeframe (1m or 5m)
    timeframe: int = 5        # which bar produced this snapshot (1 or 5)

    # ── 1-min EMAs (updated every 1-min bar) ──
    ema9_1m: float = NaN
    ema20_1m: float = NaN
    prev_ema9_1m: float = NaN
    prev_ema20_1m: float = NaN
    ema9_1m_ready: bool = False
    ema20_1m_ready: bool = False

    # ── 5-min EMAs (updated every 5-min bar) ──
    ema9_5m: float = NaN
    ema20_5m: float = NaN
    prev_ema9_5m: float = NaN
    prev_ema20_5m: float = NaN
    ema9_5m_ready: bool = False
    ema20_5m_ready: bool = False

    # ── Backward-compatible aliases (= 5-min values) ──
    # These properties are set in __post_init__ for dataclass compat
    ema9: float = NaN
    ema20: float = NaN
    prev_ema9: float = NaN
    prev_ema20: float = NaN
    ema9_ready: bool = False
    ema20_ready: bool = False

    # ── VWAP (session-local, computed on 1-min bars — canonical) ──
    vwap: float = NaN
    vwap_ready: bool = False

    # ── ATR (explicit per timeframe, no "best available") ──
    atr_1m: float = NaN       # intraday ATR from 1-min bars
    atr_5m: float = NaN       # intraday ATR from 5-min bars
    daily_atr: float = NaN    # daily ATR from prior-day ranges
    atr_1m_ready: bool = False
    atr_5m_ready: bool = False

    # Backward-compatible: snap.atr returns 5-min ATR (or daily fallback)
    atr: float = NaN
    atr_ready: bool = False

    # ── Volume (per timeframe) ──
    vol_ma_1m: float = NaN    # 20-bar volume MA on 1-min bars
    vol_ma_5m: float = NaN    # 20-bar volume MA on 5-min bars
    rvol_1m: float = NaN
    rvol_5m: float = NaN

    # Backward-compatible aliases (= 5-min values)
    vol_ma20: float = NaN
    rvol: float = NaN

    # ── Market context scores (set by manager from MarketContext) ──
    in_play_score: float = 0.0    # V2: percentile 0-1, never zeroed on failure
    in_play_passed: bool = False  # V2: active_passed from InPlayProxyV2
    regime_score: float = 0.5     # GREEN=1.0, FLAT=0.5, RED=0.0 (from SPY trend)
    alignment_score: float = 0.0  # 0-1: (0.5 if SPY>VWAP) + (0.5 if EMA9>EMA20)
    rs_market: float = 0.0        # stock % from open - SPY % from open

    # ── Session state (session-local, updated on 1-min) ──
    session_open: float = NaN
    session_high: float = NaN
    session_low: float = NaN
    day_date: Optional[date] = None

    # ── Prior day levels ──
    prior_day_high: float = NaN
    prior_day_low: float = NaN

    # ── Opening range (session-local, tracked on 1-min for precision) ──
    or_high: float = NaN
    or_low: float = NaN
    or_ready: bool = False

    # ── Time ──
    hhmm: int = 0
    bar_idx_1m: int = 0       # cumulative 1-min bar count for the session
    bar_idx_5m: int = 0       # cumulative 5-min bar count for the session

    # ── Recent bars (per timeframe) ──
    recent_bars: Optional[List] = field(default_factory=list)      # backward compat = 5m
    recent_bars_1m: Optional[List] = field(default_factory=list)
    recent_bars_5m: Optional[List] = field(default_factory=list)


# ── Dual-timeframe SharedIndicators ──────────────────────────────────

class SharedIndicators:
    """Per-symbol shared indicator state with dual-timeframe support.

    Maintains separate 1-min and 5-min indicator sets from a single instance.
    Session state (VWAP, OR, open/high/low) updated on every 1-min bar.
    EMAs and ATR are session-continuous (carry across days).

    Usage:
        indicators = SharedIndicators()
        indicators.warm_up_daily(daily_atr, prior_high, prior_low)

        # On each 1-min bar:
        snap_1m = indicators.update_1min(bar_1m)

        # On each 5-min bar:
        snap_5m = indicators.update_5min(bar_5m)
    """

    def __init__(self):
        # ── 1-min indicators (session-continuous) ──
        self.ema9_1m = EMA(9)
        self.ema20_1m = EMA(20)
        self.atr_1m = ATRPair(14, use_completed=True)
        self._vol_buf_1m: Deque[float] = deque(maxlen=20)

        # ── 5-min indicators (session-continuous) ──
        self.ema9_5m = EMA(9)
        self.ema20_5m = EMA(20)
        self.atr_5m = ATRPair(14, use_completed=True)
        self._vol_buf_5m: Deque[float] = deque(maxlen=20)

        # Backward-compatible aliases
        self.ema9 = self.ema9_5m
        self.ema20 = self.ema20_5m

        # ── Shared (session-local, updated on 1-min) ──
        self.vwap = VWAPCalc()

        # Session state
        self._session_open: float = NaN
        self._session_high: float = NaN
        self._session_low: float = NaN
        self._session_date: Optional[date] = None

        # Opening range
        self._or_high: float = NaN
        self._or_low: float = NaN
        self._or_ready: bool = False

        # Bar tracking
        self._bar_idx_1m: int = -1
        self._bar_idx_5m: int = -1
        self._prev_ema9_1m: float = NaN
        self._prev_ema20_1m: float = NaN
        self._prev_ema9_5m: float = NaN
        self._prev_ema20_5m: float = NaN

        # Recent bars buffers
        self.recent_bars_1m: Deque[Bar] = deque(maxlen=60)   # ~1 hour of 1-min
        self.recent_bars_5m: Deque[Bar] = deque(maxlen=20)   # ~100 min of 5-min

        # Prior day levels
        self.prior_day_high: float = NaN
        self.prior_day_low: float = NaN

        # Daily ATR (from prior-day ranges)
        self._daily_ranges: Deque[float] = deque(maxlen=14)
        self._daily_atr_value: float = NaN
        self._prev_day_close: float = NaN
        self._cur_day_high: float = NaN
        self._cur_day_low: float = NaN

        # EMA9 history for slope checks (5-min, matches prior behavior)
        self._ema9_5m_history: Deque[float] = deque(maxlen=10)
        # Also track 1-min E9 history for strategies that need it
        self._ema9_1m_history: Deque[float] = deque(maxlen=30)

        # ── In-play proxy: session-level baselines (rolling 20-day) ──
        # These track first-N-bar stats per session for RVOL and range expansion.
        # Updated once per session (after first_n 1-min bars). NOT per-bar.
        self._ip_first_n = 15  # first 15 1-min bars (~9:45 AM). Matches cfg.ip_first_n_bars[1].
        self._ip_vol_buf: Deque[float] = deque(maxlen=20)     # 20-day rolling first-N volume
        self._ip_range_buf: Deque[float] = deque(maxlen=20)   # 20-day rolling first-N range
        self._ip_prev_close: float = NaN                       # prior session close

        # Per-session accumulators (reset on day change)
        self._ip_session_bars: List[Bar] = []         # first N bars of current session
        self._ip_session_evaluated: bool = False       # True once first-N bars collected

        # Market context scores: set by dashboard/manager from MarketContext
        self._in_play_score: float = 0.0              # V2: percentile 0-1, never zeroed
        self._in_play_passed: bool = False             # V2: active_passed
        self._regime_score: float = 0.5               # GREEN=1.0, FLAT=0.5, RED=0.0
        self._alignment_score: float = 0.0            # (0.5 if SPY>VWAP) + (0.5 if EMA9>EMA20)
        self._rs_market: float = 0.0                  # stock % from open - SPY % from open

    def warm_up_daily(self, daily_atr: float, prior_high: float, prior_low: float,
                      prior_close: float = NaN):
        """Set daily ATR and prior-day levels from warm-up data.

        Args:
            daily_atr: ATR computed from daily bars (used for scaling)
            prior_high: prior completed day's high
            prior_low: prior completed day's low
            prior_close: prior completed day's close — critical for gap
                calculation. If provided, sets _prev_day_close so that
                _ip_prev_close is correctly initialized even if 1-min
                warmup bars don't span multiple days.
        """
        self.atr_1m.set_daily(daily_atr)
        self.atr_5m.set_daily(daily_atr)
        self.prior_day_high = prior_high
        self.prior_day_low = prior_low
        if not _isnan(prior_close):
            self._prev_day_close = prior_close
            # Also set _ip_prev_close directly — this is the gap calculation
            # input. Without this, if warmup bars only cover today (no day
            # transition), _ip_prev_close stays NaN and gap = 0.0 for all.
            self._ip_prev_close = prior_close

    def seed_5min(self, bars_5m: List[Bar]):
        """Seed 5-min indicators from prior-session history.

        Feeds prior-day 5-min bars through EMA9, EMA20, ATR, and volume MA
        accumulators so they are warm before the current day's first bar.
        This makes 5-min-native strategies indicator-ready by 9:35 AM
        without changing the strategy math.

        Should be called AFTER warm_up_daily() and BEFORE processing any
        bars from the current trading day.

        Args:
            bars_5m: Prior-session 5-min bars (at least 20 bars recommended;
                     only the most recent day's bars are used to avoid
                     polluting cross-day state).
        """
        if not bars_5m:
            return

        for bar in bars_5m:
            # Update 5-min EMAs
            self.ema9_5m.update(bar.close)
            self.ema20_5m.update(bar.close)

            # Update 5-min ATR
            self.atr_5m.update(bar)

            # Update 5-min volume buffer
            self._vol_buf_5m.append(bar.volume)

            # Update 5-min recent bars buffer
            self.recent_bars_5m.append(bar)

            # Track EMA9 history for slope checks
            if not _isnan(self.ema9_5m.value):
                self._ema9_5m_history.append(self.ema9_5m.value)

    def _check_day_reset(self, bar_date: date):
        """Reset session-local state on day change. EMAs/ATR carry over.

        IMPORTANT: Uses <= (not ==) to prevent backwards resets. This can
        happen when a stale 5-min bar from the prior day is emitted by the
        BarUpsampler AFTER the 1-min path has already advanced _session_date
        to the new day. Without this guard, the stale bar fires a spurious
        day reset that corrupts _ip_prev_close and _ip_session_bars.
        """
        if self._session_date is not None and bar_date <= self._session_date:
            return False  # same day or stale bar from earlier day

        # Compute daily range from completed day (for daily ATR)
        if not _isnan(self._cur_day_high) and not _isnan(self._cur_day_low):
            day_range = self._cur_day_high - self._cur_day_low
            if not _isnan(self._prev_day_close):
                day_tr = max(
                    day_range,
                    abs(self._cur_day_high - self._prev_day_close),
                    abs(self._cur_day_low - self._prev_day_close),
                )
            else:
                day_tr = day_range
            self._daily_ranges.append(day_tr)
            n = len(self._daily_ranges)
            if n == 1:
                self._daily_atr_value = day_tr
            elif n < 14:
                self._daily_atr_value = sum(self._daily_ranges) / n
            else:
                self._daily_atr_value = (self._daily_atr_value * 13 + day_tr) / 14

        # ── Finalize prior session's in-play baselines ──
        # Push prior session's first-N stats to rolling buffers BEFORE resetting.
        if self._ip_session_bars and self._ip_session_evaluated:
            n_bars = self._ip_session_bars
            fn_vol = sum(b.volume for b in n_bars)
            fn_high = max(b.high for b in n_bars)
            fn_low = min(b.low for b in n_bars)
            fn_range = fn_high - fn_low
            self._ip_vol_buf.append(fn_vol)
            self._ip_range_buf.append(fn_range)
        # Save prior session's close for gap calculation
        if not _isnan(self._prev_day_close):
            self._ip_prev_close = self._prev_day_close

        self._session_date = bar_date

        # Session-local resets
        self._session_open = NaN  # will be set on first bar
        self._session_high = NaN
        self._session_low = NaN
        self._cur_day_high = NaN
        self._cur_day_low = NaN
        self._or_high = NaN
        self._or_low = NaN
        self._or_ready = False
        self._bar_idx_1m = -1
        self._bar_idx_5m = -1

        self.vwap.reset()
        self._ema9_5m_history.clear()
        self._ema9_1m_history.clear()
        self.recent_bars_1m.clear()
        self.recent_bars_5m.clear()

        # In-play session accumulators reset
        self._ip_session_bars = []
        self._ip_session_evaluated = False
        self._in_play_score = 0.0  # reset until re-evaluated for new day
        self._in_play_passed = False
        self._regime_score = 0.5   # reset to neutral until SPY data arrives
        self._alignment_score = 0.0
        self._rs_market = 0.0

        # EMAs, ATRs, vol bufs, ip rolling buffers: DO NOT reset (session-continuous)
        return True

    def _update_session_state(self, bar: Bar):
        """Update session-local state from a 1-min bar."""
        if _isnan(self._session_open):
            self._session_open = bar.open
            self._session_high = bar.high
            self._session_low = bar.low
            self._cur_day_high = bar.high
            self._cur_day_low = bar.low
        else:
            self._session_high = max(self._session_high, bar.high)
            self._session_low = min(self._session_low, bar.low)
            self._cur_day_high = max(self._cur_day_high, bar.high)
            self._cur_day_low = min(self._cur_day_low, bar.low)

        self._prev_day_close = bar.close

    def update_1min(self, bar: Bar) -> IndicatorSnapshot:
        """Process a completed 1-min bar. Updates 1-min indicators + session state.

        Called on every 1-min bar. Returns a snapshot for 1-min strategies.
        """
        bar_date = bar.timestamp.date()
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute

        self._check_day_reset(bar_date)
        self._bar_idx_1m += 1
        self._update_session_state(bar)

        # ── In-play: accumulate first N bars for session-level stats ──
        # Only count bars from regular trading hours (9:30+). Premarket bars
        # (from reqMktData ticks before open) would poison gap/dolvol/range.
        if not self._ip_session_evaluated and len(self._ip_session_bars) < self._ip_first_n:
            if hhmm >= 930:  # skip premarket bars
                self._ip_session_bars.append(bar)
                if len(self._ip_session_bars) >= self._ip_first_n:
                    self._ip_session_evaluated = True  # ready for evaluate_session()

        # ── Store previous EMA values ──
        self._prev_ema9_1m = self.ema9_1m.value
        self._prev_ema20_1m = self.ema20_1m.value

        # ── Update 1-min indicators ──
        e9_1m = self.ema9_1m.update(bar.close)
        e20_1m = self.ema20_1m.update(bar.close)
        vw = self.vwap.update(bar)
        self.atr_1m.update(bar)

        # Volume MA (1-min)
        self._vol_buf_1m.append(bar.volume)
        vol_ma_1m = sum(self._vol_buf_1m) / len(self._vol_buf_1m) if len(self._vol_buf_1m) >= 5 else NaN
        rvol_1m = bar.volume / vol_ma_1m if (not _isnan(vol_ma_1m) and vol_ma_1m > 0) else NaN

        # Opening range (tracked on 1-min for precision)
        if hhmm <= 959:
            if _isnan(self._or_high):
                self._or_high = bar.high
                self._or_low = bar.low
            else:
                self._or_high = max(self._or_high, bar.high)
                self._or_low = min(self._or_low, bar.low)
        elif not self._or_ready and not _isnan(self._or_high):
            self._or_ready = True

        # EMA9 1-min history
        if not _isnan(e9_1m):
            self._ema9_1m_history.append(e9_1m)

        # Recent bars
        self.recent_bars_1m.append(bar)

        # Build snapshot
        return self._build_snapshot(
            bar=bar,
            bar_idx=self._bar_idx_1m,
            timeframe=1,
            hhmm=hhmm,
            bar_date=bar_date,
            e9_1m=e9_1m,
            e20_1m=e20_1m,
            vw=vw,
            vol_ma_1m=vol_ma_1m,
            rvol_1m=rvol_1m,
        )

    def update_5min(self, bar: Bar) -> IndicatorSnapshot:
        """Process a completed 5-min bar. Updates 5-min indicators.

        Called on every 5-min bar. Returns a snapshot for 5-min strategies.
        Session state is NOT updated here (already current from 1-min updates).
        """
        bar_date = bar.timestamp.date()
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute

        # Day reset should already have happened via update_1min, but be safe
        self._check_day_reset(bar_date)
        self._bar_idx_5m += 1

        # ── Store previous EMA values ──
        self._prev_ema9_5m = self.ema9_5m.value
        self._prev_ema20_5m = self.ema20_5m.value

        # ── Update 5-min indicators ──
        e9_5m = self.ema9_5m.update(bar.close)
        e20_5m = self.ema20_5m.update(bar.close)
        self.atr_5m.update(bar)

        # Volume MA (5-min)
        self._vol_buf_5m.append(bar.volume)
        vol_ma_5m = sum(self._vol_buf_5m) / len(self._vol_buf_5m) if len(self._vol_buf_5m) >= 5 else NaN
        rvol_5m = bar.volume / vol_ma_5m if (not _isnan(vol_ma_5m) and vol_ma_5m > 0) else NaN

        # EMA9 5-min history
        if not _isnan(e9_5m):
            self._ema9_5m_history.append(e9_5m)

        # Recent bars
        self.recent_bars_5m.append(bar)

        # Compute 1-min vol_ma for the snapshot (current state)
        vol_ma_1m = sum(self._vol_buf_1m) / len(self._vol_buf_1m) if len(self._vol_buf_1m) >= 5 else NaN
        rvol_1m = bar.volume / vol_ma_1m if (not _isnan(vol_ma_1m) and vol_ma_1m > 0) else NaN

        return self._build_snapshot(
            bar=bar,
            bar_idx=self._bar_idx_5m,
            timeframe=5,
            hhmm=hhmm,
            bar_date=bar_date,
            e9_1m=self.ema9_1m.value,
            e20_1m=self.ema20_1m.value,
            vw=self.vwap.value,
            vol_ma_1m=vol_ma_1m,
            rvol_1m=rvol_1m,
            # 5-min specific
            e9_5m=e9_5m,
            e20_5m=e20_5m,
            vol_ma_5m=vol_ma_5m,
            rvol_5m=rvol_5m,
        )

    def _build_snapshot(self, *, bar, bar_idx, timeframe, hhmm, bar_date,
                        e9_1m, e20_1m, vw, vol_ma_1m, rvol_1m,
                        e9_5m=None, e20_5m=None,
                        vol_ma_5m=None, rvol_5m=None) -> IndicatorSnapshot:
        """Build a snapshot with all fields populated."""

        # Use stored 5-min values when not provided (1-min update path)
        if e9_5m is None:
            e9_5m = self.ema9_5m.value
        if e20_5m is None:
            e20_5m = self.ema20_5m.value
        if vol_ma_5m is None:
            vol_ma_5m = sum(self._vol_buf_5m) / len(self._vol_buf_5m) if len(self._vol_buf_5m) >= 5 else NaN
        if rvol_5m is None:
            rvol_5m = bar.volume / vol_ma_5m if (not _isnan(vol_ma_5m) and vol_ma_5m > 0) else NaN

        # ATR values (explicit per timeframe)
        atr_1m_val = self.atr_1m.value
        atr_5m_val = self.atr_5m.value

        # Backward compat: snap.atr = 5-min ATR value (or daily fallback)
        if not _isnan(atr_5m_val):
            atr_compat = atr_5m_val
        elif not _isnan(self._daily_atr_value):
            atr_compat = self._daily_atr_value
        else:
            atr_compat = atr_1m_val  # last resort

        return IndicatorSnapshot(
            bar=bar,
            bar_idx=bar_idx,
            timeframe=timeframe,

            # Market context scores (set by manager/dashboard)
            in_play_score=self._in_play_score,
            in_play_passed=self._in_play_passed,
            regime_score=self._regime_score,
            alignment_score=self._alignment_score,
            rs_market=self._rs_market,

            # 1-min EMAs
            ema9_1m=e9_1m,
            ema20_1m=e20_1m,
            prev_ema9_1m=self._prev_ema9_1m,
            prev_ema20_1m=self._prev_ema20_1m,
            ema9_1m_ready=self.ema9_1m.ready,
            ema20_1m_ready=self.ema20_1m.ready,

            # 5-min EMAs
            ema9_5m=e9_5m,
            ema20_5m=e20_5m,
            prev_ema9_5m=self._prev_ema9_5m,
            prev_ema20_5m=self._prev_ema20_5m,
            ema9_5m_ready=self.ema9_5m.ready,
            ema20_5m_ready=self.ema20_5m.ready,

            # Backward-compatible aliases (= 5-min values)
            ema9=e9_5m,
            ema20=e20_5m,
            prev_ema9=self._prev_ema9_5m,
            prev_ema20=self._prev_ema20_5m,
            ema9_ready=self.ema9_5m.ready,
            ema20_ready=self.ema20_5m.ready,

            # VWAP (canonical, from 1-min)
            vwap=vw if not _isnan(vw) else self.vwap.value,
            vwap_ready=self.vwap.ready,

            # ATR (explicit per timeframe)
            atr_1m=atr_1m_val,
            atr_5m=atr_5m_val,
            daily_atr=self._daily_atr_value,
            atr_1m_ready=self.atr_1m.intra_ready or not _isnan(self.atr_1m.daily_value),
            atr_5m_ready=self.atr_5m.intra_ready or not _isnan(self.atr_5m.daily_value),

            # Backward-compatible atr (= 5-min with daily fallback)
            atr=atr_compat,
            atr_ready=self.atr_5m.intra_ready or not _isnan(self._daily_atr_value),

            # Volume (per timeframe)
            vol_ma_1m=vol_ma_1m,
            vol_ma_5m=vol_ma_5m,
            rvol_1m=rvol_1m,
            rvol_5m=rvol_5m,

            # Backward-compatible aliases (= 5-min)
            vol_ma20=vol_ma_5m,
            rvol=rvol_5m,

            # Session state
            session_open=self._session_open,
            session_high=self._session_high,
            session_low=self._session_low,
            day_date=bar_date,

            # Prior day levels
            prior_day_high=self.prior_day_high,
            prior_day_low=self.prior_day_low,

            # Opening range
            or_high=self._or_high,
            or_low=self._or_low,
            or_ready=self._or_ready,

            # Time
            hhmm=hhmm,
            bar_idx_1m=self._bar_idx_1m,
            bar_idx_5m=self._bar_idx_5m,

            # Recent bars
            recent_bars=list(self.recent_bars_5m),    # backward compat = 5m
            recent_bars_1m=list(self.recent_bars_1m),
            recent_bars_5m=list(self.recent_bars_5m),
        )

    # ── Backward-compatible update() for equivalence tests ──

    def update(self, bar: Bar) -> IndicatorSnapshot:
        """Legacy single-timeframe update. Treats bar as 5-min.

        This preserves backward compatibility for equivalence_test.py and
        any code that calls indicators.update(bar) directly. It updates
        BOTH 1-min and 5-min indicators with the same bar (since in the
        old architecture, there was only one timeframe).
        """
        # Update 1-min path (session state, VWAP, 1-min EMAs)
        self.update_1min(bar)
        # Update 5-min path and return the 5-min snapshot
        return self.update_5min(bar)

    # ── In-play baseline accessors ─────────────────────────────────────

    def get_in_play_baselines(self) -> dict:
        """Return current in-play baselines for SessionSnapshot construction.

        Called by the dashboard after first N bars of the session to build
        a SessionSnapshot for evaluate_session().

        Returns dict with:
            vol_baseline: float   — 20-day avg of first-N-bar volume (0 if not ready)
            range_baseline: float — 20-day avg of first-N-bar range (0 if not ready)
            vol_baseline_depth: int  — sessions in rolling buffer (0-20)
            range_baseline_depth: int — sessions in rolling buffer (0-20)
            prev_close: float     — prior session close (NaN if not available)
            session_bars: list    — first N bars of current session (may be < N)
            session_evaluated: bool — True if first N bars collected
        """
        vol_depth = len(self._ip_vol_buf)
        range_depth = len(self._ip_range_buf)

        vol_baseline = 0.0
        if vol_depth >= 5:
            vol_baseline = sum(self._ip_vol_buf) / vol_depth

        range_baseline = 0.0
        if range_depth >= 5:
            range_baseline = sum(self._ip_range_buf) / range_depth

        return {
            "vol_baseline": vol_baseline,
            "range_baseline": range_baseline,
            "vol_baseline_depth": vol_depth,
            "range_baseline_depth": range_depth,
            "prev_close": self._ip_prev_close,
            "session_bars": list(self._ip_session_bars),
            "session_evaluated": self._ip_session_evaluated,
        }

    @property
    def ip_session_evaluated(self) -> bool:
        """True when first N bars of current session have been collected."""
        return self._ip_session_evaluated

    @property
    def in_play_score(self) -> float:
        """Current session's in-play score (0-10). 0.0 = not yet evaluated."""
        return self._in_play_score

    @in_play_score.setter
    def in_play_score(self, value: float):
        """Set by dashboard after V2 evaluation. Never zeroed on failure."""
        self._in_play_score = value

    @property
    def in_play_passed(self) -> bool:
        """V2: whether this symbol passes the active in-play gate."""
        return self._in_play_passed

    @in_play_passed.setter
    def in_play_passed(self, value: bool):
        self._in_play_passed = value

    @property
    def regime_score(self) -> float:
        return self._regime_score

    @regime_score.setter
    def regime_score(self, value: float):
        self._regime_score = value

    @property
    def alignment_score(self) -> float:
        return self._alignment_score

    @alignment_score.setter
    def alignment_score(self, value: float):
        self._alignment_score = value

    @property
    def rs_market(self) -> float:
        return self._rs_market

    @rs_market.setter
    def rs_market(self, value: float):
        self._rs_market = value

    @property
    def ema9_history(self) -> Deque[float]:
        """Read-only EMA9 history for slope checks (5-min)."""
        return self._ema9_5m_history

    @property
    def ema9_1m_history(self) -> Deque[float]:
        """Read-only EMA9 history for slope checks (1-min)."""
        return self._ema9_1m_history
