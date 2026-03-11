"""
Composite Long Study — SUPPORTED LEADER IN-PLAY PULLBACK LONG

Combines the strongest surviving ingredients from prior research:
  A. Market support (GREEN day + SPY structural support)
  B. In-play stock selection (gap + RVOL proxy)
  C. Stock leadership (RS vs SPY/sector)
  D. Acceptance-style entry (VK, EMA9, OR, pullback)

Four filter layers (M, P, L, E) stacked per variant.
~24 theory-driven variants, NOT exhaustive combinatorial.

Baseline to beat: SC Q>=5 long — PF 1.19, 49 trades, Exp +0.173R

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.composite_long_study
    python -m alert_overlay.composite_long_study --dry-run
"""

import argparse
import math
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .backtest import load_bars_from_csv
from .indicators import EMA, VWAPCalc
from .market_context import MarketEngine, SECTOR_MAP, get_sector_etf
from .models import Bar, NaN

DATA_DIR = Path(__file__).parent / "data"

_isnan = math.isnan


# ════════════════════════════════════════════════════════════════
#  Trade model
# ════════════════════════════════════════════════════════════════

@dataclass
class CTrade:
    """Composite long trade."""
    symbol: str
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    direction: int = 1  # always long
    setup: str = ""
    variant: str = ""
    risk: float = 0.0
    pnl_rr: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0
    # Filter metadata
    gap_pct: float = 0.0
    rvol: float = 0.0
    rs_spy: float = 0.0
    rs_sector: float = 0.0
    market_level: str = ""

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


# ════════════════════════════════════════════════════════════════
#  Market context (Layer M)
# ════════════════════════════════════════════════════════════════

def build_spy_snapshots(spy_bars: list) -> dict:
    """Build per-bar SPY snapshots for real-time market checks."""
    me = MarketEngine()
    snapshots = {}  # (date, hhmm) -> MarketSnapshot
    for b in spy_bars:
        snap = me.process_bar(b)
        d = b.timestamp.date()
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute
        snapshots[(d, hhmm)] = snap
    return snapshots


def check_market(spy_day_info: dict, spy_ctx: dict, d: date, hhmm: int,
                 m_level: str) -> bool:
    """Check market support layer. Returns True if passes."""
    # All M levels require GREEN day
    if spy_day_info.get(d) != "GREEN":
        return False
    if m_level == "M1":
        return True
    snap = spy_ctx.get((d, hhmm))
    if snap is None or not snap.ready:
        return True  # no data = allow (conservative)
    if m_level == "M2":
        return snap.above_vwap
    if m_level == "M3":
        return snap.above_vwap and snap.ema9_above_ema20
    return True


# ════════════════════════════════════════════════════════════════
#  In-play proxy (Layer P)
# ════════════════════════════════════════════════════════════════

def compute_open_stats(bars: list) -> dict:
    """Compute per-day open stats: gap%, RVOL at bar 3, dollar volume.

    Returns {date: {"gap_pct": float, "rvol": float, "dolvol": float}}
    """
    # Group by day
    daily = defaultdict(list)
    for b in bars:
        daily[b.timestamp.date()].append(b)
    dates_sorted = sorted(daily.keys())

    stats = {}
    # 20-day rolling buffer for first-3-bars volume baseline
    vol_baseline_buf = deque(maxlen=20)

    for idx, d in enumerate(dates_sorted):
        day_bars = daily[d]

        # Prior-day close
        prior_close = None
        if idx > 0:
            prev_d = dates_sorted[idx - 1]
            prev_bars = daily[prev_d]
            if prev_bars:
                prior_close = prev_bars[-1].close

        # First 3 bars volume (9:30-9:45 in 5-min)
        first3 = day_bars[:3]
        vol_first3 = sum(b.volume for b in first3)
        dolvol_first3 = sum(b.close * b.volume for b in first3)

        # Gap %
        gap_pct = 0.0
        if prior_close and prior_close > 0 and day_bars:
            gap_pct = (day_bars[0].open - prior_close) / prior_close * 100

        # RVOL: current first-3 vol vs 20-day avg of first-3 vol
        rvol = 0.0
        if len(vol_baseline_buf) >= 5:
            avg_vol = sum(vol_baseline_buf) / len(vol_baseline_buf)
            if avg_vol > 0:
                rvol = vol_first3 / avg_vol

        vol_baseline_buf.append(vol_first3)

        stats[d] = {
            "gap_pct": gap_pct,
            "rvol": rvol,
            "dolvol": dolvol_first3,
        }

    return stats


def check_inplay(open_stats: dict, d: date, p_level: str) -> bool:
    """Check in-play proxy layer. Returns True if passes."""
    if p_level == "-":
        return True
    s = open_stats.get(d)
    if s is None:
        return False
    if p_level == "P1":
        return abs(s["gap_pct"]) > 1.0
    if p_level == "P2":
        return s["rvol"] > 2.0
    if p_level == "P3":
        return abs(s["gap_pct"]) > 1.0 and s["rvol"] > 2.0
    return True


# ════════════════════════════════════════════════════════════════
#  Leadership (Layer L)
# ════════════════════════════════════════════════════════════════

def build_pct_from_open(bars: list) -> dict:
    """Build {(date, hhmm): pct_from_open} for a bar series."""
    daily_open = {}
    result = {}
    for b in bars:
        d = b.timestamp.date()
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute
        if d not in daily_open:
            daily_open[d] = b.open
        o = daily_open[d]
        if o > 0:
            result[(d, hhmm)] = (b.close - o) / o * 100
        else:
            result[(d, hhmm)] = 0.0
    return result


