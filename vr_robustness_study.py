"""
VR_GREEN_ONLY + BDR_SHORT — Hard Robustness / Risk-Shape Validation

Candidate portfolio for new frozen default:
  Long:  VWAP_RECLAIM, vr_day_filter="green_only"
  Short: BDR_SHORT (frozen: RED+TREND, AM, big wick)

Reports (all R-only):
  1. Core metrics: PF, Exp, TotalR, MaxDD, DD duration, stop rate, quick-stop
  2. Robustness exclusions: ex-best-day, ex-top-sym, ex-top-3-days, ex-top-3-syms
  3. Rolling walk-forward train/test slices (weekly windows)
  4. Weekly and monthly contribution consistency
  5. Longest losing streak
  6. Contribution by symbol, day-of-week, hour bucket

Does NOT modify OverlayConfig defaults.

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.vr_robustness_study
"""

import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple
from datetime import timedelta

from .backtest import run_backtest
from .config import OverlayConfig
from .experiment_harness import (
    _load_bars, get_universe, get_sector_bars_dict,
    classify_spy_days, RTrade, compute_metrics, split_train_test, pf_str,
)
from .market_context import get_sector_etf


# ════════════════════════════════════════════════════════════════
#  Explicit candidate config
# ════════════════════════════════════════════════════════════════

def cfg_candidate() -> OverlayConfig:
    """VR_GREEN_ONLY + BDR_SHORT — candidate frozen portfolio."""
    cfg = OverlayConfig()
    # Disable everything
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
    cfg.show_failed_bounce = False
    # BDR_SHORT frozen
    cfg.show_breakdown_retest = True
    cfg.bdr_require_red_trend = True
    cfg.bdr_am_only = True
    cfg.bdr_am_cutoff = 1100
    cfg.bdr_min_rejection_wick_pct = 0.30
    # VR GREEN_ONLY
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
    cfg.vr_day_filter = "green_only"
    cfg.vr_require_market_align = True
    cfg.vr_require_sector_align = False
    return cfg


# ════════════════════════════════════════════════════════════════
#  Run engine
# ════════════════════════════════════════════════════════════════

def run_portfolio(cfg, symbols, spy_bars, qqq_bars, sector_bars_dict, spy_day_info):
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
#  Analysis functions
# ════════════════════════════════════════════════════════════════

def drawdown_analysis(trades: List[RTrade]) -> dict:
    """Max DD, DD duration, equity curve stats."""
    if not trades:
        return {"max_dd_r": 0, "dd_duration_bars": 0, "dd_start": None, "dd_end": None}

    sorted_t = sorted(trades, key=lambda t: t.entry_time)
    cum = pk = dd = 0.0
    dd_start_idx = 0
    max_dd_start = max_dd_end = 0
    # Track drawdown duration in trade-count
    in_dd_since = None
    max_dd_dur = 0
    dd_dur_start = None
    dd_dur_end = None

    for i, t in enumerate(sorted_t):
        cum += t.pnl_rr
        if cum > pk:
            pk = cum
            if in_dd_since is not None:
                dur = i - in_dd_since
                if dur > max_dd_dur:
                    max_dd_dur = dur
                    dd_dur_start = sorted_t[in_dd_since].entry_time
                    dd_dur_end = t.entry_time
                in_dd_since = None
        else:
            if in_dd_since is None:
                in_dd_since = i
        if pk - cum > dd:
            dd = pk - cum
            max_dd_start = dd_start_idx
            max_dd_end = i

    # Close any open DD
    if in_dd_since is not None:
        dur = len(sorted_t) - in_dd_since
        if dur > max_dd_dur:
            max_dd_dur = dur
            dd_dur_start = sorted_t[in_dd_since].entry_time
            dd_dur_end = sorted_t[-1].entry_time

    return {
        "max_dd_r": dd,
        "dd_duration_trades": max_dd_dur,
        "dd_dur_start": str(dd_dur_start) if dd_dur_start else None,
        "dd_dur_end": str(dd_dur_end) if dd_dur_end else None,
    }


