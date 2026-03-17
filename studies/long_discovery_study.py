#!/usr/bin/env python3
"""
Long-side discovery study — 5 hypothesis families.
Standalone scanner: reads bar CSVs, computes indicators, detects setups,
simulates trades, reports R-based metrics.

Does NOT use engine.py — all detection logic is self-contained.
"""

import csv, math, sys, os, json
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent.parent / "data"
NaN = float("nan")


# ─── Bar + Indicator Infrastructure ────────────────────────────────

@dataclass
class Bar:
    timestamp: datetime
    open: float; high: float; low: float; close: float; volume: float
    time_hhmm: int = 0
    date_int: int = 0
    # indicators (filled by compute_indicators)
    ema9: float = NaN; ema20: float = NaN; vwap: float = NaN
    atr: float = NaN; vol_ma: float = NaN
    day_open: float = NaN
    or_high: float = NaN; or_low: float = NaN
    session_high: float = NaN; session_low: float = NaN
    # RS fields (filled externally)
    rs_vs_spy: float = NaN; rs_vs_sector: float = NaN
    spy_pct: float = NaN


def load_bars(symbol: str) -> List[Bar]:
    path = DATA_DIR / f"{symbol}_5min.csv"
    if not path.exists():
        return []
    bars = []
    with open(path) as f:
        for row in csv.DictReader(f):
            norm = {k.strip().lower(): v.strip() for k, v in row.items()}
            try:
                dt = datetime.strptime(norm["datetime"], "%Y-%m-%d %H:%M:%S")
                dt = dt.replace(tzinfo=EASTERN)
                b = Bar(timestamp=dt,
                        open=float(norm["open"]), high=float(norm["high"]),
                        low=float(norm["low"]), close=float(norm["close"]),
                        volume=float(norm["volume"]))
                b.time_hhmm = dt.hour * 100 + dt.minute
                b.date_int = dt.year * 10000 + dt.month * 100 + dt.day
                bars.append(b)
            except (ValueError, KeyError):
                continue
    return bars


def compute_indicators(bars: List[Bar]):
    """Compute EMA9, EMA20, VWAP, ATR, vol_ma in-place."""
    e9 = e20 = None
    atr_vals = []
    vol_vals = []
    prev_close = NaN
    # VWAP accumulators (reset daily)
    vwap_cum_pv = vwap_cum_v = 0.0
    current_date = None
    day_open = NaN
    or_high = NaN; or_low = NaN
    session_high = NaN; session_low = NaN

    for i, b in enumerate(bars):
        # Day reset
        if b.date_int != current_date:
            current_date = b.date_int
            vwap_cum_pv = vwap_cum_v = 0.0
            day_open = b.open
            or_high = NaN; or_low = NaN
            session_high = b.high; session_low = b.low
        else:
            session_high = max(session_high, b.high)
            session_low = min(session_low, b.low)

        b.day_open = day_open
        b.session_high = session_high
        b.session_low = session_low

        # OR (9:30-9:45)
        if 930 <= b.time_hhmm <= 940:
            if math.isnan(or_high):
                or_high = b.high; or_low = b.low
            else:
                or_high = max(or_high, b.high)
                or_low = min(or_low, b.low)
        b.or_high = or_high; b.or_low = or_low

        # VWAP
        tp = (b.high + b.low + b.close) / 3.0
        vwap_cum_pv += tp * b.volume
        vwap_cum_v += b.volume
        b.vwap = vwap_cum_pv / vwap_cum_v if vwap_cum_v > 0 else b.close

        # EMA9
        if e9 is None:
            e9 = b.close
        else:
            e9 = b.close * (2/10) + e9 * (8/10)
        b.ema9 = e9

        # EMA20
        if e20 is None:
            e20 = b.close
        else:
            e20 = b.close * (2/21) + e20 * (19/21)
        b.ema20 = e20

        # ATR (14-bar Wilder's)
        if not math.isnan(prev_close):
            tr = max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close))
        else:
            tr = b.high - b.low
        atr_vals.append(tr)
        if len(atr_vals) >= 14:
            if len(atr_vals) == 14:
                b.atr = sum(atr_vals) / 14
            else:
                b.atr = (bars[i-1].atr * 13 + tr) / 14 if not math.isnan(bars[i-1].atr) else sum(atr_vals[-14:]) / 14
        prev_close = b.close

        # Volume MA (20-bar)
        vol_vals.append(b.volume)
        if len(vol_vals) >= 20:
            b.vol_ma = sum(vol_vals[-20:]) / 20
        else:
            b.vol_ma = sum(vol_vals) / len(vol_vals) if vol_vals else 1.0


def compute_rs(stock_bars: List[Bar], spy_bars: List[Bar], sector_bars: Optional[List[Bar]] = None):
    """Compute RS vs SPY (and optionally sector) per bar."""
    # Build timestamp -> pct_from_open maps
    def _pct_map(bars_list):
        m = {}
        day_open = NaN
        current_date = None
        for b in bars_list:
            d = b.date_int if hasattr(b, 'date_int') else b.timestamp.year * 10000 + b.timestamp.month * 100 + b.timestamp.day
            if d != current_date:
                current_date = d
                day_open = b.open
            if day_open > 0:
                m[b.timestamp] = (b.close - day_open) / day_open * 100
        return m

    spy_pct = _pct_map(spy_bars)
    sector_pct = _pct_map(sector_bars) if sector_bars else {}

    for b in stock_bars:
        stock_p = (b.close - b.day_open) / b.day_open * 100 if b.day_open > 0 else 0
        sp = spy_pct.get(b.timestamp, NaN)
        b.spy_pct = sp
        if not math.isnan(sp):
            b.rs_vs_spy = stock_p - sp
        if b.timestamp in sector_pct:
            b.rs_vs_sector = stock_p - sector_pct[b.timestamp]


# ─── Trade Simulation ──────────────────────────────────────────────

@dataclass
class TradeSetup:
    """A candidate trade with entry, stop, target."""
    bar_idx: int
    timestamp: datetime
    entry: float        # entry price (signal bar close)
    stop: float         # stop price
    direction: int = 1  # always 1 for longs
    setup_id: str = ""
    variant: str = ""
    symbol: str = ""
    tags: List[str] = field(default_factory=list)


@dataclass
class TradeResult:
    setup: TradeSetup
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_r: float = 0.0
    pnl_pts: float = 0.0
    bars_held: int = 0