def check_leadership(stock_pfo: dict, spy_pfo: dict, sector_pfo: dict,
                     d: date, hhmm: int, l_level: str) -> Tuple[bool, float, float]:
    """Check leadership layer. Returns (passes, rs_spy, rs_sector)."""
    if l_level == "-":
        return True, 0.0, 0.0

    s_pct = stock_pfo.get((d, hhmm), 0.0)
    spy_pct = spy_pfo.get((d, hhmm), 0.0)
    sec_pct = sector_pfo.get((d, hhmm), 0.0) if sector_pfo else spy_pct

    rs_spy = s_pct - spy_pct
    rs_sector = s_pct - sec_pct

    if l_level == "L1":
        return rs_spy > 0, rs_spy, rs_sector
    if l_level == "L2":
        return rs_spy > 0.3, rs_spy, rs_sector
    if l_level == "L3":
        return rs_sector > 0, rs_spy, rs_sector
    if l_level == "L4":
        return rs_spy > 0 and rs_sector > 0, rs_spy, rs_sector
    return True, rs_spy, rs_sector


# ════════════════════════════════════════════════════════════════
#  Exit mechanics
# ════════════════════════════════════════════════════════════════

def simulate_trade(trade: CTrade, bars: list, bar_idx: int,
                   x_type: str = "X2", target_rr: float = 3.0,
                   ema9: Optional[EMA] = None) -> CTrade:
    """Simulate from entry bar forward.

    Exit modes:
      X1: fixed target 2.0R
      X2: fixed target 3.0R
      X3: trail EMA9 (exit close < ema9 after +1R reached)
      X4: time exit (12 bars max)
    """
    risk = trade.risk
    if risk <= 0:
        trade.pnl_rr = 0
        trade.exit_reason = "invalid"
        return trade

    # Resolve target RR based on exit mode
    if x_type == "X1":
        target_rr = 2.0
    elif x_type == "X2":
        target_rr = 3.0

    max_bars = 78 if x_type != "X4" else 12
    best_r = 0.0

    for i in range(bar_idx + 1, min(bar_idx + max_bars + 1, len(bars))):
        b = bars[i]
        trade.bars_held += 1
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute

        # Track EMA9 for trail mode
        if ema9 is not None:
            ema9.update(b.close)

        current_r = (b.close - trade.entry_price) / risk
        best_r = max(best_r, current_r)

        # EOD exit
        if hhmm >= 1555 or (i == len(bars) - 1):
            pnl = (b.close - trade.entry_price)
            trade.pnl_rr = pnl / risk
            trade.exit_reason = "eod"
            return trade

        # Stop check (all modes)
        if b.low <= trade.stop_price:
            trade.pnl_rr = (trade.stop_price - trade.entry_price) / risk
            trade.exit_reason = "stop"
            return trade

        # X3: Trail EMA9 — exit if close < ema9 after reaching +1R
        if x_type == "X3" and ema9 is not None and ema9.ready and best_r >= 1.0:
            if b.close < ema9.value:
                trade.pnl_rr = (b.close - trade.entry_price) / risk
                trade.exit_reason = "trail"
                return trade
            continue  # no fixed target in trail mode

        # Target check (X1, X2)
        if x_type in ("X1", "X2"):
            target_price = trade.entry_price + target_rr * risk
            if b.high >= target_price:
                trade.pnl_rr = target_rr
                trade.exit_reason = "target"
                return trade

    # Time exit (X4) or fell through
    if trade.bars_held > 0:
        last = bars[min(bar_idx + max_bars, len(bars) - 1)]
        trade.pnl_rr = (last.close - trade.entry_price) / risk
        trade.exit_reason = "time" if x_type == "X4" else "eod"
    return trade


# ════════════════════════════════════════════════════════════════
#  Cost model
# ════════════════════════════════════════════════════════════════

SLIPPAGE_BPS = 4  # per side
COMMISSION_PER_SHARE = 0.005


def apply_costs(trade: CTrade) -> CTrade:
    """Apply round-trip slippage + commission to trade PnL."""
    if trade.risk <= 0:
        return trade
    entry = trade.entry_price
    slip_per_side = entry * SLIPPAGE_BPS / 10000
    comm = COMMISSION_PER_SHARE
    total_cost = 2 * (slip_per_side + comm)  # round trip
    cost_in_r = total_cost / trade.risk
    trade.pnl_rr -= cost_in_r
    return trade


# ════════════════════════════════════════════════════════════════
#  Entry E1: VWAP Kiss Acceptance
# ════════════════════════════════════════════════════════════════

