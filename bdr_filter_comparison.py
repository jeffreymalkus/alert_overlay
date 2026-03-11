"""
BDR Staged Filter Comparison.

Runs the breakdown-retest scanner once, then applies 6 progressive filter
combinations to the same trade pool. Reports full metrics + train/test split.

Filters (using current study thresholds):
  1. BDR baseline
  2. BDR + AM entry only             (time_bucket == "AM")
  3. BDR + big rejection wick only   (rejection_wick_pct >= 0.30)
  4. BDR + bearish tape only         (market_trend <= 0)
  5. BDR + AM + big rejection wick
  6. BDR + AM + big rejection wick + bearish tape

Train/test split: odd dates = train, even dates = test (deterministic,
no look-ahead, interleaved for minimal sampling bias).

Usage:
    python -m alert_overlay.bdr_filter_comparison --universe all94
"""

import argparse
import statistics
from pathlib import Path
from typing import List, Callable

from .breakdown_retest_study import (
    BDRTrade,
    DATA_DIR,
    WATCHLIST_FILE,
    compute_bar_contexts,
    scan_for_breakdowns,
    simulate_exit,
    build_market_context_map,
    load_bars_from_csv,
    load_watchlist,
    pf_str,
)
from .market_context import SECTOR_MAP, get_sector_etf


# ── Filter definitions ──

def f_am(t: BDRTrade) -> bool:
    return t.time_bucket == "AM"

def f_big_wick(t: BDRTrade) -> bool:
    return t.rejection_wick_pct >= 0.30

def f_bearish(t: BDRTrade) -> bool:
    return t.market_trend <= 0


VARIANTS = [
    ("1. Baseline",                    []),
    ("2. + AM entry",                  [f_am]),
    ("3. + Big rej wick",              [f_big_wick]),
    ("4. + Bearish tape",              [f_bearish]),
    ("5. + AM + Big wick",             [f_am, f_big_wick]),
    ("6. + AM + Big wick + Bearish",   [f_am, f_big_wick, f_bearish]),
]


def apply_filters(trades: List[BDRTrade], filters: List[Callable]) -> List[BDRTrade]:
    out = trades
    for fn in filters:
        out = [t for t in out if fn(t)]
    return out


# ── Metrics ──