def exclusion_robustness(trades: List[RTrade]) -> dict:
    """Ex-best-day, ex-top-sym, ex-top-3-days, ex-top-3-syms."""
    daily_r = defaultdict(float)
    sym_r = defaultdict(float)
    for t in trades:
        if t.entry_date:
            daily_r[t.entry_date] += t.pnl_rr
        sym_r[t.symbol] += t.pnl_rr

    top_days = sorted(daily_r.items(), key=lambda x: x[1], reverse=True)
    top_syms = sorted(sym_r.items(), key=lambda x: x[1], reverse=True)

    def metrics_excluding_days(days_to_exclude):
        ex_set = set(days_to_exclude)
        ex = [t for t in trades if t.entry_date not in ex_set]
        return compute_metrics(ex)

    def metrics_excluding_syms(syms_to_exclude):
        ex_set = set(syms_to_exclude)
        ex = [t for t in trades if t.symbol not in ex_set]
        return compute_metrics(ex)

    best_day = top_days[0] if top_days else (None, 0)
    top_sym = top_syms[0] if top_syms else (None, 0)
    top3_days = [d for d, _ in top_days[:3]]
    top3_syms = [s for s, _ in top_syms[:3]]

    ex1d = metrics_excluding_days([best_day[0]]) if best_day[0] else compute_metrics(trades)
    ex3d = metrics_excluding_days(top3_days)
    ex1s = metrics_excluding_syms([top_sym[0]]) if top_sym[0] else compute_metrics(trades)
    ex3s = metrics_excluding_syms(top3_syms)

    return {
        "best_day": str(best_day[0]), "best_day_r": best_day[1],
        "ex_best_day_total_r": ex1d["total_r"], "ex_best_day_pf": ex1d["pf_r"],
        "top_3_days": [(str(d), round(r, 2)) for d, r in top_days[:3]],
        "ex_top_3_days_total_r": ex3d["total_r"], "ex_top_3_days_pf": ex3d["pf_r"],
        "top_sym": top_sym[0], "top_sym_r": top_sym[1],
        "ex_top_sym_total_r": ex1s["total_r"], "ex_top_sym_pf": ex1s["pf_r"],
        "top_3_syms": [(s, round(r, 2)) for s, r in top_syms[:3]],
        "ex_top_3_syms_total_r": ex3s["total_r"], "ex_top_3_syms_pf": ex3s["pf_r"],
    }


def walk_forward_slices(trades: List[RTrade], window_days: int = 14) -> list:
    """Rolling walk-forward: train on window, test on next window."""
    if not trades:
        return []
    dates = sorted(set(t.entry_date for t in trades if t.entry_date))
    if len(dates) < window_days * 2:
        return []

    slices = []
    i = 0
    while i + window_days * 2 <= len(dates):
        train_dates = set(dates[i:i + window_days])
        test_dates = set(dates[i + window_days:i + window_days * 2])
        train = [t for t in trades if t.entry_date in train_dates]
        test = [t for t in trades if t.entry_date in test_dates]
        if train and test:
            tr_m = compute_metrics(train)
            te_m = compute_metrics(test)
            slices.append({
                "train_start": str(min(train_dates)),
                "train_end": str(max(train_dates)),
                "test_start": str(min(test_dates)),
                "test_end": str(max(test_dates)),
                "train_n": tr_m["n"], "train_pf": tr_m["pf_r"],
                "train_exp": tr_m["exp_r"], "train_total_r": tr_m["total_r"],
                "test_n": te_m["n"], "test_pf": te_m["pf_r"],
                "test_exp": te_m["exp_r"], "test_total_r": te_m["total_r"],
            })
        i += window_days  # non-overlapping
    return slices


