"""
Long + Short Portfolio Comparison — R-Normalized.

Compares three portfolio variants:
  A. Long book only (VK+SC, locked filters)
  B. Long + RED+TREND BDR baseline (wick>=30%)
  C. Long + RED+TREND BDR AM-only (wick>=30%, entry<11:00)

Short trades from standalone BDR scanner (richer features, matches study).
Long trades from integrated engine (locked filters).

Reports per variant:
  - Core: N, WR, PF(R), Exp(R), TotalR, MaxDD(R), StopRate
  - Train/test split
  - Contribution by long vs short book
  - Robustness: excl best day, excl top symbol

Usage:
    python -m alert_overlay.long_plus_short_comparison
"""

import statistics
from collections import defaultdict
from pathlib import Path
from typing import List
from dataclasses import dataclass

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import SetupId
from ..market_context import SECTOR_MAP, get_sector_etf
from .breakdown_retest_study import (
    BDRTrade, compute_bar_contexts, scan_for_breakdowns,
    simulate_exit, build_market_context_map,
)

DATA_DIR = Path(__file__).parent.parent / "data"
RISK_PER_TRADE = 100.0


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


# ── Unified trade wrapper ──

@dataclass
class PTrade:
    """Portfolio trade — unified wrapper for both long engine trades and short BDR trades."""
    pnl_rr: float
    pnl_points: float
    exit_reason: str
    bars_held: int
    entry_time: object  # datetime
    entry_date: object  # date
    sym: str
    side: str           # "LONG" or "SHORT"
    book: str           # "long" or "short"
    quality: int = 0


def from_engine_trade(t: Trade, sym: str) -> PTrade:
    return PTrade(
        pnl_rr=t.pnl_rr,
        pnl_points=t.pnl_points,
        exit_reason=t.exit_reason,
        bars_held=t.bars_held,
        entry_time=t.signal.timestamp,
        entry_date=t.signal.timestamp.date(),
        sym=sym,
        side="LONG",
        book="long",
        quality=t.signal.quality_score,
    )


def from_bdr_trade(t: BDRTrade) -> PTrade:
    return PTrade(
        pnl_rr=t.pnl_rr,
        pnl_points=t.pnl_points,
        exit_reason=t.exit_reason,
        bars_held=t.bars_held,
        entry_time=t.entry_time,
        entry_date=t.entry_time.date(),
        sym=t.sym,
        side="SHORT",
        book="short",
    )


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

def compute_r(trades: List[PTrade]) -> dict:
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
    print(f"{indent}{'Label':<40}  {'N':>4}  {'WR':>6}  {'PF(R)':>6}  {'Exp':>8}  "
          f"{'TotalR':>8}  {'MaxDD_R':>8}  {'StpR':>5}")
    print(f"{indent}{'-'*40}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  "
          f"{'-'*8}  {'-'*8}  {'-'*5}")


