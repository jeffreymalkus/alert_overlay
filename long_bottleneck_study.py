#!/usr/bin/env python3
"""
Long-side bottleneck analysis: habitat, horizon, or regime?

Three questions:
1. REGIME: Is this 62-day sample structurally hostile to intraday longs?
2. HABITAT: Do in-play names behave differently from the static watchlist?
3. HORIZON: Do longer holds capture edge that intraday exits miss?

Uses a simple, well-defined long entry (VWAP reclaim + hold) as the probe signal,
identical across all tests. Only the universe, hold period, and regime context change.
"""

import csv, math, sys, os
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from collections import defaultdict
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent / "data"
NaN = float("nan")


# ─── Reuse bar infrastructure from long_discovery_study ─────────

@dataclass
class Bar:
    timestamp: datetime
    open: float; high: float; low: float; close: float; volume: float
    time_hhmm: int = 0; date_int: int = 0
    ema9: float = NaN; ema20: float = NaN; vwap: float = NaN
    atr: float = NaN; vol_ma: float = NaN
    day_open: float = NaN; or_high: float = NaN; or_low: float = NaN
    session_high: float = NaN; session_low: float = NaN
    spy_pct: float = NaN; rs_vs_spy: float = NaN


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
                b = Bar(timestamp=dt, open=float(norm["open"]), high=float(norm["high"]),
                        low=float(norm["low"]), close=float(norm["close"]),
                        volume=float(norm["volume"]))
                b.time_hhmm = dt.hour * 100 + dt.minute
                b.date_int = dt.year * 10000 + dt.month * 100 + dt.day
                bars.append(b)
            except (ValueError, KeyError):
                continue
    return bars


def compute_indicators(bars: List[Bar]):
    e9 = e20 = None; atr_vals = []; vol_vals = []; prev_close = NaN
    vwap_cum_pv = vwap_cum_v = 0.0; current_date = None
    day_open = NaN; or_high = NaN; or_low = NaN
    session_high = NaN; session_low = NaN

    for i, b in enumerate(bars):
        if b.date_int != current_date:
            current_date = b.date_int
            vwap_cum_pv = vwap_cum_v = 0.0; day_open = b.open
            or_high = NaN; or_low = NaN; session_high = b.high; session_low = b.low
        else:
            session_high = max(session_high, b.high); session_low = min(session_low, b.low)
        b.day_open = day_open; b.session_high = session_high; b.session_low = session_low
        if 930 <= b.time_hhmm <= 940:
            or_high = max(or_high, b.high) if not math.isnan(or_high) else b.high
            or_low = min(or_low, b.low) if not math.isnan(or_low) else b.low
        b.or_high = or_high; b.or_low = or_low
        tp = (b.high + b.low + b.close) / 3.0
        vwap_cum_pv += tp * b.volume; vwap_cum_v += b.volume
        b.vwap = vwap_cum_pv / vwap_cum_v if vwap_cum_v > 0 else b.close
        if e9 is None: e9 = b.close
        else: e9 = b.close * (2/10) + e9 * (8/10)
        b.ema9 = e9
        if e20 is None: e20 = b.close
        else: e20 = b.close * (2/21) + e20 * (19/21)
        b.ema20 = e20
        if not math.isnan(prev_close):
            tr = max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close))
        else: tr = b.high - b.low
        atr_vals.append(tr)
        if len(atr_vals) >= 14:
            if len(atr_vals) == 14: b.atr = sum(atr_vals) / 14
            else: b.atr = (bars[i-1].atr * 13 + tr) / 14 if not math.isnan(bars[i-1].atr) else sum(atr_vals[-14:]) / 14
        prev_close = b.close
        vol_vals.append(b.volume)
        b.vol_ma = sum(vol_vals[-20:]) / min(len(vol_vals), 20)


def compute_spy_pct(stock_bars: List[Bar], spy_bars: List[Bar]):
    spy_map = {}
    day_open = NaN; current_date = None
    for b in spy_bars:
        d = b.timestamp.year * 10000 + b.timestamp.month * 100 + b.timestamp.day
        if d != current_date: current_date = d; day_open = b.open
        if day_open > 0: spy_map[b.timestamp] = (b.close - day_open) / day_open * 100
    for b in stock_bars:
        b.spy_pct = spy_map.get(b.timestamp, NaN)
        stock_pct = (b.close - b.day_open) / b.day_open * 100 if b.day_open > 0 else 0
        sp = spy_map.get(b.timestamp, NaN)
        b.rs_vs_spy = stock_pct - sp if not math.isnan(sp) else NaN