def simulate_trades(bars: List[Bar], setups: List[TradeSetup],
                    slip_bps: float = 0.0004, slip_min: float = 0.02,
                    exit_mode: str = "hybrid", time_stop_bars: int = 20,
                    session_end: int = 1555,
                    target_rr: float = 0.0) -> List[TradeResult]:
    """Simulate trades with slippage, time stop, EMA9 trail, and EOD exit."""
    results = []
    open_trade = None
    open_setup_idx = -1
    setup_queue = sorted(setups, key=lambda s: s.bar_idx)
    sq_ptr = 0

    for i, bar in enumerate(bars):
        # Check exits first
        if open_trade is not None:
            setup = open_trade
            entry_adj = setup._filled_entry
            bars_held = i - setup._entry_idx

            # Day boundary
            entry_date = bars[setup._entry_idx].date_int
            if bar.date_int != entry_date:
                prev = bars[i-1]
                slip_exit = max(slip_min, prev.close * slip_bps)
                pnl_pts = (prev.close - slip_exit - entry_adj)
                risk = entry_adj - setup.stop
                pnl_r = pnl_pts / risk if risk > 0 else 0
                results.append(TradeResult(setup=setup, exit_price=prev.close,
                                           exit_reason="eod", pnl_r=pnl_r,
                                           pnl_pts=pnl_pts, bars_held=bars_held-1))
                open_trade = None
                continue

            # EOD
            if bar.time_hhmm >= session_end:
                slip_exit = max(slip_min, bar.close * slip_bps)
                pnl_pts = (bar.close - slip_exit - entry_adj)
                risk = entry_adj - setup.stop
                pnl_r = pnl_pts / risk if risk > 0 else 0
                results.append(TradeResult(setup=setup, exit_price=bar.close,
                                           exit_reason="eod", pnl_r=pnl_r,
                                           pnl_pts=pnl_pts, bars_held=bars_held))
                open_trade = None
                continue

            # Stop
            if bar.low <= setup.stop:
                slip_exit = max(slip_min, setup.stop * slip_bps)
                pnl_pts = (setup.stop - slip_exit - entry_adj)
                risk = entry_adj - setup.stop
                pnl_r = pnl_pts / risk if risk > 0 else 0
                results.append(TradeResult(setup=setup, exit_price=setup.stop,
                                           exit_reason="stop", pnl_r=pnl_r,
                                           pnl_pts=pnl_pts, bars_held=bars_held))
                open_trade = None
                continue

            # Target R:R exit (if target_rr > 0)
            if target_rr > 0:
                risk = entry_adj - setup.stop
                target_price = entry_adj + target_rr * risk
                if bar.high >= target_price:
                    slip_exit = max(slip_min, target_price * slip_bps)
                    pnl_pts = (target_price - slip_exit - entry_adj)
                    pnl_r = pnl_pts / risk if risk > 0 else 0
                    results.append(TradeResult(setup=setup, exit_price=target_price,
                                               exit_reason="target", pnl_r=pnl_r,
                                               pnl_pts=pnl_pts, bars_held=bars_held))
                    open_trade = None
                    continue

            # Time stop
            if exit_mode in ("time", "hybrid") and bars_held >= time_stop_bars:
                slip_exit = max(slip_min, bar.close * slip_bps)
                pnl_pts = (bar.close - slip_exit - entry_adj)
                risk = entry_adj - setup.stop
                pnl_r = pnl_pts / risk if risk > 0 else 0
                results.append(TradeResult(setup=setup, exit_price=bar.close,
                                           exit_reason="time", pnl_r=pnl_r,
                                           pnl_pts=pnl_pts, bars_held=bars_held))
                open_trade = None
                continue

            # EMA9 trail (after 2 bars)
            if exit_mode in ("ema9_trail", "hybrid") and bars_held >= 2:
                if not math.isnan(bar.ema9) and bar.close < bar.ema9:
                    slip_exit = max(slip_min, bar.close * slip_bps)
                    pnl_pts = (bar.close - slip_exit - entry_adj)
                    risk = entry_adj - setup.stop
                    pnl_r = pnl_pts / risk if risk > 0 else 0
                    results.append(TradeResult(setup=setup, exit_price=bar.close,
                                               exit_reason="ema9trail", pnl_r=pnl_r,
                                               pnl_pts=pnl_pts, bars_held=bars_held))
                    open_trade = None
                    continue

        # Process new entries
        while sq_ptr < len(setup_queue) and setup_queue[sq_ptr].bar_idx <= i:
            s = setup_queue[sq_ptr]
            sq_ptr += 1
            if s.bar_idx == i and open_trade is None:
                slip_entry = max(slip_min, s.entry * slip_bps)
                s._filled_entry = s.entry + slip_entry
                s._entry_idx = i
                risk = s._filled_entry - s.stop
                if risk > 0:
                    open_trade = s

    # Close any remaining
    if open_trade is not None:
        last = bars[-1]
        setup = open_trade
        slip_exit = max(slip_min, last.close * slip_bps)
        pnl_pts = (last.close - slip_exit - setup._filled_entry)
        risk = setup._filled_entry - setup.stop
        pnl_r = pnl_pts / risk if risk > 0 else 0
        results.append(TradeResult(setup=setup, exit_price=last.close,
                                   exit_reason="eod", pnl_r=pnl_r,
                                   pnl_pts=pnl_pts,
                                   bars_held=len(bars)-1-setup._entry_idx))
    return results


# ─── Metrics Computation ───────────────────────────────────────────

