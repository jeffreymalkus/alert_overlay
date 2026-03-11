"""
Promoted Strategy Stack — Standalone Validation Replay.

Runs each currently promoted setup in isolation, then combined and capped.
Uses the same corrected evaluation standard as RSI v2 replay:
  - actual exit timestamps for concurrency
  - clean per-setup standalone verdicts
  - no mixing for standalone judgment

Sections:
  1. SC LONG STANDALONE
  2. BDR SHORT STANDALONE
  3. EMA PULL SHORT STANDALONE
  4. COMBINED CURRENT-STACK (unconstrained)
  5. COMBINED CURRENT-STACK CAPPED (max 3 concurrent)

Usage:
    cd /path/to/project
    python -m alert_overlay.promoted_replay
"""

import csv
import math
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
OUT_DIR = Path(__file__).parent


# ════════════════════════════════════════════════════════════════
#  Trade wrapper (same as rsi_replay — includes exit_time)
# ════════════════════════════════════════════════════════════════

@dataclass
class PTrade:
    pnl_rr: float
    exit_reason: str
    bars_held: int
    entry_time: datetime
    exit_time: datetime
    entry_date: date
    side: str
    setup: str
    setup_id: SetupId
    symbol: str
    quality: int


# ════════════════════════════════════════════════════════════════
#  Metrics (identical to rsi_replay)
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
            "stop_rate", "target_rate", "time_rate",
            "avg_win_r", "avg_loss_r", "median_r",
            "days_active", "avg_per_day", "pct_pos_days",
            "avg_bars_held",
        ]}

    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in trades)
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")

    avg_win = sum(t.pnl_rr for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnl_rr for t in losses) / len(losses) if losses else 0

    sorted_r = sorted(t.pnl_rr for t in trades)
    mid = n // 2
    median_r = sorted_r[mid] if n % 2 else (sorted_r[mid - 1] + sorted_r[mid]) / 2

    avg_bars = sum(t.bars_held for t in trades) / n

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

    train = [t for t in trades if t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date.day % 2 == 0]
    train_pf = _pf(train)
    test_pf = _pf(test)

    day_r: Dict[date, float] = defaultdict(float)
    for t in trades:
        day_r[t.entry_date] += t.pnl_rr
    if day_r:
        best_day = max(day_r, key=day_r.get)
        ex_best = [t for t in trades if t.entry_date != best_day]
    else:
        ex_best = trades
    ex_best_day_pf = _pf(ex_best)

    sym_r: Dict[str, float] = defaultdict(float)
    for t in trades:
        sym_r[t.symbol] += t.pnl_rr
    if sym_r:
        top_sym = max(sym_r, key=sym_r.get)
        ex_top = [t for t in trades if t.symbol != top_sym]
    else:
        ex_top = trades
    ex_top_sym_pf = _pf(ex_top)

    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    target = sum(1 for t in trades if t.exit_reason == "target")
    timed = sum(1 for t in trades if t.exit_reason in ("time", "ema9trail", "eod"))

    days_active = len(set(t.entry_date for t in trades))
    avg_per_day = n / days_active if days_active else 0
    day_pnl = list(day_r.values())
    pos_days = sum(1 for d in day_pnl if d > 0)
    pct_pos = pos_days / len(day_pnl) * 100 if day_pnl else 0

    return {
        "n": n, "wr": len(wins) / n * 100, "pf": pf, "exp": total_r / n,
        "total_r": total_r, "max_dd_r": max_dd,
        "train_pf": train_pf, "test_pf": test_pf,
        "ex_best_day_pf": ex_best_day_pf, "ex_top_sym_pf": ex_top_sym_pf,
        "stop_rate": stopped / n * 100, "target_rate": target / n * 100,
        "time_rate": timed / n * 100,
        "avg_win_r": avg_win, "avg_loss_r": avg_loss, "median_r": median_r,
        "days_active": days_active, "avg_per_day": avg_per_day,
        "pct_pos_days": pct_pos,
        "avg_bars_held": avg_bars,
    }


def pf_str(v: float) -> str:
    return f"{v:.2f}" if v < 999 else "inf"


