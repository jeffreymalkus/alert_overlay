"""
Candidate Scan — Run ALL enabled setups through the engine and rank by raw edge.

For each setup, reports:
  - Raw (no slippage): PF(R), Exp(R), TotalR, N trades
  - Cost-on (realistic slippage): PF(R), Exp(R), TotalR
  - Max DD(R)
  - Stop rate, quick-stop rate (stopped out in ≤2 bars)
  - Win rate, avg winner R, avg loser R

Purpose: Find which setups have enough raw edge to survive realistic friction,
         replacing VR as the primary long-side development candidate.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional

from .backtest import run_backtest, load_bars_from_csv, Trade
from .config import OverlayConfig
from .market_context import get_sector_etf, SECTOR_MAP
from .models import NaN, SetupId, SETUP_DISPLAY_NAME

DATA_DIR = Path(__file__).parent / "data"
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


def make_all_setups_cfg(with_slippage: bool = True) -> OverlayConfig:
    """Enable ALL setups for comprehensive scan."""
    cfg = OverlayConfig()

    # Long setups
    cfg.show_reversal_setups = True      # BOX_REV, MANIP, VWAP_SEP
    cfg.show_trend_setups = True         # VWAP_KISS
    cfg.show_ema_retest = True           # EMA_RETEST
    cfg.show_ema_mean_rev = True         # EMA9_SEP
    cfg.show_ema_pullback = True         # EMA_PULL
    cfg.show_second_chance = True        # SECOND_CHANCE
    cfg.show_sc_v2 = True               # SC_V2
    cfg.show_spencer = True             # SPENCER
    cfg.show_ema_scalp = True           # EMA_RECLAIM
    cfg.show_ema_fpip = True            # EMA_FPIP
    cfg.show_ema_confirm = True         # EMA_CONFIRM
    cfg.show_mcs = True                 # MCS
    cfg.show_vwap_reclaim = True        # VWAP_RECLAIM
    cfg.show_vka = True                 # VKA

    # Short setups
    cfg.show_failed_bounce = True       # FAILED_BOUNCE
    cfg.show_breakdown_retest = True    # BDR_SHORT

    # Long-only constraints
    cfg.vk_long_only = True
    cfg.sc_long_only = True
    cfg.vr_long_only = True
    cfg.vka_long_only = True

    # VR canonical params
    cfg.vr_time_start = 1000
    cfg.vr_time_end = 1059
    cfg.vr_hold_bars = 3
    cfg.vr_target_rr = 3.0
    cfg.vr_min_body_pct = 0.40
    cfg.vr_require_bull = True
    cfg.vr_require_vol = True
    cfg.vr_vol_frac = 0.70
    cfg.vr_stop_buffer = 0.02
    cfg.vr_day_filter = "none"          # No day filter for raw scan
    cfg.vr_require_market_align = False  # No market align for raw scan

    # VKA canonical params
    cfg.vka_time_start = 1000
    cfg.vka_time_end = 1059
    cfg.vka_hold_bars = 2
    cfg.vka_target_rr = 2.0
    cfg.vka_min_body_pct = 0.40
    cfg.vka_require_bull = True
    cfg.vka_require_vol = True
    cfg.vka_vol_frac = 0.70
    cfg.vka_kiss_atr_frac = 0.05
    cfg.vka_stop_buffer = 0.02
    cfg.vka_day_filter = "none"
    cfg.vka_require_market_align = False

    # BDR canonical params
    cfg.bdr_require_red_trend = True
    cfg.bdr_am_only = True
    cfg.bdr_am_cutoff = 1100
    cfg.bdr_min_rejection_wick_pct = 0.30
    cfg.bdr_time_stop_bars = 8
    cfg.bdr_exit_mode = "time"

    # Gate settings: MINIMAL gates for raw scan
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0

    if not with_slippage:
        cfg.use_dynamic_slippage = False
        cfg.slippage_per_side = 0.0
        cfg.commission_per_share = 0.0

    return cfg


def run_all_setups(cfg: OverlayConfig, symbols, spy_bars, qqq_bars) -> List[EngTrade]:
    trades = []
    for sym in symbols:
        bars = load_bars(sym)
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = load_bars(sec_etf) if sec_etf and sec_etf not in {"SPY", "QQQ"} else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars,
                              qqq_bars=qqq_bars, sector_bars=sec_bars)
        for t in result.trades:
            trades.append(EngTrade(symbol=sym, trade=t))
    return trades


def compute_metrics(trades: List[EngTrade]) -> dict:
    if not trades:
        return {"n": 0, "pf_r": 0.0, "total_r": 0.0, "exp_r": 0.0,
                "win_rate": 0.0, "avg_win_r": 0.0, "avg_loss_r": 0.0,
                "max_dd_r": 0.0, "stop_rate": 0.0, "quick_stop_rate": 0.0,
                "target_rate": 0.0}

    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    stops = [t for t in trades if t.exit_reason == "stop"]
    quick_stops = [t for t in trades if t.exit_reason == "stop" and t.bars_held <= 2]
    targets = [t for t in trades if t.exit_reason == "target"]

    gross_win = sum(t.pnl_rr for t in wins)
    gross_loss = abs(sum(t.pnl_rr for t in losses))
    total_r = sum(t.pnl_rr for t in trades)

    # Max drawdown in R
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cum += t.pnl_rr
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    n = len(trades)
    return {
        "n": n,
        "pf_r": gross_win / gross_loss if gross_loss > 0 else float('inf'),
        "total_r": total_r,
        "exp_r": total_r / n,
        "win_rate": len(wins) / n * 100,
        "avg_win_r": gross_win / len(wins) if wins else 0.0,
        "avg_loss_r": -gross_loss / len(losses) if losses else 0.0,
        "max_dd_r": max_dd,
        "stop_rate": len(stops) / n * 100,
        "quick_stop_rate": len(quick_stops) / n * 100,
        "target_rate": len(targets) / n * 100,
    }


def pf_str(pf: float) -> str:
    return "Inf" if pf == float('inf') else f"{pf:.2f}"


def main():
    symbols = get_universe()
    spy_bars = load_bars("SPY")
    qqq_bars = load_bars("QQQ")

    print("=" * 160)
    print("CANDIDATE SCAN — All setups, raw edge vs cost-on")
    print("=" * 160)
    print(f"Universe: {len(symbols)} symbols\n")

    # Run with slippage OFF (raw) and ON (cost-on)
    print("  Running raw scan (no slippage)...")
    cfg_raw = make_all_setups_cfg(with_slippage=False)
    trades_raw = run_all_setups(cfg_raw, symbols, spy_bars, qqq_bars)

    print("  Running cost-on scan (realistic slippage)...")
    cfg_cost = make_all_setups_cfg(with_slippage=True)
    trades_cost = run_all_setups(cfg_cost, symbols, spy_bars, qqq_bars)

    # Group by setup name
    raw_by_setup: Dict[str, List[EngTrade]] = defaultdict(list)
    for t in trades_raw:
        raw_by_setup[t.setup_name].append(t)

    cost_by_setup: Dict[str, List[EngTrade]] = defaultdict(list)
    for t in trades_cost:
        cost_by_setup[t.setup_name].append(t)

    # Also split by direction
    raw_long = defaultdict(list)
    raw_short = defaultdict(list)
    for t in trades_raw:
        if t.direction == 1:
            raw_long[t.setup_name].append(t)
        else:
            raw_short[t.setup_name].append(t)

    cost_long = defaultdict(list)
    cost_short = defaultdict(list)
    for t in trades_cost:
        if t.direction == 1:
            cost_long[t.setup_name].append(t)
        else:
            cost_short[t.setup_name].append(t)

    # Get all setup names
    all_setups = sorted(set(list(raw_by_setup.keys()) + list(cost_by_setup.keys())))

    # ── COMBINED TABLE ──
    print(f"\n{'=' * 160}")
    print("ALL SETUPS — COMBINED (Long + Short)")
    print(f"{'=' * 160}")
    header = (f"  {'Setup':<16s} {'Dir':>5s} │ {'N':>5s} {'PF(R)':>7s} {'TotalR':>9s} {'Exp(R)':>8s} "
              f"{'WR%':>6s} {'AvgW':>6s} {'AvgL':>7s} {'MaxDD':>7s} {'Stop%':>6s} {'QStop%':>7s} {'Tgt%':>6s} "
              f"│ {'N$':>5s} {'PF$(R)':>7s} {'TotR$':>9s} {'Exp$(R)':>8s} {'MaxDD$':>7s} │ {'Verdict':>12s}")
    print(header)
    print("  " + "─" * 156)

    results = []
    for setup in all_setups:
        raw_trades = raw_by_setup.get(setup, [])
        cost_trades = cost_by_setup.get(setup, [])
        m_raw = compute_metrics(raw_trades)
        m_cost = compute_metrics(cost_trades)

        # Determine direction
        longs = sum(1 for t in raw_trades if t.direction == 1)
        shorts = sum(1 for t in raw_trades if t.direction == -1)
        dir_str = "LONG" if longs > shorts else "SHORT" if shorts > longs else "BOTH"

        # Verdict
        if m_raw["n"] < 20:
            verdict = "TOO FEW"
        elif m_cost["pf_r"] >= 1.10:
            verdict = "PROMISING"
        elif m_cost["pf_r"] >= 1.00:
            verdict = "MARGINAL"
        elif m_raw["pf_r"] >= 1.15:
            verdict = "RAW ONLY"
        else:
            verdict = "WEAK"

        results.append((setup, dir_str, m_raw, m_cost, verdict))

        print(f"  {setup:<16s} {dir_str:>5s} │ "
              f"{m_raw['n']:>5d} {pf_str(m_raw['pf_r']):>7s} {m_raw['total_r']:>+9.2f} {m_raw['exp_r']:>+8.3f} "
              f"{m_raw['win_rate']:>5.1f}% {m_raw['avg_win_r']:>6.2f} {m_raw['avg_loss_r']:>7.2f} "
              f"{m_raw['max_dd_r']:>7.2f} {m_raw['stop_rate']:>5.1f}% {m_raw['quick_stop_rate']:>6.1f}% "
              f"{m_raw['target_rate']:>5.1f}% │ "
              f"{m_cost['n']:>5d} {pf_str(m_cost['pf_r']):>7s} {m_cost['total_r']:>+9.2f} "
              f"{m_cost['exp_r']:>+8.3f} {m_cost['max_dd_r']:>7.2f} │ {verdict:>12s}")

    # ── RANKED BY RAW PF ──
    print(f"\n{'=' * 160}")
    print("RANKED BY RAW PF(R) — Minimum 20 trades")
    print(f"{'=' * 160}")
    ranked = [(s, d, mr, mc, v) for s, d, mr, mc, v in results if mr["n"] >= 20]
    ranked.sort(key=lambda x: x[2]["pf_r"], reverse=True)

    print(f"\n  {'Rank':>4s}  {'Setup':<16s} {'Dir':>5s}  {'N_raw':>5s}  {'PF_raw':>7s}  {'TotR_raw':>9s}  "
          f"{'PF_cost':>7s}  {'TotR_cost':>9s}  {'Friction':>9s}  {'MaxDD_cost':>9s}  {'Verdict':>12s}")
    print("  " + "─" * 120)
    for i, (setup, dir_str, m_raw, m_cost, verdict) in enumerate(ranked, 1):
        friction = m_raw["total_r"] - m_cost["total_r"]
        print(f"  {i:>4d}  {setup:<16s} {dir_str:>5s}  {m_raw['n']:>5d}  {pf_str(m_raw['pf_r']):>7s}  "
              f"{m_raw['total_r']:>+9.2f}  {pf_str(m_cost['pf_r']):>7s}  {m_cost['total_r']:>+9.2f}  "
              f"{friction:>+9.2f}  {m_cost['max_dd_r']:>9.2f}  {verdict:>12s}")

    # ── LONG-ONLY TABLE ──
    print(f"\n{'=' * 160}")
    print("LONG-ONLY SETUPS — Ranked by raw PF(R)")
    print(f"{'=' * 160}")

    long_results = []
    for setup in all_setups:
        raw_l = raw_long.get(setup, [])
        cost_l = cost_long.get(setup, [])
        if not raw_l:
            continue
        m_raw = compute_metrics(raw_l)
        m_cost = compute_metrics(cost_l)
        if m_raw["n"] < 10:
            continue
        long_results.append((setup, m_raw, m_cost))

    long_results.sort(key=lambda x: x[1]["pf_r"], reverse=True)

    print(f"\n  {'Rank':>4s}  {'Setup':<16s}  {'N_raw':>5s}  {'PF_raw':>7s}  {'TotR_raw':>9s}  {'Exp_raw':>8s}  "
          f"{'PF_cost':>7s}  {'TotR_cost':>9s}  {'Exp_cost':>8s}  {'MaxDD_cost':>9s}  {'StopR%':>6s}  {'TgtR%':>6s}")
    print("  " + "─" * 120)
    for i, (setup, m_raw, m_cost) in enumerate(long_results, 1):
        print(f"  {i:>4d}  {setup:<16s}  {m_raw['n']:>5d}  {pf_str(m_raw['pf_r']):>7s}  "
              f"{m_raw['total_r']:>+9.2f}  {m_raw['exp_r']:>+8.3f}  "
              f"{pf_str(m_cost['pf_r']):>7s}  {m_cost['total_r']:>+9.2f}  "
              f"{m_cost['exp_r']:>+8.3f}  {m_cost['max_dd_r']:>9.2f}  "
              f"{m_raw['stop_rate']:>5.1f}% {m_raw['target_rate']:>5.1f}%")

    # ── SHORT-ONLY TABLE ──
    print(f"\n{'=' * 160}")
    print("SHORT-ONLY SETUPS — Ranked by raw PF(R)")
    print(f"{'=' * 160}")

    short_results = []
    for setup in all_setups:
        raw_s = raw_short.get(setup, [])
        cost_s = cost_short.get(setup, [])
        if not raw_s:
            continue
        m_raw = compute_metrics(raw_s)
        m_cost = compute_metrics(cost_s)
        if m_raw["n"] < 10:
            continue
        short_results.append((setup, m_raw, m_cost))

    short_results.sort(key=lambda x: x[1]["pf_r"], reverse=True)

    print(f"\n  {'Rank':>4s}  {'Setup':<16s}  {'N_raw':>5s}  {'PF_raw':>7s}  {'TotR_raw':>9s}  {'Exp_raw':>8s}  "
          f"{'PF_cost':>7s}  {'TotR_cost':>9s}  {'Exp_cost':>8s}  {'MaxDD_cost':>9s}  {'StopR%':>6s}  {'TgtR%':>6s}")
    print("  " + "─" * 120)
    for i, (setup, m_raw, m_cost) in enumerate(short_results, 1):
        print(f"  {i:>4d}  {setup:<16s}  {m_raw['n']:>5d}  {pf_str(m_raw['pf_r']):>7s}  "
              f"{m_raw['total_r']:>+9.2f}  {m_raw['exp_r']:>+8.3f}  "
              f"{pf_str(m_cost['pf_r']):>7s}  {m_cost['total_r']:>+9.2f}  "
              f"{m_cost['exp_r']:>+8.3f}  {m_cost['max_dd_r']:>9.2f}  "
              f"{m_raw['stop_rate']:>5.1f}% {m_raw['target_rate']:>5.1f}%")

    # ── VR REFERENCE (for comparison threshold) ──
    vr_raw = raw_by_setup.get("VWAP RECLAIM", [])
    vr_cost = cost_by_setup.get("VWAP RECLAIM", [])
    vr_mr = compute_metrics(vr_raw)
    vr_mc = compute_metrics(vr_cost)
    print(f"\n{'=' * 160}")
    print(f"VR REFERENCE (promotion threshold):  Raw PF={pf_str(vr_mr['pf_r'])}, "
          f"TotalR={vr_mr['total_r']:+.2f}  |  Cost PF={pf_str(vr_mc['pf_r'])}, "
          f"TotalR={vr_mc['total_r']:+.2f}")
    print(f"A candidate must COMFORTABLY exceed raw PF {pf_str(vr_mr['pf_r'])} "
          f"and have a plausible path to PF>1.0 after costs.")
    print(f"{'=' * 160}")


if __name__ == "__main__":
    main()
