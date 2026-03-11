"""
Robustness evaluation — locked baseline: VK + SC_VOL.

Compares VK only vs VK + SC_VOL across:
  - Full period, train, test
  - SPY regime breakdown: green/red days, high-vol, choppy/flat
  - Realistic slippage (dynamic model)
  - Daily stop frequency, trades/week, drawdown, expectancy, PF
  - Exit reason distribution
  - Avg hold time (bars)

Usage:
    python -m alert_overlay.robustness_eval --universe all94
"""

import argparse
import math
import statistics
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import (
    NaN, SetupId, SetupFamily, SETUP_DISPLAY_NAME, SETUP_FAMILY_MAP,
)

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent / "data"
WATCHLIST_FILE = Path(__file__).parent / "watchlist.txt"


# ── helpers ──────────────────────────────────────────────────────────

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


# ── SPY day classification ────────────────────────────────────────────

def classify_spy_days(spy_bars):
    """
    Classify each trading day by SPY behavior:
      - direction: GREEN (close > open), RED (close < open)
      - volatility: HIGH_VOL (daily range > 1.5× 10d avg range), NORMAL
      - character: TREND (close near high/low extreme), CHOPPY (close near mid)

    Returns: {date_obj: {"direction": str, "volatility": str, "character": str, "spy_change_pct": float}}
    """
    # Group bars by date
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

        # Direction
        change_pct = (day_close - day_open) / day_open * 100 if day_open > 0 else 0
        if change_pct > 0.05:
            direction = "GREEN"
        elif change_pct < -0.05:
            direction = "RED"
        else:
            direction = "FLAT"

        # Volatility (vs trailing 10-day avg range)
        ranges_10d.append(day_range)
        if len(ranges_10d) > 10:
            ranges_10d.pop(0)
        avg_range = statistics.mean(ranges_10d) if ranges_10d else day_range
        if len(ranges_10d) >= 5 and day_range > 1.5 * avg_range:
            volatility = "HIGH_VOL"
        else:
            volatility = "NORMAL"

        # Character: trend vs choppy
        # Trend: close in top/bottom 25% of range
        # Choppy: close in middle 50% of range
        if day_range > 0:
            close_position = (day_close - day_low) / day_range
            if close_position >= 0.75 or close_position <= 0.25:
                character = "TREND"
            else:
                character = "CHOPPY"
        else:
            character = "CHOPPY"

        day_info[d] = {
            "direction": direction,
            "volatility": volatility,
            "character": character,
            "spy_change_pct": change_pct,
            "spy_range": day_range,
            "avg_range_10d": avg_range,
        }

    return day_info


# ── metrics ──────────────────────────────────────────────────────────

