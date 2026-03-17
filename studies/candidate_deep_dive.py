"""
Candidate Deep Dive — Isolated single-setup analysis for top candidates.

Runs each candidate ALONE (no competing setups) to get clean metrics.
Includes train/test split, daily P&L curve, and gate sensitivity.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict

from ..backtest import run_backtest, load_bars_from_csv, Trade
from ..config import OverlayConfig
from ..market_context import get_sector_etf, SECTOR_MAP
from ..models import NaN, SetupId, SETUP_DISPLAY_NAME

DATA_DIR = Path(__file__).parent.parent / "data"
_isnan = math.isnan

def load_bars(sym): return load_bars_from_csv(str(DATA_DIR / f"{sym}_5min.csv")) if (DATA_DIR / f"{sym}_5min.csv").exists() else []

def get_universe():
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    return sorted([p.stem.replace("_5min", "") for p in DATA_DIR.glob("*_5min.csv") if p.stem.replace("_5min", "") not in excluded])


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


def run_single_setup(cfg, symbols, spy_bars, qqq_bars, setup_filter=None, direction_filter=None):
    trades = []
    for sym in symbols:
        bars = load_bars(sym)
        if not bars: continue
        sec_etf = get_sector_etf(sym)
        sec_bars = load_bars(sec_etf) if sec_etf and sec_etf not in {"SPY", "QQQ"} else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars, sector_bars=sec_bars)
        for t in result.trades:
            et = EngTrade(symbol=sym, trade=t)
            if setup_filter and et.setup_name != setup_filter:
                continue
            if direction_filter and et.direction != direction_filter:
                continue
            trades.append(et)
    return trades


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


def split_train_test(trades):
    """Odd dates = train, even dates = test."""
    train = [t for t in trades if t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date.day % 2 == 0]
    return train, test


def print_metrics(label, m, indent="  "):
    print(f"{indent}{label:<40s}  N={m['n']:>5d}  PF={pf(m['pf_r']):>6s}  "
          f"TotalR={m['total_r']:>+9.2f}  Exp={m['exp_r']:>+7.3f}  "
          f"WR={m['win_rate']:>5.1f}%  AvgW={m['avg_win_r']:>5.2f}  AvgL={m['avg_loss_r']:>6.2f}  "
          f"MaxDD={m['max_dd_r']:>7.2f}  Stop={m['stop_rate']:>5.1f}%  "
          f"QStop={m['quick_stop_rate']:>5.1f}%  Tgt={m['target_rate']:>5.1f}%  "
          f"EOD={m['eod_rate']:>5.1f}%")


def exit_reason_breakdown(trades, label):
    by_exit = defaultdict(list)
    for t in trades:
        by_exit[t.exit_reason].append(t)
    print(f"\n  Exit Reason Breakdown ({label}):")
    for reason in sorted(by_exit.keys()):
        group = by_exit[reason]
        m = metrics(group)
        print(f"    {reason:<12s}  N={m['n']:>5d}  PF={pf(m['pf_r']):>6s}  "
              f"TotalR={m['total_r']:>+8.2f}  Exp={m['exp_r']:>+7.3f}  WR={m['win_rate']:>5.1f}%")


def time_breakdown(trades, label):
    by_hour = defaultdict(list)
    for t in trades:
        h = (t.hhmm // 100) * 100
        by_hour[h].append(t)
    print(f"\n  Time Breakdown ({label}):")
    for h in sorted(by_hour.keys()):
        group = by_hour[h]
        m = metrics(group)
        print(f"    {h:04d}-{h+59:04d}  N={m['n']:>5d}  PF={pf(m['pf_r']):>6s}  "
              f"TotalR={m['total_r']:>+8.2f}  Exp={m['exp_r']:>+7.3f}  WR={m['win_rate']:>5.1f}%")


def symbol_breakdown(trades, label, top_n=10):
    by_sym = defaultdict(list)
    for t in trades:
        by_sym[t.symbol].append(t)
    ranked = sorted(by_sym.items(), key=lambda x: sum(t.pnl_rr for t in x[1]), reverse=True)
    print(f"\n  Top {top_n} Symbols by TotalR ({label}):")
    for sym, group in ranked[:top_n]:
        m = metrics(group)
        print(f"    {sym:<6s}  N={m['n']:>4d}  PF={pf(m['pf_r']):>6s}  TotalR={m['total_r']:>+8.2f}  Exp={m['exp_r']:>+7.3f}")
    print(f"  Bottom {top_n} Symbols:")
    for sym, group in ranked[-top_n:]:
        m = metrics(group)
        print(f"    {sym:<6s}  N={m['n']:>4d}  PF={pf(m['pf_r']):>6s}  TotalR={m['total_r']:>+8.2f}  Exp={m['exp_r']:>+7.3f}")


# ════════════════════════════════════════════════════════════════
#  CANDIDATE CONFIGS
# ════════════════════════════════════════════════════════════════

def cfg_ema_pull_short(slip=True):
    """EMA PULL — isolated, short-only."""
    cfg = OverlayConfig()
    # Disable everything
    cfg.show_reversal_setups = False
    cfg.show_trend_setups = False
    cfg.show_ema_retest = False
    cfg.show_ema_mean_rev = False
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
    # Enable ONLY EMA PULL (needs show_trend_setups as parent gate)
    cfg.show_ema_pullback = True
    cfg.show_trend_setups = True  # required: EMA_PULL is routed through trend signal path
    # Minimal gates
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    if not slip:
        cfg.use_dynamic_slippage = False
        cfg.slippage_per_side = 0.0
        cfg.commission_per_share = 0.0
    return cfg


def cfg_2nd_chance_long(slip=True):
    """2ND CHANCE — isolated, long-only."""
    cfg = OverlayConfig()
    cfg.show_reversal_setups = False
    cfg.show_trend_setups = False
    cfg.show_ema_retest = False
    cfg.show_ema_mean_rev = False
    cfg.show_ema_pullback = False
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
    cfg.show_second_chance = True
    cfg.sc_long_only = True
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    if not slip:
        cfg.use_dynamic_slippage = False
        cfg.slippage_per_side = 0.0
        cfg.commission_per_share = 0.0
    return cfg


def cfg_bdr_short(slip=True):
    """BDR SHORT — isolated, canonical params."""
    cfg = OverlayConfig()
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
    cfg.show_breakdown_retest = True
    cfg.bdr_require_red_trend = True
    cfg.bdr_am_only = True
    cfg.bdr_am_cutoff = 1100
    cfg.bdr_min_rejection_wick_pct = 0.30
    cfg.bdr_time_stop_bars = 8
    cfg.bdr_exit_mode = "time"
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    if not slip:
        cfg.use_dynamic_slippage = False
        cfg.slippage_per_side = 0.0
        cfg.commission_per_share = 0.0
    return cfg


def cfg_vka_long(slip=True):
    """VKA — isolated, canonical params, no day filter."""
    from .portfolio_configs import _canonical_vka
    cfg = OverlayConfig()
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
    cfg.show_failed_bounce = False
    cfg.show_breakdown_retest = False
    cfg = _canonical_vka(cfg)
    cfg.vka_day_filter = "none"
    cfg.vka_require_market_align = False
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    if not slip:
        cfg.use_dynamic_slippage = False
        cfg.slippage_per_side = 0.0
        cfg.commission_per_share = 0.0
    return cfg


def cfg_mcs_long(slip=True):
    """MCS — isolated."""
    cfg = OverlayConfig()
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
    cfg.show_mcs = True
    cfg.show_vwap_reclaim = False
    cfg.show_vka = False
    cfg.show_failed_bounce = False
    cfg.show_breakdown_retest = False
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    if not slip:
        cfg.use_dynamic_slippage = False
        cfg.slippage_per_side = 0.0
        cfg.commission_per_share = 0.0
    return cfg


CANDIDATES = [
    ("EMA PULL (short)", cfg_ema_pull_short, "EMA PULL", -1),
    ("2ND CHANCE (long)", cfg_2nd_chance_long, "2ND CHANCE", 1),
    ("BDR SHORT", cfg_bdr_short, "BDR SHORT", -1),
    ("VK ACCEPT (long)", cfg_vka_long, "VK ACCEPT", 1),
    ("MCS (long)", cfg_mcs_long, "MCS", 1),
]


def analyze_candidate(name, cfg_fn, setup_filter, dir_filter, symbols, spy_bars, qqq_bars):
    print(f"\n{'═' * 140}")
    print(f"  CANDIDATE: {name}")
    print(f"{'═' * 140}")

    # Raw
    cfg_raw = cfg_fn(slip=False)
    trades_raw = run_single_setup(cfg_raw, symbols, spy_bars, qqq_bars, setup_filter, dir_filter)
    m_raw = metrics(trades_raw)
    print_metrics("RAW (no slippage)", m_raw)

    # Cost-on
    cfg_cost = cfg_fn(slip=True)
    trades_cost = run_single_setup(cfg_cost, symbols, spy_bars, qqq_bars, setup_filter, dir_filter)
    m_cost = metrics(trades_cost)
    print_metrics("COST-ON (realistic slippage)", m_cost)

    # Friction
    friction = m_raw["total_r"] - m_cost["total_r"]
    print(f"\n  Friction: {friction:+.2f}R ({friction / m_raw['n']:.3f}R per trade)" if m_raw['n'] > 0 else "")

    # Train/Test split (on raw)
    train, test = split_train_test(trades_raw)
    m_train = metrics(train)
    m_test = metrics(test)
    print(f"\n  Train/Test Split (raw):")
    print_metrics("TRAIN (odd dates)", m_train, "    ")
    print_metrics("TEST  (even dates)", m_test, "    ")

    # Train/Test with costs
    train_c, test_c = split_train_test(trades_cost)
    m_train_c = metrics(train_c)
    m_test_c = metrics(test_c)
    print(f"\n  Train/Test Split (cost-on):")
    print_metrics("TRAIN (odd dates)", m_train_c, "    ")
    print_metrics("TEST  (even dates)", m_test_c, "    ")

    # Exit reason breakdown (raw)
    exit_reason_breakdown(trades_raw, "raw")

    # Time breakdown (raw)
    time_breakdown(trades_raw, "raw")

    # Symbol breakdown (raw)
    symbol_breakdown(trades_raw, "raw", top_n=5)

    # Gate sensitivity (cost-on base already has minimal gates)
    # Now test with typical gates turned ON
    print(f"\n  Gate Sensitivity (cost-on):")
    # With regime
    cfg_regime = cfg_fn(slip=True)
    cfg_regime.require_regime = True
    t_regime = run_single_setup(cfg_regime, symbols, spy_bars, qqq_bars, setup_filter, dir_filter)
    print_metrics("+ regime gate", metrics(t_regime), "    ")

    # With quality
    cfg_qual = cfg_fn(slip=True)
    cfg_qual.min_quality = 4
    t_qual = run_single_setup(cfg_qual, symbols, spy_bars, qqq_bars, setup_filter, dir_filter)
    print_metrics("+ quality gate (min=4)", metrics(t_qual), "    ")

    # With cooldown
    cfg_cool = cfg_fn(slip=True)
    cfg_cool.alert_cooldown_bars = 5
    t_cool = run_single_setup(cfg_cool, symbols, spy_bars, qqq_bars, setup_filter, dir_filter)
    print_metrics("+ cooldown gate (5 bars)", metrics(t_cool), "    ")

    # With risk floor
    cfg_risk = cfg_fn(slip=True)
    cfg_risk.min_stop_intra_atr_mult = 1.0
    t_risk = run_single_setup(cfg_risk, symbols, spy_bars, qqq_bars, setup_filter, dir_filter)
    print_metrics("+ risk floor (1.0 ATR)", metrics(t_risk), "    ")

    # All standard gates
    cfg_all = cfg_fn(slip=True)
    cfg_all.require_regime = True
    cfg_all.min_quality = 4
    cfg_all.alert_cooldown_bars = 5
    cfg_all.min_stop_intra_atr_mult = 1.0
    t_all = run_single_setup(cfg_all, symbols, spy_bars, qqq_bars, setup_filter, dir_filter)
    print_metrics("ALL STANDARD GATES ON", metrics(t_all), "    ")

    return m_raw, m_cost


def main():
    symbols = get_universe()
    spy_bars = load_bars("SPY")
    qqq_bars = load_bars("QQQ")

    print("=" * 140)
    print("CANDIDATE DEEP DIVE — Isolated single-setup analysis")
    print("=" * 140)
    print(f"Universe: {len(symbols)} symbols\n")

    summary = []
    for name, cfg_fn, setup_filter, dir_filter in CANDIDATES:
        print(f"\n  Running {name}...")
        m_raw, m_cost = analyze_candidate(name, cfg_fn, setup_filter, dir_filter, symbols, spy_bars, qqq_bars)
        summary.append((name, m_raw, m_cost))

    # Final summary
    print(f"\n{'═' * 140}")
    print("FINAL SUMMARY — Sorted by cost-on PF(R)")
    print(f"{'═' * 140}")
    summary.sort(key=lambda x: x[2]["pf_r"], reverse=True)
    print(f"\n  {'Rank':>4}  {'Candidate':<25s}  {'N_raw':>5}  {'PF_raw':>7}  {'TotR_raw':>9}  "
          f"{'N_cost':>5}  {'PF_cost':>7}  {'TotR_cost':>9}  {'Exp_cost':>8}  {'MaxDD_cost':>9}  {'Survives?':>10}")
    print("  " + "─" * 130)
    for i, (name, mr, mc) in enumerate(summary, 1):
        survives = "YES" if mc["pf_r"] >= 1.0 else "MAYBE" if mc["pf_r"] >= 0.90 else "NO"
        print(f"  {i:>4}  {name:<25s}  {mr['n']:>5}  {pf(mr['pf_r']):>7}  {mr['total_r']:>+9.2f}  "
              f"{mc['n']:>5}  {pf(mc['pf_r']):>7}  {mc['total_r']:>+9.2f}  {mc['exp_r']:>+8.3f}  "
              f"{mc['max_dd_r']:>9.2f}  {survives:>10}")

    print(f"\n{'═' * 140}")


if __name__ == "__main__":
    main()