def weekly_monthly_consistency(trades: List[RTrade]) -> Tuple[dict, dict]:
    """Weekly and monthly R contribution."""
    weekly = defaultdict(float)
    weekly_n = defaultdict(int)
    monthly = defaultdict(float)
    monthly_n = defaultdict(int)

    for t in trades:
        if not t.entry_date:
            continue
        # ISO week
        yr, wk, _ = t.entry_date.isocalendar()
        wkey = f"{yr}-W{wk:02d}"
        weekly[wkey] += t.pnl_rr
        weekly_n[wkey] += 1
        # Month
        mkey = f"{t.entry_date.year}-{t.entry_date.month:02d}"
        monthly[mkey] += t.pnl_rr
        monthly_n[mkey] += 1

    w_sorted = sorted(weekly.items())
    m_sorted = sorted(monthly.items())

    w_pos = sum(1 for _, r in w_sorted if r > 0)
    m_pos = sum(1 for _, r in m_sorted if r > 0)

    return (
        {"weeks": [(k, round(r, 2), weekly_n[k]) for k, r in w_sorted],
         "positive_weeks": w_pos, "total_weeks": len(w_sorted),
         "win_rate": w_pos / len(w_sorted) * 100 if w_sorted else 0},
        {"months": [(k, round(r, 2), monthly_n[k]) for k, r in m_sorted],
         "positive_months": m_pos, "total_months": len(m_sorted),
         "win_rate": m_pos / len(m_sorted) * 100 if m_sorted else 0},
    )


def longest_losing_streak(trades: List[RTrade]) -> dict:
    """Longest consecutive losing trades."""
    sorted_t = sorted(trades, key=lambda t: t.entry_time)
    max_streak = cur = 0
    streak_end_idx = 0
    for i, t in enumerate(sorted_t):
        if t.pnl_rr <= 0:
            cur += 1
            if cur > max_streak:
                max_streak = cur
                streak_end_idx = i
        else:
            cur = 0

    streak_r = 0.0
    if max_streak > 0:
        start_idx = streak_end_idx - max_streak + 1
        streak_trades = sorted_t[start_idx:streak_end_idx + 1]
        streak_r = sum(t.pnl_rr for t in streak_trades)
        streak_start = str(streak_trades[0].entry_time)
        streak_end = str(streak_trades[-1].entry_time)
    else:
        streak_start = streak_end = None

    return {
        "longest_losing_streak": max_streak,
        "streak_r": streak_r,
        "streak_start": streak_start,
        "streak_end": streak_end,
    }


def contribution_by_symbol(trades: List[RTrade]) -> list:
    """All symbols sorted by R contribution."""
    sym_r = defaultdict(lambda: {"r": 0.0, "n": 0, "wins": 0})
    for t in trades:
        sym_r[t.symbol]["r"] += t.pnl_rr
        sym_r[t.symbol]["n"] += 1
        if t.pnl_rr > 0:
            sym_r[t.symbol]["wins"] += 1
    return sorted(
        [(s, round(d["r"], 2), d["n"], d["wins"]) for s, d in sym_r.items()],
        key=lambda x: x[1], reverse=True
    )


def contribution_by_dow(trades: List[RTrade]) -> dict:
    """By day of week (0=Mon, 4=Fri)."""
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    dow_r = defaultdict(lambda: {"r": 0.0, "n": 0})
    for t in trades:
        if t.entry_date:
            wd = t.entry_date.weekday()
            dow_r[wd]["r"] += t.pnl_rr
            dow_r[wd]["n"] += 1
    return {dow_names[d]: {"r": round(v["r"], 2), "n": v["n"]}
            for d, v in sorted(dow_r.items())}


def contribution_by_hour(trades: List[RTrade]) -> dict:
    """By entry hour bucket."""
    hour_r = defaultdict(lambda: {"r": 0.0, "n": 0})
    for t in trades:
        h = t.entry_time.hour
        hour_r[h]["r"] += t.pnl_rr
        hour_r[h]["n"] += 1
    return {f"{h:02d}:00": {"r": round(v["r"], 2), "n": v["n"]}
            for h, v in sorted(hour_r.items())}


