"""
Regime Weakness & Symbol Robustness Analysis.

Three analyses with frozen strategy logic:
  1. RED+CHOPPY regime deep dive — which setups bleed, why, and
     whether suppression or stricter confirmation helps
  2. Symbol concentration robustness — exclude MELI, top-5,
     measure dependency
  3. Extended OOS — first-half vs second-half temporal split
     to test stability across different market environments

Usage:
    python -m alert_overlay.regime_robustness_analysis
"""

import statistics
from collections import defaultdict
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import List, Dict

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import NaN, SetupId, SETUP_DISPLAY_NAME
from .market_context import SECTOR_MAP, get_sector_etf

DATA_DIR = Path(__file__).parent / "data"


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


class UTrade:
    __slots__ = ("pnl_points", "pnl_rr", "exit_reason", "bars_held",
                 "entry_time", "exit_time", "entry_date", "side", "setup",
                 "sym", "quality", "direction")

    def __init__(self, t: Trade, sym: str):
        self.pnl_points = t.pnl_points
        self.pnl_rr = t.pnl_rr
        self.exit_reason = t.exit_reason
        self.bars_held = t.bars_held
        self.entry_time = t.signal.timestamp
        self.exit_time = t.exit_time if t.exit_time else t.signal.timestamp
        self.entry_date = t.signal.timestamp.date()
        self.side = "LONG" if t.signal.direction == 1 else "SHORT"
        self.direction = t.signal.direction
        self.setup = t.signal.setup_id
        self.sym = sym
        self.quality = t.signal.quality_score


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
        day_info[d] = {"direction": direction, "character": character,
                       "spy_change_pct": change_pct}
    return day_info


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


def run_integrated_backtest(symbols, cfg, spy_bars, qqq_bars, sector_bars_dict):
    """Run backtest, return list of UTrade."""
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

    return [UTrade(t, sym_map[id(t)]) for t in all_raw]


