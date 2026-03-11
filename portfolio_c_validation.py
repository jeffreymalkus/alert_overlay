"""
Portfolio C Validation — Frozen Two-Sided Candidate.

Portfolio C = Long book (VK+SC, locked filters) + RED+TREND BDR AM-only shorts.
Full validation with all metrics in R.

Sections:
  1. Core R metrics + train/test
  2. Day-level contribution
  3. Regime contribution
  4. Symbol contribution in R
  5. Robustness (excl best day, top symbol, both)
  6. Position overlap / concurrency
  7. (System spec produced separately as markdown)

Usage:
    python -m alert_overlay.portfolio_c_validation
"""

import statistics
from collections import defaultdict
from pathlib import Path
from typing import List
from dataclasses import dataclass

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import SetupId, SETUP_DISPLAY_NAME
from .market_context import SECTOR_MAP, get_sector_etf
from .breakdown_retest_study import (
    BDRTrade, compute_bar_contexts, scan_for_breakdowns,
    simulate_exit, build_market_context_map,
)

DATA_DIR = Path(__file__).parent / "data"
RISK_PER_TRADE = 100.0


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


@dataclass
class PTrade:
    pnl_rr: float
    pnl_points: float
    exit_reason: str
    bars_held: int
    entry_time: object
    exit_time: object
    entry_date: object
    sym: str
    side: str
    book: str
    setup_name: str = ""
    quality: int = 0


def from_engine_trade(t: Trade, sym: str) -> PTrade:
    setup_name = SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id))
    return PTrade(
        pnl_rr=t.pnl_rr, pnl_points=t.pnl_points,
        exit_reason=t.exit_reason, bars_held=t.bars_held,
        entry_time=t.signal.timestamp,
        exit_time=t.exit_time if t.exit_time else t.signal.timestamp,
        entry_date=t.signal.timestamp.date(),
        sym=sym, side="LONG", book="long",
        setup_name=setup_name, quality=t.signal.quality_score,
    )


