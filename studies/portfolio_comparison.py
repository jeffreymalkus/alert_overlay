"""
Portfolio Comparison Study — R-only

Three explicit portfolio configs compared head-to-head:
  1. FROZEN_CURRENT:  Long=VK only,      Short=BDR_SHORT (frozen)
  2. FROZEN_PLUS_VR:  Long=VK + VR,      Short=BDR_SHORT (frozen)
  3. VR_REPLACES_VK:  Long=VR only,      Short=BDR_SHORT (frozen)

Does NOT modify OverlayConfig defaults.
All configs are built explicitly with named overrides.

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.portfolio_comparison
"""

import copy
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..backtest import run_backtest, Trade
from ..config import OverlayConfig
from .experiment_harness import (
    _load_bars, get_universe, get_sector_bars_dict,
    classify_spy_days, RTrade, compute_metrics, split_train_test,
    robustness_check, pf_str,
)
from ..market_context import get_sector_etf
from ..models import SetupId, SETUP_DISPLAY_NAME


# ════════════════════════════════════════════════════════════════
#  Explicit Portfolio Configs
# ════════════════════════════════════════════════════════════════

def _base_cfg() -> OverlayConfig:
    """Minimal base: everything OFF except BDR_SHORT (frozen)."""
    cfg = OverlayConfig()
    # Disable ALL long setups
    cfg.show_trend_setups = False       # VK
    cfg.show_second_chance = False
    cfg.show_sc_v2 = False
    cfg.show_spencer = False
    cfg.show_ema_scalp = False
    cfg.show_ema_fpip = False
    cfg.show_reversal_setups = False
    cfg.show_ema_retest = False
    cfg.show_ema_mean_rev = False
    cfg.show_ema_pullback = False
    cfg.show_ema_confirm = False
    cfg.show_mcs = False
    cfg.show_vwap_reclaim = False
    cfg.show_failed_bounce = False
    # BDR_SHORT stays ON with frozen params
    cfg.show_breakdown_retest = True
    cfg.bdr_require_red_trend = True
    cfg.bdr_am_only = True
    cfg.bdr_am_cutoff = 1100
    cfg.bdr_min_rejection_wick_pct = 0.30
    return cfg


def cfg_frozen_current() -> OverlayConfig:
    """Portfolio 1: FROZEN_CURRENT — VK long + BDR short."""
    cfg = _base_cfg()
    cfg.show_trend_setups = True   # enables VK
    cfg.vk_long_only = True
    return cfg


def cfg_frozen_plus_vr() -> OverlayConfig:
    """Portfolio 2: FROZEN_PLUS_VR — VK + VR long + BDR short."""
    cfg = _base_cfg()
    cfg.show_trend_setups = True   # enables VK
    cfg.vk_long_only = True
    cfg.show_vwap_reclaim = True   # add VR
    # VR params: canonical H1 from validation
    cfg.vr_time_start = 1000
    cfg.vr_time_end = 1059
    cfg.vr_hold_bars = 3
    cfg.vr_target_rr = 3.0
    cfg.vr_min_body_pct = 0.40
    cfg.vr_require_bull = True
    cfg.vr_require_vol = True
    cfg.vr_vol_frac = 0.70
    cfg.vr_stop_buffer = 0.02
    cfg.vr_long_only = True
    cfg.vr_require_green_day = True
    cfg.vr_require_market_align = True
    cfg.vr_require_sector_align = False
    return cfg


def cfg_vr_replaces_vk() -> OverlayConfig:
    """Portfolio 3: VR_REPLACES_VK — VR long only + BDR short."""
    cfg = _base_cfg()
    cfg.show_trend_setups = False  # VK OFF
    cfg.show_vwap_reclaim = True   # VR ON
    # Same VR params as portfolio 2
    cfg.vr_time_start = 1000
    cfg.vr_time_end = 1059
    cfg.vr_hold_bars = 3
    cfg.vr_target_rr = 3.0
    cfg.vr_min_body_pct = 0.40
    cfg.vr_require_bull = True
    cfg.vr_require_vol = True
    cfg.vr_vol_frac = 0.70
    cfg.vr_stop_buffer = 0.02
    cfg.vr_long_only = True
    cfg.vr_require_green_day = True
    cfg.vr_require_market_align = True
    cfg.vr_require_sector_align = False
    return cfg