# ─── Probe Signal: Simple VWAP Reclaim Long ───────────────────────
# Entry: first bullish close above VWAP after being below, with vol > 0.7× vol_ma
# Stop: recent swing low (lowest low of last 5 bars)
# This is deliberately simple — we're testing environment, not trigger quality.

@dataclass
class ProbeTrade:
    symbol: str; date: object; entry_bar_idx: int; entry_time: datetime
    entry: float; stop: float; direction: int = 1
    # Filled by simulation
    exit_price: float = 0.0; exit_reason: str = ""; pnl_r: float = 0.0
    bars_held: int = 0; exit_time: Optional[datetime] = None
    # Context
    spy_pct_at_entry: float = NaN; first_bar_rvol: float = NaN


def detect_probe_signals(bars: List[Bar], time_start=1000, time_end=1400) -> List[ProbeTrade]:
    """Simple VWAP reclaim probe — one per day max."""
    signals = []
    current_date = None; was_below = False; fired_today = False

    for i, b in enumerate(bars):
        if b.date_int != current_date:
            current_date = b.date_int; was_below = False; fired_today = False
            continue
        if fired_today or math.isnan(b.atr) or b.atr <= 0:
            continue
        if b.time_hhmm < time_start or b.time_hhmm > time_end:
            # Track below-VWAP state before window
            if b.close < b.vwap: was_below = True
            continue

        if b.close < b.vwap:
            was_below = True
            continue

        if was_below and b.close > b.vwap and b.close > b.open:
            # Volume check
            if b.vol_ma > 0 and b.volume >= 0.7 * b.vol_ma:
                # Stop = lowest low of last 5 bars
                lookback = max(0, i - 5)
                stop = min(bars[j].low for j in range(lookback, i + 1)) - 0.02
                risk = b.close - stop
                if risk > 0 and risk <= 2.0 * b.atr:
                    # First bar RVOL
                    day_bars = [bars[j] for j in range(max(0, i-20), i+1) if bars[j].date_int == b.date_int]
                    first_vol = day_bars[0].volume if day_bars else 0
                    rvol = first_vol / b.vol_ma if b.vol_ma > 0 else 1.0

                    signals.append(ProbeTrade(
                        symbol="", date=b.timestamp.date(),
                        entry_bar_idx=i, entry_time=b.timestamp,
                        entry=b.close, stop=stop,
                        spy_pct_at_entry=b.spy_pct,
                        first_bar_rvol=rvol,
                    ))
                    fired_today = True
                    was_below = False

    return signals


