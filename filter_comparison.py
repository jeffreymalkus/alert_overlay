"""
Filter comparison — tests layered coarse filters on corrected baseline.

Variants:
  1. Corrected baseline (VK + SC_VOL, no extra filters)
  2. Baseline + Non-RED SPY day filter
  3. Baseline + Non-RED + Q >= 2
  4. Baseline + Non-RED + Q >= 2 + No entries after 15:30

Each variant reports: trades, WR, PF, expectancy, PnL, max drawdown,
stop rate, average hold time, and regime breakdown.

Usage:
    python -m alert_overlay.filter_comparison --universe all94
"""

import argparse
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import List, Dict
from zoneinfo import ZoneInfo

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import (
    NaN, SetupId, SetupFamily, SETUP_DISPLAY_NAME, SETUP_FAMILY_MAP,
)

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent / "data"
WATCHLIST_FILE = Path(__file__).parent / "watchlist.txt"


def load_watchlist(path=WATCHLIST_FILE):
    symbols = []
    with open(path) as f:
        for line in f:
            sym = line.strip().upper()
            if sym and not sym.startswith("#"):
                symbols.append(sym)
    return symbols


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "∞"


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
        day_info[d] = {
            "direction": direction,
            "volatility": volatility,
            "character": character,
            "spy_change_pct": change_pct,
        }

    return day_info


def compute_metrics(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "exp": 0.0,
                "stop_rate": 0.0, "max_dd": 0.0, "avg_hold": 0.0}
    wins = [t for t in trades if t.pnl_points > 0]
    losses = [t for t in trades if t.pnl_points <= 0]
    pnl = sum(t.pnl_points for t in trades)
    gw = sum(t.pnl_points for t in wins)
    gl = abs(sum(t.pnl_points for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    avg_rr = sum(t.pnl_rr for t in trades) / n
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    cum = pk = dd = 0.0
    for t in trades:
        cum += t.pnl_points
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum
    avg_hold = statistics.mean(t.bars_held for t in trades)
    return {"n": n, "wr": len(wins) / n * 100, "pf": pf, "pnl": pnl,
            "exp": avg_rr, "stop_rate": stopped / n * 100, "max_dd": dd,
            "avg_hold": avg_hold}


def print_metrics_row(label, m, baseline_m=None):
    delta_pnl = ""
    if baseline_m and baseline_m["n"] > 0:
        delta_pnl = f"  ({m['pnl'] - baseline_m['pnl']:+.2f})"
    print(f"  {label:<40} │ {m['n']:>4} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
          f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f}{delta_pnl:>10} "
          f"{m['max_dd']:>5.1f} {m['stop_rate']:>5.0f}% {m['avg_hold']:>5.1f}")


def print_regime_breakdown(trades, spy_day_info, label):
    """Print compact regime breakdown for a variant."""
    groups = defaultdict(list)
    for t in trades:
        d = t.signal.timestamp.date()
        info = spy_day_info.get(d, {})
        direction = info.get("direction", "UNK")
        character = info.get("character", "UNK")
        groups[f"{direction}+{character}"].append(t)

    print(f"\n    Regime breakdown ({label}):")
    print(f"    {'Regime':<20} │ {'N':>3} {'WR%':>5} {'PF':>5} {'PnL':>8} {'StpR':>5}")
    print(f"    {'─' * 20}─┼{'─' * 30}")
    for key in ["GREEN+TREND", "GREEN+CHOPPY", "RED+TREND", "RED+CHOPPY",
                "FLAT+TREND", "FLAT+CHOPPY"]:
        if key not in groups:
            continue
        m = compute_metrics(groups[key])
        print(f"    {key:<20} │ {m['n']:>3} {m['wr']:>5.1f} {pf_str(m['pf']):>5} "
              f"{m['pnl']:>+8.2f} {m['stop_rate']:>5.0f}%")


def print_setup_breakdown(trades, label):
    """Print per-setup breakdown."""
    groups = defaultdict(list)
    for t in trades:
        name = SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id))
        groups[name].append(t)

    print(f"\n    By setup ({label}):")
    print(f"    {'Setup':<18} │ {'N':>3} {'WR%':>5} {'PF':>5} {'Exp':>7} {'PnL':>8} {'StpR':>5}")
    print(f"    {'─' * 18}─┼{'─' * 38}")
    for name in sorted(groups.keys()):
        m = compute_metrics(groups[name])
        print(f"    {name:<18} │ {m['n']:>3} {m['wr']:>5.1f} {pf_str(m['pf']):>5} "
              f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['stop_rate']:>5.0f}%")


