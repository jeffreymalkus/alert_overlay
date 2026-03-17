"""
Acceptance Study — "Reclaim → Hold → Expand" Research Program

Tests whether the acceptance mechanic (cross level → hold N bars → expansion trigger)
materially improves long continuation setups across multiple level types.

Four setup families:
  A. EMA9_ACCEPT:  9EMA reclaim → hold above → expansion trigger
  B. VK_ACCEPT:    VWAP touch → hold near → expansion trigger
  C. OR_ACCEPT:    OR-high reclaim → hold above → expansion trigger
  D. COMP_ACCEPT:  Compression range → breakout → hold above → expansion trigger

Each family tested with:
  - Baseline (proximity/immediate entry)
  - Acceptance variant (hold 1, 2, 3 bars + expansion trigger)
  - Two trigger styles: micro-high break vs expansion close
  - Two stop bases: acceptance-low vs trigger-bar low

Context: GREEN SPY days only, AM window (10:00-11:30), market_align gate.

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.acceptance_study
"""

import math
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Tuple

from ..backtest import load_bars_from_csv
from ..indicators import EMA, VWAPCalc
from ..market_context import MarketEngine, MarketTrend, compute_market_context, get_sector_etf, SECTOR_MAP
from ..models import Bar, NaN

DATA_DIR = Path(__file__).parent.parent / "data"

_isnan = math.isnan


# ════════════════════════════════════════════════════════════════
#  Trade model
# ════════════════════════════════════════════════════════════════

@dataclass
class ATrade:
    """Standalone acceptance trade."""
    symbol: str
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float  # entry + target_rr * risk
    direction: int       # 1=long
    setup: str           # e.g. "EMA9_ACCEPT_H2_MH"
    risk: float = 0.0
    pnl_rr: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0

    @property
    def entry_date(self) -> date:
        return self.entry_time.date()


# ════════════════════════════════════════════════════════════════
#  Shared infrastructure
# ════════════════════════════════════════════════════════════════

def load_bars(sym: str) -> list:
    p = DATA_DIR / f"{sym}_5min.csv"
    return load_bars_from_csv(str(p)) if p.exists() else []


def get_universe() -> list:
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    return sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])


def classify_spy_days(spy_bars: list) -> dict:
    daily = defaultdict(list)
    for b in spy_bars:
        daily[b.timestamp.date()].append(b)
    day_info = {}
    for d in sorted(daily.keys()):
        bars = daily[d]
        o, c = bars[0].open, bars[-1].close
        chg = (c - o) / o * 100 if o > 0 else 0
        if chg > 0.05:
            day_info[d] = "GREEN"
        elif chg < -0.05:
            day_info[d] = "RED"
        else:
            day_info[d] = "FLAT"
    return day_info


def simulate_trade(trade: ATrade, bars: list, bar_idx: int,
                   max_bars: int = 78, target_rr: float = 3.0) -> ATrade:
    """Simulate from entry bar forward. Stop/target/EOD exit."""
    risk = trade.risk
    if risk <= 0:
        trade.pnl_rr = 0
        trade.exit_reason = "invalid"
        return trade

    for i in range(bar_idx + 1, min(bar_idx + max_bars + 1, len(bars))):
        b = bars[i]
        trade.bars_held += 1
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute

        # EOD exit
        if hhmm >= 1555 or (i == len(bars) - 1):
            pnl = (b.close - trade.entry_price) * trade.direction
            trade.pnl_rr = pnl / risk
            trade.exit_reason = "eod"
            return trade

        # Stop check
        if trade.direction == 1 and b.low <= trade.stop_price:
            trade.pnl_rr = (trade.stop_price - trade.entry_price) / risk
            trade.exit_reason = "stop"
            return trade

        # Target check
        if not _isnan(trade.target_price) and trade.direction == 1 and b.high >= trade.target_price:
            trade.pnl_rr = target_rr
            trade.exit_reason = "target"
            return trade

    # Fell through (shouldn't happen)
    trade.exit_reason = "eod"
    trade.pnl_rr = 0
    return trade


# ════════════════════════════════════════════════════════════════
#  Market context helper
# ════════════════════════════════════════════════════════════════

def build_spy_market_snapshots(spy_bars: list) -> dict:
    """Build per-bar SPY snapshots for market alignment check."""
    me = MarketEngine()
    snapshots = {}  # (date, hhmm) -> MarketSnapshot
    for b in spy_bars:
        snap = me.process_bar(b)
        d = b.timestamp.date()
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute
        snapshots[(d, hhmm)] = snap
    return snapshots


def is_market_aligned_long(spy_ctx: dict, d: date, hhmm: int) -> bool:
    """Check if market is not structurally weak (vk_ctx_ok_long equivalent)."""
    snap = spy_ctx.get((d, hhmm))
    if snap is None or not snap.ready:
        return True  # no data = allow
    # Structurally weak = below VWAP AND ema9 below ema20
    if not snap.above_vwap and not snap.ema9_above_ema20:
        return False
    return True


# ════════════════════════════════════════════════════════════════
#  Setup A: EMA9 Acceptance
# ════════════════════════════════════════════════════════════════