PORTFOLIOS = {
    "FROZEN_CURRENT": cfg_frozen_current,
    "FROZEN_PLUS_VR": cfg_frozen_plus_vr,
    "VR_REPLACES_VK": cfg_vr_replaces_vk,
}


# ════════════════════════════════════════════════════════════════
#  Run engine and collect trades with portfolio-level filtering
# ════════════════════════════════════════════════════════════════

def run_portfolio(cfg: OverlayConfig, symbols: list, spy_bars: list,
                  qqq_bars: list, sector_bars_dict: dict,
                  spy_day_info: dict) -> List[RTrade]:
    """
    Run engine with cfg, apply portfolio-level filters:
      - Longs: non-RED, entry before 15:30
      - Shorts: pass through (BDR gates itself in-engine)
    """
    all_trades = []
    for sym in symbols:
        bars = _load_bars(sym)
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf)
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        for t in result.trades:
            all_trades.append(RTrade(t, symbol=sym))

    # Portfolio filter
    def is_red(t):
        return spy_day_info.get(t.entry_date, {}).get("direction") == "RED"

    def hhmm(t):
        return t.entry_time.hour * 100 + t.entry_time.minute

    filtered = []
    for t in all_trades:
        if t.direction == 1:
            # Long: locked filter (non-RED, before 15:30)
            if not is_red(t) and hhmm(t) < 1530:
                filtered.append(t)
        else:
            # Short: keep all (BDR gates in-engine: RED+TREND, AM, big wick)
            filtered.append(t)
    return filtered


# ════════════════════════════════════════════════════════════════
#  Extended metrics
# ════════════════════════════════════════════════════════════════

def extended_metrics(trades: List[RTrade]) -> dict:
    """Compute all requested metrics for a trade set."""
    m = compute_metrics(trades)
    n = m["n"]
    if n == 0:
        return {
            "n": 0, "pf_r": 0, "exp_r": 0, "total_r": 0, "max_dd_r": 0,
            "stop_rate": 0, "quick_stop_rate": 0,
            "ex_best_day_total_r": 0, "ex_best_day_pf": 0,
            "ex_top_sym_total_r": 0, "ex_top_sym_pf": 0,
            "best_day": None, "best_day_r": 0,
            "top_sym": None, "top_sym_r": 0,
            "train_pf": 0, "test_pf": 0,
            "train_exp": 0, "test_exp": 0,
        }

    # Quick-stop: stopped within 2 bars
    quick_stopped = sum(1 for t in trades if t.exit_reason == "stop" and t.bars_held <= 2)

    # Robustness: ex-best-day and ex-top-symbol
    daily_r = defaultdict(float)
    sym_r = defaultdict(float)
    for t in trades:
        if t.entry_date:
            daily_r[t.entry_date] += t.pnl_rr
        sym_r[t.symbol] += t.pnl_rr

    best_day = max(daily_r, key=daily_r.get) if daily_r else None
    top_sym = max(sym_r, key=sym_r.get) if sym_r else None

    ex_day = [t for t in trades if t.entry_date != best_day] if best_day else trades
    ex_sym = [t for t in trades if t.symbol != top_sym] if top_sym else trades

    ex_day_m = compute_metrics(ex_day)
    ex_sym_m = compute_metrics(ex_sym)

    # Train/test
    train, test = split_train_test(trades)
    tr_m = compute_metrics(train)
    te_m = compute_metrics(test)

    return {
        "n": n,
        "pf_r": m["pf_r"],
        "exp_r": m["exp_r"],
        "total_r": m["total_r"],
        "max_dd_r": m["max_dd_r"],
        "stop_rate": m["stop_rate"],
        "quick_stop_rate": quick_stopped / n * 100,
        "ex_best_day_total_r": ex_day_m["total_r"],
        "ex_best_day_pf": ex_day_m["pf_r"],
        "ex_top_sym_total_r": ex_sym_m["total_r"],
        "ex_top_sym_pf": ex_sym_m["pf_r"],
        "best_day": str(best_day),
        "best_day_r": daily_r.get(best_day, 0),
        "top_sym": top_sym,
        "top_sym_r": sym_r.get(top_sym, 0),
        "train_pf": tr_m["pf_r"],
        "test_pf": te_m["pf_r"],
        "train_exp": tr_m["exp_r"],
        "test_exp": te_m["exp_r"],
        "train_n": tr_m["n"],
        "test_n": te_m["n"],
    }