def compute_metrics(trades: List[TradeResult], label: str = "") -> Dict:
    if not trades:
        return {"label": label, "trades": 0, "wr": 0, "pf_r": 0, "exp_r": 0,
                "total_r": 0, "max_dd_r": 0, "stop_rate": 0, "quick_stop": 0,
                "train_pf": 0, "test_pf": 0, "train_r": 0, "test_r": 0,
                "ex_best_day_r": 0, "ex_top_sym_r": 0, "n_symbols": 0, "n_days": 0}
    wins = [t for t in trades if t.pnl_r > 0]
    losses = [t for t in trades if t.pnl_r <= 0]
    gross_w = sum(t.pnl_r for t in wins)
    gross_l = abs(sum(t.pnl_r for t in losses))
    pf = gross_w / gross_l if gross_l > 0 else float('inf')
    total_r = sum(t.pnl_r for t in trades)
    exp_r = total_r / len(trades)

    # Max DD in R
    cum = peak = 0.0; max_dd = 0.0
    for t in trades:
        cum += t.pnl_r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    stops = sum(1 for t in trades if t.exit_reason == "stop")
    quick_stops = sum(1 for t in trades if t.exit_reason == "stop" and t.bars_held <= 2)

    # Train/test (odd/even dates)
    train = [t for t in trades if t.setup.timestamp.day % 2 == 1]
    test = [t for t in trades if t.setup.timestamp.day % 2 == 0]
    train_r = sum(t.pnl_r for t in train)
    test_r = sum(t.pnl_r for t in test)
    train_gw = sum(t.pnl_r for t in train if t.pnl_r > 0)
    train_gl = abs(sum(t.pnl_r for t in train if t.pnl_r <= 0))
    test_gw = sum(t.pnl_r for t in test if t.pnl_r > 0)
    test_gl = abs(sum(t.pnl_r for t in test if t.pnl_r <= 0))
    train_pf = train_gw / train_gl if train_gl > 0 else float('inf')
    test_pf = test_gw / test_gl if test_gl > 0 else float('inf')

    # Ex-best-day
    by_date = defaultdict(float)
    for t in trades:
        by_date[t.setup.timestamp.date()] += t.pnl_r
    if by_date:
        best_day = max(by_date.values())
        ex_best_total = total_r - best_day
    else:
        ex_best_total = total_r

    # Ex-top-symbol
    by_sym = defaultdict(float)
    for t in trades:
        by_sym[t.setup.symbol] += t.pnl_r
    if by_sym:
        best_sym_r = max(by_sym.values())
        ex_top_sym = total_r - best_sym_r
    else:
        ex_top_sym = total_r

    # Unique symbols and days
    n_symbols = len(set(t.setup.symbol for t in trades))
    n_days = len(set(t.setup.timestamp.date() for t in trades))

    return {
        "label": label,
        "trades": len(trades),
        "wr": len(wins) / len(trades) * 100,
        "pf_r": round(pf, 2),
        "exp_r": round(exp_r, 3),
        "total_r": round(total_r, 2),
        "max_dd_r": round(max_dd, 2),
        "stop_rate": round(stops / len(trades) * 100, 1),
        "quick_stop": round(quick_stops / len(trades) * 100, 1),
        "train_pf": round(train_pf, 2),
        "test_pf": round(test_pf, 2),
        "train_r": round(train_r, 2),
        "test_r": round(test_r, 2),
        "ex_best_day_r": round(ex_best_total, 2),
        "ex_top_sym_r": round(ex_top_sym, 2),
        "n_symbols": n_symbols,
        "n_days": n_days,
    }


# ─── SECTOR MAP ────────────────────────────────────────────────────
SECTOR_MAP = {
    "AAPL": "XLK", "ADBE": "XLK", "AMD": "XLK", "AMZN": "XLY", "ARM": "XLK",
    "BABA": "XLY", "BAC": "XLF", "C": "XLF", "COIN": "XLF", "CRSP": "XLV",
    "CVX": "XLE", "DASH": "XLY", "DKNG": "XLY", "FAS": "XLF", "GE": "XLI",
    "GOOG": "XLK", "HOOD": "XLF", "INTC": "XLK", "IONQ": "XLK", "JPM": "XLF",
    "LULU": "XLY", "MRNA": "XLV", "MS": "XLF", "MSFT": "XLK", "NFLX": "XLY",
    "NKE": "XLY", "NVDA": "XLK", "PLTR": "XLK", "RGTI": "XLK", "RIVN": "XLY",
    "RTX": "XLI", "SHOP": "XLK", "SMCI": "XLK", "SNAP": "XLK", "SOFI": "XLF",
    "TSLA": "XLY", "UBER": "XLY", "WMT": "XLP", "XOM": "XLE",
    # Leveraged ETFs — use underlying sector
    "FNGU": "XLK", "JNUG": "GDX", "LABD": "XLV", "LABU": "XLV",
    "NAIL": "XHB", "NUGT": "GDX", "SOXL": "XLK", "SOXS": "XLK",
    "SPXL": "SPY", "SPXS": "SPY", "TNA": "IWM", "TECL": "XLK",
    "TSLL": "XLY", "WEBL": "XLK", "XBI": "XLV",
    "AFRM": "XLK", "LUNR": "XLI",
}


# ═══════════════════════════════════════════════════════════════════
# H1: RELATIVE-STRENGTH LEADER PULLBACK LONG
# ═══════════════════════════════════════════════════════════════════