HEADER = (
    f"  {'Label':40s}  {'N':>5s}  "
    f"{'PF(R)':>6s}  {'Exp(R)':>7s}  {'TotalR':>8s}  "
    f"{'MaxDD':>7s}  {'WR%':>6s}  {'Stop%':>6s}  "
    f"{'Tgt%':>6s}  {'Time%':>6s}  "
    f"{'TrnPF':>6s}  {'TstPF':>6s}  "
    f"{'ExDay':>6s}  {'ExSym':>6s}  "
    f"{'%Pos':>6s}  {'AvgBr':>5s}"
)
DIVIDER = "  " + "-" * 148


def fmt_row(label: str, m: dict) -> str:
    return (
        f"  {label:40s}  {m['n']:5d}  "
        f"{pf_str(m['pf']):>6s}  {m['exp']:+7.3f}  {m['total_r']:+8.1f}  "
        f"{m['max_dd_r']:7.1f}  {m['wr']:5.1f}%  {m['stop_rate']:5.1f}%  "
        f"{m['target_rate']:5.1f}%  {m['time_rate']:5.1f}%  "
        f"{pf_str(m['train_pf']):>6s}  {pf_str(m['test_pf']):>6s}  "
        f"{pf_str(m['ex_best_day_pf']):>6s}  {pf_str(m['ex_top_sym_pf']):>6s}  "
        f"{m['pct_pos_days']:5.1f}%  {m['avg_bars_held']:5.1f}"
    )


# ════════════════════════════════════════════════════════════════
#  Config helpers — one per standalone setup
# ════════════════════════════════════════════════════════════════

def _base_cfg() -> OverlayConfig:
    """All setups disabled."""
    cfg = OverlayConfig()
    cfg.show_second_chance = False
    cfg.show_ema_pullback = False
    cfg.show_breakdown_retest = False
    cfg.show_trend_setups = False
    cfg.show_reversal_setups = False
    cfg.show_ema_retest = False
    cfg.show_ema_mean_rev = False
    cfg.show_ema_scalp = False
    cfg.show_spencer = False
    cfg.show_failed_bounce = False
    cfg.show_sc_v2 = False
    cfg.show_ema_fpip = False
    cfg.show_ema_confirm = False
    cfg.show_mcs = False
    cfg.show_vwap_reclaim = False
    cfg.show_vka = False
    cfg.show_rsi_midline_long = False
    cfg.show_rsi_bouncefail_short = False
    return cfg


def _sc_long_cfg() -> OverlayConfig:
    """SC Long only — promoted config."""
    cfg = _base_cfg()
    cfg.show_second_chance = True
    cfg.sc_min_quality = 5
    cfg.sc_require_regime = False
    cfg.sc_long_only = True
    return cfg


def _bdr_short_cfg() -> OverlayConfig:
    """BDR Short only — promoted config."""
    cfg = _base_cfg()
    cfg.show_breakdown_retest = True
    cfg.bdr_require_red_trend = True
    cfg.bdr_am_only = True
    cfg.bdr_require_regime = True
    return cfg


def _ep_short_cfg() -> OverlayConfig:
    """EMA Pull Short only — promoted config."""
    cfg = _base_cfg()
    cfg.show_ema_pullback = True
    cfg.show_trend_setups = True  # needed for EMA_PULL engine path
    cfg.ep_time_end = 1400
    cfg.ep_short_only = True
    cfg.ep_require_regime = False
    return cfg


def _all_promoted_cfg() -> OverlayConfig:
    """All promoted setups combined."""
    cfg = _base_cfg()
    # SC Long
    cfg.show_second_chance = True
    cfg.sc_min_quality = 5
    cfg.sc_require_regime = False
    cfg.sc_long_only = True
    # BDR Short
    cfg.show_breakdown_retest = True
    cfg.bdr_require_red_trend = True
    cfg.bdr_am_only = True
    cfg.bdr_require_regime = True
    # EMA Pull Short
    cfg.show_ema_pullback = True
    cfg.show_trend_setups = True
    cfg.ep_time_end = 1400
    cfg.ep_short_only = True
    cfg.ep_require_regime = False
    return cfg


# ════════════════════════════════════════════════════════════════
#  Runner
# ════════════════════════════════════════════════════════════════

