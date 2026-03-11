"""
Experiment Harness — Quantitative Research Framework.

Runs controlled experiments on strategy configurations.
Reports R-only metrics with train/test stability and robustness checks.
Does NOT modify the engine. Only generates configs and evaluates results.

Usage:
    python -m alert_overlay.experiment_harness
"""

import json
import copy
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import SetupId, SETUP_DISPLAY_NAME
from .market_context import SECTOR_MAP, get_sector_etf

DATA_DIR = Path(__file__).parent / "data"


# ── Data Loading (cached) ──

_cache: Dict[str, list] = {}

def _load_bars(sym: str):
    if sym not in _cache:
        p = DATA_DIR / f"{sym}_5min.csv"
        _cache[sym] = load_bars_from_csv(str(p)) if p.exists() else []
    return _cache[sym]


def get_universe():
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    return sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])


def get_sector_bars_dict():
    d = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        bars = _load_bars(etf)
        if bars:
            d[etf] = bars
    return d


def classify_spy_days(spy_bars):
    daily = defaultdict(list)
    for b in spy_bars:
        daily[b.timestamp.date()].append(b)
    day_info = {}
    ranges = []
    for d in sorted(daily.keys()):
        bars = daily[d]
        o, c = bars[0].open, bars[-1].close
        chg = (c - o) / o * 100 if o > 0 else 0
        if chg > 0.05:
            direction = "GREEN"
        elif chg < -0.05:
            direction = "RED"
        else:
            direction = "FLAT"
        h = max(b.high for b in bars)
        lo = min(b.low for b in bars)
        ranges.append(h - lo)
        avg10 = sum(ranges[-10:]) / len(ranges[-10:])
        trend = "TREND" if (h - lo) >= avg10 * 0.7 else "CHOPPY"
        day_info[d] = {"direction": direction, "regime": f"{direction}+{trend}"}
    return day_info


# ── Trade Wrapper ──

class RTrade:
    __slots__ = ("pnl_rr", "exit_reason", "bars_held", "entry_time",
                 "entry_date", "setup_name", "symbol", "direction")

    def __init__(self, t: Trade, symbol: str):
        self.pnl_rr = t.pnl_rr
        self.exit_reason = t.exit_reason
        self.bars_held = t.bars_held
        self.entry_time = t.signal.timestamp
        self.entry_date = t.signal.timestamp.date()
        self.setup_name = SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id))
        self.symbol = symbol
        self.direction = t.signal.direction


# ── Metrics ──

def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


