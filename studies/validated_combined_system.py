"""
Validated Combined System — Consistent Execution Framework.

Aligns BDR short execution with the same corrected framework used for longs:
  - Dynamic slippage (SHORT_STRUCT family mult = 1.2)
  - Regime classification (SPY day type for regime breakdown)
  - Overlap / concurrency statistics
  - Long vs short contribution by regime
  - Train/test stability

Does NOT optimize anything. Uses exact same thresholds throughout.

Usage:
    python -m alert_overlay.validated_combined_system --universe all94
"""

import argparse
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from ..backtest import load_bars_from_csv, run_backtest, Trade, _compute_dynamic_slippage
from ..config import OverlayConfig
from ..models import NaN, Bar, SetupId, SetupFamily, SETUP_DISPLAY_NAME, SETUP_FAMILY_MAP
from ..market_context import SECTOR_MAP, get_sector_etf

from .breakdown_retest_study import (
    BDRTrade,
    compute_bar_contexts,
    scan_for_breakdowns,
    simulate_exit as bdr_sim_exit_orig,
    build_market_context_map,
    TIME_STOP_BARS,
    STOP_BUFFER_ATR,
    SESSION_END_HHMM,
)

DATA_DIR = Path(__file__).parent.parent / "data"


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


# ── Dynamic slippage for BDR trades ──

def simulate_exit_dynamic_slip(trade: BDRTrade, bars: List[Bar],
                                ctxs, cfg: OverlayConfig):
    """
    Re-simulate BDR exit with dynamic slippage matching the engine framework.
    Uses SHORT_STRUCT family multiplier.
    """
    entry_idx = trade.entry_idx
    entry_date = bars[entry_idx].timestamp.date()
    entry_bar = bars[entry_idx]

    # Dynamic entry slippage
    entry_atr = ctxs[entry_idx].i_atr if entry_idx < len(ctxs) else 0.5
    entry_slip = _compute_dynamic_slippage(
        cfg, entry_bar, SetupFamily.SHORT_STRUCT, entry_atr)
    comm = cfg.commission_per_share

    # Short entry: fill slightly worse (higher)
    filled_entry = trade.entry_price + (entry_slip + comm)

    for j in range(entry_idx + 1, len(bars)):
        b = bars[j]
        held = j - entry_idx

        # Day boundary
        if b.timestamp.date() != entry_date:
            prev = bars[j - 1]
            exit_atr = ctxs[j - 1].i_atr if j - 1 < len(ctxs) else entry_atr
            exit_slip = _compute_dynamic_slippage(
                cfg, prev, SetupFamily.SHORT_STRUCT, exit_atr)
            adj_exit = prev.close + (exit_slip + comm)  # short cover: slightly worse
            trade.exit_price = prev.close
            trade.exit_time = prev.timestamp
            trade.exit_reason = "eod"
            trade.bars_held = held - 1
            trade.pnl_points = filled_entry - adj_exit
            trade.pnl_rr = trade.pnl_points / trade.risk if trade.risk > 0 else 0
            return

        if b.time_hhmm >= SESSION_END_HHMM:
            exit_atr = ctxs[j].i_atr if j < len(ctxs) else entry_atr
            exit_slip = _compute_dynamic_slippage(
                cfg, b, SetupFamily.SHORT_STRUCT, exit_atr)
            adj_exit = b.close + (exit_slip + comm)
            trade.exit_price = b.close
            trade.exit_time = b.timestamp
            trade.exit_reason = "eod"
            trade.bars_held = held
            trade.pnl_points = filled_entry - adj_exit
            trade.pnl_rr = trade.pnl_points / trade.risk if trade.risk > 0 else 0
            return

        if b.high >= trade.stop_price:
            exit_atr = ctxs[j].i_atr if j < len(ctxs) else entry_atr
            exit_slip = _compute_dynamic_slippage(
                cfg, b, SetupFamily.SHORT_STRUCT, exit_atr)
            adj_exit = trade.stop_price + (exit_slip + comm)
            trade.exit_price = trade.stop_price
            trade.exit_time = b.timestamp
            trade.exit_reason = "stop"
            trade.bars_held = held
            trade.pnl_points = filled_entry - adj_exit
            trade.pnl_rr = trade.pnl_points / trade.risk if trade.risk > 0 else 0
            return

        if held >= TIME_STOP_BARS:
            exit_atr = ctxs[j].i_atr if j < len(ctxs) else entry_atr
            exit_slip = _compute_dynamic_slippage(
                cfg, b, SetupFamily.SHORT_STRUCT, exit_atr)
            adj_exit = b.close + (exit_slip + comm)
            trade.exit_price = b.close
            trade.exit_time = b.timestamp
            trade.exit_reason = "time"
            trade.bars_held = held
            trade.pnl_points = filled_entry - adj_exit
            trade.pnl_rr = trade.pnl_points / trade.risk if trade.risk > 0 else 0
            return

    # End of data
    last = bars[-1]
    exit_atr = ctxs[-1].i_atr if ctxs else entry_atr
    exit_slip = _compute_dynamic_slippage(
        cfg, last, SetupFamily.SHORT_STRUCT, exit_atr)
    adj_exit = last.close + (exit_slip + comm)
    trade.exit_price = last.close
    trade.exit_time = last.timestamp
    trade.exit_reason = "eod"
    trade.bars_held = len(bars) - 1 - entry_idx
    trade.pnl_points = filled_entry - adj_exit
    trade.pnl_rr = trade.pnl_points / trade.risk if trade.risk > 0 else 0