def _run_all_symbols(
    symbols: List[str],
    cfg: OverlayConfig,
    spy_bars: list,
    qqq_bars: list,
    sector_bars_dict: dict,
    setup_filter: Optional[Set[SetupId]] = None,
) -> List[PTrade]:
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
            if setup_filter and t.signal.setup_id not in setup_filter:
                continue
            et = t.exit_time if t.exit_time else t.signal.timestamp
            trades.append(PTrade(
                pnl_rr=t.pnl_rr,
                exit_reason=t.exit_reason,
                bars_held=t.bars_held,
                entry_time=t.signal.timestamp,
                exit_time=et,
                entry_date=t.signal.timestamp.date(),
                side="LONG" if t.signal.direction == 1 else "SHORT",
                setup=SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id)),
                setup_id=t.signal.setup_id,
                symbol=sym,
                quality=t.signal.quality_score,
            ))

    return trades


# ════════════════════════════════════════════════════════════════
#  Capped portfolio (actual exit timestamps, existing priority)
# ════════════════════════════════════════════════════════════════

def _capped_portfolio(trades: List[PTrade], max_open: int = 3) -> List[PTrade]:
    """Max concurrent positions using real entry/exit timestamps.
    Earlier entry wins. Same-symbol exclusion."""
    trades_sorted = sorted(trades, key=lambda t: t.entry_time)

    accepted: List[PTrade] = []
    open_positions: List[Tuple[datetime, str]] = []

    for t in trades_sorted:
        open_positions = [
            (exit_t, sym) for (exit_t, sym) in open_positions
            if exit_t > t.entry_time
        ]
        open_syms = {sym for (_, sym) in open_positions}
        if t.symbol in open_syms:
            continue
        if len(open_positions) >= max_open:
            continue
        accepted.append(t)
        open_positions.append((t.exit_time, t.symbol))

    return accepted


# ════════════════════════════════════════════════════════════════
#  CSV export
# ════════════════════════════════════════════════════════════════

def _export_csv(trades: List[PTrade], path: Path, label: str):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["section", "date", "entry_time", "exit_time", "symbol",
                     "side", "setup", "pnl_rr", "exit_reason", "bars_held", "quality"])
        for t in sorted(trades, key=lambda x: x.entry_time):
            w.writerow([
                label,
                str(t.entry_date),
                t.entry_time.strftime("%Y-%m-%d %H:%M"),
                t.exit_time.strftime("%Y-%m-%d %H:%M"),
                t.symbol,
                t.side,
                t.setup,
                f"{t.pnl_rr:+.4f}",
                t.exit_reason,
                t.bars_held,
                t.quality,
            ])
    print(f"  → Exported {len(trades)} trades to {path.name}")


# ════════════════════════════════════════════════════════════════
#  Detailed standalone summary
# ════════════════════════════════════════════════════════════════