def scan_h1_rs_leader_pullback(bars: List[Bar], variant: dict, symbol: str) -> List[TradeSetup]:
    """
    RS Leader Pullback: stock must be outperforming SPY over a rolling window,
    then pull back shallowly, then re-expand.
    """
    setups = []
    rs_lookback = variant.get("rs_lookback", 6)  # bars (6 bars = 30 min at 5min)
    pb_max_depth = variant.get("pb_max_depth", 0.50)  # max pullback as fraction of impulse
    require_above_vwap = variant.get("require_above_vwap", True)
    require_sector_rs = variant.get("require_sector_rs", False)
    trigger_type = variant.get("trigger", "expansion_close")  # or "micro_pivot"
    min_rs = variant.get("min_rs", 0.10)  # min RS vs SPY in pct
    time_start = variant.get("time_start", 1000)
    time_end = variant.get("time_end", 1400)

    # State tracking
    impulse_high = NaN
    impulse_start_idx = -1
    in_pullback = False
    pb_low = NaN
    pb_start_idx = -1

    for i in range(rs_lookback + 1, len(bars)):
        b = bars[i]
        if b.time_hhmm < time_start or b.time_hhmm > time_end:
            # Reset on day boundary or out of window
            if b.time_hhmm <= 935:
                impulse_high = NaN; in_pullback = False
            continue
        if math.isnan(b.atr) or b.atr <= 0:
            continue
        if math.isnan(b.rs_vs_spy):
            continue

        # RS gate: must be outperforming SPY over lookback window
        lookback_rs = b.rs_vs_spy  # already cumulative from open
        if lookback_rs < min_rs:
            impulse_high = NaN; in_pullback = False
            continue

        # Optional sector RS gate
        if require_sector_rs and (math.isnan(b.rs_vs_sector) or b.rs_vs_sector < 0):
            continue

        # VWAP gate
        if require_above_vwap and b.close < b.vwap:
            # Still track if we were in an impulse
            if not math.isnan(impulse_high):
                in_pullback = True
                if math.isnan(pb_low) or b.low < pb_low:
                    pb_low = b.low
                    pb_start_idx = i
            continue

        # Track impulse: new high in an RS-leader context
        if math.isnan(impulse_high) or b.high > impulse_high:
            if not in_pullback:
                impulse_high = b.high
                impulse_start_idx = i
                pb_low = NaN
                continue

        # Detect pullback start: price pulls back from impulse_high
        if not in_pullback and not math.isnan(impulse_high):
            if b.close < impulse_high - 0.3 * b.atr:
                in_pullback = True
                pb_low = b.low
                pb_start_idx = i
                continue

        # In pullback: track depth
        if in_pullback:
            if b.low < pb_low or math.isnan(pb_low):
                pb_low = b.low

            # Check pullback depth
            impulse_range = impulse_high - bars[impulse_start_idx].low if impulse_start_idx >= 0 else b.atr
            if impulse_range <= 0:
                impulse_range = b.atr
            pb_depth = (impulse_high - pb_low) / impulse_range

            if pb_depth > pb_max_depth:
                # Too deep — reset
                impulse_high = NaN; in_pullback = False; pb_low = NaN
                continue

            # Check trigger: re-expansion
            triggered = False
            if trigger_type == "expansion_close":
                # Close above prior bar high, bullish close
                if i > 0 and b.close > bars[i-1].high and b.close > b.open:
                    triggered = True
            elif trigger_type == "micro_pivot":
                # Close above highest high of last 3 bars in pullback
                lookback_high = max(bars[j].high for j in range(max(0, i-3), i))
                if b.close > lookback_high and b.close > b.open:
                    triggered = True

            if triggered:
                # Volume confirmation: above average
                if b.volume < b.vol_ma * 0.8:
                    continue

                stop = pb_low - 0.02
                risk = b.close - stop
                if risk <= 0 or risk > 2.0 * b.atr:
                    impulse_high = NaN; in_pullback = False; pb_low = NaN
                    continue

                setups.append(TradeSetup(
                    bar_idx=i, timestamp=b.timestamp,
                    entry=b.close, stop=stop,
                    setup_id="H1_RS_LEADER_PB", variant=str(variant),
                    symbol=symbol,
                    tags=[f"rs={lookback_rs:.2f}", f"pb_depth={pb_depth:.2f}"]
                ))
                # Reset after signal
                impulse_high = NaN; in_pullback = False; pb_low = NaN

    return setups


# ═══════════════════════════════════════════════════════════════════
# H2: FAILED DOWNSIDE AUCTION → REVERSAL LONG
# ═══════════════════════════════════════════════════════════════════

def scan_h2_failed_auction(bars: List[Bar], variant: dict, symbol: str) -> List[TradeSetup]:
    """
    Failed downside auction: morning flush or probe below key reference fails,
    price reclaims the reference, holds, then triggers long.
    """
    setups = []
    flush_depth_atr = variant.get("flush_depth_atr", 0.30)  # min penetration below anchor
    reclaim_anchor = variant.get("reclaim_anchor", "vwap")  # "vwap", "open", "or_low"
    hold_bars = variant.get("hold_bars", 1)
    trigger_type = variant.get("trigger", "expansion_close")  # or "higher_low"
    use_market_filter = variant.get("use_market_filter", False)
    time_start = variant.get("time_start", 945)
    time_end = variant.get("time_end", 1200)

    # State per day
    flushed = False
    flush_low = NaN
    reclaimed = False
    reclaim_bar_idx = -1
    hold_count = 0
    hold_low = NaN
    fired_today = False
    current_date = None

    for i, b in enumerate(bars):
        # Day reset
        if b.date_int != current_date:
            current_date = b.date_int
            flushed = False; reclaimed = False; fired_today = False
            flush_low = NaN; hold_count = 0; hold_low = NaN
            continue

        if b.time_hhmm < time_start or b.time_hhmm > time_end:
            continue
        if math.isnan(b.atr) or b.atr <= 0:
            continue
        if fired_today:
            continue

        # Get anchor level
        if reclaim_anchor == "vwap":
            anchor = b.vwap
        elif reclaim_anchor == "open":
            anchor = b.day_open
        elif reclaim_anchor == "or_low":
            anchor = b.or_low if not math.isnan(b.or_low) else b.day_open
        else:
            anchor = b.vwap

        if math.isnan(anchor):
            continue

        # Phase 1: Detect flush below anchor
        if not flushed:
            depth = anchor - b.low
            if depth >= flush_depth_atr * b.atr:
                flushed = True
                flush_low = b.low
            continue

        # Track flush continuation
        if not reclaimed:
            if b.low < flush_low:
                flush_low = b.low
            # Phase 2: Detect reclaim
            if b.close > anchor:
                reclaimed = True
                reclaim_bar_idx = i
                hold_count = 0
                hold_low = b.low
            continue

        # Phase 3: Hold period
        if hold_count < hold_bars:
            hold_count += 1
            hold_low = min(hold_low, b.low) if not math.isnan(hold_low) else b.low
            # Must stay above anchor
            if b.close < anchor - 0.1 * b.atr:
                # Failed hold — reset
                reclaimed = False; hold_count = 0
            continue

        # Phase 4: Trigger
        triggered = False
        if trigger_type == "expansion_close":
            if b.close > bars[i-1].high and b.close > b.open:
                triggered = True
        elif trigger_type == "higher_low":
            if b.low > hold_low and b.close > bars[i-1].high:
                triggered = True

        # Market filter (optional): SPY not deeply red
        if use_market_filter and not math.isnan(b.spy_pct):
            if b.spy_pct < -0.30:  # SPY down more than 0.3%
                continue

        if triggered:
            stop = min(flush_low, hold_low) - 0.02
            risk = b.close - stop
            if risk <= 0 or risk > 2.5 * b.atr:
                continue

            setups.append(TradeSetup(
                bar_idx=i, timestamp=b.timestamp,
                entry=b.close, stop=stop,
                setup_id="H2_FAILED_AUCTION", variant=str(variant),
                symbol=symbol,
                tags=[f"anchor={reclaim_anchor}", f"flush={anchor-flush_low:.2f}"]
            ))
            fired_today = True
            flushed = False; reclaimed = False

    return setups


