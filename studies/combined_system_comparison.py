"""
Combined Long + Short System Comparison.

Compares four system variants:
  1. Locked long baseline only  (VK + SC, Non-RED, Q>=2, <15:30)
  2. BDR baseline only          (all BDR shorts, no filters)
  3. BDR + big rejection wick   (rejection_wick_pct >= 0.30)
  4. Long baseline + BDR + big rejection wick  (combined two-sided)

For each: N, WR, PF, Exp, PnL, MaxDD, StopRate, QStopRate, train/test split.
For variant 4: contribution by long vs short book.

Train/test: odd dates = train, even dates = test (same as BDR filter study).

Usage:
    python -m alert_overlay.combined_system_comparison --universe all94
"""

import argparse
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import List

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import NaN, SetupId, SetupFamily, SETUP_DISPLAY_NAME, SETUP_FAMILY_MAP
from ..market_context import SECTOR_MAP, get_sector_etf

from .breakdown_retest_study import (
    BDRTrade,
    compute_bar_contexts,
    scan_for_breakdowns,
    simulate_exit,
    build_market_context_map,
)

DATA_DIR = Path(__file__).parent.parent / "data"
WATCHLIST_FILE = Path(__file__).parent.parent / "watchlist.txt"


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


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
        ranges_10d.append(day_range)
        day_info[d] = {"direction": direction, "spy_change_pct": change_pct}
    return day_info


# ── Unified trade wrapper ──

class UTrade:
    """Unified trade wrapper for both engine Trade and BDRTrade."""
    __slots__ = ("pnl_points", "pnl_rr", "exit_reason", "bars_held",
                 "entry_time", "entry_date", "side", "source")

    def __init__(self, pnl_points, pnl_rr, exit_reason, bars_held,
                 entry_time, side, source):
        self.pnl_points = pnl_points
        self.pnl_rr = pnl_rr
        self.exit_reason = exit_reason
        self.bars_held = bars_held
        self.entry_time = entry_time
        self.entry_date = entry_time.date() if entry_time else None
        self.side = side      # "LONG" or "SHORT"
        self.source = source  # "ENGINE" or "BDR"


def wrap_engine_trade(t: Trade) -> UTrade:
    return UTrade(
        pnl_points=t.pnl_points,
        pnl_rr=t.pnl_rr,
        exit_reason=t.exit_reason,
        bars_held=t.bars_held,
        entry_time=t.signal.timestamp,
        side="LONG" if t.signal.direction == 1 else "SHORT",
        source="ENGINE",
    )


def wrap_bdr_trade(t: BDRTrade) -> UTrade:
    return UTrade(
        pnl_points=t.pnl_points,
        pnl_rr=t.pnl_rr,
        exit_reason=t.exit_reason,
        bars_held=t.bars_held,
        entry_time=t.entry_time,
        side="SHORT",
        source="BDR",
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
        "n": n,
        "wr": len(wins) / n * 100,
        "pf": pf,
        "exp": sum(t.pnl_rr for t in trades) / n,
        "pnl": pnl,
        "max_dd": dd,
        "stop_rate": stopped / n * 100,
        "qstop_rate": qstop / n * 100,
    }


def split_train_test(trades: List[UTrade]):
    train = [t for t in trades if t.entry_date and t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date and t.entry_date.day % 2 == 0]
    return train, test


def print_row(label, m, indent="  "):
    print(f"{indent}{label:42s}  N={m['n']:4d}  WR={m['wr']:5.1f}%  "
          f"PF={pf_str(m['pf']):>6s}  Exp={m['exp']:+.2f}R  PnL={m['pnl']:+8.2f}  "
          f"MaxDD={m['max_dd']:7.2f}  Stop={m['stop_rate']:4.1f}%  QStop={m['qstop_rate']:4.1f}%")