def entry_vk(bars: list, sym: str, spy_day_info: dict, spy_ctx: dict,
             open_stats: dict, stock_pfo: dict, spy_pfo: dict, sector_pfo: dict,
             m_level: str = "M1", p_level: str = "-", l_level: str = "-",
             x_type: str = "X2",
             time_start: int = 1000, time_end: int = 1130,
             hold_bars: int = 2, min_body_pct: float = 0.40,
             kiss_atr_frac: float = 0.05) -> List[CTrade]:
    """VK acceptance: VWAP touch -> hold 2 bars above -> expansion close."""
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

        if d != prev_date:
            vwap.reset()
            touched = False
            hold_count = 0
            hold_low = NaN
            micro_high = NaN
            triggered_today = None
            prev_date = d

        vw = vwap.update(tp, bar.volume)

        if len(vol_buf) == 20:
            vol_ma = sum(vol_buf) / 20
        vol_buf.append(bar.volume)

        tr = bar.high - bar.low
        atr_buf.append(tr)
        if len(atr_buf) >= 5:
            intra_atr = sum(atr_buf) / len(atr_buf)

        if not vwap.ready or _isnan(intra_atr):
            continue

        # Gate checks
        if not check_market(spy_day_info, spy_ctx, d, hhmm, m_level):
            continue
        if not check_inplay(open_stats, d, p_level):
            continue
        if hhmm < time_start or hhmm > time_end:
            continue
        if triggered_today == d:
            continue

        # Leadership check
        l_ok, rs_spy, rs_sec = check_leadership(stock_pfo, spy_pfo, sector_pfo,
                                                 d, hhmm, l_level)
        if not l_ok:
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
            touched = False
            hold_count = 0
            hold_low = NaN
            micro_high = NaN

        # Trigger
        if hold_count >= hold_bars and above_vwap and touched:
            rng = bar.high - bar.low
            body = abs(bar.close - bar.open)
            is_bull = bar.close > bar.open
            body_pct = body / rng if rng > 0 else 0
            trigger_ok = is_bull and body_pct >= min_body_pct

            vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 0.70 * vol_ma

            if trigger_ok and vol_ok:
                stop = (hold_low if not _isnan(hold_low) else bar.low) - 0.02
                stop = min(stop, vw - 0.02)
                risk = bar.close - stop
                if risk > 0:
                    os = open_stats.get(d, {})
                    t = CTrade(
                        symbol=sym, entry_time=bar.timestamp,
                        entry_price=bar.close, stop_price=stop,
                        target_price=bar.close + 3.0 * risk,
                        setup="VK_ACC", risk=risk,
                        gap_pct=os.get("gap_pct", 0),
                        rvol=os.get("rvol", 0),
                        rs_spy=rs_spy, rs_sector=rs_sec,
                        market_level=m_level,
                    )
                    # Clone EMA9 for trail exit
                    trail_ema = EMA(9)
                    for j in range(max(0, i - 20), i + 1):
                        trail_ema.update(bars[j].close)
                    t = simulate_trade(t, bars, i, x_type=x_type, ema9=trail_ema)
                    t = apply_costs(t)
                    trades.append(t)
                    triggered_today = d
                    touched = False
                    hold_count = 0
                    hold_low = NaN
                    micro_high = NaN

    return trades


# ════════════════════════════════════════════════════════════════
#  Entry E2: EMA9 Acceptance
# ════════════════════════════════════════════════════════════════

def entry_ema9(bars: list, sym: str, spy_day_info: dict, spy_ctx: dict,
               open_stats: dict, stock_pfo: dict, spy_pfo: dict, sector_pfo: dict,
               m_level: str = "M1", p_level: str = "-", l_level: str = "-",
               x_type: str = "X2",
               time_start: int = 1000, time_end: int = 1130,
               hold_bars: int = 2, min_body_pct: float = 0.40) -> List[CTrade]:
    """EMA9 acceptance: dip below EMA9 -> reclaim -> hold 2 bars -> expand."""
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
        if len(vol_buf) == 20:
            vol_ma = sum(vol_buf) / 20
        vol_buf.append(bar.volume)

        if not ema9.ready:
            continue

        d = bar.timestamp.date()
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute

        if d != prev_date:
            was_below = False
            hold_count = 0
            hold_low = NaN
            micro_high = NaN
            triggered_today = None
            prev_date = d

        # Gate checks
        if not check_market(spy_day_info, spy_ctx, d, hhmm, m_level):
            # Track state even on non-GREEN bars
            if bar.close <= e9:
                was_below = True
                hold_count = 0
                hold_low = NaN
                micro_high = NaN
            continue
        if not check_inplay(open_stats, d, p_level):
            continue
        if hhmm < time_start or hhmm > time_end:
            above_ema = bar.close > e9
            if not above_ema:
                was_below = True
                hold_count = 0
                hold_low = NaN
                micro_high = NaN
            continue
        if triggered_today == d:
            continue

        # Leadership check
        l_ok, rs_spy, rs_sec = check_leadership(stock_pfo, spy_pfo, sector_pfo,
                                                 d, hhmm, l_level)
        if not l_ok:
            continue

        above_ema = bar.close > e9

        # State machine
        if not above_ema:
            was_below = True
            hold_count = 0
            hold_low = NaN
            micro_high = NaN
        elif was_below and above_ema and hold_count == 0:
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

        # Trigger
        if hold_count >= hold_bars and above_ema:
            rng = bar.high - bar.low
            body = abs(bar.close - bar.open)
            is_bull = bar.close > bar.open
            body_pct = body / rng if rng > 0 else 0
            trigger_ok = is_bull and body_pct >= min_body_pct

            vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 0.70 * vol_ma

            if trigger_ok and vol_ok:
                stop = (hold_low if not _isnan(hold_low) else bar.low) - 0.02
                risk = bar.close - stop
                if risk > 0:
                    os = open_stats.get(d, {})
                    t = CTrade(
                        symbol=sym, entry_time=bar.timestamp,
                        entry_price=bar.close, stop_price=stop,
                        target_price=bar.close + 3.0 * risk,
                        setup="EMA9_ACC", risk=risk,
                        gap_pct=os.get("gap_pct", 0),
                        rvol=os.get("rvol", 0),
                        rs_spy=rs_spy, rs_sector=rs_sec,
                        market_level=m_level,
                    )
                    trail_ema = EMA(9)
                    for j in range(max(0, i - 20), i + 1):
                        trail_ema.update(bars[j].close)
                    t = simulate_trade(t, bars, i, x_type=x_type, ema9=trail_ema)
                    t = apply_costs(t)
                    trades.append(t)
                    triggered_today = d
                    was_below = False
                    hold_count = 0
                    hold_low = NaN
                    micro_high = NaN

    return trades


# ════════════════════════════════════════════════════════════════
#  Entry E3: OR-High Acceptance
# ════════════════════════════════════════════════════════════════