def contribution_by_setup(trades: List[RTrade]) -> dict:
    by_setup = defaultdict(list)
    for t in trades:
        by_setup[t.setup_name].append(t)
    result = {}
    for sn in sorted(by_setup):
        st = by_setup[sn]
        m = compute_metrics(st)
        result[sn] = {"n": m["n"], "pf_r": m["pf_r"], "exp_r": m["exp_r"],
                       "total_r": m["total_r"], "stop_rate": m["stop_rate"],
                       "dir": "LONG" if st[0].direction == 1 else "SHORT"}
    return result


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    symbols = get_universe()
    spy_bars = _load_bars("SPY")
    qqq_bars = _load_bars("QQQ")
    sector_bars_dict = get_sector_bars_dict()
    spy_day_info = classify_spy_days(spy_bars)

    cfg = cfg_candidate()
    trades = run_portfolio(cfg, symbols, spy_bars, qqq_bars,
                           sector_bars_dict, spy_day_info)
    trades_sorted = sorted(trades, key=lambda t: t.entry_time)

    print("=" * 140)
    print("VR_GREEN_ONLY + BDR_SHORT — HARD ROBUSTNESS / RISK-SHAPE VALIDATION")
    print("=" * 140)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Data: {spy_bars[0].timestamp.date()} → {spy_bars[-1].timestamp.date()}")
    print(f"Config: VR day_filter=green_only, BDR frozen")

    # ── 1. Core Metrics ──
    print(f"\n{'─'*140}")
    print("1. CORE METRICS")
    print(f"{'─'*140}")
    m = compute_metrics(trades)
    quick_stopped = sum(1 for t in trades if t.exit_reason == "stop" and t.bars_held <= 2)
    dd = drawdown_analysis(trades)

    print(f"  Trades:       {m['n']}")
    print(f"  PF(R):        {pf_str(m['pf_r'])}")
    print(f"  Exp(R):       {m['exp_r']:+.3f}R")
    print(f"  Total R:      {m['total_r']:+.2f}R")
    print(f"  Max DD(R):    {dd['max_dd_r']:.2f}R")
    print(f"  DD Duration:  {dd['dd_duration_trades']} trades ({dd['dd_dur_start']} → {dd['dd_dur_end']})")
    print(f"  Stop Rate:    {m['stop_rate']:.1f}%")
    print(f"  Quick-Stop:   {quick_stopped/m['n']*100:.1f}% (stopped ≤2 bars)")

    # Setup breakdown
    sbd = contribution_by_setup(trades)
    print(f"\n  Setup breakdown:")
    for sn, sm in sbd.items():
        print(f"    {sn:18s} {sm['dir']:5s}  N={sm['n']:4d}  PF={pf_str(sm['pf_r']):>6s}  "
              f"Exp={sm['exp_r']:+.3f}R  TotalR={sm['total_r']:+.2f}R  Stop={sm['stop_rate']:.1f}%")

    # ── 2. Robustness Exclusions ──
    print(f"\n{'─'*140}")
    print("2. ROBUSTNESS EXCLUSIONS")
    print(f"{'─'*140}")
    rob = exclusion_robustness(trades)

    print(f"  Best day:         {rob['best_day']}  {rob['best_day_r']:+.2f}R")
    print(f"  Ex-best-day:      {rob['ex_best_day_total_r']:+.2f}R  PF={pf_str(rob['ex_best_day_pf'])}")
    print(f"  Top 3 days:       {', '.join(f'{d} {r:+.2f}R' for d,r in rob['top_3_days'])}")
    print(f"  Ex-top-3-days:    {rob['ex_top_3_days_total_r']:+.2f}R  PF={pf_str(rob['ex_top_3_days_pf'])}")
    print(f"  Top symbol:       {rob['top_sym']}  {rob['top_sym_r']:+.2f}R")
    print(f"  Ex-top-sym:       {rob['ex_top_sym_total_r']:+.2f}R  PF={pf_str(rob['ex_top_sym_pf'])}")
    print(f"  Top 3 symbols:    {', '.join(f'{s} {r:+.2f}R' for s,r in rob['top_3_syms'])}")
    print(f"  Ex-top-3-syms:    {rob['ex_top_3_syms_total_r']:+.2f}R  PF={pf_str(rob['ex_top_3_syms_pf'])}")

    # ── 3. Train/Test (odd/even) ──
    print(f"\n{'─'*140}")
    print("3. TRAIN/TEST (ODD/EVEN DAY SPLIT)")
    print(f"{'─'*140}")
    train, test = split_train_test(trades)
    tr_m = compute_metrics(train)
    te_m = compute_metrics(test)
    stable = tr_m["pf_r"] >= 1.0 and te_m["pf_r"] >= 1.0
    print(f"  Train: N={tr_m['n']:4d}  PF={pf_str(tr_m['pf_r']):>6s}  Exp={tr_m['exp_r']:+.3f}R  TotalR={tr_m['total_r']:+.2f}R")
    print(f"  Test:  N={te_m['n']:4d}  PF={pf_str(te_m['pf_r']):>6s}  Exp={te_m['exp_r']:+.3f}R  TotalR={te_m['total_r']:+.2f}R")
    print(f"  STABLE: {'YES' if stable else 'NO'}")

    # ── 4. Walk-Forward Slices ──
    print(f"\n{'─'*140}")
    print("4. WALK-FORWARD SLICES (14-day non-overlapping windows)")
    print(f"{'─'*140}")
    wf = walk_forward_slices(trades, window_days=14)
    if wf:
        print(f"  {'Slice':5s}  {'Train':23s}  {'N':>3s} {'PF':>6s} {'Exp':>7s} {'TotR':>7s}  "
              f"{'Test':23s}  {'N':>3s} {'PF':>6s} {'Exp':>7s} {'TotR':>7s}  {'Pass':>4s}")
        for i, s in enumerate(wf):
            tr_pass = s['train_pf'] >= 0.80
            te_pass = s['test_pf'] >= 0.80
            both = "YES" if (tr_pass and te_pass) else " NO"
            print(f"  {i+1:5d}  {s['train_start']}→{s['train_end']}  "
                  f"{s['train_n']:3d} {pf_str(s['train_pf']):>6s} {s['train_exp']:+6.3f}R {s['train_total_r']:+6.2f}R  "
                  f"{s['test_start']}→{s['test_end']}  "
                  f"{s['test_n']:3d} {pf_str(s['test_pf']):>6s} {s['test_exp']:+6.3f}R {s['test_total_r']:+6.2f}R  {both}")
        pass_count = sum(1 for s in wf if s['train_pf'] >= 0.80 and s['test_pf'] >= 0.80)
        print(f"\n  Walk-forward pass rate: {pass_count}/{len(wf)} slices ({pass_count/len(wf)*100:.0f}%)")
    else:
        print("  Not enough data for 14-day walk-forward slices.")

    # ── 5. Weekly / Monthly Consistency ──
    print(f"\n{'─'*140}")
    print("5. WEEKLY / MONTHLY CONSISTENCY")
    print(f"{'─'*140}")
    weekly, monthly = weekly_monthly_consistency(trades)

    print(f"\n  Weekly ({weekly['positive_weeks']}/{weekly['total_weeks']} positive = {weekly['win_rate']:.0f}%):")
    for wk, r, n in weekly["weeks"]:
        bar = "+" * max(0, int(r)) + "-" * max(0, int(-r))
        print(f"    {wk}  {r:+6.2f}R  N={n:3d}  {bar}")

    print(f"\n  Monthly ({monthly['positive_months']}/{monthly['total_months']} positive = {monthly['win_rate']:.0f}%):")
    for mo, r, n in monthly["months"]:
        bar = "+" * max(0, int(r / 2)) + "-" * max(0, int(-r / 2))
        print(f"    {mo}  {r:+7.2f}R  N={n:3d}  {bar}")

    # ── 6. Longest Losing Streak ──
    print(f"\n{'─'*140}")
    print("6. LONGEST LOSING STREAK")
    print(f"{'─'*140}")
    lls = longest_losing_streak(trades)
    print(f"  Streak:   {lls['longest_losing_streak']} consecutive losses")
    print(f"  Streak R: {lls['streak_r']:+.2f}R")
    print(f"  Period:   {lls['streak_start']} → {lls['streak_end']}")

    # ── 7. Contribution by Symbol ──
    print(f"\n{'─'*140}")
    print("7. CONTRIBUTION BY SYMBOL")
    print(f"{'─'*140}")
    sym_contrib = contribution_by_symbol(trades)
    profitable = sum(1 for _, r, _, _ in sym_contrib if r > 0)
    losing = sum(1 for _, r, _, _ in sym_contrib if r <= 0)
    print(f"  {profitable} profitable / {losing} losing symbols")
    print(f"\n  {'Symbol':8s} {'TotalR':>8s} {'N':>4s} {'Wins':>4s} {'WR%':>6s}")
    print(f"  {'-'*8} {'-'*8} {'-'*4} {'-'*4} {'-'*6}")
    for sym, r, n, w in sym_contrib[:15]:
        print(f"  {sym:8s} {r:+7.2f}R {n:4d} {w:4d} {w/n*100:5.1f}%")
    print(f"  ...")
    for sym, r, n, w in sym_contrib[-5:]:
        print(f"  {sym:8s} {r:+7.2f}R {n:4d} {w:4d} {w/n*100:5.1f}%")

    # ── 8. Contribution by Day of Week ──
    print(f"\n{'─'*140}")
    print("8. CONTRIBUTION BY DAY OF WEEK")
    print(f"{'─'*140}")
    dow = contribution_by_dow(trades)
    for day, v in dow.items():
        exp = v['r'] / v['n'] if v['n'] > 0 else 0
        print(f"  {day:3s}  {v['r']:+7.2f}R  N={v['n']:3d}  Exp={exp:+.3f}R")

    # ── 9. Contribution by Hour ──
    print(f"\n{'─'*140}")
    print("9. CONTRIBUTION BY HOUR BUCKET")
    print(f"{'─'*140}")
    hour = contribution_by_hour(trades)
    for h, v in hour.items():
        exp = v['r'] / v['n'] if v['n'] > 0 else 0
        print(f"  {h}  {v['r']:+7.2f}R  N={v['n']:3d}  Exp={exp:+.3f}R")

    # ── 10. Exit reason distribution ──
    print(f"\n{'─'*140}")
    print("10. EXIT REASON DISTRIBUTION")
    print(f"{'─'*140}")
    exit_reasons = defaultdict(lambda: {"n": 0, "r": 0.0})
    for t in trades:
        exit_reasons[t.exit_reason]["n"] += 1
        exit_reasons[t.exit_reason]["r"] += t.pnl_rr
    for reason, v in sorted(exit_reasons.items(), key=lambda x: -x[1]["n"]):
        exp = v['r'] / v['n'] if v['n'] > 0 else 0
        print(f"  {reason:12s}  N={v['n']:4d} ({v['n']/m['n']*100:5.1f}%)  "
              f"TotalR={v['r']:+7.2f}R  Exp={exp:+.3f}R")

    # ── Save JSON ──
    output = {
        "core": {k: round(v, 4) if isinstance(v, float) else v for k, v in m.items()},
        "drawdown": dd,
        "robustness": rob,
        "train_test": {"train": {k: round(v, 4) if isinstance(v, float) else v for k, v in tr_m.items()},
                       "test": {k: round(v, 4) if isinstance(v, float) else v for k, v in te_m.items()},
                       "stable": stable},
        "walk_forward": wf,
        "weekly": weekly,
        "monthly": monthly,
        "losing_streak": lls,
        "exit_reasons": {k: {"n": v["n"], "r": round(v["r"], 3)} for k, v in exit_reasons.items()},
    }
    out_path = Path(__file__).parent / "vr_robustness_results.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved: {out_path}")

    print(f"\n{'='*140}")
    print("STUDY COMPLETE")
    print(f"{'='*140}")


if __name__ == "__main__":
    main()
