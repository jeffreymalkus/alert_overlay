"""
Risk-Normalized System Analysis.

Removes point-based distortion by analyzing everything in R-multiples
and dollar PnL with consistent per-trade risk sizing.

Three analyses:
  1. Risk-normalized performance (R-multiples, $100 risk per trade)
  2. RED+CHOPPY BDR suppression comparison
  3. Concentration robustness (exclude MELI, top-5, per-symbol cap)

Usage:
    python -m alert_overlay.risk_normalized_analysis
"""

import statistics
from collections import defaultdict
from pathlib import Path
from typing import List

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import SetupId, SETUP_DISPLAY_NAME
from .market_context import SECTOR_MAP, get_sector_etf

DATA_DIR = Path(__file__).parent / "data"
RISK_PER_TRADE = 100.0  # $100 risk per trade for dollar-normalized PnL


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


class UTrade:
    __slots__ = ("pnl_points", "pnl_rr", "exit_reason", "bars_held",
                 "entry_time", "exit_time", "entry_date", "side", "setup",
                 "sym", "quality", "risk_points", "pnl_dollar")

    def __init__(self, t: Trade, sym: str):
        self.pnl_points = t.pnl_points
        self.pnl_rr = t.pnl_rr
        self.exit_reason = t.exit_reason
        self.bars_held = t.bars_held
        self.entry_time = t.signal.timestamp
        self.exit_time = t.exit_time if t.exit_time else t.signal.timestamp
        self.entry_date = t.signal.timestamp.date()
        self.side = "LONG" if t.signal.direction == 1 else "SHORT"
        self.setup = t.signal.setup_id
        self.sym = sym
        self.quality = t.signal.quality_score
        self.risk_points = t.signal.risk if t.signal.risk > 0 else 1e-9
        # Dollar PnL assuming $RISK_PER_TRADE risk per trade
        self.pnl_dollar = self.pnl_rr * RISK_PER_TRADE


def classify_spy_days(spy_bars):
    daily = defaultdict(list)
    for b in spy_bars:
        daily[b.timestamp.date()].append(b)
    day_info = {}
    for d in sorted(daily.keys()):
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
        if day_range > 0:
            close_pos = (day_close - day_low) / day_range
            character = "TREND" if (close_pos >= 0.75 or close_pos <= 0.25) else "CHOPPY"
        else:
            character = "CHOPPY"
        day_info[d] = {"direction": direction, "character": character}
    return day_info