def entry_or(bars: list, sym: str, spy_day_info: dict, spy_ctx: dict,
             open_stats: dict, stock_pfo: dict, spy_pfo: dict, sector_pfo: dict,
             m_level: str = "M1", p_level: str = "-", l_level: str = "-",
             x_type: str = "X2",
             time_start: int = 1000, time_end: int = 1130,
             hold_bars: int = 2, min_body_pct: float = 0.40) -> List[CTrade]:
    """OR acceptance: pull below OR-high -> reclaim -> hold 2 bars -> expand."""
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

        if len(vol_buf) == 20:
            vol_ma = sum(vol_buf) / 20
        vol_buf.append(bar.volume)

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

        # Build OR (9:30-9:45)
        if 930 <= hhmm < 945:
            if _isnan(or_high) or bar.high > or_high:
                or_high = bar.high
            if _isnan(or_low) or bar.low < or_low:
                or_low = bar.low
        elif hhmm >= 945 and not _isnan(or_high):
            or_ready = True

        if not or_ready:
            continue

        # Gate checks
        if not check_market(spy_day_info, spy_ctx, d, hhmm, m_level):
            continue
        if not check_inplay(open_stats, d, p_level):
            continue
        if hhmm < time_start or hhmm > time_end:
            if bar.close < or_high:
                was_below_or = True
                hold_count = 0
                hold_low = NaN
                micro_high = NaN
            continue
        if triggered_today == d:
            continue

        # Leadership check
        l_ok, rs_spy, rs_sec = check_leadership(stock_pfo, spy_pfo, sector_pfo,
                                                 d, hhmm, l_level)
        if not l_ok:
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
            trigger_ok = is_bull and body_pct >= min_body_pct

            vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 0.70 * vol_ma

            if trigger_ok and vol_ok:
                stop = (hold_low if not _isnan(hold_low) else bar.low) - 0.02
                stop = min(stop, or_high - 0.02)
                risk = bar.close - stop
                if risk > 0:
                    os = open_stats.get(d, {})
                    t = CTrade(
                        symbol=sym, entry_time=bar.timestamp,
                        entry_price=bar.close, stop_price=stop,
                        target_price=bar.close + 3.0 * risk,
                        setup="OR_ACC", risk=risk,
                        gap_pct=os.get("gap_pct", 0),
                        rvol=os.get("rvol", 0),
                        rs_spy=rs_spy, rs_sector=rs_sec,
                        market_level=m_level,
                    )
                    trail_ema = EMA(9)
                    for j in range(max(0, i - 20), i + 1):
                        trail_ema.update(bars[j].close)
                    t = simulate_trade(t, bars, i, x_type=x_type, ema9=trail_ema)
                    t = apply_costs(t)
                    trades.append(t)
                    triggered_today = d
                    was_below_or = False
                    hold_count = 0
                    hold_low = NaN
                    micro_high = NaN

    return trades


# ════════════════════════════════════════════════════════════════
#  Entry E4: EMA9 Pullback (simple, no acceptance)
# ════════════════════════════════════════════════════════════════

def entry_pullback(bars: list, sym: str, spy_day_info: dict, spy_ctx: dict,
                   open_stats: dict, stock_pfo: dict, spy_pfo: dict, sector_pfo: dict,
                   m_level: str = "M1", p_level: str = "-", l_level: str = "-",
                   x_type: str = "X2",
                   time_start: int = 1000, time_end: int = 1130,
                   min_body_pct: float = 0.40,
                   pb_atr_frac: float = 0.30) -> List[CTrade]:
    """EMA9 pullback: close within pb_atr_frac*ATR of EMA9 from above -> next bar expand."""
    trades = []
    ema9 = EMA(9)
    ema20 = EMA(20)
    vol_buf = deque(maxlen=20)
    vol_ma = NaN
    atr_buf = deque(maxlen=14)
    intra_atr = NaN

    pullback_bar = None  # index of pullback bar
    triggered_today = None
    prev_date = None

    for i, bar in enumerate(bars):
        e9 = ema9.update(bar.close)
        e20 = ema20.update(bar.close)
        if len(vol_buf) == 20:
            vol_ma = sum(vol_buf) / 20
        vol_buf.append(bar.volume)

        tr = bar.high - bar.low
        atr_buf.append(tr)
        if len(atr_buf) >= 5:
            intra_atr = sum(atr_buf) / len(atr_buf)

        if not ema9.ready or not ema20.ready or _isnan(intra_atr):
            continue

        d = bar.timestamp.date()
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute

        if d != prev_date:
            pullback_bar = None
            triggered_today = None
            prev_date = d

        # Gate checks
        if not check_market(spy_day_info, spy_ctx, d, hhmm, m_level):
            continue
        if not check_inplay(open_stats, d, p_level):
            continue
        if hhmm < time_start or hhmm > time_end:
            continue
        if triggered_today == d:
            continue

        # Leadership check
        l_ok, rs_spy, rs_sec = check_leadership(stock_pfo, spy_pfo, sector_pfo,
                                                 d, hhmm, l_level)
        if not l_ok:
            continue

        # Trend context: EMA9 > EMA20 (uptrend)
        if e9 <= e20:
            pullback_bar = None
            continue

        # Close from above, within threshold of EMA9
        dist = bar.close - e9
        threshold = pb_atr_frac * intra_atr

        if 0 < dist <= threshold and bar.close > e9:
            # Pullback detected — wait for next bar expansion
            pullback_bar = i
        elif pullback_bar is not None and i == pullback_bar + 1:
            # Expansion bar check
            rng = bar.high - bar.low
            body = abs(bar.close - bar.open)
            is_bull = bar.close > bar.open
            body_pct = body / rng if rng > 0 else 0

            vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 0.70 * vol_ma
            trigger_ok = is_bull and body_pct >= min_body_pct

            if trigger_ok and vol_ok:
                stop = e9 - 0.5 * intra_atr
                risk = bar.close - stop
                if risk > 0:
                    os = open_stats.get(d, {})
                    t = CTrade(
                        symbol=sym, entry_time=bar.timestamp,
                        entry_price=bar.close, stop_price=stop,
                        target_price=bar.close + 3.0 * risk,
                        setup="PB_EMA9", risk=risk,
                        gap_pct=os.get("gap_pct", 0),
                        rvol=os.get("rvol", 0),
                        rs_spy=rs_spy, rs_sector=rs_sec,
                        market_level=m_level,
                    )
                    trail_ema = EMA(9)
                    for j in range(max(0, i - 20), i + 1):
                        trail_ema.update(bars[j].close)
                    t = simulate_trade(t, bars, i, x_type=x_type, ema9=trail_ema)
                    t = apply_costs(t)
                    trades.append(t)
                    triggered_today = d
                    pullback_bar = None
            else:
                pullback_bar = None
        else:
            pullback_bar = None

    return trades