# ── SPY day classification ──

def classify_spy_days(spy_bars):
    daily = defaultdict(list)
    for b in spy_bars:
        daily[b.timestamp.date()].append(b)
    sorted_dates = sorted(daily.keys())
    day_info = {}
    ranges_10d = []
    for d in sorted_dates:
        bars = daily[d]
        day_open = bars[0].open
        day_close = bars[-1].close
        day_high = max(b.high for b in bars)
        day_low = min(b.low for b in bars)
        day_range = day_high - day_low
        change_pct = (day_close - day_open) / day_open * 100 if day_open > 0 else 0
        if change_pct > 0.05:
            direction = "GREEN"
        elif change_pct < -0.05:
            direction = "RED"
        else:
            direction = "FLAT"
        avg_range_10d = statistics.mean(ranges_10d[-10:]) if ranges_10d else day_range
        volatility = "HIGH_VOL" if len(ranges_10d) >= 5 and day_range > 1.5 * avg_range_10d else "NORMAL"
        if day_range > 0:
            close_pos = (day_close - day_low) / day_range
            character = "TREND" if (close_pos >= 0.75 or close_pos <= 0.25) else "CHOPPY"
        else:
            character = "CHOPPY"
        ranges_10d.append(day_range)
        day_info[d] = {"direction": direction, "volatility": volatility,
                       "character": character, "spy_change_pct": change_pct}
    return day_info


# ── Unified trade ──

class UTrade:
    __slots__ = ("pnl_points", "pnl_rr", "exit_reason", "bars_held",
                 "entry_time", "exit_time", "entry_date", "side", "source", "sym")

    def __init__(self, pnl_points, pnl_rr, exit_reason, bars_held,
                 entry_time, exit_time, side, source, sym=""):
        self.pnl_points = pnl_points
        self.pnl_rr = pnl_rr
        self.exit_reason = exit_reason
        self.bars_held = bars_held
        self.entry_time = entry_time
        self.exit_time = exit_time
        self.entry_date = entry_time.date() if entry_time else None
        self.side = side
        self.source = source
        self.sym = sym