def simulate_probe(bars: List[Bar], signals: List[ProbeTrade],
                   exit_mode: str = "eod",  # "eod", "2R", "3R", "next_open", "hybrid20"
                   slip_bps=0.0004, slip_min=0.02) -> List[ProbeTrade]:
    """Simulate probe trades with different exit modes."""
    results = []
    for sig in signals:
        entry_slip = max(slip_min, sig.entry * slip_bps)
        filled_entry = sig.entry + entry_slip
        risk = filled_entry - sig.stop
        if risk <= 0: continue

        target_price = None
        if exit_mode == "2R": target_price = filled_entry + 2.0 * risk
        elif exit_mode == "3R": target_price = filled_entry + 3.0 * risk

        exited = False
        for j in range(sig.entry_bar_idx + 1, len(bars)):
            b = bars[j]

            # Day boundary for "next_open" mode
            if exit_mode == "next_open" and b.date_int != bars[sig.entry_bar_idx].date_int:
                exit_slip = max(slip_min, b.open * slip_bps)
                sig.exit_price = b.open
                sig.pnl_r = (b.open - exit_slip - filled_entry) / risk
                sig.exit_reason = "next_open"
                sig.bars_held = j - sig.entry_bar_idx
                sig.exit_time = b.timestamp
                exited = True; break

            # Day boundary for other modes — force EOD
            if b.date_int != bars[sig.entry_bar_idx].date_int:
                prev = bars[j-1]
                exit_slip = max(slip_min, prev.close * slip_bps)
                sig.exit_price = prev.close
                sig.pnl_r = (prev.close - exit_slip - filled_entry) / risk
                sig.exit_reason = "eod"
                sig.bars_held = j - 1 - sig.entry_bar_idx
                sig.exit_time = prev.timestamp
                exited = True; break

            # Stop
            if b.low <= sig.stop:
                exit_slip = max(slip_min, sig.stop * slip_bps)
                sig.exit_price = sig.stop
                sig.pnl_r = (sig.stop - exit_slip - filled_entry) / risk
                sig.exit_reason = "stop"
                sig.bars_held = j - sig.entry_bar_idx
                sig.exit_time = b.timestamp
                exited = True; break

            # Target
            if target_price and b.high >= target_price:
                exit_slip = max(slip_min, target_price * slip_bps)
                sig.exit_price = target_price
                sig.pnl_r = (target_price - exit_slip - filled_entry) / risk
                sig.exit_reason = "target"
                sig.bars_held = j - sig.entry_bar_idx
                sig.exit_time = b.timestamp
                exited = True; break

            # EOD exit
            if exit_mode == "eod" and b.time_hhmm >= 1555:
                exit_slip = max(slip_min, b.close * slip_bps)
                sig.exit_price = b.close
                sig.pnl_r = (b.close - exit_slip - filled_entry) / risk
                sig.exit_reason = "eod"
                sig.bars_held = j - sig.entry_bar_idx
                sig.exit_time = b.timestamp
                exited = True; break

            # Hybrid20: EMA9 trail after 2 bars, or time stop at 20
            if exit_mode == "hybrid20":
                bars_held = j - sig.entry_bar_idx
                if bars_held >= 2 and not math.isnan(b.ema9) and b.close < b.ema9:
                    exit_slip = max(slip_min, b.close * slip_bps)
                    sig.exit_price = b.close
                    sig.pnl_r = (b.close - exit_slip - filled_entry) / risk
                    sig.exit_reason = "ema9trail"
                    sig.bars_held = bars_held
                    sig.exit_time = b.timestamp
                    exited = True; break
                if bars_held >= 20:
                    exit_slip = max(slip_min, b.close * slip_bps)
                    sig.exit_price = b.close
                    sig.pnl_r = (b.close - exit_slip - filled_entry) / risk
                    sig.exit_reason = "time"
                    sig.bars_held = bars_held
                    sig.exit_time = b.timestamp
                    exited = True; break

            # Safety: same-day EOD for target modes
            if exit_mode in ("2R", "3R") and b.time_hhmm >= 1555:
                exit_slip = max(slip_min, b.close * slip_bps)
                sig.exit_price = b.close
                sig.pnl_r = (b.close - exit_slip - filled_entry) / risk
                sig.exit_reason = "eod"
                sig.bars_held = j - sig.entry_bar_idx
                sig.exit_time = b.timestamp
                exited = True; break

        if not exited:
            last = bars[-1]
            exit_slip = max(slip_min, last.close * slip_bps)
            sig.exit_price = last.close
            sig.pnl_r = (last.close - exit_slip - filled_entry) / risk
            sig.exit_reason = "eod"
            sig.bars_held = len(bars) - 1 - sig.entry_bar_idx
            sig.exit_time = last.timestamp

        results.append(sig)
    return results


def metrics(trades: List[ProbeTrade], label=""):
    if not trades:
        return {"label": label, "n": 0, "wr": 0, "pf": 0, "exp": 0, "total_r": 0, "max_dd": 0}
    wins = [t for t in trades if t.pnl_r > 0]
    gw = sum(t.pnl_r for t in wins)
    gl = abs(sum(t.pnl_r for t in trades if t.pnl_r <= 0))
    pf = gw / gl if gl > 0 else float('inf')
    total = sum(t.pnl_r for t in trades)
    cum = peak = max_dd = 0.0
    for t in trades:
        cum += t.pnl_r; peak = max(peak, cum); max_dd = max(max_dd, peak - cum)
    return {"label": label, "n": len(trades), "wr": round(len(wins)/len(trades)*100, 1),
            "pf": round(pf, 2), "exp": round(total/len(trades), 3),
            "total_r": round(total, 1), "max_dd": round(max_dd, 1)}