# ═══════════════════════════════════════════════════════════════════
# H3: GAP-AND-HOLD LONG IN TRUE IN-PLAY NAMES
# ═══════════════════════════════════════════════════════════════════

def scan_h3_gap_and_hold(bars: List[Bar], variant: dict, symbol: str) -> List[TradeSetup]:
    """
    Gap-and-hold: stock gaps up with strong volume, first pullback holds
    above a reference, long on re-expansion.
    """
    setups = []
    gap_min_pct = variant.get("gap_min_pct", 1.0)  # min gap %
    vol_min_mult = variant.get("vol_min_mult", 1.5)  # opening volume vs avg
    hold_ref = variant.get("hold_ref", "vwap")  # "vwap", "or_mid", "open"
    trigger_type = variant.get("trigger", "expansion_close")
    strict_in_play = variant.get("strict_in_play", True)
    time_start = variant.get("time_start", 945)
    time_end = variant.get("time_end", 1200)

    current_date = None
    is_gap_day = False
    first_bar_vol = 0
    pb_low = NaN
    in_pullback = False
    impulse_high = NaN
    fired_today = False
    prev_day_close = NaN

    for i, b in enumerate(bars):
        if b.date_int != current_date:
            # Detect gap on first bar of day
            if current_date is not None and i > 0:
                prev_day_close = bars[i-1].close
            current_date = b.date_int
            is_gap_day = False
            fired_today = False
            in_pullback = False
            impulse_high = NaN
            pb_low = NaN

            # Check gap
            if not math.isnan(prev_day_close) and prev_day_close > 0:
                gap_pct = (b.open - prev_day_close) / prev_day_close * 100
                if gap_pct >= gap_min_pct:
                    is_gap_day = True
            first_bar_vol = b.volume
            continue

        if not is_gap_day or fired_today:
            continue
        if b.time_hhmm < time_start or b.time_hhmm > time_end:
            continue
        if math.isnan(b.atr) or b.atr <= 0:
            continue

        # Volume check on first few bars (in-play filter)
        if strict_in_play and b.time_hhmm <= 940:
            if b.vol_ma > 0 and first_bar_vol < vol_min_mult * b.vol_ma:
                is_gap_day = False
                continue

        # Get hold reference
        if hold_ref == "vwap":
            ref = b.vwap
        elif hold_ref == "or_mid":
            ref = (b.or_high + b.or_low) / 2 if not math.isnan(b.or_high) else b.vwap
        elif hold_ref == "open":
            ref = b.day_open
        else:
            ref = b.vwap

        if math.isnan(ref):
            continue

        # Track impulse high after gap
        if math.isnan(impulse_high) or b.high > impulse_high:
            impulse_high = b.high

        # Detect pullback
        if not in_pullback and b.close < impulse_high - 0.3 * b.atr:
            in_pullback = True
            pb_low = b.low

        if in_pullback:
            pb_low = min(pb_low, b.low)

            # Hold check: pullback must stay above reference
            if b.close < ref - 0.15 * b.atr:
                # Failed hold
                in_pullback = False; impulse_high = NaN; pb_low = NaN
                continue

            # Trigger
            triggered = False
            if trigger_type == "expansion_close":
                if b.close > bars[i-1].high and b.close > b.open:
                    triggered = True
            elif trigger_type == "pb_high_break":
                recent_high = max(bars[j].high for j in range(max(0, i-3), i))
                if b.close > recent_high and b.close > b.open:
                    triggered = True

            if triggered:
                stop = pb_low - 0.02
                risk = b.close - stop
                if risk <= 0 or risk > 2.0 * b.atr:
                    continue

                setups.append(TradeSetup(
                    bar_idx=i, timestamp=b.timestamp,
                    entry=b.close, stop=stop,
                    setup_id="H3_GAP_HOLD", variant=str(variant),
                    symbol=symbol, tags=[f"gap_day"]
                ))
                fired_today = True
                in_pullback = False

    return setups


# ═══════════════════════════════════════════════════════════════════
# H4: TIGHT CONSOLIDATION AFTER STRONG OPENING DRIVE
# ═══════════════════════════════════════════════════════════════════

def scan_h4_tight_consol(bars: List[Bar], variant: dict, symbol: str) -> List[TradeSetup]:
    """
    Tight consolidation after opening drive: strong initial buying wave,
    then tight range contraction above VWAP, long on expansion from box.
    """
    setups = []
    drive_min_atr = variant.get("drive_min_atr", 0.50)  # min opening drive in ATR
    compression_type = variant.get("compression_type", "range")  # "range" or "atr"
    compression_bars_min = variant.get("compression_bars_min", 3)
    compression_bars_max = variant.get("compression_bars_max", 6)
    compression_max_range_atr = variant.get("compression_max_range_atr", 0.60)
    trigger_type = variant.get("trigger", "box_break")  # or "expansion_close"
    require_rs = variant.get("require_rs", False)
    time_start = variant.get("time_start", 1000)
    time_end = variant.get("time_end", 1300)

    current_date = None
    drive_detected = False
    drive_high = NaN
    drive_low = NaN
    consol_bars = []
    fired_today = False

    for i, b in enumerate(bars):
        if b.date_int != current_date:
            current_date = b.date_int
            drive_detected = False; fired_today = False
            drive_high = NaN; drive_low = NaN
            consol_bars = []
            continue

        if fired_today:
            continue
        if math.isnan(b.atr) or b.atr <= 0:
            continue

        # Phase 1: Detect opening drive (continuously check until found)
        if not drive_detected:
            # Check move from day open to current session high
            if not math.isnan(b.day_open) and not math.isnan(b.atr) and b.atr > 0:
                move = b.session_high - b.day_open if not math.isnan(b.session_high) else b.high - b.day_open
                if move >= drive_min_atr * b.atr:
                    drive_detected = True
                    drive_high = b.session_high if not math.isnan(b.session_high) else b.high
                    drive_low = b.day_open
            if not drive_detected:
                continue

        if b.time_hhmm < time_start or b.time_hhmm > time_end:
            continue

        # Must be above VWAP
        if b.close < b.vwap:
            consol_bars = []
            continue

        # RS check (optional)
        if require_rs and not math.isnan(b.rs_vs_spy) and b.rs_vs_spy < 0:
            continue

        # Phase 2: Check if prior consolidation window qualifies for breakout
        if len(consol_bars) >= compression_bars_min:
            box_high = max(cb.high for cb in consol_bars)
            box_low = min(cb.low for cb in consol_bars)
            box_range = box_high - box_low

            is_tight = False
            if compression_type == "range":
                is_tight = box_range <= compression_max_range_atr * b.atr
            elif compression_type == "atr":
                recent_ranges = [cb.high - cb.low for cb in consol_bars[-3:]]
                avg_recent = sum(recent_ranges) / len(recent_ranges)
                is_tight = avg_recent <= compression_max_range_atr * b.atr

            if is_tight:
                # Phase 3: Check current bar as trigger (NOT part of consol)
                triggered = False
                if trigger_type == "box_break":
                    if b.close > box_high and b.close > b.open:
                        triggered = True
                elif trigger_type == "expansion_close":
                    if b.close > bars[i-1].high and b.close > b.open:
                        triggered = True

                if triggered:
                    stop = box_low - 0.02
                    risk = b.close - stop
                    if risk > 0 and risk <= 2.0 * b.atr:
                        setups.append(TradeSetup(
                            bar_idx=i, timestamp=b.timestamp,
                            entry=b.close, stop=stop,
                            setup_id="H4_TIGHT_CONSOL", variant=str(variant),
                            symbol=symbol,
                            tags=[f"box_range={box_range:.2f}", f"consol_bars={len(consol_bars)}"]
                        ))
                        fired_today = True
                        consol_bars = []
                        continue

        # Add current bar to consolidation window (for future checks)
        consol_bars.append(b)

        # Trim if too many bars
        if len(consol_bars) > compression_bars_max:
            consol_bars.pop(0)

    return setups


