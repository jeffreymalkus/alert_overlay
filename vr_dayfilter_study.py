"""
VR Day-Filter Variant Study — R-only

Tests three VR day-filter modes enforced IN-ENGINE:
  1. VR_GREEN_ONLY        — vr_day_filter="green_only"  (SPY > +0.05%)
  2. VR_NON_RED           — vr_day_filter="non_red"     (SPY >= -0.05%)
  3. VR_MARKET_ALIGN_ONLY — vr_day_filter="none"        (no day gate; market_align only)

Plus FROZEN_CURRENT as baseline.

All portfolios use VR_REPLACES_VK structure (VR long + BDR short).
Does NOT modify OverlayConfig defaults.

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.vr_dayfilter_study
"""

import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from .backtest import run_backtest
from .config import OverlayConfig
from .experiment_harness import (
    _load_bars, get_universe, get_sector_bars_dict,
    classify_spy_days, RTrade, compute_metrics, split_train_test, pf_str,
)
from .market_context import get_sector_etf
from .models import SETUP_DISPLAY_NAME


# ════════════════════════════════════════════════════════════════
#  Explicit configs — no config drift
# ════════════════════════════════════════════════════════════════

def _base_cfg() -> OverlayConfig:
    """Everything OFF except BDR_SHORT (frozen)."""
    cfg = OverlayConfig()
    cfg.show_trend_setups = False
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
    # BDR frozen
    cfg.show_breakdown_retest = True
    cfg.bdr_require_red_trend = True
    cfg.bdr_am_only = True
    cfg.bdr_am_cutoff = 1100
    cfg.bdr_min_rejection_wick_pct = 0.30
    return cfg


def _vr_params(cfg: OverlayConfig) -> OverlayConfig:
    """Apply canonical VR H1 params."""
    cfg.show_vwap_reclaim = True
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
    cfg.vr_require_market_align = True
    cfg.vr_require_sector_align = False
    return cfg


def cfg_frozen_current() -> OverlayConfig:
    """FROZEN_CURRENT: VK long + BDR short."""
    cfg = _base_cfg()
    cfg.show_trend_setups = True
    cfg.vk_long_only = True
    return cfg


def cfg_vr_green_only() -> OverlayConfig:
    """VR_GREEN_ONLY: VR long (GREEN days only) + BDR short."""
    cfg = _base_cfg()
    cfg = _vr_params(cfg)
    cfg.vr_day_filter = "green_only"
    return cfg


def cfg_vr_non_red() -> OverlayConfig:
    """VR_NON_RED: VR long (non-RED days) + BDR short."""
    cfg = _base_cfg()
    cfg = _vr_params(cfg)
    cfg.vr_day_filter = "non_red"
    return cfg


def cfg_vr_market_align_only() -> OverlayConfig:
    """VR_MARKET_ALIGN_ONLY: VR long (no day gate, market_align only) + BDR short."""
    cfg = _base_cfg()
    cfg = _vr_params(cfg)
    cfg.vr_day_filter = "none"  # no SPY-day gate
    return cfg


PORTFOLIOS = {
    "FROZEN_CURRENT":       cfg_frozen_current,
    "VR_GREEN_ONLY":        cfg_vr_green_only,
    "VR_NON_RED":           cfg_vr_non_red,
    "VR_MARKET_ALIGN_ONLY": cfg_vr_market_align_only,
}


# ════════════════════════════════════════════════════════════════
#  Run engine + portfolio-level filter
# ════════════════════════════════════════════════════════════════

