"""
SC Repair Study — Can SECOND_CHANCE become positive in R?

Tests causal mechanisms, not threshold sweeps:
  A. EXIT MODE: How does SC exit affect payout distribution?
     - A1: hybrid (current) = stop | 20-bar time | EMA9 trail
     - A2: time-only = stop | time stop (no EMA9 trail)
     - A3: time-only shorter = stop | 8-bar time stop
     - A4: time-only 12-bar = stop | 12-bar time stop
  B. STOP MECHANICS: Is the retest-low stop too tight?
     - B1: current ($0.02 buffer)
     - B2: wider buffer ($0.10)
     - B3: wider buffer ($0.25)
     - B4: deeper retest allowed (0.50 ATR instead of 0.35)
  C. ALIGNMENT: Does requiring market alignment improve quality?
     - C1: current (hostile block only — soft gate)
     - C2: require market align = True (hard gate)
     - C3: raise pre-bonus quality floor from 3 to 4
     - C4: raise min_quality from 4 to 5
  D. BEST COMBO: Combine top findings from A/B/C

All metrics in R. Train/test stability required.

Usage:
    python -m alert_overlay.sc_repair_study
"""

import copy
from collections import defaultdict
from pathlib import Path
from typing import List

from .experiment_harness import (
    _load_bars, get_universe, get_sector_bars_dict, classify_spy_days,
    run_experiment, evaluate, print_evaluation, compute_metrics,
    split_train_test, robustness_check, pf_str, RTrade, OverlayConfig
)

DATA_DIR = Path(__file__).parent / "data"


def make_base_cfg():
    cfg = OverlayConfig()
    cfg.show_ema_scalp = False
    cfg.show_failed_bounce = False
    cfg.show_spencer = False
    cfg.show_ema_fpip = False
    cfg.show_sc_v2 = False
    return cfg


def run_sc(cfg, symbols, spy_bars, qqq_bars, sector_bars_dict, spy_day_info):
    return run_experiment(cfg, symbols, spy_bars, qqq_bars, sector_bars_dict,
                          spy_day_info, long_filter="locked",
                          setup_filter={"2ND CHANCE"})


def exit_reason_dist(trades: List[RTrade]) -> dict:
    """Distribution of exit reasons."""
    counts = defaultdict(int)
    r_by_exit = defaultdict(float)
    for t in trades:
        counts[t.exit_reason] += 1
        r_by_exit[t.exit_reason] += t.pnl_rr
    n = len(trades) or 1
    return {reason: {"count": c, "pct": round(c/n*100, 1),
                     "total_r": round(r_by_exit[reason], 2),
                     "avg_r": round(r_by_exit[reason]/c, 3) if c > 0 else 0}
            for reason, c in sorted(counts.items())}


def print_exit_dist(trades):
    dist = exit_reason_dist(trades)
    print(f"      {'Exit':12s}  {'N':>4s}  {'%':>5s}  {'TotalR':>7s}  {'AvgR':>7s}")
    for reason, d in dist.items():
        print(f"      {reason:12s}  {d['count']:4d}  {d['pct']:5.1f}  {d['total_r']:+6.2f}R  {d['avg_r']:+6.3f}R")


def print_compact(label, ev):
    m = ev["full"]
    tr = ev["train"]
    te = ev["test"]
    rob = ev["robustness"]
    s = "YES" if ev["stable"] else "NO "
    print(f"    {label:42s}  N={m['n']:3d}  PF(R)={pf_str(m['pf_r']):>6s}  "
          f"Exp={m['exp_r']:+.3f}R  TotR={m['total_r']:+7.2f}R  "
          f"Stop={m['stop_rate']:4.1f}%  "
          f"TrnPF={pf_str(tr['pf_r']):>5s}  TstPF={pf_str(te['pf_r']):>5s}  "
          f"Stab={s}  ExDay={rob['ex_best_day']:+.1f}R")


