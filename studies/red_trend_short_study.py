"""
RED+TREND Short Study — R-Normalized.

Dedicated study of BDR shorts on RED+TREND days only.
Uses the standalone scanner (richer features) filtered to RED+TREND regime.

Reports:
  1. Core R metrics + contribution by symbol and day
  2. Sensitivity filters (wick size, quality, level type, vol ratio, time)
  3. Robustness (excl MELI, excl best day, excl top symbol)
  4. Feature distribution comparison (winners vs losers in R)

All metrics R-primary. Points shown for reference only.

Usage:
    python -m alert_overlay.red_trend_short_study
"""

import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import List, Dict, Optional

from ..backtest import load_bars_from_csv
from ..models import Bar
from .breakdown_retest_study import (
    BDRTrade, compute_bar_contexts, scan_for_breakdowns,
    simulate_exit, build_market_context_map,
    SWING_LOOKBACK, BD_MIN_CLOSE_DIST_ATR, BD_MIN_RANGE_FRAC,
    BD_MIN_VOL_FRAC, BD_CLOSE_IN_LOWER_PCT,
    RETEST_WINDOW, RETEST_MIN_APPROACH_ATR, RETEST_MAX_RECLAIM_ATR,
    REJECT_WINDOW, STOP_BUFFER_ATR, TIME_STOP_BARS,
)
from ..market_context import SECTOR_MAP, get_sector_etf

DATA_DIR = Path(__file__).parent.parent / "data"
RISK_PER_TRADE = 100.0


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


# ── SPY regime classifier ──

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


# ── R-metrics ──

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


def print_r_header(indent="    "):
    print(f"{indent}{'Label':<32}  {'N':>4}  {'WR':>6}  {'PF(R)':>6}  {'Exp':>8}  "
          f"{'TotalR':>8}  {'MaxDD_R':>8}  {'StpR':>5}")
    print(f"{indent}{'-'*32}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  "
          f"{'-'*8}  {'-'*8}  {'-'*5}")