# ── Main ──

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="all94")
    args = parser.parse_args()

    # Build symbol list (all 87 tradable symbols)
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    symbols = sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])

    print("=" * 125)
    print("COMBINED LONG + SHORT SYSTEM COMPARISON")
    print("=" * 125)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Long baseline: VK + SC, Non-RED, Q>=2, <15:30")
    print(f"Short candidate: BDR + big rejection wick (>=30%)")
    print(f"Train/test: odd dates = train, even dates = test\n")

    # ── Load index/sector bars ──
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    spy_day_info = classify_spy_days(spy_bars)

    # ── Run LONG baseline (engine) ──
    cfg = OverlayConfig()
    cfg.show_ema_scalp = False
    cfg.show_failed_bounce = False
    cfg.show_spencer = False
    cfg.show_ema_fpip = False
    cfg.show_sc_v2 = False

    print("Running long baseline (engine)...")
    raw_long_trades: List[Trade] = []
    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        raw_long_trades.extend(result.trades)

    # Apply locked baseline filters: Non-RED, Q>=2, <15:30
    def is_red(t):
        return spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == "RED"
    def signal_hhmm(t):
        ts = t.signal.timestamp
        return ts.hour * 100 + ts.minute

    locked_long = [t for t in raw_long_trades
                   if not is_red(t)
                   and t.signal.quality_score >= 2
                   and signal_hhmm(t) < 1530]

    long_utrades = [wrap_engine_trade(t) for t in locked_long]
    print(f"  Long baseline: {len(long_utrades)} trades")

    # ── Run BDR scanner ──
    print("Running BDR scanner...")
    all_bdr_trades: List[BDRTrade] = []
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
            simulate_exit(t, bars, ctxs)
        all_bdr_trades.extend(candidates)

    # BDR baseline (all)
    bdr_baseline_utrades = [wrap_bdr_trade(t) for t in all_bdr_trades]
    print(f"  BDR baseline: {len(bdr_baseline_utrades)} trades")

    # BDR + big rejection wick
    bdr_wick = [t for t in all_bdr_trades if t.rejection_wick_pct >= 0.30]
    bdr_wick_utrades = [wrap_bdr_trade(t) for t in bdr_wick]
    print(f"  BDR + big wick: {len(bdr_wick_utrades)} trades")

    # Combined
    combined = long_utrades + bdr_wick_utrades
    print(f"  Combined: {len(combined)} trades\n")

    # ── Define variants ──
    variants = [
        ("1. Locked long baseline",         long_utrades),
        ("2. BDR baseline (shorts only)",    bdr_baseline_utrades),
        ("3. BDR + big rej wick (shorts)",   bdr_wick_utrades),
        ("4. Long + BDR wick (combined)",    combined),
    ]

    # ════════════════════════════════════════════════════════════
    #  FULL UNIVERSE
    # ════════════════════════════════════════════════════════════
    print("=" * 125)
    print("FULL UNIVERSE")
    print("=" * 125)
    for label, trades in variants:
        m = compute_metrics(trades)
        print_row(label, m)

    # ════════════════════════════════════════════════════════════
    #  TRAIN SPLIT
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 125)
    print("TRAIN SPLIT (odd dates)")
    print("=" * 125)
    for label, trades in variants:
        train, _ = split_train_test(trades)
        m = compute_metrics(train)
        print_row(label, m)

    # ════════════════════════════════════════════════════════════
    #  TEST SPLIT
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 125)
    print("TEST SPLIT (even dates)")
    print("=" * 125)
    for label, trades in variants:
        _, test = split_train_test(trades)
        m = compute_metrics(test)
        print_row(label, m)

    # ════════════════════════════════════════════════════════════
    #  STABILITY CHECK
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 125)
    print("STABILITY CHECK: TRAIN vs TEST")
    print("=" * 125)
    print(f"\n  {'Variant':42s}  {'Trn PF':>6s}  {'Tst PF':>6s}  {'Delta':>6s}  "
          f"{'Trn WR':>6s}  {'Tst WR':>6s}  {'Delta':>6s}  {'Stable?':>7s}")
    print(f"  {'-'*42}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*7}")

    for label, trades in variants:
        train, test = split_train_test(trades)
        tr = compute_metrics(train)
        te = compute_metrics(test)
        pf_d = te["pf"] - tr["pf"] if tr["pf"] < 900 and te["pf"] < 900 else float("nan")
        wr_d = te["wr"] - tr["wr"]
        stable = "YES" if (tr["pf"] >= 1.0 and te["pf"] >= 1.0 and abs(wr_d) < 5.0) else "NO"
        pf_d_s = f"{pf_d:+.2f}" if abs(pf_d) < 100 else "N/A"
        print(f"  {label:42s}  {pf_str(tr['pf']):>6s}  {pf_str(te['pf']):>6s}  {pf_d_s:>6s}  "
              f"{tr['wr']:5.1f}%  {te['wr']:5.1f}%  {wr_d:+5.1f}%  {stable:>7s}")

    # ════════════════════════════════════════════════════════════
    #  COMBINED SYSTEM: LONG vs SHORT CONTRIBUTION
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 125)
    print("VARIANT 4 — LONG vs SHORT BOOK CONTRIBUTION")
    print("=" * 125)

    combined_longs = [t for t in combined if t.side == "LONG"]
    combined_shorts = [t for t in combined if t.side == "SHORT"]

    print("\n  Full universe:")
    print_row("Long book", compute_metrics(combined_longs), indent="    ")
    print_row("Short book", compute_metrics(combined_shorts), indent="    ")
    print_row("Combined", compute_metrics(combined), indent="    ")

    # Train
    train_all, test_all = split_train_test(combined)
    train_l = [t for t in train_all if t.side == "LONG"]
    train_s = [t for t in train_all if t.side == "SHORT"]
    test_l = [t for t in test_all if t.side == "LONG"]
    test_s = [t for t in test_all if t.side == "SHORT"]

    print("\n  Train split:")
    print_row("Long book", compute_metrics(train_l), indent="    ")
    print_row("Short book", compute_metrics(train_s), indent="    ")
    print_row("Combined", compute_metrics(train_all), indent="    ")

    print("\n  Test split:")
    print_row("Long book", compute_metrics(test_l), indent="    ")
    print_row("Short book", compute_metrics(test_s), indent="    ")
    print_row("Combined", compute_metrics(test_all), indent="    ")

    # ════════════════════════════════════════════════════════════
    #  DAILY EQUITY CURVE (combined)
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 125)
    print("DAILY EQUITY — LONG ONLY vs COMBINED")
    print("=" * 125)

    for tag, tlist in [("Long only", long_utrades), ("Combined", combined)]:
        daily_pnl = defaultdict(float)
        for t in tlist:
            if t.entry_date:
                daily_pnl[t.entry_date] += t.pnl_points
        cum = pk = dd = 0.0
        print(f"\n  {tag}:")
        print(f"    {'Date':<12} {'DayPnL':>8} {'CumPnL':>8} {'DD':>6}")
        print(f"    {'-'*12} {'-'*8} {'-'*8} {'-'*6}")
        for d in sorted(daily_pnl.keys()):
            cum += daily_pnl[d]
            if cum > pk:
                pk = cum
            dd_now = pk - cum
            if dd_now > dd:
                dd = dd_now
            marker = " <--MaxDD" if dd_now > 0 and dd_now == dd and dd > 0.5 else ""
            print(f"    {d} {daily_pnl[d]:>+8.2f} {cum:>+8.2f} {dd_now:>6.2f}{marker}")

    # ════════════════════════════════════════════════════════════
    #  VERDICT
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 125)
    print("VERDICT")
    print("=" * 125)

    m_long = compute_metrics(long_utrades)
    m_short = compute_metrics(bdr_wick_utrades)
    m_comb = compute_metrics(combined)

    # Train/test stability for combined
    train_c, test_c = split_train_test(combined)
    mt_c = compute_metrics(train_c)
    ms_c = compute_metrics(test_c)

    print(f"""
  Long baseline:    N={m_long['n']:4d}  WR={m_long['wr']:5.1f}%  PF={pf_str(m_long['pf'])}  PnL={m_long['pnl']:+8.2f}  MaxDD={m_long['max_dd']:.2f}
  BDR + wick:       N={m_short['n']:4d}  WR={m_short['wr']:5.1f}%  PF={pf_str(m_short['pf'])}  PnL={m_short['pnl']:+8.2f}  MaxDD={m_short['max_dd']:.2f}
  Combined:         N={m_comb['n']:4d}  WR={m_comb['wr']:5.1f}%  PF={pf_str(m_comb['pf'])}  PnL={m_comb['pnl']:+8.2f}  MaxDD={m_comb['max_dd']:.2f}

  Train PF: {pf_str(mt_c['pf'])}  |  Test PF: {pf_str(ms_c['pf'])}  |  WR delta: {ms_c['wr'] - mt_c['wr']:+.1f}%
""")

    both_stable = mt_c["pf"] >= 1.0 and ms_c["pf"] >= 1.0
    short_adds_pnl = m_comb["pnl"] > m_long["pnl"]
    short_reduces_dd = m_comb["max_dd"] <= m_long["max_dd"] * 1.05  # allow 5% tolerance

    if both_stable and short_adds_pnl:
        print("  PASS: Combined system is the first credible two-sided candidate.")
        if short_reduces_dd:
            print("  BONUS: Short book also reduces max drawdown.")
        else:
            print(f"  NOTE: Short book increases MaxDD ({m_long['max_dd']:.1f} -> {m_comb['max_dd']:.1f}).")
        print("  Next step: engine integration of BDR with big-wick filter.")
    elif both_stable:
        print("  PARTIAL: Stable but short book does not add PnL. Needs more refinement.")
    else:
        print("  FAIL: Combined system not stable across train/test splits.")
        print("  Short book needs further development before integration.")


if __name__ == "__main__":
    main()
