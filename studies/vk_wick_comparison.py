"""
VK Opposing Wick Filter Comparison — one refinement validation.

Compares:
  A: Current locked baseline (Non-RED + Q>=2 + <15:30)
  B: Locked baseline + VK opposing wick <= 20%

Reports: trade count, WR, PF, expectancy, PnL, max DD, stop rate,
         train/test split, setup-level contribution.

Usage:
    python -m alert_overlay.vk_wick_comparison --universe all94
"""

import argparse
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import List, Dict
from zoneinfo import ZoneInfo

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import NaN, SetupId

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent.parent / "data"
WATCHLIST_FILE = Path(__file__).parent.parent / "watchlist.txt"


def load_watchlist(path=WATCHLIST_FILE):
    symbols = []
    with open(path) as f:
        for line in f:
            sym = line.strip().upper()
            if sym and not sym.startswith("#"):
                symbols.append(sym)
    return symbols


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
        day_info[d] = {"direction": direction, "character": character}
    return day_info


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "∞"


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
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    cum = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: t.signal.timestamp):
        cum += t.pnl_points
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum
    avg_hold = statistics.mean(t.bars_held for t in trades)
    td = len(set(t.signal.timestamp.date() for t in trades))
    weeks = (n_days or td) / 5.0
    return {"n": n, "wr": len(wins) / n * 100, "pf": pf, "pnl": pnl,
            "exp": sum(t.pnl_rr for t in trades) / n,
            "stop_rate": stopped / n * 100, "max_dd": dd,
            "avg_hold": avg_hold, "tpw": n / weeks if weeks > 0 else 0}


def apply_locked_filters(trades, spy_day_info):
    filtered = []
    for t in trades:
        d = t.signal.timestamp.date()
        info = spy_day_info.get(d, {})
        if info.get("direction") == "RED":
            continue
        if t.signal.quality_score < 2:
            continue
        hhmm = t.signal.timestamp.hour * 100 + t.signal.timestamp.minute
        if hhmm >= 1530:
            continue
        filtered.append(t)
    return filtered


def print_row(label, m):
    print(f"  {label:<40s} │ {m['n']:>4} {m['tpw']:>5.1f} {m['wr']:>5.1f}% {pf_str(m['pf']):>6s} "
          f"{m['exp']:>+6.2f}R {m['pnl']:>+8.2f} {m['max_dd']:>6.1f} "
          f"{m['stop_rate']:>5.0f}% {m['avg_hold']:>5.1f}")