def setup_breakdown(trades: List[RTrade]) -> dict:
    """Per-setup metrics."""
    by_setup = defaultdict(list)
    for t in trades:
        by_setup[t.setup_name].append(t)
    result = {}
    for sn in sorted(by_setup):
        st = by_setup[sn]
        sm = compute_metrics(st)
        result[sn] = {
            "n": sm["n"],
            "pf_r": sm["pf_r"],
            "exp_r": sm["exp_r"],
            "total_r": sm["total_r"],
            "stop_rate": sm["stop_rate"],
            "direction": "LONG" if st[0].direction == 1 else "SHORT",
        }
    return result


def overlap_analysis(trades: List[RTrade]) -> dict:
    """VK vs VR overlap: same-day and same-bar collisions."""
    vk_trades = [t for t in trades if t.setup_name == "VWAP KISS"]
    vr_trades = [t for t in trades if t.setup_name == "VWAP RECLAIM"]

    if not vk_trades or not vr_trades:
        return {
            "vk_n": len(vk_trades), "vr_n": len(vr_trades),
            "same_day_overlap": 0, "same_bar_overlap": 0,
            "overlap_pct": 0, "additive": True,
        }

    # Same sym-day overlap
    vk_sym_days = {(t.symbol, t.entry_date) for t in vk_trades}
    vr_sym_days = {(t.symbol, t.entry_date) for t in vr_trades}
    same_day = vk_sym_days & vr_sym_days

    # Same sym-bar overlap (same symbol, same entry timestamp)
    vk_sym_bars = {(t.symbol, t.entry_time) for t in vk_trades}
    vr_sym_bars = {(t.symbol, t.entry_time) for t in vr_trades}
    same_bar = vk_sym_bars & vr_sym_bars

    total_unique = len(vk_sym_days | vr_sym_days)
    overlap_pct = len(same_day) / total_unique * 100 if total_unique > 0 else 0

    return {
        "vk_n": len(vk_trades),
        "vr_n": len(vr_trades),
        "same_day_overlap": len(same_day),
        "same_bar_overlap": len(same_bar),
        "overlap_pct": overlap_pct,
        "additive": overlap_pct < 20,  # <20% overlap = additive
    }


def concentration_analysis(trades: List[RTrade]) -> dict:
    """Top 5 symbols and days by R contribution."""
    daily_r = defaultdict(float)
    sym_r = defaultdict(float)
    for t in trades:
        if t.entry_date:
            daily_r[t.entry_date] += t.pnl_rr
        sym_r[t.symbol] += t.pnl_rr

    top5_sym = sorted(sym_r.items(), key=lambda x: x[1], reverse=True)[:5]
    top5_day = sorted(daily_r.items(), key=lambda x: x[1], reverse=True)[:5]

    total_r = sum(t.pnl_rr for t in trades)
    top5_sym_r = sum(r for _, r in top5_sym)
    top5_day_r = sum(r for _, r in top5_day)

    return {
        "top5_symbols": [(s, round(r, 2)) for s, r in top5_sym],
        "top5_days": [(str(d), round(r, 2)) for d, r in top5_day],
        "top5_sym_pct": top5_sym_r / total_r * 100 if total_r > 0 else 0,
        "top5_day_pct": top5_day_r / total_r * 100 if total_r > 0 else 0,
    }


# ════════════════════════════════════════════════════════════════
#  Pretty printing
# ════════════════════════════════════════════════════════════════

def print_portfolio_metrics(name: str, m: dict):
    print(f"\n  ┌── {name} ──")
    print(f"  │ Trades:     {m['n']:5d}")
    print(f"  │ PF(R):      {pf_str(m['pf_r']):>6s}")
    print(f"  │ Exp(R):     {m['exp_r']:+.3f}R")
    print(f"  │ Total R:    {m['total_r']:+.2f}R")
    print(f"  │ Max DD(R):  {m['max_dd_r']:.2f}R")
    print(f"  │ Stop Rate:  {m['stop_rate']:.1f}%")
    print(f"  │ Quick-Stop: {m['quick_stop_rate']:.1f}%")
    print(f"  │ ExBestDay:  {m['ex_best_day_total_r']:+.2f}R  PF={pf_str(m['ex_best_day_pf'])}"
          f"  (best={m['best_day']} {m['best_day_r']:+.2f}R)")
    print(f"  │ ExTopSym:   {m['ex_top_sym_total_r']:+.2f}R  PF={pf_str(m['ex_top_sym_pf'])}"
          f"  (top={m['top_sym']} {m['top_sym_r']:+.2f}R)")
    print(f"  │ Train:      N={m['train_n']:4d}  PF={pf_str(m['train_pf']):>6s}  Exp={m['train_exp']:+.3f}R")
    print(f"  │ Test:       N={m['test_n']:4d}  PF={pf_str(m['test_pf']):>6s}  Exp={m['test_exp']:+.3f}R")
    stable = m['train_pf'] >= 1.0 and m['test_pf'] >= 1.0
    print(f"  │ STABLE:     {'YES' if stable else 'NO'}")
    print(f"  └──")