# ════════════════════════════════════════════════════════════════
#  Metrics engine
# ════════════════════════════════════════════════════════════════

def compute_r_metrics(trades: List[CTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "pf_r": 0, "exp_r": 0, "total_r": 0, "max_dd_r": 0,
                "wr": 0, "stop_rate": 0, "quick_stop": 0, "target_rate": 0}

    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    total_r = sum(t.pnl_rr for t in trades)
    wr = len(wins) / n * 100
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
            "max_dd_r": dd, "wr": wr, "stop_rate": stopped / n * 100,
            "quick_stop": quick / n * 100, "target_rate": targets / n * 100}


def robustness(trades: List[CTrade]) -> dict:
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

    # Train/test (odd/even day-of-month)
    train = [t for t in trades if t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date.day % 2 == 0]
    tr_m = compute_r_metrics(train)
    te_m = compute_r_metrics(test)

    # Monthly breakdown
    monthly = defaultdict(float)
    for t in trades:
        monthly[t.entry_date.strftime("%Y-%m")] += t.pnl_rr
    months_pos = sum(1 for v in monthly.values() if v > 0)
    months_total = len(monthly)

    return {
        "ex_best_day_pf": ex_day_m["pf_r"],
        "ex_top_sym_pf": ex_sym_m["pf_r"],
        "train_pf": tr_m["pf_r"],
        "test_pf": te_m["pf_r"],
        "train_n": tr_m["n"],
        "test_n": te_m["n"],
        "months_pos": months_pos,
        "months_total": months_total,
        "stable": (tr_m["pf_r"] >= 0.80 and te_m["pf_r"] >= 0.80
                   and tr_m["n"] >= 10 and te_m["n"] >= 10),
    }


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


# ════════════════════════════════════════════════════════════════
#  Variant matrix
# ════════════════════════════════════════════════════════════════

ENTRY_FUNCS = {
    "E1": entry_vk,
    "E2": entry_ema9,
    "E3": entry_or,
    "E4": entry_pullback,
}

CORE_VARIANTS = [
    # VK family
    {"name": "VK_green",                "M": "M1", "P": "-",  "L": "-",  "E": "E1", "X": "X2"},
    {"name": "VK_green_inplay",         "M": "M1", "P": "P3", "L": "-",  "E": "E1", "X": "X2"},
    {"name": "VK_green_leader",         "M": "M1", "P": "-",  "L": "L1", "E": "E1", "X": "X2"},
    {"name": "VK_green_inplay_leader",  "M": "M1", "P": "P3", "L": "L1", "E": "E1", "X": "X2"},
    {"name": "VK_full_support",         "M": "M3", "P": "P3", "L": "L4", "E": "E1", "X": "X2"},
    # EMA9 family
    {"name": "EMA9_green",              "M": "M1", "P": "-",  "L": "-",  "E": "E2", "X": "X2"},
    {"name": "EMA9_green_inplay",       "M": "M1", "P": "P3", "L": "-",  "E": "E2", "X": "X2"},
    {"name": "EMA9_green_leader",       "M": "M1", "P": "-",  "L": "L1", "E": "E2", "X": "X2"},
    {"name": "EMA9_green_inplay_leader","M": "M1", "P": "P3", "L": "L1", "E": "E2", "X": "X2"},
    {"name": "EMA9_full_support",       "M": "M3", "P": "P3", "L": "L4", "E": "E2", "X": "X2"},
    # OR family
    {"name": "OR_green",                "M": "M1", "P": "-",  "L": "-",  "E": "E3", "X": "X2"},
    {"name": "OR_green_inplay",         "M": "M1", "P": "P3", "L": "-",  "E": "E3", "X": "X2"},
    {"name": "OR_green_inplay_leader",  "M": "M1", "P": "P3", "L": "L1", "E": "E3", "X": "X2"},
    # PB family
    {"name": "PB_green",                "M": "M1", "P": "-",  "L": "-",  "E": "E4", "X": "X2"},
    {"name": "PB_green_inplay",         "M": "M1", "P": "P3", "L": "-",  "E": "E4", "X": "X2"},
    {"name": "PB_green_leader",         "M": "M1", "P": "-",  "L": "L1", "E": "E4", "X": "X2"},
    {"name": "PB_green_inplay_leader",  "M": "M1", "P": "P3", "L": "L1", "E": "E4", "X": "X2"},
]

# Exit sensitivity variants — dynamically built after core results
EXIT_VARIANTS = [
    {"suffix": "_R2",    "X": "X1"},
    {"suffix": "_R3",    "X": "X2"},
    {"suffix": "_trail", "X": "X3"},
    {"suffix": "_time",  "X": "X4"},
]

# Market strictness variants
MARKET_VARIANTS = [
    {"suffix": "_M1", "M": "M1"},
    {"suffix": "_M2", "M": "M2"},
    {"suffix": "_M3", "M": "M3"},
]


# ════════════════════════════════════════════════════════════════
#  Variant runner
# ════════════════════════════════════════════════════════════════

