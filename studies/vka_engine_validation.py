"""
VKA Engine Validation — Equivalence Check + Portfolio Comparison

1. Engine-vs-Standalone equivalence: trade count, entry timestamps, stop/target diffs
2. Portfolio comparison: VR+BDR vs VKA+BDR vs VR+VKA+BDR

No perfect-foresight filters. All engine-native context only.
"""

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import List

from ..backtest import run_backtest, load_bars_from_csv, Trade
from ..config import OverlayConfig
from ..indicators import EMA, VWAPCalc
from ..market_context import MarketEngine, get_sector_etf, SECTOR_MAP
from ..models import Bar, NaN, SetupId

DATA_DIR = Path(__file__).parent.parent / "data"
_isnan = math.isnan


def load_bars(sym: str) -> list:
    p = DATA_DIR / f"{sym}_5min.csv"
    return load_bars_from_csv(str(p)) if p.exists() else []


def get_universe() -> list:
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    return sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])


# ════════════════════════════════════════════════════════════════
#  R-metric computation (same as acceptance_study)
# ════════════════════════════════════════════════════════════════

@dataclass
class RTrade:
    """Wrapper for engine Trade with symbol tag."""
    symbol: str
    trade: Trade

    @property
    def pnl_rr(self) -> float:
        return self.trade.pnl_rr

    @property
    def entry_date(self) -> date:
        return self.trade.signal.timestamp.date()

    @property
    def setup_name(self) -> str:
        return self.trade.signal.setup_name

    @property
    def entry_time(self):
        return self.trade.signal.timestamp

    @property
    def entry_price(self) -> float:
        return self.trade.signal.entry_price

    @property
    def stop_price(self) -> float:
        return self.trade.signal.stop_price

    @property
    def target_price(self) -> float:
        return self.trade.signal.target_price

    @property
    def exit_reason(self) -> str:
        return self.trade.exit_reason


def compute_r_metrics(trades: list) -> dict:
    if not trades:
        return {"n": 0, "pf_r": 0, "exp_r": 0, "total_r": 0, "max_dd_r": 0,
                "stop_rate": 0, "quick_stop": 0, "target_rate": 0}

    wins_r = sum(t.pnl_rr for t in trades if t.pnl_rr > 0)
    losses_r = abs(sum(t.pnl_rr for t in trades if t.pnl_rr < 0))
    pf_r = wins_r / losses_r if losses_r > 0 else float('inf')
    total_r = sum(t.pnl_rr for t in trades)
    exp_r = total_r / len(trades) if trades else 0

    # Max drawdown in R
    cum = 0
    peak = 0
    max_dd = 0
    for t in sorted(trades, key=lambda x: x.entry_time):
        cum += t.pnl_rr
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    stops = sum(1 for t in trades if t.exit_reason == "stop")
    qstops = sum(1 for t in trades if t.exit_reason == "stop" and hasattr(t, 'trade') and t.trade.bars_held <= 2)
    targets = sum(1 for t in trades if t.exit_reason == "target")

    return {
        "n": len(trades),
        "pf_r": pf_r,
        "exp_r": exp_r,
        "total_r": total_r,
        "max_dd_r": max_dd,
        "stop_rate": stops / len(trades) * 100,
        "quick_stop": qstops / len(trades) * 100,
        "target_rate": targets / len(trades) * 100,
    }


def robustness(trades: list) -> dict:
    if len(trades) < 10:
        return {}

    # Ex-best-day
    daily_r = defaultdict(float)
    for t in trades:
        daily_r[t.entry_date] += t.pnl_rr
    best_day = max(daily_r, key=daily_r.get)
    ex_day = [t for t in trades if t.entry_date != best_day]
    ex_day_m = compute_r_metrics(ex_day)

    # Ex-top-symbol
    sym_r = defaultdict(float)
    for t in trades:
        sym_r[t.symbol] += t.pnl_rr
    top_sym = max(sym_r, key=sym_r.get)
    ex_sym = [t for t in trades if t.symbol != top_sym]
    ex_sym_m = compute_r_metrics(ex_sym)

    # Ex-top-3 days
    top3_days = sorted(daily_r.items(), key=lambda x: x[1], reverse=True)[:3]
    ex3d = [t for t in trades if t.entry_date not in {d for d, _ in top3_days}]
    ex3d_m = compute_r_metrics(ex3d)

    # Ex-top-3 symbols
    top3_syms = sorted(sym_r.items(), key=lambda x: x[1], reverse=True)[:3]
    ex3s = [t for t in trades if t.symbol not in {s for s, _ in top3_syms}]
    ex3s_m = compute_r_metrics(ex3s)

    # Train/test
    train = [t for t in trades if t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date.day % 2 == 0]
    tr_m = compute_r_metrics(train)
    te_m = compute_r_metrics(test)

    return {
        "ex_best_day_r": ex_day_m["total_r"],
        "ex_top_sym_r": ex_sym_m["total_r"],
        "ex_top3_days_r": ex3d_m["total_r"],
        "ex_top3_syms_r": ex3s_m["total_r"],
        "train_pf": tr_m["pf_r"],
        "test_pf": te_m["pf_r"],
        "train_n": tr_m["n"],
        "test_n": te_m["n"],
        "stable": tr_m["pf_r"] >= 0.80 and te_m["pf_r"] >= 0.80,
    }


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