def apply_locked_filters(utrades, spy_day_info):
    """Apply locked long baseline filters; pass all shorts through."""
    def is_red(t):
        return spy_day_info.get(t.entry_date, {}).get("direction") == "RED"

    filtered = []
    for t in utrades:
        if t.side == "LONG":
            hhmm = t.entry_time.hour * 100 + t.entry_time.minute
            if is_red(t):
                continue
            if t.quality < 2:
                continue
            if hhmm >= 1530:
                continue
        filtered.append(t)
    return filtered


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

    print("=" * 120)
    print("REGIME WEAKNESS & SYMBOL ROBUSTNESS ANALYSIS")
    print("=" * 120)
    print(f"Universe: {len(symbols)} symbols  |  Strategy logic FROZEN")

    # Run single backtest
    print("\nRunning integrated backtest...")
    all_utrades = run_integrated_backtest(symbols, cfg, spy_bars, qqq_bars, sector_bars_dict)
    filtered = apply_locked_filters(all_utrades, spy_day_info)
    longs = [t for t in filtered if t.side == "LONG"]
    shorts = [t for t in filtered if t.side == "SHORT"]
    print(f"  Trades: {len(filtered)} ({len(longs)}L / {len(shorts)}S)\n")

    # ═══════════════════════════════════════════════════════════════
    #  ANALYSIS 1: RED+CHOPPY REGIME DEEP DIVE
    # ═══════════════════════════════════════════════════════════════
    print("=" * 120)
    print("ANALYSIS 1: RED+CHOPPY REGIME DEEP DIVE")
    print("=" * 120)

    rc_trades = [t for t in filtered if get_regime(t, spy_day_info) == "RED+CHOPPY"]
    rc_shorts = [t for t in rc_trades if t.side == "SHORT"]
    rc_longs = [t for t in rc_trades if t.side == "LONG"]

    m_rc = compute_metrics(rc_trades)
    m_rc_s = compute_metrics(rc_shorts)
    m_rc_l = compute_metrics(rc_longs)

    print(f"\n  RED+CHOPPY overall:")
    print(f"    Combined:  N={m_rc['n']:4d}  WR={m_rc['wr']:5.1f}%  PF={pf_str(m_rc['pf'])}  "
          f"PnL={m_rc['pnl']:+.2f}  MaxDD={m_rc['max_dd']:.2f}  StpR={m_rc['stop_rate']:.1f}%")
    print(f"    Short:     N={m_rc_s['n']:4d}  WR={m_rc_s['wr']:5.1f}%  PF={pf_str(m_rc_s['pf'])}  "
          f"PnL={m_rc_s['pnl']:+.2f}  MaxDD={m_rc_s['max_dd']:.2f}  StpR={m_rc_s['stop_rate']:.1f}%")
    if rc_longs:
        print(f"    Long:      N={m_rc_l['n']:4d}  WR={m_rc_l['wr']:5.1f}%  PF={pf_str(m_rc_l['pf'])}  "
              f"PnL={m_rc_l['pnl']:+.2f}")
    else:
        print(f"    Long:      N=   0  (Non-RED filter blocks all longs)")

    # By setup within RED+CHOPPY
    print(f"\n  By setup within RED+CHOPPY:")
    setup_grp = defaultdict(list)
    for t in rc_trades:
        name = SETUP_DISPLAY_NAME.get(t.setup, str(t.setup))
        setup_grp[name].append(t)

    print(f"    {'Setup':<16}  {'N':>4}  {'WR':>6}  {'PF':>6}  {'PnL':>8}  {'StpR':>5}  {'QStpR':>6}")
    print(f"    {'-'*16}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*5}  {'-'*6}")
    for name in sorted(setup_grp.keys()):
        m = compute_metrics(setup_grp[name])
        print(f"    {name:<16}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  "
              f"{m['pnl']:+8.2f}  {m['stop_rate']:4.1f}%  {m['qstop_rate']:5.1f}%")

    # Per-day breakdown within RED+CHOPPY
    rc_daily = defaultdict(list)
    for t in rc_trades:
        rc_daily[t.entry_date].append(t)

    print(f"\n  Per-day breakdown (RED+CHOPPY days):")
    print(f"    {'Date':<12}  {'N':>3}  {'PnL':>8}  {'WR':>6}  {'Stops':>5}  {'SPY%':>6}")
    print(f"    {'-'*12}  {'-'*3}  {'-'*8}  {'-'*6}  {'-'*5}  {'-'*6}")
    for d in sorted(rc_daily.keys()):
        dt = rc_daily[d]
        m = compute_metrics(dt)
        spy_chg = spy_day_info.get(d, {}).get("spy_change_pct", 0)
        stops = sum(1 for t in dt if t.exit_reason == "stop")
        print(f"    {d}  {m['n']:3d}  {m['pnl']:+8.2f}  {m['wr']:5.1f}%  {stops:5d}  {spy_chg:+5.2f}%")

    # Exit reason distribution
    print(f"\n  Exit reason distribution (RED+CHOPPY shorts):")
    exit_dist = defaultdict(int)
    for t in rc_shorts:
        exit_dist[t.exit_reason] += 1
    for reason, cnt in sorted(exit_dist.items(), key=lambda x: x[1], reverse=True):
        pnl_r = sum(t.pnl_points for t in rc_shorts if t.exit_reason == reason)
        print(f"    {reason:<12}  N={cnt:3d}  PnL={pnl_r:+.2f}")

    # Quality distribution within RED+CHOPPY
    print(f"\n  Quality distribution (RED+CHOPPY BDR shorts):")
    rc_bdr = [t for t in rc_shorts if t.setup == SetupId.BDR_SHORT]
    q_groups = defaultdict(list)
    for t in rc_bdr:
        q_groups[t.quality].append(t)
    print(f"    {'Q':>2}  {'N':>4}  {'WR':>6}  {'PF':>6}  {'PnL':>8}")
    print(f"    {'-'*2}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}")
    for q in sorted(q_groups.keys()):
        m = compute_metrics(q_groups[q])
        print(f"    {q:2d}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  {m['pnl']:+8.2f}")

    # Bars held distribution
    held_wins = [t.bars_held for t in rc_shorts if t.pnl_points > 0]
    held_losses = [t.bars_held for t in rc_shorts if t.pnl_points <= 0]
    print(f"\n  Bars held (RED+CHOPPY shorts):")
    if held_wins:
        print(f"    Winners:  avg={statistics.mean(held_wins):.1f}  median={statistics.median(held_wins):.0f}")
    if held_losses:
        print(f"    Losers:   avg={statistics.mean(held_losses):.1f}  median={statistics.median(held_losses):.0f}")

    # Compare RED+CHOPPY vs RED+TREND
    rt_shorts = [t for t in shorts if get_regime(t, spy_day_info) == "RED+TREND"]
    m_rt = compute_metrics(rt_shorts)
    print(f"\n  Comparison: RED+TREND vs RED+CHOPPY (shorts only):")
    print(f"    RED+TREND:   N={m_rt['n']:4d}  WR={m_rt['wr']:5.1f}%  PF={pf_str(m_rt['pf'])}  "
          f"PnL={m_rt['pnl']:+.2f}  StpR={m_rt['stop_rate']:.1f}%")
    print(f"    RED+CHOPPY:  N={m_rc_s['n']:4d}  WR={m_rc_s['wr']:5.1f}%  PF={pf_str(m_rc_s['pf'])}  "
          f"PnL={m_rc_s['pnl']:+.2f}  StpR={m_rc_s['stop_rate']:.1f}%")

    # ═══════════════════════════════════════════════════════════════
    #  ANALYSIS 1B: REGIME SUPPRESSION VARIANTS
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("ANALYSIS 1B: REGIME SUPPRESSION VARIANTS (hypothetical)")
    print("=" * 120)
    print(f"  NOTE: These variants filter post-hoc, not changing engine logic.\n")

    regimes_all = ["GREEN+TREND", "GREEN+CHOPPY", "RED+TREND", "RED+CHOPPY",
                   "FLAT+TREND", "FLAT+CHOPPY"]

    variants = [
        ("Baseline (all regimes)",
         lambda t: True),
        ("Suppress RED+CHOPPY shorts",
         lambda t: not (t.side == "SHORT" and get_regime(t, spy_day_info) == "RED+CHOPPY")),
        ("Suppress RED+CHOPPY all",
         lambda t: get_regime(t, spy_day_info) != "RED+CHOPPY"),
        ("BDR only on RED+TREND",
         lambda t: not (t.setup == SetupId.BDR_SHORT and
                        get_regime(t, spy_day_info) not in ("RED+TREND",))),
        ("BDR on RED+TREND & GREEN (no CHOPPY RED)",
         lambda t: not (t.setup == SetupId.BDR_SHORT and
                        get_regime(t, spy_day_info) == "RED+CHOPPY")),
        ("BDR Q>=2 on RED+CHOPPY only",
         lambda t: not (t.setup == SetupId.BDR_SHORT and
                        get_regime(t, spy_day_info) == "RED+CHOPPY" and
                        t.quality < 2)),
        ("BDR Q>=3 on RED+CHOPPY only",
         lambda t: not (t.setup == SetupId.BDR_SHORT and
                        get_regime(t, spy_day_info) == "RED+CHOPPY" and
                        t.quality < 3)),
    ]

    print(f"  {'Variant':<42}  {'N':>4}  {'WR':>6}  {'PF':>6}  {'PnL':>8}  {'MaxDD':>7}  {'PnL/DD':>7}")
    print(f"  {'-'*42}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*7}  {'-'*7}")

    for name, filt in variants:
        vt = [t for t in filtered if filt(t)]
        m = compute_metrics(vt)
        pnl_dd = m["pnl"] / m["max_dd"] if m["max_dd"] > 0 else float("inf")
        print(f"  {name:<42}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  "
              f"{m['pnl']:+8.2f}  {m['max_dd']:7.2f}  {pnl_dd:7.2f}")

    # Train/test stability for key variants
    print(f"\n  Train/test stability for key suppression variants:")
    for name, filt in variants:
        vt = [t for t in filtered if filt(t)]
        train = [t for t in vt if t.entry_date.day % 2 == 1]
        test = [t for t in vt if t.entry_date.day % 2 == 0]
        mt = compute_metrics(train)
        ms = compute_metrics(test)
        wr_d = ms["wr"] - mt["wr"]
        stable = mt["pf"] >= 1.0 and ms["pf"] >= 1.0 and abs(wr_d) < 5.0
        print(f"    {name:<42}  Train PF={pf_str(mt['pf']):>5s}  Test PF={pf_str(ms['pf']):>5s}  "
              f"WR Δ={wr_d:+.1f}%  {'STABLE' if stable else 'UNSTABLE'}")

    # ═══════════════════════════════════════════════════════════════
    #  ANALYSIS 2: SYMBOL CONCENTRATION ROBUSTNESS
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("ANALYSIS 2: SYMBOL CONCENTRATION ROBUSTNESS")
    print("=" * 120)

    # Compute per-symbol PnL
    sym_pnl = defaultdict(float)
    sym_count = defaultdict(int)
    for t in filtered:
        sym_pnl[t.sym] += t.pnl_points
        sym_count[t.sym] += 1

    sorted_by_pnl = sorted(sym_pnl.items(), key=lambda x: x[1], reverse=True)
    total_pnl = compute_metrics(filtered)["pnl"]

    print(f"\n  Top 10 symbols by PnL contribution:")
    print(f"    {'Sym':<8}  {'N':>4}  {'PnL':>8}  {'% Total':>8}  {'Cum%':>6}")
    print(f"    {'-'*8}  {'-'*4}  {'-'*8}  {'-'*8}  {'-'*6}")
    cum_pct = 0
    for sym, pnl in sorted_by_pnl[:10]:
        pct = pnl / total_pnl * 100 if total_pnl != 0 else 0
        cum_pct += pct
        print(f"    {sym:<8}  {sym_count[sym]:4d}  {pnl:+8.2f}  {pct:+7.1f}%  {cum_pct:5.1f}%")

    # Bottom 10
    print(f"\n  Bottom 10 symbols by PnL contribution:")
    for sym, pnl in sorted_by_pnl[-10:]:
        pct = pnl / total_pnl * 100 if total_pnl != 0 else 0
        print(f"    {sym:<8}  {sym_count[sym]:4d}  {pnl:+8.2f}  {pct:+7.1f}%")

    # Exclusion tests
    exclusion_tests = [
        ("Exclude MELI",           {"MELI"}),
        ("Exclude top-1 (MELI)",   {sorted_by_pnl[0][0]}),
        ("Exclude top-3",          {s[0] for s in sorted_by_pnl[:3]}),
        ("Exclude top-5",          {s[0] for s in sorted_by_pnl[:5]}),
        ("Exclude bottom-5",       {s[0] for s in sorted_by_pnl[-5:]}),
        ("Exclude top-5 & bot-5",  {s[0] for s in sorted_by_pnl[:5]} |
                                   {s[0] for s in sorted_by_pnl[-5:]}),
    ]

    print(f"\n  Symbol exclusion impact:")
    print(f"    {'Variant':<28}  {'N':>4}  {'WR':>6}  {'PF':>6}  {'PnL':>8}  {'MaxDD':>7}  {'PnL/DD':>7}")
    print(f"    {'-'*28}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*7}  {'-'*7}")

    # Baseline
    m_base = compute_metrics(filtered)
    pnl_dd_base = m_base["pnl"] / m_base["max_dd"] if m_base["max_dd"] > 0 else float("inf")
    print(f"    {'Baseline (all symbols)':<28}  {m_base['n']:4d}  {m_base['wr']:5.1f}%  "
          f"{pf_str(m_base['pf']):>6s}  {m_base['pnl']:+8.2f}  {m_base['max_dd']:7.2f}  {pnl_dd_base:7.2f}")

    for name, exc_syms in exclusion_tests:
        vt = [t for t in filtered if t.sym not in exc_syms]
        m = compute_metrics(vt)
        pnl_dd = m["pnl"] / m["max_dd"] if m["max_dd"] > 0 else float("inf")
        print(f"    {name:<28}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  "
              f"{m['pnl']:+8.2f}  {m['max_dd']:7.2f}  {pnl_dd:7.2f}")

    # MELI detail
    meli_trades = [t for t in filtered if t.sym == "MELI"]
    if meli_trades:
        print(f"\n  MELI trade detail:")
        print(f"    {'Date':<12}  {'Side':<6}  {'Setup':<12}  {'PnL':>8}  {'Exit':>8}  {'Bars':>4}")
        print(f"    {'-'*12}  {'-'*6}  {'-'*12}  {'-'*8}  {'-'*8}  {'-'*4}")
        for t in sorted(meli_trades, key=lambda t: t.entry_time):
            setup_name = SETUP_DISPLAY_NAME.get(t.setup, str(t.setup))
            print(f"    {t.entry_date}  {t.side:<6}  {setup_name:<12}  "
                  f"{t.pnl_points:+8.2f}  {t.exit_reason:<8}  {t.bars_held:4d}")

    # Gini coefficient of PnL concentration
    pnl_vals = [abs(v) for v in sym_pnl.values()]
    pnl_vals.sort()
    n_syms = len(pnl_vals)
    if n_syms > 0 and sum(pnl_vals) > 0:
        cumsum = 0.0
        area = 0.0
        total_abs = sum(pnl_vals)
        for i, v in enumerate(pnl_vals):
            cumsum += v
            area += cumsum / total_abs
        gini = 1 - 2 * area / n_syms + 1 / n_syms
        print(f"\n  PnL Gini coefficient: {gini:.3f} (0=equal, 1=concentrated)")

    # ═══════════════════════════════════════════════════════════════
    #  ANALYSIS 3: TEMPORAL SPLIT (FIRST HALF vs SECOND HALF)
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("ANALYSIS 3: TEMPORAL SPLIT — FIRST HALF vs SECOND HALF")
    print("=" * 120)

    all_dates = sorted(set(t.entry_date for t in filtered))
    if len(all_dates) >= 4:
        mid_idx = len(all_dates) // 2
        mid_date = all_dates[mid_idx]
        first_half = [t for t in filtered if t.entry_date < mid_date]
        second_half = [t for t in filtered if t.entry_date >= mid_date]

        print(f"\n  Date range: {all_dates[0]} → {all_dates[-1]} ({len(all_dates)} trading days)")
        print(f"  Split point: {mid_date}")
        print(f"  First half:  {all_dates[0]} → {all_dates[mid_idx-1]} ({mid_idx} days)")
        print(f"  Second half: {mid_date} → {all_dates[-1]} ({len(all_dates) - mid_idx} days)")

        # Overall comparison
        m1 = compute_metrics(first_half)
        m2 = compute_metrics(second_half)
        print(f"\n  {'Period':<14}  {'N':>4}  {'WR':>6}  {'PF':>6}  {'PnL':>8}  {'MaxDD':>7}  {'StpR':>5}")
        print(f"  {'-'*14}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*7}  {'-'*5}")
        print(f"  {'First half':<14}  {m1['n']:4d}  {m1['wr']:5.1f}%  {pf_str(m1['pf']):>6s}  "
              f"{m1['pnl']:+8.2f}  {m1['max_dd']:7.2f}  {m1['stop_rate']:4.1f}%")
        print(f"  {'Second half':<14}  {m2['n']:4d}  {m2['wr']:5.1f}%  {pf_str(m2['pf']):>6s}  "
              f"{m2['pnl']:+8.2f}  {m2['max_dd']:7.2f}  {m2['stop_rate']:4.1f}%")

        # By side per half
        print(f"\n  By side:")
        for label, half in [("First half", first_half), ("Second half", second_half)]:
            l = [t for t in half if t.side == "LONG"]
            s = [t for t in half if t.side == "SHORT"]
            ml = compute_metrics(l)
            ms = compute_metrics(s)
            print(f"    {label}  Long:  N={ml['n']:3d}  PF={pf_str(ml['pf']):>5s}  PnL={ml['pnl']:+7.2f}  |  "
                  f"Short: N={ms['n']:3d}  PF={pf_str(ms['pf']):>5s}  PnL={ms['pnl']:+7.2f}")

        # By regime per half
        print(f"\n  By regime per half:")
        for regime in ["GREEN+TREND", "GREEN+CHOPPY", "RED+TREND", "RED+CHOPPY", "FLAT+CHOPPY"]:
            r1 = [t for t in first_half if get_regime(t, spy_day_info) == regime]
            r2 = [t for t in second_half if get_regime(t, spy_day_info) == regime]
            m1r = compute_metrics(r1)
            m2r = compute_metrics(r2)
            if m1r["n"] == 0 and m2r["n"] == 0:
                continue
            print(f"    {regime:<16}  1st: N={m1r['n']:3d} PF={pf_str(m1r['pf']):>5s} PnL={m1r['pnl']:+7.2f}  |  "
                  f"2nd: N={m2r['n']:3d} PF={pf_str(m2r['pf']):>5s} PnL={m2r['pnl']:+7.2f}")

        # Weekly PnL per half
        print(f"\n  Weekly PnL:")
        week_pnl = defaultdict(float)
        for t in filtered:
            iso = t.entry_date.isocalendar()
            wk = f"{iso[0]}-W{iso[1]:02d}"
            week_pnl[wk] += t.pnl_points

        print(f"    {'Week':<10}  {'PnL':>8}  {'Half':>6}")
        print(f"    {'-'*10}  {'-'*8}  {'-'*6}")
        for wk in sorted(week_pnl.keys()):
            # Determine which half
            wk_trades = [t for t in filtered if
                         f"{t.entry_date.isocalendar()[0]}-W{t.entry_date.isocalendar()[1]:02d}" == wk]
            half = "1st" if wk_trades[0].entry_date < mid_date else "2nd"
            print(f"    {wk:<10}  {week_pnl[wk]:+8.2f}  {half:>6}")

    # ═══════════════════════════════════════════════════════════════
    #  CONSOLIDATED VERDICT
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("CONSOLIDATED VERDICT")
    print("=" * 120)

    # Regime check
    rc_pnl = compute_metrics(rc_trades)["pnl"]
    norc_trades = [t for t in filtered if get_regime(t, spy_day_info) != "RED+CHOPPY"]
    m_norc = compute_metrics(norc_trades)

    print(f"\n  1. RED+CHOPPY impact:")
    print(f"     PnL with RED+CHOPPY:     {total_pnl:+.2f} (N={len(filtered)})")
    print(f"     PnL without RED+CHOPPY:  {m_norc['pnl']:+.2f} (N={m_norc['n']})")
    print(f"     RED+CHOPPY drag:         {rc_pnl:+.2f} ({rc_pnl/total_pnl*100:.0f}% of total)")

    # Symbol dependency
    no_meli = [t for t in filtered if t.sym != "MELI"]
    m_no_meli = compute_metrics(no_meli)
    no_top5 = [t for t in filtered if t.sym not in {s[0] for s in sorted_by_pnl[:5]}]
    m_no_top5 = compute_metrics(no_top5)

    print(f"\n  2. Symbol concentration:")
    print(f"     Baseline PnL:          {total_pnl:+.2f}")
    print(f"     Excluding MELI:        {m_no_meli['pnl']:+.2f} "
          f"(PF={pf_str(m_no_meli['pf'])}, -{total_pnl - m_no_meli['pnl']:.2f})")
    print(f"     Excluding top-5:       {m_no_top5['pnl']:+.2f} "
          f"(PF={pf_str(m_no_top5['pf'])})")
    top5_pct = (total_pnl - m_no_top5['pnl']) / total_pnl * 100 if total_pnl != 0 else 0
    print(f"     Top-5 concentration:   {top5_pct:.0f}% of total PnL")

    # Temporal stability
    if len(all_dates) >= 4:
        m1 = compute_metrics(first_half)
        m2 = compute_metrics(second_half)
        print(f"\n  3. Temporal stability:")
        print(f"     First half PF:  {pf_str(m1['pf'])}  PnL={m1['pnl']:+.2f}")
        print(f"     Second half PF: {pf_str(m2['pf'])}  PnL={m2['pnl']:+.2f}")
        both_profitable = m1["pnl"] > 0 and m2["pnl"] > 0
        print(f"     Both halves profitable: {'YES' if both_profitable else 'NO'}")

    print()


if __name__ == "__main__":
    main()