def ema9_accept(bars: list, sym: str, spy_day_info: dict, spy_ctx: dict,
                hold_bars: int = 2,
                trigger: str = "micro_high",  # "micro_high" or "expansion_close"
                stop_basis: str = "accept_low",  # "accept_low" or "trigger_low"
                target_rr: float = 3.0,
                time_start: int = 1000, time_end: int = 1130,
                min_body_pct: float = 0.40,
                require_green: bool = True,
                require_market: bool = True) -> List[ATrade]:
    """
    EMA9 Acceptance: price drops below EMA9 → reclaims above → holds above
    for N bars → triggers on expansion.

    Baseline (hold_bars=0): fire immediately on reclaim bar.
    """
    trades = []
    ema9 = EMA(9)
    vol_buf = deque(maxlen=20)
    vol_ma = NaN

    was_below = False
    hold_count = 0
    hold_low = NaN
    micro_high = NaN
    triggered_today = None

    prev_date = None

    for i, bar in enumerate(bars):
        e9 = ema9.update(bar.close)
        # Volume MA [1]
        if len(vol_buf) == 20:
            vol_ma = sum(vol_buf) / 20
        vol_buf.append(bar.volume)

        if not ema9.ready:
            continue

        d = bar.timestamp.date()
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute

        # Day reset
        if d != prev_date:
            was_below = False
            hold_count = 0
            hold_low = NaN
            micro_high = NaN
            triggered_today = None
            prev_date = d

        # Context gates
        if require_green and spy_day_info.get(d) != "GREEN":
            continue
        if hhmm < time_start or hhmm > time_end:
            # Still track state outside window
            above_ema = bar.close > e9
            if not above_ema:
                was_below = True
                hold_count = 0
                hold_low = NaN
                micro_high = NaN
            continue
        if triggered_today == d:
            continue

        above_ema = bar.close > e9

        # State machine
        if not above_ema:
            was_below = True
            hold_count = 0
            hold_low = NaN
            micro_high = NaN
        elif was_below and above_ema and hold_count == 0:
            # Just reclaimed
            hold_count = 1
            hold_low = bar.low
            micro_high = bar.high
        elif hold_count > 0 and hold_count < hold_bars:
            if above_ema:
                hold_count += 1
                hold_low = min(hold_low, bar.low) if not _isnan(hold_low) else bar.low
                micro_high = max(micro_high, bar.high) if not _isnan(micro_high) else bar.high
            else:
                was_below = True
                hold_count = 0
                hold_low = NaN
                micro_high = NaN

        # Trigger check
        if hold_count >= hold_bars and above_ema:
            # Baseline (hold_bars=0) triggers on reclaim bar itself
            rng = bar.high - bar.low
            body = abs(bar.close - bar.open)
            is_bull = bar.close > bar.open
            body_pct = body / rng if rng > 0 else 0

            trigger_ok = False
            if trigger == "micro_high" and hold_bars > 0:
                # Price must break above the micro-high of the hold period
                trigger_ok = bar.high > micro_high and is_bull and body_pct >= min_body_pct
            elif trigger == "expansion_close":
                trigger_ok = is_bull and body_pct >= min_body_pct
            else:
                # Baseline or hold_bars=0
                trigger_ok = is_bull and body_pct >= min_body_pct

            vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 0.70 * vol_ma
            market_ok = not require_market or is_market_aligned_long(spy_ctx, d, hhmm)

            if trigger_ok and vol_ok and market_ok:
                if stop_basis == "accept_low":
                    stop = (hold_low if not _isnan(hold_low) else bar.low) - 0.02
                else:
                    stop = bar.low - 0.02

                risk = bar.close - stop
                if risk > 0:
                    target = bar.close + target_rr * risk
                    t = ATrade(
                        symbol=sym, entry_time=bar.timestamp,
                        entry_price=bar.close, stop_price=stop,
                        target_price=target, direction=1,
                        setup=f"EMA9_H{hold_bars}_{trigger[:2].upper()}_{stop_basis[:3].upper()}",
                        risk=risk,
                    )
                    t = simulate_trade(t, bars, i, target_rr=target_rr)
                    trades.append(t)
                    triggered_today = d

                    # Reset state
                    was_below = False
                    hold_count = 0
                    hold_low = NaN
                    micro_high = NaN

    return trades


# ════════════════════════════════════════════════════════════════
#  Setup B: VWAP Kiss Acceptance
# ════════════════════════════════════════════════════════════════

def vwap_kiss_accept(bars: list, sym: str, spy_day_info: dict, spy_ctx: dict,
                     hold_bars: int = 2,
                     trigger: str = "expansion_close",
                     stop_basis: str = "accept_low",
                     target_rr: float = 3.0,
                     time_start: int = 1000, time_end: int = 1130,
                     min_body_pct: float = 0.40,
                     kiss_atr_frac: float = 0.05,
                     require_green: bool = True,
                     require_market: bool = True) -> List[ATrade]:
    """
    VWAP Kiss Acceptance: price pulls back to within kiss_dist of VWAP,
    holds near/above VWAP for N bars, then triggers on expansion.

    Baseline (hold_bars=0): fire immediately on kiss bar (proximity entry).
    """
    trades = []
    ema9 = EMA(9)
    vwap = VWAPCalc()
    vol_buf = deque(maxlen=20)
    vol_ma = NaN
    atr_buf = deque(maxlen=14)
    intra_atr = NaN

    touched = False
    hold_count = 0
    hold_low = NaN
    micro_high = NaN
    triggered_today = None
    prev_date = None

    for i, bar in enumerate(bars):
        e9 = ema9.update(bar.close)
        tp = (bar.high + bar.low + bar.close) / 3.0

        d = bar.timestamp.date()
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute

        # Day reset
        if d != prev_date:
            vwap.reset()
            touched = False
            hold_count = 0
            hold_low = NaN
            micro_high = NaN
            triggered_today = None
            prev_date = d

        vw = vwap.update(tp, bar.volume)

        # Volume MA [1]
        if len(vol_buf) == 20:
            vol_ma = sum(vol_buf) / 20
        vol_buf.append(bar.volume)

        # Intra ATR
        tr = bar.high - bar.low
        atr_buf.append(tr)
        if len(atr_buf) >= 5:
            intra_atr = sum(atr_buf) / len(atr_buf)

        if not vwap.ready or _isnan(intra_atr):
            continue

        if require_green and spy_day_info.get(d) != "GREEN":
            continue
        if hhmm < time_start or hhmm > time_end:
            continue
        if triggered_today == d:
            continue

        kiss_dist = kiss_atr_frac * intra_atr
        above_vwap = bar.close > vw
        near_vwap = abs(bar.low - vw) <= kiss_dist or bar.low <= vw <= bar.high

        # State machine
        if near_vwap and above_vwap:
            if not touched:
                touched = True
                hold_count = 1
                hold_low = bar.low
                micro_high = bar.high
            elif hold_count > 0 and hold_count < hold_bars:
                hold_count += 1
                hold_low = min(hold_low, bar.low)
                micro_high = max(micro_high, bar.high)
        elif above_vwap and touched and hold_count > 0:
            if hold_count < hold_bars:
                hold_count += 1
                hold_low = min(hold_low, bar.low)
                micro_high = max(micro_high, bar.high)
        elif not above_vwap:
            # Lost VWAP — reset
            touched = False
            hold_count = 0
            hold_low = NaN
            micro_high = NaN

        # Trigger check
        if hold_count >= hold_bars and above_vwap and touched:
            rng = bar.high - bar.low
            body = abs(bar.close - bar.open)
            is_bull = bar.close > bar.open
            body_pct = body / rng if rng > 0 else 0

            trigger_ok = False
            if trigger == "micro_high" and hold_bars > 0:
                trigger_ok = bar.high > micro_high and is_bull and body_pct >= min_body_pct
            else:
                trigger_ok = is_bull and body_pct >= min_body_pct

            vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 0.70 * vol_ma
            market_ok = not require_market or is_market_aligned_long(spy_ctx, d, hhmm)

            if trigger_ok and vol_ok and market_ok:
                if stop_basis == "accept_low":
                    stop = (hold_low if not _isnan(hold_low) else bar.low) - 0.02
                else:
                    stop = bar.low - 0.02
                # Also ensure stop is below VWAP
                stop = min(stop, vw - 0.02)

                risk = bar.close - stop
                if risk > 0:
                    target = bar.close + target_rr * risk
                    t = ATrade(
                        symbol=sym, entry_time=bar.timestamp,
                        entry_price=bar.close, stop_price=stop,
                        target_price=target, direction=1,
                        setup=f"VK_H{hold_bars}_{trigger[:2].upper()}_{stop_basis[:3].upper()}",
                        risk=risk,
                    )
                    t = simulate_trade(t, bars, i, target_rr=target_rr)
                    trades.append(t)
                    triggered_today = d

                    touched = False
                    hold_count = 0
                    hold_low = NaN
                    micro_high = NaN

    return trades


