"""
Expanded Portfolio Study — All active strategies on 207-day dataset.

Runs the validated 3-setup model (SC long Q≥5, BDR SHORT regime-gated,
EMA PULL short early) plus the combined portfolio across the expanded
207-day dataset (2025-05-12 → 2026-03-09).

Metrics: full R-first suite (PF, Exp, TotalR, MaxDD, train/test,
ex-best-day, ex-top-symbol, stop/target rates, daily P&L distribution).

Usage:
    cd /path/to/project
    python -m alert_overlay.expanded_portfolio_study
"""

import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import NaN, SetupId, SETUP_DISPLAY_NAME
from .market_context import SECTOR_MAP, get_sector_etf

DATA_DIR = Path(__file__).parent / "data"


# ════════════════════════════════════════════════════════════════
#  Trade wrapper with metadata
# ════════════════════════════════════════════════════════════════

@dataclass
class PTrade:
    """Trade with symbol, setup, and daily metadata for portfolio analysis."""
    pnl_rr: float
    pnl_rr_raw: float  # before costs (estimate: add back cost impact)
    exit_reason: str
    bars_held: int
    entry_time: datetime
    entry_date: date
    side: str  # "LONG" or "SHORT"
    setup: str  # display name
    setup_id: SetupId
    symbol: str
    quality: int


# ════════════════════════════════════════════════════════════════
#  Metrics
# ════════════════════════════════════════════════════════════════

def _pf(trades: List[PTrade]) -> float:
    if not trades:
        return 0.0
    gw = sum(t.pnl_rr for t in trades if t.pnl_rr > 0)
    gl = abs(sum(t.pnl_rr for t in trades if t.pnl_rr <= 0))
    return gw / gl if gl > 0 else float("inf")


def compute_metrics(trades: List[PTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {k: 0 for k in [
            "n", "wr", "pf", "exp", "total_r", "max_dd_r",
            "train_pf", "test_pf", "ex_best_day_pf", "ex_top_sym_pf",
            "stop_rate", "qstop_rate", "target_rate", "time_rate",
            "avg_win_r", "avg_loss_r", "median_r",
            "days_active", "avg_per_day", "pct_pos_days", "median_daily_r",
        ]}

    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in trades)
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")

    # Avg win/loss
    avg_win = sum(t.pnl_rr for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnl_rr for t in losses) / len(losses) if losses else 0

    # Median R
    sorted_r = sorted(t.pnl_rr for t in trades)
    mid = n // 2
    median_r = sorted_r[mid] if n % 2 else (sorted_r[mid - 1] + sorted_r[mid]) / 2

    # Max drawdown in R
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: (x.entry_date, x.entry_time)):
        cum += t.pnl_rr
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    # Train/test: odd dates = train, even = test
    train = [t for t in trades if t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date.day % 2 == 0]
    train_pf = _pf(train)
    test_pf = _pf(test)

    # Ex-best-day
    day_r: Dict[date, float] = defaultdict(float)
    for t in trades:
        day_r[t.entry_date] += t.pnl_rr
    if day_r:
        best_day = max(day_r, key=day_r.get)
        ex_best = [t for t in trades if t.entry_date != best_day]
    else:
        ex_best = trades
    ex_best_day_pf = _pf(ex_best)

    # Ex-top-symbol
    sym_r: Dict[str, float] = defaultdict(float)
    for t in trades:
        sym_r[t.symbol] += t.pnl_rr
    if sym_r:
        top_sym = max(sym_r, key=sym_r.get)
        ex_top = [t for t in trades if t.symbol != top_sym]
    else:
        ex_top = trades
    ex_top_sym_pf = _pf(ex_top)

    # Exit breakdown
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    qstop = sum(1 for t in trades if t.exit_reason == "stop" and t.bars_held <= 3)
    target = sum(1 for t in trades if t.exit_reason == "target")
    timed = sum(1 for t in trades if t.exit_reason in ("time", "ema9trail", "eod"))

    # Daily stats
    days_active = len(set(t.entry_date for t in trades))
    avg_per_day = n / days_active if days_active else 0
    day_pnl = list(day_r.values())
    pos_days = sum(1 for d in day_pnl if d > 0)
    pct_pos = pos_days / len(day_pnl) * 100 if day_pnl else 0
    sorted_daily = sorted(day_pnl)
    md = len(sorted_daily) // 2
    median_daily = sorted_daily[md] if len(sorted_daily) % 2 else (
        (sorted_daily[md - 1] + sorted_daily[md]) / 2) if sorted_daily else 0

    return {
        "n": n, "wr": len(wins) / n * 100, "pf": pf, "exp": total_r / n,
        "total_r": total_r, "max_dd_r": max_dd,
        "train_pf": train_pf, "test_pf": test_pf,
        "ex_best_day_pf": ex_best_day_pf, "ex_top_sym_pf": ex_top_sym_pf,
        "stop_rate": stopped / n * 100, "qstop_rate": qstop / n * 100,
        "target_rate": target / n * 100, "time_rate": timed / n * 100,
        "avg_win_r": avg_win, "avg_loss_r": avg_loss, "median_r": median_r,
        "days_active": days_active, "avg_per_day": avg_per_day,
        "pct_pos_days": pct_pos, "median_daily_r": median_daily,
    }