def compute_r_metrics(trades: List[UTrade]) -> dict:
    """Compute metrics using R-multiples and dollar-normalized PnL."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0, "pf_r": 0, "pf_dollar": 0, "exp_r": 0,
                "total_r": 0, "total_dollar": 0, "max_dd_r": 0, "max_dd_dollar": 0,
                "stop_rate": 0}
    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]

    # R-based
    total_r = sum(t.pnl_rr for t in trades)
    gw_r = sum(t.pnl_rr for t in wins)
    gl_r = abs(sum(t.pnl_rr for t in losses))
    pf_r = gw_r / gl_r if gl_r > 0 else float("inf")
    exp_r = total_r / n

    # Dollar-based ($RISK_PER_TRADE per trade)
    total_dollar = sum(t.pnl_dollar for t in trades)
    gw_d = sum(t.pnl_dollar for t in wins)
    gl_d = abs(sum(t.pnl_dollar for t in losses))
    pf_dollar = gw_d / gl_d if gl_d > 0 else float("inf")

    # Drawdown in R
    cum = pk = dd_r = 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl_rr
        if cum > pk:
            pk = cum
        if pk - cum > dd_r:
            dd_r = pk - cum

    # Drawdown in dollars
    cum = pk = dd_d = 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl_dollar
        if cum > pk:
            pk = cum
        if pk - cum > dd_d:
            dd_d = pk - cum

    stopped = sum(1 for t in trades if t.exit_reason == "stop")

    return {
        "n": n, "wr": len(wins) / n * 100,
        "pf_r": pf_r, "pf_dollar": pf_dollar,
        "exp_r": exp_r,
        "total_r": total_r, "total_dollar": total_dollar,
        "max_dd_r": dd_r, "max_dd_dollar": dd_d,
        "stop_rate": stopped / n * 100,
    }


def print_r_row(label, m, indent="  "):
    print(f"{indent}{label:<34}  N={m['n']:4d}  WR={m['wr']:5.1f}%  "
          f"PF(R)={pf_str(m['pf_r']):>6s}  Exp={m['exp_r']:+.3f}R  "
          f"TotalR={m['total_r']:+7.2f}  MaxDD={m['max_dd_r']:.2f}R  "
          f"${m['total_dollar']:+8.0f}  DD${m['max_dd_dollar']:7.0f}")


def get_regime(t, spy_day_info):
    info = spy_day_info.get(t.entry_date, {})
    return f"{info.get('direction', 'UNK')}+{info.get('character', 'UNK')}"


def main():
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    symbols = sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])

    cfg = OverlayConfig()
    cfg.show_ema_scalp = False
    cfg.show_failed_bounce = False
    cfg.show_spencer = False
    cfg.show_ema_fpip = False
    cfg.show_sc_v2 = False
    cfg.show_breakdown_retest = True

    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    spy_day_info = classify_spy_days(spy_bars)

    print("=" * 130)
    print("RISK-NORMALIZED SYSTEM ANALYSIS")
    print("=" * 130)
    print(f"Universe: {len(symbols)} symbols  |  Risk sizing: ${RISK_PER_TRADE:.0f} per trade  |  Strategy FROZEN")

    # Run backtest
    print("\nRunning integrated backtest...")
    all_raw = []
    sym_map = {}
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
        for t in result.trades:
            all_raw.append(t)
            sym_map[id(t)] = sym

    all_utrades = [UTrade(t, sym_map[id(t)]) for t in all_raw]

    def is_red(t):
        return spy_day_info.get(t.entry_date, {}).get("direction") == "RED"

    filtered = []
    for t in all_utrades:
        if t.side == "LONG":
            hhmm = t.entry_time.hour * 100 + t.entry_time.minute
            if is_red(t):
                continue
            if t.quality < 2:
                continue
            if hhmm >= 1530:
                continue
        filtered.append(t)

    longs = [t for t in filtered if t.side == "LONG"]
    shorts = [t for t in filtered if t.side == "SHORT"]
    print(f"  Trades: {len(filtered)} ({len(longs)}L / {len(shorts)}S)\n")

    # ═══════════════════════════════════════════════════════════════
    #  PART 1: RISK-NORMALIZED PERFORMANCE
    # ═══════════════════════════════════════════════════════════════
    print("=" * 130)
    print("PART 1: RISK-NORMALIZED PERFORMANCE")
    print("=" * 130)

    # Compare points vs R
    m_pts_all = sum(t.pnl_points for t in filtered)
    m_r = compute_r_metrics(filtered)
    m_r_l = compute_r_metrics(longs)
    m_r_s = compute_r_metrics(shorts)

    print(f"\n  Points-based PnL: {m_pts_all:+.2f}  |  R-normalized: {m_r['total_r']:+.2f}R  "
          f"|  Dollar (${RISK_PER_TRADE:.0f}/trade): ${m_r['total_dollar']:+,.0f}")

    print(f"\n  {'Book':<34}  {'N':>4}  {'WR':>6}  {'PF(R)':>6}  {'Exp':>8}  "
          f"{'TotalR':>7}  {'MaxDD':>7}  {'$PnL':>8}  {'$MaxDD':>7}")
    print(f"  {'-'*34}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  "
          f"{'-'*7}  {'-'*7}  {'-'*8}  {'-'*7}")
    print_r_row("Long book", m_r_l)
    print_r_row("Short book (BDR)", m_r_s)
    print_r_row("Combined", m_r)

    # R-distribution
    all_rr = sorted([t.pnl_rr for t in filtered])
    print(f"\n  R-multiple distribution:")
    print(f"    Mean:    {statistics.mean(all_rr):+.3f}R")
    print(f"    Median:  {statistics.median(all_rr):+.3f}R")
    print(f"    Stdev:   {statistics.stdev(all_rr):.3f}R")
    print(f"    Min:     {min(all_rr):+.3f}R")
    print(f"    Max:     {max(all_rr):+.3f}R")

    # R buckets
    buckets = [(-99, -2, "< -2R"), (-2, -1, "-2R to -1R"), (-1, -0.5, "-1R to -0.5R"),
               (-0.5, 0, "-0.5R to 0"), (0, 0.5, "0 to +0.5R"), (0.5, 1, "+0.5R to +1R"),
               (1, 2, "+1R to +2R"), (2, 99, "> +2R")]
    print(f"\n  R-distribution buckets:")
    print(f"    {'Bucket':<16}  {'N':>4}  {'% of all':>8}  {'Cum R':>8}")
    for lo, hi, label in buckets:
        bt = [t for t in filtered if lo <= t.pnl_rr < hi]
        cum_r = sum(t.pnl_rr for t in bt)
        print(f"    {label:<16}  {len(bt):4d}  {len(bt)/len(filtered)*100:7.1f}%  {cum_r:+8.2f}R")

    # By setup in R
    print(f"\n  By setup (R-normalized):")
    setup_grp = defaultdict(list)
    for t in filtered:
        name = SETUP_DISPLAY_NAME.get(t.setup, str(t.setup))
        setup_grp[name].append(t)

    print(f"    {'Setup':<16}  {'N':>4}  {'WR':>6}  {'PF(R)':>6}  {'Exp':>8}  {'TotalR':>8}  {'$PnL':>8}")
    print(f"    {'-'*16}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*8}")
    for name in sorted(setup_grp.keys()):
        m = compute_r_metrics(setup_grp[name])
        print(f"    {name:<16}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf_r']):>6s}  "
              f"{m['exp_r']:+.3f}R  {m['total_r']:+8.2f}R  ${m['total_dollar']:+7.0f}")

    # By regime in R
    regimes = ["GREEN+TREND", "GREEN+CHOPPY", "RED+TREND", "RED+CHOPPY", "FLAT+CHOPPY"]
    print(f"\n  By regime (R-normalized):")
    print(f"    {'Regime':<16}  {'N':>4}  {'WR':>6}  {'PF(R)':>6}  {'Exp':>8}  {'TotalR':>8}  {'$PnL':>8}")
    print(f"    {'-'*16}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*8}")
    for regime in regimes:
        rt = [t for t in filtered if get_regime(t, spy_day_info) == regime]
        if not rt:
            continue
        m = compute_r_metrics(rt)
        print(f"    {regime:<16}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf_r']):>6s}  "
              f"{m['exp_r']:+.3f}R  {m['total_r']:+8.2f}R  ${m['total_dollar']:+7.0f}")

    # Per-symbol contribution in R
    sym_r = defaultdict(lambda: {"total_r": 0.0, "n": 0, "total_dollar": 0.0})
    for t in filtered:
        sym_r[t.sym]["total_r"] += t.pnl_rr
        sym_r[t.sym]["total_dollar"] += t.pnl_dollar
        sym_r[t.sym]["n"] += 1

    sorted_sym_r = sorted(sym_r.items(), key=lambda x: x[1]["total_r"], reverse=True)

    print(f"\n  Per-symbol contribution (R-normalized, top 15):")
    print(f"    {'Sym':<8}  {'N':>4}  {'TotalR':>8}  {'AvgR':>7}  {'$PnL':>8}  {'vs Points':>10}")
    print(f"    {'-'*8}  {'-'*4}  {'-'*8}  {'-'*7}  {'-'*8}  {'-'*10}")
    # Also compute points-based for comparison
    sym_pts = defaultdict(float)
    for t in filtered:
        sym_pts[t.sym] += t.pnl_points
    for sym, stats in sorted_sym_r[:15]:
        avg_r = stats["total_r"] / stats["n"] if stats["n"] > 0 else 0
        pts = sym_pts.get(sym, 0)
        # Rank difference indicator
        print(f"    {sym:<8}  {stats['n']:4d}  {stats['total_r']:+8.2f}R  {avg_r:+6.3f}R  "
              f"${stats['total_dollar']:+7.0f}  (pts:{pts:+.1f})")

    print(f"\n  Bottom 10 by R:")
    for sym, stats in sorted_sym_r[-10:]:
        avg_r = stats["total_r"] / stats["n"] if stats["n"] > 0 else 0
        pts = sym_pts.get(sym, 0)
        print(f"    {sym:<8}  {stats['n']:4d}  {stats['total_r']:+8.2f}R  {avg_r:+6.3f}R  "
              f"${stats['total_dollar']:+7.0f}  (pts:{pts:+.1f})")

    # MELI comparison: points vs R
    meli_trades = [t for t in filtered if t.sym == "MELI"]
    if meli_trades:
        m_meli_r = compute_r_metrics(meli_trades)
        meli_pts = sum(t.pnl_points for t in meli_trades)
        print(f"\n  MELI distortion check:")
        print(f"    Points PnL: {meli_pts:+.2f} ({meli_pts/m_pts_all*100:.0f}% of total)")
        print(f"    R PnL:      {m_meli_r['total_r']:+.2f}R ({m_meli_r['total_r']/m_r['total_r']*100:.0f}% of total)")
        print(f"    Dollar PnL: ${m_meli_r['total_dollar']:+,.0f} ({m_meli_r['total_dollar']/m_r['total_dollar']*100:.0f}% of total)")

    # ═══════════════════════════════════════════════════════════════
    #  PART 2: RED+CHOPPY BDR SUPPRESSION
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("PART 2: RED+CHOPPY BDR SUPPRESSION")
    print("=" * 130)

    baseline = filtered
    suppressed = [t for t in filtered
                  if not (t.setup == SetupId.BDR_SHORT and
                          get_regime(t, spy_day_info) == "RED+CHOPPY")]

    m_base = compute_r_metrics(baseline)
    m_supp = compute_r_metrics(suppressed)

    print(f"\n  {'Variant':<40}  {'N':>4}  {'WR':>6}  {'PF(R)':>6}  {'Exp':>8}  "
          f"{'TotalR':>8}  {'MaxDD_R':>8}  {'$PnL':>8}  {'$MaxDD':>7}")
    print(f"  {'-'*40}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  "
          f"{'-'*8}  {'-'*8}  {'-'*8}  {'-'*7}")

    for label, tlist in [("Baseline (all regimes)", baseline),
                          ("Suppress RED+CHOPPY BDR", suppressed)]:
        m = compute_r_metrics(tlist)
        print(f"  {label:<40}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf_r']):>6s}  "
              f"{m['exp_r']:+.3f}R  {m['total_r']:+8.2f}R  {m['max_dd_r']:8.2f}R  "
              f"${m['total_dollar']:+7.0f}  ${m['max_dd_dollar']:6.0f}")

    # Trades removed
    removed = [t for t in filtered
               if t.setup == SetupId.BDR_SHORT and
               get_regime(t, spy_day_info) == "RED+CHOPPY"]
    m_removed = compute_r_metrics(removed)
    print(f"\n  Removed trades (RED+CHOPPY BDR):")
    print(f"    N={m_removed['n']}  WR={m_removed['wr']:.1f}%  PF(R)={pf_str(m_removed['pf_r'])}  "
          f"Exp={m_removed['exp_r']:+.3f}R  TotalR={m_removed['total_r']:+.2f}R  "
          f"${m_removed['total_dollar']:+,.0f}")

    # Long/short breakdown for both variants
    print(f"\n  Book-level comparison:")
    for label, tlist in [("Baseline", baseline), ("Suppressed", suppressed)]:
        l = [t for t in tlist if t.side == "LONG"]
        s = [t for t in tlist if t.side == "SHORT"]
        ml = compute_r_metrics(l)
        ms = compute_r_metrics(s)
        mc = compute_r_metrics(tlist)
        print(f"    {label}:")
        print(f"      Long:     N={ml['n']:3d}  PF(R)={pf_str(ml['pf_r'])}  TotalR={ml['total_r']:+.2f}  ${ml['total_dollar']:+,.0f}")
        print(f"      Short:    N={ms['n']:3d}  PF(R)={pf_str(ms['pf_r'])}  TotalR={ms['total_r']:+.2f}  ${ms['total_dollar']:+,.0f}")
        print(f"      Combined: N={mc['n']:3d}  PF(R)={pf_str(mc['pf_r'])}  TotalR={mc['total_r']:+.2f}  ${mc['total_dollar']:+,.0f}")

    # Train/test for both variants
    print(f"\n  Train/test stability (R-normalized):")
    for label, tlist in [("Baseline", baseline), ("Suppressed", suppressed)]:
        train = [t for t in tlist if t.entry_date.day % 2 == 1]
        test = [t for t in tlist if t.entry_date.day % 2 == 0]
        mt = compute_r_metrics(train)
        ms = compute_r_metrics(test)
        wr_d = ms["wr"] - mt["wr"]
        stable = mt["pf_r"] >= 1.0 and ms["pf_r"] >= 1.0 and abs(wr_d) < 5.0
        print(f"    {label:<20}  Train: PF(R)={pf_str(mt['pf_r']):>5s} Exp={mt['exp_r']:+.3f}R  "
              f"Test: PF(R)={pf_str(ms['pf_r']):>5s} Exp={ms['exp_r']:+.3f}R  "
              f"WR Δ={wr_d:+.1f}%  {'STABLE' if stable else 'UNSTABLE'}")

    # By regime after suppression
    print(f"\n  Suppressed variant by regime:")
    print(f"    {'Regime':<16}  {'N':>4}  {'PF(R)':>6}  {'Exp':>8}  {'TotalR':>8}")
    print(f"    {'-'*16}  {'-'*4}  {'-'*6}  {'-'*8}  {'-'*8}")
    for regime in regimes:
        rt = [t for t in suppressed if get_regime(t, spy_day_info) == regime]
        if not rt:
            continue
        m = compute_r_metrics(rt)
        print(f"    {regime:<16}  {m['n']:4d}  {pf_str(m['pf_r']):>6s}  "
              f"{m['exp_r']:+.3f}R  {m['total_r']:+8.2f}R")

    # ═══════════════════════════════════════════════════════════════
    #  PART 3: CONCENTRATION ROBUSTNESS (R-NORMALIZED)
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("PART 3: CONCENTRATION ROBUSTNESS (R-NORMALIZED)")
    print("=" * 130)

    total_r = m_r["total_r"]

    # Build exclusion sets from R-ranking
    top5_syms = {s[0] for s in sorted_sym_r[:5]}
    top3_syms = {s[0] for s in sorted_sym_r[:3]}
    bot5_syms = {s[0] for s in sorted_sym_r[-5:]}

    tests = [
        ("Baseline (all symbols)",  set()),
        ("Exclude MELI",            {"MELI"}),
        ("Exclude top-3 by R",      top3_syms),
        ("Exclude top-5 by R",      top5_syms),
        ("Exclude bottom-5 by R",   bot5_syms),
        ("Exclude top-5 & bot-5",   top5_syms | bot5_syms),
    ]

    print(f"\n  Top-5 by R: {', '.join(s[0] for s in sorted_sym_r[:5])}")
    print(f"  Bot-5 by R: {', '.join(s[0] for s in sorted_sym_r[-5:])}")

    print(f"\n  {'Variant':<28}  {'N':>4}  {'WR':>6}  {'PF(R)':>6}  {'Exp':>8}  "
          f"{'TotalR':>8}  {'MaxDD_R':>8}  {'$PnL':>8}  {'R/DD':>6}")
    print(f"  {'-'*28}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  "
          f"{'-'*8}  {'-'*8}  {'-'*8}  {'-'*6}")

    for label, exc in tests:
        vt = [t for t in filtered if t.sym not in exc]
        m = compute_r_metrics(vt)
        r_dd = m["total_r"] / m["max_dd_r"] if m["max_dd_r"] > 0 else float("inf")
        print(f"  {label:<28}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf_r']):>6s}  "
              f"{m['exp_r']:+.3f}R  {m['total_r']:+8.2f}R  {m['max_dd_r']:8.2f}R  "
              f"${m['total_dollar']:+7.0f}  {r_dd:6.2f}")

    # Per-symbol R cap test: cap each symbol's R contribution at +/- 5R
    R_CAP = 5.0
    print(f"\n  Per-symbol R-cap test (cap at +/-{R_CAP}R per symbol):")
    capped_total_r = 0.0
    capped_count = 0
    for sym, stats in sym_r.items():
        contribution = stats["total_r"]
        if contribution > R_CAP:
            capped_total_r += R_CAP
            capped_count += 1
        elif contribution < -R_CAP:
            capped_total_r += -R_CAP
            capped_count += 1
        else:
            capped_total_r += contribution
    print(f"    Uncapped total R:  {total_r:+.2f}R")
    print(f"    Capped total R:    {capped_total_r:+.2f}R")
    print(f"    Symbols capped:    {capped_count}")
    print(f"    PnL retained:      {capped_total_r/total_r*100:.0f}%" if total_r != 0 else "")

    # Concentration metrics in R
    sym_r_vals = [stats["total_r"] for stats in sym_r.values()]
    pos_r = [v for v in sym_r_vals if v > 0]
    neg_r = [v for v in sym_r_vals if v <= 0]
    print(f"\n  Concentration metrics (R-based):")
    print(f"    Symbols with positive R: {len(pos_r)}/{len(sym_r_vals)}")
    print(f"    Symbols with negative R: {len(neg_r)}/{len(sym_r_vals)}")
    if pos_r:
        print(f"    Avg positive symbol R:   {statistics.mean(pos_r):+.2f}R")
    if neg_r:
        print(f"    Avg negative symbol R:   {statistics.mean(neg_r):+.2f}R")
    top1_pct = sorted_sym_r[0][1]["total_r"] / total_r * 100 if total_r != 0 else 0
    top5_r = sum(s[1]["total_r"] for s in sorted_sym_r[:5])
    top5_pct = top5_r / total_r * 100 if total_r != 0 else 0
    print(f"    Top-1 concentration:     {top1_pct:.0f}% ({sorted_sym_r[0][0]})")
    print(f"    Top-5 concentration:     {top5_pct:.0f}%")

    # Train/test stability for key exclusion variants
    print(f"\n  Train/test stability for exclusion variants:")
    for label, exc in tests:
        vt = [t for t in filtered if t.sym not in exc]
        train = [t for t in vt if t.entry_date.day % 2 == 1]
        test = [t for t in vt if t.entry_date.day % 2 == 0]
        mt = compute_r_metrics(train)
        ms = compute_r_metrics(test)
        wr_d = ms["wr"] - mt["wr"]
        stable = mt["pf_r"] >= 1.0 and ms["pf_r"] >= 1.0 and abs(wr_d) < 5.0
        print(f"    {label:<28}  Train PF(R)={pf_str(mt['pf_r']):>5s}  "
              f"Test PF(R)={pf_str(ms['pf_r']):>5s}  WR Δ={wr_d:+.1f}%  "
              f"{'STABLE' if stable else 'UNSTABLE'}")

    # ═══════════════════════════════════════════════════════════════
    #  COMBINED ANALYSIS: SUPPRESSION + EXCLUSION
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("COMBINED: RED+CHOPPY SUPPRESSION + SYMBOL EXCLUSION")
    print("=" * 130)

    combos = [
        ("Baseline",
         lambda t: True, set()),
        ("Suppress RC",
         lambda t: not (t.setup == SetupId.BDR_SHORT and get_regime(t, spy_day_info) == "RED+CHOPPY"), set()),
        ("Exclude MELI",
         lambda t: True, {"MELI"}),
        ("Suppress RC + excl MELI",
         lambda t: not (t.setup == SetupId.BDR_SHORT and get_regime(t, spy_day_info) == "RED+CHOPPY"), {"MELI"}),
        ("Suppress RC + excl top-5",
         lambda t: not (t.setup == SetupId.BDR_SHORT and get_regime(t, spy_day_info) == "RED+CHOPPY"), top5_syms),
    ]

    print(f"\n  {'Variant':<34}  {'N':>4}  {'WR':>6}  {'PF(R)':>6}  {'Exp':>8}  "
          f"{'TotalR':>8}  {'MaxDD_R':>8}  {'$PnL':>8}  {'R/DD':>6}")
    print(f"  {'-'*34}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  "
          f"{'-'*8}  {'-'*8}  {'-'*8}  {'-'*6}")

    for label, filt, exc in combos:
        vt = [t for t in filtered if filt(t) and t.sym not in exc]
        m = compute_r_metrics(vt)
        r_dd = m["total_r"] / m["max_dd_r"] if m["max_dd_r"] > 0 else float("inf")
        print(f"  {label:<34}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf_r']):>6s}  "
              f"{m['exp_r']:+.3f}R  {m['total_r']:+8.2f}R  {m['max_dd_r']:8.2f}R  "
              f"${m['total_dollar']:+7.0f}  {r_dd:6.2f}")

    # ═══════════════════════════════════════════════════════════════
    #  VERDICT
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("VERDICT")
    print("=" * 130)

    # Key checks on R-normalized baseline
    m_all = compute_r_metrics(filtered)
    no_meli = compute_r_metrics([t for t in filtered if t.sym != "MELI"])
    no_top5 = compute_r_metrics([t for t in filtered if t.sym not in top5_syms])
    supp_rc = compute_r_metrics(suppressed)
    supp_rc_no_meli = compute_r_metrics([t for t in suppressed if t.sym != "MELI"])

    checks = [
        ("R-normalized PF >= 1.0",           m_all["pf_r"] >= 1.0),
        ("R-normalized Exp > 0",             m_all["exp_r"] > 0),
        ("Excluding MELI: PF(R) >= 1.0",    no_meli["pf_r"] >= 1.0),
        ("Excluding top-5: PF(R) >= 1.0",   no_top5["pf_r"] >= 1.0),
        ("Suppress RC: PF(R) >= 1.0",       supp_rc["pf_r"] >= 1.0),
        ("Suppress RC + no MELI: PF(R)>=1", supp_rc_no_meli["pf_r"] >= 1.0),
        ("MELI < 40% of total R",           sorted_sym_r[0][1]["total_r"] / total_r < 0.4 if total_r > 0 else False),
    ]

    for name, passed in checks:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")

    print(f"\n  Key numbers:")
    print(f"    Baseline:              {m_all['total_r']:+.2f}R  PF(R)={pf_str(m_all['pf_r'])}  Exp={m_all['exp_r']:+.3f}R")
    print(f"    Excl MELI:             {no_meli['total_r']:+.2f}R  PF(R)={pf_str(no_meli['pf_r'])}  Exp={no_meli['exp_r']:+.3f}R")
    print(f"    Excl top-5:            {no_top5['total_r']:+.2f}R  PF(R)={pf_str(no_top5['pf_r'])}  Exp={no_top5['exp_r']:+.3f}R")
    print(f"    Suppress RC:           {supp_rc['total_r']:+.2f}R  PF(R)={pf_str(supp_rc['pf_r'])}  Exp={supp_rc['exp_r']:+.3f}R")
    print(f"    Suppress RC + no MELI: {supp_rc_no_meli['total_r']:+.2f}R  PF(R)={pf_str(supp_rc_no_meli['pf_r'])}  Exp={supp_rc_no_meli['exp_r']:+.3f}R")
    print()


if __name__ == "__main__":
    main()