def run_portfolio(cfg: OverlayConfig, symbols: list, spy_bars: list,
                  qqq_bars: list, sector_bars_dict: dict,
                  spy_day_info: dict) -> List[RTrade]:
    """
    Longs: harness-level non-RED + <15:30 (matches frozen book rule).
    Shorts: pass through (BDR gates in-engine).

    NOTE: For VR variants, the in-engine vr_day_filter applies BEFORE this
    harness filter. So GREEN_ONLY in-engine + non-RED harness = effectively GREEN_ONLY.
    The harness filter is a safety net, not the primary gate for VR.
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

    def is_red(t):
        return spy_day_info.get(t.entry_date, {}).get("direction") == "RED"

    def hhmm(t):
        return t.entry_time.hour * 100 + t.entry_time.minute

    filtered = []
    for t in all_trades:
        if t.direction == 1:
            if not is_red(t) and hhmm(t) < 1530:
                filtered.append(t)
        else:
            filtered.append(t)
    return filtered


# ════════════════════════════════════════════════════════════════
#  Metrics
# ════════════════════════════════════════════════════════════════

def full_metrics(trades: List[RTrade]) -> dict:
    m = compute_metrics(trades)
    n = m["n"]
    if n == 0:
        return {k: 0 for k in [
            "n", "pf_r", "exp_r", "total_r", "max_dd_r", "stop_rate",
            "quick_stop_rate", "ex_best_day_total_r", "ex_best_day_pf",
            "ex_top_sym_total_r", "ex_top_sym_pf", "best_day", "best_day_r",
            "top_sym", "top_sym_r", "train_pf", "test_pf", "train_exp",
            "test_exp", "train_n", "test_n",
        ]}

    quick_stopped = sum(1 for t in trades if t.exit_reason == "stop" and t.bars_held <= 2)

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

    train, test = split_train_test(trades)
    tr_m = compute_metrics(train)
    te_m = compute_metrics(test)

    return {
        "n": n, "pf_r": m["pf_r"], "exp_r": m["exp_r"],
        "total_r": m["total_r"], "max_dd_r": m["max_dd_r"],
        "stop_rate": m["stop_rate"],
        "quick_stop_rate": quick_stopped / n * 100,
        "ex_best_day_total_r": ex_day_m["total_r"],
        "ex_best_day_pf": ex_day_m["pf_r"],
        "ex_top_sym_total_r": ex_sym_m["total_r"],
        "ex_top_sym_pf": ex_sym_m["pf_r"],
        "best_day": str(best_day), "best_day_r": daily_r.get(best_day, 0),
        "top_sym": top_sym, "top_sym_r": sym_r.get(top_sym, 0),
        "train_pf": tr_m["pf_r"], "test_pf": te_m["pf_r"],
        "train_exp": tr_m["exp_r"], "test_exp": te_m["exp_r"],
        "train_n": tr_m["n"], "test_n": te_m["n"],
    }


def setup_breakdown(trades: List[RTrade]) -> dict:
    by_setup = defaultdict(list)
    for t in trades:
        by_setup[t.setup_name].append(t)
    result = {}
    for sn in sorted(by_setup):
        st = by_setup[sn]
        sm = compute_metrics(st)
        result[sn] = {
            "n": sm["n"], "pf_r": sm["pf_r"], "exp_r": sm["exp_r"],
            "total_r": sm["total_r"], "stop_rate": sm["stop_rate"],
            "direction": "LONG" if st[0].direction == 1 else "SHORT",
        }
    return result


# ════════════════════════════════════════════════════════════════
#  Printing
# ════════════════════════════════════════════════════════════════

def print_detail(name: str, m: dict, bd: dict):
    stable = m.get('train_pf', 0) >= 1.0 and m.get('test_pf', 0) >= 1.0
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
    print(f"  │ STABLE:     {'YES' if stable else 'NO'}")
    print(f"  └──")
    if bd:
        print(f"  Setup breakdown:")
        for sn, sm in bd.items():
            print(f"    {sn:18s} {sm['direction']:5s}  N={sm['n']:4d}  PF={pf_str(sm['pf_r']):>6s}  "
                  f"Exp={sm['exp_r']:+.3f}R  TotalR={sm['total_r']:+.2f}R  Stop={sm['stop_rate']:.1f}%")


def print_comparison(results: dict):
    hdr = (f"  {'Portfolio':24s} {'N':>5s} {'PF(R)':>6s} {'Exp(R)':>8s} {'TotalR':>8s} "
           f"{'MaxDD':>7s} {'Stop%':>6s} {'QStop%':>6s} "
           f"{'ExDay':>8s} {'ExDayPF':>7s} {'ExSym':>8s} {'ExSymPF':>7s} "
           f"{'TrnPF':>6s} {'TstPF':>6s} {'TrnExp':>8s} {'TstExp':>8s} {'Stbl':>4s}")
    sep = "  " + "-" * (len(hdr) - 2)
    print(hdr)
    print(sep)
    for name, m in results.items():
        stable = m.get('train_pf', 0) >= 1.0 and m.get('test_pf', 0) >= 1.0
        print(f"  {name:24s} {m['n']:5d} {pf_str(m['pf_r']):>6s} {m['exp_r']:+7.3f}R "
              f"{m['total_r']:+7.2f}R {m['max_dd_r']:6.2f}R {m['stop_rate']:5.1f}% {m['quick_stop_rate']:5.1f}% "
              f"{m['ex_best_day_total_r']:+7.2f}R {pf_str(m['ex_best_day_pf']):>7s} "
              f"{m['ex_top_sym_total_r']:+7.2f}R {pf_str(m['ex_top_sym_pf']):>7s} "
              f"{pf_str(m['train_pf']):>6s} {pf_str(m['test_pf']):>6s} "
              f"{m['train_exp']:+7.3f}R {m['test_exp']:+7.3f}R "
              f"{'YES' if stable else ' NO':>4s}")


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    symbols = get_universe()
    spy_bars = _load_bars("SPY")
    qqq_bars = _load_bars("QQQ")
    sector_bars_dict = get_sector_bars_dict()
    spy_day_info = classify_spy_days(spy_bars)

    print("=" * 140)
    print("VR DAY-FILTER VARIANT STUDY — R-only")
    print("=" * 140)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Data: {spy_bars[0].timestamp.date()} → {spy_bars[-1].timestamp.date()}")
    print(f"Long filter (harness): non-RED, <15:30")
    print(f"Short filter: BDR_SHORT in-engine (RED+TREND, AM, big wick)")
    print(f"VR day-filter: ENFORCED IN-ENGINE via vr_day_filter param")

    all_metrics = {}
    all_breakdowns = {}
    all_trades = {}

    for pname, cfg_fn in PORTFOLIOS.items():
        print(f"\n{'─'*140}")
        print(f"Running: {pname}")
        print(f"{'─'*140}")

        cfg = cfg_fn()
        trades = run_portfolio(cfg, symbols, spy_bars, qqq_bars,
                               sector_bars_dict, spy_day_info)
        all_trades[pname] = trades

        m = full_metrics(trades)
        all_metrics[pname] = m

        bd = setup_breakdown(trades)
        all_breakdowns[pname] = bd

        print_detail(pname, m, bd)

    # ── Long-side isolation ──
    print(f"\n{'='*140}")
    print("LONG-SIDE ISOLATION (VR trades only, excluding BDR short)")
    print(f"{'='*140}")
    vr_only_metrics = {}
    for pname in ["VR_GREEN_ONLY", "VR_NON_RED", "VR_MARKET_ALIGN_ONLY"]:
        vr_trades = [t for t in all_trades[pname] if t.setup_name == "VWAP RECLAIM"]
        vm = full_metrics(vr_trades)
        vr_only_metrics[pname] = vm
        print(f"\n  {pname} — VR longs only:")
        print(f"    N={vm['n']:4d}  PF={pf_str(vm['pf_r']):>6s}  Exp={vm['exp_r']:+.3f}R  "
              f"TotalR={vm['total_r']:+.2f}R  MaxDD={vm['max_dd_r']:.2f}R  "
              f"Stop={vm['stop_rate']:.1f}%  QStop={vm['quick_stop_rate']:.1f}%")
        stable = vm['train_pf'] >= 1.0 and vm['test_pf'] >= 1.0
        print(f"    Train: PF={pf_str(vm['train_pf']):>6s} Exp={vm['train_exp']:+.3f}R  "
              f"Test: PF={pf_str(vm['test_pf']):>6s} Exp={vm['test_exp']:+.3f}R  STABLE={'YES' if stable else 'NO'}")
        print(f"    ExBestDay={vm['ex_best_day_total_r']:+.2f}R  ExTopSym={vm['ex_top_sym_total_r']:+.2f}R")

    # ── Head-to-head comparison ──
    print(f"\n{'='*140}")
    print("HEAD-TO-HEAD COMPARISON TABLE (full portfolio: VR long + BDR short)")
    print(f"{'='*140}")
    print_comparison(all_metrics)

    # ── Delta vs FROZEN_CURRENT ──
    print(f"\n{'='*140}")
    print("DELTA vs FROZEN_CURRENT")
    print(f"{'='*140}")
    base = all_metrics["FROZEN_CURRENT"]
    for pname, m in all_metrics.items():
        if pname == "FROZEN_CURRENT":
            continue
        print(f"\n  {pname}:")
        print(f"    ΔN:       {m['n'] - base['n']:+d}")
        if base['pf_r'] < 900 and m['pf_r'] < 900:
            print(f"    ΔPF(R):   {m['pf_r'] - base['pf_r']:+.3f}")
        print(f"    ΔExp(R):  {m['exp_r'] - base['exp_r']:+.4f}R")
        print(f"    ΔTotalR:  {m['total_r'] - base['total_r']:+.2f}R")
        print(f"    ΔMaxDD:   {m['max_dd_r'] - base['max_dd_r']:+.2f}R")

    # ── Delta: GREEN_ONLY vs NON_RED (VR trades only) ──
    print(f"\n{'='*140}")
    print("GREEN_ONLY vs NON_RED — VR long trades only")
    print(f"{'='*140}")
    g = vr_only_metrics["VR_GREEN_ONLY"]
    nr = vr_only_metrics["VR_NON_RED"]
    print(f"  GREEN_ONLY:  N={g['n']:4d}  PF={pf_str(g['pf_r']):>6s}  Exp={g['exp_r']:+.3f}R  TotalR={g['total_r']:+.2f}R")
    print(f"  NON_RED:     N={nr['n']:4d}  PF={pf_str(nr['pf_r']):>6s}  Exp={nr['exp_r']:+.3f}R  TotalR={nr['total_r']:+.2f}R")
    extra_n = nr['n'] - g['n']
    extra_r = nr['total_r'] - g['total_r']
    print(f"  FLAT-day marginal trades: {extra_n}  contributing {extra_r:+.2f}R")
    if extra_n > 0:
        print(f"  FLAT-day marginal exp: {extra_r/extra_n:+.3f}R per trade")

    # ── Save JSON ──
    output = {
        "portfolio_metrics": {k: {kk: round(vv, 4) if isinstance(vv, float) else vv
                                   for kk, vv in v.items()}
                              for k, v in all_metrics.items()},
        "vr_only_metrics": {k: {kk: round(vv, 4) if isinstance(vv, float) else vv
                                 for kk, vv in v.items()}
                            for k, v in vr_only_metrics.items()},
        "breakdowns": all_breakdowns,
    }
    out_path = Path(__file__).parent / "vr_dayfilter_results.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved: {out_path}")

    print(f"\n{'='*140}")
    print("STUDY COMPLETE")
    print(f"{'='*140}")


if __name__ == "__main__":
    main()
