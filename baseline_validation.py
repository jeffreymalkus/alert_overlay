"""
Locked baseline validation — broad stability test.

Locked baseline:
  - VWAP_KISS + SECOND_CHANCE with strong BO volume
  - No entries on RED SPY days
  - No entries after 15:30
  - Q >= 2 guardrail

Reports:
  1. Full OOS metrics (all 94 symbols)
  2. Walk-forward (train/test split)
  3. Corrected 1-min execution comparison (5-min vs auto-0m)
  4. Setup-level contribution (VK only, SC only, combined)
  5. Regime breakdown (GREEN+TREND, GREEN+CHOPPY, RED+TREND, RED+CHOPPY)
  6. Trades/week, PF, expectancy, drawdown, stop rate

Usage:
    python -m alert_overlay.baseline_validation --universe all94
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
ONEMIN_DIR = DATA_DIR / "1min"
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


# ── Locked baseline filters ──────────────────────────────────────

def apply_locked_filters(trades, spy_day_info):
    """Apply the 3 locked baseline filters: Non-RED, Q>=2, <15:30."""
    filtered = []
    for t in trades:
        d = t.signal.timestamp.date()
        info = spy_day_info.get(d, {})
        direction = info.get("direction", "UNK")
        if direction == "RED":
            continue
        if t.signal.quality_score < 2:
            continue
        ts = t.signal.timestamp
        hhmm = ts.hour * 100 + ts.minute
        if hhmm >= 1530:
            continue
        filtered.append(t)
    return filtered


def apply_tradability_filters(trades, long_thresh=-0.3, short_thresh=-0.3):
    """Apply tradability-based filters: Q>=2, <15:30, tradability score gate."""
    filtered = []
    for t in trades:
        if t.signal.quality_score < 2:
            continue
        ts = t.signal.timestamp
        hhmm = ts.hour * 100 + ts.minute
        if hhmm >= 1530:
            continue
        if t.signal.direction == 1:
            if t.signal.tradability_long < long_thresh:
                continue
        else:
            if t.signal.tradability_short > -short_thresh:
                continue
        filtered.append(t)
    return filtered


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

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

    # Frozen config
    cfg = OverlayConfig()
    cfg.show_ema_scalp = False
    cfg.show_failed_bounce = False
    cfg.show_spencer = False
    cfg.show_ema_fpip = False
    cfg.show_sc_v2 = False

    # Run backtest — collect all unfiltered trades
    all_trades = []
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

    all_dates = sorted(spy_day_info.keys())
    n_days = len(all_dates)

    # Apply locked filters
    filtered = apply_locked_filters(all_trades, spy_day_info)

    # Setup-level splits
    vk_all = [t for t in all_trades if t.signal.setup_id == SetupId.VWAP_KISS]
    sc_all = [t for t in all_trades if t.signal.setup_id == SetupId.SECOND_CHANCE]
    vk_filt = [t for t in filtered if t.signal.setup_id == SetupId.VWAP_KISS]
    sc_filt = [t for t in filtered if t.signal.setup_id == SetupId.SECOND_CHANCE]

    red_days = sum(1 for d, info in spy_day_info.items() if info["direction"] == "RED")
    green_days = sum(1 for d, info in spy_day_info.items() if info["direction"] == "GREEN")
    flat_days = n_days - red_days - green_days

    # ══════════════════════════════════════════════════════════════
    #  SECTION 1: FULL OOS — LOCKED BASELINE
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  LOCKED BASELINE VALIDATION — FULL OOS")
    print(f"{'=' * 115}")
    print(f"  Period: {min(all_dates)} → {max(all_dates)} ({n_days} days)")
    print(f"  Universe: {len(symbols)} symbols")
    print(f"  SPY days: {green_days} GREEN, {red_days} RED, {flat_days} FLAT")
    print(f"  Filters: Non-RED + Q≥2 + <15:30")

    m_unfilt = compute_metrics(all_trades, n_days)
    m_filt = compute_metrics(filtered, n_days)

    print(f"\n  {'Variant':<35} │ {'N':>4} {'T/Wk':>5} {'WR%':>5} {'PF':>5} "
          f"{'Exp':>7} {'PnL':>8} {'MaxDD':>5} {'StpR':>5} {'AvgH':>5}")
    print(f"  {'─' * 35}─┼{'─' * 58}")
    for label, m in [("Unfiltered (reference)", m_unfilt), ("LOCKED BASELINE", m_filt)]:
        print(f"  {label:<35} │ {m['n']:>4} {m['tpw']:>5.1f} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
              f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['max_dd']:>5.1f} "
              f"{m['stop_rate']:>5.0f}% {m['avg_hold']:>5.1f}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 2: SETUP-LEVEL CONTRIBUTION
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  SETUP-LEVEL CONTRIBUTION (locked baseline)")
    print(f"{'=' * 115}")

    print(f"\n  {'Setup':<20} │ {'N':>4} {'T/Wk':>5} {'WR%':>5} {'PF':>5} "
          f"{'Exp':>7} {'PnL':>8} {'MaxDD':>5} {'StpR':>5} {'AvgH':>5}")
    print(f"  {'─' * 20}─┼{'─' * 58}")
    for label, trades in [("VK only", vk_filt), ("SC only", sc_filt), ("Combined", filtered)]:
        m = compute_metrics(trades, n_days)
        print(f"  {label:<20} │ {m['n']:>4} {m['tpw']:>5.1f} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
              f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['max_dd']:>5.1f} "
              f"{m['stop_rate']:>5.0f}% {m['avg_hold']:>5.1f}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 3: REGIME BREAKDOWN
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  REGIME BREAKDOWN (locked baseline)")
    print(f"{'=' * 115}")

    regime_groups = defaultdict(list)
    for t in filtered:
        d = t.signal.timestamp.date()
        info = spy_day_info.get(d, {})
        direction = info.get("direction", "UNK")
        character = info.get("character", "UNK")
        regime_groups[f"{direction}+{character}"].append(t)

    print(f"\n  {'Regime':<20} │ {'N':>4} {'T/Wk':>5} {'WR%':>5} {'PF':>5} "
          f"{'Exp':>7} {'PnL':>8} {'MaxDD':>5} {'StpR':>5}")
    print(f"  {'─' * 20}─┼{'─' * 48}")
    for key in ["GREEN+TREND", "GREEN+CHOPPY", "FLAT+TREND", "FLAT+CHOPPY",
                "RED+TREND", "RED+CHOPPY"]:
        trades = regime_groups.get(key, [])
        m = compute_metrics(trades, n_days)
        if m["n"] == 0:
            print(f"  {key:<20} │ {0:>4}   —")
            continue
        print(f"  {key:<20} │ {m['n']:>4} {m['tpw']:>5.1f} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
              f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['max_dd']:>5.1f} "
              f"{m['stop_rate']:>5.0f}%")

    # Per-setup × per-regime
    print(f"\n  Per-setup regime breakdown:")
    for setup_label, setup_trades in [("VK", vk_filt), ("SC", sc_filt)]:
        print(f"\n    {setup_label}:")
        sg = defaultdict(list)
        for t in setup_trades:
            d = t.signal.timestamp.date()
            info = spy_day_info.get(d, {})
            sg[f"{info.get('direction', 'UNK')}+{info.get('character', 'UNK')}"].append(t)
        for key in ["GREEN+TREND", "GREEN+CHOPPY", "FLAT+TREND", "FLAT+CHOPPY"]:
            trades = sg.get(key, [])
            if not trades:
                continue
            m = compute_metrics(trades)
            print(f"      {key:<18} N={m['n']:>3}  WR={m['wr']:>5.1f}%  PF={pf_str(m['pf']):>5}  "
                  f"PnL={m['pnl']:>+7.2f}  StpR={m['stop_rate']:>4.0f}%")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 4: WALK-FORWARD (TRAIN / TEST)
    # ══════════════════════════════════════════════════════════════
    split_date = date(2026, 2, 21)

    print(f"\n{'=' * 115}")
    print(f"  WALK-FORWARD SPLIT (train < {split_date}, test ≥ {split_date})")
    print(f"{'=' * 115}")

    train = [t for t in filtered if t.signal.timestamp.date() < split_date]
    test = [t for t in filtered if t.signal.timestamp.date() >= split_date]

    train_dates = [d for d in all_dates if d < split_date]
    test_dates = [d for d in all_dates if d >= split_date]

    mt = compute_metrics(train, len(train_dates))
    ms = compute_metrics(test, len(test_dates))

    print(f"\n  {'Period':<15} │ {'N':>4} {'T/Wk':>5} {'WR%':>5} {'PF':>5} "
          f"{'Exp':>7} {'PnL':>8} {'MaxDD':>5} {'StpR':>5}")
    print(f"  {'─' * 15}─┼{'─' * 48}")
    for label, m, n_d in [("Train", mt, len(train_dates)), ("Test", ms, len(test_dates))]:
        print(f"  {label:<15} │ {m['n']:>4} {m['tpw']:>5.1f} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
              f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['max_dd']:>5.1f} "
              f"{m['stop_rate']:>5.0f}%")

    # Setup-level in each period
    for period_label, period_trades in [("Train", train), ("Test", test)]:
        print(f"\n    {period_label} by setup:")
        for setup_label, sid in [("VK", SetupId.VWAP_KISS), ("SC", SetupId.SECOND_CHANCE)]:
            st = [t for t in period_trades if t.signal.setup_id == sid]
            m = compute_metrics(st)
            if m["n"] == 0:
                continue
            print(f"      {setup_label:<6} N={m['n']:>3}  WR={m['wr']:>5.1f}%  PF={pf_str(m['pf']):>5}  "
                  f"PnL={m['pnl']:>+7.2f}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 5: 1-MIN EXECUTION COMPARISON
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  1-MIN EXECUTION COMPARISON (locked baseline, filtered)")
    print(f"{'=' * 115}")

    from .exec_sim_1min import simulate_1min_execution, extract_5min_signals, ExecTrade
    from .exec_sim_1min import compute_metrics as exec_compute_metrics

    # Only symbols with 1-min data
    onemin_syms = sorted(set(
        p.stem.replace("_1min", "") for p in ONEMIN_DIR.glob("*_1min.csv")
    ))
    common_syms = [s for s in onemin_syms if s not in ("SPY", "QQQ") and s in symbols]

    # Load 1-min bars
    bars_1min = {}
    for sym in common_syms:
        p = ONEMIN_DIR / f"{sym}_1min.csv"
        if p.exists():
            bars_1min[sym] = load_bars_from_csv(str(p))

    # Load 5-min bars for common symbols
    bars_5min = {}
    for sym in common_syms:
        p = DATA_DIR / f"{sym}_5min.csv"
        if p.exists():
            bars_5min[sym] = load_bars_from_csv(str(p))

    common_syms = sorted(set(bars_5min.keys()) & set(bars_1min.keys()))
    print(f"  Symbols with 1-min data: {len(common_syms)}")

    # 5-min baseline (filtered) on common symbols only
    baseline_5min_trades = []
    for sym in common_syms:
        sec_bars = None
        se = get_sector_etf(sym)
        if se and se in sector_bars_dict:
            sec_bars = sector_bars_dict[se]
        result = run_backtest(bars_5min[sym], cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        baseline_5min_trades.extend(result.trades)

    baseline_5min_filt = apply_locked_filters(baseline_5min_trades, spy_day_info)

    # 1-min auto execution (filtered) on common symbols
    sim_trades = []
    killed = 0
    total_sigs = 0
    for sym in common_syms:
        sec_bars = None
        se = get_sector_etf(sym)
        if se and se in sector_bars_dict:
            sec_bars = sector_bars_dict[se]
        signals = extract_5min_signals(bars_5min[sym], cfg,
                                        spy_bars=spy_bars, qqq_bars=qqq_bars,
                                        sector_bars=sec_bars)
        # Apply locked filters to signals
        for sig in signals:
            d = sig.timestamp.date()
            info = spy_day_info.get(d, {})
            if info.get("direction") == "RED":
                continue
            if sig.quality_score < 2:
                continue
            hhmm = sig.timestamp.hour * 100 + sig.timestamp.minute
            if hhmm >= 1530:
                continue

            total_sigs += 1
            onemin = bars_1min.get(sym, [])
            if not onemin:
                continue
            et = simulate_1min_execution(sig, onemin, cfg,
                                          entry_delay_minutes=0,
                                          session_end_hhmm=1555)
            if et is None:
                killed += 1
            else:
                sim_trades.append(et)

    # Compare
    m_5 = compute_metrics(baseline_5min_filt, n_days)

    # Compute exec metrics manually for ExecTrade objects
    def exec_metrics(trades, n_days=None):
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
            if cum > pk: pk = cum
            if pk - cum > dd: dd = pk - cum
        avg_hold = statistics.mean(t.bars_held_5min_equiv for t in trades)
        td = len(set(t.signal.timestamp.date() for t in trades))
        weeks = (n_days or td) / 5.0
        return {"n": n, "wr": len(wins)/n*100, "pf": pf, "pnl": pnl,
                "exp": avg_rr, "stop_rate": stopped/n*100, "max_dd": dd,
                "avg_hold": avg_hold, "tpw": n/weeks if weeks > 0 else 0}

    m_1 = exec_metrics(sim_trades, n_days)
    fill_rate = len(sim_trades) / total_sigs * 100 if total_sigs > 0 else 0

    print(f"\n  Filtered signals sent to 1-min sim: {total_sigs}")
    print(f"  Killed before fill: {killed} ({killed/total_sigs*100:.0f}%)" if total_sigs > 0 else "")
    print(f"  Fill rate: {fill_rate:.0f}%")

    print(f"\n  {'Mode':<30} │ {'N':>4} {'T/Wk':>5} {'WR%':>5} {'PF':>5} "
          f"{'Exp':>7} {'PnL':>8} {'MaxDD':>5} {'StpR':>5}")
    print(f"  {'─' * 30}─┼{'─' * 48}")
    for label, m in [("5-min idealized (filtered)", m_5), ("1-min auto (filtered)", m_1)]:
        print(f"  {label:<30} │ {m['n']:>4} {m['tpw']:>5.1f} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
              f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['max_dd']:>5.1f} "
              f"{m['stop_rate']:>5.0f}%")

    # Delta
    print(f"\n  Degradation (1-min vs 5-min):")
    print(f"    ΔN:    {m_1['n'] - m_5['n']:+d}")
    print(f"    ΔWR:   {m_1['wr'] - m_5['wr']:+.1f}%")
    dpf = m_1['pf'] - m_5['pf'] if m_5['pf'] < 999 and m_1['pf'] < 999 else 0
    print(f"    ΔPF:   {dpf:+.2f}")
    print(f"    ΔPnL:  {m_1['pnl'] - m_5['pnl']:+.2f}")
    print(f"    ΔDD:   {m_1['max_dd'] - m_5['max_dd']:+.1f}")
    print(f"    ΔStpR: {m_1['stop_rate'] - m_5['stop_rate']:+.1f}%")

    # Killed trades: what were they in 5-min?
    sim_sigs = {t.signal.timestamp for t in sim_trades}
    killed_5min = [t for t in baseline_5min_filt if t.signal.timestamp not in sim_sigs
                   and t.signal.timestamp.date() in {s.signal.timestamp.date() for s in sim_trades}]
    # Only count killed from symbols we had 1-min data for
    common_set = set(common_syms)
    # We can't easily get symbol from Trade, but we can check killed count
    if killed > 0:
        print(f"\n  Killed trade analysis:")
        print(f"    {killed} trades killed before 1-min fill (stop hit in delay window)")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 6: EXIT REASON BREAKDOWN
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  EXIT REASON BREAKDOWN (locked baseline)")
    print(f"{'=' * 115}")

    exit_groups = defaultdict(list)
    for t in filtered:
        exit_groups[t.exit_reason].append(t)

    print(f"\n  {'Reason':<12} │ {'N':>4} {'WR%':>5} {'PF':>5} {'PnL':>8} {'AvgR':>7}")
    print(f"  {'─' * 12}─┼{'─' * 32}")
    for reason in sorted(exit_groups.keys()):
        trades = exit_groups[reason]
        m = compute_metrics(trades)
        print(f"  {reason:<12} │ {m['n']:>4} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
              f"{m['pnl']:>+8.2f} {m['exp']:>+6.3f}R")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 7: DAILY EQUITY CURVE
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  DAILY EQUITY CURVE (locked baseline)")
    print(f"{'=' * 115}")

    daily_pnl = defaultdict(float)
    for t in filtered:
        d = t.signal.timestamp.date()
        daily_pnl[d] += t.pnl_points

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_date = None
    winning_days = 0
    losing_days = 0
    print(f"\n  {'Date':<12} {'DayPnL':>8} {'CumPnL':>8} {'DD':>6} {'Trades':>6}")
    print(f"  {'─' * 12} {'─' * 8} {'─' * 8} {'─' * 6} {'─' * 6}")
    for d in sorted(daily_pnl.keys()):
        n_trades_day = sum(1 for t in filtered if t.signal.timestamp.date() == d)
        cum += daily_pnl[d]
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
            max_dd_date = d
        if daily_pnl[d] > 0:
            winning_days += 1
        elif daily_pnl[d] < 0:
            losing_days += 1
        marker = " ←DD" if dd > 0 and dd == max_dd and dd > 0.5 else ""
        print(f"  {d} {daily_pnl[d]:>+8.2f} {cum:>+8.2f} {dd:>6.2f} {n_trades_day:>6}{marker}")

    trade_days = len(daily_pnl)
    print(f"\n  Trading days: {trade_days}  |  Win days: {winning_days}  |  "
          f"Lose days: {losing_days}  |  Win day rate: "
          f"{winning_days/trade_days*100:.0f}%" if trade_days > 0 else "")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 8: WORST TRADES
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  5 WORST TRADES (locked baseline)")
    print(f"{'=' * 115}")

    worst = sorted(filtered, key=lambda t: t.pnl_points)[:5]
    for t in worst:
        setup = SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id))
        d = t.signal.timestamp.date()
        info = spy_day_info.get(d, {})
        regime = f"{info.get('direction', '?')}+{info.get('character', '?')}"
        print(f"  {t.signal.timestamp.strftime('%Y-%m-%d %H:%M')}  {setup:<12}  "
              f"PnL={t.pnl_points:+.3f}  Exit={t.exit_reason:<8}  "
              f"Q={t.signal.quality_score}  Regime={regime}")

    # ══════════════════════════════════════════════════════════════
    #  SUMMARY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 115}")
    print("  LOCKED BASELINE VALIDATION SUMMARY")
    print(f"{'=' * 115}")
    print(f"""
  Locked baseline: VK + SC_VOL, Non-RED, Q≥2, <15:30
  Period:          {min(all_dates)} → {max(all_dates)} ({n_days} days, {n_days/5:.0f} weeks)
  Universe:        {len(symbols)} symbols

  ── Full OOS ──
  Trades:     {m_filt['n']}  ({m_filt['tpw']:.1f}/week)
  Win rate:   {m_filt['wr']:.1f}%
  PF:         {pf_str(m_filt['pf'])}
  Expectancy: {m_filt['exp']:+.3f}R
  PnL:        {m_filt['pnl']:+.2f} pts
  MaxDD:      {m_filt['max_dd']:.1f} pts
  Stop rate:  {m_filt['stop_rate']:.0f}%

  ── Walk-forward ──
  Train: N={mt['n']}  PF={pf_str(mt['pf'])}  PnL={mt['pnl']:+.2f}
  Test:  N={ms['n']}  PF={pf_str(ms['pf'])}  PnL={ms['pnl']:+.2f}

  ── 1-min execution (common symbols) ──
  5-min:  N={m_5['n']}  PF={pf_str(m_5['pf'])}  PnL={m_5['pnl']:+.2f}
  1-min:  N={m_1['n']}  PF={pf_str(m_1['pf'])}  PnL={m_1['pnl']:+.2f}
  Gap:    {m_1['pnl'] - m_5['pnl']:+.2f} pts

  ── Setup contribution ──
  VK:       N={compute_metrics(vk_filt)['n']}  PF={pf_str(compute_metrics(vk_filt)['pf'])}  PnL={compute_metrics(vk_filt)['pnl']:+.2f}
  SC:       N={compute_metrics(sc_filt)['n']}  PF={pf_str(compute_metrics(sc_filt)['pf'])}  PnL={compute_metrics(sc_filt)['pnl']:+.2f}
""")
    print(f"{'=' * 115}\n")


if __name__ == "__main__":
    main()
