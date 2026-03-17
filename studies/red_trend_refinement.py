"""
RED+TREND Staged Refinement Test.

Tests 5 BDR short variants inside RED+TREND only:
  1. Baseline (wick >= 30%)
  2. AM entry only (< 11:00)
  3. Retest vol ratio < 1.0 only
  4. Wick >= 40% only
  5. AM entry + retest vol ratio < 1.0

For each variant:
  - Core: N, WR, PF(R), Exp(R), TotalR, MaxDD(R), StopRate
  - Train/test split (odd/even date)
  - Robustness: excl best day, excl top symbol

All metrics R-primary.

Usage:
    python -m alert_overlay.red_trend_refinement
"""

import statistics
from collections import defaultdict
from pathlib import Path
from typing import List

from ..backtest import load_bars_from_csv
from ..models import Bar
from .breakdown_retest_study import (
    BDRTrade, compute_bar_contexts, scan_for_breakdowns,
    simulate_exit, build_market_context_map,
)
from ..market_context import SECTOR_MAP, get_sector_etf

DATA_DIR = Path(__file__).parent.parent / "data"
RISK_PER_TRADE = 100.0


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


def classify_spy_days(spy_bars):
    daily = defaultdict(list)
    for b in spy_bars:
        daily[b.timestamp.date()].append(b)
    day_info = {}
    for d in sorted(daily.keys()):
        bars = daily[d]
        o, c = bars[0].open, bars[-1].close
        h, lo = max(b.high for b in bars), min(b.low for b in bars)
        rng = h - lo
        chg = (c - o) / o * 100 if o > 0 else 0
        if chg > 0.05:
            direction = "GREEN"
        elif chg < -0.05:
            direction = "RED"
        else:
            direction = "FLAT"
        if rng > 0:
            cp = (c - lo) / rng
            character = "TREND" if (cp >= 0.75 or cp <= 0.25) else "CHOPPY"
        else:
            character = "CHOPPY"
        day_info[d] = {"direction": direction, "character": character, "chg": chg}
    return day_info


def compute_r(trades: List[BDRTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0, "pf_r": 0, "exp_r": 0, "total_r": 0,
                "max_dd_r": 0, "stop_rate": 0, "total_pts": 0}
    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in trades)
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf_r = gw / gl if gl > 0 else float("inf")
    cum = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl_rr
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    return {
        "n": n, "wr": len(wins) / n * 100, "pf_r": pf_r,
        "exp_r": total_r / n, "total_r": total_r,
        "max_dd_r": dd, "stop_rate": stopped / n * 100,
        "total_pts": sum(t.pnl_points for t in trades),
    }


# ── Variant definitions ──

VARIANTS = [
    ("1. Baseline (wick>=30%)",
     lambda t: True),
    ("2. AM entry only (<11:00)",
     lambda t: t.entry_time.hour * 100 + t.entry_time.minute < 1100),
    ("3. Retest vol ratio < 1.0",
     lambda t: t.retest_vol_ratio < 1.0),
    ("4. Wick >= 40%",
     lambda t: t.rejection_wick_pct >= 0.40),
    ("5. AM + retest vol < 1.0",
     lambda t: (t.entry_time.hour * 100 + t.entry_time.minute < 1100) and t.retest_vol_ratio < 1.0),
]


def print_r_header(indent="    "):
    print(f"{indent}{'Label':<36}  {'N':>4}  {'WR':>6}  {'PF(R)':>6}  {'Exp':>8}  "
          f"{'TotalR':>8}  {'MaxDD_R':>8}  {'StpR':>5}")
    print(f"{indent}{'-'*36}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  "
          f"{'-'*8}  {'-'*8}  {'-'*5}")


def print_r_row(label, m, indent="    "):
    print(f"{indent}{label:<36}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf_r']):>6s}  "
          f"{m['exp_r']:+.3f}R  {m['total_r']:+8.2f}R  {m['max_dd_r']:8.2f}R  "
          f"{m['stop_rate']:4.1f}%")