def wrap_engine_trade(t: Trade, sym: str) -> UTrade:
    # Estimate exit time from entry + bars_held * 5min
    entry_ts = t.signal.timestamp
    exit_ts = t.exit_time if t.exit_time else entry_ts
    return UTrade(
        pnl_points=t.pnl_points, pnl_rr=t.pnl_rr,
        exit_reason=t.exit_reason, bars_held=t.bars_held,
        entry_time=entry_ts, exit_time=exit_ts,
        side="LONG" if t.signal.direction == 1 else "SHORT",
        source="ENGINE", sym=sym,
    )


def wrap_bdr_trade(t: BDRTrade) -> UTrade:
    exit_ts = t.exit_time if t.exit_time else t.entry_time
    return UTrade(
        pnl_points=t.pnl_points, pnl_rr=t.pnl_rr,
        exit_reason=t.exit_reason, bars_held=t.bars_held,
        entry_time=t.entry_time, exit_time=exit_ts,
        side="SHORT", source="BDR", sym=t.sym,
    )


# ── Metrics ──

def compute_metrics(trades: List[UTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0, "pf": 0, "exp": 0, "pnl": 0,
                "max_dd": 0, "stop_rate": 0, "qstop_rate": 0}
    wins = [t for t in trades if t.pnl_points > 0]
    losses = [t for t in trades if t.pnl_points <= 0]
    pnl = sum(t.pnl_points for t in trades)
    gw = sum(t.pnl_points for t in wins)
    gl = abs(sum(t.pnl_points for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    qstop = sum(1 for t in trades if t.exit_reason == "stop" and t.bars_held <= 3)
    cum = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl_points
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum
    return {
        "n": n, "wr": len(wins) / n * 100, "pf": pf,
        "exp": sum(t.pnl_rr for t in trades) / n,
        "pnl": pnl, "max_dd": dd,
        "stop_rate": stopped / n * 100,
        "qstop_rate": qstop / n * 100,
    }


def split_train_test(trades):
    train = [t for t in trades if t.entry_date and t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date and t.entry_date.day % 2 == 0]
    return train, test


def print_row(label, m, indent="  "):
    print(f"{indent}{label:42s}  N={m['n']:4d}  WR={m['wr']:5.1f}%  "
          f"PF={pf_str(m['pf']):>6s}  Exp={m['exp']:+.2f}R  PnL={m['pnl']:+8.2f}  "
          f"MaxDD={m['max_dd']:7.2f}  Stop={m['stop_rate']:4.1f}%  QStop={m['qstop_rate']:4.1f}%")


# ── Concurrency analysis ──

def compute_concurrency(trades: List[UTrade], bar_minutes: int = 5):
    """
    Compute overlap statistics: for each 5-min bar in the dataset,
    count how many trades are open. Returns stats.
    """
    if not trades:
        return {"avg_open": 0, "max_open": 0, "avg_long": 0, "avg_short": 0,
                "max_long": 0, "max_short": 0, "pct_flat": 100}

    # Build list of (entry_time, exit_time, side) intervals
    intervals = []
    for t in trades:
        if t.entry_time and t.exit_time:
            intervals.append((t.entry_time, t.exit_time, t.side))

    if not intervals:
        return {"avg_open": 0, "max_open": 0, "avg_long": 0, "avg_short": 0,
                "max_long": 0, "max_short": 0, "pct_flat": 100}

    # Generate all 5-min bars from first entry to last exit
    min_t = min(i[0] for i in intervals)
    max_t = max(i[1] for i in intervals)

    # Snap to 5-min grid
    delta = timedelta(minutes=bar_minutes)
    cursor = min_t.replace(second=0, microsecond=0)
    cursor = cursor - timedelta(minutes=cursor.minute % bar_minutes)

    total_bars = 0
    total_open = 0
    total_long = 0
    total_short = 0
    max_open = 0
    max_long = 0
    max_short = 0
    flat_bars = 0

    while cursor <= max_t:
        # Only count market hours (9:30-16:00 ET)
        hhmm = cursor.hour * 100 + cursor.minute
        if 930 <= hhmm <= 1555:
            n_open = 0
            n_long = 0
            n_short = 0
            for entry, exit_t, side in intervals:
                if entry <= cursor < exit_t:
                    n_open += 1
                    if side == "LONG":
                        n_long += 1
                    else:
                        n_short += 1
            total_bars += 1
            total_open += n_open
            total_long += n_long
            total_short += n_short
            if n_open > max_open:
                max_open = n_open
            if n_long > max_long:
                max_long = n_long
            if n_short > max_short:
                max_short = n_short
            if n_open == 0:
                flat_bars += 1

        cursor += delta

    avg_open = total_open / total_bars if total_bars > 0 else 0
    avg_long = total_long / total_bars if total_bars > 0 else 0
    avg_short = total_short / total_bars if total_bars > 0 else 0
    pct_flat = flat_bars / total_bars * 100 if total_bars > 0 else 100

    return {
        "avg_open": avg_open, "max_open": max_open,
        "avg_long": avg_long, "avg_short": avg_short,
        "max_long": max_long, "max_short": max_short,
        "pct_flat": pct_flat,
    }


# ── Main ──

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="all94")
    args = parser.parse_args()

    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    symbols = sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])

    cfg = OverlayConfig()

    print("=" * 125)
    print("VALIDATED COMBINED SYSTEM — CONSISTENT EXECUTION FRAMEWORK")
    print("=" * 125)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Long: VK + SC, Non-RED, Q>=2, <15:30  |  Short: BDR + big wick (>=30%)")
    print(f"Execution: dynamic slippage (bps={cfg.slip_bps}, SHORT_STRUCT mult={cfg.slip_family_mult_short_struct})")
    print(f"Train/test: odd dates = train, even dates = test\n")

    # Load index/sector bars
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    spy_day_info = classify_spy_days(spy_bars)

    # ── Long baseline (engine, same config) ──
    long_cfg = OverlayConfig()
    long_cfg.show_ema_scalp = False
    long_cfg.show_failed_bounce = False
    long_cfg.show_spencer = False
    long_cfg.show_ema_fpip = False
    long_cfg.show_sc_v2 = False

    print("Running long baseline (engine)...")
    raw_long_trades = []
    sym_for_long = {}
    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        result = run_backtest(bars, cfg=long_cfg, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        for t in result.trades:
            raw_long_trades.append(t)
            sym_for_long[id(t)] = sym

    # Apply locked baseline filters
    def is_red(t):
        return spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == "RED"
    def signal_hhmm(t):
        ts = t.signal.timestamp
        return ts.hour * 100 + ts.minute

    locked_long = [t for t in raw_long_trades
                   if not is_red(t)
                   and t.signal.quality_score >= 2
                   and signal_hhmm(t) < 1530]

    long_utrades = [wrap_engine_trade(t, sym_for_long[id(t)]) for t in locked_long]
    print(f"  Long baseline: {len(long_utrades)} trades")

    # ── BDR scanner with dynamic slippage ──
    print("Running BDR scanner (dynamic slippage)...")
    all_bdr_trades: List[BDRTrade] = []
    bdr_ctxs_map: Dict[str, tuple] = {}  # sym -> (bars, ctxs) for re-sim

    for sym in symbols:
        fpath = DATA_DIR / f"{sym}_5min.csv"
        if not fpath.exists():
            continue
        bars = load_bars_from_csv(str(fpath))
        if not bars or len(bars) < 30:
            continue
        ctxs = compute_bar_contexts(bars)
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        mkt_map = build_market_context_map(spy_bars, qqq_bars, bars, sec_bars)
        candidates = scan_for_breakdowns(bars, ctxs, mkt_map, sym)

        # Simulate exit with DYNAMIC slippage
        for t in candidates:
            simulate_exit_dynamic_slip(t, bars, ctxs, cfg)
        all_bdr_trades.extend(candidates)

    # Filter: big rejection wick >= 0.30
    bdr_wick = [t for t in all_bdr_trades if t.rejection_wick_pct >= 0.30]
    bdr_wick_utrades = [wrap_bdr_trade(t) for t in bdr_wick]
    print(f"  BDR + big wick (dynamic slip): {len(bdr_wick_utrades)} trades")

    combined = long_utrades + bdr_wick_utrades
    print(f"  Combined: {len(combined)} trades\n")

    # ════════════════════════════════════════════════════════════
    #  SECTION 1: FULL UNIVERSE COMPARISON
    # ════════════════════════════════════════════════════════════
    variants = [
        ("1. Long baseline",              long_utrades),
        ("2. BDR + big wick (dyn slip)",   bdr_wick_utrades),
        ("3. Combined",                    combined),
    ]

    print("=" * 125)
    print("SECTION 1: FULL UNIVERSE — CONSISTENT EXECUTION")
    print("=" * 125)
    for label, trades in variants:
        print_row(label, compute_metrics(trades))

    # ════════════════════════════════════════════════════════════
    #  SECTION 2: OVERLAP / CONCURRENCY
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 125)
    print("SECTION 2: OVERLAP & CONCURRENCY STATISTICS")
    print("=" * 125)

    for tag, tlist in [("Long only", long_utrades),
                       ("Short only", bdr_wick_utrades),
                       ("Combined", combined)]:
        c = compute_concurrency(tlist)
        print(f"\n  {tag}:")
        print(f"    Avg open trades:  {c['avg_open']:.2f}")
        print(f"    Max simultaneous: {c['max_open']}")
        print(f"    Avg long open:    {c['avg_long']:.2f}")
        print(f"    Avg short open:   {c['avg_short']:.2f}")
        print(f"    Max long:         {c['max_long']}")
        print(f"    Max short:        {c['max_short']}")
        print(f"    Pct flat (no pos):{c['pct_flat']:.1f}%")

    # Per-day concurrency
    print("\n  Per-day trade counts (combined):")
    day_counts = defaultdict(lambda: {"long": 0, "short": 0})
    for t in combined:
        if t.entry_date:
            day_counts[t.entry_date][t.side.lower()] += 1

    print(f"    {'Date':<12}  {'Long':>5}  {'Short':>5}  {'Total':>5}")
    print(f"    {'-'*12}  {'-'*5}  {'-'*5}  {'-'*5}")
    for d in sorted(day_counts.keys()):
        dc = day_counts[d]
        print(f"    {d}  {dc['long']:5d}  {dc['short']:5d}  {dc['long']+dc['short']:5d}")

    # ════════════════════════════════════════════════════════════
    #  SECTION 3: CONTRIBUTION BY REGIME
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 125)
    print("SECTION 3: CONTRIBUTION BY REGIME")
    print("=" * 125)

    regimes = ["GREEN+TREND", "GREEN+CHOPPY", "RED+TREND", "RED+CHOPPY",
               "FLAT+TREND", "FLAT+CHOPPY"]

    def get_regime(t):
        info = spy_day_info.get(t.entry_date, {})
        direction = info.get("direction", "UNK")
        character = info.get("character", "UNK")
        return f"{direction}+{character}"

    # Build regime groups for each book
    for book_name, book_trades in [("Long book", long_utrades),
                                    ("Short book", bdr_wick_utrades),
                                    ("Combined", combined)]:
        groups = defaultdict(list)
        for t in book_trades:
            groups[get_regime(t)].append(t)

        print(f"\n  {book_name}:")
        print(f"    {'Regime':<16}  {'N':>4}  {'WR':>6}  {'PF':>6}  {'PnL':>8}  {'StpR':>5}  {'QStpR':>5}")
        print(f"    {'-'*16}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*5}  {'-'*5}")
        for regime in regimes:
            if regime not in groups:
                continue
            m = compute_metrics(groups[regime])
            print(f"    {regime:<16}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  "
                  f"{m['pnl']:+8.2f}  {m['stop_rate']:4.1f}%  {m['qstop_rate']:4.1f}%")
        # Total
        m = compute_metrics(book_trades)
        print(f"    {'TOTAL':<16}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  "
              f"{m['pnl']:+8.2f}  {m['stop_rate']:4.1f}%  {m['qstop_rate']:4.1f}%")

    # Complementarity analysis
    print(f"\n  Complementarity analysis:")
    for regime in regimes:
        long_in_regime = [t for t in long_utrades if get_regime(t) == regime]
        short_in_regime = [t for t in bdr_wick_utrades if get_regime(t) == regime]
        ml = compute_metrics(long_in_regime)
        ms = compute_metrics(short_in_regime)
        if ml["n"] == 0 and ms["n"] == 0:
            continue
        long_contrib = "+" if ml["pnl"] > 0 else "-"
        short_contrib = "+" if ms["pnl"] > 0 else "-"
        complementary = "YES" if (ml["pnl"] > 0 or ms["pnl"] > 0) else "NO"
        hedged = "HEDGED" if ml["pnl"] * ms["pnl"] < 0 else ("ALIGNED" if ml["pnl"] > 0 and ms["pnl"] > 0 else "BOTH NEG")
        print(f"    {regime:<16}  Long: {long_contrib} ({ml['pnl']:+.2f}, N={ml['n']})  "
              f"Short: {short_contrib} ({ms['pnl']:+.2f}, N={ms['n']})  → {hedged}")

    # ════════════════════════════════════════════════════════════
    #  SECTION 4: TRAIN / TEST STABILITY
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 125)
    print("SECTION 4: TRAIN / TEST STABILITY")
    print("=" * 125)

    for label, trades in variants:
        train, test = split_train_test(trades)
        mt = compute_metrics(train)
        ms = compute_metrics(test)
        print(f"\n  {label}:")
        print_row("Train (odd dates)", mt, indent="    ")
        print_row("Test  (even dates)", ms, indent="    ")
        pf_d = ms["pf"] - mt["pf"] if mt["pf"] < 900 and ms["pf"] < 900 else float("nan")
        wr_d = ms["wr"] - mt["wr"]
        stable = mt["pf"] >= 1.0 and ms["pf"] >= 1.0 and abs(wr_d) < 5.0
        pf_d_s = f"{pf_d:+.2f}" if abs(pf_d) < 100 else "N/A"
        print(f"    PF delta: {pf_d_s}  |  WR delta: {wr_d:+.1f}%  |  "
              f"Stable: {'YES' if stable else 'NO'}")

    # Long/short within combined train/test
    train_c, test_c = split_train_test(combined)
    print(f"\n  Combined — Long vs Short book by split:")
    for split_name, split_trades in [("Train", train_c), ("Test", test_c)]:
        longs = [t for t in split_trades if t.side == "LONG"]
        shorts = [t for t in split_trades if t.side == "SHORT"]
        ml = compute_metrics(longs)
        ms = compute_metrics(shorts)
        mc = compute_metrics(split_trades)
        print(f"    {split_name}:  Long N={ml['n']} PF={pf_str(ml['pf'])} PnL={ml['pnl']:+.2f}  |  "
              f"Short N={ms['n']} PF={pf_str(ms['pf'])} PnL={ms['pnl']:+.2f}  |  "
              f"Combined PF={pf_str(mc['pf'])} PnL={mc['pnl']:+.2f}")

    # ════════════════════════════════════════════════════════════
    #  SECTION 5: SLIPPAGE IMPACT COMPARISON
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 125)
    print("SECTION 5: SLIPPAGE IMPACT — FLAT vs DYNAMIC")
    print("=" * 125)

    # Re-run BDR with flat slippage for comparison
    flat_bdr_trades: List[BDRTrade] = []
    for sym in symbols:
        fpath = DATA_DIR / f"{sym}_5min.csv"
        if not fpath.exists():
            continue
        bars = load_bars_from_csv(str(fpath))
        if not bars or len(bars) < 30:
            continue
        ctxs = compute_bar_contexts(bars)
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        mkt_map = build_market_context_map(spy_bars, qqq_bars, bars, sec_bars)
        candidates = scan_for_breakdowns(bars, ctxs, mkt_map, sym)
        for t in candidates:
            bdr_sim_exit_orig(t, bars, ctxs)  # flat $0.02 slippage
        flat_bdr_trades.extend(candidates)

    flat_wick = [t for t in flat_bdr_trades if t.rejection_wick_pct >= 0.30]
    flat_wick_u = [wrap_bdr_trade(t) for t in flat_wick]

    m_flat = compute_metrics(flat_wick_u)
    m_dyn = compute_metrics(bdr_wick_utrades)

    print(f"\n  BDR + big wick — flat $0.02 slip:")
    print_row("Flat slippage", m_flat, indent="    ")
    print(f"\n  BDR + big wick — dynamic slippage:")
    print_row("Dynamic slippage", m_dyn, indent="    ")
    print(f"\n  Impact:  PnL {m_flat['pnl']:+.2f} -> {m_dyn['pnl']:+.2f}  "
          f"({m_dyn['pnl'] - m_flat['pnl']:+.2f})  |  "
          f"PF {pf_str(m_flat['pf'])} -> {pf_str(m_dyn['pf'])}")

    # ════════════════════════════════════════════════════════════
    #  VERDICT
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 125)
    print("VERDICT")
    print("=" * 125)

    m_long = compute_metrics(long_utrades)
    m_short = compute_metrics(bdr_wick_utrades)
    m_comb = compute_metrics(combined)
    train_c, test_c = split_train_test(combined)
    mt_c = compute_metrics(train_c)
    ms_c = compute_metrics(test_c)

    print(f"""
  Consistent execution (dynamic slippage on both books):

  Long baseline:    N={m_long['n']:4d}  WR={m_long['wr']:5.1f}%  PF={pf_str(m_long['pf'])}  PnL={m_long['pnl']:+8.2f}  MaxDD={m_long['max_dd']:.2f}
  BDR + wick:       N={m_short['n']:4d}  WR={m_short['wr']:5.1f}%  PF={pf_str(m_short['pf'])}  PnL={m_short['pnl']:+8.2f}  MaxDD={m_short['max_dd']:.2f}
  Combined:         N={m_comb['n']:4d}  WR={m_comb['wr']:5.1f}%  PF={pf_str(m_comb['pf'])}  PnL={m_comb['pnl']:+8.2f}  MaxDD={m_comb['max_dd']:.2f}

  Train PF: {pf_str(mt_c['pf'])}  |  Test PF: {pf_str(ms_c['pf'])}  |  WR delta: {ms_c['wr'] - mt_c['wr']:+.1f}%
""")

    both_stable = mt_c["pf"] >= 1.0 and ms_c["pf"] >= 1.0
    short_adds_pnl = m_comb["pnl"] > m_long["pnl"]
    slip_survives = m_dyn["pf"] >= 1.0

    checks = [
        ("Dynamic slippage survival", slip_survives),
        ("Train/test stability", both_stable),
        ("Short book adds PnL", short_adds_pnl),
    ]

    all_pass = True
    for name, passed in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {name}")

    if all_pass:
        print(f"\n  ALL CHECKS PASS. This is a validated two-sided integration candidate.")
        print(f"  Ready for engine integration of BDR + big-wick filter.")
    else:
        print(f"\n  NOT ALL CHECKS PASS. Review failures before engine integration.")


if __name__ == "__main__":
    main()