def print_exit_breakdown(trades, label):
    """Print exit reason breakdown."""
    groups = defaultdict(list)
    for t in trades:
        groups[t.exit_reason].append(t)

    print(f"\n    Exit reasons ({label}):")
    print(f"    {'Reason':<12} │ {'N':>3} {'WR%':>5} {'PnL':>8}")
    print(f"    {'─' * 12}─┼{'─' * 18}")
    for reason in sorted(groups.keys()):
        m = compute_metrics(groups[reason])
        print(f"    {reason:<12} │ {m['n']:>3} {m['wr']:>5.1f} {m['pnl']:>+8.2f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="all94")
    args = parser.parse_args()

    if args.universe == "all94":
        symbols = load_watchlist()
    else:
        symbols = args.universe.split(",")

    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    from .market_context import SECTOR_MAP, get_sector_etf
    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    spy_day_info = classify_spy_days(spy_bars)

    # Frozen baseline config
    cfg = OverlayConfig()
    cfg.show_ema_scalp = False
    cfg.show_failed_bounce = False
    cfg.show_spencer = False
    cfg.show_ema_fpip = False
    cfg.show_sc_v2 = False

    # Run backtest, collect all trades with metadata
    all_trades = []  # List of Trade
    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue

        sec_bars = None
        se = get_sector_etf(sym)
        if se and se in sector_bars_dict:
            sec_bars = sector_bars_dict[se]

        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        all_trades.extend(result.trades)

    all_dates = sorted(set(
        info_date for info_date in spy_day_info.keys()
    ))
    n_days = len(all_dates)

    print(f"Period: {min(all_dates)} → {max(all_dates)} ({n_days} days)")
    print(f"Total trades (unfiltered): {len(all_trades)}")

    # Count RED days
    red_days = sum(1 for d, info in spy_day_info.items() if info["direction"] == "RED")
    green_days = sum(1 for d, info in spy_day_info.items() if info["direction"] == "GREEN")
    flat_days = n_days - red_days - green_days
    print(f"SPY days: {green_days} GREEN, {red_days} RED, {flat_days} FLAT")

    # ── Define 4 variants ────────────────────────────────────────
    def is_red(t):
        return spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == "RED"

    def signal_hhmm(t):
        ts = t.signal.timestamp
        return ts.hour * 100 + ts.minute

    v1 = all_trades  # Corrected baseline
    v2 = [t for t in all_trades if not is_red(t)]  # + Non-RED
    v3 = [t for t in v2 if t.signal.quality_score >= 2]  # + Q>=2
    v4 = [t for t in v3 if signal_hhmm(t) < 1530]  # + No entries after 15:30

    variants = [
        ("1. Corrected baseline", v1),
        ("2. + Non-RED day", v2),
        ("3. + Non-RED + Q>=2", v3),
        ("4. + Non-RED + Q>=2 + <15:30", v4),
    ]

    # ══════════════════════════════════════════════════════════════
    #  MAIN COMPARISON TABLE
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  FILTER COMPARISON — LAYERED COARSE FILTERS")
    print(f"{'=' * 115}")

    print(f"\n  {'Variant':<40} │ {'N':>4} {'WR%':>5} {'PF':>5} "
          f"{'Exp':>7} {'PnL':>8} {'(ΔPnL)':>10} "
          f"{'MaxDD':>5} {'StpR':>5} {'AvgH':>5}")
    print(f"  {'─' * 40}─┼{'─' * 60}")

    m_base = compute_metrics(v1)
    for label, trades in variants:
        m = compute_metrics(trades)
        if m["n"] == 0:
            continue
        print_metrics_row(label, m, m_base if label != variants[0][0] else None)

    # ══════════════════════════════════════════════════════════════
    #  TRADES REMOVED AT EACH LAYER
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  TRADES REMOVED AT EACH FILTER LAYER")
    print(f"{'=' * 115}")

    v1_sigs = {t.signal.timestamp for t in v1}
    v2_sigs = {t.signal.timestamp for t in v2}
    v3_sigs = {t.signal.timestamp for t in v3}
    v4_sigs = {t.signal.timestamp for t in v4}

    removed_by_red = [t for t in v1 if t.signal.timestamp not in v2_sigs]
    removed_by_q = [t for t in v2 if t.signal.timestamp not in v3_sigs]
    removed_by_time = [t for t in v3 if t.signal.timestamp not in v4_sigs]

    print(f"\n  Layer 1→2 (Non-RED filter):  removed {len(removed_by_red)} trades")
    if removed_by_red:
        m_rm = compute_metrics(removed_by_red)
        print(f"    Removed: N={m_rm['n']}  WR={m_rm['wr']:.1f}%  PF={pf_str(m_rm['pf'])}  "
              f"PnL={m_rm['pnl']:+.2f}  StpR={m_rm['stop_rate']:.0f}%")
        print(f"    → Removing these {'helps' if m_rm['pnl'] < 0 else 'HURTS'} "
              f"(PnL impact: {-m_rm['pnl']:+.2f})")

    print(f"\n  Layer 2→3 (Q>=2 filter):     removed {len(removed_by_q)} trades")
    if removed_by_q:
        m_rm = compute_metrics(removed_by_q)
        print(f"    Removed: N={m_rm['n']}  WR={m_rm['wr']:.1f}%  PF={pf_str(m_rm['pf'])}  "
              f"PnL={m_rm['pnl']:+.2f}  StpR={m_rm['stop_rate']:.0f}%")
        print(f"    → Removing these {'helps' if m_rm['pnl'] < 0 else 'HURTS'} "
              f"(PnL impact: {-m_rm['pnl']:+.2f})")
    else:
        print(f"    No trades removed (all trades already Q>=2)")

    print(f"\n  Layer 3→4 (<15:30 filter):   removed {len(removed_by_time)} trades")
    if removed_by_time:
        m_rm = compute_metrics(removed_by_time)
        print(f"    Removed: N={m_rm['n']}  WR={m_rm['wr']:.1f}%  PF={pf_str(m_rm['pf'])}  "
              f"PnL={m_rm['pnl']:+.2f}  StpR={m_rm['stop_rate']:.0f}%")
        print(f"    → Removing these {'helps' if m_rm['pnl'] < 0 else 'HURTS'} "
              f"(PnL impact: {-m_rm['pnl']:+.2f})")

        # Show which late trades were removed
        print(f"\n    Late trades removed:")
        for t in sorted(removed_by_time, key=lambda x: x.pnl_points):
            setup = SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id))
            hhmm = signal_hhmm(t)
            print(f"      {t.signal.timestamp.strftime('%Y-%m-%d')} {hhmm:04d}  "
                  f"{setup:<12} PnL={t.pnl_points:+.3f}  Exit={t.exit_reason}")

    # ══════════════════════════════════════════════════════════════
    #  DETAILED BREAKDOWN PER VARIANT
    # ══════════════════════════════════════════════════════════════
    for label, trades in variants:
        if not trades:
            continue
        print(f"\n{'=' * 115}")
        print(f"  DETAIL: {label}")
        print(f"{'=' * 115}")

        print_setup_breakdown(trades, label)
        print_regime_breakdown(trades, spy_day_info, label)
        print_exit_breakdown(trades, label)

    # ══════════════════════════════════════════════════════════════
    #  TRAIN / TEST SPLIT
    # ══════════════════════════════════════════════════════════════
    split_date = date(2026, 2, 21)  # Same split as OOS validation

    print(f"\n{'=' * 115}")
    print(f"  TRAIN / TEST SPLIT (split: {split_date})")
    print(f"{'=' * 115}")

    print(f"\n  {'Variant':<40} │ {'── TRAIN ──':>30} │ {'── TEST ──':>30}")
    print(f"  {'':40} │ {'N':>3} {'WR%':>5} {'PF':>5} {'PnL':>8} {'StpR':>4} │ "
          f"{'N':>3} {'WR%':>5} {'PF':>5} {'PnL':>8} {'StpR':>4}")
    print(f"  {'─' * 40}─┼{'─' * 30}─┼{'─' * 30}")

    for label, trades in variants:
        if not trades:
            continue
        train = [t for t in trades if t.signal.timestamp.date() < split_date]
        test = [t for t in trades if t.signal.timestamp.date() >= split_date]
        mt = compute_metrics(train)
        ms = compute_metrics(test)

        print(f"  {label:<40} │ {mt['n']:>3} {mt['wr']:>5.1f} {pf_str(mt['pf']):>5} "
              f"{mt['pnl']:>+8.2f} {mt['stop_rate']:>4.0f}% │ "
              f"{ms['n']:>3} {ms['wr']:>5.1f} {pf_str(ms['pf']):>5} "
              f"{ms['pnl']:>+8.2f} {ms['stop_rate']:>4.0f}%")

    # ══════════════════════════════════════════════════════════════
    #  EQUITY CURVE COMPARISON (daily P&L)
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  DAILY EQUITY CURVE — VARIANT 1 (baseline) vs VARIANT 4 (full filter)")
    print(f"{'=' * 115}")

    for vname, vtrades in [("V1 Baseline", v1), ("V4 Filtered", v4)]:
        daily_pnl = defaultdict(float)
        for t in vtrades:
            d = t.signal.timestamp.date()
            daily_pnl[d] += t.pnl_points

        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        print(f"\n    {vname}:")
        print(f"    {'Date':<12} {'DayPnL':>8} {'CumPnL':>8} {'DD':>6}")
        print(f"    {'─' * 12} {'─' * 8} {'─' * 8} {'─' * 6}")
        for d in sorted(daily_pnl.keys()):
            cum += daily_pnl[d]
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
            marker = " ←DD" if dd > 0 and dd == max_dd and dd > 0.5 else ""
            print(f"    {d} {daily_pnl[d]:>+8.2f} {cum:>+8.2f} {dd:>6.2f}{marker}")

    # ══════════════════════════════════════════════════════════════
    #  SUMMARY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  SUMMARY")
    print(f"{'=' * 115}")

    m1 = compute_metrics(v1)
    m4 = compute_metrics(v4)
    removed_total = m1["n"] - m4["n"]
    pnl_of_removed = m1["pnl"] - m4["pnl"]

    print(f"""
  Baseline:   {m1['n']} trades, PF {pf_str(m1['pf'])}, WR {m1['wr']:.1f}%, PnL {m1['pnl']:+.2f}, MaxDD {m1['max_dd']:.1f}
  Filtered:   {m4['n']} trades, PF {pf_str(m4['pf'])}, WR {m4['wr']:.1f}%, PnL {m4['pnl']:+.2f}, MaxDD {m4['max_dd']:.1f}

  Trades removed: {removed_total} ({removed_total/m1['n']*100:.0f}% of baseline)
  PnL of removed: {pnl_of_removed:+.2f} pts
  Net effect:     PF {m1['pf']:.2f} → {m4['pf']:.2f}, PnL {m1['pnl']:+.2f} → {m4['pnl']:+.2f}
  MaxDD:          {m1['max_dd']:.1f} → {m4['max_dd']:.1f}
  Stop rate:      {m1['stop_rate']:.0f}% → {m4['stop_rate']:.0f}%
    """)
    print(f"{'=' * 115}\n")


if __name__ == "__main__":
    main()