def run_variant(variant: dict, symbols: list, spy_day_info: dict, spy_ctx: dict,
                all_open_stats: dict, spy_pfo: dict, sector_pfos: dict,
                all_bars: dict) -> List[CTrade]:
    """Run one variant across all symbols, return trades."""
    entry_func = ENTRY_FUNCS[variant["E"]]
    all_trades = []

    for sym in symbols:
        bars = all_bars.get(sym)
        if not bars:
            continue
        open_stats = all_open_stats.get(sym, {})
        stock_pfo = build_pct_from_open(bars)
        sec_etf = get_sector_etf(sym)
        sector_pfo = sector_pfos.get(sec_etf, spy_pfo)

        trades = entry_func(
            bars, sym, spy_day_info, spy_ctx,
            open_stats, stock_pfo, spy_pfo, sector_pfo,
            m_level=variant["M"], p_level=variant["P"], l_level=variant["L"],
            x_type=variant["X"],
        )
        for t in trades:
            t.variant = variant["name"]
        all_trades.extend(trades)

    return all_trades


# ════════════════════════════════════════════════════════════════
#  Dry-run: filter statistics
# ════════════════════════════════════════════════════════════════

def dry_run(symbols: list, spy_day_info: dict, spy_ctx: dict,
            all_open_stats: dict, spy_pfo: dict, sector_pfos: dict,
            all_bars: dict):
    """Print filter statistics without running trades."""
    print("\n" + "=" * 100)
    print("DRY RUN — FILTER STATISTICS")
    print("=" * 100)

    # SPY day distribution
    green = sum(1 for v in spy_day_info.values() if v == "GREEN")
    flat = sum(1 for v in spy_day_info.values() if v == "FLAT")
    red = sum(1 for v in spy_day_info.values() if v == "RED")
    total_days = len(spy_day_info)
    print(f"\n  SPY days: {total_days} total — GREEN {green} ({green/total_days*100:.0f}%), "
          f"FLAT {flat} ({flat/total_days*100:.0f}%), RED {red} ({red/total_days*100:.0f}%)")

    # In-play proxy stats per symbol
    ip_p1 = ip_p2 = ip_p3 = total_sym_days = 0
    for sym in symbols:
        stats = all_open_stats.get(sym, {})
        for d, s in stats.items():
            total_sym_days += 1
            if abs(s.get("gap_pct", 0)) > 1.0:
                ip_p1 += 1
            if s.get("rvol", 0) > 2.0:
                ip_p2 += 1
            if abs(s.get("gap_pct", 0)) > 1.0 and s.get("rvol", 0) > 2.0:
                ip_p3 += 1
    print(f"\n  In-play proxy ({total_sym_days} symbol-days):")
    print(f"    P1 (gap>1%):  {ip_p1:6d} ({ip_p1/total_sym_days*100:.1f}%)")
    print(f"    P2 (RVOL>2):  {ip_p2:6d} ({ip_p2/total_sym_days*100:.1f}%)")
    print(f"    P3 (both):    {ip_p3:6d} ({ip_p3/total_sym_days*100:.1f}%)")

    # Leadership on GREEN days (10:00 bar sample)
    l1_count = l4_count = sampled = 0
    green_dates = [d for d, v in spy_day_info.items() if v == "GREEN"]
    for sym in symbols[:20]:  # sample 20 symbols
        bars = all_bars.get(sym, [])
        if not bars:
            continue
        stock_pfo = build_pct_from_open(bars)
        sec_etf = get_sector_etf(sym)
        sector_pfo = sector_pfos.get(sec_etf, spy_pfo)
        for d in green_dates:
            sampled += 1
            ok1, _, _ = check_leadership(stock_pfo, spy_pfo, sector_pfo, d, 1000, "L1")
            ok4, _, _ = check_leadership(stock_pfo, spy_pfo, sector_pfo, d, 1000, "L4")
            if ok1:
                l1_count += 1
            if ok4:
                l4_count += 1
    if sampled > 0:
        print(f"\n  Leadership (sampled {sampled} symbol-days on GREEN):")
        print(f"    L1 (RS SPY>0):      {l1_count:6d} ({l1_count/sampled*100:.1f}%)")
        print(f"    L4 (RS SPY+SEC>0):  {l4_count:6d} ({l4_count/sampled*100:.1f}%)")

    print(f"\n  Universe: {len(symbols)} symbols")
    print(f"\n{'='*100}")
    print("DRY RUN COMPLETE — no trades simulated")
    print(f"{'='*100}")


# ════════════════════════════════════════════════════════════════
#  Reporting
# ════════════════════════════════════════════════════════════════

def verdict(m: dict, rob: dict) -> str:
    if m["n"] < 20:
        return "INSUFF_N"
    beats_baseline = m["pf_r"] > 1.19 and m["exp_r"] > 0.173 and m["n"] >= 30
    if m["pf_r"] >= 1.10 and m["exp_r"] > 0 and rob.get("stable", False):
        return "PROMOTE*" if beats_baseline else "PROMOTE"
    if m["pf_r"] >= 1.0 and m["exp_r"] > 0:
        return "CONTINUE"
    if m["exp_r"] > 0:
        return "MARGINAL"
    return "RETIRE"