# ════════════════════════════════════════════════════════════════
#  Setup C: OR Continuation Acceptance
# ════════════════════════════════════════════════════════════════

def or_accept(bars: list, sym: str, spy_day_info: dict, spy_ctx: dict,
              hold_bars: int = 2,
              trigger: str = "expansion_close",
              stop_basis: str = "accept_low",
              target_rr: float = 3.0,
              time_start: int = 1000, time_end: int = 1130,
              min_body_pct: float = 0.40,
              require_green: bool = True,
              require_market: bool = True) -> List[ATrade]:
    """
    OR Continuation Acceptance: after OR (9:30-9:45), price pulls back below
    OR high, then reclaims above OR high, holds for N bars, triggers on expansion.

    Baseline (hold_bars=0): fire immediately when bar closes above OR high.
    """
    trades = []
    vol_buf = deque(maxlen=20)
    vol_ma = NaN

    or_high = NaN
    or_low = NaN
    or_ready = False
    was_below_or = False
    hold_count = 0
    hold_low = NaN
    micro_high = NaN
    triggered_today = None
    prev_date = None

    for i, bar in enumerate(bars):
        d = bar.timestamp.date()
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute

        # Volume MA [1]
        if len(vol_buf) == 20:
            vol_ma = sum(vol_buf) / 20
        vol_buf.append(bar.volume)

        # Day reset
        if d != prev_date:
            or_high = NaN
            or_low = NaN
            or_ready = False
            was_below_or = False
            hold_count = 0
            hold_low = NaN
            micro_high = NaN
            triggered_today = None
            prev_date = d

        # Build OR
        if 930 <= hhmm < 945:
            if _isnan(or_high) or bar.high > or_high:
                or_high = bar.high
            if _isnan(or_low) or bar.low < or_low:
                or_low = bar.low
        elif hhmm >= 945 and not _isnan(or_high):
            or_ready = True

        if not or_ready:
            continue
        if require_green and spy_day_info.get(d) != "GREEN":
            continue
        if hhmm < time_start or hhmm > time_end:
            # Track was_below outside window
            if bar.close < or_high:
                was_below_or = True
                hold_count = 0
                hold_low = NaN
                micro_high = NaN
            continue
        if triggered_today == d:
            continue

        above_or = bar.close > or_high

        # State machine
        if not above_or:
            was_below_or = True
            hold_count = 0
            hold_low = NaN
            micro_high = NaN
        elif was_below_or and above_or and hold_count == 0:
            hold_count = 1
            hold_low = bar.low
            micro_high = bar.high
        elif hold_count > 0 and hold_count < hold_bars:
            if above_or:
                hold_count += 1
                hold_low = min(hold_low, bar.low) if not _isnan(hold_low) else bar.low
                micro_high = max(micro_high, bar.high) if not _isnan(micro_high) else bar.high
            else:
                was_below_or = True
                hold_count = 0
                hold_low = NaN
                micro_high = NaN

        # Trigger
        if hold_count >= hold_bars and above_or:
            rng = bar.high - bar.low
            body = abs(bar.close - bar.open)
            is_bull = bar.close > bar.open
            body_pct = body / rng if rng > 0 else 0

            trigger_ok = False
            if trigger == "micro_high" and hold_bars > 0:
                trigger_ok = bar.high > micro_high and is_bull and body_pct >= min_body_pct
            else:
                trigger_ok = is_bull and body_pct >= min_body_pct

            vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 0.70 * vol_ma
            market_ok = not require_market or is_market_aligned_long(spy_ctx, d, hhmm)

            if trigger_ok and vol_ok and market_ok:
                if stop_basis == "accept_low":
                    stop = (hold_low if not _isnan(hold_low) else bar.low) - 0.02
                else:
                    stop = bar.low - 0.02
                # Also below OR high as backstop
                stop = min(stop, or_high - 0.02)

                risk = bar.close - stop
                if risk > 0:
                    target = bar.close + target_rr * risk
                    t = ATrade(
                        symbol=sym, entry_time=bar.timestamp,
                        entry_price=bar.close, stop_price=stop,
                        target_price=target, direction=1,
                        setup=f"OR_H{hold_bars}_{trigger[:2].upper()}_{stop_basis[:3].upper()}",
                        risk=risk,
                    )
                    t = simulate_trade(t, bars, i, target_rr=target_rr)
                    trades.append(t)
                    triggered_today = d

                    was_below_or = False
                    hold_count = 0
                    hold_low = NaN
                    micro_high = NaN

    return trades


