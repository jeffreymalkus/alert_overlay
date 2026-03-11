"""
Retired Strategy Re-Validation — Phase 2.

Tests each retired/unpromoted setup in standalone isolation using the same
corrected evaluation standard as promoted_replay.py:
  - actual exit timestamps
  - per-setup standalone verdicts
  - no mixing for individual judgment

Priority order:
  1. Best prior long candidates (Spencer, SC_V2, VWAP_RECLAIM, VKA, MCS)
  2. Best prior short candidates (FAILED_BOUNCE)
  3. EMA scalps (EMA_RECLAIM, EMA_FPIP, EMA_CONFIRM)
  4. Legacy primary-path setups (VWAP_KISS, EMA_RETEST, Reversals, EMA9_SEP)

Usage:
    cd /path/to/project
    python -m alert_overlay.retired_replay
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
#  Trade wrapper (same as promoted_replay)
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
#  Config helpers — base with everything disabled
# ════════════════════════════════════════════════════════════════

def _base_cfg() -> OverlayConfig:
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


# ── PRIORITY 1: Long candidates ──

def _spencer_cfg() -> OverlayConfig:
    """Spencer long-only with default params."""
    cfg = _base_cfg()
    cfg.show_spencer = True
    cfg.sp_long_only = True
    return cfg

def _spencer_both_cfg() -> OverlayConfig:
    """Spencer both directions."""
    cfg = _base_cfg()
    cfg.show_spencer = True
    cfg.sp_long_only = False
    return cfg

def _sc_v2_cfg() -> OverlayConfig:
    """SC V2 with default params."""
    cfg = _base_cfg()
    cfg.show_sc_v2 = True
    return cfg

def _vwap_reclaim_cfg() -> OverlayConfig:
    """VWAP Reclaim long-only with promoted params from config comments."""
    cfg = _base_cfg()
    cfg.show_vwap_reclaim = True
    cfg.vr_long_only = True
    return cfg

def _vka_cfg() -> OverlayConfig:
    """VK Accept long-only with default params."""
    cfg = _base_cfg()
    cfg.show_vka = True
    cfg.vka_long_only = True
    return cfg

def _mcs_cfg() -> OverlayConfig:
    """MCS long-only with default params."""
    cfg = _base_cfg()
    cfg.show_mcs = True
    cfg.show_trend_setups = True  # MCS uses show_trend_setups path
    cfg.mcs_long_only = True
    return cfg


# ── PRIORITY 2: Short candidates ──

def _failed_bounce_cfg() -> OverlayConfig:
    """Failed Bounce short-only."""
    cfg = _base_cfg()
    cfg.show_failed_bounce = True
    return cfg


# ── PRIORITY 3: EMA scalps ──

def _ema_reclaim_cfg() -> OverlayConfig:
    """EMA Reclaim (9EMA scalp) with default params."""
    cfg = _base_cfg()
    cfg.show_ema_scalp = True
    return cfg

def _ema_fpip_cfg() -> OverlayConfig:
    """EMA FPIP (First Pullback In Play) with default params."""
    cfg = _base_cfg()
    cfg.show_ema_fpip = True
    return cfg

def _ema_confirm_cfg() -> OverlayConfig:
    """EMA Confirm with default params."""
    cfg = _base_cfg()
    cfg.show_ema_confirm = True
    return cfg


# ── PRIORITY 4: Legacy primary-path setups ──

def _vwap_kiss_cfg() -> OverlayConfig:
    """VWAP Kiss long-only (requires show_trend_setups)."""
    cfg = _base_cfg()
    cfg.show_trend_setups = True
    cfg.vk_long_only = True
    return cfg

def _ema_retest_cfg() -> OverlayConfig:
    """EMA Retest both directions."""
    cfg = _base_cfg()
    cfg.show_ema_retest = True
    cfg.show_trend_setups = True  # retest uses trend path
    return cfg

def _reversals_cfg() -> OverlayConfig:
    """BOX_REV, MANIP, VWAP_SEP — legacy reversal family."""
    cfg = _base_cfg()
    cfg.show_reversal_setups = True
    return cfg

def _ema9_sep_cfg() -> OverlayConfig:
    """EMA9_SEP — mean reversion."""
    cfg = _base_cfg()
    cfg.show_ema_mean_rev = True
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
#  CSV export
# ════════════════════════════════════════════════════════════════

def _export_csv(trades: List[PTrade], path: Path, label: str):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["section", "date", "entry_time", "exit_time", "symbol",
                     "side", "setup", "pnl_rr", "exit_reason", "bars_held", "quality"])
        for t in sorted(trades, key=lambda x: x.entry_time):
            w.writerow([
                label, str(t.entry_date),
                t.entry_time.strftime("%Y-%m-%d %H:%M"),
                t.exit_time.strftime("%Y-%m-%d %H:%M"),
                t.symbol, t.side, t.setup,
                f"{t.pnl_rr:+.4f}", t.exit_reason, t.bars_held, t.quality,
            ])
    print(f"  → Exported {len(trades)} trades to {path.name}")


# ════════════════════════════════════════════════════════════════
#  Standalone summary + verdict
# ════════════════════════════════════════════════════════════════

def _print_summary_and_verdict(label: str, trades: List[PTrade], date_range: str,
                                n_symbols: int) -> Tuple[dict, str]:
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
#  Run one setup section
# ════════════════════════════════════════════════════════════════

def _run_setup_section(
    section_num: int,
    label: str,
    cfg: OverlayConfig,
    setup_filter: Optional[Set[SetupId]],
    csv_name: str,
    symbols: List[str],
    spy_bars: list,
    qqq_bars: list,
    sector_bars_dict: dict,
    date_range: str,
    n_symbols: int,
) -> Tuple[str, dict, str, List[PTrade]]:
    """Run one setup and print full section. Returns (label, metrics, verdict, trades)."""
    print(f"\n{'=' * 150}")
    print(f"SECTION {section_num} — {label}")
    print("=" * 150)

    print(f"\n  Running {label}...")
    trades = _run_all_symbols(symbols, cfg, spy_bars, qqq_bars, sector_bars_dict,
                               setup_filter=setup_filter)

    # Split by direction if both exist
    longs = [t for t in trades if t.side == "LONG"]
    shorts = [t for t in trades if t.side == "SHORT"]

    print(HEADER)
    print(DIVIDER)
    if longs and shorts:
        print(fmt_row(f"{label} (LONG)", compute_metrics(longs)))
        print(fmt_row(f"{label} (SHORT)", compute_metrics(shorts)))
    print(fmt_row(f"{label} (ALL)", compute_metrics(trades)))

    m, v = _print_summary_and_verdict(label, trades, date_range, n_symbols)
    _print_monthly(label, trades)
    _export_csv(trades, OUT_DIR / f"replay_retired_{csv_name}.csv", csv_name)

    return label, m, v, trades


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main(batch: str = "all"):
    """Run retired strategy re-validation.
    batch: "1" = priority 1-7, "2" = priority 8-14, "all" = everything
    """
    print("=" * 150)
    print(f"RETIRED STRATEGY RE-VALIDATION — Phase 2 (batch={batch})")
    print("Corrected evaluation standard: actual exit timestamps, per-setup isolation")
    print("=" * 150)

    # Load market data
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
    section = 0

    run_batch1 = batch in ("1", "all")
    run_batch2 = batch in ("2", "all")

    if run_batch1:
        # ═══════════════════════════════════════
        # PRIORITY 1: LONG CANDIDATES
        # ═══════════════════════════════════════

        # 1. Spencer Long
        section += 1
        lbl, m, v, _ = _run_setup_section(
            section, "SPENCER LONG-ONLY", _spencer_cfg(),
            {SetupId.SPENCER}, "spencer_long",
            symbols, spy_bars, qqq_bars, sector_bars_dict, date_range, n_symbols)
        verdicts[lbl] = v

        # 2. Spencer Both
        section += 1
        lbl, m, v, _ = _run_setup_section(
            section, "SPENCER BOTH DIRECTIONS", _spencer_both_cfg(),
            {SetupId.SPENCER}, "spencer_both",
            symbols, spy_bars, qqq_bars, sector_bars_dict, date_range, n_symbols)
        verdicts[lbl] = v

        # 3. SC V2
        section += 1
        lbl, m, v, _ = _run_setup_section(
            section, "SC V2 (SECOND CHANCE V2)", _sc_v2_cfg(),
            {SetupId.SC_V2}, "sc_v2",
            symbols, spy_bars, qqq_bars, sector_bars_dict, date_range, n_symbols)
        verdicts[lbl] = v

        # 4. VWAP Reclaim
        section += 1
        lbl, m, v, _ = _run_setup_section(
            section, "VWAP RECLAIM LONG", _vwap_reclaim_cfg(),
            {SetupId.VWAP_RECLAIM}, "vwap_reclaim",
            symbols, spy_bars, qqq_bars, sector_bars_dict, date_range, n_symbols)
        verdicts[lbl] = v

        # 5. VKA
        section += 1
        lbl, m, v, _ = _run_setup_section(
            section, "VK ACCEPT LONG", _vka_cfg(),
            {SetupId.VKA}, "vka",
            symbols, spy_bars, qqq_bars, sector_bars_dict, date_range, n_symbols)
        verdicts[lbl] = v

        # 6. MCS
        section += 1
        lbl, m, v, _ = _run_setup_section(
            section, "MCS LONG-ONLY", _mcs_cfg(),
            {SetupId.MCS}, "mcs",
            symbols, spy_bars, qqq_bars, sector_bars_dict, date_range, n_symbols)
        verdicts[lbl] = v

        # ═══════════════════════════════════════
        # PRIORITY 2: SHORT CANDIDATES
        # ═══════════════════════════════════════

        # 7. Failed Bounce
        section += 1
        lbl, m, v, _ = _run_setup_section(
            section, "FAILED BOUNCE SHORT", _failed_bounce_cfg(),
            {SetupId.FAILED_BOUNCE}, "failed_bounce",
            symbols, spy_bars, qqq_bars, sector_bars_dict, date_range, n_symbols)
        verdicts[lbl] = v

    if run_batch2:
        section = 7  # continue numbering

        # ═══════════════════════════════════════
        # PRIORITY 3: EMA SCALPS
        # ═══════════════════════════════════════

        # 8. EMA Reclaim (scalp)
        section += 1
        lbl, m, v, _ = _run_setup_section(
            section, "EMA RECLAIM (9EMA SCALP)", _ema_reclaim_cfg(),
            {SetupId.EMA_RECLAIM}, "ema_reclaim",
            symbols, spy_bars, qqq_bars, sector_bars_dict, date_range, n_symbols)
        verdicts[lbl] = v

        # 9. EMA FPIP
        section += 1
        lbl, m, v, _ = _run_setup_section(
            section, "EMA FPIP (FIRST PULLBACK IN PLAY)", _ema_fpip_cfg(),
            {SetupId.EMA_FPIP}, "ema_fpip",
            symbols, spy_bars, qqq_bars, sector_bars_dict, date_range, n_symbols)
        verdicts[lbl] = v

        # 10. EMA Confirm
        section += 1
        lbl, m, v, _ = _run_setup_section(
            section, "EMA CONFIRM", _ema_confirm_cfg(),
            {SetupId.EMA_CONFIRM}, "ema_confirm",
            symbols, spy_bars, qqq_bars, sector_bars_dict, date_range, n_symbols)
        verdicts[lbl] = v

        # ═══════════════════════════════════════
        # PRIORITY 4: LEGACY PRIMARY-PATH
        # ═══════════════════════════════════════

        # 11. VWAP Kiss long-only
        section += 1
        lbl, m, v, _ = _run_setup_section(
            section, "VWAP KISS LONG-ONLY", _vwap_kiss_cfg(),
            {SetupId.VWAP_KISS}, "vwap_kiss",
            symbols, spy_bars, qqq_bars, sector_bars_dict, date_range, n_symbols)
        verdicts[lbl] = v

        # 12. EMA Retest
        section += 1
        lbl, m, v, _ = _run_setup_section(
            section, "EMA RETEST", _ema_retest_cfg(),
            {SetupId.EMA_RETEST}, "ema_retest",
            symbols, spy_bars, qqq_bars, sector_bars_dict, date_range, n_symbols)
        verdicts[lbl] = v

        # 13. Reversals (BOX_REV + MANIP + VWAP_SEP)
        section += 1
        lbl, m, v, _ = _run_setup_section(
            section, "REVERSALS (BOX_REV/MANIP/VWAP_SEP)", _reversals_cfg(),
            {SetupId.BOX_REV, SetupId.MANIP, SetupId.VWAP_SEP}, "reversals",
            symbols, spy_bars, qqq_bars, sector_bars_dict, date_range, n_symbols)
        verdicts[lbl] = v

        # 14. EMA9 Sep
        section += 1
        lbl, m, v, _ = _run_setup_section(
            section, "EMA9 SEP (MEAN REVERSION)", _ema9_sep_cfg(),
            {SetupId.EMA9_SEP}, "ema9_sep",
            symbols, spy_bars, qqq_bars, sector_bars_dict, date_range, n_symbols)
        verdicts[lbl] = v

    # ═══════════════════════════════════════
    # FINAL VERDICT TABLE
    # ═══════════════════════════════════════
    print(f"\n{'=' * 150}")
    print("FINAL VERDICT TABLE — RETIRED STRATEGY RE-VALIDATION")
    print("=" * 150)
    survivors = []
    failures = []
    for name, v in verdicts.items():
        status = "✓" if v == "SURVIVES" else "✗"
        print(f"  {status} {name:45s} → {v}")
        if v == "SURVIVES":
            survivors.append(name)
        else:
            failures.append(name)

    print(f"\n  Survivors: {len(survivors)} / {len(verdicts)}")
    if survivors:
        for s in survivors:
            print(f"    ✓ {s}")
    print(f"  Failures:  {len(failures)} / {len(verdicts)}")

    print(f"\n{'=' * 150}")
    print("DONE.")
    print("=" * 150)


if __name__ == "__main__":
    import sys
    batch = sys.argv[1] if len(sys.argv) > 1 else "all"
    main(batch=batch)