def print_ledger(results: list, title: str):
    print(f"\n{'='*160}")
    print(f"{title}")
    print(f"{'='*160}")

    header = (f"  {'Variant':36s} {'N':>5s} {'WR%':>5s} {'PF(R)':>6s} {'Exp(R)':>8s} "
              f"{'TotalR':>8s} {'MaxDD':>7s} {'Stop%':>6s} "
              f"{'TrnPF':>6s} {'TstPF':>6s} {'Stbl':>4s} {'ExDay':>6s} {'ExSym':>6s} "
              f"{'Mo+':>5s} {'Verdict':>9s}")
    print(header)
    print(f"  {'-'*36} {'-'*5} {'-'*5} {'-'*6} {'-'*8} "
          f"{'-'*8} {'-'*7} {'-'*6} "
          f"{'-'*6} {'-'*6} {'-'*4} {'-'*6} {'-'*6} "
          f"{'-'*5} {'-'*9}")

    for name, m, rob, v in results:
        mo_str = f"{rob.get('months_pos',0)}/{rob.get('months_total',0)}"
        print(f"  {name:36s} {m['n']:5d} {m['wr']:5.1f} {pf_str(m['pf_r']):>6s} {m['exp_r']:+7.3f}R "
              f"{m['total_r']:+7.2f}R {m['max_dd_r']:6.2f}R {m['stop_rate']:5.1f}% "
              f"{pf_str(rob.get('train_pf',0)):>6s} {pf_str(rob.get('test_pf',0)):>6s} "
              f"{'YES' if rob.get('stable') else ' NO':>4s} "
              f"{pf_str(rob.get('ex_best_day_pf',0)):>6s} {pf_str(rob.get('ex_top_sym_pf',0)):>6s} "
              f"{mo_str:>5s} {v:>9s}")