def _print_setup_summary(label: str, trades: List[PTrade], date_range: str, n_symbols: int):
    m = compute_metrics(trades)
    print(f"\n  ── {label} STANDALONE SUMMARY ──")
    print(f"  Trade count:       {m['n']}")
    print(f"  Win rate:          {m['wr']:.1f}%")
    print(f"  Profit factor:     {pf_str(m['pf'])}")
    print(f"  Expectancy (R):    {m['exp']:+.3f}")
    print(f"  Total R:           {m['total_r']:+.1f}")
    print(f"  Avg winner (R):    {m['avg_win_r']:+.3f}")
    print(f"  Avg loser (R):     {m['avg_loss_r']:+.3f}")
    print(f"  Max drawdown (R):  {m['max_dd_r']:.1f}")
    print(f"  Avg hold bars:     {m['avg_bars_held']:.1f}")
    print(f"  Stop rate:         {m['stop_rate']:.1f}%")
    print(f"  Target rate:       {m['target_rate']:.1f}%")
    print(f"  Time/trail/EOD:    {m['time_rate']:.1f}%")
    print(f"  Train PF:          {pf_str(m['train_pf'])}")
    print(f"  Test PF:           {pf_str(m['test_pf'])}")
    print(f"  Ex-best-day PF:    {pf_str(m['ex_best_day_pf'])}")
    print(f"  Ex-top-symbol PF:  {pf_str(m['ex_top_sym_pf'])}")
    print(f"  Days active:       {m['days_active']}")
    print(f"  Avg trades/day:    {m['avg_per_day']:.2f}")
    print(f"  % positive days:   {m['pct_pos_days']:.1f}%")
    print(f"  Date range:        {date_range}")
    print(f"  Universe:          {n_symbols} symbols (IBKR-extended)")
    print(f"  Slippage:          Dynamic (4bps base, vol×family multiplier)")
    print(f"  Commission:        $0.00/share (config default)")

    # Promotion criteria
    c1 = m["pf"] > 1.0
    c2 = m["exp"] > 0
    c3 = m["train_pf"] > 0.80
    c4 = m["test_pf"] > 0.80
    n_ok = m["n"] >= 10
    print(f"\n  PROMOTION CRITERIA:")
    print(f"    N ≥ 10:           {'PASS' if n_ok else 'FAIL'} ({m['n']})")
    print(f"    PF > 1.0:         {'PASS' if c1 else 'FAIL'} ({pf_str(m['pf'])})")
    print(f"    Exp > 0:          {'PASS' if c2 else 'FAIL'} ({m['exp']:+.3f})")
    print(f"    Train PF > 0.80:  {'PASS' if c3 else 'FAIL'} ({pf_str(m['train_pf'])})")
    print(f"    Test PF > 0.80:   {'PASS' if c4 else 'FAIL'} ({pf_str(m['test_pf'])})")
    all_pass = c1 and c2 and c3 and c4 and n_ok
    verdict = "SURVIVES" if all_pass else "DOES NOT SURVIVE"
    print(f"    → {verdict}")
    return m, verdict


# ════════════════════════════════════════════════════════════════
#  Monthly breakdown helper
# ════════════════════════════════════════════════════════════════