def print_comparison_table(results: dict):
    print(f"\n  {'Portfolio':20s} {'N':>5s} {'PF(R)':>6s} {'Exp(R)':>8s} {'TotalR':>8s} "
          f"{'MaxDD':>7s} {'Stop%':>6s} {'QStop%':>6s} "
          f"{'ExDay':>8s} {'ExSym':>8s} {'TrnPF':>6s} {'TstPF':>6s} {'TrnExp':>8s} {'TstExp':>8s} {'Stable':>6s}")
    print(f"  {'-'*20} {'-'*5} {'-'*6} {'-'*8} {'-'*8} "
          f"{'-'*7} {'-'*6} {'-'*6} "
          f"{'-'*8} {'-'*8} {'-'*6} {'-'*6} {'-'*8} {'-'*8} {'-'*6}")

    for name, m in results.items():
        stable = m['train_pf'] >= 1.0 and m['test_pf'] >= 1.0
        print(f"  {name:20s} {m['n']:5d} {pf_str(m['pf_r']):>6s} {m['exp_r']:+7.3f}R "
              f"{m['total_r']:+7.2f}R {m['max_dd_r']:6.2f}R {m['stop_rate']:5.1f}% {m['quick_stop_rate']:5.1f}% "
              f"{m['ex_best_day_total_r']:+7.2f}R {m['ex_top_sym_total_r']:+7.2f}R "
              f"{pf_str(m['train_pf']):>6s} {pf_str(m['test_pf']):>6s} "
              f"{m['train_exp']:+7.3f}R {m['test_exp']:+7.3f}R "
              f"{'YES' if stable else ' NO':>6s}")


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    symbols = get_universe()
    spy_bars = _load_bars("SPY")
    qqq_bars = _load_bars("QQQ")
    sector_bars_dict = get_sector_bars_dict()
    spy_day_info = classify_spy_days(spy_bars)

    print("=" * 130)
    print("PORTFOLIO COMPARISON STUDY — R-only")
    print("=" * 130)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Data: {spy_bars[0].timestamp.date()} → {spy_bars[-1].timestamp.date()}")
    print(f"Long filter: non-RED, <15:30 (applied post-engine)")
    print(f"Short filter: BDR_SHORT in-engine (RED+TREND, AM, big wick)")

    all_metrics = {}
    all_trades = {}
    all_breakdowns = {}
    all_concentrations = {}

    for pname, cfg_fn in PORTFOLIOS.items():
        print(f"\n{'─'*130}")
        print(f"Running: {pname}")
        print(f"{'─'*130}")

        cfg = cfg_fn()
        trades = run_portfolio(cfg, symbols, spy_bars, qqq_bars,
                               sector_bars_dict, spy_day_info)
        all_trades[pname] = trades

        m = extended_metrics(trades)
        all_metrics[pname] = m
        print_portfolio_metrics(pname, m)

        # Setup breakdown
        bd = setup_breakdown(trades)
        all_breakdowns[pname] = bd
        if bd:
            print(f"\n  Setup breakdown:")
            for sn, sm in bd.items():
                print(f"    {sn:18s} {sm['direction']:5s}  N={sm['n']:4d}  PF={pf_str(sm['pf_r']):>6s}  "
                      f"Exp={sm['exp_r']:+.3f}R  TotalR={sm['total_r']:+.2f}R  Stop={sm['stop_rate']:.1f}%")

        # Concentration
        conc = concentration_analysis(trades)
        all_concentrations[pname] = conc
        print(f"\n  Top 5 symbols ({conc['top5_sym_pct']:.1f}% of total R):")
        for sym, r in conc["top5_symbols"]:
            print(f"    {sym:8s}  {r:+.2f}R")
        print(f"\n  Top 5 days ({conc['top5_day_pct']:.1f}% of total R):")
        for day, r in conc["top5_days"]:
            print(f"    {day}  {r:+.2f}R")

    # ── Overlap analysis (from FROZEN_PLUS_VR which has both) ──
    print(f"\n{'='*130}")
    print("OVERLAP ANALYSIS — VK vs VR (from FROZEN_PLUS_VR)")
    print(f"{'='*130}")

    ov = overlap_analysis(all_trades.get("FROZEN_PLUS_VR", []))
    print(f"  VK trades:          {ov['vk_n']}")
    print(f"  VR trades:          {ov['vr_n']}")
    print(f"  Same-day overlap:   {ov['same_day_overlap']}")
    print(f"  Same-bar overlap:   {ov['same_bar_overlap']}")
    print(f"  Overlap %:          {ov['overlap_pct']:.1f}%")
    print(f"  Additive:           {'YES' if ov['additive'] else 'NO'}")

    # ── Long-side contribution ──
    print(f"\n{'='*130}")
    print("LONG-SIDE CONTRIBUTION BY SETUP")
    print(f"{'='*130}")
    for pname in PORTFOLIOS:
        longs = [t for t in all_trades[pname] if t.direction == 1]
        shorts = [t for t in all_trades[pname] if t.direction != 1]
        long_m = compute_metrics(longs)
        short_m = compute_metrics(shorts)
        print(f"\n  {pname}:")
        print(f"    Longs:  N={long_m['n']:4d}  PF={pf_str(long_m['pf_r']):>6s}  "
              f"Exp={long_m['exp_r']:+.3f}R  TotalR={long_m['total_r']:+.2f}R")
        print(f"    Shorts: N={short_m['n']:4d}  PF={pf_str(short_m['pf_r']):>6s}  "
              f"Exp={short_m['exp_r']:+.3f}R  TotalR={short_m['total_r']:+.2f}R")

        # Per-setup long breakdown
        long_bd = setup_breakdown(longs)
        for sn, sm in long_bd.items():
            print(f"      {sn:18s}  N={sm['n']:4d}  PF={pf_str(sm['pf_r']):>6s}  "
                  f"Exp={sm['exp_r']:+.3f}R  TotalR={sm['total_r']:+.2f}R")

    # ── Comparison table ──
    print(f"\n{'='*130}")
    print("HEAD-TO-HEAD COMPARISON TABLE")
    print(f"{'='*130}")
    print_comparison_table(all_metrics)

    # ── Delta table ──
    print(f"\n{'='*130}")
    print("DELTA vs FROZEN_CURRENT")
    print(f"{'='*130}")
    base = all_metrics["FROZEN_CURRENT"]
    for pname, m in all_metrics.items():
        if pname == "FROZEN_CURRENT":
            continue
        print(f"\n  {pname} vs FROZEN_CURRENT:")
        print(f"    ΔN:       {m['n'] - base['n']:+d}")
        print(f"    ΔPF(R):   {m['pf_r'] - base['pf_r']:+.3f}" if base['pf_r'] < 900 and m['pf_r'] < 900 else "    ΔPF(R):   N/A (inf)")
        print(f"    ΔExp(R):  {m['exp_r'] - base['exp_r']:+.4f}R")
        print(f"    ΔTotalR:  {m['total_r'] - base['total_r']:+.2f}R")
        print(f"    ΔMaxDD:   {m['max_dd_r'] - base['max_dd_r']:+.2f}R")
        print(f"    ΔStopRt:  {m['stop_rate'] - base['stop_rate']:+.1f}%")

    # ── Save JSON ──
    output = {
        "metrics": {k: {kk: round(vv, 4) if isinstance(vv, float) else vv
                         for kk, vv in v.items()}
                    for k, v in all_metrics.items()},
        "breakdowns": all_breakdowns,
        "overlap": ov,
        "concentrations": all_concentrations,
    }
    out_path = Path(__file__).parent.parent / "portfolio_comparison_results.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved: {out_path}")

    print(f"\n{'='*130}")
    print("STUDY COMPLETE")
    print(f"{'='*130}")


if __name__ == "__main__":
    main()