def print_metrics(m):
    print(f"    {m['label']:<40} N={m['n']:>4}  WR={m['wr']:>5.1f}%  PF={m['pf']:>5.2f}  "
          f"Exp={m['exp']:>+6.3f}  TotalR={m['total_r']:>+7.1f}  MaxDD={m['max_dd']:>5.1f}")


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    print("Long-Side Bottleneck Study: Habitat / Horizon / Regime")
    print("=" * 70)

    # Load watchlist
    wl_path = Path(__file__).parent / "watchlist.txt"
    with open(wl_path) as f:
        symbols = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    print(f"Static universe: {len(symbols)} symbols")

    # Load in-play names
    ip_path = Path(__file__).parent / "in_play.txt"
    in_play = []
    if ip_path.exists():
        with open(ip_path) as f:
            in_play = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    print(f"In-play names: {in_play}")

    # Load SPY
    spy_bars = load_bars("SPY")
    compute_indicators(spy_bars)
    print(f"SPY bars: {len(spy_bars)}")

    # Compute SPY daily pct-from-open for regime classification
    spy_daily = {}
    for b in spy_bars:
        if b.time_hhmm >= 1550:
            spy_daily[b.date_int] = (b.close - b.day_open) / b.day_open * 100 if b.day_open > 0 else 0

    # Classify days
    green_days = set(); red_days = set(); flat_days = set()
    for d, pct in spy_daily.items():
        if pct > 0.15: green_days.add(d)
        elif pct < -0.15: red_days.add(d)
        else: flat_days.add(d)

    print(f"\nSPY regime: {len(green_days)} GREEN, {len(flat_days)} FLAT, {len(red_days)} RED days")
    total_days = len(spy_daily)
    if total_days > 0:
        print(f"  GREEN: {len(green_days)/total_days*100:.0f}%  FLAT: {len(flat_days)/total_days*100:.0f}%  RED: {len(red_days)/total_days*100:.0f}%")

    # Monthly breakdown
    monthly_pct = defaultdict(list)
    for d, pct in spy_daily.items():
        month = str(d)[:6]  # YYYYMM
        monthly_pct[month].append(pct)

    print(f"\n  Monthly SPY performance:")
    for month in sorted(monthly_pct.keys()):
        vals = monthly_pct[month]
        avg = sum(vals) / len(vals)
        greens = sum(1 for v in vals if v > 0.15)
        reds = sum(1 for v in vals if v < -0.15)
        print(f"    {month}: avg={avg:+.2f}%, {len(vals)} days, {greens}G/{reds}R")

    # ── Pre-load all bars ──
    bars_cache = {}
    for sym in symbols:
        b = load_bars(sym)
        if b:
            compute_indicators(b)
            compute_spy_pct(b, spy_bars)
            bars_cache[sym] = b

    exit_modes = ["eod", "2R", "3R", "next_open", "hybrid20"]

    # ═══════════════════════════════════════════════════════════════
    # TEST 1: REGIME ASYMMETRY
    # Same probe signal, same universe, split by SPY day color
    # ═══════════════════════════════════════════════════════════════
    print(f"\n\n{'='*70}")
    print("  TEST 1: REGIME ASYMMETRY — Long probe by SPY day color")
    print(f"{'='*70}")

    for em in exit_modes:
        print(f"\n  Exit mode: {em}")
        all_trades = []; green_trades = []; red_trades = []; flat_trades = []

        for sym in symbols:
            bars = bars_cache.get(sym)
            if not bars: continue
            sigs = detect_probe_signals(bars)
            for s in sigs: s.symbol = sym
            trades = simulate_probe(bars, sigs, exit_mode=em)

            for t in trades:
                all_trades.append(t)
                d = t.entry_time.year * 10000 + t.entry_time.month * 100 + t.entry_time.day
                if d in green_days: green_trades.append(t)
                elif d in red_days: red_trades.append(t)
                else: flat_trades.append(t)

        print_metrics(metrics(all_trades, "ALL DAYS"))
        print_metrics(metrics(green_trades, "GREEN days (SPY >+0.15%)"))
        print_metrics(metrics(red_trades, "RED days (SPY <-0.15%)"))
        print_metrics(metrics(flat_trades, "FLAT days"))

    # ═══════════════════════════════════════════════════════════════
    # TEST 2: MONTHLY BREAKDOWN
    # ═══════════════════════════════════════════════════════════════
    print(f"\n\n{'='*70}")
    print("  TEST 2: MONTHLY BREAKDOWN — Long probe by calendar month")
    print(f"{'='*70}")

    # Use EOD exit for clarity
    monthly_trades = defaultdict(list)
    for sym in symbols:
        bars = bars_cache.get(sym)
        if not bars: continue
        sigs = detect_probe_signals(bars)
        for s in sigs: s.symbol = sym
        trades = simulate_probe(bars, sigs, exit_mode="eod")
        for t in trades:
            m = t.entry_time.strftime("%Y-%m")
            monthly_trades[m].append(t)

    print(f"\n  Exit mode: eod (hold to close)")
    for month in sorted(monthly_trades.keys()):
        print_metrics(metrics(monthly_trades[month], f"  {month}"))

    # ═══════════════════════════════════════════════════════════════
    # TEST 3: HABITAT — HIGH-RVOL vs LOW-RVOL NAMES
    # ═══════════════════════════════════════════════════════════════
    print(f"\n\n{'='*70}")
    print("  TEST 3: HABITAT — High RVOL (in-play proxy) vs Low RVOL")
    print(f"{'='*70}")

    for em in ["eod", "2R", "next_open"]:
        print(f"\n  Exit mode: {em}")
        high_rvol = []; low_rvol = []

        for sym in symbols:
            bars = bars_cache.get(sym)
            if not bars: continue
            sigs = detect_probe_signals(bars)
            for s in sigs: s.symbol = sym
            trades = simulate_probe(bars, sigs, exit_mode=em)

            for t in trades:
                if t.first_bar_rvol >= 1.5:
                    high_rvol.append(t)
                else:
                    low_rvol.append(t)

        print_metrics(metrics(high_rvol, "HIGH RVOL (>= 1.5x, in-play proxy)"))
        print_metrics(metrics(low_rvol, "LOW RVOL (< 1.5x, static names)"))

    # ═══════════════════════════════════════════════════════════════
    # TEST 4: HORIZON — SAME SIGNAL, DIFFERENT HOLD PERIODS
    # ═══════════════════════════════════════════════════════════════
    print(f"\n\n{'='*70}")
    print("  TEST 4: HORIZON — Same probe signal, different exit modes")
    print(f"{'='*70}")

    for em in exit_modes:
        all_trades = []
        for sym in symbols:
            bars = bars_cache.get(sym)
            if not bars: continue
            sigs = detect_probe_signals(bars)
            for s in sigs: s.symbol = sym
            trades = simulate_probe(bars, sigs, exit_mode=em)
            all_trades.extend(trades)
        print_metrics(metrics(all_trades, f"  {em}"))

    # ═══════════════════════════════════════════════════════════════
    # TEST 5: RS-FILTERED PROBE ON GREEN DAYS ONLY
    # ═══════════════════════════════════════════════════════════════
    print(f"\n\n{'='*70}")
    print("  TEST 5: BEST-CASE STACK — GREEN day + high RVOL + RS > 0")
    print(f"{'='*70}")

    for em in ["eod", "2R", "3R", "next_open"]:
        best_case = []
        for sym in symbols:
            bars = bars_cache.get(sym)
            if not bars: continue
            sigs = detect_probe_signals(bars)
            for s in sigs: s.symbol = sym
            trades = simulate_probe(bars, sigs, exit_mode=em)

            for t in trades:
                d = t.entry_time.year * 10000 + t.entry_time.month * 100 + t.entry_time.day
                if d in green_days and t.first_bar_rvol >= 1.5 and not math.isnan(t.spy_pct_at_entry) and t.spy_pct_at_entry > 0:
                    best_case.append(t)

        print_metrics(metrics(best_case, f"  GREEN+highRVOL+RS>0 [{em}]"))

    print(f"\n\n{'='*70}")
    print("  STUDY COMPLETE")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