def _print_monthly(label: str, trades: List[PTrade]):
    if not trades:
        print(f"\n  {label}: no trades")
        return
    monthly_r: Dict[str, float] = defaultdict(float)
    monthly_n: Dict[str, int] = defaultdict(int)
    for t in trades:
        key = t.entry_date.strftime("%Y-%m")
        monthly_r[key] += t.pnl_rr
        monthly_n[key] += 1
    cum = 0.0
    print(f"\n  {label}:")
    print(f"    {'Month':>8s}  {'N':>5s}  {'R':>8s}  {'CumR':>8s}")
    print(f"    {'-'*35}")
    for mo in sorted(monthly_r.keys()):
        cum += monthly_r[mo]
        print(f"    {mo:>8s}  {monthly_n[mo]:5d}  {monthly_r[mo]:+8.1f}  {cum:+8.1f}")


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    print("=" * 150)
    print("PROMOTED STRATEGY STACK — Standalone Validation Replay")
    print("Corrected evaluation standard: actual exit timestamps, per-setup isolation")
    print("=" * 150)

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

    spy_dates = sorted(set(b.timestamp.date() for b in spy_bars))
    date_range = f"{spy_dates[0]} → {spy_dates[-1]} ({len(spy_dates)} trading days)"
    print(f"  SPY date range: {date_range}")

    excluded = {"SPY", "QQQ", "IWM"} | set(sector_etfs)
    all_data_files = sorted(DATA_DIR.glob("*_5min.csv"))
    symbols = sorted([
        p.stem.replace("_5min", "")
        for p in all_data_files
        if p.stem.replace("_5min", "") not in excluded
    ])
    n_symbols = len(symbols)
    print(f"  Trading symbols: {n_symbols}")

    verdicts = {}

    # ══════════════════════════════════════════════════════════
    #  SECTION 1: SC LONG STANDALONE
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 150)
    print("SECTION 1 — SC LONG STANDALONE")
    print("=" * 150)

    print("\n  Running SC Long standalone...")
    cfg_sc = _sc_long_cfg()
    sc_trades = _run_all_symbols(symbols, cfg_sc, spy_bars, qqq_bars, sector_bars_dict,
                                  setup_filter={SetupId.SECOND_CHANCE})
    print(HEADER)
    print(DIVIDER)
    print(fmt_row("SC Long (Q≥5, long-only)", compute_metrics(sc_trades)))

    m, v = _print_setup_summary("SC LONG", sc_trades, date_range, n_symbols)
    verdicts["SC Long"] = v
    _print_monthly("SC Long", sc_trades)
    _export_csv(sc_trades, OUT_DIR / "replay_sc_long_standalone.csv", "sc_long")

    # Top 10 symbols by R
    sym_r: Dict[str, float] = defaultdict(float)
    sym_n: Dict[str, int] = defaultdict(int)
    for t in sc_trades:
        sym_r[t.symbol] += t.pnl_rr
        sym_n[t.symbol] += 1
    if sym_r:
        print(f"\n  SC Long — Top 10 symbols by R:")
        print(f"    {'Symbol':>8s}  {'N':>4s}  {'TotalR':>8s}")
        for sym in sorted(sym_r, key=sym_r.get, reverse=True)[:10]:
            print(f"    {sym:>8s}  {sym_n[sym]:4d}  {sym_r[sym]:+8.1f}")

    # ══════════════════════════════════════════════════════════
    #  SECTION 2: BDR SHORT STANDALONE
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 150)
    print("SECTION 2 — BDR SHORT STANDALONE")
    print("=" * 150)

    print("\n  Running BDR Short standalone...")
    cfg_bdr = _bdr_short_cfg()
    bdr_trades = _run_all_symbols(symbols, cfg_bdr, spy_bars, qqq_bars, sector_bars_dict,
                                   setup_filter={SetupId.BDR_SHORT})
    print(HEADER)
    print(DIVIDER)
    print(fmt_row("BDR Short (RED+TREND, AM-only)", compute_metrics(bdr_trades)))

    m, v = _print_setup_summary("BDR SHORT", bdr_trades, date_range, n_symbols)
    verdicts["BDR Short"] = v
    _print_monthly("BDR Short", bdr_trades)
    _export_csv(bdr_trades, OUT_DIR / "replay_bdr_short_standalone.csv", "bdr_short")

    sym_r = defaultdict(float)
    sym_n = defaultdict(int)
    for t in bdr_trades:
        sym_r[t.symbol] += t.pnl_rr
        sym_n[t.symbol] += 1
    if sym_r:
        print(f"\n  BDR Short — Top 10 symbols by R:")
        print(f"    {'Symbol':>8s}  {'N':>4s}  {'TotalR':>8s}")
        for sym in sorted(sym_r, key=sym_r.get, reverse=True)[:10]:
            print(f"    {sym:>8s}  {sym_n[sym]:4d}  {sym_r[sym]:+8.1f}")

    # ══════════════════════════════════════════════════════════
    #  SECTION 3: EMA PULL SHORT STANDALONE
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 150)
    print("SECTION 3 — EMA PULL SHORT STANDALONE")
    print("=" * 150)

    print("\n  Running EMA Pull Short standalone...")
    cfg_ep = _ep_short_cfg()
    ep_trades = _run_all_symbols(symbols, cfg_ep, spy_bars, qqq_bars, sector_bars_dict,
                                  setup_filter={SetupId.EMA_PULL})
    print(HEADER)
    print(DIVIDER)
    print(fmt_row("EMA Pull Short (≤14:00, short-only)", compute_metrics(ep_trades)))

    m, v = _print_setup_summary("EMA PULL SHORT", ep_trades, date_range, n_symbols)
    verdicts["EMA Pull Short"] = v
    _print_monthly("EMA Pull Short", ep_trades)
    _export_csv(ep_trades, OUT_DIR / "replay_ep_short_standalone.csv", "ep_short")

    sym_r = defaultdict(float)
    sym_n = defaultdict(int)
    for t in ep_trades:
        sym_r[t.symbol] += t.pnl_rr
        sym_n[t.symbol] += 1
    if sym_r:
        print(f"\n  EMA Pull Short — Top 10 symbols by R:")
        print(f"    {'Symbol':>8s}  {'N':>4s}  {'TotalR':>8s}")
        for sym in sorted(sym_r, key=sym_r.get, reverse=True)[:10]:
            print(f"    {sym:>8s}  {sym_n[sym]:4d}  {sym_r[sym]:+8.1f}")

    # ══════════════════════════════════════════════════════════
    #  SECTION 4: COMBINED CURRENT-STACK (unconstrained)
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 150)
    print("SECTION 4 — COMBINED CURRENT-STACK (unconstrained)")
    print("=" * 150)

    print("\n  Running combined current-stack...")
    cfg_all = _all_promoted_cfg()
    existing_ids = {SetupId.SECOND_CHANCE, SetupId.BDR_SHORT, SetupId.EMA_PULL}
    combined_trades = _run_all_symbols(symbols, cfg_all, spy_bars, qqq_bars,
                                        sector_bars_dict, setup_filter=existing_ids)

    print(HEADER)
    print(DIVIDER)
    for label, trades in [
        ("Combined - SC Long", [t for t in combined_trades if t.setup_id == SetupId.SECOND_CHANCE]),
        ("Combined - BDR Short", [t for t in combined_trades if t.setup_id == SetupId.BDR_SHORT]),
        ("Combined - EMA Pull Short", [t for t in combined_trades if t.setup_id == SetupId.EMA_PULL]),
        ("Combined - ALL", combined_trades),
    ]:
        print(fmt_row(label, compute_metrics(trades)))

    # Peak concurrent
    all_sorted = sorted(combined_trades, key=lambda t: t.entry_time)
    max_concurrent = 0
    for i, t in enumerate(all_sorted):
        concurrent = sum(1 for o in all_sorted[:i+1] if o.exit_time > t.entry_time)
        if concurrent > max_concurrent:
            max_concurrent = concurrent
    print(f"\n  Peak concurrent positions (unconstrained): {max_concurrent}")

    _export_csv(combined_trades, OUT_DIR / "replay_combined_promoted.csv", "combined_promoted")

    # ══════════════════════════════════════════════════════════
    #  SECTION 5: COMBINED CURRENT-STACK CAPPED (max 3)
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 150)
    print("SECTION 5 — COMBINED CURRENT-STACK CAPPED (max 3 concurrent)")
    print("=" * 150)

    print("\n  Running capped combined...")
    capped = _capped_portfolio(combined_trades, max_open=3)

    print(HEADER)
    print(DIVIDER)
    for label, trades in [
        ("Capped - SC Long", [t for t in capped if t.setup_id == SetupId.SECOND_CHANCE]),
        ("Capped - BDR Short", [t for t in capped if t.setup_id == SetupId.BDR_SHORT]),
        ("Capped - EMA Pull Short", [t for t in capped if t.setup_id == SetupId.EMA_PULL]),
        ("Capped - ALL", capped),
    ]:
        print(fmt_row(label, compute_metrics(trades)))

    # Verify cap
    capped_sorted = sorted(capped, key=lambda t: t.entry_time)
    max_capped = 0
    for i, t in enumerate(capped_sorted):
        concurrent = sum(1 for o in capped_sorted[:i+1] if o.exit_time > t.entry_time)
        if concurrent > max_capped:
            max_capped = concurrent
    print(f"\n  Peak concurrent positions (capped): {max_capped} (should be ≤ 3)")

    _export_csv(capped, OUT_DIR / "replay_combined_promoted_capped.csv", "combined_promoted_capped")

    # ══════════════════════════════════════════════════════════
    #  FINAL VERDICT SUMMARY
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 150)
    print("FINAL VERDICT SUMMARY")
    print("=" * 150)
    for name, v in verdicts.items():
        status = "✓" if v == "SURVIVES" else "✗"
        print(f"  {status} {name:20s} → {v}")

    combined_m = compute_metrics(combined_trades)
    capped_m = compute_metrics(capped)
    print(f"\n  Combined stack (unconstrained): {combined_m['n']} trades, "
          f"PF {pf_str(combined_m['pf'])}, Exp {combined_m['exp']:+.3f}R, "
          f"TotalR {combined_m['total_r']:+.1f}")
    print(f"  Combined stack (capped, max 3):  {capped_m['n']} trades, "
          f"PF {pf_str(capped_m['pf'])}, Exp {capped_m['exp']:+.3f}R, "
          f"TotalR {capped_m['total_r']:+.1f}")

    print("\n" + "=" * 150)
    print("DONE.")
    print("=" * 150)


if __name__ == "__main__":
    main()