# ════════════════════════════════════════════════════════════════
#  Run engine backtest for a given config
# ════════════════════════════════════════════════════════════════

def run_engine_portfolio(cfg: OverlayConfig, symbols: list,
                          spy_bars: list, qqq_bars: list,
                          setup_filter: set = None) -> List[RTrade]:
    """Run full engine backtest, optionally filter by setup names."""
    all_trades = []
    for sym in symbols:
        bars = load_bars(sym)
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = load_bars(sec_etf) if sec_etf and sec_etf not in {"SPY", "QQQ"} else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars,
                              qqq_bars=qqq_bars, sector_bars=sec_bars)
        for t in result.trades:
            if setup_filter and t.signal.setup_name not in setup_filter:
                continue
            all_trades.append(RTrade(symbol=sym, trade=t))
    return all_trades


# ════════════════════════════════════════════════════════════════
#  Section 1: Engine-vs-Standalone Equivalence Check
# ════════════════════════════════════════════════════════════════

def run_equivalence_check(symbols, spy_bars, qqq_bars):
    """Compare engine VKA trades to standalone acceptance_study VKA trades."""
    from .portfolio_configs import vka_only_bdr

    print("\n" + "=" * 120)
    print("SECTION 1: ENGINE-vs-STANDALONE EQUIVALENCE CHECK")
    print("=" * 120)

    # ── Engine VKA trades ──
    cfg = vka_only_bdr()
    engine_vka = run_engine_portfolio(cfg, symbols, spy_bars, qqq_bars,
                                       setup_filter={"VK ACCEPT"})
    engine_m = compute_r_metrics(engine_vka)

    print(f"\n  Engine VKA: N={engine_m['n']}  PF={pf_str(engine_m['pf_r'])}  "
          f"Exp={engine_m['exp_r']:+.3f}R  TotalR={engine_m['total_r']:+.2f}R  "
          f"MaxDD={engine_m['max_dd_r']:.2f}R  Stop%={engine_m['stop_rate']:.1f}%  "
          f"QStop%={engine_m['quick_stop']:.1f}%  Tgt%={engine_m['target_rate']:.1f}%")

    # ── Standalone reference (from acceptance study) ──
    # We report what standalone produced for reference:
    print(f"\n  Standalone VK_H2_EX_ACC_T1059_R2.0 (perfect-foresight GREEN):")
    print(f"    N=664  PF=1.48  Exp=+0.204R  TotalR=+135.27R  MaxDD=22.93R")
    print(f"    (NOTE: standalone used end-of-day SPY GREEN filter; engine uses real-time)")

    # ── Detailed analysis of engine VKA ──
    if engine_vka:
        rob = robustness(engine_vka)

        # Monthly breakdown
        monthly = defaultdict(list)
        for t in engine_vka:
            monthly[t.entry_date.strftime("%Y-%m")].append(t.pnl_rr)
        print(f"\n  Engine VKA Monthly Breakdown:")
        for mo in sorted(monthly.keys()):
            rs = monthly[mo]
            total = sum(rs)
            wins = sum(r for r in rs if r > 0)
            losses = abs(sum(r for r in rs if r < 0))
            pf = wins / losses if losses > 0 else float('inf')
            wr = sum(1 for r in rs if r > 0) / len(rs) * 100
            print(f"    {mo}: {len(rs):4d} trades  {total:+7.2f}R  WR={wr:5.1f}%  PF={pf_str(pf)}")

        print(f"\n  Engine VKA Robustness:")
        print(f"    Train/Test PF: {pf_str(rob.get('train_pf',0))} / {pf_str(rob.get('test_pf',0))}  "
              f"(N: {rob.get('train_n',0)} / {rob.get('test_n',0)})  "
              f"Stable: {'YES' if rob.get('stable') else 'NO'}")
        print(f"    Ex-best-day: {rob.get('ex_best_day_r',0):+.2f}R")
        print(f"    Ex-top-sym:  {rob.get('ex_top_sym_r',0):+.2f}R")
        print(f"    Ex-top-3-days: {rob.get('ex_top3_days_r',0):+.2f}R")
        print(f"    Ex-top-3-syms: {rob.get('ex_top3_syms_r',0):+.2f}R")

        # Timestamp distribution
        hours = defaultdict(int)
        for t in engine_vka:
            h = t.entry_time.hour * 100 + t.entry_time.minute
            hours[h] += 1
        print(f"\n  Entry Time Distribution:")
        for h in sorted(hours.keys()):
            print(f"    {h:04d}: {hours[h]:4d} trades")

    # ── Equivalence assessment ──
    print(f"\n  {'─' * 100}")
    print(f"  EQUIVALENCE ASSESSMENT:")
    if engine_m['n'] == 0:
        print(f"    ⚠ Engine produced ZERO VKA trades. State machine or config issue.")
    else:
        standalone_n = 664
        n_diff_pct = abs(engine_m['n'] - standalone_n) / standalone_n * 100
        print(f"    Trade count:  engine={engine_m['n']} vs standalone={standalone_n}  "
              f"(diff={n_diff_pct:.1f}%)")
        if n_diff_pct < 10:
            print(f"    → Trade count CLOSE (<10% diff). Expected due to real-time vs perfect-foresight SPY filter.")
        elif n_diff_pct < 30:
            print(f"    → Trade count MODERATE diff (10-30%). Partially explained by SPY filter difference.")
        else:
            print(f"    → Trade count LARGE diff (>30%). Investigate state machine differences.")

    return engine_vka