# ═══════════════════════════════════════════════════════════════════
# H5: TREND-DAY SECOND-LEG LONG
# ═══════════════════════════════════════════════════════════════════

def scan_h5_second_leg(bars: List[Bar], variant: dict, symbol: str) -> List[TradeSetup]:
    """
    Second-leg continuation: first impulse proves trend, one orderly pullback,
    then long on second-leg trigger.
    """
    setups = []
    min_first_leg_atr = variant.get("min_first_leg_atr", 0.60)
    pb_max_depth = variant.get("pb_max_depth", 0.50)  # fraction of first leg
    trigger_type = variant.get("trigger", "second_leg_break")  # or "expansion_close"
    time_start = variant.get("time_start", 1015)
    time_end = variant.get("time_end", 1200)
    vwap_must_hold = variant.get("vwap_must_hold", True)

    current_date = None
    first_leg_high = NaN
    first_leg_low = NaN
    first_leg_done = False
    in_pb = False
    pb_low = NaN
    fired_today = False

    for i, b in enumerate(bars):
        if b.date_int != current_date:
            current_date = b.date_int
            first_leg_high = NaN; first_leg_low = NaN
            first_leg_done = False; in_pb = False; pb_low = NaN
            fired_today = False
            continue

        if fired_today:
            continue
        if math.isnan(b.atr) or b.atr <= 0:
            continue

        # Track first leg (morning impulse)
        if not first_leg_done:
            if b.time_hhmm <= 1000:
                if math.isnan(first_leg_low):
                    first_leg_low = b.low
                first_leg_low = min(first_leg_low, b.low)
                if math.isnan(first_leg_high) or b.high > first_leg_high:
                    first_leg_high = b.high
            else:
                # Check if first leg qualifies
                leg_size = first_leg_high - first_leg_low if not math.isnan(first_leg_high) else 0
                if leg_size >= min_first_leg_atr * b.atr:
                    first_leg_done = True
                else:
                    continue  # No qualified first leg today

        if b.time_hhmm < time_start or b.time_hhmm > time_end:
            continue

        # VWAP check
        if vwap_must_hold and b.close < b.vwap:
            in_pb = False; pb_low = NaN
            continue

        # Detect pullback from first leg high
        if not in_pb:
            if b.close < first_leg_high - 0.2 * b.atr:
                in_pb = True
                pb_low = b.low
            else:
                first_leg_high = max(first_leg_high, b.high)
            continue

        # Track pullback
        pb_low = min(pb_low, b.low)
        pb_depth = (first_leg_high - pb_low) / (first_leg_high - first_leg_low) if first_leg_high > first_leg_low else 1.0

        if pb_depth > pb_max_depth:
            in_pb = False; pb_low = NaN
            continue

        # Trigger
        triggered = False
        if trigger_type == "second_leg_break":
            if b.close > first_leg_high and b.close > b.open:
                triggered = True
        elif trigger_type == "expansion_close":
            if b.close > bars[i-1].high and b.close > b.open:
                triggered = True

        if triggered:
            stop = pb_low - 0.02
            risk = b.close - stop
            if risk <= 0 or risk > 2.5 * b.atr:
                continue

            setups.append(TradeSetup(
                bar_idx=i, timestamp=b.timestamp,
                entry=b.close, stop=stop,
                setup_id="H5_SECOND_LEG", variant=str(variant),
                symbol=symbol,
                tags=[f"leg1={first_leg_high-first_leg_low:.2f}", f"pb={pb_depth:.2f}"]
            ))
            fired_today = True
            in_pb = False

    return setups


# ═══════════════════════════════════════════════════════════════════
# VARIANT DEFINITIONS
# ═══════════════════════════════════════════════════════════════════

H1_VARIANTS = {
    "H1a_spy15_shallow_expansion": {
        "rs_lookback": 3, "min_rs": 0.10, "pb_max_depth": 0.40,
        "require_above_vwap": True, "require_sector_rs": False,
        "trigger": "expansion_close",
    },
    "H1b_spy30_shallow_pivot": {
        "rs_lookback": 6, "min_rs": 0.15, "pb_max_depth": 0.40,
        "require_above_vwap": True, "require_sector_rs": False,
        "trigger": "micro_pivot",
    },
    "H1c_spy_sector_shallow": {
        "rs_lookback": 6, "min_rs": 0.10, "pb_max_depth": 0.50,
        "require_above_vwap": True, "require_sector_rs": True,
        "trigger": "expansion_close",
    },
    "H1d_spy15_moderate_reclaim": {
        "rs_lookback": 3, "min_rs": 0.10, "pb_max_depth": 0.60,
        "require_above_vwap": False, "require_sector_rs": False,
        "trigger": "expansion_close",
    },
}