def pf_str(v: float) -> str:
    return f"{v:.2f}" if v < 999 else "inf"


def fmt_full(label: str, m: dict) -> str:
    return (
        f"  {label:40s}  {m['n']:5d}  {m['days_active']:4d}  "
        f"{pf_str(m['pf']):>6s}  {m['exp']:+7.3f}  {m['total_r']:+8.1f}  "
        f"{m['max_dd_r']:7.1f}  {m['wr']:5.1f}%  {m['stop_rate']:5.1f}%  "
        f"{m['target_rate']:5.1f}%  "
        f"{pf_str(m['train_pf']):>6s}  {pf_str(m['test_pf']):>6s}  "
        f"{pf_str(m['ex_best_day_pf']):>6s}  {pf_str(m['ex_top_sym_pf']):>6s}  "
        f"{m['pct_pos_days']:5.1f}%  {m['median_daily_r']:+6.2f}"
    )


HEADER = (
    f"  {'Label':40s}  {'N':>5s}  {'Days':>4s}  "
    f"{'PF(R)':>6s}  {'Exp(R)':>7s}  {'TotalR':>8s}  "
    f"{'MaxDD':>7s}  {'WR%':>6s}  {'Stop%':>6s}  {'Tgt%':>6s}  "
    f"{'TrnPF':>6s}  {'TstPF':>6s}  "
    f"{'ExDay':>6s}  {'ExSym':>6s}  "
    f"{'%Pos':>6s}  {'MedDR':>6s}"
)
DIVIDER = "  " + "-" * 150


# ════════════════════════════════════════════════════════════════
#  Top-N concentration
# ════════════════════════════════════════════════════════════════