def compute_metrics(trades: List[RTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0, "pf_r": 0, "exp_r": 0, "total_r": 0,
                "max_dd_r": 0, "stop_rate": 0}
    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    total_r = sum(t.pnl_rr for t in trades)
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    cum = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl_rr
        if cum > pk: pk = cum
        if pk - cum > dd: dd = pk - cum
    return {"n": n, "wr": len(wins)/n*100, "pf_r": pf, "exp_r": total_r/n,
            "total_r": total_r, "max_dd_r": dd, "stop_rate": stopped/n*100}


def split_train_test(trades):
    train = [t for t in trades if t.entry_date and t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date and t.entry_date.day % 2 == 0]
    return train, test


def robustness_check(trades):
    """Exclude best day and top symbol."""
    daily_r = defaultdict(float)
    sym_r = defaultdict(float)
    for t in trades:
        if t.entry_date: daily_r[t.entry_date] += t.pnl_rr
        sym_r[t.symbol] += t.pnl_rr
    best_day = max(daily_r, key=daily_r.get) if daily_r else None
    top_sym = max(sym_r, key=sym_r.get) if sym_r else None
    ex_day = [t for t in trades if t.entry_date != best_day] if best_day else trades
    ex_sym = [t for t in trades if t.symbol != top_sym] if top_sym else trades
    return {
        "ex_best_day": compute_metrics(ex_day)["total_r"],
        "best_day": str(best_day),
        "best_day_r": daily_r.get(best_day, 0),
        "ex_top_sym": compute_metrics(ex_sym)["total_r"],
        "top_sym": top_sym,
        "top_sym_r": sym_r.get(top_sym, 0),
    }


# ── Run Engine Backtest ──

def run_experiment(cfg: OverlayConfig, symbols: list, spy_bars: list,
                   qqq_bars: list, sector_bars_dict: dict,
                   spy_day_info: dict,
                   long_filter: str = "locked",
                   setup_filter: Optional[set] = None) -> List[RTrade]:
    """
    Run the backtest with a given config and return filtered trades.

    long_filter:
      "locked" = Non-RED, Q>=2, <15:30, direction=1
      "all_long" = Q>=2, direction=1 (no regime gate)
      "none" = all trades as-is

    setup_filter: if set, only include trades with setup_name in this set.
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

    # Apply filters
    filtered = all_trades
    if long_filter == "locked":
        def is_red(t):
            return spy_day_info.get(t.entry_date, {}).get("direction") == "RED"
        def hhmm(t):
            return t.entry_time.hour * 100 + t.entry_time.minute
        filtered = [t for t in filtered
                    if not is_red(t) and hhmm(t) < 1530 and t.direction == 1]
    elif long_filter == "all_long":
        filtered = [t for t in filtered if t.direction == 1]

    if setup_filter:
        filtered = [t for t in filtered if t.setup_name in setup_filter]

    return filtered


# ── Experiment Result ──

def evaluate(label: str, trades: List[RTrade], min_n: int = 20) -> dict:
    """Full evaluation of a trade set."""
    m = compute_metrics(trades)
    train, test = split_train_test(trades)
    tr = compute_metrics(train)
    te = compute_metrics(test)
    rob = robustness_check(trades)

    stable = (tr["pf_r"] >= 1.0 and te["pf_r"] >= 1.0
              and abs(te["wr"] - tr["wr"]) < 8.0) if m["n"] >= min_n else False
    sufficient_n = m["n"] >= min_n

    # Setup breakdown
    setup_breakdown = {}
    by_setup = defaultdict(list)
    for t in trades:
        by_setup[t.setup_name].append(t)
    for sn, st in sorted(by_setup.items()):
        sm = compute_metrics(st)
        setup_breakdown[sn] = {
            "n": sm["n"], "pf_r": round(sm["pf_r"], 3),
            "exp_r": round(sm["exp_r"], 3), "total_r": round(sm["total_r"], 2),
            "stop_rate": round(sm["stop_rate"], 1)
        }

    return {
        "label": label,
        "full": {k: round(v, 3) if isinstance(v, float) else v for k, v in m.items()},
        "train": {k: round(v, 3) if isinstance(v, float) else v for k, v in tr.items()},
        "test": {k: round(v, 3) if isinstance(v, float) else v for k, v in te.items()},
        "robustness": {k: round(v, 3) if isinstance(v, float) else v for k, v in rob.items()},
        "stable": stable,
        "sufficient_n": sufficient_n,
        "setup_breakdown": setup_breakdown,
    }


def print_evaluation(ev: dict):
    """Pretty print an evaluation result."""
    m = ev["full"]
    tr = ev["train"]
    te = ev["test"]
    rob = ev["robustness"]
    label = ev["label"]

    print(f"\n  {label}")
    print(f"    Full:  N={m['n']:4d}  WR={m['wr']:5.1f}%  PF(R)={pf_str(m['pf_r']):>6s}  "
          f"Exp={m['exp_r']:+.3f}R  TotalR={m['total_r']:+.2f}R  MaxDD={m['max_dd_r']:.2f}R  Stop={m['stop_rate']:.1f}%")
    print(f"    Train: N={tr['n']:4d}  PF(R)={pf_str(tr['pf_r']):>6s}  Exp={tr['exp_r']:+.3f}R  TotalR={tr['total_r']:+.2f}R")
    print(f"    Test:  N={te['n']:4d}  PF(R)={pf_str(te['pf_r']):>6s}  Exp={te['exp_r']:+.3f}R  TotalR={te['total_r']:+.2f}R")
    print(f"    Robust: ExBestDay={rob['ex_best_day']:+.2f}R ({rob['best_day']})  "
          f"ExTopSym={rob['ex_top_sym']:+.2f}R ({rob['top_sym']})")
    print(f"    Stable: {'YES' if ev['stable'] else 'NO'}  |  N >= 20: {'YES' if ev['sufficient_n'] else 'NO'}")

    if ev["setup_breakdown"]:
        print(f"    Setups:")
        for sn, sm in ev["setup_breakdown"].items():
            print(f"      {sn:20s}  N={sm['n']:3d}  PF(R)={pf_str(sm['pf_r']):>6s}  "
                  f"Exp={sm['exp_r']:+.3f}R  TotalR={sm['total_r']:+.2f}R  Stop={sm['stop_rate']:.1f}%")


def compare_to_baseline(baseline: dict, experiment: dict) -> dict:
    """Compare experiment to baseline, return deltas."""
    b = baseline["full"]
    e = experiment["full"]
    return {
        "delta_n": e["n"] - b["n"],
        "delta_exp_r": round(e["exp_r"] - b["exp_r"], 4),
        "delta_total_r": round(e["total_r"] - b["total_r"], 2),
        "delta_pf_r": round(e["pf_r"] - b["pf_r"], 3) if b["pf_r"] < 900 and e["pf_r"] < 900 else None,
        "delta_stop_rate": round(e["stop_rate"] - b["stop_rate"], 1),
        "baseline_stable": baseline["stable"],
        "experiment_stable": experiment["stable"],
    }


# ── Main: Experiment Batch 1 ──

def main():
    symbols = get_universe()
    spy_bars = _load_bars("SPY")
    qqq_bars = _load_bars("QQQ")
    sector_bars_dict = get_sector_bars_dict()
    spy_day_info = classify_spy_days(spy_bars)

    print("=" * 120)
    print("EXPERIMENT BATCH 1 — CAUSAL HYPOTHESIS TESTING")
    print("=" * 120)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Data: {spy_bars[0].timestamp.date()} → {spy_bars[-1].timestamp.date()}")

    results = {}

    # ═══════════════════════════════════════════════
    # BASELINE: Current locked long book (VK + SC)
    # ═══════════════════════════════════════════════
    print("\n" + "─" * 120)
    print("BASELINE: Current locked long book (VK + SC, Non-RED, Q>=2, <15:30)")
    print("─" * 120)

    cfg_base = OverlayConfig()
    cfg_base.show_ema_scalp = False
    cfg_base.show_failed_bounce = False
    cfg_base.show_spencer = False
    cfg_base.show_ema_fpip = False
    cfg_base.show_sc_v2 = False

    base_trades = run_experiment(cfg_base, symbols, spy_bars, qqq_bars,
                                 sector_bars_dict, spy_day_info,
                                 long_filter="locked")
    results["baseline"] = evaluate("BASELINE: VK + SC locked", base_trades)
    print_evaluation(results["baseline"])

    # ═══════════════════════════════════════════════
    # EXP 1: VK-only (drop SC entirely)
    # Hypothesis: SC is a net negative mechanism. Removing it
    # improves the long book by eliminating -7.32R drag.
    # Causal mechanism: SC's breakout-retest-confirm pattern
    # has no fixed target, leading to asymmetric payoffs
    # (small trail wins, full stop losses).
    # ═══════════════════════════════════════════════
    print("\n" + "─" * 120)
    print("EXP 1: VK-only (drop SC)")
    print("Hypothesis: SC's asymmetric exit (EMA9 trail vs hard stop) creates negative expectancy.")
    print("─" * 120)

    vk_trades = run_experiment(cfg_base, symbols, spy_bars, qqq_bars,
                               sector_bars_dict, spy_day_info,
                               long_filter="locked",
                               setup_filter={"VWAP KISS"})
    results["exp1_vk_only"] = evaluate("EXP1: VK-only", vk_trades)
    print_evaluation(results["exp1_vk_only"])

    # ═══════════════════════════════════════════════
    # EXP 2: SC with fixed target (OR range) instead of trail
    # Hypothesis: SC's negative R comes from the trail exit,
    # not the entry logic. If we give SC the same fixed-target
    # exit as VK, the asymmetry disappears.
    # Mechanism: breakout_exit_mode = "target" (uses OR-range target)
    # ═══════════════════════════════════════════════
    print("\n" + "─" * 120)
    print("EXP 2: SC with fixed target (OR range) instead of trail exit")
    print("Hypothesis: SC entry logic is sound; the EMA9 trail exit cuts winners short.")
    print("─" * 120)

    cfg_sc_target = copy.deepcopy(cfg_base)
    cfg_sc_target.breakout_exit_mode = "target"  # use fixed target for SC

    sc_target_trades = run_experiment(cfg_sc_target, symbols, spy_bars, qqq_bars,
                                      sector_bars_dict, spy_day_info,
                                      long_filter="locked",
                                      setup_filter={"2ND CHANCE"})
    results["exp2_sc_target"] = evaluate("EXP2: SC + fixed target", sc_target_trades)
    print_evaluation(results["exp2_sc_target"])

    # Also run SC-only with current trail exit for comparison
    sc_trail_trades = run_experiment(cfg_base, symbols, spy_bars, qqq_bars,
                                     sector_bars_dict, spy_day_info,
                                     long_filter="locked",
                                     setup_filter={"2ND CHANCE"})
    results["exp2b_sc_trail"] = evaluate("EXP2b: SC + trail (current)", sc_trail_trades)
    print_evaluation(results["exp2b_sc_trail"])

    # ═══════════════════════════════════════════════
    # EXP 3: VK with wider stop (2× intra ATR floor)
    # Hypothesis: VK's 50% stop rate is due to stop placement
    # at prior bar low, which is often noise-level distance.
    # Widening the min stop distance reduces stop-outs
    # at the cost of worse R per loss.
    # ═══════════════════════════════════════════════
    print("\n" + "─" * 120)
    print("EXP 3: VK with wider stop (min_stop = 1.5× intra ATR)")
    print("Hypothesis: VK stops are noise-level tight. Wider stops reduce churn.")
    print("─" * 120)

    cfg_vk_wide = copy.deepcopy(cfg_base)
    cfg_vk_wide.min_stop_intra_atr_mult = 1.5  # was 1.0

    vk_wide_trades = run_experiment(cfg_vk_wide, symbols, spy_bars, qqq_bars,
                                    sector_bars_dict, spy_day_info,
                                    long_filter="locked",
                                    setup_filter={"VWAP KISS"})
    results["exp3_vk_wide_stop"] = evaluate("EXP3: VK + wider stop (1.5× ATR)", vk_wide_trades)
    print_evaluation(results["exp3_vk_wide_stop"])

    # Also test 2.0×
    cfg_vk_wide2 = copy.deepcopy(cfg_base)
    cfg_vk_wide2.min_stop_intra_atr_mult = 2.0

    vk_wide2_trades = run_experiment(cfg_vk_wide2, symbols, spy_bars, qqq_bars,
                                     sector_bars_dict, spy_day_info,
                                     long_filter="locked",
                                     setup_filter={"VWAP KISS"})
    results["exp3b_vk_wide2_stop"] = evaluate("EXP3b: VK + wider stop (2.0× ATR)", vk_wide2_trades)
    print_evaluation(results["exp3b_vk_wide2_stop"])

    # ═══════════════════════════════════════════════
    # EXP 4: Drop regime gate entirely for longs
    # Hypothesis: The Non-RED filter blocks profitable RED-day
    # trades (period split showed +1.93R from RED longs at
    # tape >= 0.30). The regime gate may be destroying more
    # edge than it protects.
    # ═══════════════════════════════════════════════
    print("\n" + "─" * 120)
    print("EXP 4: All longs (drop Non-RED gate)")
    print("Hypothesis: Regime gate costs more in missed longs than it saves.")
    print("─" * 120)

    all_long_trades = run_experiment(cfg_base, symbols, spy_bars, qqq_bars,
                                     sector_bars_dict, spy_day_info,
                                     long_filter="all_long")
    results["exp4_no_regime"] = evaluate("EXP4: All longs (no regime gate)", all_long_trades)
    print_evaluation(results["exp4_no_regime"])

    # VK-only, no regime gate
    vk_no_regime = [t for t in all_long_trades if t.setup_name == "VWAP KISS"]
    results["exp4b_vk_no_regime"] = evaluate("EXP4b: VK-only, no regime", vk_no_regime)
    print_evaluation(results["exp4b_vk_no_regime"])

    # ═══════════════════════════════════════════════
    # EXP 5: SC with stronger breakout volume requirement
    # Hypothesis: Low-volume breakouts fail to hold. Requiring
    # stronger BO volume (1.5× instead of 1.25×) filters
    # weak breakouts that subsequently fail the retest.
    # ═══════════════════════════════════════════════
    print("\n" + "─" * 120)
    print("EXP 5: SC with stronger breakout volume (1.50× vol_ma)")
    print("Hypothesis: Weak-volume breakouts fail. Stronger filter removes noise trades.")
    print("─" * 120)

    cfg_sc_vol = copy.deepcopy(cfg_base)
    cfg_sc_vol.sc_strong_bo_vol_mult = 1.50  # was 1.25

    sc_vol_trades = run_experiment(cfg_sc_vol, symbols, spy_bars, qqq_bars,
                                   sector_bars_dict, spy_day_info,
                                   long_filter="locked",
                                   setup_filter={"2ND CHANCE"})
    results["exp5_sc_strong_vol"] = evaluate("EXP5: SC + strong BO vol (1.5×)", sc_vol_trades)
    print_evaluation(results["exp5_sc_strong_vol"])

    # ═══════════════════════════════════════════════
    # EXP 6: SC with shorter time stop (12 bars instead of 20)
    # Hypothesis: SC trades that haven't moved after 12 bars
    # are dead money. Cutting earlier reduces time decay
    # and the average size of losses.
    # ═══════════════════════════════════════════════
    print("\n" + "─" * 120)
    print("EXP 6: SC with shorter time stop (12 bars)")
    print("Hypothesis: SC dead money after 12 bars. Earlier cut reduces loss magnitude.")
    print("─" * 120)

    cfg_sc_time = copy.deepcopy(cfg_base)
    cfg_sc_time.breakout_time_stop_bars = 12  # was 20

    sc_time_trades = run_experiment(cfg_sc_time, symbols, spy_bars, qqq_bars,
                                    sector_bars_dict, spy_day_info,
                                    long_filter="locked",
                                    setup_filter={"2ND CHANCE"})
    results["exp6_sc_short_time"] = evaluate("EXP6: SC + 12-bar time stop", sc_time_trades)
    print_evaluation(results["exp6_sc_short_time"])

    # ═══════════════════════════════════════════════
    # EXP 7: VK + higher min R:R (2.0 instead of 1.5)
    # Hypothesis: VK trades with R:R < 2.0 have worse
    # expectancy because the target is too close relative
    # to the stop. Higher R:R gate filters poor-geometry trades.
    # ═══════════════════════════════════════════════
    print("\n" + "─" * 120)
    print("EXP 7: VK with min R:R = 2.0")
    print("Hypothesis: Low R:R VK trades have poor geometry. Filter improves exp.")
    print("─" * 120)

    cfg_vk_rr = copy.deepcopy(cfg_base)
    cfg_vk_rr.min_rr = 2.0  # was 1.5

    vk_rr_trades = run_experiment(cfg_vk_rr, symbols, spy_bars, qqq_bars,
                                  sector_bars_dict, spy_day_info,
                                  long_filter="locked",
                                  setup_filter={"VWAP KISS"})
    results["exp7_vk_rr2"] = evaluate("EXP7: VK + min R:R = 2.0", vk_rr_trades)
    print_evaluation(results["exp7_vk_rr2"])

    # ═══════════════════════════════════════════════
    #  SUMMARY TABLE
    # ═══════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SUMMARY — ALL EXPERIMENTS vs BASELINE")
    print("=" * 120)

    print(f"\n  {'Experiment':44s}  {'N':>4s}  {'PF(R)':>6s}  {'Exp(R)':>8s}  "
          f"{'TotalR':>8s}  {'Trn PF':>6s}  {'Tst PF':>6s}  {'Stable':>6s}  {'Decision':>8s}")
    print(f"  {'-'*44}  {'-'*4}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*8}")

    baseline_total_r = results["baseline"]["full"]["total_r"]

    for key in sorted(results.keys()):
        ev = results[key]
        m = ev["full"]
        tr = ev["train"]
        te = ev["test"]

        # Decision logic
        if not ev["sufficient_n"]:
            decision = "INSUFF_N"
        elif ev["stable"] and m["exp_r"] > 0 and m["total_r"] > baseline_total_r + 1.0:
            decision = "PROMOTE"
        elif m["exp_r"] > 0 and m["total_r"] > 0:
            decision = "REFINE"
        elif m["exp_r"] > results["baseline"]["full"]["exp_r"]:
            decision = "EXPAND"
        else:
            decision = "REJECT"

        print(f"  {ev['label']:44s}  {m['n']:4d}  {pf_str(m['pf_r']):>6s}  {m['exp_r']:+7.3f}R  "
              f"{m['total_r']:+7.2f}R  {pf_str(tr['pf_r']):>6s}  {pf_str(te['pf_r']):>6s}  "
              f"{'YES' if ev['stable'] else 'NO':>6s}  {decision:>8s}")

    # ═══════════════════════════════════════════════
    #  JSON OUTPUT
    # ═══════════════════════════════════════════════
    output_path = Path(__file__).parent / "experiment_results_batch1.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Results saved to: {output_path}")

    print("\n" + "=" * 120)
    print("BATCH 1 COMPLETE")
    print("=" * 120)


if __name__ == "__main__":
    main()