def compute_full_metrics(trades: List[BDRTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0, "pf": 0, "exp": 0, "pnl": 0,
                "max_dd": 0, "stop_rate": 0, "qstop_rate": 0, "avg_hold": 0}
    wins = [t for t in trades if t.pnl_points > 0]
    losses = [t for t in trades if t.pnl_points <= 0]
    pnl = sum(t.pnl_points for t in trades)
    gw = sum(t.pnl_points for t in wins)
    gl = abs(sum(t.pnl_points for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    qstop = sum(1 for t in trades if t.exit_reason == "stop" and t.bars_held <= 3)
    # Max drawdown
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
        "avg_hold": statistics.mean(t.bars_held for t in trades),
    }


def split_train_test(trades: List[BDRTrade]):
    """Odd dates = train, even dates = test."""
    train, test = [], []
    for t in trades:
        day = t.entry_time.day
        if day % 2 == 1:
            train.append(t)
        else:
            test.append(t)
    return train, test


def print_row(label, m, indent="  "):
    print(f"{indent}{label:40s}  N={m['n']:4d}  WR={m['wr']:5.1f}%  "
          f"PF={pf_str(m['pf']):>6s}  Exp={m['exp']:+.2f}R  PnL={m['pnl']:+8.2f}  "
          f"MaxDD={m['max_dd']:7.2f}  Stop={m['stop_rate']:4.1f}%  QStop={m['qstop_rate']:4.1f}%")


# ── Main ──

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="all94")
    args = parser.parse_args()

    if args.universe == "all94":
        excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
        symbols = sorted([
            p.stem.replace("_5min", "")
            for p in DATA_DIR.glob("*_5min.csv")
            if p.stem.replace("_5min", "") not in excluded
        ])
    elif args.universe == "watchlist":
        symbols = load_watchlist()
    else:
        symbols = [s.strip().upper() for s in args.universe.split(",")]

    print("=" * 120)
    print("BDR STAGED FILTER COMPARISON")
    print("=" * 120)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Train/test split: odd dates = train, even dates = test\n")

    # Load SPY/QQQ/sector bars
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    # Scan all symbols
    all_trades: List[BDRTrade] = []
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
        all_trades.extend(candidates)

    print(f"Total BDR trades scanned: {len(all_trades)}\n")
    if not all_trades:
        print("NO TRADES FOUND.")
        return

    # ── Split ──
    train_all, test_all = split_train_test(all_trades)
    print(f"Train set: {len(train_all)} trades (odd dates)")
    print(f"Test  set: {len(test_all)} trades (even dates)\n")

    # ── Header ──
    print("=" * 120)
    print("FULL UNIVERSE (ALL TRADES)")
    print("=" * 120)
    hdr = (f"  {'Variant':40s}  {'N':>4s}  {'WR':>6s}  "
           f"{'PF':>6s}  {'Exp':>6s}  {'PnL':>8s}  "
           f"{'MaxDD':>7s}  {'Stop':>5s}  {'QStop':>6s}")
    sep = "  " + "-" * 40 + "  " + "  ".join(["-" * 4, "-" * 6, "-" * 6, "-" * 6,
                                                "-" * 8, "-" * 7, "-" * 5, "-" * 6])
    print(hdr)
    print(sep)

    for label, filters in VARIANTS:
        subset = apply_filters(all_trades, filters)
        m = compute_full_metrics(subset)
        print_row(label, m)

    # ── Train split ──
    print("\n" + "=" * 120)
    print("TRAIN SPLIT (odd dates)")
    print("=" * 120)
    print(hdr)
    print(sep)

    for label, filters in VARIANTS:
        subset = apply_filters(train_all, filters)
        m = compute_full_metrics(subset)
        print_row(label, m)

    # ── Test split ──
    print("\n" + "=" * 120)
    print("TEST SPLIT (even dates)")
    print("=" * 120)
    print(hdr)
    print(sep)

    for label, filters in VARIANTS:
        subset = apply_filters(test_all, filters)
        m = compute_full_metrics(subset)
        print_row(label, m)

    # ── Stability check ──
    print("\n" + "=" * 120)
    print("STABILITY CHECK: TRAIN vs TEST DELTA")
    print("=" * 120)
    print(f"\n  {'Variant':40s}  {'Train PF':>8s}  {'Test PF':>8s}  {'Delta':>7s}  "
          f"{'Train WR':>8s}  {'Test WR':>8s}  {'Delta':>7s}  {'Stable?':>8s}")
    print(f"  {'-'*40}  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*8}")

    for label, filters in VARIANTS:
        tr = compute_full_metrics(apply_filters(train_all, filters))
        te = compute_full_metrics(apply_filters(test_all, filters))
        pf_delta = te["pf"] - tr["pf"] if tr["pf"] < 900 and te["pf"] < 900 else float("nan")
        wr_delta = te["wr"] - tr["wr"]
        # "Stable" if both train and test PF > 1.0 and WR delta < 5%
        stable = "YES" if (tr["pf"] >= 1.0 and te["pf"] >= 1.0 and abs(wr_delta) < 5.0) else "NO"
        pf_d_str = f"{pf_delta:+.2f}" if abs(pf_delta) < 100 else "N/A"
        print(f"  {label:40s}  {pf_str(tr['pf']):>8s}  {pf_str(te['pf']):>8s}  {pf_d_str:>7s}  "
              f"{tr['wr']:7.1f}%  {te['wr']:7.1f}%  {wr_delta:+6.1f}%  {stable:>8s}")

    # ── Verdict ──
    print("\n" + "=" * 120)
    print("VERDICT")
    print("=" * 120)

    # Find the best stable variant
    best_label = None
    best_pf = 0.0
    for label, filters in VARIANTS:
        tr = compute_full_metrics(apply_filters(train_all, filters))
        te = compute_full_metrics(apply_filters(test_all, filters))
        full = compute_full_metrics(apply_filters(all_trades, filters))
        wr_delta = abs(te["wr"] - tr["wr"])
        if tr["pf"] >= 1.0 and te["pf"] >= 1.0 and wr_delta < 5.0:
            if full["pf"] > best_pf and full["n"] >= 50:
                best_pf = full["pf"]
                best_label = label

    if best_label:
        full_m = None
        for label, filters in VARIANTS:
            if label == best_label:
                full_m = compute_full_metrics(apply_filters(all_trades, filters))
                break
        print(f"\n  Best stable variant: {best_label}")
        print(f"  Full: N={full_m['n']}  WR={full_m['wr']:.1f}%  PF={pf_str(full_m['pf'])}  "
              f"PnL={full_m['pnl']:+.2f}  MaxDD={full_m['max_dd']:.2f}")
        print(f"  Recommendation: proceed with this variant for engine integration.")
    else:
        print(f"\n  No variant is both profitable and stable across train/test.")
        print(f"  Consider adjusting thresholds or adding more data.")


if __name__ == "__main__":
    main()
