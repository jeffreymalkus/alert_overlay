"""
Native short-side research — disciplined feature study for RED day shorts.

Goal: identify short setups specifically suited to RED trend days, using
the same methodology as the long-side feature studies. Do NOT mirror long
logic blindly.

Studies:
  1. Raw short universe — what exists when shorts are unlocked?
  2. Regime filter — RED vs non-RED performance
  3. Setup selection — which setup families produce viable shorts?
  4. Feature study — candle anatomy, EMA alignment, VWAP position, quality
  5. Composite filter candidates — layered filtering for shorts
  6. Best-case combined: long baseline + filtered shorts

Usage:
    python -m alert_overlay.short_research --universe all94
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

        if day_range > 0:
            close_pos = (day_close - day_low) / day_range
            character = "TREND" if (close_pos >= 0.75 or close_pos <= 0.25) else "CHOPPY"
        else:
            character = "CHOPPY"

        ranges_10d.append(day_range)
        day_info[d] = {
            "direction": direction,
            "character": character,
            "spy_change_pct": change_pct,
        }

    return day_info


def compute_metrics(trades, n_days=None):
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "exp": 0.0,
                "stop_rate": 0.0, "max_dd": 0.0, "avg_hold": 0.0, "tpw": 0.0}
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
    td = len(set(t.signal.timestamp.date() for t in trades))
    weeks = (n_days or td) / 5.0
    return {"n": n, "wr": len(wins) / n * 100, "pf": pf, "pnl": pnl,
            "exp": avg_rr, "stop_rate": stopped / n * 100, "max_dd": dd,
            "avg_hold": avg_hold, "tpw": n / weeks if weeks > 0 else 0}


def print_row(label, m, width=35):
    print(f"  {label:<{width}} │ {m['n']:>4} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
          f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['max_dd']:>5.1f} "
          f"{m['stop_rate']:>4.0f}% {m['avg_hold']:>5.1f}b")


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

    all_dates = sorted(spy_day_info.keys())
    n_days = len(all_dates)
    red_dates = {d for d, info in spy_day_info.items() if info["direction"] == "RED"}
    red_trend_dates = {d for d, info in spy_day_info.items()
                       if info["direction"] == "RED" and info["character"] == "TREND"}
    n_red_days = len(red_dates)

    # Config: shorts enabled, all setup paths
    cfg_all_shorts = OverlayConfig()
    cfg_all_shorts.show_ema_scalp = False
    cfg_all_shorts.show_failed_bounce = True
    cfg_all_shorts.show_spencer = False
    cfg_all_shorts.show_ema_fpip = False
    cfg_all_shorts.show_sc_v2 = False
    cfg_all_shorts.show_reversal_setups = False
    cfg_all_shorts.vk_long_only = False
    cfg_all_shorts.sc_long_only = False

    # Run backtest with shorts enabled
    all_trades_raw = []  # (sym, trade)
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
        result = run_backtest(bars, cfg=cfg_all_shorts, spy_bars=spy_bars,
                              qqq_bars=qqq_bars, sector_bars=sec_bars)
        for t in result.trades:
            all_trades_raw.append((sym, t))

    all_trades = [t for _, t in all_trades_raw]
    shorts = [t for t in all_trades if t.signal.direction == -1]
    longs = [t for t in all_trades if t.signal.direction == 1]

    print(f"\n{'=' * 115}")
    print("  NATIVE SHORT-SIDE RESEARCH")
    print(f"{'=' * 115}")
    print(f"  Period: {min(all_dates)} → {max(all_dates)} ({n_days} days)")
    print(f"  Universe: {len(symbols)} symbols")
    print(f"  RED days: {n_red_days}, RED+TREND: {len(red_trend_dates)}")
    print(f"  Total shorts found: {len(shorts)}, Total longs: {len(longs)}")

    # ══════════════════════════════════════════════════════════════
    #  STUDY 1: RAW SHORT UNIVERSE
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  STUDY 1: RAW SHORT UNIVERSE — ALL SHORTS UNLOCKED")
    print(f"{'=' * 115}")

    hdr = (f"  {'Filter':<35} │ {'N':>4} {'WR%':>5} {'PF':>5} "
           f"{'Exp':>7} {'PnL':>8} {'MaxDD':>5} {'StpR':>4} {'Hold':>5}")
    print(f"\n{hdr}")
    print(f"  {'─' * 35}─┼{'─' * 50}")
    print_row("All shorts", compute_metrics(shorts, n_days))

    # By setup
    setup_groups = defaultdict(list)
    for t in shorts:
        name = SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id))
        setup_groups[name].append(t)

    for name in sorted(setup_groups.keys()):
        print_row(f"  {name}", compute_metrics(setup_groups[name], n_days))

    # ══════════════════════════════════════════════════════════════
    #  STUDY 2: REGIME FILTER — RED VS NON-RED
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  STUDY 2: SHORTS BY SPY REGIME")
    print(f"{'=' * 115}")

    regime_groups = defaultdict(list)
    for t in shorts:
        d = t.signal.timestamp.date()
        info = spy_day_info.get(d, {})
        direction = info.get("direction", "UNK")
        character = info.get("character", "UNK")
        regime_groups[f"{direction}+{character}"].append(t)

    print(f"\n{hdr}")
    print(f"  {'─' * 35}─┼{'─' * 50}")
    for key in ["RED+TREND", "RED+CHOPPY", "GREEN+TREND", "GREEN+CHOPPY",
                "FLAT+TREND", "FLAT+CHOPPY"]:
        trades = regime_groups.get(key, [])
        if trades:
            print_row(key, compute_metrics(trades))
        else:
            print(f"  {key:<35} │    0 trades")

    # Summary: RED-only shorts
    red_shorts = [t for t in shorts if t.signal.timestamp.date() in red_dates]
    non_red_shorts = [t for t in shorts if t.signal.timestamp.date() not in red_dates]

    print(f"\n  Aggregate:")
    print(f"\n{hdr}")
    print(f"  {'─' * 35}─┼{'─' * 50}")
    print_row("RED-day shorts", compute_metrics(red_shorts, n_red_days))
    print_row("Non-RED-day shorts", compute_metrics(non_red_shorts, n_days - n_red_days))

    # ══════════════════════════════════════════════════════════════
    #  STUDY 3: SETUP × REGIME MATRIX
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  STUDY 3: SETUP × REGIME MATRIX (shorts only)")
    print(f"{'=' * 115}")

    for setup_name in sorted(setup_groups.keys()):
        print(f"\n  {setup_name}:")
        srg = defaultdict(list)
        for t in setup_groups[setup_name]:
            d = t.signal.timestamp.date()
            info = spy_day_info.get(d, {})
            srg[f"{info.get('direction', 'UNK')}+{info.get('character', 'UNK')}"].append(t)

        for key in ["RED+TREND", "RED+CHOPPY", "GREEN+TREND", "GREEN+CHOPPY",
                    "FLAT+TREND", "FLAT+CHOPPY"]:
            trades = srg.get(key, [])
            if trades:
                m = compute_metrics(trades)
                print(f"    {key:<20} N={m['n']:>3}  WR={m['wr']:>5.1f}%  PF={pf_str(m['pf']):>5}  "
                      f"Exp={m['exp']:>+6.3f}R  PnL={m['pnl']:>+7.2f}  StpR={m['stop_rate']:>4.0f}%")

    # ══════════════════════════════════════════════════════════════
    #  STUDY 4: FEATURE ANALYSIS ON RED-DAY SHORTS
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  STUDY 4: FEATURE ANALYSIS — RED-DAY SHORTS")
    print(f"{'=' * 115}")

    target = red_shorts  # focus on RED-day shorts
    if not target:
        print("  No RED-day shorts found.")
    else:
        # 4a: Quality score
        print(f"\n  ── By Quality Score ──")
        q_groups = defaultdict(list)
        for t in target:
            q_groups[t.signal.quality_score].append(t)
        for q in sorted(q_groups):
            m = compute_metrics(q_groups[q])
            print(f"    Q={q}: N={m['n']:>3}  WR={m['wr']:>5.1f}%  PF={pf_str(m['pf']):>5}  "
                  f"Exp={m['exp']:>+6.3f}R  PnL={m['pnl']:>+7.2f}")

        # 4b: Time of day
        print(f"\n  ── By Time of Day ──")
        time_groups = defaultdict(list)
        for t in target:
            ts = t.signal.timestamp
            hhmm = ts.hour * 100 + ts.minute
            if hhmm < 1000:
                bucket = "09:30-10:00"
            elif hhmm < 1100:
                bucket = "10:00-11:00"
            elif hhmm < 1300:
                bucket = "11:00-13:00"
            elif hhmm < 1500:
                bucket = "13:00-15:00"
            else:
                bucket = "15:00-15:55"
            time_groups[bucket].append(t)

        for bucket in ["09:30-10:00", "10:00-11:00", "11:00-13:00", "13:00-15:00", "15:00-15:55"]:
            trades = time_groups.get(bucket, [])
            if trades:
                m = compute_metrics(trades)
                print(f"    {bucket}: N={m['n']:>3}  WR={m['wr']:>5.1f}%  PF={pf_str(m['pf']):>5}  "
                      f"PnL={m['pnl']:>+7.2f}")

        # 4c: Exit reason
        print(f"\n  ── By Exit Reason ──")
        exit_groups = defaultdict(list)
        for t in target:
            exit_groups[t.exit_reason].append(t)
        for reason in sorted(exit_groups.keys()):
            m = compute_metrics(exit_groups[reason])
            print(f"    {reason:<12}: N={m['n']:>3}  WR={m['wr']:>5.1f}%  PnL={m['pnl']:>+7.2f}")

        # 4d: Market trend tag
        print(f"\n  ── By Market Trend Tag ──")
        trend_groups = defaultdict(list)
        for t in target:
            mt = getattr(t.signal, 'market_trend', None)
            if mt:
                trend_groups[str(mt)].append(t)
            else:
                trend_groups["None"].append(t)
        for tag in sorted(trend_groups.keys()):
            m = compute_metrics(trend_groups[tag])
            print(f"    {tag:<20}: N={m['n']:>3}  WR={m['wr']:>5.1f}%  PF={pf_str(m['pf']):>5}  "
                  f"PnL={m['pnl']:>+7.2f}")

        # 4e: VWAP bias
        print(f"\n  ── By VWAP Bias ──")
        vwap_groups = defaultdict(list)
        for t in target:
            vb = getattr(t.signal, 'vwap_bias', None)
            if vb:
                vwap_groups[str(vb)].append(t)
            else:
                vwap_groups["None"].append(t)
        for tag in sorted(vwap_groups.keys()):
            m = compute_metrics(vwap_groups[tag])
            print(f"    {tag:<20}: N={m['n']:>3}  WR={m['wr']:>5.1f}%  PF={pf_str(m['pf']):>5}  "
                  f"PnL={m['pnl']:>+7.2f}")

        # 4f: Bars held distribution
        print(f"\n  ── Hold Time ──")
        hold_groups = {"1-3 bars": [], "4-8 bars": [], "9-15 bars": [], "16+ bars": []}
        for t in target:
            if t.bars_held <= 3:
                hold_groups["1-3 bars"].append(t)
            elif t.bars_held <= 8:
                hold_groups["4-8 bars"].append(t)
            elif t.bars_held <= 15:
                hold_groups["9-15 bars"].append(t)
            else:
                hold_groups["16+ bars"].append(t)
        for label in ["1-3 bars", "4-8 bars", "9-15 bars", "16+ bars"]:
            trades = hold_groups[label]
            if trades:
                m = compute_metrics(trades)
                print(f"    {label:<12}: N={m['n']:>3}  WR={m['wr']:>5.1f}%  PnL={m['pnl']:>+7.2f}")

    # ══════════════════════════════════════════════════════════════
    #  STUDY 5: COMPOSITE FILTER CANDIDATES
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  STUDY 5: COMPOSITE FILTER CANDIDATES — SHORTS")
    print(f"{'=' * 115}")

    # Build filter combinations
    def hhmm(t):
        ts = t.signal.timestamp
        return ts.hour * 100 + ts.minute

    # Filters to test on shorts:
    f_all = shorts
    f_red = [t for t in shorts if t.signal.timestamp.date() in red_dates]
    f_red_trend = [t for t in shorts if t.signal.timestamp.date() in red_trend_dates]
    f_q4_plus = [t for t in shorts if t.signal.quality_score >= 4]
    f_q5_plus = [t for t in shorts if t.signal.quality_score >= 5]
    f_morning = [t for t in shorts if hhmm(t) < 1300]
    f_pre_1530 = [t for t in shorts if hhmm(t) < 1530]

    # Layered candidates
    candidates = [
        ("All shorts",                                   f_all),
        ("RED-day only",                                 f_red),
        ("RED+TREND only",                               f_red_trend),
        ("RED + Q≥4",                                    [t for t in f_red if t.signal.quality_score >= 4]),
        ("RED + Q≥5",                                    [t for t in f_red if t.signal.quality_score >= 5]),
        ("RED + <13:00",                                 [t for t in f_red if hhmm(t) < 1300]),
        ("RED + <15:30",                                 [t for t in f_red if hhmm(t) < 1530]),
        ("RED + Q≥4 + <13:00",                           [t for t in f_red if t.signal.quality_score >= 4 and hhmm(t) < 1300]),
        ("RED + Q≥4 + <15:30",                           [t for t in f_red if t.signal.quality_score >= 4 and hhmm(t) < 1530]),
        ("RED + Q≥5 + <15:30",                           [t for t in f_red if t.signal.quality_score >= 5 and hhmm(t) < 1530]),
        ("RED+TREND + Q≥4",                              [t for t in f_red_trend if t.signal.quality_score >= 4]),
        ("RED+TREND + Q≥5",                              [t for t in f_red_trend if t.signal.quality_score >= 5]),
    ]

    # Per-setup candidates
    for setup_name in sorted(setup_groups.keys()):
        setup_shorts_red = [t for t in setup_groups[setup_name]
                            if t.signal.timestamp.date() in red_dates]
        if setup_shorts_red:
            candidates.append((f"RED + {setup_name} only", setup_shorts_red))

    print(f"\n  {'Candidate':<35} │ {'N':>4} {'WR%':>5} {'PF':>5} "
          f"{'Exp':>7} {'PnL':>8} {'MaxDD':>5} {'StpR':>4}")
    print(f"  {'─' * 35}─┼{'─' * 43}")

    best_pf = 0
    best_label = ""
    for label, trades in candidates:
        if not trades:
            print(f"  {label:<35} │    0 trades")
            continue
        m = compute_metrics(trades)
        star = " ★" if m["pf"] > 1.5 and m["n"] >= 5 else ""
        print(f"  {label:<35} │ {m['n']:>4} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
              f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['max_dd']:>5.1f} "
              f"{m['stop_rate']:>4.0f}%{star}")
        if m["pf"] > best_pf and m["n"] >= 5 and m["pnl"] > 0:
            best_pf = m["pf"]
            best_label = label

    if best_label:
        print(f"\n  ★ Best viable candidate: {best_label} (PF={pf_str(best_pf)})")

    # ══════════════════════════════════════════════════════════════
    #  STUDY 6: COMBINED — LONG BASELINE + FILTERED SHORTS
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  STUDY 6: COMBINED — LONG BASELINE + SHORT CANDIDATES")
    print(f"{'=' * 115}")

    # Get locked long baseline
    cfg_long = OverlayConfig()
    cfg_long.show_ema_scalp = False
    cfg_long.show_failed_bounce = False
    cfg_long.show_spencer = False
    cfg_long.show_ema_fpip = False
    cfg_long.show_sc_v2 = False

    long_trades = []
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
        result = run_backtest(bars, cfg=cfg_long, spy_bars=spy_bars,
                              qqq_bars=qqq_bars, sector_bars=sec_bars)
        long_trades.extend(result.trades)

    # Apply locked long filters
    def apply_long_filters(trades):
        filtered = []
        for t in trades:
            d = t.signal.timestamp.date()
            info = spy_day_info.get(d, {})
            if info.get("direction") == "RED":
                continue
            if t.signal.quality_score < 2:
                continue
            hh = t.signal.timestamp.hour * 100 + t.signal.timestamp.minute
            if hh >= 1530:
                continue
            filtered.append(t)
        return filtered

    long_filtered = apply_long_filters(long_trades)
    m_long = compute_metrics(long_filtered, n_days)

    # Test various short additions
    short_variants = [
        ("RED + Q≥4 shorts",    [t for t in f_red if t.signal.quality_score >= 4]),
        ("RED + Q≥5 shorts",    [t for t in f_red if t.signal.quality_score >= 5]),
        ("RED + <15:30 shorts", [t for t in f_red if hhmm(t) < 1530]),
        ("RED + Q≥4 + <15:30",  [t for t in f_red if t.signal.quality_score >= 4 and hhmm(t) < 1530]),
        ("RED + Q≥5 + <15:30",  [t for t in f_red if t.signal.quality_score >= 5 and hhmm(t) < 1530]),
    ]

    print(f"\n  {'Combination':<40} │ {'N':>4} {'WR%':>5} {'PF':>5} "
          f"{'Exp':>7} {'PnL':>8} {'MaxDD':>5}")
    print(f"  {'─' * 40}─┼{'─' * 38}")

    print_row("Long baseline only", m_long, 40)

    for label, short_subset in short_variants:
        if not short_subset:
            print(f"  {f'Long + {label}':<40} │    0 short trades to add")
            continue
        combined = sorted(long_filtered + short_subset, key=lambda t: t.signal.timestamp)
        mc = compute_metrics(combined, n_days)
        ms = compute_metrics(short_subset, n_days)
        delta_pnl = mc['pnl'] - m_long['pnl']
        print(f"  {'Long + ' + label:<40} │ {mc['n']:>4} {mc['wr']:>5.1f}% {pf_str(mc['pf']):>5} "
              f"{mc['exp']:>+6.3f}R {mc['pnl']:>+8.2f} {mc['max_dd']:>5.1f}"
              f"  (shorts: N={ms['n']}, PnL={ms['pnl']:+.2f}, Δ={delta_pnl:+.2f})")

    # ══════════════════════════════════════════════════════════════
    #  STUDY 7: WORST SHORT TRADES (for pattern recognition)
    # ══════════════════════════════════════════════════════════════
    if red_shorts:
        print(f"\n{'=' * 115}")
        print("  RED-DAY SHORT TRADE LOG (for pattern recognition)")
        print(f"{'=' * 115}")

        # Sort by PnL
        sorted_shorts = sorted(red_shorts, key=lambda t: t.pnl_points)

        print(f"\n  ── All RED-day shorts (sorted by PnL) ──")
        print(f"  {'#':>3} {'Date':>12} {'HHMM':>5} {'Sym':<6} {'Setup':<14} "
              f"{'Q':>2} {'PnL':>8} {'R:R':>6} {'Exit':<10} {'Bars':>4} {'Regime':<14}")
        print(f"  {'─' * 90}")

        for i, t in enumerate(sorted_shorts, 1):
            # Find symbol
            sym = "?"
            for s, tr in all_trades_raw:
                if tr is t:
                    sym = s
                    break
            setup = SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id))
            d = t.signal.timestamp.date()
            info = spy_day_info.get(d, {})
            regime = f"{info.get('direction', '?')}+{info.get('character', '?')}"
            ts = t.signal.timestamp
            hh = ts.hour * 100 + ts.minute
            print(f"  {i:>3} {d} {hh:>5} {sym:<6} {setup:<14} "
                  f"{t.signal.quality_score:>2} {t.pnl_points:>+8.3f} {t.pnl_rr:>+5.2f}R "
                  f"{t.exit_reason:<10} {t.bars_held:>4} {regime:<14}")

    # ══════════════════════════════════════════════════════════════
    #  SUMMARY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  SHORT-SIDE RESEARCH SUMMARY")
    print(f"{'=' * 115}")

    print(f"""
  Period: {min(all_dates)} → {max(all_dates)} ({n_days} days)
  Universe: {len(symbols)} symbols
  RED days: {n_red_days} ({n_red_days/n_days*100:.0f}% of all days)

  Raw short universe: {len(shorts)} trades, PF={pf_str(compute_metrics(shorts)['pf'])}, PnL={compute_metrics(shorts)['pnl']:+.2f}
  RED-day shorts:     {len(red_shorts)} trades, PF={pf_str(compute_metrics(red_shorts)['pf']) if red_shorts else '—'}, PnL={compute_metrics(red_shorts)['pnl']:+.2f if red_shorts else 0}

  Long baseline:      N={m_long['n']}, PF={pf_str(m_long['pf'])}, PnL={m_long['pnl']:+.2f}
""")
    if best_label:
        print(f"  ★ Best short candidate: {best_label}")
        best_trades = [t for l, ts in candidates if l == best_label for t in ts]
        # fallback
        for l, ts in candidates:
            if l == best_label:
                best_trades = ts
                break
        mb = compute_metrics(best_trades)
        print(f"    N={mb['n']}, PF={pf_str(mb['pf'])}, PnL={mb['pnl']:+.2f}")

    print(f"{'=' * 115}\n")


if __name__ == "__main__":
    main()