def print_r_row(label, m, indent="    "):
    print(f"{indent}{label:<40}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf_r']):>6s}  "
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
    print("LONG + SHORT PORTFOLIO COMPARISON — R-NORMALIZED")
    print("=" * 120)
    print(f"Universe: {len(symbols)} symbols  |  Risk: ${RISK_PER_TRADE:.0f}/trade")
    print(f"RED+TREND days: {len(rt_dates)}")
    for d in sorted(rt_dates):
        info = sdi[d]
        print(f"  {d}  SPY {info['chg']:+.2f}%")

    # ═══════════════════════════════════════════════════════════════
    #  LONG BOOK — Engine backtest (VK+SC, locked filters)
    # ═══════════════════════════════════════════════════════════════
    print(f"\nRunning long-only engine backtest...")
    cfg_long = OverlayConfig()
    cfg_long.show_ema_scalp = False
    cfg_long.show_failed_bounce = False
    cfg_long.show_spencer = False
    cfg_long.show_ema_fpip = False
    cfg_long.show_sc_v2 = False
    cfg_long.show_breakdown_retest = False

    long_trades: List[PTrade] = []
    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        result = run_backtest(bars, cfg=cfg_long, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        for t in result.trades:
            long_trades.append(from_engine_trade(t, sym))

    # Apply locked long filters: Non-RED, Q>=2, entry < 15:30
    def is_red(t):
        return sdi.get(t.entry_date, {}).get("direction") == "RED"

    long_filtered = []
    for t in long_trades:
        if t.side == "SHORT":
            continue
        hhmm = t.entry_time.hour * 100 + t.entry_time.minute
        if is_red(t):
            continue
        if t.quality < 2:
            continue
        if hhmm >= 1530:
            continue
        long_filtered.append(t)

    print(f"  Long candidates (pre-quality): {len(long_filtered)}")

    # ═══════════════════════════════════════════════════════════════
    #  SHORT BOOK — Standalone BDR scanner (RED+TREND only)
    # ═══════════════════════════════════════════════════════════════
    print(f"Running standalone BDR scanner...")
    all_bdr: List[BDRTrade] = []
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
        all_bdr.extend(trades)

    # Wick >= 30% + RED+TREND
    rt_bdr_baseline = [t for t in all_bdr
                       if t.rejection_wick_pct >= 0.30 and t.entry_time.date() in rt_dates]

    # AM-only variant: entry before 11:00
    rt_bdr_am = [t for t in rt_bdr_baseline
                 if t.entry_time.hour * 100 + t.entry_time.minute < 1100]

    print(f"  BDR all: {len(all_bdr)}")
    print(f"  RED+TREND baseline (wick>=30%): {len(rt_bdr_baseline)}")
    print(f"  RED+TREND AM-only: {len(rt_bdr_am)}")

    # Convert to PTrade
    short_baseline = [from_bdr_trade(t) for t in rt_bdr_baseline]
    short_am = [from_bdr_trade(t) for t in rt_bdr_am]

    # ═══════════════════════════════════════════════════════════════
    #  BUILD THREE PORTFOLIOS
    # ═══════════════════════════════════════════════════════════════
    portfolios = {
        "A. Long only":                     long_filtered,
        "B. Long + RT BDR baseline":        long_filtered + short_baseline,
        "C. Long + RT BDR AM-only":         long_filtered + short_am,
    }

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 1: CORE R METRICS
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 1: CORE R METRICS — ALL PORTFOLIOS")
    print("=" * 120)
    print()
    print_r_header()
    for name, trades in portfolios.items():
        m = compute_r(trades)
        print_r_row(name, m)

    # Also show the short books separately for reference
    print()
    print_r_header()
    print_r_row("  Short: RT BDR baseline", compute_r(short_baseline))
    print_r_row("  Short: RT BDR AM-only", compute_r(short_am))

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 2: CONTRIBUTION BY BOOK
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 2: CONTRIBUTION BY BOOK")
    print("=" * 120)

    for name, trades in portfolios.items():
        longs = [t for t in trades if t.book == "long"]
        shorts = [t for t in trades if t.book == "short"]
        ml = compute_r(longs)
        ms = compute_r(shorts)
        mt = compute_r(trades)

        print(f"\n  {name}:")
        print(f"    {'Book':<12}  {'N':>4}  {'PF(R)':>6}  {'Exp':>8}  {'TotalR':>8}  "
              f"{'MaxDD':>6}  {'% of TotalR':>11}")
        print(f"    {'-'*12}  {'-'*4}  {'-'*6}  {'-'*8}  {'-'*8}  "
              f"{'-'*6}  {'-'*11}")
        total = mt["total_r"] if mt["total_r"] != 0 else 1
        print(f"    {'LONG':<12}  {ml['n']:4d}  {pf_str(ml['pf_r']):>6s}  {ml['exp_r']:+.3f}R  "
              f"{ml['total_r']:+8.2f}R  {ml['max_dd_r']:5.2f}R  "
              f"{ml['total_r']/total*100:10.1f}%")
        if shorts:
            print(f"    {'SHORT':<12}  {ms['n']:4d}  {pf_str(ms['pf_r']):>6s}  {ms['exp_r']:+.3f}R  "
                  f"{ms['total_r']:+8.2f}R  {ms['max_dd_r']:5.2f}R  "
                  f"{ms['total_r']/total*100:10.1f}%")
        print(f"    {'COMBINED':<12}  {mt['n']:4d}  {pf_str(mt['pf_r']):>6s}  {mt['exp_r']:+.3f}R  "
              f"{mt['total_r']:+8.2f}R  {mt['max_dd_r']:5.2f}R  {'100.0':>10s}%")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 3: TRAIN/TEST SPLIT
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 3: TRAIN/TEST SPLIT (odd/even date)")
    print("=" * 120)
    print()
    print(f"    {'Portfolio':<40}  {'Train N':>7}  {'Train PF':>8}  {'Train Exp':>9}  "
          f"{'Test N':>6}  {'Test PF':>7}  {'Test Exp':>8}  {'Verdict':>8}")
    print(f"    {'-'*40}  {'-'*7}  {'-'*8}  {'-'*9}  "
          f"{'-'*6}  {'-'*7}  {'-'*8}  {'-'*8}")

    for name, trades in portfolios.items():
        train = [t for t in trades if t.entry_date.day % 2 == 1]
        test = [t for t in trades if t.entry_date.day % 2 == 0]
        mt = compute_r(train)
        ms = compute_r(test)
        stable = (mt["n"] >= 5 and ms["n"] >= 5
                  and mt["pf_r"] >= 1.0 and ms["pf_r"] >= 1.0)
        print(f"    {name:<40}  {mt['n']:7d}  {pf_str(mt['pf_r']):>8s}  {mt['exp_r']:+8.3f}R  "
              f"{ms['n']:6d}  {pf_str(ms['pf_r']):>7s}  {ms['exp_r']:+8.3f}R  "
              f"{'STABLE' if stable else 'UNSTABLE'}")

    # Also show long/short books train/test separately for B and C
    print(f"\n  Book-level train/test (Portfolio C: Long + RT BDR AM-only):")
    for book_name, book_trades in [("Long", long_filtered), ("Short (AM)", short_am)]:
        train = [t for t in book_trades if t.entry_date.day % 2 == 1]
        test = [t for t in book_trades if t.entry_date.day % 2 == 0]
        mt = compute_r(train)
        ms = compute_r(test)
        stable = (mt["n"] >= 3 and ms["n"] >= 3
                  and mt["pf_r"] >= 1.0 and ms["pf_r"] >= 1.0)
        print(f"    {book_name:<20}  Train: N={mt['n']:3d} PF(R)={pf_str(mt['pf_r']):>5s} Exp={mt['exp_r']:+.3f}R  "
              f"Test: N={ms['n']:3d} PF(R)={pf_str(ms['pf_r']):>5s} Exp={ms['exp_r']:+.3f}R  "
              f"{'STABLE' if stable else 'UNSTABLE'}")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 4: ROBUSTNESS — EXCL BEST DAY & TOP SYMBOL
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 4: ROBUSTNESS — EXCL BEST DAY & TOP SYMBOL")
    print("=" * 120)

    for name, trades in portfolios.items():
        if not trades:
            continue
        m_base = compute_r(trades)

        day_grp = defaultdict(list)
        sym_r = defaultdict(float)
        for t in trades:
            day_grp[t.entry_date].append(t)
            sym_r[t.sym] += t.pnl_rr

        best_day = max(day_grp.keys(), key=lambda d: compute_r(day_grp[d])["total_r"])
        best_day_r = compute_r(day_grp[best_day])["total_r"]
        top_sym = max(sym_r, key=sym_r.get)
        top_sym_r = sym_r[top_sym]

        no_best_day = [t for t in trades if t.entry_date != best_day]
        no_top_sym = [t for t in trades if t.sym != top_sym]
        no_both = [t for t in trades if t.entry_date != best_day and t.sym != top_sym]

        m_nbd = compute_r(no_best_day)
        m_nts = compute_r(no_top_sym)
        m_nb = compute_r(no_both)

        print(f"\n  {name}")
        print(f"    Best day: {best_day} ({best_day_r:+.2f}R)  |  Top sym: {top_sym} ({top_sym_r:+.2f}R)")
        print_r_header()
        print_r_row("Full", m_base)
        print_r_row(f"Excl best day ({best_day})", m_nbd)
        print_r_row(f"Excl top sym ({top_sym})", m_nts)
        print_r_row(f"Excl both", m_nb)

        robust_day = m_nbd["pf_r"] >= 1.0 and m_nbd["exp_r"] > 0
        robust_sym = m_nts["pf_r"] >= 1.0 and m_nts["exp_r"] > 0
        robust_both = m_nb["pf_r"] >= 1.0 and m_nb["exp_r"] > 0
        print(f"    Excl best day: {'PASS' if robust_day else 'FAIL'}  |  "
              f"Excl top sym: {'PASS' if robust_sym else 'FAIL'}  |  "
              f"Excl both: {'PASS' if robust_both else 'FAIL'}")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 5: DAILY EQUITY CURVE COMPARISON
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 5: DAILY EQUITY CURVE (R)")
    print("=" * 120)

    all_dates = sorted(set(t.entry_date for t in portfolios["C. Long + RT BDR AM-only"]))

    print(f"\n    {'Date':<12}  {'A (Long)':>10}  {'B (L+BDR)':>10}  {'C (L+AM)':>10}  "
          f"{'A cum':>8}  {'B cum':>8}  {'C cum':>8}")
    print(f"    {'-'*12}  {'-'*10}  {'-'*10}  {'-'*10}  "
          f"{'-'*8}  {'-'*8}  {'-'*8}")

    cum = {"A": 0.0, "B": 0.0, "C": 0.0}
    port_keys = ["A. Long only", "B. Long + RT BDR baseline", "C. Long + RT BDR AM-only"]
    port_short = ["A", "B", "C"]

    # Build day-by-day for all portfolios
    daily_pnl = {k: defaultdict(float) for k in port_keys}
    for pk in port_keys:
        for t in portfolios[pk]:
            daily_pnl[pk][t.entry_date] += t.pnl_rr

    for d in all_dates:
        vals = []
        for i, pk in enumerate(port_keys):
            day_r = daily_pnl[pk].get(d, 0.0)
            cum[port_short[i]] += day_r
            vals.append((day_r, cum[port_short[i]]))
        print(f"    {d}  {vals[0][0]:+10.2f}R  {vals[1][0]:+10.2f}R  {vals[2][0]:+10.2f}R  "
              f"{vals[0][1]:+8.2f}R  {vals[1][1]:+8.2f}R  {vals[2][1]:+8.2f}R")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 6: RED+TREND DAY HEDGE ANALYSIS
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 6: RED+TREND DAY ANALYSIS — DOES SHORT BOOK HEDGE?")
    print("=" * 120)

    print(f"\n    {'Date':<12}  {'Long R':>8}  {'Short(AM) R':>11}  {'Combined R':>10}  "
          f"{'Net effect':>10}")
    print(f"    {'-'*12}  {'-'*8}  {'-'*11}  {'-'*10}  {'-'*10}")

    for d in sorted(rt_dates):
        long_day = [t for t in long_filtered if t.entry_date == d]
        short_day = [t for t in short_am if t.entry_date == d]
        lr = sum(t.pnl_rr for t in long_day)
        sr = sum(t.pnl_rr for t in short_day)
        cr = lr + sr
        effect = "hedge" if sr > 0 and lr < 0 else ("boost" if sr > 0 else "drag")
        print(f"    {d}  {lr:+8.2f}R  {sr:+11.2f}R  {cr:+10.2f}R  {effect:>10s}")

    # ═══════════════════════════════════════════════════════════════
    #  VERDICT
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("VERDICT")
    print("=" * 120)

    m_a = compute_r(portfolios["A. Long only"])
    m_b = compute_r(portfolios["B. Long + RT BDR baseline"])
    m_c = compute_r(portfolios["C. Long + RT BDR AM-only"])

    # Key comparisons
    checks = [
        ("C improves TotalR over A",
         m_c["total_r"] > m_a["total_r"]),
        ("C improves PF(R) over A",
         m_c["pf_r"] > m_a["pf_r"]),
        ("C MaxDD(R) <= A MaxDD(R) + 3R",
         m_c["max_dd_r"] <= m_a["max_dd_r"] + 3.0),
        ("C Exp(R) > 0",
         m_c["exp_r"] > 0),
        ("C train/test both PF(R) >= 1.0",
         None),  # filled below
        ("C robust: excl best day PF(R) >= 1.0",
         None),  # filled below
        ("Short AM book Exp(R) > 0",
         compute_r(short_am)["exp_r"] > 0),
    ]

    # Fill in train/test check
    train_c = [t for t in portfolios["C. Long + RT BDR AM-only"] if t.entry_date.day % 2 == 1]
    test_c = [t for t in portfolios["C. Long + RT BDR AM-only"] if t.entry_date.day % 2 == 0]
    mt_c = compute_r(train_c)
    ms_c = compute_r(test_c)
    checks[4] = ("C train/test both PF(R) >= 1.0",
                  mt_c["pf_r"] >= 1.0 and ms_c["pf_r"] >= 1.0)

    # Fill in robustness check
    day_grp_c = defaultdict(list)
    for t in portfolios["C. Long + RT BDR AM-only"]:
        day_grp_c[t.entry_date].append(t)
    best_day_c = max(day_grp_c.keys(), key=lambda d: compute_r(day_grp_c[d])["total_r"])
    m_nbd_c = compute_r([t for t in portfolios["C. Long + RT BDR AM-only"]
                         if t.entry_date != best_day_c])
    checks[5] = ("C robust: excl best day PF(R) >= 1.0",
                  m_nbd_c["pf_r"] >= 1.0)

    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")

    print(f"\n  Summary comparison:")
    print(f"    A (Long only):    N={m_a['n']:3d}  PF(R)={pf_str(m_a['pf_r'])}  Exp={m_a['exp_r']:+.3f}R  "
          f"TotalR={m_a['total_r']:+.2f}  MaxDD={m_a['max_dd_r']:.2f}R")
    print(f"    B (L+BDR base):   N={m_b['n']:3d}  PF(R)={pf_str(m_b['pf_r'])}  Exp={m_b['exp_r']:+.3f}R  "
          f"TotalR={m_b['total_r']:+.2f}  MaxDD={m_b['max_dd_r']:.2f}R")
    print(f"    C (L+BDR AM):     N={m_c['n']:3d}  PF(R)={pf_str(m_c['pf_r'])}  Exp={m_c['exp_r']:+.3f}R  "
          f"TotalR={m_c['total_r']:+.2f}  MaxDD={m_c['max_dd_r']:.2f}R")

    delta_r = m_c["total_r"] - m_a["total_r"]
    delta_dd = m_c["max_dd_r"] - m_a["max_dd_r"]
    print(f"\n    C vs A:  ΔTotalR={delta_r:+.2f}R  ΔMaxDD={delta_dd:+.2f}R  "
          f"ΔExp={m_c['exp_r'] - m_a['exp_r']:+.3f}R")
    print()


if __name__ == "__main__":
    main()