def print_header():
    print(f"\n  {'Variant':<40s} │ {'N':>4} {'T/Wk':>5} {'WR%':>5} {'PF':>6} "
          f"{'Exp':>7} {'PnL':>8} {'MaxDD':>6} {'StpR':>5} {'AvgH':>5}")
    print(f"  {'─' * 40}─┼{'─' * 58}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="all94")
    args = parser.parse_args()

    if args.universe == "all94":
        symbols = load_watchlist()
    else:
        symbols = [s.strip().upper() for s in args.universe.split(",")]

    from .market_context import SECTOR_MAP, get_sector_etf

    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    spy_day_info = classify_spy_days(spy_bars)

    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    all_dates = sorted(spy_day_info.keys())
    n_days = len(all_dates)

    # ── Config A: current locked baseline (no wick filter) ──
    cfg_a = OverlayConfig(
        show_reversal_setups=False,
        show_trend_setups=True,
        show_ema_retest=False,
        show_ema_mean_rev=False,
        show_ema_pullback=False,
        show_second_chance=True,
        show_spencer=False,
        show_failed_bounce=False,
        show_ema_scalp=False,
        show_ema_fpip=False,
        show_sc_v2=False,
        vk_long_only=True,
        sc_long_only=True,
        use_market_context=True,
        use_sector_context=True,
        vk_max_opposing_wick_pct=1.0,  # disabled — allow any wick
    )

    # ── Config B: locked baseline + VK wick filter ──
    cfg_b = OverlayConfig(
        show_reversal_setups=False,
        show_trend_setups=True,
        show_ema_retest=False,
        show_ema_mean_rev=False,
        show_ema_pullback=False,
        show_second_chance=True,
        show_spencer=False,
        show_failed_bounce=False,
        show_ema_scalp=False,
        show_ema_fpip=False,
        show_sc_v2=False,
        vk_long_only=True,
        sc_long_only=True,
        use_market_context=True,
        use_sector_context=True,
        vk_max_opposing_wick_pct=0.20,  # wick filter active
    )

    # ── Run both backtests ──
    trades_a: List[Trade] = []
    trades_b: List[Trade] = []
    sym_a: Dict[int, str] = {}
    sym_b: Dict[int, str] = {}

    for sym in symbols:
        fpath = DATA_DIR / f"{sym}_5min.csv"
        if not fpath.exists():
            continue
        bars = load_bars_from_csv(str(fpath))
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None

        ra = run_backtest(bars, cfg=cfg_a, spy_bars=spy_bars, qqq_bars=qqq_bars, sector_bars=sec_bars)
        rb = run_backtest(bars, cfg=cfg_b, spy_bars=spy_bars, qqq_bars=qqq_bars, sector_bars=sec_bars)

        for t in ra.trades:
            sym_a[id(t)] = sym
        for t in rb.trades:
            sym_b[id(t)] = sym

        trades_a.extend(ra.trades)
        trades_b.extend(rb.trades)

    # Apply locked filters
    filt_a = apply_locked_filters(trades_a, spy_day_info)
    filt_b = apply_locked_filters(trades_b, spy_day_info)

    print(f"{'=' * 105}")
    print(f"  VK OPPOSING WICK FILTER COMPARISON")
    print(f"{'=' * 105}")
    print(f"  Period: {min(all_dates)} → {max(all_dates)} ({n_days} days)")
    print(f"  Universe: {len(symbols)} symbols")
    print(f"  Filters: Non-RED + Q>=2 + <15:30")
    print(f"  Change: VK opposing wick <= 20% of bar range")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 1: HEAD-TO-HEAD
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 105}")
    print(f"  SECTION 1: HEAD-TO-HEAD")
    print(f"{'=' * 105}")

    print_header()
    print_row("A: Current locked baseline", compute_metrics(filt_a, n_days))
    print_row("B: + VK wick <= 20%", compute_metrics(filt_b, n_days))

    # Delta trades
    # Find trades in A that are NOT in B (dropped by wick filter)
    # Match by timestamp + setup_id since different Trade objects
    def trade_key(t, sym_map):
        return (sym_map.get(id(t), ""), t.signal.timestamp, t.signal.setup_id, t.signal.direction)

    keys_a = {trade_key(t, sym_a): t for t in filt_a}
    keys_b = {trade_key(t, sym_b): t for t in filt_b}

    dropped = [keys_a[k] for k in keys_a if k not in keys_b]
    kept = [keys_a[k] for k in keys_a if k in keys_b]

    print(f"\n  Trades dropped by wick filter: {len(dropped)}")
    if dropped:
        m_drop = compute_metrics(dropped)
        print(f"    → WR={m_drop['wr']:.1f}%  PF={pf_str(m_drop['pf'])}  PnL={m_drop['pnl']:+.2f}")
        print(f"\n    {'Date':>10s} {'Time':>5s} {'Sym':>6s} {'Setup':>14s} {'PnL':>7s} {'Exit':>5s}")
        print(f"    {'─'*10} {'─'*5} {'─'*6} {'─'*14} {'─'*7} {'─'*5}")
        for t in sorted(dropped, key=lambda t: t.signal.timestamp):
            sym = sym_a.get(id(t), "?")
            print(f"    {str(t.signal.timestamp.date()):>10s} {t.signal.timestamp.strftime('%H:%M'):>5s} "
                  f"{sym:>6s} {t.signal.setup_name:>14s} {t.pnl_points:>+7.2f} {t.exit_reason:>5s}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 2: SETUP-LEVEL CONTRIBUTION
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 105}")
    print(f"  SECTION 2: SETUP-LEVEL CONTRIBUTION")
    print(f"{'=' * 105}")

    for label, trades, nd in [("A: Current locked", filt_a, n_days),
                                ("B: + VK wick filter", filt_b, n_days)]:
        vk = [t for t in trades if t.signal.setup_id == SetupId.VWAP_KISS]
        sc = [t for t in trades if t.signal.setup_id == SetupId.SECOND_CHANCE]

        print(f"\n  {label}:")
        print_header()
        print_row(f"  VK only", compute_metrics(vk, nd))
        print_row(f"  SC only", compute_metrics(sc, nd))
        print_row(f"  Combined", compute_metrics(trades, nd))

    # ══════════════════════════════════════════════════════════════
    #  SECTION 3: TRAIN / TEST SPLIT
    # ══════════════════════════════════════════════════════════════
    split_date = date(2026, 2, 21)
    train_dates = [d for d in all_dates if d < split_date]
    test_dates = [d for d in all_dates if d >= split_date]

    print(f"\n{'=' * 105}")
    print(f"  SECTION 3: WALK-FORWARD (train < {split_date}, test ≥ {split_date})")
    print(f"{'=' * 105}")

    for label, trades in [("A: Current locked", filt_a),
                           ("B: + VK wick filter", filt_b)]:
        train = [t for t in trades if t.signal.timestamp.date() < split_date]
        test = [t for t in trades if t.signal.timestamp.date() >= split_date]

        mt = compute_metrics(train, len(train_dates))
        ms = compute_metrics(test, len(test_dates))

        print(f"\n  {label}:")
        print(f"    {'Period':<12} │ {'N':>4} {'T/Wk':>5} {'WR%':>5} {'PF':>6} "
              f"{'Exp':>7} {'PnL':>8} {'MaxDD':>6} {'StpR':>5}")
        print(f"    {'─' * 12}─┼{'─' * 52}")
        for plabel, pm in [("Train", mt), ("Test", ms)]:
            if pm['n'] == 0:
                print(f"    {plabel:<12} │    0   — no trades")
            else:
                print(f"    {plabel:<12} │ {pm['n']:>4} {pm['tpw']:>5.1f} {pm['wr']:>5.1f}% {pf_str(pm['pf']):>6s} "
                      f"{pm['exp']:>+6.2f}R {pm['pnl']:>+8.2f} {pm['max_dd']:>6.1f} "
                      f"{pm['stop_rate']:>5.0f}%")

        # Per-setup in train/test
        for period_label, period_trades in [("Train", train), ("Test", test)]:
            if not period_trades:
                continue
            vk_p = [t for t in period_trades if t.signal.setup_id == SetupId.VWAP_KISS]
            sc_p = [t for t in period_trades if t.signal.setup_id == SetupId.SECOND_CHANCE]
            if vk_p or sc_p:
                print(f"    {period_label} breakdown:")
                if vk_p:
                    mv = compute_metrics(vk_p)
                    print(f"      VK:  N={mv['n']:>3}  WR={mv['wr']:>5.1f}%  PF={pf_str(mv['pf']):>6s}  PnL={mv['pnl']:>+7.2f}")
                if sc_p:
                    ms2 = compute_metrics(sc_p)
                    print(f"      SC:  N={ms2['n']:>3}  WR={ms2['wr']:>5.1f}%  PF={pf_str(ms2['pf']):>6s}  PnL={ms2['pnl']:>+7.2f}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 4: REGIME BREAKDOWN
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 105}")
    print(f"  SECTION 4: REGIME BREAKDOWN")
    print(f"{'=' * 105}")

    for label, trades in [("A: Current locked", filt_a),
                           ("B: + VK wick filter", filt_b)]:
        print(f"\n  {label}:")
        regime_groups = defaultdict(list)
        for t in trades:
            d = t.signal.timestamp.date()
            info = spy_day_info.get(d, {})
            key = f"{info.get('direction', 'UNK')}+{info.get('character', 'UNK')}"
            regime_groups[key].append(t)

        print(f"    {'Regime':<18} │ {'N':>4} {'WR%':>5} {'PF':>6} {'PnL':>8}")
        print(f"    {'─' * 18}─┼{'─' * 28}")
        for key in ["GREEN+TREND", "GREEN+CHOPPY", "FLAT+TREND", "FLAT+CHOPPY"]:
            rt = regime_groups.get(key, [])
            if not rt:
                print(f"    {key:<18} │    0   —")
                continue
            rm = compute_metrics(rt)
            print(f"    {key:<18} │ {rm['n']:>4} {rm['wr']:>5.1f}% {pf_str(rm['pf']):>6s} {rm['pnl']:>+8.2f}")

    # ══════════════════════════════════════════════════════════════
    #  SUMMARY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 105}")
    print(f"  SUMMARY")
    print(f"{'=' * 105}")

    ma = compute_metrics(filt_a, n_days)
    mb = compute_metrics(filt_b, n_days)

    print(f"\n  A (current):    N={ma['n']:3d}  WR={ma['wr']:.1f}%  PF={pf_str(ma['pf']):>6s}  "
          f"PnL={ma['pnl']:+8.2f}  Exp={ma['exp']:+.2f}R  MaxDD={ma['max_dd']:.1f}  StpR={ma['stop_rate']:.0f}%")
    print(f"  B (+ wick):     N={mb['n']:3d}  WR={mb['wr']:.1f}%  PF={pf_str(mb['pf']):>6s}  "
          f"PnL={mb['pnl']:+8.2f}  Exp={mb['exp']:+.2f}R  MaxDD={mb['max_dd']:.1f}  StpR={mb['stop_rate']:.0f}%")

    delta_n = mb['n'] - ma['n']
    delta_pnl = mb['pnl'] - ma['pnl']
    delta_wr = mb['wr'] - ma['wr']
    print(f"\n  Delta: {delta_n:+d} trades  {delta_wr:+.1f}pp WR  {delta_pnl:+.2f} pts PnL")

    # Assessment
    improved = mb['pf'] > ma['pf'] and mb['pnl'] >= ma['pnl'] * 0.85
    print(f"\n  Assessment: {'IMPROVEMENT — wick filter tightens quality' if improved else 'NEUTRAL/REGRESSION — wick filter may not generalize'}")

    print(f"\n{'=' * 105}")


if __name__ == "__main__":
    main()