H2_VARIANTS = {
    "H2a_vwap_moderate_1bar": {
        "flush_depth_atr": 0.30, "reclaim_anchor": "vwap",
        "hold_bars": 1, "trigger": "expansion_close", "use_market_filter": False,
    },
    "H2b_vwap_deep_2bar": {
        "flush_depth_atr": 0.50, "reclaim_anchor": "vwap",
        "hold_bars": 2, "trigger": "expansion_close", "use_market_filter": False,
    },
    "H2c_open_moderate_HL": {
        "flush_depth_atr": 0.30, "reclaim_anchor": "open",
        "hold_bars": 1, "trigger": "higher_low", "use_market_filter": False,
    },
    "H2d_orlow_deep_mktfilter": {
        "flush_depth_atr": 0.40, "reclaim_anchor": "or_low",
        "hold_bars": 2, "trigger": "expansion_close", "use_market_filter": True,
    },
}

H3_VARIANTS = {
    "H3a_strong_gap_vwap_strict": {
        "gap_min_pct": 2.0, "vol_min_mult": 2.0, "hold_ref": "vwap",
        "trigger": "expansion_close", "strict_in_play": True,
    },
    "H3b_modest_gap_vwap_loose": {
        "gap_min_pct": 1.0, "vol_min_mult": 1.5, "hold_ref": "vwap",
        "trigger": "expansion_close", "strict_in_play": False,
    },
    "H3c_strong_gap_ormid": {
        "gap_min_pct": 2.0, "vol_min_mult": 1.5, "hold_ref": "or_mid",
        "trigger": "pb_high_break", "strict_in_play": True,
    },
}

H4_VARIANTS = {
    "H4a_moderate_range_boxbreak": {
        "drive_min_atr": 0.50, "compression_type": "range",
        "compression_bars_min": 3, "compression_bars_max": 6,
        "compression_max_range_atr": 0.60, "trigger": "box_break",
        "require_rs": False,
    },
    "H4b_strong_range_boxbreak": {
        "drive_min_atr": 0.75, "compression_type": "range",
        "compression_bars_min": 3, "compression_bars_max": 6,
        "compression_max_range_atr": 0.50, "trigger": "box_break",
        "require_rs": False,
    },
    "H4c_moderate_atr_expansion": {
        "drive_min_atr": 0.50, "compression_type": "atr",
        "compression_bars_min": 3, "compression_bars_max": 8,
        "compression_max_range_atr": 0.60, "trigger": "expansion_close",
        "require_rs": False,
    },
    "H4d_strong_range_rs": {
        "drive_min_atr": 0.75, "compression_type": "range",
        "compression_bars_min": 3, "compression_bars_max": 6,
        "compression_max_range_atr": 0.60, "trigger": "box_break",
        "require_rs": True,
    },
}

H5_VARIANTS = {
    "H5a_moderate_shallow_break": {
        "min_first_leg_atr": 0.60, "pb_max_depth": 0.40,
        "trigger": "second_leg_break", "vwap_must_hold": True,
        "time_start": 1015, "time_end": 1200,
    },
    "H5b_strong_shallow_expansion": {
        "min_first_leg_atr": 0.80, "pb_max_depth": 0.40,
        "trigger": "expansion_close", "vwap_must_hold": True,
        "time_start": 1015, "time_end": 1200,
    },
    "H5c_moderate_moderate_broader": {
        "min_first_leg_atr": 0.60, "pb_max_depth": 0.50,
        "trigger": "expansion_close", "vwap_must_hold": False,
        "time_start": 1015, "time_end": 1400,
    },
}


# ═══════════════════════════════════════════════════════════════════
# MAIN RUNNER
# ═══════════════════════════════════════════════════════════════════

def get_symbols():
    wl_path = Path(__file__).parent.parent / "watchlist.txt"
    if wl_path.exists():
        with open(wl_path) as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    # Fallback: scan data dir
    return [p.stem.replace("_5min", "") for p in DATA_DIR.glob("*_5min.csv")
            if p.stem.replace("_5min", "") not in ("SPY", "QQQ") and not p.stem.startswith("X")]


def run_hypothesis(h_name, scanner_fn, variants, symbols, spy_bars, exit_mode="hybrid",
                   time_stop_bars=20, target_rr=0.0):
    """Run all variants of a hypothesis across all symbols."""
    print(f"\n{'='*70}")
    print(f"  {h_name}")
    if target_rr > 0:
        print(f"  Exit: {target_rr:.1f}R target + {time_stop_bars}-bar time stop")
    else:
        print(f"  Exit: {exit_mode} ({time_stop_bars}-bar time stop)")
    print(f"{'='*70}")

    spy_bars_loaded = spy_bars
    sector_cache = {}
    bars_cache = {}

    all_results = {}

    for vname, vparams in variants.items():
        all_setups = []
        setups_by_sym = defaultdict(list)

        for sym in symbols:
            if sym not in bars_cache:
                bars = load_bars(sym)
                if not bars:
                    bars_cache[sym] = None
                    continue
                compute_indicators(bars)

                sector_etf = SECTOR_MAP.get(sym)
                if sector_etf and sector_etf not in sector_cache:
                    sb = load_bars(sector_etf)
                    if sb:
                        compute_indicators(sb)
                        sector_cache[sector_etf] = sb
                    else:
                        sector_cache[sector_etf] = None

                compute_rs(bars, spy_bars_loaded, sector_cache.get(SECTOR_MAP.get(sym)))
                bars_cache[sym] = bars
            else:
                bars = bars_cache[sym]
                if bars is None:
                    continue

            setups = scanner_fn(bars, vparams, sym)
            setups_by_sym[sym].extend(setups)

        all_trades = []
        for sym, sym_setups in setups_by_sym.items():
            bars = bars_cache.get(sym)
            if not bars or not sym_setups:
                continue
            trades = simulate_trades(bars, sym_setups, exit_mode=exit_mode,
                                    time_stop_bars=time_stop_bars,
                                    target_rr=target_rr)
            all_trades.extend(trades)

        raw_metrics = compute_metrics(all_trades, label=f"{vname}")

        all_results[vname] = {
            "raw": raw_metrics,
            "trades": all_trades,
        }

        m = raw_metrics
        train_test = f"Train PF {m['train_pf']:.2f} ({m['train_r']:+.1f}R) / Test PF {m['test_pf']:.2f} ({m['test_r']:+.1f}R)"
        stability = "STABLE" if m['train_pf'] > 1.0 and m['test_pf'] > 1.0 else "UNSTABLE" if m['train_pf'] < 1.0 or m['test_pf'] < 1.0 else "MIXED"

        print(f"\n  {vname}:")
        print(f"    Trades: {m['trades']:>4}  |  WR: {m['wr']:.1f}%  |  PF(R): {m['pf_r']:.2f}  |  Exp(R): {m['exp_r']:+.3f}  |  Total(R): {m['total_r']:+.1f}")
        print(f"    MaxDD: {m['max_dd_r']:.1f}R  |  StopRate: {m['stop_rate']:.0f}%  |  QuickStop: {m['quick_stop']:.0f}%")
        print(f"    {train_test}  [{stability}]")
        print(f"    Ex-best-day: {m['ex_best_day_r']:+.1f}R  |  Ex-top-sym: {m['ex_top_sym_r']:+.1f}R")
        print(f"    Symbols: {m['n_symbols']}  |  Days: {m['n_days']}")

    return all_results