# ════════════════════════════════════════════════════════════════
#  Setup D: Compression Breakout Acceptance
# ════════════════════════════════════════════════════════════════

def compression_accept(bars: list, sym: str, spy_day_info: dict, spy_ctx: dict,
                       hold_bars: int = 2,
                       trigger: str = "expansion_close",
                       stop_basis: str = "accept_low",
                       target_rr: float = 3.0,
                       time_start: int = 1000, time_end: int = 1130,
                       min_body_pct: float = 0.40,
                       comp_bars: int = 4,
                       comp_shrink: float = 0.60,
                       require_green: bool = True,
                       require_market: bool = True) -> List[ATrade]:
    """
    Compression Breakout Acceptance: detect N-bar compression (range shrinking),
    then price breaks above the compression high, holds for N bars, triggers
    on expansion.

    Compression: comp_bars consecutive bars where the N-bar range is <=
    comp_shrink × the prior N-bar range.

    Baseline (hold_bars=0): fire immediately on breakout bar.
    """
    trades = []
    vol_buf = deque(maxlen=20)
    vol_ma = NaN
    range_buf = deque(maxlen=8)

    comp_high = NaN
    comp_low = NaN
    comp_detected = False
    hold_count = 0
    hold_low = NaN
    micro_high = NaN
    triggered_today = None
    prev_date = None

    for i, bar in enumerate(bars):
        d = bar.timestamp.date()
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute

        if len(vol_buf) == 20:
            vol_ma = sum(vol_buf) / 20
        vol_buf.append(bar.volume)

        if d != prev_date:
            comp_detected = False
            hold_count = 0
            hold_low = NaN
            micro_high = NaN
            triggered_today = None
            range_buf.clear()
            prev_date = d

        range_buf.append(bar.high - bar.low)

        if require_green and spy_day_info.get(d) != "GREEN":
            continue
        if hhmm < time_start or hhmm > time_end:
            continue
        if triggered_today == d:
            continue

        # Detect compression: last comp_bars ranges shrinking
        if len(range_buf) >= comp_bars * 2:
            recent = list(range_buf)[-comp_bars:]
            prior = list(range_buf)[-(comp_bars*2):-comp_bars]
            recent_avg = sum(recent) / len(recent)
            prior_avg = sum(prior) / len(prior)
            if prior_avg > 0 and recent_avg <= comp_shrink * prior_avg:
                if not comp_detected:
                    comp_detected = True
                    comp_high = max(b.high for b in bars[max(0,i-comp_bars+1):i+1])
                    comp_low = min(b.low for b in bars[max(0,i-comp_bars+1):i+1])

        if not comp_detected:
            continue

        above_comp = bar.close > comp_high

        # State machine
        if not above_comp and hold_count == 0:
            pass  # waiting for breakout
        elif above_comp and hold_count == 0:
            hold_count = 1
            hold_low = bar.low
            micro_high = bar.high
        elif hold_count > 0 and hold_count < hold_bars:
            if above_comp:
                hold_count += 1
                hold_low = min(hold_low, bar.low) if not _isnan(hold_low) else bar.low
                micro_high = max(micro_high, bar.high) if not _isnan(micro_high) else bar.high
            else:
                hold_count = 0
                hold_low = NaN
                micro_high = NaN
                comp_detected = False

        # Trigger
        if hold_count >= hold_bars and above_comp:
            rng = bar.high - bar.low
            body = abs(bar.close - bar.open)
            is_bull = bar.close > bar.open
            body_pct = body / rng if rng > 0 else 0

            trigger_ok = False
            if trigger == "micro_high" and hold_bars > 0:
                trigger_ok = bar.high > micro_high and is_bull and body_pct >= min_body_pct
            else:
                trigger_ok = is_bull and body_pct >= min_body_pct

            vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 0.70 * vol_ma
            market_ok = not require_market or is_market_aligned_long(spy_ctx, d, hhmm)

            if trigger_ok and vol_ok and market_ok:
                if stop_basis == "accept_low":
                    stop = (hold_low if not _isnan(hold_low) else bar.low) - 0.02
                else:
                    stop = bar.low - 0.02
                stop = min(stop, comp_low - 0.02)

                risk = bar.close - stop
                if risk > 0:
                    target = bar.close + target_rr * risk
                    t = ATrade(
                        symbol=sym, entry_time=bar.timestamp,
                        entry_price=bar.close, stop_price=stop,
                        target_price=target, direction=1,
                        setup=f"COMP_H{hold_bars}_{trigger[:2].upper()}_{stop_basis[:3].upper()}",
                        risk=risk,
                    )
                    t = simulate_trade(t, bars, i, target_rr=target_rr)
                    trades.append(t)
                    triggered_today = d

                    comp_detected = False
                    hold_count = 0
                    hold_low = NaN
                    micro_high = NaN

    return trades


# ════════════════════════════════════════════════════════════════
#  Metrics
# ════════════════════════════════════════════════════════════════