def from_bdr_trade(t: BDRTrade) -> PTrade:
    return PTrade(
        pnl_rr=t.pnl_rr, pnl_points=t.pnl_points,
        exit_reason=t.exit_reason, bars_held=t.bars_held,
        entry_time=t.entry_time,
        exit_time=t.exit_time if hasattr(t, 'exit_time') and t.exit_time else t.entry_time,
        entry_date=t.entry_time.date(),
        sym=t.sym, side="SHORT", book="short",
        setup_name="BDR_SHORT",
    )


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
    print("PORTFOLIO C VALIDATION — FROZEN TWO-SIDED CANDIDATE")
    print("=" * 120)
    print(f"Universe: {len(symbols)} symbols  |  Risk: ${RISK_PER_TRADE:.0f}/trade")
    print(f"RED+TREND days: {len(rt_dates)}")
    for d in sorted(rt_dates):
        info = sdi[d]
        print(f"  {d}  SPY {info['chg']:+.2f}%")

    # ── LONG BOOK ──
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

    # ── SHORT BOOK ──
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

    # RED+TREND + wick>=30% + AM entry (<11:00)
    short_am = [from_bdr_trade(t) for t in all_bdr
                if t.rejection_wick_pct >= 0.30
                and t.entry_time.date() in rt_dates
                and t.entry_time.hour * 100 + t.entry_time.minute < 1100]

    portfolio = long_filtered + short_am

    print(f"  Long book: {len(long_filtered)}")
    print(f"  Short book (RT BDR AM): {len(short_am)}")
    print(f"  Portfolio C total: {len(portfolio)}")

    all_dates = sorted(set(t.entry_date for t in portfolio))
    print(f"  Date range: {all_dates[0]} to {all_dates[-1]}  ({len(all_dates)} active days)")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 1: CORE R METRICS + TRAIN/TEST
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 1: CORE R METRICS + TRAIN/TEST")
    print("=" * 120)

    m_all = compute_r(portfolio)
    m_long = compute_r(long_filtered)
    m_short = compute_r(short_am)

    print(f"\n  Portfolio C (combined):")
    print_r_header()
    print_r_row("Combined", m_all)
    print_r_row("Long book", m_long)
    print_r_row("Short book (AM BDR)", m_short)

    # R-distribution
    rr = [t.pnl_rr for t in portfolio]
    print(f"\n  R-distribution:")
    print(f"    Mean:   {statistics.mean(rr):+.3f}R")
    print(f"    Median: {statistics.median(rr):+.3f}R")
    print(f"    Stdev:  {statistics.stdev(rr):.3f}R")
    print(f"    Min:    {min(rr):+.3f}R  Max: {max(rr):+.3f}R")

    # By exit reason
    print(f"\n  By exit reason:")
    exit_grp = defaultdict(list)
    for t in portfolio:
        exit_grp[t.exit_reason].append(t)
    print(f"    {'Reason':<10}  {'N':>4}  {'AvgR':>8}  {'TotalR':>8}  {'AvgBars':>7}")
    print(f"    {'-'*10}  {'-'*4}  {'-'*8}  {'-'*8}  {'-'*7}")
    for reason in sorted(exit_grp.keys()):
        grp = exit_grp[reason]
        m = compute_r(grp)
        avg_bars = statistics.mean(t.bars_held for t in grp)
        print(f"    {reason:<10}  {m['n']:4d}  {m['exp_r']:+.3f}R  {m['total_r']:+8.2f}R  {avg_bars:7.1f}")

    # Train/test
    print(f"\n  Train/test split (odd/even date):")
    for label, trades in [("Combined", portfolio), ("Long", long_filtered), ("Short (AM)", short_am)]:
        train = [t for t in trades if t.entry_date.day % 2 == 1]
        test = [t for t in trades if t.entry_date.day % 2 == 0]
        mt = compute_r(train)
        ms = compute_r(test)
        stable = mt["n"] >= 3 and ms["n"] >= 3 and mt["pf_r"] >= 1.0 and ms["pf_r"] >= 1.0
        print(f"    {label:<20}  Train: N={mt['n']:3d} PF(R)={pf_str(mt['pf_r']):>5s} Exp={mt['exp_r']:+.3f}R  "
              f"Test: N={ms['n']:3d} PF(R)={pf_str(ms['pf_r']):>5s} Exp={ms['exp_r']:+.3f}R  "
              f"{'STABLE' if stable else 'UNSTABLE'}")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 2: DAY-LEVEL CONTRIBUTION
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 2: DAY-LEVEL CONTRIBUTION")
    print("=" * 120)

    day_grp = defaultdict(list)
    for t in portfolio:
        day_grp[t.entry_date].append(t)

    print(f"\n    {'Date':<12}  {'N':>3}  {'Long':>6}  {'Short':>6}  {'TotalR':>8}  {'CumR':>8}  {'Regime':<16}")
    print(f"    {'-'*12}  {'-'*3}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*16}")
    cum_r = 0.0
    profitable_days = 0
    for d in sorted(day_grp.keys()):
        trades_d = day_grp[d]
        long_r = sum(t.pnl_rr for t in trades_d if t.book == "long")
        short_r = sum(t.pnl_rr for t in trades_d if t.book == "short")
        day_r = long_r + short_r
        cum_r += day_r
        info = sdi.get(d, {})
        regime = f"{info.get('direction', '?')}+{info.get('character', '?')}"
        if day_r > 0:
            profitable_days += 1
        n_d = len(trades_d)
        print(f"    {d}  {n_d:3d}  {long_r:+6.2f}  {short_r:+6.2f}  {day_r:+8.2f}R  "
              f"{cum_r:+8.2f}R  {regime:<16}")

    print(f"\n    Profitable days: {profitable_days}/{len(day_grp)} "
          f"({profitable_days/len(day_grp)*100:.0f}%)")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 3: REGIME CONTRIBUTION
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 3: REGIME CONTRIBUTION")
    print("=" * 120)

    regime_grp = defaultdict(list)
    for t in portfolio:
        info = sdi.get(t.entry_date, {})
        regime = f"{info.get('direction', '?')}+{info.get('character', '?')}"
        regime_grp[regime].append(t)

    print(f"\n    {'Regime':<16}  {'N':>4}  {'Long':>4}  {'Short':>5}  {'PF(R)':>6}  "
          f"{'Exp':>8}  {'TotalR':>8}  {'%TotalR':>8}")
    print(f"    {'-'*16}  {'-'*4}  {'-'*4}  {'-'*5}  {'-'*6}  "
          f"{'-'*8}  {'-'*8}  {'-'*8}")
    total_portfolio_r = m_all["total_r"] if m_all["total_r"] != 0 else 1
    for regime in sorted(regime_grp.keys()):
        rg = regime_grp[regime]
        m = compute_r(rg)
        n_l = sum(1 for t in rg if t.book == "long")
        n_s = sum(1 for t in rg if t.book == "short")
        pct = m["total_r"] / total_portfolio_r * 100
        print(f"    {regime:<16}  {m['n']:4d}  {n_l:4d}  {n_s:5d}  {pf_str(m['pf_r']):>6s}  "
              f"{m['exp_r']:+.3f}R  {m['total_r']:+8.2f}R  {pct:+7.1f}%")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 4: SYMBOL CONTRIBUTION IN R
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 4: SYMBOL CONTRIBUTION IN R")
    print("=" * 120)

    sym_data = defaultdict(lambda: {"r": 0.0, "n": 0, "long_r": 0.0, "short_r": 0.0,
                                     "long_n": 0, "short_n": 0})
    for t in portfolio:
        sd = sym_data[t.sym]
        sd["r"] += t.pnl_rr
        sd["n"] += 1
        if t.book == "long":
            sd["long_r"] += t.pnl_rr
            sd["long_n"] += 1
        else:
            sd["short_r"] += t.pnl_rr
            sd["short_n"] += 1

    sorted_sym = sorted(sym_data.items(), key=lambda x: x[1]["r"], reverse=True)
    pos_syms = sum(1 for _, sd in sorted_sym if sd["r"] > 0)
    neg_syms = sum(1 for _, sd in sorted_sym if sd["r"] <= 0)

    print(f"\n  Top 15 by R:")
    print(f"    {'Sym':<8}  {'N':>3}  {'TotalR':>8}  {'LongR':>7}  {'ShortR':>7}  {'AvgR':>7}")
    print(f"    {'-'*8}  {'-'*3}  {'-'*8}  {'-'*7}  {'-'*7}  {'-'*7}")
    for sym, sd in sorted_sym[:15]:
        avg = sd["r"] / sd["n"]
        print(f"    {sym:<8}  {sd['n']:3d}  {sd['r']:+8.2f}R  {sd['long_r']:+7.2f}  "
              f"{sd['short_r']:+7.2f}  {avg:+7.3f}")

    print(f"\n  Bottom 10 by R:")
    for sym, sd in sorted_sym[-10:]:
        avg = sd["r"] / sd["n"]
        print(f"    {sym:<8}  {sd['n']:3d}  {sd['r']:+8.2f}R  {sd['long_r']:+7.2f}  "
              f"{sd['short_r']:+7.2f}  {avg:+7.3f}")

    print(f"\n  Breadth: {pos_syms} positive ({pos_syms/(pos_syms+neg_syms)*100:.0f}%), "
          f"{neg_syms} negative, {pos_syms+neg_syms} total")

    # Concentration
    abs_r = [abs(sd["r"]) for _, sd in sorted_sym]
    total_abs = sum(abs_r)
    if total_abs > 0:
        sorted_abs = sorted(abs_r, reverse=True)
        cum = 0.0
        for i, v in enumerate(sorted_abs):
            cum += v
            if cum >= total_abs * 0.5:
                print(f"  50% of absolute R from top {i+1} symbols")
                break
        top1_pct = sorted_abs[0] / total_abs * 100
        top5_pct = sum(sorted_abs[:5]) / total_abs * 100
        print(f"  Top-1 = {top1_pct:.1f}% of |R|  |  Top-5 = {top5_pct:.1f}% of |R|")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 5: ROBUSTNESS
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 5: ROBUSTNESS — EXCL BEST DAY, TOP SYMBOL, BOTH")
    print("=" * 120)

    best_day = max(day_grp.keys(), key=lambda d: sum(t.pnl_rr for t in day_grp[d]))
    best_day_r = sum(t.pnl_rr for t in day_grp[best_day])
    top_sym = sorted_sym[0][0]
    top_sym_r = sorted_sym[0][1]["r"]

    variants = [
        ("Full portfolio",                lambda t: True),
        (f"Excl best day ({best_day})",   lambda t: t.entry_date != best_day),
        (f"Excl top sym ({top_sym})",     lambda t: t.sym != top_sym),
        ("Excl both",                     lambda t: t.entry_date != best_day and t.sym != top_sym),
    ]

    print(f"\n  Best day: {best_day} ({best_day_r:+.2f}R)")
    print(f"  Top symbol: {top_sym} ({top_sym_r:+.2f}R)\n")
    print_r_header()
    for label, filt in variants:
        ft = [t for t in portfolio if filt(t)]
        m = compute_r(ft)
        print_r_row(label, m)

    # Train/test for each
    print(f"\n  Train/test per robustness variant:")
    for label, filt in variants:
        ft = [t for t in portfolio if filt(t)]
        train = [t for t in ft if t.entry_date.day % 2 == 1]
        test = [t for t in ft if t.entry_date.day % 2 == 0]
        mt = compute_r(train)
        ms = compute_r(test)
        stable = mt["n"] >= 3 and ms["n"] >= 3 and mt["pf_r"] >= 1.0 and ms["pf_r"] >= 1.0
        print(f"    {label:<40}  Train: N={mt['n']:3d} PF={pf_str(mt['pf_r']):>5s} Exp={mt['exp_r']:+.3f}R  "
              f"Test: N={ms['n']:3d} PF={pf_str(ms['pf_r']):>5s} Exp={ms['exp_r']:+.3f}R  "
              f"{'STABLE' if stable else 'UNSTABLE'}")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 6: POSITION OVERLAP / CONCURRENCY
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 6: POSITION OVERLAP / CONCURRENCY")
    print("=" * 120)

    # For each 5-min bar timestamp, count how many trades are open
    # Approximate: trade is "open" from entry_time for bars_held bars (each 5 min)
    from datetime import timedelta

    open_counts = defaultdict(int)
    long_counts = defaultdict(int)
    short_counts = defaultdict(int)
    for t in portfolio:
        # Each bar is 5 minutes
        for b in range(t.bars_held + 1):
            ts = t.entry_time + timedelta(minutes=5 * b)
            open_counts[ts] += 1
            if t.book == "long":
                long_counts[ts] += 1
            else:
                short_counts[ts] += 1

    if open_counts:
        max_conc = max(open_counts.values())
        avg_conc = statistics.mean(open_counts.values())
        max_long = max(long_counts.values()) if long_counts else 0
        max_short = max(short_counts.values()) if short_counts else 0

        print(f"\n  Max simultaneous positions: {max_conc}")
        print(f"  Avg simultaneous positions: {avg_conc:.2f}")
        print(f"  Max long concurrent: {max_long}")
        print(f"  Max short concurrent: {max_short}")

        # Overlap: any bar where both long and short are open?
        overlap_bars = sum(1 for ts in open_counts
                           if long_counts.get(ts, 0) > 0 and short_counts.get(ts, 0) > 0)
        total_bars = len(open_counts)
        print(f"  Bars with long+short overlap: {overlap_bars}/{total_bars} "
              f"({overlap_bars/total_bars*100:.1f}%)")

    # Concurrency distribution
    conc_dist = defaultdict(int)
    for v in open_counts.values():
        conc_dist[v] += 1
    print(f"\n  Concurrency distribution:")
    for k in sorted(conc_dist.keys()):
        pct = conc_dist[k] / len(open_counts) * 100
        bar = "#" * int(pct / 2)
        print(f"    {k:2d} positions: {conc_dist[k]:5d} bars ({pct:5.1f}%)  {bar}")

    # Per-day max concurrency
    day_max_conc = defaultdict(int)
    for ts, cnt in open_counts.items():
        d = ts.date()
        if cnt > day_max_conc[d]:
            day_max_conc[d] = cnt
    avg_day_max = statistics.mean(day_max_conc.values()) if day_max_conc else 0
    max_day_max = max(day_max_conc.values()) if day_max_conc else 0
    print(f"\n  Per-day max concurrency:  Avg={avg_day_max:.1f}  Max={max_day_max}")

    # ═══════════════════════════════════════════════════════════════
    #  VERDICT
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("VERDICT")
    print("=" * 120)

    m_nbd = compute_r([t for t in portfolio if t.entry_date != best_day])
    m_nts = compute_r([t for t in portfolio if t.sym != top_sym])
    m_nb = compute_r([t for t in portfolio if t.entry_date != best_day and t.sym != top_sym])

    train_all = compute_r([t for t in portfolio if t.entry_date.day % 2 == 1])
    test_all = compute_r([t for t in portfolio if t.entry_date.day % 2 == 0])

    checks = [
        ("PF(R) >= 1.3",                        m_all["pf_r"] >= 1.3),
        ("Exp(R) > +0.15R",                     m_all["exp_r"] > 0.15),
        ("TotalR > +20R",                        m_all["total_r"] > 20),
        ("MaxDD(R) < 12R",                       m_all["max_dd_r"] < 12),
        ("StopRate < 25%",                       m_all["stop_rate"] < 25),
        ("Train PF(R) >= 1.0",                   train_all["pf_r"] >= 1.0),
        ("Test PF(R) >= 1.0",                    test_all["pf_r"] >= 1.0),
        ("Excl best day: PF(R) >= 1.0",         m_nbd["pf_r"] >= 1.0),
        ("Excl top sym: PF(R) >= 1.0",          m_nts["pf_r"] >= 1.0),
        ("Excl both: PF(R) >= 1.0",             m_nb["pf_r"] >= 1.0),
        ("Long book Exp(R) > 0",                 m_long["exp_r"] > 0),
        ("Short book Exp(R) > 0",                m_short["exp_r"] > 0),
        (">=50% profitable days",
         profitable_days / len(day_grp) >= 0.50 if day_grp else False),
        (">=55% symbols positive R",
         pos_syms / (pos_syms + neg_syms) >= 0.55 if (pos_syms + neg_syms) > 0 else False),
    ]

    passed = sum(1 for _, p in checks if p)
    for label, p in checks:
        print(f"  [{'PASS' if p else 'FAIL'}] {label}")

    print(f"\n  Result: {passed}/{len(checks)} checks passed")
    print(f"\n  PORTFOLIO C SUMMARY:")
    print(f"    N={m_all['n']}  PF(R)={pf_str(m_all['pf_r'])}  Exp={m_all['exp_r']:+.3f}R  "
          f"TotalR={m_all['total_r']:+.2f}  MaxDD={m_all['max_dd_r']:.2f}R  "
          f"StpR={m_all['stop_rate']:.1f}%")
    print(f"    Long:  N={m_long['n']}  PF(R)={pf_str(m_long['pf_r'])}  "
          f"Exp={m_long['exp_r']:+.3f}R  TotalR={m_long['total_r']:+.2f}")
    print(f"    Short: N={m_short['n']}  PF(R)={pf_str(m_short['pf_r'])}  "
          f"Exp={m_short['exp_r']:+.3f}R  TotalR={m_short['total_r']:+.2f}")
    print()


if __name__ == "__main__":
    main()
