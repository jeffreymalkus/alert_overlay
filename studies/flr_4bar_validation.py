"""
FLR 4-BAR TURN — Deep Validation

4-BAR TURN passed promotion criteria: PF 1.03, N=660, Exp=+0.018
This script runs robustness checks:
  1. Cross-sample validation (4 groups)
  2. Ex-best-day PF
  3. Ex-top-symbol PF
  4. Monthly breakdown
  5. Exit analysis
  6. 4-bar combos (stop, body, dec variants)
  7. 5-bar turn (even more selective)

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.studies.flr_4bar_validation
"""
import sys
import math
import random
from collections import defaultdict
from pathlib import Path
from datetime import date

from ..backtest import load_bars_from_csv, run_backtest
from ..config import OverlayConfig
from ..models import SetupId
from ..market_context import SECTOR_MAP, get_sector_etf
from ..replays.new_strategy_replay import (
    PTrade, compute_metrics, pf_str, _run_all_symbols,
)

DATA_DIR = Path(__file__).parent.parent / "data"
FLR_IDS = {SetupId.FL_MOMENTUM_REBUILD}


def _base_cfg():
    cfg = OverlayConfig()
    for attr in dir(cfg):
        if attr.startswith("show_"):
            setattr(cfg, attr, False)
    cfg.require_regime = False
    return cfg


def _flr_cfg(**overrides):
    cfg = _base_cfg()
    cfg.show_fl_momentum_rebuild = True
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _pf(trades):
    gw = sum(t.pnl_rr for t in trades if t.pnl_rr > 0)
    gl = abs(sum(t.pnl_rr for t in trades if t.pnl_rr <= 0))
    return gw / gl if gl > 0 else float("inf")