def compute_r_metrics(trades: List[ATrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "pf_r": 0, "exp_r": 0, "total_r": 0, "max_dd_r": 0,
                "stop_rate": 0, "quick_stop": 0, "target_rate": 0}

    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    total_r = sum(t.pnl_rr for t in trades)
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    quick = sum(1 for t in trades if t.exit_reason == "stop" and t.bars_held <= 2)
    targets = sum(1 for t in trades if t.exit_reason == "target")

    cum = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl_rr
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum

    return {"n": n, "pf_r": pf, "exp_r": total_r / n, "total_r": total_r,
            "max_dd_r": dd, "stop_rate": stopped / n * 100,
            "quick_stop": quick / n * 100, "target_rate": targets / n * 100}


def robustness(trades: List[ATrade]) -> dict:
    daily_r = defaultdict(float)
    sym_r = defaultdict(float)
    for t in trades:
        daily_r[t.entry_date] += t.pnl_rr
        sym_r[t.symbol] += t.pnl_rr

    best_day = max(daily_r, key=daily_r.get) if daily_r else None
    top_sym = max(sym_r, key=sym_r.get) if sym_r else None

    ex_day = [t for t in trades if t.entry_date != best_day] if best_day else trades
    ex_sym = [t for t in trades if t.symbol != top_sym] if top_sym else trades

    ex_day_m = compute_r_metrics(ex_day)
    ex_sym_m = compute_r_metrics(ex_sym)

    # Train/test (odd/even)
    train = [t for t in trades if t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date.day % 2 == 0]
    tr_m = compute_r_metrics(train)
    te_m = compute_r_metrics(test)

    return {
        "ex_best_day_r": ex_day_m["total_r"],
        "ex_best_day_pf": ex_day_m["pf_r"],
        "best_day": str(best_day),
        "best_day_r": daily_r.get(best_day, 0),
        "ex_top_sym_r": ex_sym_m["total_r"],
        "ex_top_sym_pf": ex_sym_m["pf_r"],
        "top_sym": top_sym,
        "train_pf": tr_m["pf_r"],
        "test_pf": te_m["pf_r"],
        "train_exp": tr_m["exp_r"],
        "test_exp": te_m["exp_r"],
        "train_n": tr_m["n"],
        "test_n": te_m["n"],
        "stable": tr_m["pf_r"] >= 0.80 and te_m["pf_r"] >= 0.80 and tr_m["n"] >= 10 and te_m["n"] >= 10,
    }


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


# ════════════════════════════════════════════════════════════════
#  Test Matrix Definition
# ════════════════════════════════════════════════════════════════

WAVE1_MATRIX = {
    "EMA9": {
        "func": ema9_accept,
        "variants": [
            # Baseline: no hold, immediate entry
            {"hold_bars": 0, "trigger": "expansion_close", "stop_basis": "trigger_low"},
            # Acceptance: hold 1/2/3, expansion close, accept_low stop
            {"hold_bars": 1, "trigger": "expansion_close", "stop_basis": "accept_low"},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low"},
            {"hold_bars": 3, "trigger": "expansion_close", "stop_basis": "accept_low"},
            # Acceptance: hold 2, micro_high break
            {"hold_bars": 2, "trigger": "micro_high", "stop_basis": "accept_low"},
            # Acceptance: hold 2, trigger_low stop (tighter)
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "trigger_low"},
        ],
    },
    "VK": {
        "func": vwap_kiss_accept,
        "variants": [
            {"hold_bars": 0, "trigger": "expansion_close", "stop_basis": "trigger_low"},
            {"hold_bars": 1, "trigger": "expansion_close", "stop_basis": "accept_low"},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low"},
            {"hold_bars": 3, "trigger": "expansion_close", "stop_basis": "accept_low"},
            {"hold_bars": 2, "trigger": "micro_high", "stop_basis": "accept_low"},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "trigger_low"},
        ],
    },
    "OR": {
        "func": or_accept,
        "variants": [
            {"hold_bars": 0, "trigger": "expansion_close", "stop_basis": "trigger_low"},
            {"hold_bars": 1, "trigger": "expansion_close", "stop_basis": "accept_low"},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low"},
            {"hold_bars": 3, "trigger": "expansion_close", "stop_basis": "accept_low"},
            {"hold_bars": 2, "trigger": "micro_high", "stop_basis": "accept_low"},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "trigger_low"},
        ],
    },
    "COMP": {
        "func": compression_accept,
        "variants": [
            {"hold_bars": 0, "trigger": "expansion_close", "stop_basis": "trigger_low"},
            {"hold_bars": 1, "trigger": "expansion_close", "stop_basis": "accept_low"},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low"},
            {"hold_bars": 3, "trigger": "expansion_close", "stop_basis": "accept_low"},
            {"hold_bars": 2, "trigger": "micro_high", "stop_basis": "accept_low"},
        ],
    },
}


# ════════════════════════════════════════════════════════════════
#  WAVE 2 — Deepen H2_EX_ACC across EMA9, VK, OR
#  Sweep: time window, target R:R
#  COMP retired from deepening (acceptance barely helped)
# ════════════════════════════════════════════════════════════════

WAVE2_MATRIX = {
    "EMA9_W2": {
        "func": ema9_accept,
        "variants": [
            # W1 winner (reference)
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1130, "target_rr": 3.0},
            # Tighter window (match VR)
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1059, "target_rr": 3.0},
            # Very tight AM window
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1030, "target_rr": 3.0},
            # Target sweep (wide window)
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1130, "target_rr": 2.0},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1130, "target_rr": 2.5},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1130, "target_rr": 4.0},
            # Best combo: tight window + optimal target (will be determined)
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1059, "target_rr": 2.0},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1059, "target_rr": 2.5},
        ],
    },
    "VK_W2": {
        "func": vwap_kiss_accept,
        "variants": [
            # W1 winner (reference)
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1130, "target_rr": 3.0},
            # Tighter window
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1059, "target_rr": 3.0},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1030, "target_rr": 3.0},
            # Target sweep
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1130, "target_rr": 2.0},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1130, "target_rr": 2.5},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1130, "target_rr": 4.0},
            # Tight + target
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1059, "target_rr": 2.0},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1059, "target_rr": 2.5},
        ],
    },
    "OR_W2": {
        "func": or_accept,
        "variants": [
            # W1 winner (reference)
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1130, "target_rr": 3.0},
            # Tighter window
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1059, "target_rr": 3.0},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1030, "target_rr": 3.0},
            # Target sweep
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1130, "target_rr": 2.0},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1130, "target_rr": 2.5},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1130, "target_rr": 4.0},
            # Tight + target
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1059, "target_rr": 2.0},
            {"hold_bars": 2, "trigger": "expansion_close", "stop_basis": "accept_low",
             "time_start": 1000, "time_end": 1059, "target_rr": 2.5},
        ],
    },
}


# ════════════════════════════════════════════════════════════════
#  Runner
# ════════════════════════════════════════════════════════════════