def main():
    symbols = get_universe()
    spy_bars = _load_bars("SPY")
    qqq_bars = _load_bars("QQQ")
    sector_bars_dict = get_sector_bars_dict()
    spy_day_info = classify_spy_days(spy_bars)

    print("=" * 130)
    print("SC REPAIR STUDY — CAN SECOND_CHANCE BECOME POSITIVE IN R?")
    print("=" * 130)
    print(f"Universe: {len(symbols)} symbols | {spy_bars[0].timestamp.date()} → {spy_bars[-1].timestamp.date()}")
    print(f"Filters: Non-RED, Q>=gate, <15:30, longs only")

    base_cfg = make_base_cfg()

    # ═══════════════════════════════════════════════
    #  SECTION A: EXIT MODE
    # ═══════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("SECTION A: EXIT MODE — How exit mechanics affect SC payout")
    print("=" * 130)

    exit_variants = [
        ("A0: hybrid/20 (current)",      "hybrid",    20),
        ("A1: time-only/20",             "time",      20),
        ("A2: time-only/16",             "time",      16),
        ("A3: time-only/12",             "time",      12),
        ("A4: time-only/8",              "time",       8),
        ("A5: ema9_trail only",          "ema9_trail", 20),
    ]

    a_results = {}
    for label, mode, bars in exit_variants:
        cfg = copy.deepcopy(base_cfg)
        cfg.breakout_exit_mode = mode
        cfg.breakout_time_stop_bars = bars
        trades = run_sc(cfg, symbols, spy_bars, qqq_bars, sector_bars_dict, spy_day_info)
        ev = evaluate(label, trades, min_n=15)
        a_results[label] = (ev, trades)
        print_compact(label, ev)

    # Show exit distribution for current and best
    print("\n    Exit distribution — A0 (current):")
    print_exit_dist(a_results["A0: hybrid/20 (current)"][1])
    print("\n    Exit distribution — A1 (time-only/20):")
    print_exit_dist(a_results["A1: time-only/20"][1])
    print("\n    Exit distribution — A4 (time-only/8):")
    print_exit_dist(a_results["A4: time-only/8"][1])

    # ═══════════════════════════════════════════════
    #  SECTION B: STOP MECHANICS
    # ═══════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("SECTION B: STOP MECHANICS — Is the retest-low stop too tight?")
    print("=" * 130)

    stop_variants = [
        ("B0: buffer=$0.02 (current)",   0.02, 0.35),
        ("B1: buffer=$0.10",             0.10, 0.35),
        ("B2: buffer=$0.25",             0.25, 0.35),
        ("B3: buffer=$0.50",             0.50, 0.35),
        ("B4: deeper retest (0.50 ATR)", 0.02, 0.50),
        ("B5: buffer=$0.10 + deep 0.50", 0.10, 0.50),
    ]

    b_results = {}
    for label, buffer, depth in stop_variants:
        cfg = copy.deepcopy(base_cfg)
        cfg.sc_stop_buffer = buffer
        cfg.sc_retest_max_depth_atr = depth
        trades = run_sc(cfg, symbols, spy_bars, qqq_bars, sector_bars_dict, spy_day_info)
        ev = evaluate(label, trades, min_n=15)
        b_results[label] = ev
        print_compact(label, ev)

    # ═══════════════════════════════════════════════
    #  SECTION C: ALIGNMENT / QUALITY GATES
    # ═══════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("SECTION C: ALIGNMENT — Does stricter context gating help?")
    print("=" * 130)

    align_variants = [
        ("C0: hostile block only (current)", False, True,  3, 4),
        ("C1: require market align",         True,  True,  3, 4),
        ("C2: pre-bonus floor = 4",          False, True,  4, 4),
        ("C3: min_quality = 5",              False, True,  3, 5),
        ("C4: market align + min_q=5",       True,  True,  3, 5),
        ("C5: pre-bonus=4 + min_q=5",        False, True,  4, 5),
    ]

    c_results = {}
    for label, mkt_align, hostile, pre_bonus, min_q in align_variants:
        cfg = copy.deepcopy(base_cfg)
        cfg.sc_require_market_align = mkt_align
        cfg.sc_hostile_tape_block = hostile
        cfg.sc_pre_bonus_min_quality = pre_bonus
        cfg.min_quality = min_q
        trades = run_sc(cfg, symbols, spy_bars, qqq_bars, sector_bars_dict, spy_day_info)
        ev = evaluate(label, trades, min_n=10)
        c_results[label] = ev
        print_compact(label, ev)

    # ═══════════════════════════════════════════════
    #  SECTION D: BEST COMBINATIONS
    # ═══════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("SECTION D: COMBINATIONS — Best exit + stop + alignment")
    print("=" * 130)

    combos = [
        ("D1: time/8 + buf=$0.10",
         {"breakout_exit_mode": "time", "breakout_time_stop_bars": 8,
          "sc_stop_buffer": 0.10}),
        ("D2: time/8 + buf=$0.10 + min_q=5",
         {"breakout_exit_mode": "time", "breakout_time_stop_bars": 8,
          "sc_stop_buffer": 0.10, "min_quality": 5}),
        ("D3: time/12 + buf=$0.10",
         {"breakout_exit_mode": "time", "breakout_time_stop_bars": 12,
          "sc_stop_buffer": 0.10}),
        ("D4: time/12 + buf=$0.10 + mkt_align",
         {"breakout_exit_mode": "time", "breakout_time_stop_bars": 12,
          "sc_stop_buffer": 0.10, "sc_require_market_align": True}),
        ("D5: time/8 + buf=$0.25 + min_q=5",
         {"breakout_exit_mode": "time", "breakout_time_stop_bars": 8,
          "sc_stop_buffer": 0.25, "min_quality": 5}),
        ("D6: time/20 + buf=$0.10 + mkt_align",
         {"breakout_exit_mode": "time", "breakout_time_stop_bars": 20,
          "sc_stop_buffer": 0.10, "sc_require_market_align": True}),
        ("D7: hybrid + buf=$0.10 + min_q=5",
         {"sc_stop_buffer": 0.10, "min_quality": 5}),
        ("D8: time/8 + deep 0.50 + buf=$0.10 + min_q=5",
         {"breakout_exit_mode": "time", "breakout_time_stop_bars": 8,
          "sc_stop_buffer": 0.10, "sc_retest_max_depth_atr": 0.50,
          "min_quality": 5}),
    ]

    d_results = {}
    for label, overrides in combos:
        cfg = copy.deepcopy(base_cfg)
        for k, v in overrides.items():
            setattr(cfg, k, v)
        trades = run_sc(cfg, symbols, spy_bars, qqq_bars, sector_bars_dict, spy_day_info)
        ev = evaluate(label, trades, min_n=10)
        d_results[label] = (ev, trades)
        print_compact(label, ev)

    # Show exit dist for best combos
    for label in ["D1: time/8 + buf=$0.10", "D2: time/8 + buf=$0.10 + min_q=5"]:
        if label in d_results:
            print(f"\n    Exit distribution — {label}:")
            print_exit_dist(d_results[label][1])

    # ═══════════════════════════════════════════════
    #  VERDICT
    # ═══════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("VERDICT")
    print("=" * 130)

    # Find best by total_r among those with sufficient N
    all_results = {}
    all_results.update({k: v for k, v in a_results.items()})
    all_results.update({k: (v, None) for k, v in b_results.items()})
    all_results.update({k: (v, None) for k, v in c_results.items()})
    all_results.update(d_results)

    positive = []
    for label, item in all_results.items():
        ev = item[0] if isinstance(item, tuple) else item
        if ev["full"]["total_r"] > 0 and ev["full"]["n"] >= 10:
            positive.append((label, ev))

    if positive:
        positive.sort(key=lambda x: x[1]["full"]["total_r"], reverse=True)
        print("\n  Variants with positive R (sorted by TotalR):")
        for label, ev in positive:
            m = ev["full"]
            s = "STABLE" if ev["stable"] else "UNSTABLE"
            print(f"    {label:42s}  N={m['n']:3d}  Exp={m['exp_r']:+.3f}R  "
                  f"TotR={m['total_r']:+.2f}R  {s}")

        best_label, best_ev = positive[0]
        if best_ev["stable"] and best_ev["full"]["exp_r"] > 0.05:
            print(f"\n  PROMOTE: {best_label} — positive R, stable, meaningful exp.")
        elif best_ev["full"]["exp_r"] > 0:
            print(f"\n  REFINE: {best_label} — positive R but needs more validation.")
        else:
            print(f"\n  WEAK: Best variant has low per-trade edge.")
    else:
        print("\n  REJECT: No SC variant achieves positive R with sufficient sample.")
        print("  SC's breakout-retest-confirm mechanism does not produce edge on this data.")
        print("  Recommendation: Disable SC (show_second_chance = False).")

    print("\n" + "=" * 130)
    print("SC REPAIR STUDY COMPLETE")
    print("=" * 130)


if __name__ == "__main__":
    main()