def top_concentration(trades: List[PTrade]) -> str:
    if not trades:
        return "  (no trades)"
    total_r = sum(t.pnl_rr for t in trades)

    # Top 3 days
    day_r: Dict[date, float] = defaultdict(float)
    for t in trades:
        day_r[t.entry_date] += t.pnl_rr
    top_days = sorted(day_r.items(), key=lambda x: x[1], reverse=True)[:3]

    # Top 3 symbols
    sym_r: Dict[str, float] = defaultdict(float)
    for t in trades:
        sym_r[t.symbol] += t.pnl_rr
    top_syms = sorted(sym_r.items(), key=lambda x: x[1], reverse=True)[:3]

    lines = []
    day_strs = "  ".join(f"{d} ({r:+.1f}R)" for d, r in top_days)
    sym_strs = "  ".join(f"{s} ({r:+.1f}R)" for s, r in top_syms)
    lines.append(f"    Top-3 days:    {day_strs}")
    lines.append(f"    Top-3 symbols: {sym_strs}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    print("=" * 160)
    print("EXPANDED PORTFOLIO STUDY — All active strategies on 207-day dataset")
    print("=" * 160)

    # ── Load market data ──
    print("\n  Loading market data...")
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    sector_bars_dict = {}
    sector_etfs = sorted(set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    # Count trading days from SPY
    spy_dates = sorted(set(b.timestamp.date() for b in spy_bars))
    print(f"  SPY date range: {spy_dates[0]} → {spy_dates[-1]} ({len(spy_dates)} trading days)")

    # ── Build symbol list ──
    # Use expanded watchlist (matches data files), exclude index/sector ETFs
    excluded = {"SPY", "QQQ", "IWM"} | set(sector_etfs)
    all_data_files = sorted(DATA_DIR.glob("*_5min.csv"))
    symbols = sorted([
        p.stem.replace("_5min", "")
        for p in all_data_files
        if p.stem.replace("_5min", "") not in excluded
    ])
    print(f"  Trading symbols: {len(symbols)}")

    # ══════════════════════════════════════════════════════════
    #  RUN 1: SC Long (Q≥5, no regime gate)
    # ══════════════════════════════════════════════════════════
    print("\n  Running SC Long (Q≥5, no regime)...")
    cfg_sc = OverlayConfig()
    # Enable only SC
    cfg_sc.show_second_chance = True
    cfg_sc.show_ema_pullback = False
    cfg_sc.show_breakdown_retest = False
    cfg_sc.show_trend_setups = False
    cfg_sc.show_reversal_setups = False
    cfg_sc.show_ema_retest = False
    cfg_sc.show_ema_mean_rev = False
    cfg_sc.show_ema_scalp = False
    cfg_sc.show_spencer = False
    cfg_sc.show_failed_bounce = False
    cfg_sc.show_sc_v2 = False
    cfg_sc.show_ema_fpip = False
    cfg_sc.show_ema_confirm = False
    cfg_sc.show_mcs = False
    cfg_sc.show_vwap_reclaim = False
    cfg_sc.show_vka = False
    # SC-specific gates
    cfg_sc.sc_min_quality = 5
    cfg_sc.require_regime = False
    cfg_sc.sc_require_regime = False
    cfg_sc.sc_long_only = True

    sc_trades = _run_all_symbols(symbols, cfg_sc, spy_bars, qqq_bars, sector_bars_dict,
                                  setup_filter={SetupId.SECOND_CHANCE})

    # ══════════════════════════════════════════════════════════
    #  RUN 2: BDR SHORT (regime-gated, AM-only)
    # ══════════════════════════════════════════════════════════
    print(f"  Running BDR SHORT (regime-gated, AM-only)...")
    cfg_bdr = OverlayConfig()
    # Enable only BDR
    cfg_bdr.show_breakdown_retest = True
    cfg_bdr.show_second_chance = False
    cfg_bdr.show_ema_pullback = False
    cfg_bdr.show_trend_setups = False
    cfg_bdr.show_reversal_setups = False
    cfg_bdr.show_ema_retest = False
    cfg_bdr.show_ema_mean_rev = False
    cfg_bdr.show_ema_scalp = False
    cfg_bdr.show_spencer = False
    cfg_bdr.show_failed_bounce = False
    cfg_bdr.show_sc_v2 = False
    cfg_bdr.show_ema_fpip = False
    cfg_bdr.show_ema_confirm = False
    cfg_bdr.show_mcs = False
    cfg_bdr.show_vwap_reclaim = False
    cfg_bdr.show_vka = False
    # BDR-specific gates
    cfg_bdr.bdr_require_red_trend = True
    cfg_bdr.bdr_am_only = True
    cfg_bdr.require_regime = True
    cfg_bdr.bdr_require_regime = True

    bdr_trades = _run_all_symbols(symbols, cfg_bdr, spy_bars, qqq_bars, sector_bars_dict,
                                   setup_filter={SetupId.BDR_SHORT})

    # ══════════════════════════════════════════════════════════
    #  RUN 3: EMA PULL SHORT (early session, <14:00)
    # ══════════════════════════════════════════════════════════
    print(f"  Running EMA PULL SHORT (early, <14:00)...")
    cfg_ep = OverlayConfig()
    # Enable only EMA PULL (requires show_trend_setups for engine)
    cfg_ep.show_trend_setups = True
    cfg_ep.show_ema_pullback = True
    cfg_ep.show_second_chance = False
    cfg_ep.show_breakdown_retest = False
    cfg_ep.show_reversal_setups = False
    cfg_ep.show_ema_retest = False
    cfg_ep.show_ema_mean_rev = False
    cfg_ep.show_ema_scalp = False
    cfg_ep.show_spencer = False
    cfg_ep.show_failed_bounce = False
    cfg_ep.show_sc_v2 = False
    cfg_ep.show_ema_fpip = False
    cfg_ep.show_ema_confirm = False
    cfg_ep.show_mcs = False
    cfg_ep.show_vwap_reclaim = False
    cfg_ep.show_vka = False
    # EP-specific gates
    cfg_ep.ep_time_end = 1400
    cfg_ep.ep_short_only = True
    cfg_ep.require_regime = False
    cfg_ep.ep_require_regime = False
    # Suppress VWAP KISS (also enabled by show_trend_setups)
    cfg_ep.vk_long_only = True  # still on, but we'll filter in post

    ep_trades = _run_all_symbols(symbols, cfg_ep, spy_bars, qqq_bars, sector_bars_dict,
                                  setup_filter={SetupId.EMA_PULL})

    # ══════════════════════════════════════════════════════════
    #  COMBINED PORTFOLIO
    # ══════════════════════════════════════════════════════════
    combined = sc_trades + bdr_trades + ep_trades
    combined.sort(key=lambda t: (t.entry_date, t.entry_time))

    # ── Section 1: Study Definition ──
    print("\n" + "=" * 160)
    print("SECTION 1 — STUDY DEFINITION")
    print("=" * 160)
    print(f"  Dataset: {spy_dates[0]} → {spy_dates[-1]} ({len(spy_dates)} trading days)")
    print(f"  Symbols: {len(symbols)} (expanded watchlist, excluding index/sector ETFs)")
    print(f"  Cost model: dynamic slippage (4bps base) + $0.005/share commission")
    print(f"  Train/test: odd dates = train, even dates = test")
    print()
    print("  Active setups (isolated runs, per-setup gating):")
    print("    1. SC Long  (Q≥5, no regime, sc_long_only=True)")
    print("    2. BDR SHORT (RED+TREND regime, AM-only <11:00)")
    print("    3. EMA PULL SHORT (early <14:00, no regime)")
    print()
    print(f"  Trades found: SC={len(sc_trades)}, BDR={len(bdr_trades)}, EP={len(ep_trades)}, "
          f"Combined={len(combined)}")

    # ── Section 2: Per-Setup Results ──
    print("\n" + "=" * 160)
    print("SECTION 2 — PER-SETUP RESULTS")
    print("=" * 160)
    print(HEADER)
    print(DIVIDER)

    for label, trades in [
        ("SC Long (Q≥5)", sc_trades),
        ("BDR SHORT (regime, AM)", bdr_trades),
        ("EMA PULL SHORT (early)", ep_trades),
        ("COMBINED PORTFOLIO", combined),
    ]:
        m = compute_metrics(trades)
        print(fmt_full(label, m))

    # ── Section 3: Robustness & Concentration ──
    print("\n" + "=" * 160)
    print("SECTION 3 — ROBUSTNESS & CONCENTRATION")
    print("=" * 160)

    for label, trades in [
        ("SC Long (Q≥5)", sc_trades),
        ("BDR SHORT (regime, AM)", bdr_trades),
        ("EMA PULL SHORT (early)", ep_trades),
        ("COMBINED PORTFOLIO", combined),
    ]:
        m = compute_metrics(trades)
        if m["n"] == 0:
            continue
        print(f"\n  {label}:")
        print(f"    Train PF: {pf_str(m['train_pf'])}   Test PF: {pf_str(m['test_pf'])}")
        print(f"    Ex-best-day PF: {pf_str(m['ex_best_day_pf'])}   "
              f"Ex-top-sym PF: {pf_str(m['ex_top_sym_pf'])}")
        print(f"    Avg Win: {m['avg_win_r']:+.3f}R   Avg Loss: {m['avg_loss_r']:+.3f}R   "
              f"Median R: {m['median_r']:+.3f}")
        print(f"    Days active: {m['days_active']}   Avg/day: {m['avg_per_day']:.1f}   "
              f"%Pos days: {m['pct_pos_days']:.1f}%   Median daily R: {m['median_daily_r']:+.2f}")
        print(top_concentration(trades))

    # ── Section 4: 62-day vs 207-day Comparison ──
    print("\n" + "=" * 160)
    print("SECTION 4 — ORIGINAL (62-day) vs EXPANDED (207-day) COMPARISON")
    print("=" * 160)
    print("  Original scorecard numbers (62-day, Dec 2025 – Mar 2026):")
    print(f"    {'Setup':40s}  {'N':>5s}  {'PF':>6s}  {'TotalR':>8s}  {'MaxDD':>7s}")
    print(f"    {'-'*70}")
    print(f"    {'SC Long (Q≥5)':40s}  {'49':>5s}  {'1.19':>6s}  {'+4.82':>8s}  {'7.35':>7s}")
    print(f"    {'BDR SHORT (regime, AM)':40s}  {'42':>5s}  {'1.39':>6s}  {'+7.50':>8s}  {'6.04':>7s}")
    print(f"    {'EMA PULL SHORT (early)':40s}  {'72':>5s}  {'1.45':>6s}  {'+30.97':>8s}  {'15.53':>7s}")
    print(f"    {'COMBINED':40s}  {'163':>5s}  {'1.38':>6s}  {'+43.29':>8s}  {'15.65':>7s}")
    print()
    print("  Expanded (207-day) numbers:")
    print(f"    {'Setup':40s}  {'N':>5s}  {'PF':>6s}  {'TotalR':>8s}  {'MaxDD':>7s}")
    print(f"    {'-'*70}")
    for label, trades in [
        ("SC Long (Q≥5)", sc_trades),
        ("BDR SHORT (regime, AM)", bdr_trades),
        ("EMA PULL SHORT (early)", ep_trades),
        ("COMBINED", combined),
    ]:
        m = compute_metrics(trades)
        print(f"    {label:40s}  {m['n']:5d}  {pf_str(m['pf']):>6s}  "
              f"{m['total_r']:+8.1f}  {m['max_dd_r']:7.1f}")

    # ── Section 5: Time-of-Day Breakdown ──
    print("\n" + "=" * 160)
    print("SECTION 5 — TIME-OF-DAY BREAKDOWN")
    print("=" * 160)

    for label, trades in [
        ("SC Long (Q≥5)", sc_trades),
        ("BDR SHORT (regime, AM)", bdr_trades),
        ("EMA PULL SHORT (early)", ep_trades),
    ]:
        if not trades:
            continue
        print(f"\n  {label}:")
        hour_groups: Dict[int, List[PTrade]] = defaultdict(list)
        for t in trades:
            h = t.entry_time.hour
            hour_groups[h].append(t)
        print(f"    {'Hour':>6s}  {'N':>5s}  {'PF':>6s}  {'Exp':>7s}  {'TotR':>8s}  {'WR%':>6s}")
        print(f"    {'-'*50}")
        for h in sorted(hour_groups.keys()):
            hm = compute_metrics(hour_groups[h])
            print(f"    {h:02d}:00   {hm['n']:5d}  {pf_str(hm['pf']):>6s}  "
                  f"{hm['exp']:+7.3f}  {hm['total_r']:+8.1f}  {hm['wr']:5.1f}%")

    # ── Section 6: Monthly Equity Curve ──
    print("\n" + "=" * 160)
    print("SECTION 6 — MONTHLY P&L (COMBINED)")
    print("=" * 160)

    monthly_r: Dict[str, float] = defaultdict(float)
    monthly_n: Dict[str, int] = defaultdict(int)
    for t in combined:
        key = t.entry_date.strftime("%Y-%m")
        monthly_r[key] += t.pnl_rr
        monthly_n[key] += 1

    cum = 0.0
    print(f"    {'Month':>8s}  {'N':>5s}  {'R':>8s}  {'CumR':>8s}")
    print(f"    {'-'*35}")
    for mo in sorted(monthly_r.keys()):
        cum += monthly_r[mo]
        print(f"    {mo:>8s}  {monthly_n[mo]:5d}  {monthly_r[mo]:+8.1f}  {cum:+8.1f}")

    print("\n" + "=" * 160)
    print("EXPANDED PORTFOLIO STUDY COMPLETE")
    print("=" * 160)


def _run_all_symbols(
    symbols: List[str],
    cfg: OverlayConfig,
    spy_bars: list,
    qqq_bars: list,
    sector_bars_dict: dict,
    setup_filter: Set[SetupId],
) -> List[PTrade]:
    """Run backtest across all symbols, return PTrade list filtered to setup_filter."""
    trades: List[PTrade] = []

    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue

        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None

        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)

        for t in result.trades:
            if t.signal.setup_id not in setup_filter:
                continue
            trades.append(PTrade(
                pnl_rr=t.pnl_rr,
                pnl_rr_raw=t.pnl_rr,  # backtest already applies costs
                exit_reason=t.exit_reason,
                bars_held=t.bars_held,
                entry_time=t.signal.timestamp,
                entry_date=t.signal.timestamp.date(),
                side="LONG" if t.signal.direction == 1 else "SHORT",
                setup=SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id)),
                setup_id=t.signal.setup_id,
                symbol=sym,
                quality=t.signal.quality_score,
            ))

    return trades


if __name__ == "__main__":
    main()
