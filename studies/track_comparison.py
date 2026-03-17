"""
3-Track Comparison Runner — R-only

Compares all three portfolio tracks head-to-head:
  Track 1: frozen_v1_vk_bdr       (archived baseline)
  Track 2: candidate_v2_vrgreen_bdr (active paper candidate)
  Track 3: discovery_shadow        (active + shadow setups)

Discovery track separates:
  - ACTIVE signals (VWAP RECLAIM, BDR SHORT) — tradeable
  - SHADOW signals (VK, SC, Spencer, etc.) — log-only research

Single source of truth: portfolio_configs.py

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.track_comparison
"""

import json
from collections import defaultdict
from pathlib import Path
from typing import List

from ..backtest import run_backtest
from ..config import OverlayConfig
from .experiment_harness import (
    _load_bars, get_universe, get_sector_bars_dict,
    classify_spy_days, RTrade, compute_metrics, split_train_test, pf_str,
)
from ..market_context import get_sector_etf
from .portfolio_configs import (
    CONFIGS, ACTIVE_SETUPS_V2, SHADOW_SETUPS,
    frozen_v1_vk_bdr, candidate_v2_vrgreen_bdr, discovery_shadow,
)


# ════════════════════════════════════════════════════════════════
#  Run engine with portfolio-level filters
# ════════════════════════════════════════════════════════════════

def run_track(cfg: OverlayConfig, symbols, spy_bars, qqq_bars,
              sector_bars_dict, spy_day_info) -> List[RTrade]:
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
#  Extended metrics
# ════════════════════════════════════════════════════════════════