def compute_metrics(trades, trading_days_count=None):
    """Extended metrics for a set of trades."""
    n = len(trades)
    if n == 0:
        return {
            "n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "expectancy": 0.0,
            "stop_rate": 0.0, "daily_stop_freq": 0.0, "max_dd": 0.0,
            "trades_per_week": 0.0, "avg_hold_bars": 0.0,
            "exit_dist": {}, "avg_win_rr": 0.0, "avg_loss_rr": 0.0,
        }

    wins = [t for t in trades if t.pnl_points > 0]
    losses = [t for t in trades if t.pnl_points <= 0]
    pnl = sum(t.pnl_points for t in trades)
    gw = sum(t.pnl_points for t in wins)
    gl = abs(sum(t.pnl_points for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    avg_rr = sum(t.pnl_rr for t in trades) / n

    # Stop rate
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    stop_rate = stopped / n * 100

    # Daily stop freq
    daily_stops = defaultdict(int)
    daily_trades = defaultdict(int)
    for t in trades:
        d = str(t.signal.timestamp)[:10]
        daily_trades[d] += 1
        if t.exit_reason == "stop":
            daily_stops[d] += 1
    td = len(daily_trades) if daily_trades else 1
    daily_stop_freq = sum(daily_stops.values()) / td

    # Trades per week
    if trading_days_count and trading_days_count > 0:
        weeks = trading_days_count / 5.0
        trades_per_week = n / weeks if weeks > 0 else 0
    else:
        weeks = td / 5.0
        trades_per_week = n / weeks if weeks > 0 else 0

    # Avg hold time in bars
    avg_hold = statistics.mean(t.bars_held for t in trades)

    # Max drawdown
    cum = pk = dd = 0.0
    for t in trades:
        cum += t.pnl_points
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum

    # Exit reason distribution
    exit_dist = defaultdict(int)
    for t in trades:
        exit_dist[t.exit_reason] += 1

    # Avg win/loss in R
    avg_win_rr = statistics.mean(t.pnl_rr for t in wins) if wins else 0
    avg_loss_rr = statistics.mean(t.pnl_rr for t in losses) if losses else 0

    return {
        "n": n, "wr": len(wins) / n * 100, "pf": pf, "pnl": pnl,
        "expectancy": avg_rr, "stop_rate": stop_rate,
        "daily_stop_freq": daily_stop_freq, "max_dd": dd,
        "trades_per_week": trades_per_week, "avg_hold_bars": avg_hold,
        "exit_dist": dict(exit_dist),
        "avg_win_rr": avg_win_rr, "avg_loss_rr": avg_loss_rr,
    }


# ── collect trades ──────────────────────────────────────────────────

def collect_trades(bars_dict, cfg, count_after_date=None, setup_filter=None,
                   spy_bars=None, qqq_bars=None, sector_bars_dict=None):
    from .market_context import get_sector_etf
    all_trades = []
    for sym, bars in bars_dict.items():
        sec_bars = None
        if sector_bars_dict and cfg.use_sector_context:
            sector_etf = get_sector_etf(sym)
            if sector_etf and sector_etf in sector_bars_dict:
                sec_bars = sector_bars_dict[sector_etf]

        result = run_backtest(bars, cfg=cfg,
                              spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        for t in result.trades:
            sig_date = t.signal.timestamp
            if count_after_date and hasattr(sig_date, "date"):
                if sig_date.date() < count_after_date:
                    continue
            if setup_filter and t.signal.setup_id not in setup_filter:
                continue
            all_trades.append((sym, t))
    return all_trades


# ── config builders (locked baseline) ────────────────────────────────

def cfg_locked_baseline():
    """VK + SC_VOL — the locked active baseline."""
    c = OverlayConfig()
    c.show_ema_scalp = False
    c.show_failed_bounce = False
    c.show_spencer = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    # sc_require_strong_bo_vol defaults to True
    return c


def cfg_vk_only():
    """VK KISS only — no SC."""
    c = OverlayConfig()
    c.show_second_chance = False
    c.show_ema_scalp = False
    c.show_failed_bounce = False
    c.show_spencer = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    return c


# ── printing helpers ─────────────────────────────────────────────────

def print_metric_block(label, m):
    """Print a compact metrics summary."""
    print(f"    {label}")
    print(f"      Trades: {m['n']}   WR: {m['wr']:.1f}%   PF: {pf_str(m['pf'])}   "
          f"Exp: {m['expectancy']:+.3f}R   PnL: {m['pnl']:+.2f}pts")
    print(f"      MaxDD: {m['max_dd']:.1f}pts   StopRate: {m['stop_rate']:.0f}%   "
          f"DlyStops: {m['daily_stop_freq']:.2f}   Trades/wk: {m['trades_per_week']:.1f}")
    print(f"      AvgHold: {m['avg_hold_bars']:.1f} bars   "
          f"AvgWin: {m['avg_win_rr']:+.2f}R   AvgLoss: {m['avg_loss_rr']:+.2f}R")
    exits = "  ".join(f"{k}:{v}" for k, v in sorted(m['exit_dist'].items()))
    print(f"      Exits: {exits}")


def print_comparison_row(label, m):
    """One-line comparison row."""
    print(f"  {label:<25} │ {m['n']:>4} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
          f"{m['expectancy']:>+6.3f}R {m['pnl']:>+8.2f} "
          f"{m['max_dd']:>5.1f} {m['stop_rate']:>4.0f}% "
          f"{m['daily_stop_freq']:>5.2f} {m['trades_per_week']:>4.1f} "
          f"{m['avg_hold_bars']:>5.1f}")


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Robustness evaluation — locked baseline")
    parser.add_argument("--universe", type=str, default="all94",
                        choices=["orig35", "all94"])
    parser.add_argument("--split-date", type=str, default="2026-02-21")
    args = parser.parse_args()

    split_date = datetime.strptime(args.split_date, "%Y-%m-%d").date()

    # Load symbols
    if args.universe == "all94":
        symbols = sorted(set(
            p.stem.replace("_5min", "")
            for p in DATA_DIR.glob("*_5min.csv")
        ))
    else:
        symbols = load_watchlist(WATCHLIST_FILE)

    # Load SPY/QQQ + sector bars
    spy_bars = qqq_bars = None
    sector_bars_dict = {}

    spy_path = DATA_DIR / "SPY_5min.csv"
    qqq_path = DATA_DIR / "QQQ_5min.csv"
    if spy_path.exists():
        spy_bars = load_bars_from_csv(str(spy_path))
        print(f"SPY bars loaded: {len(spy_bars)}")
    if qqq_path.exists():
        qqq_bars = load_bars_from_csv(str(qqq_path))
        print(f"QQQ bars loaded: {len(qqq_bars)}")

    from .market_context import SECTOR_MAP
    sector_etfs = set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))
    if sector_bars_dict:
        print(f"Sector ETF bars loaded: {len(sector_bars_dict)} ETFs")

    # Classify SPY days
    spy_days = classify_spy_days(spy_bars) if spy_bars else {}
    if spy_days:
        g = sum(1 for v in spy_days.values() if v["direction"] == "GREEN")
        r = sum(1 for v in spy_days.values() if v["direction"] == "RED")
        f = sum(1 for v in spy_days.values() if v["direction"] == "FLAT")
        hv = sum(1 for v in spy_days.values() if v["volatility"] == "HIGH_VOL")
        tr = sum(1 for v in spy_days.values() if v["character"] == "TREND")
        ch = sum(1 for v in spy_days.values() if v["character"] == "CHOPPY")
        print(f"\nSPY day classification ({len(spy_days)} days):")
        print(f"  Direction: {g} GREEN, {r} RED, {f} FLAT")
        print(f"  Volatility: {hv} HIGH_VOL, {len(spy_days)-hv} NORMAL")
        print(f"  Character: {tr} TREND, {ch} CHOPPY")

    # Load bars
    all_full = {}
    all_train = {}
    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue
        dates = set(b.timestamp.date() for b in bars)
        has_train = any(d < split_date for d in dates)
        has_test = any(d >= split_date for d in dates)
        if has_train and has_test:
            all_full[sym] = bars
            all_train[sym] = [b for b in bars if b.timestamp.date() < split_date]

    all_dates = sorted(set(
        b.timestamp.date()
        for bars in all_full.values()
        for b in bars
    ))
    train_days = [d for d in all_dates if d < split_date]
    test_days = [d for d in all_dates if d >= split_date]

    print(f"\nRobustness Evaluation — {len(all_full)} symbols [{args.universe}]")
    print(f"Full period: {min(all_dates)} → {max(all_dates)} ({len(all_dates)} trading days)")
    print(f"Train: {len(train_days)} days   Test: {len(test_days)} days   Split: {args.split_date}")

    ctx_kwargs = dict(spy_bars=spy_bars, qqq_bars=qqq_bars, sector_bars_dict=sector_bars_dict)

    # ══════════════════════════════════════════════════════════════
    #  SECTION 1: HEAD-TO-HEAD — VK only vs VK + SC_VOL
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 110}")
    print("  SECTION 1: HEAD-TO-HEAD COMPARISON — VK only vs VK + SC_VOL")
    print(f"{'=' * 110}")

    configs = [
        ("VK + SC_VOL (baseline)", cfg_locked_baseline(), None),
        ("VK only",                cfg_vk_only(),         {SetupId.VWAP_KISS}),
    ]

    for period_label, date_filter_fn, days_count in [
        ("FULL PERIOD", None, len(all_dates)),
        ("TRAIN ONLY", lambda d: d < split_date, len(train_days)),
        ("TEST ONLY", lambda d: d >= split_date, len(test_days)),
    ]:
        print(f"\n  ── {period_label} ──")
        hdr = (f"  {'Config':<25} │ {'N':>4} {'WR%':>5} {'PF':>5} "
               f"{'Exp':>7} {'PnL':>8} {'MaxDD':>5} {'StpR':>4} "
               f"{'DlyS':>5} {'T/wk':>4} {'Hold':>5}")
        print(hdr)
        print(f"  {'─'*25}─┼{'─'*72}")

        for label, cfg, setup_filt in configs:
            if date_filter_fn:
                if period_label == "TRAIN ONLY":
                    trades_raw = collect_trades(all_train, cfg, setup_filter=setup_filt, **ctx_kwargs)
                else:
                    trades_raw = collect_trades(all_full, cfg, count_after_date=split_date,
                                                setup_filter=setup_filt, **ctx_kwargs)
            else:
                trades_raw = collect_trades(all_full, cfg, setup_filter=setup_filt, **ctx_kwargs)

            trades = [t for _, t in trades_raw]
            m = compute_metrics(trades, days_count)
            print_comparison_row(label, m)

    # ══════════════════════════════════════════════════════════════
    #  SECTION 2: DETAILED METRICS — LOCKED BASELINE
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 110}")
    print("  SECTION 2: DETAILED METRICS — VK + SC_VOL (locked baseline)")
    print(f"{'=' * 110}")

    cfg = cfg_locked_baseline()
    full_trades_raw = collect_trades(all_full, cfg, **ctx_kwargs)
    full_trades = [t for _, t in full_trades_raw]
    train_trades_raw = collect_trades(all_train, cfg, **ctx_kwargs)
    train_trades = [t for _, t in train_trades_raw]
    test_trades_raw = collect_trades(all_full, cfg, count_after_date=split_date, **ctx_kwargs)
    test_trades = [t for _, t in test_trades_raw]

    print("\n  ── Full Period ──")
    m_full = compute_metrics(full_trades, len(all_dates))
    print_metric_block("VK + SC_VOL", m_full)

    print("\n  ── Train ──")
    m_train = compute_metrics(train_trades, len(train_days))
    print_metric_block("VK + SC_VOL", m_train)

    print("\n  ── Test ──")
    m_test = compute_metrics(test_trades, len(test_days))
    print_metric_block("VK + SC_VOL", m_test)

    # Setup-level breakdown (full period)
    print(f"\n  ── Setup Breakdown (full period) ──")
    by_setup = defaultdict(list)
    for sym, t in full_trades_raw:
        name = SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id))
        by_setup[name].append(t)

    hdr = (f"  {'Setup':<18} │ {'N':>4} {'WR%':>5} {'PF':>5} "
           f"{'Exp':>7} {'PnL':>8} {'MaxDD':>5} {'StpR':>4} "
           f"{'DlyS':>5} {'Hold':>5} │ {'Long':>4} {'Short':>5}")
    print(hdr)
    print(f"  {'─'*18}─┼{'─'*62}─┼{'─'*11}")
    for name in sorted(by_setup):
        trades = by_setup[name]
        m = compute_metrics(trades, len(all_dates))
        longs = sum(1 for t in trades if t.signal.direction == 1)
        shorts = len(trades) - longs
        print(f"  {name:<18} │ {m['n']:>4} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
              f"{m['expectancy']:>+6.3f}R {m['pnl']:>+8.2f} "
              f"{m['max_dd']:>5.1f} {m['stop_rate']:>4.0f}% "
              f"{m['daily_stop_freq']:>5.2f} {m['avg_hold_bars']:>5.1f} │ "
              f"{longs:>4} {shorts:>5}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 3: SPY REGIME BREAKDOWN
    # ══════════════════════════════════════════════════════════════
    if spy_days:
        print(f"\n{'=' * 110}")
        print("  SECTION 3: SPY REGIME BREAKDOWN — VK + SC_VOL (full period)")
        print(f"{'=' * 110}")

        # Tag each trade with its SPY day regime
        def trade_date(t):
            return t.signal.timestamp.date() if hasattr(t.signal.timestamp, "date") else None

        # Group by direction
        print(f"\n  ── By SPY Direction ──")
        for regime_val in ["GREEN", "RED", "FLAT"]:
            regime_dates = {d for d, info in spy_days.items() if info["direction"] == regime_val}
            regime_trades = [t for t in full_trades if trade_date(t) in regime_dates]
            n_days = len(regime_dates)
            if regime_trades:
                m = compute_metrics(regime_trades, n_days)
                print(f"    {regime_val:<8} ({n_days:>2}d, {m['n']:>3} trades): "
                      f"WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  "
                      f"Exp={m['expectancy']:+.3f}R  PnL={m['pnl']:+.2f}  "
                      f"DD={m['max_dd']:.1f}  StpR={m['stop_rate']:.0f}%  "
                      f"T/wk={m['trades_per_week']:.1f}")
            else:
                print(f"    {regime_val:<8} ({n_days:>2}d,   0 trades)")

        # Group by volatility
        print(f"\n  ── By Volatility ──")
        for vol_val in ["HIGH_VOL", "NORMAL"]:
            regime_dates = {d for d, info in spy_days.items() if info["volatility"] == vol_val}
            regime_trades = [t for t in full_trades if trade_date(t) in regime_dates]
            n_days = len(regime_dates)
            if regime_trades:
                m = compute_metrics(regime_trades, n_days)
                print(f"    {vol_val:<8} ({n_days:>2}d, {m['n']:>3} trades): "
                      f"WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  "
                      f"Exp={m['expectancy']:+.3f}R  PnL={m['pnl']:+.2f}  "
                      f"DD={m['max_dd']:.1f}  StpR={m['stop_rate']:.0f}%  "
                      f"T/wk={m['trades_per_week']:.1f}")
            else:
                print(f"    {vol_val:<8} ({n_days:>2}d,   0 trades)")

        # Group by character
        print(f"\n  ── By Day Character ──")
        for char_val in ["TREND", "CHOPPY"]:
            regime_dates = {d for d, info in spy_days.items() if info["character"] == char_val}
            regime_trades = [t for t in full_trades if trade_date(t) in regime_dates]
            n_days = len(regime_dates)
            if regime_trades:
                m = compute_metrics(regime_trades, n_days)
                print(f"    {char_val:<8} ({n_days:>2}d, {m['n']:>3} trades): "
                      f"WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  "
                      f"Exp={m['expectancy']:+.3f}R  PnL={m['pnl']:+.2f}  "
                      f"DD={m['max_dd']:.1f}  StpR={m['stop_rate']:.0f}%  "
                      f"T/wk={m['trades_per_week']:.1f}")
            else:
                print(f"    {char_val:<8} ({n_days:>2}d,   0 trades)")

        # Composite regimes (e.g. green+trend, red+choppy)
        print(f"\n  ── Composite Regimes ──")
        composites = [
            ("GREEN + TREND",   lambda i: i["direction"] == "GREEN" and i["character"] == "TREND"),
            ("GREEN + CHOPPY",  lambda i: i["direction"] == "GREEN" and i["character"] == "CHOPPY"),
            ("RED + TREND",     lambda i: i["direction"] == "RED" and i["character"] == "TREND"),
            ("RED + CHOPPY",    lambda i: i["direction"] == "RED" and i["character"] == "CHOPPY"),
            ("HIGH_VOL + RED",  lambda i: i["volatility"] == "HIGH_VOL" and i["direction"] == "RED"),
            ("HIGH_VOL + GREEN",lambda i: i["volatility"] == "HIGH_VOL" and i["direction"] == "GREEN"),
        ]
        for comp_label, comp_fn in composites:
            regime_dates = {d for d, info in spy_days.items() if comp_fn(info)}
            regime_trades = [t for t in full_trades if trade_date(t) in regime_dates]
            n_days = len(regime_dates)
            if regime_trades and n_days > 0:
                m = compute_metrics(regime_trades, n_days)
                print(f"    {comp_label:<18} ({n_days:>2}d, {m['n']:>3} trades): "
                      f"WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  "
                      f"Exp={m['expectancy']:+.3f}R  PnL={m['pnl']:+.2f}  "
                      f"StpR={m['stop_rate']:.0f}%")
            else:
                print(f"    {comp_label:<18} ({n_days:>2}d,   0 trades)")

        # ── SC_VOL incremental value by regime ──
        print(f"\n  ── SC_VOL Incremental Value by Regime ──")
        print(f"    (VK+SC_VOL minus VK-only, per regime)")

        cfg_vk = cfg_vk_only()
        vk_trades_raw = collect_trades(all_full, cfg_vk, setup_filter={SetupId.VWAP_KISS}, **ctx_kwargs)
        vk_trades = [t for _, t in vk_trades_raw]

        for regime_label, regime_filter in [
            ("GREEN",  lambda i: i["direction"] == "GREEN"),
            ("RED",    lambda i: i["direction"] == "RED"),
            ("TREND",  lambda i: i["character"] == "TREND"),
            ("CHOPPY", lambda i: i["character"] == "CHOPPY"),
        ]:
            regime_dates = {d for d, info in spy_days.items() if regime_filter(info)}
            bl_regime = [t for t in full_trades if trade_date(t) in regime_dates]
            vk_regime = [t for t in vk_trades if trade_date(t) in regime_dates]

            if bl_regime or vk_regime:
                m_bl = compute_metrics(bl_regime)
                m_vk = compute_metrics(vk_regime)
                delta_n = m_bl["n"] - m_vk["n"]
                delta_pnl = m_bl["pnl"] - m_vk["pnl"]
                delta_exp = m_bl["expectancy"] - m_vk["expectancy"]
                delta_dd = m_bl["max_dd"] - m_vk["max_dd"]
                print(f"    {regime_label:<8}: ΔTrades={delta_n:+d}  ΔPnL={delta_pnl:+.2f}  "
                      f"ΔExp={delta_exp:+.3f}R  ΔDD={delta_dd:+.1f}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 4: SLIPPAGE SENSITIVITY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 110}")
    print("  SECTION 4: SLIPPAGE SENSITIVITY — VK + SC_VOL")
    print(f"{'=' * 110}")

    slip_scenarios = [
        ("Zero slip",       0.0,    1.0),
        ("Light (2bps)",    0.0002, 1.5),
        ("Default (4bps)",  0.0004, 2.0),
        ("Heavy (8bps)",    0.0008, 2.5),
        ("Extreme (12bps)", 0.0012, 3.0),
    ]

    hdr = (f"  {'Scenario':<20} │ {'N':>4} {'WR%':>5} {'PF':>5} "
           f"{'Exp':>7} {'PnL':>8} {'MaxDD':>5}")
    print(f"\n{hdr}")
    print(f"  {'─'*20}─┼{'─'*40}")

    for slip_label, slip_bps, slip_cap in slip_scenarios:
        cfg_s = cfg_locked_baseline()
        cfg_s.slip_bps = slip_bps
        cfg_s.slip_vol_mult_cap = slip_cap
        if slip_bps == 0:
            cfg_s.use_dynamic_slippage = False
        trades_raw = collect_trades(all_full, cfg_s, **ctx_kwargs)
        trades = [t for _, t in trades_raw]
        m = compute_metrics(trades, len(all_dates))
        print(f"  {slip_label:<20} │ {m['n']:>4} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
              f"{m['expectancy']:>+6.3f}R {m['pnl']:>+8.2f} {m['max_dd']:>5.1f}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 5: DAILY P&L CURVE
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 110}")
    print("  SECTION 5: DAILY P&L — VK + SC_VOL (full period)")
    print(f"{'=' * 110}")

    daily_pnl = defaultdict(float)
    daily_cnt = defaultdict(int)
    for t in full_trades:
        d = str(t.signal.timestamp)[:10]
        daily_pnl[d] += t.pnl_points
        daily_cnt[d] += 1

    cum = 0.0
    print(f"\n  {'Date':<12} {'Trades':>6} {'DayPnL':>8} {'CumPnL':>8} │ Equity")
    print(f"  {'─'*12}─{'─'*6}─{'─'*8}─{'─'*8}─┼{'─'*40}")
    for d in sorted(daily_pnl):
        day_p = daily_pnl[d]
        cum += day_p
        bar_len = int(cum / 2) if cum >= 0 else 0
        bar_neg = int(abs(cum) / 2) if cum < 0 else 0
        bar = "█" * min(bar_len, 30) if cum >= 0 else "░" * min(bar_neg, 30)
        regime_tag = ""
        try:
            d_obj = datetime.strptime(d, "%Y-%m-%d").date()
            if d_obj in spy_days:
                info = spy_days[d_obj]
                regime_tag = f" [{info['direction'][:1]}{info['character'][:1]}]"
        except:
            pass
        print(f"  {d:<12} {daily_cnt[d]:>6} {day_p:>+8.2f} {cum:>+8.2f} │ {bar}{regime_tag}")

    # Win/loss day stats
    win_days = sum(1 for p in daily_pnl.values() if p > 0)
    loss_days = sum(1 for p in daily_pnl.values() if p <= 0)
    total_days_traded = len(daily_pnl)
    print(f"\n  Win days: {win_days}/{total_days_traded} ({win_days/total_days_traded*100:.0f}%)" if total_days_traded else "")
    if daily_pnl:
        best_day = max(daily_pnl.values())
        worst_day = min(daily_pnl.values())
        print(f"  Best day: {best_day:+.2f}pts   Worst day: {worst_day:+.2f}pts")

    # ══════════════════════════════════════════════════════════════
    #  SUMMARY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 110}")
    print("  ROBUSTNESS SUMMARY")
    print(f"{'=' * 110}")
    print(f"  Locked baseline: VWAP_KISS + SECOND_CHANCE (sc_require_strong_bo_vol=True)")
    print(f"  Universe: {args.universe} ({len(all_full)} symbols)")
    print(f"  History: {min(all_dates)} → {max(all_dates)} ({len(all_dates)} trading days)")
    print(f"  Slippage: dynamic (bps={cfg.slip_bps}, cap={cfg.slip_vol_mult_cap}x)")
    cfg_ref = cfg_locked_baseline()
    print(f"  SC gate: strong_bo_vol={cfg_ref.sc_require_strong_bo_vol}, "
          f"mult={cfg_ref.sc_strong_bo_vol_mult}x")
    print(f"  Disabled: EMA_RECLAIM, EMA_CONFIRM, FPIP, SC_V2, Spencer, Failed Bounce")
    print(f"{'=' * 110}\n")


if __name__ == "__main__":
    main()