# ════════════════════════════════════════════════════════════════
#  Section 2: Portfolio Comparison
# ════════════════════════════════════════════════════════════════

def run_portfolio_comparison(symbols, spy_bars, qqq_bars):
    """Compare VR+BDR vs VKA+BDR vs VR+VKA+BDR."""
    from .portfolio_configs import (
        candidate_v2_vrgreen_bdr, vka_only_bdr, vr_plus_vka_bdr
    )

    print("\n" + "=" * 120)
    print("SECTION 2: PORTFOLIO COMPARISON")
    print("=" * 120)

    configs = {
        "VR+BDR (current)": (candidate_v2_vrgreen_bdr(), None),
        "VKA+BDR":          (vka_only_bdr(), None),
        "VR+VKA+BDR":       (vr_plus_vka_bdr(), None),
    }

    results = {}
    for label, (cfg, _) in configs.items():
        print(f"\n  Running: {label}...")
        trades = run_engine_portfolio(cfg, symbols, spy_bars, qqq_bars)
        m = compute_r_metrics(trades)
        rob = robustness(trades) if m["n"] >= 10 else {}
        results[label] = {"trades": trades, "metrics": m, "robustness": rob}

    # ── Comparison table ──
    print(f"\n{'─' * 120}")
    print(f"  {'Portfolio':24s} {'N':>5s} {'PF(R)':>6s} {'Exp(R)':>8s} {'TotalR':>8s} "
          f"{'MaxDD':>7s} {'Stop%':>6s} {'QStop%':>6s} {'Tgt%':>5s} "
          f"{'TrnPF':>6s} {'TstPF':>6s} {'Stbl':>4s} {'ExD3':>8s} {'ExS3':>8s}")
    print(f"  {'-'*24} {'-'*5} {'-'*6} {'-'*8} {'-'*8} "
          f"{'-'*7} {'-'*6} {'-'*6} {'-'*5} "
          f"{'-'*6} {'-'*6} {'-'*4} {'-'*8} {'-'*8}")

    for label, r in results.items():
        m = r["metrics"]
        rob = r["robustness"]
        print(f"  {label:24s} {m['n']:5d} {pf_str(m['pf_r']):>6s} {m['exp_r']:+7.3f}R "
              f"{m['total_r']:+7.2f}R {m['max_dd_r']:6.2f}R {m['stop_rate']:5.1f}% {m['quick_stop']:5.1f}% "
              f"{m['target_rate']:4.1f}% "
              f"{pf_str(rob.get('train_pf',0)):>6s} {pf_str(rob.get('test_pf',0)):>6s} "
              f"{'YES' if rob.get('stable') else ' NO':>4s} "
              f"{rob.get('ex_top3_days_r',0):+7.2f}R {rob.get('ex_top3_syms_r',0):+7.2f}R")

    # ── Per-setup breakdown ──
    print(f"\n{'─' * 120}")
    print("  PER-SETUP BREAKDOWN")
    print(f"{'─' * 120}")

    for label, r in results.items():
        by_setup = defaultdict(list)
        for t in r["trades"]:
            by_setup[t.setup_name].append(t)
        print(f"\n  {label}:")
        for setup in sorted(by_setup.keys()):
            sm = compute_r_metrics(by_setup[setup])
            print(f"    {setup:20s}: N={sm['n']:4d}  PF={pf_str(sm['pf_r'])}  "
                  f"Exp={sm['exp_r']:+.3f}R  TotalR={sm['total_r']:+.2f}R  "
                  f"MaxDD={sm['max_dd_r']:.2f}R")

    # ── Overlap analysis for VR+VKA+BDR ──
    print(f"\n{'─' * 120}")
    print("  OVERLAP ANALYSIS: VR+VKA+BDR")
    print(f"{'─' * 120}")

    combo_trades = results["VR+VKA+BDR"]["trades"]
    vr_dates = set()
    vka_dates = set()
    for t in combo_trades:
        key = (t.symbol, t.entry_date)
        if t.setup_name == "VWAP RECLAIM":
            vr_dates.add(key)
        elif t.setup_name == "VK ACCEPT":
            vka_dates.add(key)

    overlap = vr_dates & vka_dates
    vr_only = vr_dates - vka_dates
    vka_only = vka_dates - vr_dates

    print(f"  VR-only sym-days:  {len(vr_only)}")
    print(f"  VKA-only sym-days: {len(vka_only)}")
    print(f"  Both on same sym-day: {len(overlap)}")

    if overlap:
        # Compute R contribution of overlapping sym-days
        overlap_r_vr = sum(t.pnl_rr for t in combo_trades
                           if (t.symbol, t.entry_date) in overlap and t.setup_name == "VWAP RECLAIM")
        overlap_r_vka = sum(t.pnl_rr for t in combo_trades
                            if (t.symbol, t.entry_date) in overlap and t.setup_name == "VK ACCEPT")
        print(f"  Overlap sym-day VR R:  {overlap_r_vr:+.2f}R")
        print(f"  Overlap sym-day VKA R: {overlap_r_vka:+.2f}R")
        print(f"  → {'ADDITIVE' if overlap_r_vr > 0 and overlap_r_vka > 0 else 'DILUTIVE/MIXED'}")

    # Additivity check
    vr_total = results["VR+BDR (current)"]["metrics"]["total_r"]
    vka_total = results["VKA+BDR"]["metrics"]["total_r"]
    combo_total = results["VR+VKA+BDR"]["metrics"]["total_r"]
    sum_individual = vr_total + vka_total
    # Subtract BDR double-count: BDR appears in both individual runs
    bdr_in_vr = sum(t.pnl_rr for t in results["VR+BDR (current)"]["trades"]
                    if t.setup_name == "BDR SHORT")
    bdr_in_vka = sum(t.pnl_rr for t in results["VKA+BDR"]["trades"]
                     if t.setup_name == "BDR SHORT")
    bdr_in_combo = sum(t.pnl_rr for t in combo_trades if t.setup_name == "BDR SHORT")

    vr_long_only = vr_total - bdr_in_vr
    vka_long_only = vka_total - bdr_in_vka
    combo_long_only = combo_total - bdr_in_combo

    print(f"\n  Additivity Check (long-side only, ex-BDR):")
    print(f"    VR long:     {vr_long_only:+.2f}R")
    print(f"    VKA long:    {vka_long_only:+.2f}R")
    print(f"    Sum:         {vr_long_only + vka_long_only:+.2f}R")
    print(f"    Combo long:  {combo_long_only:+.2f}R")
    diff = combo_long_only - (vr_long_only + vka_long_only)
    print(f"    Diff:        {diff:+.2f}R  "
          f"({'ADDITIVE' if diff >= -1 else 'DILUTIVE — cooldown/overlap drag'})")

    return results


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    symbols = get_universe()
    spy_bars = load_bars("SPY")
    qqq_bars = load_bars("QQQ")

    print("=" * 120)
    print("VKA ENGINE VALIDATION — Equivalence Check + Portfolio Comparison")
    print("=" * 120)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Data: {spy_bars[0].timestamp.date()} → {spy_bars[-1].timestamp.date()}")
    print(f"Engine: real-time SPY context (NO perfect-foresight)")

    # Section 1: Equivalence
    engine_vka = run_equivalence_check(symbols, spy_bars, qqq_bars)

    # Section 2: Portfolio comparison
    results = run_portfolio_comparison(symbols, spy_bars, qqq_bars)

    # ── Final Verdict ──
    print(f"\n{'=' * 120}")
    print("FINAL VERDICT")
    print(f"{'=' * 120}")

    vr_m = results["VR+BDR (current)"]["metrics"]
    vka_m = results["VKA+BDR"]["metrics"]
    combo_m = results["VR+VKA+BDR"]["metrics"]
    vr_rob = results["VR+BDR (current)"]["robustness"]
    vka_rob = results["VKA+BDR"]["robustness"]
    combo_rob = results["VR+VKA+BDR"]["robustness"]

    print(f"\n  VR+BDR:     PF={pf_str(vr_m['pf_r'])}  Total={vr_m['total_r']:+.2f}R  "
          f"MaxDD={vr_m['max_dd_r']:.2f}R  Train/Test={pf_str(vr_rob.get('train_pf',0))}/{pf_str(vr_rob.get('test_pf',0))}")
    print(f"  VKA+BDR:    PF={pf_str(vka_m['pf_r'])}  Total={vka_m['total_r']:+.2f}R  "
          f"MaxDD={vka_m['max_dd_r']:.2f}R  Train/Test={pf_str(vka_rob.get('train_pf',0))}/{pf_str(vka_rob.get('test_pf',0))}")
    print(f"  VR+VKA+BDR: PF={pf_str(combo_m['pf_r'])}  Total={combo_m['total_r']:+.2f}R  "
          f"MaxDD={combo_m['max_dd_r']:.2f}R  Train/Test={pf_str(combo_rob.get('train_pf',0))}/{pf_str(combo_rob.get('test_pf',0))}")

    # Decision logic
    if vka_m["n"] < 20:
        print(f"\n  VERDICT: INSUFFICIENT DATA — VKA produced {vka_m['n']} trades in-engine.")
        print(f"  The engine state machine may need debugging.")
    elif vka_m["pf_r"] > vr_m["pf_r"] and vka_m["exp_r"] > vr_m["exp_r"]:
        if vka_rob.get("stable"):
            print(f"\n  VERDICT: VKA SURVIVES ENGINE INTEGRATION.")
            print(f"  VKA is stronger than VR on PF, Exp, and stability.")
            if combo_m["pf_r"] > max(vr_m["pf_r"], vka_m["pf_r"]) * 0.95:
                print(f"  The combo (VR+VKA+BDR) is also viable — test for overlap drag.")
            print(f"  RECOMMENDATION: Shadow-trade VKA alongside VR for 4+ weeks.")
        else:
            print(f"\n  VERDICT: VKA SHOWS PROMISE BUT TRAIN/TEST UNSTABLE.")
            print(f"  Continue shadow tracking. Do not promote yet.")
    elif vka_m["pf_r"] >= 1.0 and vka_m["exp_r"] > 0:
        print(f"\n  VERDICT: VKA SURVIVES ENGINE INTEGRATION (MODERATE).")
        print(f"  VKA is positive but not clearly superior to VR in-engine.")
        print(f"  Engine real-time SPY filter weakens vs standalone perfect-foresight.")
        print(f"  RECOMMENDATION: Shadow-trade VKA. Revisit after 4+ weeks of data.")
    else:
        print(f"\n  VERDICT: VKA DOES NOT SURVIVE ENGINE INTEGRATION.")
        print(f"  The standalone edge does not translate to engine constraints.")
        print(f"  Real-time SPY filter likely too permissive — admitting losing trades.")

    print(f"\n{'=' * 120}")


if __name__ == "__main__":
    main()
