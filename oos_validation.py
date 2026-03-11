"""
Out-of-sample validation — v3.

Tests the decision-relevant system bundles:
  System 1: VWAP_KISS + SECOND_CHANCE (no Spencer, no EMA)
  System 2: System 1 + EMA_RECLAIM
  System 3: System 2 + EMA_CONFIRM
  System 4: EMA_RECLAIM isolated

Spencer is tested separately (not in core bundle).

Metrics reported: trades, WR%, PF, PnL, expectancy, maxDD, stop rate, daily stop freq.

Three sections:
  1. Train/Test split comparison across config variants
  2. Rolling walk-forward (train 15d → test 5d → slide)
  3. Per-symbol robustness

Usage:
    python -m alert_overlay.oos_validation
    python -m alert_overlay.oos_validation --split-date 2026-02-21 --universe all94
"""

import argparse
import statistics
import sys
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
WATCHLIST_EXPANDED = Path(__file__).parent / "watchlist_expanded.txt"


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


def stats(trades):
    """Return dict with n, wr, pf, pnl, avg_rr, expectancy, stop_rate, daily_stop_freq, max_dd."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "avg_rr": 0.0,
                "expectancy": 0.0, "stop_rate": 0.0, "daily_stop_freq": 0.0, "max_dd": 0.0}
    wins = [t for t in trades if t.pnl_points > 0]
    losses = [t for t in trades if t.pnl_points <= 0]
    pnl = sum(t.pnl_points for t in trades)
    gw = sum(t.pnl_points for t in wins)
    gl = abs(sum(t.pnl_points for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    avg_rr = sum(t.pnl_rr for t in trades) / n

    # Expectancy: avg PnL per trade in R-multiples
    expectancy = avg_rr

    # Stop rate: % of trades that exited via stop
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    stop_rate = stopped / n * 100

    # Daily stop frequency: avg stops per trading day
    daily_stops = defaultdict(int)
    daily_trades = defaultdict(int)
    for t in trades:
        d = str(t.signal.timestamp)[:10]
        daily_trades[d] += 1
        if t.exit_reason == "stop":
            daily_stops[d] += 1
    trading_days = len(daily_trades) if daily_trades else 1
    daily_stop_freq = sum(daily_stops.values()) / trading_days

    # Max drawdown from cumulative equity curve
    cum = pk = dd = 0.0
    for t in trades:
        cum += t.pnl_points
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum

    return {"n": n, "wr": len(wins) / n * 100, "pf": pf, "pnl": pnl, "avg_rr": avg_rr,
            "expectancy": expectancy, "stop_rate": stop_rate,
            "daily_stop_freq": daily_stop_freq, "max_dd": dd}


def collect_trades(bars_dict, cfg, count_after_date=None, setup_filter=None,
                   spy_bars=None, qqq_bars=None, sector_bars_dict=None):
    """
    Run backtest across multiple symbols using run_backtest (real exit logic).
    """
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


def setup_breakdown(trades_with_sym):
    """Return {setup_name: [trades]}."""
    groups = defaultdict(list)
    for sym, t in trades_with_sym:
        name = SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id))
        groups[name].append(t)
    return dict(groups)


def max_drawdown_pts(trades_with_sym):
    """Simple max drawdown in points, from daily PnL."""
    daily = defaultdict(float)
    for sym, t in trades_with_sym:
        d = str(t.signal.timestamp)[:10]
        daily[d] += t.pnl_points
    eq = pk = dd = 0.0
    for d in sorted(daily):
        eq += daily[d]
        if eq > pk:
            pk = eq
        if pk - eq > dd:
            dd = pk - eq
    return dd


# ── config builders ──────────────────────────────────────────────────

def cfg_core_only():
    """VK + SC only (no Spencer, no EMA scalp, no FB, no FPIP, no SC_V2)."""
    c = OverlayConfig()
    c.show_ema_scalp = False
    c.show_failed_bounce = False
    c.show_spencer = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    return c


def cfg_core_sc_vol():
    """VK + SC with strong BO volume gate."""
    c = OverlayConfig()
    c.show_ema_scalp = False
    c.show_failed_bounce = False
    c.show_spencer = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    c.sc_require_strong_bo_vol = True
    return c


def cfg_core_sc_vol_shallow():
    """VK + SC with strong BO volume + shallow reset gates."""
    c = OverlayConfig()
    c.show_ema_scalp = False
    c.show_failed_bounce = False
    c.show_spencer = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    c.sc_require_strong_bo_vol = True
    c.sc_require_shallow_reset = True
    return c


def cfg_sc_only():
    """Current SECOND_CHANCE in isolation."""
    c = OverlayConfig()
    c.show_reversal_setups = False
    c.show_trend_setups = False
    c.show_ema_retest = False
    c.show_ema_mean_rev = False
    c.show_ema_pullback = False
    c.show_second_chance = True
    c.show_spencer = False
    c.show_failed_bounce = False
    c.show_ema_scalp = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    return c


def cfg_sc_vol_only():
    """SC with strong BO volume gate in isolation."""
    c = cfg_sc_only()
    c.sc_require_strong_bo_vol = True
    return c


def cfg_sc_vol_shallow_only():
    """SC with strong BO volume + shallow reset in isolation."""
    c = cfg_sc_only()
    c.sc_require_strong_bo_vol = True
    c.sc_require_shallow_reset = True
    return c


def cfg_core_plus_ema_reclaim():
    """VK + SC + EMA_RECLAIM only."""
    c = OverlayConfig()
    c.show_failed_bounce = False
    c.show_spencer = False
    c.show_ema_confirm = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    return c


def cfg_core_plus_all_ema():
    """VK + SC + EMA_RECLAIM + EMA_CONFIRM."""
    c = OverlayConfig()
    c.show_failed_bounce = False
    c.show_spencer = False
    c.show_ema_confirm = True
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    return c


def cfg_ema_reclaim_only():
    """EMA_RECLAIM in isolation."""
    c = OverlayConfig()
    c.show_reversal_setups = False
    c.show_trend_setups = False
    c.show_ema_retest = False
    c.show_ema_mean_rev = False
    c.show_ema_pullback = False
    c.show_second_chance = False
    c.show_spencer = False
    c.show_failed_bounce = False
    c.show_ema_scalp = True
    c.show_ema_confirm = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    return c


def cfg_spencer_only():
    """Spencer in isolation."""
    c = OverlayConfig()
    c.show_reversal_setups = False
    c.show_trend_setups = False
    c.show_ema_retest = False
    c.show_ema_mean_rev = False
    c.show_ema_pullback = False
    c.show_second_chance = False
    c.show_spencer = True
    c.show_failed_bounce = False
    c.show_ema_scalp = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    return c


def cfg_core_plus_fpip():
    """VK + SC + EMA_FPIP."""
    c = OverlayConfig()
    c.show_failed_bounce = False
    c.show_spencer = False
    c.show_ema_scalp = False
    c.show_ema_confirm = False
    c.show_ema_fpip = True
    c.show_sc_v2 = False
    return c


def cfg_fpip_only():
    """EMA_FPIP in isolation."""
    c = OverlayConfig()
    c.show_reversal_setups = False
    c.show_trend_setups = False
    c.show_ema_retest = False
    c.show_ema_mean_rev = False
    c.show_ema_pullback = False
    c.show_second_chance = False
    c.show_spencer = False
    c.show_failed_bounce = False
    c.show_ema_scalp = False
    c.show_ema_confirm = False
    c.show_ema_fpip = True
    c.show_sc_v2 = False
    return c


def cfg_vk_plus_sc_v2():
    """VK + SC_V2 (replacing current SC)."""
    c = OverlayConfig()
    c.show_second_chance = False   # disable old SC
    c.show_ema_scalp = False
    c.show_failed_bounce = False
    c.show_spencer = False
    c.show_ema_fpip = False
    c.show_sc_v2 = True
    return c


def cfg_vk_only():
    """VK KISS only (no SC, no SC_V2, no EMA, no Spencer)."""
    c = OverlayConfig()
    c.show_second_chance = False
    c.show_ema_scalp = False
    c.show_failed_bounce = False
    c.show_spencer = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    return c


def cfg_sc_v2_only():
    """SC_V2 in isolation."""
    c = OverlayConfig()
    c.show_reversal_setups = False
    c.show_trend_setups = False
    c.show_ema_retest = False
    c.show_ema_mean_rev = False
    c.show_ema_pullback = False
    c.show_second_chance = False
    c.show_spencer = False
    c.show_failed_bounce = False
    c.show_ema_scalp = False
    c.show_ema_confirm = False
    c.show_ema_fpip = False
    c.show_sc_v2 = True
    return c


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Out-of-sample validation v3")
    parser.add_argument("--split-date", type=str, default="2026-02-21",
                        help="Train/test split date YYYY-MM-DD")
    parser.add_argument("--universe", type=str, default="orig35",
                        choices=["orig35", "all94"],
                        help="Symbol universe to validate")
    parser.add_argument("--session-end", type=int, default=1555)
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

    # Load SPY/QQQ bars for market context
    spy_bars = None
    qqq_bars = None
    sector_bars_dict = {}

    spy_path = DATA_DIR / "SPY_5min.csv"
    qqq_path = DATA_DIR / "QQQ_5min.csv"
    if spy_path.exists():
        spy_bars = load_bars_from_csv(str(spy_path))
        print(f"SPY bars loaded: {len(spy_bars)}")
    if qqq_path.exists():
        qqq_bars = load_bars_from_csv(str(qqq_path))
        print(f"QQQ bars loaded: {len(qqq_bars)}")

    # Load sector ETF bars
    from .market_context import SECTOR_MAP
    sector_etfs = set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))
    if sector_bars_dict:
        print(f"Sector ETF bars loaded: {len(sector_bars_dict)} ETFs")

    # Load bars and split
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

    train_days = sorted(set(
        b.timestamp.date()
        for bars in all_train.values()
        for b in bars
    ))
    test_days = sorted(set(
        b.timestamp.date()
        for bars in all_full.values()
        for b in bars
        if b.timestamp.date() >= split_date
    ))

    print(f"\nOut-of-Sample Validation v3 — {len(all_full)} symbols [{args.universe}]")
    print(f"Split date: {args.split_date}")
    print(f"Train: {len(train_days)} days ({min(train_days)} → {max(train_days)})")
    print(f"Test:  {len(test_days)} days ({min(test_days)} → {max(test_days)})")

    ctx_kwargs = dict(spy_bars=spy_bars, qqq_bars=qqq_bars, sector_bars_dict=sector_bars_dict)

    # ==================================================================
    #  TEST 1: TRAIN vs TEST — CONFIGURATION COMPARISON
    # ==================================================================
    print(f"\n{'=' * 120}")
    print("  TEST 1: TRAIN vs TEST COMPARISON (Spencer excluded from core)")
    print(f"{'=' * 120}")

    # Note: sc_require_strong_bo_vol is now True by default.
    # cfg_core_only() = VK + SC with strong BO vol (the new baseline).
    # For old SC comparison, explicitly disable it.
    def _cfg_old_sc():
        c = cfg_core_only()
        c.sc_require_strong_bo_vol = False
        return c

    def _cfg_old_sc_isolated():
        c = cfg_sc_only()
        c.sc_require_strong_bo_vol = False
        return c

    configs = [
        ("1. VK + SC_VOL (new dflt)",cfg_core_only(),             None),
        ("2. VK + old SC",           _cfg_old_sc(),               None),
        ("3. VK only",               cfg_vk_only(),               {SetupId.VWAP_KISS}),
        ("4. SC_VOL isolated",       cfg_sc_only(),               {SetupId.SECOND_CHANCE}),
        ("5. Old SC isolated",       _cfg_old_sc_isolated(),      {SetupId.SECOND_CHANCE}),
        ("6. Core + EMA_RECLAIM",    cfg_core_plus_ema_reclaim(), None),
    ]

    header = (
        f"  {'Config':<27} │ {'──── TRAIN ────':^40} │ {'──── TEST ─────':^40} │ {'Δ':>6}"
    )
    sub = (
        f"  {'':27} │ {'Trds':>5} {'WR%':>6} {'PF':>5} {'PnL':>8} {'Exp':>6} {'DD':>6} {'StpR':>5} │ "
        f"{'Trds':>5} {'WR%':>6} {'PF':>5} {'PnL':>8} {'Exp':>6} {'DD':>6} {'StpR':>5} │"
    )
    print(f"\n{header}")
    print(sub)
    print(f"  {'─' * 27}─┼{'─' * 40}─┼{'─' * 40}─┼{'─' * 6}")

    for label, cfg, setup_filt in configs:
        train_t = collect_trades(all_train, cfg, setup_filter=setup_filt, **ctx_kwargs)
        test_t = collect_trades(all_full, cfg, count_after_date=split_date,
                                setup_filter=setup_filt, **ctx_kwargs)

        trn = stats([t for _, t in train_t])
        tst = stats([t for _, t in test_t])

        both_prof = trn["pnl"] > 0 and tst["pnl"] > 0
        pf_close = (abs(trn["pf"] - tst["pf"]) < 0.5
                     if trn["pf"] < 999 and tst["pf"] < 999 else False)
        stable = ("YES" if both_prof and pf_close
                  else ("MIXED" if trn["pnl"] > 0 or tst["pnl"] > 0 else "NO"))

        print(f"  {label:<27} │ "
              f"{trn['n']:>5} {trn['wr']:>5.1f}% {pf_str(trn['pf']):>5} {trn['pnl']:>+8.2f} "
              f"{trn['expectancy']:>+5.3f} {trn['max_dd']:>6.1f} {trn['stop_rate']:>4.0f}% │ "
              f"{tst['n']:>5} {tst['wr']:>5.1f}% {pf_str(tst['pf']):>5} {tst['pnl']:>+8.2f} "
              f"{tst['expectancy']:>+5.3f} {tst['max_dd']:>6.1f} {tst['stop_rate']:>4.0f}% │ "
              f"{stable:>6}")

    # ==================================================================
    #  TEST 1b: SETUP-LEVEL BREAKDOWN (full period, Core + EMA_RECLAIM)
    # ==================================================================
    print(f"\n{'=' * 120}")
    print("  TEST 1b: SETUP-LEVEL BREAKDOWN (full period, Core + EMA_RECLAIM — no Spencer)")
    print(f"{'=' * 120}")

    cfg_full = cfg_core_plus_ema_reclaim()
    full_t = collect_trades(all_full, cfg_full, **ctx_kwargs)
    breakdown = setup_breakdown(full_t)

    print(f"\n  {'Setup':<15} │ {'N':>5} {'WR%':>6} {'PF':>5} {'PnL':>9} {'Exp':>6} "
          f"{'StpR':>5} {'DlyS':>5} │ {'Long':>5} {'Short':>5}")
    print(f"  {'─' * 15}─┼{'─' * 44}─┼{'─' * 11}")

    for name in sorted(breakdown):
        trades = breakdown[name]
        s = stats(trades)
        longs = sum(1 for t in trades if t.signal.direction == 1)
        shorts = len(trades) - longs
        print(f"  {name:<15} │ {s['n']:>5} {s['wr']:>5.1f}% {pf_str(s['pf']):>5} "
              f"{s['pnl']:>+9.2f} {s['expectancy']:>+5.3f} "
              f"{s['stop_rate']:>4.0f}% {s['daily_stop_freq']:>5.2f} │ {longs:>5} {shorts:>5}")

    total_s = stats([t for _, t in full_t])
    dd = max_drawdown_pts(full_t)
    print(f"  {'─' * 15}─┼{'─' * 44}─┼{'─' * 11}")
    print(f"  {'TOTAL':<15} │ {total_s['n']:>5} {total_s['wr']:>5.1f}% "
          f"{pf_str(total_s['pf']):>5} {total_s['pnl']:>+9.2f} {total_s['expectancy']:>+5.3f} "
          f"{total_s['stop_rate']:>4.0f}% {total_s['daily_stop_freq']:>5.2f} │ "
          f"maxDD={dd:.1f}")

    # ==================================================================
    #  TEST 2: ROLLING WALK-FORWARD (Core + EMA_RECLAIM only)
    # ==================================================================
    print(f"\n{'=' * 120}")
    print("  TEST 2: ROLLING WALK-FORWARD (Core + EMA_RECLAIM — no Spencer)")
    print(f"{'=' * 120}")

    all_days = sorted(set(
        b.timestamp.date()
        for bars in all_full.values()
        for b in bars
    ))

    train_window = 15
    test_window = 5
    step = 5

    print(f"\n  {'Window':<10} {'Test Period':<25} {'Trds':>5} {'WR%':>6} "
          f"{'PF':>5} {'PnL':>9} {'Exp':>6} │ By Setup")
    print(f"  {'─' * 100}")

    cfg_wf = cfg_core_plus_ema_reclaim()
    walk_results = []
    wnum = 0

    i = 0
    while i + train_window + test_window <= len(all_days):
        test_start = all_days[i + train_window]
        test_end_idx = min(i + train_window + test_window - 1, len(all_days) - 1)
        test_end = all_days[test_end_idx]
        test_dates = set(all_days[i + train_window: i + train_window + test_window])

        window_bars = {}
        for sym, bars in all_full.items():
            eligible = [b for b in bars if b.timestamp.date() <= max(test_dates)]
            if eligible:
                window_bars[sym] = eligible

        wt = collect_trades(window_bars, cfg_wf, count_after_date=min(test_dates), **ctx_kwargs)
        s = stats([t for _, t in wt])

        bd = setup_breakdown(wt)
        setup_strs = []
        for sname in sorted(bd):
            sp = sum(t.pnl_points for t in bd[sname])
            setup_strs.append(f"{sname}({len(bd[sname])}): {sp:+.1f}")

        walk_results.append(s)
        wnum += 1
        print(f"  W{wnum:<8} {str(test_start)}→{str(test_end):<14} "
              f"{s['n']:>5} {s['wr']:>5.1f}% {pf_str(s['pf']):>5} {s['pnl']:>+9.2f} "
              f"{s['expectancy']:>+5.3f} │ "
              f"{', '.join(setup_strs)}")
        i += step

    # Walk-forward summary
    if walk_results:
        prof_w = sum(1 for w in walk_results if w["pnl"] > 0)
        total_w = len(walk_results)
        total_pnl = sum(w["pnl"] for w in walk_results)
        pfs = [w["pf"] for w in walk_results if w["pf"] < 999 and w["n"] > 0]
        avg_pf = statistics.mean(pfs) if pfs else 0
        exps = [w["expectancy"] for w in walk_results if w["n"] > 0]
        avg_exp = statistics.mean(exps) if exps else 0

        print(f"\n  Walk-forward summary:")
        print(f"    Windows:     {total_w}")
        print(f"    Profitable:  {prof_w}/{total_w} ({prof_w/total_w*100:.0f}%)")
        print(f"    Total PnL:   {total_pnl:+.2f} pts")
        print(f"    Avg PF/win:  {avg_pf:.2f}")
        print(f"    Avg Exp:     {avg_exp:+.3f}R")

    # ==================================================================
    #  TEST 3: PER-SYMBOL ROBUSTNESS (Core + EMA_RECLAIM)
    # ==================================================================
    print(f"\n{'=' * 120}")
    print("  TEST 3: PER-SYMBOL ROBUSTNESS (Core + EMA_RECLAIM — no Spencer)")
    print(f"{'=' * 120}")

    cfg_sym = cfg_core_plus_ema_reclaim()
    print(f"\n  {'Sym':<7} │ {'TRAIN':^28} │ {'TEST':^28} │ {'Both+':>5}")
    print(f"  {'':7} │ {'N':>5} {'WR%':>6} {'PnL':>8} {'Exp':>6} {'StpR':>5} │ "
          f"{'N':>5} {'WR%':>6} {'PnL':>8} {'Exp':>6} {'StpR':>5} │")
    print(f"  {'─' * 7}─┼{'─' * 28}─┼{'─' * 28}─┼{'─' * 5}")

    sym_robust = 0
    sym_total = 0
    for sym in sorted(all_full.keys()):
        trn_t = collect_trades({sym: all_train[sym]}, cfg_sym, **ctx_kwargs)
        tst_t = collect_trades({sym: all_full[sym]}, cfg_sym,
                               count_after_date=split_date, **ctx_kwargs)

        trn_s = stats([t for _, t in trn_t])
        tst_s = stats([t for _, t in tst_t])

        if trn_s["n"] == 0 and tst_s["n"] == 0:
            continue

        sym_total += 1
        both = trn_s["pnl"] > 0 and tst_s["pnl"] > 0
        if both:
            sym_robust += 1

        print(f"  {sym:<7} │ {trn_s['n']:>5} {trn_s['wr']:>5.1f}% {trn_s['pnl']:>+8.2f} "
              f"{trn_s['expectancy']:>+5.3f} {trn_s['stop_rate']:>4.0f}% │ "
              f"{tst_s['n']:>5} {tst_s['wr']:>5.1f}% {tst_s['pnl']:>+8.2f} "
              f"{tst_s['expectancy']:>+5.3f} {tst_s['stop_rate']:>4.0f}% │ "
              f"{'YES' if both else 'no':>5}")

    pct = sym_robust / sym_total * 100 if sym_total > 0 else 0
    print(f"\n  Symbols profitable in BOTH periods: {sym_robust}/{sym_total} ({pct:.0f}%)")

    # ==================================================================
    #  SUMMARY
    # ==================================================================
    print(f"\n{'=' * 120}")
    print("  VALIDATION SUMMARY")
    print(f"{'=' * 120}")
    print(f"  Universe:    {args.universe} ({len(all_full)} symbols)")
    print(f"  Split:       {args.split_date}")
    print(f"  Core:        VK KISS + 2ND CHANCE (Spencer tested separately)")
    print(f"  EMA scalp:   buf={cfg_full.ema_scalp_stop_buffer_atr}×ATR, "
          f"exit={cfg_full.ema_scalp_exit_mode}, "
          f"time={cfg_full.ema_scalp_time_stop_bars}bars, "
          f"Q>={cfg_full.ema_scalp_min_quality}")
    fpip_cfg = cfg_core_plus_fpip()
    print(f"  EMA FPIP:    RVOL>={fpip_cfg.ema_fpip_rvol_tod_min}, "
          f"exit={fpip_cfg.ema_fpip_exit_mode}, "
          f"time={fpip_cfg.ema_fpip_time_stop_bars}bars, "
          f"Q>={fpip_cfg.ema_fpip_min_quality}, "
          f"PB vol<={fpip_cfg.ema_fpip_max_pullback_volume_ratio}×exp")
    sc2_cfg = cfg_sc_v2_only()
    print(f"  SC_V2:       exit={sc2_cfg.sc2_exit_mode}, "
          f"time={sc2_cfg.sc2_time_stop_bars}bars, "
          f"Q>={sc2_cfg.sc2_min_quality}, "
          f"reset vol<={sc2_cfg.sc2_max_reset_volume_ratio}×exp, "
          f"max depth<={sc2_cfg.sc2_max_reset_depth_pct*100:.0f}%")
    print(f"  Context:     granular (VWAP/EMA9/EMA20/slope per snapshot)")
    print(f"  Slippage:    dynamic (bps={cfg_full.slip_bps}, cap={cfg_full.slip_vol_mult_cap}x)")
    print(f"  Disabled:    FAILED BOUNCE, old EMA_RETEST, EMA_PULL, EMA_CONFIRM")
    print(f"{'=' * 120}\n")


if __name__ == "__main__":
    main()