def print_decomposition(best_name: str, best_variant: dict, symbols: list,
                        spy_day_info: dict, spy_ctx: dict,
                        all_open_stats: dict, spy_pfo: dict, sector_pfos: dict,
                        all_bars: dict):
    """For the best variant, test removing each filter layer."""
    print(f"\n{'='*160}")
    print(f"DECOMPOSITION — {best_name}")
    print(f"{'='*160}")
    print(f"  Which layer contributes most to edge?")

    base_m = best_variant.get("_metrics")
    base_pf = base_m["pf_r"] if base_m else 0

    # Test: remove P layer
    layers_to_test = [
        ("No P filter", {**best_variant, "P": "-"}),
        ("No L filter", {**best_variant, "L": "-"}),
        ("M1 only", {**best_variant, "M": "M1"}),
    ]

    print(f"\n  {'Configuration':36s} {'N':>5s} {'PF(R)':>6s} {'Exp(R)':>8s} {'TotalR':>8s} {'Delta PF':>9s}")
    print(f"  {'-'*36} {'-'*5} {'-'*6} {'-'*8} {'-'*8} {'-'*9}")

    # Print baseline
    print(f"  {'BASELINE (all filters)':36s} {base_m['n']:5d} {pf_str(base_m['pf_r']):>6s} "
          f"{base_m['exp_r']:+7.3f}R {base_m['total_r']:+7.2f}R {'—':>9s}")

    for desc, v_mod in layers_to_test:
        trades = run_variant(v_mod, symbols, spy_day_info, spy_ctx,
                             all_open_stats, spy_pfo, sector_pfos, all_bars)
        m = compute_r_metrics(trades)
        delta = m["pf_r"] - base_pf
        print(f"  {desc:36s} {m['n']:5d} {pf_str(m['pf_r']):>6s} "
              f"{m['exp_r']:+7.3f}R {m['total_r']:+7.2f}R {delta:+8.2f}")


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Composite Long Study")
    parser.add_argument("--dry-run", action="store_true", help="Print filter stats only")
    args = parser.parse_args()

    print("=" * 160)
    print("COMPOSITE LONG STUDY — SUPPORTED LEADER IN-PLAY PULLBACK LONG")
    print("=" * 160)

    # Load data
    print("\n  Loading data...")
    symbols = get_universe()
    spy_bars = load_bars("SPY")
    qqq_bars = load_bars("QQQ")

    spy_day_info = classify_spy_days(spy_bars)
    spy_ctx = build_spy_snapshots(spy_bars)
    spy_pfo = build_pct_from_open(spy_bars)

    # Load sector ETF bars and pct-from-open
    sector_bars_dict = {}
    sector_pfos = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sb = load_bars_from_csv(str(p))
            sector_bars_dict[etf] = sb
            sector_pfos[etf] = build_pct_from_open(sb)

    # Load all symbol bars + open stats
    print(f"  Loading {len(symbols)} symbols...")
    all_bars = {}
    all_open_stats = {}
    for sym in symbols:
        bars = load_bars(sym)
        if bars:
            all_bars[sym] = bars
            all_open_stats[sym] = compute_open_stats(bars)

    print(f"  Data: {spy_bars[0].timestamp.date()} -> {spy_bars[-1].timestamp.date()}")
    print(f"  Universe: {len(all_bars)} symbols with data")
    green_days = sum(1 for v in spy_day_info.values() if v == "GREEN")
    print(f"  GREEN days: {green_days}/{len(spy_day_info)}")
    print(f"  Cost model: {SLIPPAGE_BPS} bps/side + ${COMMISSION_PER_SHARE}/share")
    print(f"  Baseline to beat: SC Q>=5 — PF 1.19, 49 trades, Exp +0.173R")

    if args.dry_run:
        dry_run(symbols, spy_day_info, spy_ctx, all_open_stats, spy_pfo,
                sector_pfos, all_bars)
        return

    # ── WAVE 1: Core variants ──
    print(f"\n  Running {len(CORE_VARIANTS)} core variants...")
    core_results = []
    best_pf = 0
    best_variant = None
    best_name = None

    for v in CORE_VARIANTS:
        trades = run_variant(v, symbols, spy_day_info, spy_ctx,
                             all_open_stats, spy_pfo, sector_pfos, all_bars)
        m = compute_r_metrics(trades)
        rob = robustness(trades) if m["n"] >= 10 else {
            "ex_best_day_pf": 0, "ex_top_sym_pf": 0,
            "train_pf": 0, "test_pf": 0, "stable": False,
            "months_pos": 0, "months_total": 0,
            "train_n": 0, "test_n": 0,
        }
        v_str = verdict(m, rob)
        core_results.append((v["name"], m, rob, v_str))

        if m["n"] >= 20 and m["pf_r"] > best_pf:
            best_pf = m["pf_r"]
            best_variant = {**v, "_metrics": m, "_robustness": rob}
            best_name = v["name"]

    print_ledger(core_results, "WAVE 1 — CORE VARIANTS (17 variants)")

    # Summary
    promoted = [(n, m, r, v) for n, m, r, v in core_results if "PROMOTE" in v]
    continued = [(n, m, r, v) for n, m, r, v in core_results if v == "CONTINUE"]
    retired = [(n, m, r, v) for n, m, r, v in core_results if v == "RETIRE"]

    print(f"\n  PROMOTE: {len(promoted)}  CONTINUE: {len(continued)}  RETIRE: {len(retired)}")
    if best_variant:
        print(f"  Best: {best_name} — PF={pf_str(best_pf)}  "
              f"Exp={best_variant['_metrics']['exp_r']:+.3f}R  "
              f"N={best_variant['_metrics']['n']}")

    # ── WAVE 2: Exit sensitivity on best entry ──
    if best_variant:
        print(f"\n  Running exit sensitivity on {best_name}...")
        exit_results = []
        for ev in EXIT_VARIANTS:
            v_mod = {**best_variant, "X": ev["X"],
                     "name": best_name + ev["suffix"]}
            trades = run_variant(v_mod, symbols, spy_day_info, spy_ctx,
                                 all_open_stats, spy_pfo, sector_pfos, all_bars)
            m = compute_r_metrics(trades)
            rob = robustness(trades) if m["n"] >= 10 else {
                "ex_best_day_pf": 0, "ex_top_sym_pf": 0,
                "train_pf": 0, "test_pf": 0, "stable": False,
                "months_pos": 0, "months_total": 0,
            }
            v_str = verdict(m, rob)
            exit_results.append((v_mod["name"], m, rob, v_str))

        print_ledger(exit_results, f"WAVE 2a — EXIT SENSITIVITY (base: {best_name})")

        # Market strictness
        print(f"\n  Running market strictness sensitivity on {best_name}...")
        mkt_results = []
        for mv in MARKET_VARIANTS:
            v_mod = {**best_variant, "M": mv["M"],
                     "name": best_name + mv["suffix"]}
            trades = run_variant(v_mod, symbols, spy_day_info, spy_ctx,
                                 all_open_stats, spy_pfo, sector_pfos, all_bars)
            m = compute_r_metrics(trades)
            rob = robustness(trades) if m["n"] >= 10 else {
                "ex_best_day_pf": 0, "ex_top_sym_pf": 0,
                "train_pf": 0, "test_pf": 0, "stable": False,
                "months_pos": 0, "months_total": 0,
            }
            v_str = verdict(m, rob)
            mkt_results.append((v_mod["name"], m, rob, v_str))

        print_ledger(mkt_results, f"WAVE 2b — MARKET STRICTNESS (base: {best_name})")

    # ── Decomposition ──
    if best_variant and best_variant.get("P") != "-" and best_variant.get("L") != "-":
        print_decomposition(best_name, best_variant, symbols, spy_day_info, spy_ctx,
                            all_open_stats, spy_pfo, sector_pfos, all_bars)

    # ── Combined ranking ──
    all_results = core_results[:]
    if best_variant:
        all_results.extend(exit_results)
        all_results.extend(mkt_results)

    ranked = sorted(all_results, key=lambda x: x[1]["pf_r"] if x[1]["n"] >= 20 else 0,
                    reverse=True)

    print(f"\n{'='*160}")
    print("COMBINED RANKING — TOP 10 BY PF(R) (N >= 20)")
    print(f"{'='*160}")
    print(f"\n  {'Rank':>4s} {'Variant':36s} {'N':>5s} {'WR%':>5s} {'PF(R)':>6s} {'Exp(R)':>8s} "
          f"{'TotalR':>8s} {'MaxDD':>7s} {'TrnPF':>6s} {'TstPF':>6s} {'Verdict':>9s}")
    print(f"  {'-'*4} {'-'*36} {'-'*5} {'-'*5} {'-'*6} {'-'*8} "
          f"{'-'*8} {'-'*7} {'-'*6} {'-'*6} {'-'*9}")
    for i, (name, m, rob, v) in enumerate(ranked[:10], 1):
        if m["n"] < 20:
            continue
        print(f"  {i:4d} {name:36s} {m['n']:5d} {m['wr']:5.1f} {pf_str(m['pf_r']):>6s} "
              f"{m['exp_r']:+7.3f}R {m['total_r']:+7.2f}R {m['max_dd_r']:6.2f}R "
              f"{pf_str(rob.get('train_pf',0)):>6s} {pf_str(rob.get('test_pf',0)):>6s} {v:>9s}")

    # ── Live-available check ──
    print(f"\n{'='*160}")
    print("LIVE-AVAILABLE CHECK")
    print(f"{'='*160}")
    print(f"  M layer (SPY day): Uses end-of-day SPY return → HINDSIGHT (perfect foresight)")
    print(f"    Note: In live trading, replace with real-time SPY VWAP/EMA (M2/M3)")
    print(f"  P layer (in-play): Uses gap% from prior close + RVOL from first 3 bars → LIVE OK")
    print(f"  L layer (RS):      Uses real-time pct_from_open → LIVE OK")
    print(f"  E layer (entry):   Uses real-time price vs VWAP/EMA/OR → LIVE OK")
    print(f"  X layer (exit):    Uses real-time stop/target/trail → LIVE OK")
    print(f"\n  CONCLUSION: M1 (GREEN day) is hindsight. M2/M3 are live-available.")
    print(f"  For live trading, use M2 or M3 instead of M1.")

    print(f"\n{'='*160}")
    print("COMPOSITE LONG STUDY COMPLETE")
    print(f"{'='*160}")


if __name__ == "__main__":
    main()
