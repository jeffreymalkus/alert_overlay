"""
Portfolio Capacity Sensitivity Study

Runs each setup in ISOLATION to collect the full universe of possible trades
(no intra-symbol blocking), then simulates portfolio-wide capacity rules.

Key insight: the current backtest applies a 1-trade-at-a-time constraint
PER SYMBOL. This study decouples that and tests portfolio-wide limits.

Capacity models tested:
  1. max_open=1 (portfolio-wide — extremely restrictive)
  2. max_open=2
  3. max_open=3
  4. max_open=4
  5. 1 long + 1 short simultaneously
  6. max_open=4, no same-symbol overlap
  7. unlimited (all isolated trades, no blocking)
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from .backtest import run_backtest, load_bars_from_csv, Trade
from .config import OverlayConfig
from .market_context import get_sector_etf, SECTOR_MAP
from .models import NaN, SetupId, SETUP_DISPLAY_NAME

DATA_DIR = Path(__file__).parent / "data"
_isnan = math.isnan


def load_bars(sym):
    p = DATA_DIR / f"{sym}_5min.csv"
    return load_bars_from_csv(str(p)) if p.exists() else []


def get_universe():
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    return sorted([p.stem.replace("_5min", "") for p in DATA_DIR.glob("*_5min.csv")
                   if p.stem.replace("_5min", "") not in excluded])


@dataclass
class PortTrade:
    """A trade with full entry/exit timing for portfolio simulation."""
    symbol: str
    setup_name: str
    direction: int       # 1=long, -1=short
    entry_time: datetime
    exit_time: datetime
    pnl_rr: float
    exit_reason: str
    bars_held: int

    @property
    def entry_date(self): return self.entry_time.date()


def _disable_all(cfg):
    cfg.show_reversal_setups = False
    cfg.show_trend_setups = False
    cfg.show_ema_retest = False
    cfg.show_ema_mean_rev = False
    cfg.show_ema_pullback = False
    cfg.show_second_chance = False
    cfg.show_sc_v2 = False
    cfg.show_spencer = False
    cfg.show_ema_scalp = False
    cfg.show_ema_fpip = False
    cfg.show_ema_confirm = False
    cfg.show_mcs = False
    cfg.show_vwap_reclaim = False
    cfg.show_vka = False
    cfg.show_failed_bounce = False
    cfg.show_breakdown_retest = False
    return cfg


def cfg_sc_isolated():
    """SC long, quality>=5, no regime, cost-on."""
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)
    cfg.show_second_chance = True
    cfg.sc_long_only = True
    cfg.sc_min_quality = 5
    cfg.sc_require_regime = False
    cfg.require_regime = False
    cfg.min_quality = 5        # global quality matches SC
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    return cfg


def cfg_bdr_isolated():
    """BDR short, regime required, cost-on."""
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)
    cfg.show_breakdown_retest = True
    cfg.bdr_require_regime = True
    cfg.require_regime = True  # BDR needs regime
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    return cfg


def cfg_ep_isolated():
    """EP short, early session (<14:00), no regime, cost-on."""
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)
    cfg.show_ema_pullback = True
    # Decoupled: no longer needs show_trend_setups
    cfg.ep_short_only = True
    cfg.ep_time_end = 1400
    cfg.ep_require_regime = False
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    return cfg


def collect_isolated_trades(cfg, symbols, spy_bars, qqq_bars, setup_filter=None, direction_filter=None):
    """Run backtest per symbol, collect trades with full timing."""
    trades = []
    for sym in symbols:
        bars = load_bars(sym)
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = load_bars(sec_etf) if sec_etf and sec_etf not in {"SPY", "QQQ"} else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars, sector_bars=sec_bars)
        for t in result.trades:
            sname = SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id))
            if setup_filter and sname != setup_filter:
                continue
            if direction_filter and t.signal.direction != direction_filter:
                continue
            pt = PortTrade(
                symbol=sym,
                setup_name=sname,
                direction=t.signal.direction,
                entry_time=t.signal.timestamp,
                exit_time=t.exit_time,
                pnl_rr=t.pnl_rr,
                exit_reason=t.exit_reason,
                bars_held=t.bars_held,
            )
            trades.append(pt)
    return trades


def simulate_capacity(all_trades: List[PortTrade], model: str, max_open: int = 1) -> List[PortTrade]:
    """
    Simulate portfolio capacity by filtering trades that can co-exist.

    Models:
      "max_N"        — max N concurrent trades, any direction
      "1L_1S"        — max 1 long + 1 short simultaneously
      "max_N_nosym"  — max N concurrent, no same-symbol overlap
      "unlimited"    — all trades taken (no blocking)
    """
    if model == "unlimited":
        return list(all_trades)

    # Sort by entry time
    sorted_trades = sorted(all_trades, key=lambda t: t.entry_time)
    taken = []
    open_trades: List[PortTrade] = []

    for t in sorted_trades:
        # Close expired trades
        open_trades = [ot for ot in open_trades if ot.exit_time > t.entry_time]

        # Check capacity
        can_take = False

        if model == "max_N":
            can_take = len(open_trades) < max_open

        elif model == "1L_1S":
            open_long = sum(1 for ot in open_trades if ot.direction == 1)
            open_short = sum(1 for ot in open_trades if ot.direction == -1)
            if t.direction == 1:
                can_take = open_long < 1
            else:
                can_take = open_short < 1

        elif model == "max_N_nosym":
            sym_open = any(ot.symbol == t.symbol for ot in open_trades)
            can_take = len(open_trades) < max_open and not sym_open

        if can_take:
            taken.append(t)
            open_trades.append(t)

    return taken


def metrics(trades: List[PortTrade]) -> dict:
    if not trades:
        return {"n": 0, "pf_r": 0, "total_r": 0, "exp_r": 0, "win_rate": 0,
                "avg_win_r": 0, "avg_loss_r": 0, "max_dd_r": 0,
                "stop_rate": 0, "quick_stop_rate": 0, "recovery_ratio": 0}
    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    gross_w = sum(t.pnl_rr for t in wins)
    gross_l = abs(sum(t.pnl_rr for t in losses))
    total = sum(t.pnl_rr for t in trades)
    n = len(trades)
    cum = peak = max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.entry_time):
        cum += t.pnl_rr
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    stops = [t for t in trades if t.exit_reason == "stop"]
    quick = [t for t in trades if t.exit_reason == "stop" and t.bars_held <= 2]
    recovery = total / max_dd if max_dd > 0 else float('inf')
    return {
        "n": n,
        "pf_r": gross_w / gross_l if gross_l > 0 else float('inf'),
        "total_r": total,
        "exp_r": total / n,
        "win_rate": len(wins) / n * 100,
        "avg_win_r": gross_w / len(wins) if wins else 0,
        "avg_loss_r": -gross_l / len(losses) if losses else 0,
        "max_dd_r": max_dd,
        "stop_rate": len(stops) / n * 100,
        "quick_stop_rate": len(quick) / n * 100,
        "recovery_ratio": recovery,
    }


def pf(v): return "Inf" if v == float('inf') else f"{v:.2f}"
def rr(v): return "Inf" if v == float('inf') else f"{v:.1f}"


def split_train_test(trades):
    train = [t for t in trades if t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date.day % 2 == 0]
    return train, test


def print_metrics(label, m, indent="  "):
    print(f"{indent}{label:<35s}  N={m['n']:>5d}  PF={pf(m['pf_r']):>6s}  "
          f"TotalR={m['total_r']:>+9.2f}  Exp={m['exp_r']:>+7.3f}  "
          f"WR={m['win_rate']:>5.1f}%  MaxDD={m['max_dd_r']:>7.2f}  "
          f"RecRatio={rr(m['recovery_ratio']):>5s}")


def print_model_report(label, taken, all_trades):
    m = metrics(taken)
    print(f"\n{'─'*80}")
    print(f"  {label}")
    print(f"{'─'*80}")
    print_metrics("COMBINED", m)

    # Per-setup
    by_setup = defaultdict(list)
    for t in taken:
        by_setup[t.setup_name].append(t)
    for name in sorted(by_setup.keys()):
        sm = metrics(by_setup[name])
        # How many of this setup were available vs taken
        avail = sum(1 for t in all_trades if t.setup_name == name)
        print(f"    {name:<20s}  taken={sm['n']:>4d}/{avail:<4d}  "
              f"PF={pf(sm['pf_r']):>6s}  TotalR={sm['total_r']:>+8.2f}  Exp={sm['exp_r']:>+7.3f}")

    # Train/test
    train, test = split_train_test(taken)
    mt = metrics(train)
    me = metrics(test)
    print(f"    Train: N={mt['n']:>4d}  PF={pf(mt['pf_r']):>6s}  TotalR={mt['total_r']:>+8.2f}")
    print(f"    Test:  N={me['n']:>4d}  PF={pf(me['pf_r']):>6s}  TotalR={me['total_r']:>+8.2f}")

    # Max concurrent
    events = []
    for t in taken:
        events.append((t.entry_time, +1, t))
        events.append((t.exit_time, -1, t))
    events.sort(key=lambda x: (x[0], x[1]))
    cur = max_conc = 0
    for _, delta, _ in events:
        cur += delta
        max_conc = max(max_conc, cur)
    print(f"    Max concurrent positions observed: {max_conc}")

    return m


def run_capacity_study():
    symbols = get_universe()
    spy_bars = load_bars("SPY")
    qqq_bars = load_bars("QQQ")

    print(f"{'='*80}")
    print(f"PORTFOLIO CAPACITY SENSITIVITY STUDY")
    print(f"{'='*80}")
    print(f"Universe: {len(symbols)} symbols")
    print()

    # ── Collect all isolated trades ──
    print("Collecting isolated trades per setup (no intra-symbol blocking)...")

    sc_trades = collect_isolated_trades(
        cfg_sc_isolated(), symbols, spy_bars, qqq_bars,
        setup_filter="2ND CHANCE", direction_filter=1)
    print(f"  SC long:     {len(sc_trades)} trades")

    bdr_trades = collect_isolated_trades(
        cfg_bdr_isolated(), symbols, spy_bars, qqq_bars,
        setup_filter="BDR SHORT", direction_filter=-1)
    print(f"  BDR short:   {len(bdr_trades)} trades")

    ep_trades = collect_isolated_trades(
        cfg_ep_isolated(), symbols, spy_bars, qqq_bars,
        setup_filter="EMA PULL", direction_filter=-1)
    print(f"  EP short:    {len(ep_trades)} trades")

    all_trades = sc_trades + bdr_trades + ep_trades
    all_trades.sort(key=lambda t: t.entry_time)
    print(f"  TOTAL:       {len(all_trades)} isolated trades")

    # ── Baseline: unlimited (isolated-sum) ──
    m_unlimited = print_model_report(
        "MODEL 0: UNLIMITED (isolated-sum, no capacity limit)",
        all_trades, all_trades)

    # ── Model 1: max 1 open ──
    taken_1 = simulate_capacity(all_trades, "max_N", max_open=1)
    m1 = print_model_report(
        "MODEL 1: MAX 1 OPEN TRADE (portfolio-wide)",
        taken_1, all_trades)

    # ── Model 2: max 2 open ──
    taken_2 = simulate_capacity(all_trades, "max_N", max_open=2)
    m2 = print_model_report(
        "MODEL 2: MAX 2 OPEN TRADES",
        taken_2, all_trades)

    # ── Model 3: 1 long + 1 short ──
    taken_1L1S = simulate_capacity(all_trades, "1L_1S")
    m3 = print_model_report(
        "MODEL 3: MAX 1 LONG + 1 SHORT",
        taken_1L1S, all_trades)

    # ── Model 4: max 3 open ──
    taken_3 = simulate_capacity(all_trades, "max_N", max_open=3)
    m4 = print_model_report(
        "MODEL 4: MAX 3 OPEN TRADES",
        taken_3, all_trades)

    # ── Model 5: max 4 open ──
    taken_4 = simulate_capacity(all_trades, "max_N", max_open=4)
    m5 = print_model_report(
        "MODEL 5: MAX 4 OPEN TRADES",
        taken_4, all_trades)

    # ── Model 6: max 4 open, no same-symbol ──
    taken_4ns = simulate_capacity(all_trades, "max_N_nosym", max_open=4)
    m6 = print_model_report(
        "MODEL 6: MAX 4 OPEN, NO SAME-SYMBOL OVERLAP",
        taken_4ns, all_trades)

    # ── Summary comparison table ──
    print(f"\n{'='*80}")
    print(f"SUMMARY COMPARISON")
    print(f"{'='*80}")
    print(f"  {'Model':<40s} {'N':>5s} {'PF':>6s} {'TotalR':>9s} {'Exp':>7s} "
          f"{'MaxDD':>7s} {'RecR':>5s} {'EP':>4s}")
    print(f"  {'-'*40} {'-'*5} {'-'*6} {'-'*9} {'-'*7} {'-'*7} {'-'*5} {'-'*4}")

    models = [
        ("0: Unlimited (isolated-sum)", m_unlimited, all_trades),
        ("1: Max 1 open", m1, taken_1),
        ("2: Max 2 open", m2, taken_2),
        ("3: 1 long + 1 short", m3, taken_1L1S),
        ("4: Max 3 open", m4, taken_3),
        ("5: Max 4 open", m5, taken_4),
        ("6: Max 4, no same-sym", m6, taken_4ns),
    ]
    for label, m, trades in models:
        ep_ct = sum(1 for t in trades if t.setup_name == "EMA PULL")
        print(f"  {label:<40s} {m['n']:>5d} {pf(m['pf_r']):>6s} {m['total_r']:>+9.2f} "
              f"{m['exp_r']:>+7.3f} {m['max_dd_r']:>7.2f} {rr(m['recovery_ratio']):>5s} "
              f"{ep_ct:>4d}")

    # ── Current integrated engine result for reference ──
    print(f"\n  {'Ref: Integrated engine (1-per-sym)':<40s} {'105':>5s} {'1.37':>6s} "
          f"{'+20.44':>9s} {'+0.195':>7s} {'12.99':>7s} {'1.6':>5s} {'14':>4s}")

    print(f"\n{'='*80}")
    print(f"KEY TAKEAWAYS")
    print(f"{'='*80}")

    # Find best model (highest total R with PF >= 1.20)
    valid = [(l, m, t) for l, m, t in models if m['pf_r'] >= 1.20 and m['n'] > 0]
    if valid:
        best = max(valid, key=lambda x: x[1]['total_r'])
        print(f"  Best viable model: {best[0]}")
        print(f"    PF={pf(best[1]['pf_r'])}, TotalR={best[1]['total_r']:+.2f}, "
              f"MaxDD={best[1]['max_dd_r']:.2f}, RecoveryRatio={rr(best[1]['recovery_ratio'])}")

    # EP recovery comparison
    ep_baseline = sum(1 for t in all_trades if t.setup_name == "EMA PULL")
    print(f"\n  EP trade recovery:")
    for label, m, trades in models:
        ep_ct = sum(1 for t in trades if t.setup_name == "EMA PULL")
        print(f"    {label:<40s} EP={ep_ct:>3d}/{ep_baseline}")

    print(f"\n  Note: 'Integrated engine' result uses per-symbol 1-at-a-time,")
    print(f"  which is different from portfolio-wide limits. The engine blocks")
    print(f"  trades only on the SAME SYMBOL, not cross-symbol.")
    print(f"{'='*80}")


if __name__ == "__main__":
    run_capacity_study()