def main():
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    symbols = sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])

    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    sdi = classify_spy_days(spy_bars)
    rt_dates = set(d for d, info in sdi.items()
                   if info["direction"] == "RED" and info["character"] == "TREND")

    print("=" * 120)
    print("RED+TREND STAGED REFINEMENT TEST")
    print("=" * 120)
    print(f"Universe: {len(symbols)} symbols  |  Risk: ${RISK_PER_TRADE:.0f}/trade")
    print(f"RED+TREND days: {len(rt_dates)}")
    for d in sorted(rt_dates):
        info = sdi[d]
        print(f"  {d}  SPY {info['chg']:+.2f}%")

    # ── Scan ──
    print(f"\nScanning...")
    all_trades: List[BDRTrade] = []
    for sym in symbols:
        fpath = DATA_DIR / f"{sym}_5min.csv"
        if not fpath.exists():
            continue
        bars = load_bars_from_csv(str(fpath))
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        ctxs = compute_bar_contexts(bars)
        mkt = build_market_context_map(spy_bars, qqq_bars, bars, sec_bars)
        trades = scan_for_breakdowns(bars, ctxs, mkt, sym)
        for t in trades:
            simulate_exit(t, bars, ctxs)
        all_trades.extend(trades)

    # Wick >= 30% + RED+TREND
    rt_trades = [t for t in all_trades
                 if t.rejection_wick_pct >= 0.30 and t.entry_time.date() in rt_dates]
    print(f"  RED+TREND BDR (wick>=30%): {len(rt_trades)} trades\n")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 1: CORE METRICS — ALL VARIANTS
    # ═══════════════════════════════════════════════════════════════
    print("=" * 120)
    print("SECTION 1: CORE R METRICS — ALL VARIANTS")
    print("=" * 120)
    print()
    print_r_header()
    variant_trades = {}
    for label, filt in VARIANTS:
        ft = [t for t in rt_trades if filt(t)]
        variant_trades[label] = ft
        m = compute_r(ft)
        print_r_row(label, m)

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 2: PER-DAY CONTRIBUTION — ALL VARIANTS
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 2: PER-DAY CONTRIBUTION")
    print("=" * 120)

    for label, filt in VARIANTS:
        ft = variant_trades[label]
        day_grp = defaultdict(list)
        for t in ft:
            day_grp[t.entry_time.date()].append(t)
        print(f"\n  {label}:")
        print(f"    {'Date':<12}  {'N':>3}  {'WR':>6}  {'TotalR':>8}  {'MaxDD_R':>8}  {'StpR':>5}")
        print(f"    {'-'*12}  {'-'*3}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*5}")
        for d in sorted(day_grp.keys()):
            dt = day_grp[d]
            m = compute_r(dt)
            print(f"    {d}  {m['n']:3d}  {m['wr']:5.1f}%  {m['total_r']:+8.2f}R  "
                  f"{m['max_dd_r']:8.2f}R  {m['stop_rate']:4.1f}%")
        profitable_days = sum(1 for d in day_grp if compute_r(day_grp[d])["total_r"] > 0)
        print(f"    Profitable days: {profitable_days}/{len(day_grp)}")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 3: TRAIN/TEST SPLIT — ALL VARIANTS
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 3: TRAIN/TEST SPLIT (odd/even date)")
    print("=" * 120)
    print()
    print(f"    {'Label':<36}  {'Train N':>7}  {'Train PF':>8}  {'Train Exp':>9}  "
          f"{'Test N':>6}  {'Test PF':>7}  {'Test Exp':>8}  {'Verdict':>8}")
    print(f"    {'-'*36}  {'-'*7}  {'-'*8}  {'-'*9}  "
          f"{'-'*6}  {'-'*7}  {'-'*8}  {'-'*8}")

    for label, filt in VARIANTS:
        ft = variant_trades[label]
        train = [t for t in ft if t.entry_time.date().day % 2 == 1]
        test = [t for t in ft if t.entry_time.date().day % 2 == 0]
        mt = compute_r(train)
        ms = compute_r(test)
        stable = (mt["n"] >= 3 and ms["n"] >= 3
                  and mt["pf_r"] >= 1.0 and ms["pf_r"] >= 1.0)
        print(f"    {label:<36}  {mt['n']:7d}  {pf_str(mt['pf_r']):>8s}  {mt['exp_r']:+8.3f}R  "
              f"{ms['n']:6d}  {pf_str(ms['pf_r']):>7s}  {ms['exp_r']:+8.3f}R  "
              f"{'STABLE' if stable else 'UNSTABLE'}")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 4: ROBUSTNESS — EXCL BEST DAY & TOP SYMBOL
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 4: ROBUSTNESS — EXCL BEST DAY & TOP SYMBOL")
    print("=" * 120)

    for label, filt in VARIANTS:
        ft = variant_trades[label]
        if not ft:
            print(f"\n  {label}: NO TRADES")
            continue

        m_base = compute_r(ft)

        # Find best day and top symbol for this variant
        day_grp = defaultdict(list)
        sym_r = defaultdict(float)
        for t in ft:
            day_grp[t.entry_time.date()].append(t)
            sym_r[t.sym] += t.pnl_rr
        best_day = max(day_grp.keys(), key=lambda d: compute_r(day_grp[d])["total_r"])
        best_day_r = compute_r(day_grp[best_day])["total_r"]
        top_sym = max(sym_r, key=sym_r.get)
        top_sym_r = sym_r[top_sym]

        no_best_day = [t for t in ft if t.entry_time.date() != best_day]
        no_top_sym = [t for t in ft if t.sym != top_sym]
        no_both = [t for t in ft if t.entry_time.date() != best_day and t.sym != top_sym]

        m_nbd = compute_r(no_best_day)
        m_nts = compute_r(no_top_sym)
        m_nb = compute_r(no_both)

        print(f"\n  {label}")
        print(f"    Best day: {best_day} ({best_day_r:+.2f}R)  |  Top sym: {top_sym} ({top_sym_r:+.2f}R)")
        print_r_header()
        print_r_row("Full", m_base)
        print_r_row(f"Excl best day ({best_day})", m_nbd)
        print_r_row(f"Excl top sym ({top_sym})", m_nts)
        print_r_row(f"Excl both", m_nb)

        # Robustness verdicts
        robust_day = m_nbd["pf_r"] >= 1.0 and m_nbd["exp_r"] > 0
        robust_sym = m_nts["pf_r"] >= 1.0 and m_nts["exp_r"] > 0
        robust_both = m_nb["pf_r"] >= 1.0 and m_nb["exp_r"] > 0
        print(f"    Excl best day: {'PASS' if robust_day else 'FAIL'}  |  "
              f"Excl top sym: {'PASS' if robust_sym else 'FAIL'}  |  "
              f"Excl both: {'PASS' if robust_both else 'FAIL'}")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 5: SYMBOL BREADTH — ALL VARIANTS
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 5: SYMBOL BREADTH")
    print("=" * 120)

    for label, filt in VARIANTS:
        ft = variant_trades[label]
        sym_r = defaultdict(float)
        for t in ft:
            sym_r[t.sym] += t.pnl_rr
        pos = sum(1 for v in sym_r.values() if v > 0)
        neg = sum(1 for v in sym_r.values() if v <= 0)
        total = pos + neg
        pct = pos / total * 100 if total > 0 else 0
        print(f"  {label:<36}  Syms: {total}  Positive: {pos} ({pct:.0f}%)  Negative: {neg}")

    # ═══════════════════════════════════════════════════════════════
    #  VERDICT
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("VERDICT — VARIANT COMPARISON")
    print("=" * 120)

    print(f"\n    {'Variant':<36}  {'N':>4}  {'PF(R)':>6}  {'Exp':>8}  {'TotalR':>8}  "
          f"{'MaxDD':>6}  {'StpR':>5}  {'Robust':>6}  {'T/T':>8}")
    print(f"    {'-'*36}  {'-'*4}  {'-'*6}  {'-'*8}  {'-'*8}  "
          f"{'-'*6}  {'-'*5}  {'-'*6}  {'-'*8}")

    for label, filt in VARIANTS:
        ft = variant_trades[label]
        m = compute_r(ft)

        # Robustness: excl best day still positive R?
        day_grp = defaultdict(list)
        for t in ft:
            day_grp[t.entry_time.date()].append(t)
        if day_grp:
            best_day = max(day_grp.keys(), key=lambda d: compute_r(day_grp[d])["total_r"])
            m_nbd = compute_r([t for t in ft if t.entry_time.date() != best_day])
            robust = m_nbd["pf_r"] >= 1.0 and m_nbd["exp_r"] > 0
        else:
            robust = False

        # Train/test
        train = [t for t in ft if t.entry_time.date().day % 2 == 1]
        test = [t for t in ft if t.entry_time.date().day % 2 == 0]
        mt = compute_r(train)
        ms = compute_r(test)
        stable = (mt["n"] >= 3 and ms["n"] >= 3
                  and mt["pf_r"] >= 1.0 and ms["pf_r"] >= 1.0)

        print(f"    {label:<36}  {m['n']:4d}  {pf_str(m['pf_r']):>6s}  {m['exp_r']:+.3f}R  "
              f"{m['total_r']:+8.2f}R  {m['max_dd_r']:5.2f}R  {m['stop_rate']:4.1f}%  "
              f"{'PASS' if robust else 'FAIL':>6s}  {'STABLE' if stable else 'UNSTABLE':>8s}")

    # Overall recommendation
    print(f"\n  RECOMMENDATION:")
    print(f"  Select the variant with best Exp(R) that maintains:")
    print(f"    - PF(R) >= 1.3 after excluding best day")
    print(f"    - Train/test both positive")
    print(f"    - N >= 15 (minimum for statistical meaning)")
    print(f"    - Stop rate < 30%")
    print()


if __name__ == "__main__":
    main()