def print_r_row(label, m, indent="    "):
    print(f"{indent}{label:<32}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf_r']):>6s}  "
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

    # Identify RED+TREND dates
    rt_dates = set(d for d, info in sdi.items()
                   if info["direction"] == "RED" and info["character"] == "TREND")

    print("=" * 120)
    print("RED+TREND SHORT STUDY — R-NORMALIZED")
    print("=" * 120)
    print(f"Universe: {len(symbols)} symbols  |  Risk: ${RISK_PER_TRADE:.0f}/trade  |  Scanner: standalone BDR")
    print(f"RED+TREND days in dataset: {len(rt_dates)}")
    for d in sorted(rt_dates):
        info = sdi[d]
        print(f"  {d}  SPY {info['chg']:+.2f}%")

    # ── Scan all symbols, collect trades, filter to RED+TREND ──
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

    # Apply big rejection wick filter (the locked BDR filter)
    wick_filtered = [t for t in all_trades if t.rejection_wick_pct >= 0.30]

    # Filter to RED+TREND only
    rt_trades = [t for t in wick_filtered
                 if t.entry_time.date() in rt_dates]

    # Also keep all-regime for reference
    all_wick = wick_filtered

    print(f"  All BDR + wick: {len(all_wick)}")
    print(f"  RED+TREND only: {len(rt_trades)}")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 1: RED+TREND CORE METRICS
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 1: RED+TREND BDR SHORTS — CORE R METRICS")
    print("=" * 120)

    m_rt = compute_r(rt_trades)
    m_all = compute_r(all_wick)

    print(f"\n  Reference (all regimes):")
    print_r_header()
    print_r_row("All regimes + wick", m_all)
    print_r_row("RED+TREND only", m_rt)

    # R-distribution
    if rt_trades:
        rr = [t.pnl_rr for t in rt_trades]
        print(f"\n  R-distribution (RED+TREND):")
        print(f"    Mean:   {statistics.mean(rr):+.3f}R")
        print(f"    Median: {statistics.median(rr):+.3f}R")
        print(f"    Stdev:  {statistics.stdev(rr):.3f}R" if len(rr) > 1 else "")
        print(f"    Min:    {min(rr):+.3f}R  Max: {max(rr):+.3f}R")

    # By exit reason
    print(f"\n  By exit reason:")
    exit_grp = defaultdict(list)
    for t in rt_trades:
        exit_grp[t.exit_reason].append(t)
    for reason in sorted(exit_grp.keys()):
        m = compute_r(exit_grp[reason])
        avg_held = statistics.mean(t.bars_held for t in exit_grp[reason])
        print(f"    {reason:<8}  N={m['n']:3d}  AvgR={m['exp_r']:+.3f}  "
              f"TotalR={m['total_r']:+.2f}  AvgBars={avg_held:.1f}")

    # ── Per-day contribution ──
    print(f"\n  Per-day contribution (R):")
    day_grp = defaultdict(list)
    for t in rt_trades:
        day_grp[t.entry_time.date()].append(t)
    print(f"    {'Date':<12}  {'N':>3}  {'WR':>6}  {'TotalR':>8}  {'MaxDD_R':>8}  {'StpR':>5}  {'SPY':>6}")
    print(f"    {'-'*12}  {'-'*3}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*5}  {'-'*6}")
    for d in sorted(day_grp.keys()):
        dt = day_grp[d]
        m = compute_r(dt)
        spy_chg = sdi.get(d, {}).get("chg", 0)
        print(f"    {d}  {m['n']:3d}  {m['wr']:5.1f}%  {m['total_r']:+8.2f}R  "
              f"{m['max_dd_r']:8.2f}R  {m['stop_rate']:4.1f}%  {spy_chg:+5.2f}%")

    # ── Per-symbol contribution ──
    sym_r = defaultdict(lambda: {"r": 0.0, "n": 0, "pts": 0.0})
    for t in rt_trades:
        sym_r[t.sym]["r"] += t.pnl_rr
        sym_r[t.sym]["n"] += 1
        sym_r[t.sym]["pts"] += t.pnl_points
    sorted_sr = sorted(sym_r.items(), key=lambda x: x[1]["r"], reverse=True)

    pos_syms = sum(1 for _, st in sym_r.items() if st["r"] > 0)
    neg_syms = sum(1 for _, st in sym_r.items() if st["r"] <= 0)

    print(f"\n  Per-symbol (top 15 by R):")
    print(f"    {'Sym':<8}  {'N':>3}  {'TotalR':>8}  {'AvgR':>7}  {'Pts':>8}")
    print(f"    {'-'*8}  {'-'*3}  {'-'*8}  {'-'*7}  {'-'*8}")
    for sym, st in sorted_sr[:15]:
        avg = st["r"] / st["n"]
        print(f"    {sym:<8}  {st['n']:3d}  {st['r']:+8.2f}R  {avg:+6.3f}R  {st['pts']:+8.2f}")
    print(f"\n  Bottom 10:")
    for sym, st in sorted_sr[-10:]:
        avg = st["r"] / st["n"]
        print(f"    {sym:<8}  {st['n']:3d}  {st['r']:+8.2f}R  {avg:+6.3f}R  {st['pts']:+8.2f}")
    print(f"\n  Symbols: {pos_syms} positive R, {neg_syms} negative R, {len(sym_r)} total")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 2: SENSITIVITY FILTERS (RED+TREND ONLY)
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 2: SENSITIVITY FILTERS (RED+TREND only)")
    print("=" * 120)

    filters = [
        ("Baseline (wick>=30%)",         lambda t: True),
        # Wick size
        ("Wick >= 35%",                  lambda t: t.rejection_wick_pct >= 0.35),
        ("Wick >= 40%",                  lambda t: t.rejection_wick_pct >= 0.40),
        # Level type
        ("Level = ORL",                  lambda t: t.level_type == "ORL"),
        ("Level = VWAP",                 lambda t: t.level_type == "VWAP"),
        ("Level = SWING",               lambda t: t.level_type == "SWING"),
        ("Level = ORL or VWAP",          lambda t: t.level_type in ("ORL", "VWAP")),
        # BD volume
        ("BD vol ratio >= 1.0",          lambda t: t.bd_vol_ratio >= 1.0),
        ("BD vol ratio >= 1.25",         lambda t: t.bd_vol_ratio >= 1.25),
        # Retest features
        ("Retest depth <= 80%",          lambda t: t.retest_depth_pct <= 0.80),
        ("Retest depth <= 60%",          lambda t: t.retest_depth_pct <= 0.60),
        ("Retest bars <= 4",             lambda t: t.retest_bar_count <= 4),
        ("Retest vol ratio < 1.0",       lambda t: t.retest_vol_ratio < 1.0),
        # Time
        ("AM entry (< 11:00)",           lambda t: t.entry_time.hour * 100 + t.entry_time.minute < 1100),
        ("MID+ entry (>= 11:00)",        lambda t: t.entry_time.hour * 100 + t.entry_time.minute >= 1100),
        # Context
        ("Below VWAP (dist < 0)",        lambda t: t.dist_vwap_atr < 0),
        ("Below EMA9 (dist < 0)",        lambda t: t.dist_ema9_atr < 0),
        ("Below both VWAP & EMA9",       lambda t: t.dist_vwap_atr < 0 and t.dist_ema9_atr < 0),
        # BD range
        ("BD range >= 1.0 ATR",          lambda t: t.bd_range_atr >= 1.0),
        ("BD range >= 1.5 ATR",          lambda t: t.bd_range_atr >= 1.5),
    ]

    print(f"\n")
    print_r_header()
    for label, filt in filters:
        ft = [t for t in rt_trades if filt(t)]
        m = compute_r(ft)
        print_r_row(label, m)

    # Combined promising filters
    print(f"\n  Combined filter tests:")
    print_r_header()

    combos = [
        ("Wick>=35% + below VWAP",
         lambda t: t.rejection_wick_pct >= 0.35 and t.dist_vwap_atr < 0),
        ("Wick>=35% + BD vol>=1.0",
         lambda t: t.rejection_wick_pct >= 0.35 and t.bd_vol_ratio >= 1.0),
        ("ORL/VWAP + below VWAP",
         lambda t: t.level_type in ("ORL", "VWAP") and t.dist_vwap_atr < 0),
        ("BD vol>=1.0 + below VWAP",
         lambda t: t.bd_vol_ratio >= 1.0 and t.dist_vwap_atr < 0),
        ("Wick>=35% + BD vol>=1.0 + below VWAP",
         lambda t: t.rejection_wick_pct >= 0.35 and t.bd_vol_ratio >= 1.0 and t.dist_vwap_atr < 0),
        ("Retest depth<=80% + below VWAP",
         lambda t: t.retest_depth_pct <= 0.80 and t.dist_vwap_atr < 0),
        ("AM + below VWAP",
         lambda t: (t.entry_time.hour * 100 + t.entry_time.minute < 1100) and t.dist_vwap_atr < 0),
    ]

    for label, filt in combos:
        ft = [t for t in rt_trades if filt(t)]
        m = compute_r(ft)
        print_r_row(label, m)

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 3: ROBUSTNESS CHECKS
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 3: ROBUSTNESS CHECKS")
    print("=" * 120)

    # Find best day and top symbol
    best_day = max(day_grp.keys(), key=lambda d: compute_r(day_grp[d])["total_r"])
    best_day_r = compute_r(day_grp[best_day])["total_r"]
    top_sym = sorted_sr[0][0] if sorted_sr else ""
    top_sym_r = sorted_sr[0][1]["r"] if sorted_sr else 0

    tests = [
        ("Baseline",               lambda t: True),
        (f"Excl MELI",             lambda t: t.sym != "MELI"),
        (f"Excl best day ({best_day})",  lambda t: t.entry_time.date() != best_day),
        (f"Excl top sym ({top_sym})",    lambda t: t.sym != top_sym),
        (f"Excl MELI + best day",  lambda t: t.sym != "MELI" and t.entry_time.date() != best_day),
        (f"Excl top-3 syms",       lambda t: t.sym not in {s[0] for s in sorted_sr[:3]}),
    ]

    print(f"\n  Best day: {best_day} ({best_day_r:+.2f}R)")
    print(f"  Top symbol: {top_sym} ({top_sym_r:+.2f}R)\n")
    print_r_header()
    for label, filt in tests:
        ft = [t for t in rt_trades if filt(t)]
        m = compute_r(ft)
        print_r_row(label, m)

    # Train/test for each robustness variant
    print(f"\n  Train/test stability:")
    for label, filt in tests:
        ft = [t for t in rt_trades if filt(t)]
        train = [t for t in ft if t.entry_time.date().day % 2 == 1]
        test = [t for t in ft if t.entry_time.date().day % 2 == 0]
        mt = compute_r(train)
        ms = compute_r(test)
        wr_d = ms["wr"] - mt["wr"] if mt["n"] > 0 and ms["n"] > 0 else 0
        stable = mt["n"] >= 5 and ms["n"] >= 5 and mt["pf_r"] >= 1.0 and ms["pf_r"] >= 1.0
        print(f"    {label:<32}  Train: N={mt['n']:3d} PF(R)={pf_str(mt['pf_r']):>5s} Exp={mt['exp_r']:+.3f}R  "
              f"Test: N={ms['n']:3d} PF(R)={pf_str(ms['pf_r']):>5s} Exp={ms['exp_r']:+.3f}R  "
              f"{'STABLE' if stable else 'UNSTABLE'}")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 4: FEATURE COMPARISON — WINNERS VS LOSERS
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 4: FEATURE COMPARISON — WINNERS vs LOSERS (RED+TREND)")
    print("=" * 120)

    w = [t for t in rt_trades if t.pnl_rr > 0]
    l = [t for t in rt_trades if t.pnl_rr <= 0]

    if w and l:
        features = [
            ("rejection_wick_pct",  lambda t: t.rejection_wick_pct),
            ("bd_range_atr",        lambda t: t.bd_range_atr),
            ("bd_vol_ratio",        lambda t: t.bd_vol_ratio),
            ("retest_depth_pct",    lambda t: t.retest_depth_pct),
            ("retest_bar_count",    lambda t: t.retest_bar_count),
            ("retest_vol_ratio",    lambda t: t.retest_vol_ratio),
            ("dist_vwap_atr",       lambda t: t.dist_vwap_atr),
            ("dist_ema9_atr",       lambda t: t.dist_ema9_atr),
            ("bars_held",           lambda t: t.bars_held),
        ]

        print(f"\n    {'Feature':<22}  {'Win mean':>10}  {'Loss mean':>10}  {'Delta':>8}  {'Separation':>10}")
        print(f"    {'-'*22}  {'-'*10}  {'-'*10}  {'-'*8}  {'-'*10}")

        for name, fn in features:
            w_vals = [fn(t) for t in w]
            l_vals = [fn(t) for t in l]
            w_mean = statistics.mean(w_vals)
            l_mean = statistics.mean(l_vals)
            delta = w_mean - l_mean
            # Effect size (Cohen's d approximation)
            all_vals = w_vals + l_vals
            pooled_std = statistics.stdev(all_vals) if len(all_vals) > 1 else 1
            effect = delta / pooled_std if pooled_std > 0 else 0
            direction = "strong" if abs(effect) > 0.5 else ("moderate" if abs(effect) > 0.3 else "weak")
            print(f"    {name:<22}  {w_mean:10.3f}  {l_mean:10.3f}  {delta:+8.3f}  {effect:+8.3f} ({direction})")

    # Level type breakdown
    print(f"\n  By level type:")
    for lt in ["ORL", "VWAP", "SWING"]:
        lt_trades = [t for t in rt_trades if t.level_type == lt]
        if not lt_trades:
            continue
        m = compute_r(lt_trades)
        print(f"    {lt:<8}  N={m['n']:3d}  PF(R)={pf_str(m['pf_r'])}  Exp={m['exp_r']:+.3f}R  "
              f"TotalR={m['total_r']:+.2f}")

    # ═══════════════════════════════════════════════════════════════
    #  VERDICT
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("VERDICT")
    print("=" * 120)

    # Key checks
    no_meli = compute_r([t for t in rt_trades if t.sym != "MELI"])
    no_best_day = compute_r([t for t in rt_trades if t.entry_time.date() != best_day])
    no_top3 = compute_r([t for t in rt_trades if t.sym not in {s[0] for s in sorted_sr[:3]}])

    checks = [
        ("RED+TREND PF(R) >= 1.0",           m_rt["pf_r"] >= 1.0),
        ("RED+TREND Exp > 0",                m_rt["exp_r"] > 0),
        ("Excl MELI: PF(R) >= 1.0",         no_meli["pf_r"] >= 1.0),
        ("Excl best day: PF(R) >= 1.0",     no_best_day["pf_r"] >= 1.0),
        ("Excl top-3 syms: PF(R) >= 1.0",   no_top3["pf_r"] >= 1.0),
        (">=3 RED+TREND days profitable",
         sum(1 for d in day_grp if compute_r(day_grp[d])["total_r"] > 0) >= 3),
        (">=50% symbols positive R",         pos_syms / (pos_syms + neg_syms) >= 0.5 if (pos_syms + neg_syms) > 0 else False),
    ]

    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")

    print(f"\n  Summary:")
    print(f"    RED+TREND days: {len(rt_dates)}")
    print(f"    Trades: {m_rt['n']}  PF(R)={pf_str(m_rt['pf_r'])}  Exp={m_rt['exp_r']:+.3f}R  "
          f"TotalR={m_rt['total_r']:+.2f}  MaxDD={m_rt['max_dd_r']:.2f}R")
    print(f"    Excl MELI: PF(R)={pf_str(no_meli['pf_r'])}  TotalR={no_meli['total_r']:+.2f}")
    print(f"    Excl best day: PF(R)={pf_str(no_best_day['pf_r'])}  TotalR={no_best_day['total_r']:+.2f}")
    print()


if __name__ == "__main__":
    main()