def main():
    print("Long-Side Discovery Study")
    print("=" * 70)

    symbols = get_symbols()
    print(f"Universe: {len(symbols)} symbols")

    # Load SPY once
    spy_bars = load_bars("SPY")
    if not spy_bars:
        print("ERROR: Cannot load SPY data")
        return
    compute_indicators(spy_bars)
    print(f"SPY bars: {len(spy_bars)}")

    # Determine which hypotheses to run
    run_all = "--all" in sys.argv
    run_h = [a.upper() for a in sys.argv[1:] if a.startswith("H") or a.startswith("h")]

    if not run_h and not run_all:
        run_h = ["H1", "H2", "H3", "H4", "H5"]  # All 5
        print(f"Running all 5 hypotheses")

    # Test exit configurations
    exit_configs = [
        {"label": "2R_target", "exit_mode": "time", "time_stop_bars": 20, "target_rr": 2.0},
        {"label": "3R_target", "exit_mode": "time", "time_stop_bars": 20, "target_rr": 3.0},
        {"label": "hybrid_trail", "exit_mode": "hybrid", "time_stop_bars": 20, "target_rr": 0.0},
    ]

    results = {}

    for ec in exit_configs:
        ec_label = ec["label"]
        print(f"\n\n{'#'*70}")
        print(f"  EXIT CONFIG: {ec_label}")
        print(f"{'#'*70}")

        if "H4" in run_h or run_all:
            results[f"H4_{ec_label}"] = run_hypothesis(
                f"H4: TIGHT CONSOLIDATION [{ec_label}]",
                scan_h4_tight_consol, H4_VARIANTS, symbols, spy_bars,
                exit_mode=ec["exit_mode"], time_stop_bars=ec["time_stop_bars"],
                target_rr=ec["target_rr"])

        if "H1" in run_h or run_all:
            results[f"H1_{ec_label}"] = run_hypothesis(
                f"H1: RS LEADER PULLBACK [{ec_label}]",
                scan_h1_rs_leader_pullback, H1_VARIANTS, symbols, spy_bars,
                exit_mode=ec["exit_mode"], time_stop_bars=ec["time_stop_bars"],
                target_rr=ec["target_rr"])

        if "H2" in run_h or run_all:
            results[f"H2_{ec_label}"] = run_hypothesis(
                f"H2: FAILED AUCTION [{ec_label}]",
                scan_h2_failed_auction, H2_VARIANTS, symbols, spy_bars,
                exit_mode=ec["exit_mode"], time_stop_bars=ec["time_stop_bars"],
                target_rr=ec["target_rr"])

        if "H3" in run_h or run_all:
            results[f"H3_{ec_label}"] = run_hypothesis(
                f"H3: GAP-AND-HOLD [{ec_label}]",
                scan_h3_gap_and_hold, H3_VARIANTS, symbols, spy_bars,
                exit_mode=ec["exit_mode"], time_stop_bars=ec["time_stop_bars"],
                target_rr=ec["target_rr"])

        if "H5" in run_h or run_all:
            results[f"H5_{ec_label}"] = run_hypothesis(
                f"H5: SECOND-LEG [{ec_label}]",
                scan_h5_second_leg, H5_VARIANTS, symbols, spy_bars,
                exit_mode=ec["exit_mode"], time_stop_bars=ec["time_stop_bars"],
                target_rr=ec["target_rr"])

    # ── SUMMARY LEDGER ──
    print(f"\n\n{'='*90}")
    print("  RESEARCH LEDGER — WAVE 1 SUMMARY")
    print(f"{'='*90}")
    print(f"  {'Variant':<35} {'Trades':>6} {'PF(R)':>7} {'Exp(R)':>8} {'TotalR':>8} {'MaxDD':>7} {'Train':>7} {'Test':>7} {'Verdict':<10}")
    print(f"  {'-'*86}")

    for h_key in sorted(results.keys()):
        h_results = results[h_key]
        for vname, vdata in sorted(h_results.items()):
            m = vdata["raw"]
            # Auto-verdict
            if m["trades"] < 15:
                verdict = "LOW_N"
            elif m["pf_r"] < 1.0:
                verdict = "RETIRE"
            elif m["pf_r"] >= 1.3 and m["train_pf"] >= 1.0 and m["test_pf"] >= 1.0:
                verdict = "PROMOTE"
            elif m["pf_r"] >= 1.1:
                verdict = "CONTINUE"
            else:
                verdict = "RETIRE"

            # Check concentration risk
            if m["trades"] > 0:
                if m["ex_top_sym_r"] < 0 and m["total_r"] > 0:
                    verdict = "1-SYM RISK"
                if m["ex_best_day_r"] < 0 and m["total_r"] > 0:
                    verdict = "1-DAY RISK"

            print(f"  {vname:<35} {m['trades']:>6} {m['pf_r']:>7.2f} {m['exp_r']:>+8.3f} {m['total_r']:>+8.1f} {m['max_dd_r']:>7.1f} {m['train_pf']:>7.2f} {m['test_pf']:>7.2f} {verdict:<10}")

    print(f"  {'-'*86}")
    print(f"\n  Verdicts: PROMOTE = promising, deepen | CONTINUE = worth exploring | RETIRE = kill")
    print(f"            LOW_N = insufficient trades | 1-SYM/1-DAY RISK = concentrated edge\n")


if __name__ == "__main__":
    main()