def full_metrics(trades: List[RTrade]) -> dict:
    m = compute_metrics(trades)
    n = m["n"]
    if n == 0:
        return {k: 0 for k in [
            "n", "pf_r", "exp_r", "total_r", "max_dd_r", "stop_rate",
            "quick_stop_rate", "ex_best_day_r", "ex_best_day_pf",
            "ex_top_sym_r", "ex_top_sym_pf", "train_pf", "test_pf",
            "train_exp", "test_exp", "train_n", "test_n",
            "best_day", "best_day_r", "top_sym", "top_sym_r",
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
        "ex_best_day_r": ex_day_m["total_r"], "ex_best_day_pf": ex_day_m["pf_r"],
        "ex_top_sym_r": ex_sym_m["total_r"], "ex_top_sym_pf": ex_sym_m["pf_r"],
        "best_day": str(best_day), "best_day_r": daily_r.get(best_day, 0),
        "top_sym": top_sym, "top_sym_r": sym_r.get(top_sym, 0),
        "train_pf": tr_m["pf_r"], "test_pf": te_m["pf_r"],
        "train_exp": tr_m["exp_r"], "test_exp": te_m["exp_r"],
        "train_n": tr_m["n"], "test_n": te_m["n"],
    }


def setup_table(trades: List[RTrade]) -> dict:
    by_setup = defaultdict(list)
    for t in trades:
        by_setup[t.setup_name].append(t)
    result = {}
    for sn in sorted(by_setup):
        st = by_setup[sn]
        sm = compute_metrics(st)
        result[sn] = {"n": sm["n"], "pf_r": sm["pf_r"], "exp_r": sm["exp_r"],
                       "total_r": sm["total_r"], "stop_rate": sm["stop_rate"],
                       "dir": "LONG" if st[0].direction == 1 else "SHORT"}
    return result


# ════════════════════════════════════════════════════════════════
#  Printing
# ════════════════════════════════════════════════════════════════

def print_track(name: str, m: dict, setups: dict, tag_map: dict = None):
    stable = m.get('train_pf', 0) >= 1.0 and m.get('test_pf', 0) >= 1.0
    print(f"\n  ┌── {name} ──")
    print(f"  │ Trades:     {m['n']:5d}")
    print(f"  │ PF(R):      {pf_str(m['pf_r']):>6s}")
    print(f"  │ Exp(R):     {m['exp_r']:+.3f}R")
    print(f"  │ Total R:    {m['total_r']:+.2f}R")
    print(f"  │ Max DD(R):  {m['max_dd_r']:.2f}R")
    print(f"  │ Stop Rate:  {m['stop_rate']:.1f}%")
    print(f"  │ Quick-Stop: {m['quick_stop_rate']:.1f}%")
    print(f"  │ ExBestDay:  {m['ex_best_day_r']:+.2f}R  PF={pf_str(m['ex_best_day_pf'])}"
          f"  (best={m['best_day']} {m['best_day_r']:+.2f}R)")
    print(f"  │ ExTopSym:   {m['ex_top_sym_r']:+.2f}R  PF={pf_str(m['ex_top_sym_pf'])}"
          f"  (top={m['top_sym']} {m['top_sym_r']:+.2f}R)")
    print(f"  │ Train:      N={m['train_n']:4d}  PF={pf_str(m['train_pf']):>6s}  Exp={m['train_exp']:+.3f}R")
    print(f"  │ Test:       N={m['test_n']:4d}  PF={pf_str(m['test_pf']):>6s}  Exp={m['test_exp']:+.3f}R")
    print(f"  │ STABLE:     {'YES' if stable else 'NO'}")
    print(f"  └──")
    if setups:
        print(f"  Setup breakdown:")
        for sn, sm in setups.items():
            tag = ""
            if tag_map:
                tag = f" [{tag_map.get(sn, '?')}]"
            print(f"    {sn:18s} {sm['dir']:5s}  N={sm['n']:4d}  PF={pf_str(sm['pf_r']):>6s}  "
                  f"Exp={sm['exp_r']:+.3f}R  TotalR={sm['total_r']:+.2f}R  Stop={sm['stop_rate']:.1f}%{tag}")


def print_comparison_table(results: dict):
    print(f"\n  {'Track':28s} {'N':>5s} {'PF(R)':>6s} {'Exp(R)':>8s} {'TotalR':>8s} "
          f"{'MaxDD':>7s} {'Stop%':>6s} {'QStop%':>6s} "
          f"{'ExDay':>8s} {'ExSym':>8s} {'TrnPF':>6s} {'TstPF':>6s} {'Stbl':>4s}")
    sep = "  " + "-" * 120
    print(sep)
    for name, m in results.items():
        stable = m.get('train_pf', 0) >= 1.0 and m.get('test_pf', 0) >= 1.0
        print(f"  {name:28s} {m['n']:5d} {pf_str(m['pf_r']):>6s} {m['exp_r']:+7.3f}R "
              f"{m['total_r']:+7.2f}R {m['max_dd_r']:6.2f}R {m['stop_rate']:5.1f}% {m['quick_stop_rate']:5.1f}% "
              f"{m['ex_best_day_r']:+7.2f}R {m['ex_top_sym_r']:+7.2f}R "
              f"{pf_str(m['train_pf']):>6s} {pf_str(m['test_pf']):>6s} "
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
    print("3-TRACK COMPARISON — R-only")
    print("=" * 140)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Data: {spy_bars[0].timestamp.date()} → {spy_bars[-1].timestamp.date()}")
    print(f"Source of truth: alert_overlay/portfolio_configs.py")

    all_metrics = {}
    all_setups = {}

    # ═══════════════════════════════════════════════
    # Track 1: ARCHIVED BASELINE
    # ═══════════════════════════════════════════════
    print(f"\n{'─'*140}")
    print("TRACK 1: ARCHIVED BASELINE — frozen_v1_vk_bdr")
    print(f"{'─'*140}")
    cfg1 = frozen_v1_vk_bdr()
    t1 = run_track(cfg1, symbols, spy_bars, qqq_bars, sector_bars_dict, spy_day_info)
    m1 = full_metrics(t1)
    s1 = setup_table(t1)
    all_metrics["T1_FROZEN_V1"] = m1
    all_setups["T1_FROZEN_V1"] = s1
    print_track("T1: frozen_v1_vk_bdr", m1, s1)

    # ═══════════════════════════════════════════════
    # Track 2: ACTIVE PAPER CANDIDATE
    # ═══════════════════════════════════════════════
    print(f"\n{'─'*140}")
    print("TRACK 2: ACTIVE PAPER CANDIDATE — candidate_v2_vrgreen_bdr")
    print(f"{'─'*140}")
    cfg2 = candidate_v2_vrgreen_bdr()
    t2 = run_track(cfg2, symbols, spy_bars, qqq_bars, sector_bars_dict, spy_day_info)
    m2 = full_metrics(t2)
    s2 = setup_table(t2)
    all_metrics["T2_CANDIDATE_V2"] = m2
    all_setups["T2_CANDIDATE_V2"] = s2
    print_track("T2: candidate_v2_vrgreen_bdr", m2, s2)

    # ═══════════════════════════════════════════════
    # Track 3: DISCOVERY / SHADOW
    # ═══════════════════════════════════════════════
    print(f"\n{'─'*140}")
    print("TRACK 3: DISCOVERY / SHADOW — all setups enabled")
    print(f"{'─'*140}")
    cfg3 = discovery_shadow()
    t3 = run_track(cfg3, symbols, spy_bars, qqq_bars, sector_bars_dict, spy_day_info)

    # Tag each setup as ACTIVE or SHADOW
    tag_map = {}
    for sn in set(t.setup_name for t in t3):
        if sn in ACTIVE_SETUPS_V2:
            tag_map[sn] = "ACTIVE"
        elif sn in SHADOW_SETUPS:
            tag_map[sn] = "SHADOW"
        else:
            tag_map[sn] = "UNKNOWN"

    # Full discovery metrics (all signals)
    m3_all = full_metrics(t3)
    s3_all = setup_table(t3)
    print_track("T3: discovery (ALL signals)", m3_all, s3_all, tag_map)

    # Active-only subset from discovery
    t3_active = [t for t in t3 if t.setup_name in ACTIVE_SETUPS_V2]
    m3_active = full_metrics(t3_active)
    s3_active = setup_table(t3_active)
    all_metrics["T3_DISC_ACTIVE"] = m3_active

    # Shadow-only subset
    t3_shadow = [t for t in t3 if t.setup_name in SHADOW_SETUPS]
    m3_shadow = full_metrics(t3_shadow)
    s3_shadow = setup_table(t3_shadow)
    all_metrics["T3_DISC_SHADOW"] = m3_shadow

    print(f"\n  ── Discovery: ACTIVE signals only ──")
    print_track("T3: discovery (ACTIVE only)", m3_active, s3_active)

    print(f"\n  ── Discovery: SHADOW signals only ──")
    print_track("T3: discovery (SHADOW only)", m3_shadow, s3_shadow, tag_map)

    # ═══════════════════════════════════════════════
    # Integrity check: T2 active == T3 active?
    # ═══════════════════════════════════════════════
    print(f"\n{'─'*140}")
    print("INTEGRITY CHECK: T2 candidate vs T3 active-only")
    print(f"{'─'*140}")
    t2_n = m2["n"]
    t3a_n = m3_active["n"]
    t2_r = m2["total_r"]
    t3a_r = m3_active["total_r"]
    match_n = t2_n == t3a_n
    match_r = abs(t2_r - t3a_r) < 0.5  # allow small float tolerance
    print(f"  T2 trades: {t2_n}   T3-active trades: {t3a_n}   Match: {'YES' if match_n else 'NO ← INVESTIGATE'}")
    print(f"  T2 totalR: {t2_r:+.2f}   T3-active totalR: {t3a_r:+.2f}   Match: {'YES' if match_r else 'NO ← INVESTIGATE'}")
    if not match_n:
        # VR might have slightly different trade count when other setups compete for cooldown
        t2_vr = [t for t in t2 if t.setup_name == "VWAP RECLAIM"]
        t3_vr = [t for t in t3_active if t.setup_name == "VWAP RECLAIM"]
        print(f"  T2 VR: {len(t2_vr)}   T3 VR: {len(t3_vr)}")
        t2_bdr = [t for t in t2 if t.setup_name == "BDR SHORT"]
        t3_bdr = [t for t in t3_active if t.setup_name == "BDR SHORT"]
        print(f"  T2 BDR: {len(t2_bdr)}   T3 BDR: {len(t3_bdr)}")
        print(f"  NOTE: Discovery config has shadow setups that may affect cooldown/state.")

    # ═══════════════════════════════════════════════
    # Head-to-head comparison table
    # ═══════════════════════════════════════════════
    print(f"\n{'='*140}")
    print("HEAD-TO-HEAD COMPARISON")
    print(f"{'='*140}")
    compare = {
        "T1_FROZEN_V1": m1,
        "T2_CANDIDATE_V2": m2,
        "T3_DISC_ACTIVE": m3_active,
        "T3_DISC_SHADOW": m3_shadow,
    }
    print_comparison_table(compare)

    # ═══════════════════════════════════════════════
    # Delta: T2 vs T1
    # ═══════════════════════════════════════════════
    print(f"\n{'='*140}")
    print("DELTA: T2 (candidate) vs T1 (baseline)")
    print(f"{'='*140}")
    print(f"  ΔN:       {m2['n'] - m1['n']:+d}")
    if m1['pf_r'] < 900 and m2['pf_r'] < 900:
        print(f"  ΔPF(R):   {m2['pf_r'] - m1['pf_r']:+.3f}")
    print(f"  ΔExp(R):  {m2['exp_r'] - m1['exp_r']:+.4f}R")
    print(f"  ΔTotalR:  {m2['total_r'] - m1['total_r']:+.2f}R")
    print(f"  ΔMaxDD:   {m2['max_dd_r'] - m1['max_dd_r']:+.2f}R")

    # ═══════════════════════════════════════════════
    # Shadow setup watch list
    # ═══════════════════════════════════════════════
    print(f"\n{'='*140}")
    print("SHADOW SETUP WATCH LIST (not tradeable, monitor for future promotion)")
    print(f"{'='*140}")
    for sn, sm in s3_all.items():
        if sn in SHADOW_SETUPS:
            verdict = "WATCH" if sm["total_r"] > 0 and sm["pf_r"] > 1.0 else "DORMANT"
            print(f"  {sn:18s}  N={sm['n']:4d}  PF={pf_str(sm['pf_r']):>6s}  "
                  f"Exp={sm['exp_r']:+.3f}R  TotalR={sm['total_r']:+.2f}R  → {verdict}")

    # ═══════════════════════════════════════════════
    # Save JSON
    # ═══════════════════════════════════════════════
    output = {
        "tracks": {k: {kk: round(vv, 4) if isinstance(vv, float) else vv
                        for kk, vv in v.items()}
                   for k, v in all_metrics.items()},
        "setups": all_setups,
        "shadow_setups": {sn: sm for sn, sm in s3_all.items() if sn in SHADOW_SETUPS},
        "integrity": {"t2_n": t2_n, "t3a_n": t3a_n, "match_n": match_n,
                       "t2_r": round(t2_r, 2), "t3a_r": round(t3a_r, 2), "match_r": match_r},
    }
    out_path = Path(__file__).parent.parent / "track_comparison_results.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved: {out_path}")

    print(f"\n{'='*140}")
    print("3-TRACK COMPARISON COMPLETE")
    print(f"{'='*140}")


if __name__ == "__main__":
    main()