def run_wave(matrix: dict, symbols: list, spy_day_info: dict, spy_ctx: dict,
             wave_name: str = "WAVE 1") -> dict:
    """Run all variants in a matrix, return results ledger."""

    print(f"\n{'='*140}")
    print(f"{wave_name} — ACCEPTANCE STUDY")
    print(f"{'='*140}")

    ledger = {}

    for family, spec in matrix.items():
        func = spec["func"]
        print(f"\n{'─'*140}")
        print(f"Family: {family}")
        print(f"{'─'*140}")

        print(f"\n  {'Variant':44s} {'N':>5s} {'PF(R)':>6s} {'Exp(R)':>8s} {'TotalR':>8s} "
              f"{'MaxDD':>7s} {'Stop%':>6s} {'QStop%':>6s} {'Tgt%':>5s} "
              f"{'TrnPF':>6s} {'TstPF':>6s} {'Stbl':>4s} {'ExDay':>8s} {'ExSym':>8s} {'Verdict':>8s}")
        print(f"  {'-'*44} {'-'*5} {'-'*6} {'-'*8} {'-'*8} "
              f"{'-'*7} {'-'*6} {'-'*6} {'-'*5} "
              f"{'-'*6} {'-'*6} {'-'*4} {'-'*8} {'-'*8} {'-'*8}")

        for v in spec["variants"]:
            all_trades = []
            for sym in symbols:
                bars = load_bars(sym)
                if not bars:
                    continue
                trades = func(bars, sym, spy_day_info, spy_ctx, **v)
                all_trades.extend(trades)

            m = compute_r_metrics(all_trades)
            rob = robustness(all_trades) if m["n"] >= 10 else {
                "ex_best_day_r": 0, "ex_top_sym_r": 0,
                "train_pf": 0, "test_pf": 0, "stable": False,
                "ex_best_day_pf": 0, "ex_top_sym_pf": 0,
                "train_exp": 0, "test_exp": 0,
            }

            # Variant label
            h = v["hold_bars"]
            trig = v["trigger"][:2].upper()
            sb = v["stop_basis"][:3].upper()
            te = v.get("time_end", 1130)
            rr = v.get("target_rr", 3.0)
            time_tag = f"_T{te}" if te != 1130 else ""
            rr_tag = f"_R{rr:.1f}" if rr != 3.0 else ""
            label = f"{family}_H{h}_{trig}_{sb}{time_tag}{rr_tag}"
            if h == 0:
                label = f"{family}_BASELINE_{trig}_{sb}{time_tag}{rr_tag}"

            # Verdict
            if m["n"] < 20:
                verdict = "INSUFF_N"
            elif m["pf_r"] >= 1.10 and m["exp_r"] > 0 and rob.get("stable", False):
                verdict = "PROMOTE"
            elif m["pf_r"] >= 1.0 and m["exp_r"] > 0:
                verdict = "CONTINUE"
            elif m["exp_r"] > 0:
                verdict = "MARGINAL"
            else:
                verdict = "RETIRE"

            print(f"  {label:44s} {m['n']:5d} {pf_str(m['pf_r']):>6s} {m['exp_r']:+7.3f}R "
                  f"{m['total_r']:+7.2f}R {m['max_dd_r']:6.2f}R {m['stop_rate']:5.1f}% {m['quick_stop']:5.1f}% "
                  f"{m['target_rate']:4.1f}% "
                  f"{pf_str(rob.get('train_pf',0)):>6s} {pf_str(rob.get('test_pf',0)):>6s} "
                  f"{'YES' if rob.get('stable') else ' NO':>4s} "
                  f"{rob.get('ex_best_day_r',0):+7.2f}R {rob.get('ex_top_sym_r',0):+7.2f}R "
                  f"{verdict:>8s}")

            ledger[label] = {
                "family": family,
                "params": v,
                "metrics": m,
                "robustness": rob,
                "verdict": verdict,
                "trades": all_trades,
            }

    return ledger


def print_wave_summary(ledger: dict, wave_name: str):
    """Print promote/continue/retire summary for a wave."""
    print(f"\n{'='*140}")
    print(f"{wave_name} SUMMARY — RESEARCH LEDGER")
    print(f"{'='*140}")

    promote = []
    continue_list = []
    retire = []
    for label, entry in sorted(ledger.items()):
        v = entry["verdict"]
        if v == "PROMOTE":
            promote.append(label)
        elif v in ("CONTINUE", "MARGINAL"):
            continue_list.append(label)
        else:
            retire.append(label)

    print(f"\n  PROMOTE ({len(promote)}):")
    for l in promote:
        e = ledger[l]
        print(f"    {l:48s}  PF={pf_str(e['metrics']['pf_r'])}  Exp={e['metrics']['exp_r']:+.3f}R  "
              f"TotalR={e['metrics']['total_r']:+.2f}R  N={e['metrics']['n']}")

    print(f"\n  CONTINUE ({len(continue_list)}):")
    for l in continue_list:
        e = ledger[l]
        print(f"    {l:48s}  PF={pf_str(e['metrics']['pf_r'])}  Exp={e['metrics']['exp_r']:+.3f}R  "
              f"TotalR={e['metrics']['total_r']:+.2f}R  N={e['metrics']['n']}")

    print(f"\n  RETIRE ({len(retire)}):")
    for l in retire:
        e = ledger[l]
        print(f"    {l:48s}  PF={pf_str(e['metrics']['pf_r'])}  Exp={e['metrics']['exp_r']:+.3f}R  "
              f"TotalR={e['metrics']['total_r']:+.2f}R  N={e['metrics']['n']}")

    print(f"\n{'='*140}")
    print(f"{wave_name} COMPLETE")
    print(f"{'='*140}")