def main():
    wl_path = DATA_DIR.parent / "watchlist_expanded.txt"
    if not wl_path.exists():
        wl_path = DATA_DIR.parent / "watchlist.txt"
    all_symbols = [s.strip() for s in wl_path.read_text().splitlines()
                   if s.strip() and not s.startswith("#")]
    print(f"Universe: {len(all_symbols)} symbols")

    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()):
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))
    print(f"Loaded.\n")

    # ═══ 1. Full universe baseline ═══
    print("=" * 100)
    print("  1. FULL UNIVERSE — 4-BAR TURN vs neighbors")
    print("=" * 100)

    for label, overrides in [
        ("3-BAR TURN", {"flr_turn_confirm_bars": 3}),
        ("4-BAR TURN", {"flr_turn_confirm_bars": 4}),
        ("5-BAR TURN", {"flr_turn_confirm_bars": 5}),
        ("4-BAR + stop=0.45", {"flr_turn_confirm_bars": 4, "flr_stop_frac": 0.45}),
        ("4-BAR + stop=0.50", {"flr_turn_confirm_bars": 4, "flr_stop_frac": 0.50}),
        ("4-BAR + body=0.65", {"flr_turn_confirm_bars": 4, "flr_cross_body_pct": 0.65}),
        ("4-BAR + dec=3.5", {"flr_turn_confirm_bars": 4, "flr_min_decline_atr": 3.5}),
        ("4-BAR + accel(0.001)", {"flr_turn_confirm_bars": 4, "flr_require_ema_accel": True, "flr_ema_accel_min": 0.001}),
        ("4-BAR + stop=0.50 + body=0.65", {"flr_turn_confirm_bars": 4, "flr_stop_frac": 0.50, "flr_cross_body_pct": 0.65}),
    ]:
        cfg = _flr_cfg(**overrides)
        trades = _run_all_symbols(all_symbols, cfg, spy_bars, qqq_bars,
                                  sector_bars_dict, setup_filter=FLR_IDS)
        m = compute_metrics(trades)
        n_stop = sum(1 for t in trades if t.exit_reason == "stop")
        stop_pct = (n_stop / len(trades) * 100) if trades else 0
        c1 = "✓" if m["pf"] > 1.0 else "✗"
        c2 = "✓" if m["exp"] > 0 else "✗"
        c3 = "✓" if m["train_pf"] > 0.80 else "✗"
        c4 = "✓" if m["test_pf"] > 0.80 else "✗"
        print(f"  {label:<38s}  N={m['n']:5d}  PF={pf_str(m['pf'])}  Exp={m['exp']:+.3f}  "
              f"WR={m['wr']*100:.1f}%  trn={pf_str(m['train_pf'])} tst={pf_str(m['test_pf'])}  "
              f"stp={stop_pct:.0f}%  [{c1}{c2}{c3}{c4}]")

    # ═══ 2. Cross-sample validation (4 groups) ═══
    print(f"\n{'=' * 100}")
    print("  2. CROSS-SAMPLE VALIDATION — 4 groups")
    print("=" * 100)

    random.seed(42)
    shuffled = list(all_symbols)
    random.shuffle(shuffled)
    g_size = len(shuffled) // 4
    groups = [
        shuffled[:g_size],
        shuffled[g_size:2*g_size],
        shuffled[2*g_size:3*g_size],
        shuffled[3*g_size:],
    ]

    cfg_4bar = _flr_cfg(flr_turn_confirm_bars=4)
    for gi, grp in enumerate(groups, 1):
        trades = _run_all_symbols(grp, cfg_4bar, spy_bars, qqq_bars,
                                  sector_bars_dict, setup_filter=FLR_IDS)
        m = compute_metrics(trades)
        print(f"  G{gi} ({len(grp)} syms): N={m['n']:4d}  PF={pf_str(m['pf'])}  "
              f"Exp={m['exp']:+.3f}  WR={m['wr']*100:.1f}%  trn={pf_str(m['train_pf'])} tst={pf_str(m['test_pf'])}")

    # ═══ 3. Ex-best-day / ex-top-symbol ═══
    print(f"\n{'=' * 100}")
    print("  3. ROBUSTNESS — ex-best-day, ex-top-symbol")
    print("=" * 100)

    all_trades_4bar = _run_all_symbols(all_symbols, cfg_4bar, spy_bars, qqq_bars,
                                       sector_bars_dict, setup_filter=FLR_IDS)

    # ex-best-day
    day_r = defaultdict(float)
    for t in all_trades_4bar:
        day_r[t.entry_date] += t.pnl_rr
    best_day = max(day_r, key=day_r.get)
    ex_best = [t for t in all_trades_4bar if t.entry_date != best_day]
    m_ex_best = compute_metrics(ex_best)
    print(f"  Best day: {best_day} ({day_r[best_day]:+.1f}R from {sum(1 for t in all_trades_4bar if t.entry_date == best_day)} trades)")
    print(f"  Ex-best-day: N={m_ex_best['n']:5d}  PF={pf_str(m_ex_best['pf'])}  Exp={m_ex_best['exp']:+.3f}")

    # ex-top-symbol
    sym_r = defaultdict(float)
    for t in all_trades_4bar:
        sym_r[t.symbol] += t.pnl_rr
    best_sym = max(sym_r, key=sym_r.get)
    ex_best_sym = [t for t in all_trades_4bar if t.symbol != best_sym]
    m_ex_sym = compute_metrics(ex_best_sym)
    print(f"  Best symbol: {best_sym} ({sym_r[best_sym]:+.1f}R from {sum(1 for t in all_trades_4bar if t.symbol == best_sym)} trades)")
    print(f"  Ex-best-sym: N={m_ex_sym['n']:5d}  PF={pf_str(m_ex_sym['pf'])}  Exp={m_ex_sym['exp']:+.3f}")

    # worst symbol
    worst_sym = min(sym_r, key=sym_r.get)
    ex_worst_sym = [t for t in all_trades_4bar if t.symbol != worst_sym]
    m_ex_worst = compute_metrics(ex_worst_sym)
    print(f"  Worst symbol: {worst_sym} ({sym_r[worst_sym]:+.1f}R from {sum(1 for t in all_trades_4bar if t.symbol == worst_sym)} trades)")
    print(f"  Ex-worst-sym: N={m_ex_worst['n']:5d}  PF={pf_str(m_ex_worst['pf'])}  Exp={m_ex_worst['exp']:+.3f}")

    # ═══ 4. Monthly breakdown ═══
    print(f"\n{'=' * 100}")
    print("  4. MONTHLY BREAKDOWN")
    print("=" * 100)

    month_trades = defaultdict(list)
    for t in all_trades_4bar:
        key = t.entry_date.strftime("%Y-%m")
        month_trades[key].append(t)

    pos_months = 0
    neg_months = 0
    for month in sorted(month_trades.keys()):
        mt = month_trades[month]
        m_month = compute_metrics(mt)
        total_r = sum(t.pnl_rr for t in mt)
        sign = "+" if total_r >= 0 else "-"
        if total_r >= 0:
            pos_months += 1
        else:
            neg_months += 1
        print(f"  {month}  N={len(mt):4d}  PF={pf_str(m_month['pf'])}  "
              f"TotalR={total_r:+6.1f}  WR={m_month['wr']*100:.1f}%")

    print(f"\n  Positive months: {pos_months}, Negative months: {neg_months}")

    # ═══ 5. Exit analysis ═══
    print(f"\n{'=' * 100}")
    print("  5. EXIT ANALYSIS")
    print("=" * 100)

    exit_groups = defaultdict(list)
    for t in all_trades_4bar:
        exit_groups[t.exit_reason].append(t)

    for reason in sorted(exit_groups.keys()):
        group = exit_groups[reason]
        n = len(group)
        wr = sum(1 for t in group if t.pnl_rr > 0) / n * 100 if n else 0
        avg_r = sum(t.pnl_rr for t in group) / n if n else 0
        pct = n / len(all_trades_4bar) * 100
        print(f"  {reason:<15s}  N={n:4d} ({pct:4.1f}%)  WR={wr:.1f}%  AvgR={avg_r:+.3f}")

    # ═══ 6. Top/Bottom symbols ═══
    print(f"\n{'=' * 100}")
    print("  6. TOP/BOTTOM SYMBOLS")
    print("=" * 100)

    sym_data = {}
    for sym, total_r in sym_r.items():
        n = sum(1 for t in all_trades_4bar if t.symbol == sym)
        wr = sum(1 for t in all_trades_4bar if t.symbol == sym and t.pnl_rr > 0) / n * 100
        sym_data[sym] = (n, total_r, wr)

    sorted_syms = sorted(sym_data.items(), key=lambda x: x[1][1], reverse=True)
    print("  TOP 10:")
    for sym, (n, tr, wr) in sorted_syms[:10]:
        print(f"    {sym:<6s}  N={n:3d}  TotalR={tr:+6.1f}  WR={wr:.0f}%")
    print("  BOTTOM 10:")
    for sym, (n, tr, wr) in sorted_syms[-10:]:
        print(f"    {sym:<6s}  N={n:3d}  TotalR={tr:+6.1f}  WR={wr:.0f}%")

    print(f"\nDone.")


if __name__ == "__main__":
    main()
