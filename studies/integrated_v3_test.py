"""
Integrated v3 Portfolio Test — Per-Setup Gating Validation

Runs the candidate_v3_sc_bdr_ep config in a TRUE SINGLE ENGINE PASS
and compares to the isolated-sum baseline (PF 1.38, +43.29R, 163 trades).

Reports: trades, PF(R), exp(R), total R, max DD, stop rate, quick-stop rate,
         ex-best-day, ex-top-symbol, train/test split, per-setup breakdown.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import List

from ..backtest import run_backtest, load_bars_from_csv, Trade
from ..config import OverlayConfig
from ..market_context import get_sector_etf, SECTOR_MAP
from ..models import NaN, SetupId, SETUP_DISPLAY_NAME
from .portfolio_configs import candidate_v3_sc_bdr_ep, ACTIVE_SETUPS_V3

DATA_DIR = Path(__file__).parent.parent / "data"
_isnan = math.isnan


def load_bars(sym):
    p = DATA_DIR / f"{sym}_5min.csv"
    return load_bars_from_csv(str(p)) if p.exists() else []


def get_universe():
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    return sorted([p.stem.replace("_5min", "") for p in DATA_DIR.glob("*_5min.csv")
                   if p.stem.replace("_5min", "") not in excluded])


@dataclass
class EngTrade:
    symbol: str
    trade: Trade

    @property
    def pnl_rr(self): return self.trade.pnl_rr
    @property
    def setup_name(self): return self.trade.signal.setup_name
    @property
    def exit_reason(self): return self.trade.exit_reason
    @property
    def bars_held(self): return self.trade.bars_held
    @property
    def direction(self): return self.trade.signal.direction
    @property
    def entry_date(self): return self.trade.signal.timestamp.date()
    @property
    def hhmm(self): return self.trade.signal.timestamp.hour * 100 + self.trade.signal.timestamp.minute


def metrics(trades):
    if not trades:
        return {"n": 0, "pf_r": 0, "total_r": 0, "exp_r": 0, "win_rate": 0,
                "avg_win_r": 0, "avg_loss_r": 0, "max_dd_r": 0,
                "stop_rate": 0, "quick_stop_rate": 0, "target_rate": 0, "eod_rate": 0}
    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    gross_w = sum(t.pnl_rr for t in wins)
    gross_l = abs(sum(t.pnl_rr for t in losses))
    total = sum(t.pnl_rr for t in trades)
    n = len(trades)
    cum = peak = max_dd = 0.0
    for t in trades:
        cum += t.pnl_rr
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    stops = [t for t in trades if t.exit_reason == "stop"]
    quick = [t for t in trades if t.exit_reason == "stop" and t.bars_held <= 2]
    targets = [t for t in trades if t.exit_reason == "target"]
    eods = [t for t in trades if t.exit_reason == "eod"]
    return {
        "n": n, "pf_r": gross_w / gross_l if gross_l > 0 else float('inf'),
        "total_r": total, "exp_r": total / n,
        "win_rate": len(wins) / n * 100,
        "avg_win_r": gross_w / len(wins) if wins else 0,
        "avg_loss_r": -gross_l / len(losses) if losses else 0,
        "max_dd_r": max_dd,
        "stop_rate": len(stops) / n * 100,
        "quick_stop_rate": len(quick) / n * 100,
        "target_rate": len(targets) / n * 100,
        "eod_rate": len(eods) / n * 100,
    }


def pf(v): return "Inf" if v == float('inf') else f"{v:.2f}"


def print_metrics(label, m, indent="  "):
    print(f"{indent}{label:<40s}  N={m['n']:>5d}  PF={pf(m['pf_r']):>6s}  "
          f"TotalR={m['total_r']:>+9.2f}  Exp={m['exp_r']:>+7.3f}  "
          f"WR={m['win_rate']:>5.1f}%  AvgW={m['avg_win_r']:>5.2f}  AvgL={m['avg_loss_r']:>6.2f}  "
          f"MaxDD={m['max_dd_r']:>7.2f}  Stop={m['stop_rate']:>5.1f}%  "
          f"QStop={m['quick_stop_rate']:>5.1f}%  Tgt={m['target_rate']:>5.1f}%  "
          f"EOD={m['eod_rate']:>5.1f}%")


def split_train_test(trades):
    train = [t for t in trades if t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date.day % 2 == 0]
    return train, test


def ex_best_day(trades):
    """Metrics excluding the best single-day P&L."""
    by_date = defaultdict(float)
    for t in trades:
        by_date[t.entry_date] += t.pnl_rr
    if not by_date:
        return metrics([])
    best_date = max(by_date, key=by_date.get)
    filtered = [t for t in trades if t.entry_date != best_date]
    return metrics(filtered), best_date, by_date[best_date]


def ex_top_symbol(trades):
    """Metrics excluding the top-contributing symbol."""
    by_sym = defaultdict(float)
    for t in trades:
        by_sym[t.symbol] += t.pnl_rr
    if not by_sym:
        return metrics([])
    top_sym = max(by_sym, key=by_sym.get)
    filtered = [t for t in trades if t.symbol != top_sym]
    return metrics(filtered), top_sym, by_sym[top_sym]


def run_integrated():
    """Run integrated v3 portfolio in a single engine pass."""
    cfg = candidate_v3_sc_bdr_ep()
    symbols = get_universe()
    spy_bars = load_bars("SPY")
    qqq_bars = load_bars("QQQ")

    print(f"{'='*80}")
    print(f"INTEGRATED V3 PORTFOLIO TEST — Per-Setup Gating")
    print(f"{'='*80}")
    print(f"Universe: {len(symbols)} symbols")
    print(f"Active setups: {ACTIVE_SETUPS_V3}")
    print()

    # Config summary
    print("Config summary:")
    print(f"  show_second_chance={cfg.show_second_chance}  sc_long_only={cfg.sc_long_only}  "
          f"sc_min_quality={cfg.sc_min_quality}  sc_require_regime={cfg.sc_require_regime}")
    print(f"  show_breakdown_retest={cfg.show_breakdown_retest}  bdr_require_regime={cfg.bdr_require_regime}  "
          f"bdr_am_only={cfg.bdr_am_only}")
    print(f"  show_ema_pullback={cfg.show_ema_pullback}  show_trend_setups={cfg.show_trend_setups}  "
          f"ep_short_only={cfg.ep_short_only}  ep_time_end={cfg.ep_time_end}  "
          f"ep_require_regime={cfg.ep_require_regime}")
    print(f"  require_regime={cfg.require_regime}  min_quality={cfg.min_quality}")
    print()

    # Run single engine pass
    all_trades = []
    for sym in symbols:
        bars = load_bars(sym)
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = load_bars(sec_etf) if sec_etf and sec_etf not in {"SPY", "QQQ"} else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars, sector_bars=sec_bars)
        for t in result.trades:
            et = EngTrade(symbol=sym, trade=t)
            if et.setup_name in ACTIVE_SETUPS_V3:
                all_trades.append(et)

    # Sort by entry time for proper equity curve
    all_trades.sort(key=lambda t: t.trade.signal.timestamp)

    print(f"\n{'─'*80}")
    print(f"INTEGRATED RESULTS (single engine pass, cost-on)")
    print(f"{'─'*80}")

    m = metrics(all_trades)
    print_metrics("COMBINED (all 3 setups)", m)

    # Per-setup breakdown
    print(f"\n  Per-setup breakdown:")
    setup_trades = defaultdict(list)
    for t in all_trades:
        setup_trades[t.setup_name].append(t)
    for name in sorted(setup_trades.keys()):
        sm = metrics(setup_trades[name])
        dirs = [t.direction for t in setup_trades[name]]
        long_ct = sum(1 for d in dirs if d == 1)
        short_ct = sum(1 for d in dirs if d == -1)
        print_metrics(f"  {name} (L:{long_ct} S:{short_ct})", sm, indent="    ")

    # Train/test split
    print(f"\n  Train/Test (odd/even date):")
    train, test = split_train_test(all_trades)
    mt = metrics(train)
    me = metrics(test)
    print_metrics("  Train (odd dates)", mt, indent="    ")
    print_metrics("  Test  (even dates)", me, indent="    ")

    # Ex-best-day
    print(f"\n  Robustness checks:")
    ebd_result = ex_best_day(all_trades)
    if isinstance(ebd_result, tuple):
        ebd_m, best_date, best_pnl = ebd_result
        print(f"    Best day: {best_date} (+{best_pnl:.2f}R)")
        print_metrics("  Ex-best-day", ebd_m, indent="    ")

    # Ex-top-symbol
    ets_result = ex_top_symbol(all_trades)
    if isinstance(ets_result, tuple):
        ets_m, top_sym, top_pnl = ets_result
        print(f"    Top symbol: {top_sym} (+{top_pnl:.2f}R)")
        print_metrics("  Ex-top-symbol", ets_m, indent="    ")

    # Time distribution
    print(f"\n  Time distribution:")
    by_hour = defaultdict(list)
    for t in all_trades:
        h = (t.hhmm // 100) * 100
        by_hour[h].append(t)
    for h in sorted(by_hour.keys()):
        hm = metrics(by_hour[h])
        print(f"    {h:04d}-{h+59:04d}: N={hm['n']:>4d}  PF={pf(hm['pf_r']):>6s}  TotalR={hm['total_r']:>+8.2f}")

    # Unexpected setup check (signals from setups NOT in ACTIVE_SETUPS_V3)
    print(f"\n{'─'*80}")
    print(f"UNEXPECTED SIGNALS CHECK")
    print(f"{'─'*80}")

    unexpected_cfg = candidate_v3_sc_bdr_ep()
    all_signals = []
    for sym in symbols[:10]:  # spot check first 10
        bars = load_bars(sym)
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = load_bars(sec_etf) if sec_etf and sec_etf not in {"SPY", "QQQ"} else None
        result = run_backtest(bars, cfg=unexpected_cfg, spy_bars=spy_bars, qqq_bars=qqq_bars, sector_bars=sec_bars)
        for t in result.trades:
            et = EngTrade(symbol=sym, trade=t)
            if et.setup_name not in ACTIVE_SETUPS_V3:
                all_signals.append(et)
    if all_signals:
        print(f"  WARNING: {len(all_signals)} unexpected trades from non-active setups:")
        by_setup = defaultdict(int)
        for t in all_signals:
            by_setup[t.setup_name] += 1
        for name, ct in sorted(by_setup.items()):
            print(f"    {name}: {ct} trades")
    else:
        print(f"  CLEAN: No unexpected signals from non-active setups (checked 10 symbols)")

    # Comparison to isolated baseline
    print(f"\n{'─'*80}")
    print(f"COMPARISON: Integrated vs Isolated-Sum Baseline")
    print(f"{'─'*80}")
    print(f"  {'Metric':<25s} {'Baseline':>12s} {'Integrated':>12s} {'Delta':>12s}")
    baseline = {"n": 163, "pf_r": 1.38, "total_r": 43.29, "exp_r": 0.266, "max_dd_r": 15.65}
    for key, label in [("n", "Trades"), ("pf_r", "PF(R)"), ("total_r", "TotalR"),
                        ("exp_r", "Exp(R)"), ("max_dd_r", "MaxDD(R)")]:
        bv = baseline[key]
        iv = m[key]
        delta = iv - bv
        if key == "n":
            print(f"  {label:<25s} {bv:>12d} {iv:>12d} {delta:>+12d}")
        else:
            print(f"  {label:<25s} {bv:>12.2f} {iv:>12.2f} {delta:>+12.2f}")

    print(f"\n  Train/Test comparison:")
    print(f"  {'':>25s} {'Baseline':>12s} {'Integrated':>12s}")
    print(f"  {'Train PF':>25s} {'1.04':>12s} {pf(mt['pf_r']):>12s}")
    print(f"  {'Test PF':>25s} {'1.84':>12s} {pf(me['pf_r']):>12s}")
    print(f"  {'Train TotalR':>25s} {'+2.50':>12s} {mt['total_r']:>+12.2f}")
    print(f"  {'Test TotalR':>25s} {'+40.79':>12s} {me['total_r']:>+12.2f}")

    print(f"\n{'='*80}")
    verdict = "PASS" if m["pf_r"] >= 1.20 and m["total_r"] > 0 else "FAIL"
    print(f"VERDICT: {verdict}")
    if m["pf_r"] >= 1.20:
        print(f"  PF {pf(m['pf_r'])} >= 1.20 threshold ✓")
    else:
        print(f"  PF {pf(m['pf_r'])} < 1.20 threshold ✗")
    print(f"{'='*80}")


if __name__ == "__main__":
    run_integrated()