def main():
    symbols = get_universe()
    spy_bars = load_bars("SPY")
    spy_day_info = classify_spy_days(spy_bars)
    spy_ctx = build_spy_market_snapshots(spy_bars)

    print("=" * 140)
    print("ACCEPTANCE STUDY — Reclaim → Hold → Expand Research Program")
    print("=" * 140)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Data: {spy_bars[0].timestamp.date()} → {spy_bars[-1].timestamp.date()}")
    print(f"Context: GREEN days only, AM window, market_align gate")
    print(f"Target R:R = 3.0, stop/target/EOD exit")

    # ── WAVE 1 ──
    ledger1 = run_wave(WAVE1_MATRIX, symbols, spy_day_info, spy_ctx, "WAVE 1")
    print_wave_summary(ledger1, "WAVE 1")

    # ── WAVE 2 ──
    ledger2 = run_wave(WAVE2_MATRIX, symbols, spy_day_info, spy_ctx, "WAVE 2")
    print_wave_summary(ledger2, "WAVE 2")

    # ── COMBINED LEDGER ──
    full_ledger = {**ledger1, **ledger2}
    print(f"\n{'='*140}")
    print("COMBINED LEDGER — TOP 10 BY PF(R)")
    print(f"{'='*140}")
    ranked = sorted(full_ledger.items(), key=lambda x: x[1]["metrics"]["pf_r"], reverse=True)
    print(f"\n  {'Rank':>4s} {'Variant':48s} {'N':>5s} {'PF(R)':>6s} {'Exp(R)':>8s} {'TotalR':>8s} "
          f"{'MaxDD':>7s} {'TrnPF':>6s} {'TstPF':>6s} {'ExDay':>8s} {'Wave':>6s}")
    print(f"  {'-'*4} {'-'*48} {'-'*5} {'-'*6} {'-'*8} {'-'*8} "
          f"{'-'*7} {'-'*6} {'-'*6} {'-'*8} {'-'*6}")
    for i, (label, entry) in enumerate(ranked[:10], 1):
        m = entry["metrics"]
        rob = entry["robustness"]
        wave = "W1" if label in ledger1 else "W2"
        print(f"  {i:4d} {label:48s} {m['n']:5d} {pf_str(m['pf_r']):>6s} {m['exp_r']:+7.3f}R "
              f"{m['total_r']:+7.2f}R {m['max_dd_r']:6.2f}R "
              f"{pf_str(rob.get('train_pf',0)):>6s} {pf_str(rob.get('test_pf',0)):>6s} "
              f"{rob.get('ex_best_day_r',0):+7.2f}R {wave:>6s}")

    # ── WAVE 3: Deep robustness on top 4 + VR reference ──
    print(f"\n{'='*140}")
    print("WAVE 3 — DEEP ROBUSTNESS STRESS TEST")
    print(f"{'='*140}")

    # Select top candidates from combined ledger
    wave3_candidates = [
        "VK_W2_H2_EX_ACC_T1059_R2.0",
        "VK_W2_H2_EX_ACC_R2.0",
        "VK_H2_EX_ACC",
        "EMA9_H2_EX_ACC",
    ]

    # Also run VR reference for apples-to-apples
    print("\n  Running VR reference (active candidate)...")
    from .portfolio_configs import candidate_v2_vrgreen_bdr
    from .backtest import run_backtest

    vr_cfg = candidate_v2_vrgreen_bdr()
    qqq_bars = load_bars("QQQ")
    vr_trades = []
    for sym in symbols:
        bars_raw = load_bars(sym)
        if not bars_raw:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = load_bars(sec_etf) if sec_etf and sec_etf not in {"SPY", "QQQ"} else None
        result = run_backtest(bars_raw, cfg=vr_cfg, spy_bars=spy_bars,
                              qqq_bars=qqq_bars, sector_bars=sec_bars)
        for t in result.trades:
            if t.signal.setup_name == "VWAP RECLAIM":
                trade_date = t.signal.timestamp.date() if hasattr(t.signal.timestamp, 'date') else t.signal.timestamp
                # Apply same perfect-foresight GREEN filter as acceptance families
                if spy_day_info.get(trade_date) != "GREEN":
                    continue
                vr_trades.append(ATrade(
                    symbol=sym, entry_time=t.signal.timestamp,
                    entry_price=t.signal.entry_price or 0,
                    stop_price=t.signal.stop_price or 0,
                    target_price=t.signal.target_price or 0,
                    direction=1, setup="VR_REFERENCE",
                    risk=t.signal.risk or 0,
                    pnl_rr=t.pnl_rr,
                    exit_reason=t.exit_reason or "",
                    bars_held=t.bars_held or 0,
                ))

    # Build candidate -> trades mapping
    candidate_trades = {}
    for label in wave3_candidates:
        if label in full_ledger:
            candidate_trades[label] = full_ledger[label]["trades"]
    candidate_trades["VR_REFERENCE"] = vr_trades

    # Deep robustness for each
    for label, trades in candidate_trades.items():
        print(f"\n{'─'*140}")
        print(f"  {label}")
        print(f"{'─'*140}")

        m = compute_r_metrics(trades)
        print(f"\n  Core: N={m['n']}  PF={pf_str(m['pf_r'])}  Exp={m['exp_r']:+.3f}R  "
              f"TotalR={m['total_r']:+.2f}R  MaxDD={m['max_dd_r']:.2f}R  "
              f"Stop%={m['stop_rate']:.1f}%  QStop%={m['quick_stop']:.1f}%  Tgt%={m['target_rate']:.1f}%")

        # Monthly breakdown
        monthly = defaultdict(list)
        for t in trades:
            key = t.entry_date.strftime("%Y-%m")
            monthly[key].append(t.pnl_rr)
        print(f"\n  Monthly:")
        months_positive = 0
        for mo in sorted(monthly.keys()):
            rs = monthly[mo]
            total = sum(rs)
            n = len(rs)
            wr = sum(1 for r in rs if r > 0) / n * 100 if n else 0
            wins = sum(r for r in rs if r > 0)
            losses = abs(sum(r for r in rs if r < 0))
            pf = wins / losses if losses > 0 else float('inf')
            if total > 0:
                months_positive += 1
            print(f"    {mo}: {n:4d} trades  {total:+7.2f}R  WR={wr:5.1f}%  PF={pf_str(pf)}")
        print(f"    Positive months: {months_positive}/{len(monthly)}")

        # Ex-top-3 days
        daily_r = defaultdict(float)
        for t in trades:
            daily_r[t.entry_date] += t.pnl_rr
        top3_days = sorted(daily_r.items(), key=lambda x: x[1], reverse=True)[:3]
        top3_total = sum(v for _, v in top3_days)
        ex3_trades = [t for t in trades if t.entry_date not in {d for d, _ in top3_days}]
        ex3_m = compute_r_metrics(ex3_trades)
        print(f"\n  Ex-top-3-days: {ex3_m['total_r']:+.2f}R  PF={pf_str(ex3_m['pf_r'])}  "
              f"(removed {top3_total:+.2f}R from top 3 days)")
        for d, v in top3_days:
            print(f"    {d}: {v:+.2f}R")

        # Ex-top-3 symbols
        sym_r = defaultdict(float)
        for t in trades:
            sym_r[t.symbol] += t.pnl_rr
        top3_syms = sorted(sym_r.items(), key=lambda x: x[1], reverse=True)[:3]
        top3_sym_total = sum(v for _, v in top3_syms)
        ex3s_trades = [t for t in trades if t.symbol not in {s for s, _ in top3_syms}]
        ex3s_m = compute_r_metrics(ex3s_trades)
        print(f"\n  Ex-top-3-syms: {ex3s_m['total_r']:+.2f}R  PF={pf_str(ex3s_m['pf_r'])}  "
              f"(removed {top3_sym_total:+.2f}R from top 3 symbols)")
        for s, v in top3_syms:
            print(f"    {s}: {v:+.2f}R")

        # Walk-forward (14-day windows)
        dates_sorted = sorted(set(t.entry_date for t in trades))
        if len(dates_sorted) >= 28:
            print(f"\n  Walk-forward (14-day windows):")
            win_size = 14
            i = 0
            while i + win_size <= len(dates_sorted):
                win_dates = set(dates_sorted[i:i+win_size])
                win_trades = [t for t in trades if t.entry_date in win_dates]
                wm = compute_r_metrics(win_trades)
                d0 = dates_sorted[i]
                d1 = dates_sorted[min(i+win_size-1, len(dates_sorted)-1)]
                print(f"    {d0} → {d1}: N={wm['n']:4d}  PF={pf_str(wm['pf_r'])}  "
                      f"Exp={wm['exp_r']:+.3f}R  Total={wm['total_r']:+.2f}R")
                i += win_size

        # Weekly win rate
        weekly_r = defaultdict(float)
        for t in trades:
            iso_yr, iso_wk, _ = t.entry_date.isocalendar()
            weekly_r[(iso_yr, iso_wk)] += t.pnl_rr
        pos_weeks = sum(1 for v in weekly_r.values() if v > 0)
        print(f"\n  Weekly WR: {pos_weeks}/{len(weekly_r)} weeks positive "
              f"({pos_weeks/len(weekly_r)*100:.1f}%)" if weekly_r else "")

        # Longest losing streak (by days)
        daily_sorted = sorted(daily_r.items())
        max_streak = 0
        cur_streak = 0
        for _, v in daily_sorted:
            if v < 0:
                cur_streak += 1
                max_streak = max(max_streak, cur_streak)
            else:
                cur_streak = 0
        print(f"  Max losing day streak: {max_streak}")

    # ── FINAL COMPARISON TABLE ──
    print(f"\n{'='*140}")
    print("FINAL COMPARISON — TOP CANDIDATES vs VR REFERENCE")
    print(f"{'='*140}")
    print(f"\n  {'Candidate':48s} {'N':>5s} {'PF':>5s} {'Exp':>7s} {'TotR':>8s} "
          f"{'MaxDD':>7s} {'Trn':>5s} {'Tst':>5s} {'ExD3':>8s} {'ExS3':>8s} {'Mo+':>4s} {'WkWR':>5s}")
    print(f"  {'-'*48} {'-'*5} {'-'*5} {'-'*7} {'-'*8} "
          f"{'-'*7} {'-'*5} {'-'*5} {'-'*8} {'-'*8} {'-'*4} {'-'*5}")

    for label, trades in candidate_trades.items():
        m = compute_r_metrics(trades)
        rob = robustness(trades) if m["n"] >= 10 else {}

        # Monthly positive count
        monthly = defaultdict(float)
        for t in trades:
            monthly[t.entry_date.strftime("%Y-%m")] += t.pnl_rr
        mo_pos = sum(1 for v in monthly.values() if v > 0)
        mo_tot = len(monthly)

        # Weekly WR
        weekly = defaultdict(float)
        for t in trades:
            yr, wk, _ = t.entry_date.isocalendar()
            weekly[(yr, wk)] += t.pnl_rr
        wk_wr = sum(1 for v in weekly.values() if v > 0) / len(weekly) * 100 if weekly else 0

        # Ex-top-3 days
        daily = defaultdict(float)
        for t in trades:
            daily[t.entry_date] += t.pnl_rr
        top3d = sorted(daily.values(), reverse=True)[:3]
        ex3d_trades = [t for t in trades
                       if t.entry_date not in {d for d, v in sorted(daily.items(), key=lambda x: x[1], reverse=True)[:3]}]
        ex3d_m = compute_r_metrics(ex3d_trades)

        # Ex-top-3 syms
        sym_totals = defaultdict(float)
        for t in trades:
            sym_totals[t.symbol] += t.pnl_rr
        top3s = sorted(sym_totals.items(), key=lambda x: x[1], reverse=True)[:3]
        ex3s_trades = [t for t in trades if t.symbol not in {s for s, _ in top3s}]
        ex3s_m = compute_r_metrics(ex3s_trades)

        print(f"  {label:48s} {m['n']:5d} {pf_str(m['pf_r']):>5s} {m['exp_r']:+6.3f}R "
              f"{m['total_r']:+7.2f}R {m['max_dd_r']:6.2f}R "
              f"{pf_str(rob.get('train_pf',0)):>5s} {pf_str(rob.get('test_pf',0)):>5s} "
              f"{ex3d_m['total_r']:+7.2f}R {ex3s_m['total_r']:+7.2f}R "
              f"{mo_pos}/{mo_tot} {wk_wr:5.1f}%")

    print(f"\n{'='*140}")
    print("ACCEPTANCE STUDY COMPLETE")
    print(f"{'='*140}")


if __name__ == "__main__":
    main()
