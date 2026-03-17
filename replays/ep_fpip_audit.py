"""
EMA PULL SHORT + EMA FPIP — Comprehensive Audit Replay.

Three unresolved items from the promoted stack:
  1. SC / 2ND_CHANCE → already dead (PF 0.48, sc_repair_study exhaustive). RETIRE.
  2. EMA PULL SHORT → PF 0.94, N=26. Near-miss. Ablation to determine salvageable or retire.
  3. EMA FPIP → retired without proper audit. Baseline + gate relaxation.

Plus:
  4. CLEAN PROMOTED STACK → BDR Short + FLR only (the two survivors)
  5. CLEAN STACK CAPPED (max 3 concurrent)

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.replays.ep_fpip_audit
"""

import csv
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import NaN, SetupId, SETUP_DISPLAY_NAME
from ..market_context import SECTOR_MAP, get_sector_etf

DATA_DIR = Path(__file__).parent.parent / "data"
OUT_DIR = Path(__file__).parent.parent / "outputs"


# ════════════════════════════════════════════════════════════════
#  Trade wrapper
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
    f"  {'Label':45s}  {'N':>5s}  "
    f"{'PF(R)':>6s}  {'Exp(R)':>7s}  {'TotalR':>8s}  "
    f"{'WR%':>6s}  {'Stop%':>6s}  "
    f"{'TrnPF':>6s}  {'TstPF':>6s}  "
    f"{'ExDay':>6s}  {'ExSym':>6s}"
)
DIVIDER = "  " + "-" * 130


def fmt_row(label: str, m: dict) -> str:
    return (
        f"  {label:45s}  {m['n']:5d}  "
        f"{pf_str(m['pf']):>6s}  {m['exp']:+7.3f}  {m['total_r']:+8.1f}  "
        f"{m['wr']:5.1f}%  {m['stop_rate']:5.1f}%  "
        f"{pf_str(m['train_pf']):>6s}  {pf_str(m['test_pf']):>6s}  "
        f"{pf_str(m['ex_best_day_pf']):>6s}  {pf_str(m['ex_top_sym_pf']):>6s}"
    )


# ════════════════════════════════════════════════════════════════
#  Config helpers — all setups disabled baseline
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
    cfg.show_hitchhiker = False
    cfg.show_fashionably_late = False
    cfg.show_backside = False
    cfg.show_rubberband = False
    cfg.show_fl_momentum_rebuild = False
    cfg.show_ema9_first_pb = False
    cfg.show_ema9_backside_rb = False
    return cfg


# ── EMA Pull Short configs ──

def _ep_baseline_cfg() -> OverlayConfig:
    """EP Short — current promoted config."""
    cfg = _base_cfg()
    cfg.show_ema_pullback = True
    cfg.ep_time_end = 1400
    cfg.ep_short_only = True
    cfg.ep_require_regime = False
    return cfg


def _ep_extended_time_cfg() -> OverlayConfig:
    """EP Short — extend time to 15:00."""
    cfg = _ep_baseline_cfg()
    cfg.ep_time_end = 1500
    return cfg


def _ep_full_day_cfg() -> OverlayConfig:
    """EP Short — full day (no time cutoff essentially)."""
    cfg = _ep_baseline_cfg()
    cfg.ep_time_end = 1530
    return cfg


def _ep_both_sides_cfg() -> OverlayConfig:
    """EP — both long and short."""
    cfg = _base_cfg()
    cfg.show_ema_pullback = True
    cfg.ep_time_end = 1400
    cfg.ep_short_only = False
    cfg.ep_require_regime = False
    return cfg


def _ep_with_regime_cfg() -> OverlayConfig:
    """EP Short — with regime gate."""
    cfg = _ep_baseline_cfg()
    cfg.ep_require_regime = True
    return cfg


# ── EMA FPIP configs ──

def _fpip_baseline_cfg() -> OverlayConfig:
    """FPIP — current config (all gates on)."""
    cfg = _base_cfg()
    cfg.show_ema_scalp = True
    cfg.show_ema_fpip = True
    return cfg


def _fpip_no_market_cfg() -> OverlayConfig:
    """FPIP — no market alignment gate."""
    cfg = _fpip_baseline_cfg()
    cfg.ema_fpip_require_market_align = False
    return cfg


def _fpip_no_vwap_cfg() -> OverlayConfig:
    """FPIP — no VWAP alignment gate."""
    cfg = _fpip_baseline_cfg()
    cfg.ema_fpip_require_vwap_align = False
    return cfg


def _fpip_no_ema20_cfg() -> OverlayConfig:
    """FPIP — no EMA20 slope gate."""
    cfg = _fpip_baseline_cfg()
    cfg.ema_fpip_require_ema20_slope = False
    return cfg


def _fpip_relaxed_rvol_cfg() -> OverlayConfig:
    """FPIP — lower RVOL (1.0 vs 1.5)."""
    cfg = _fpip_baseline_cfg()
    cfg.ema_fpip_rvol_tod_min = 1.0
    return cfg


def _fpip_no_context_cfg() -> OverlayConfig:
    """FPIP — all context gates off (VWAP, market, EMA20)."""
    cfg = _fpip_baseline_cfg()
    cfg.ema_fpip_require_market_align = False
    cfg.ema_fpip_require_vwap_align = False
    cfg.ema_fpip_require_ema20_slope = False
    return cfg


def _fpip_wide_open_cfg() -> OverlayConfig:
    """FPIP — all context off + relaxed RVOL + quality 0."""
    cfg = _fpip_no_context_cfg()
    cfg.ema_fpip_rvol_tod_min = 1.0
    cfg.ema_fpip_min_quality = 0
    return cfg


def _fpip_q0_cfg() -> OverlayConfig:
    """FPIP — quality gate removed."""
    cfg = _fpip_baseline_cfg()
    cfg.ema_fpip_min_quality = 0
    return cfg


# ── Clean promoted stack (BDR + FLR only) ──

def _bdr_flr_cfg() -> OverlayConfig:
    """Clean promoted stack: BDR Short + FLR only."""
    cfg = _base_cfg()
    # BDR Short
    cfg.show_breakdown_retest = True
    cfg.bdr_require_red_trend = True
    cfg.bdr_am_only = True
    cfg.bdr_require_regime = True
    # FLR
    cfg.show_fl_momentum_rebuild = True
    # FLR uses config defaults (4-bar turn, stop=0.50)
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
#  Capped portfolio
# ════════════════════════════════════════════════════════════════

def _capped_portfolio(trades: List[PTrade], max_open: int = 3) -> List[PTrade]:
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
#  Summary helpers
# ════════════════════════════════════════════════════════════════

def _print_setup_summary(label: str, trades: List[PTrade], date_range: str, n_symbols: int):
    m = compute_metrics(trades)
    print(f"\n  ── {label} SUMMARY ──")
    print(f"  Trade count:       {m['n']}")
    print(f"  Win rate:          {m['wr']:.1f}%")
    print(f"  Profit factor:     {pf_str(m['pf'])}")
    print(f"  Expectancy (R):    {m['exp']:+.3f}")
    print(f"  Total R:           {m['total_r']:+.1f}")
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


def _print_top_symbols(label: str, trades: List[PTrade], n_top: int = 10):
    sym_r: Dict[str, float] = defaultdict(float)
    sym_n: Dict[str, int] = defaultdict(int)
    for t in trades:
        sym_r[t.symbol] += t.pnl_rr
        sym_n[t.symbol] += 1
    if sym_r:
        print(f"\n  {label} — Top {n_top} symbols by R:")
        print(f"    {'Symbol':>8s}  {'N':>4s}  {'TotalR':>8s}")
        for sym in sorted(sym_r, key=sym_r.get, reverse=True)[:n_top]:
            print(f"    {sym:>8s}  {sym_n[sym]:4d}  {sym_r[sym]:+8.1f}")


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    print("=" * 130)
    print("THREE-SETUP AUDIT: EMA PULL SHORT + EMA FPIP + CLEAN PROMOTED STACK")
    print("SC / 2ND_CHANCE: ALREADY DEAD (PF 0.48, sc_repair_study exhaustive). RETIRE.")
    print("=" * 130)

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

    # ══════════════════════════════════════════════════════════
    #  SECTION 1: EMA PULL SHORT — ABLATION
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("SECTION 1 — EMA PULL SHORT ABLATION")
    print("Baseline: ep_short_only=True, ep_time_end=1400, ep_require_regime=False")
    print("=" * 130)

    ep_variants = [
        ("EP baseline (short, ≤14:00)",       _ep_baseline_cfg(),      {SetupId.EMA_PULL}),
        ("EP extended (short, ≤15:00)",        _ep_extended_time_cfg(), {SetupId.EMA_PULL}),
        ("EP full-day (short, ≤15:30)",        _ep_full_day_cfg(),      {SetupId.EMA_PULL}),
        ("EP both sides (long+short, ≤14:00)", _ep_both_sides_cfg(),    {SetupId.EMA_PULL}),
        ("EP with regime gate",                _ep_with_regime_cfg(),   {SetupId.EMA_PULL}),
    ]

    print(f"\n{HEADER}")
    print(DIVIDER)

    ep_baseline_trades = None
    for label, cfg, filt in ep_variants:
        trades = _run_all_symbols(symbols, cfg, spy_bars, qqq_bars, sector_bars_dict, setup_filter=filt)
        m = compute_metrics(trades)
        print(fmt_row(label, m))
        if ep_baseline_trades is None:
            ep_baseline_trades = trades

    # Detailed baseline summary
    _, ep_verdict = _print_setup_summary("EMA PULL SHORT (baseline)", ep_baseline_trades, date_range, n_symbols)
    _print_monthly("EP Short", ep_baseline_trades)
    _print_top_symbols("EP Short", ep_baseline_trades)

    # Side breakdown for "both sides" variant
    both_cfg = _ep_both_sides_cfg()
    both_trades = _run_all_symbols(symbols, both_cfg, spy_bars, qqq_bars, sector_bars_dict,
                                     setup_filter={SetupId.EMA_PULL})
    longs = [t for t in both_trades if t.side == "LONG"]
    shorts = [t for t in both_trades if t.side == "SHORT"]
    print(f"\n  EP Both Sides — Side Breakdown:")
    print(f"    Longs:  N={len(longs)}, PF={pf_str(_pf(longs))}, "
          f"TotalR={sum(t.pnl_rr for t in longs):+.1f}")
    print(f"    Shorts: N={len(shorts)}, PF={pf_str(_pf(shorts))}, "
          f"TotalR={sum(t.pnl_rr for t in shorts):+.1f}")

    _export_csv(ep_baseline_trades, OUT_DIR / "replay_ep_audit_baseline.csv", "ep_baseline")

    # ══════════════════════════════════════════════════════════
    #  SECTION 2: EMA FPIP — BASELINE + GATE RELAXATION
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("SECTION 2 — EMA FPIP AUDIT (BASELINE + GATE RELAXATION)")
    print("Default: all context gates ON, RVOL≥1.5, quality≥4")
    print("=" * 130)

    fpip_variants = [
        ("FPIP baseline (all gates on)",         _fpip_baseline_cfg(),     {SetupId.EMA_FPIP}),
        ("FPIP no market align",                 _fpip_no_market_cfg(),    {SetupId.EMA_FPIP}),
        ("FPIP no VWAP align",                   _fpip_no_vwap_cfg(),      {SetupId.EMA_FPIP}),
        ("FPIP no EMA20 slope",                  _fpip_no_ema20_cfg(),     {SetupId.EMA_FPIP}),
        ("FPIP relaxed RVOL (1.0)",              _fpip_relaxed_rvol_cfg(), {SetupId.EMA_FPIP}),
        ("FPIP quality=0",                       _fpip_q0_cfg(),           {SetupId.EMA_FPIP}),
        ("FPIP no context (all 3 off)",          _fpip_no_context_cfg(),   {SetupId.EMA_FPIP}),
        ("FPIP wide open (no ctx + RVOL=1 + Q0)", _fpip_wide_open_cfg(), {SetupId.EMA_FPIP}),
    ]

    print(f"\n{HEADER}")
    print(DIVIDER)

    fpip_baseline_trades = None
    best_fpip_pf = 0.0
    best_fpip_label = ""
    best_fpip_trades = []

    for label, cfg, filt in fpip_variants:
        trades = _run_all_symbols(symbols, cfg, spy_bars, qqq_bars, sector_bars_dict, setup_filter=filt)
        m = compute_metrics(trades)
        print(fmt_row(label, m))
        if fpip_baseline_trades is None:
            fpip_baseline_trades = trades
        if m["n"] >= 10 and m["pf"] > best_fpip_pf:
            best_fpip_pf = m["pf"]
            best_fpip_label = label
            best_fpip_trades = trades

    # Detailed baseline summary
    if fpip_baseline_trades:
        _, fpip_verdict = _print_setup_summary("EMA FPIP (baseline)", fpip_baseline_trades, date_range, n_symbols)
    else:
        fpip_verdict = "NO TRADES"
        print("\n  EMA FPIP baseline: 0 trades generated")

    # Best FPIP variant detail
    if best_fpip_trades:
        print(f"\n  ── Best FPIP variant: {best_fpip_label} ──")
        _, best_fpip_verdict = _print_setup_summary(f"FPIP BEST ({best_fpip_label})", best_fpip_trades, date_range, n_symbols)
        _print_monthly("FPIP best", best_fpip_trades)
        _print_top_symbols("FPIP best", best_fpip_trades)
        _export_csv(best_fpip_trades, OUT_DIR / "replay_fpip_audit_best.csv", "fpip_best")

    # Also export baseline
    if fpip_baseline_trades:
        _export_csv(fpip_baseline_trades, OUT_DIR / "replay_fpip_audit_baseline.csv", "fpip_baseline")

    # ══════════════════════════════════════════════════════════
    #  SECTION 3: CLEAN PROMOTED STACK (BDR + FLR only)
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("SECTION 3 — CLEAN PROMOTED STACK (BDR Short + FLR only)")
    print("SC Long: RETIRED (PF 0.48). EMA Pull Short: under audit.")
    print("=" * 130)

    cfg_clean = _bdr_flr_cfg()
    clean_ids = {SetupId.BDR_SHORT, SetupId.FL_MOMENTUM_REBUILD}
    clean_trades = _run_all_symbols(symbols, cfg_clean, spy_bars, qqq_bars,
                                     sector_bars_dict, setup_filter=clean_ids)

    bdr_clean = [t for t in clean_trades if t.setup_id == SetupId.BDR_SHORT]
    flr_clean = [t for t in clean_trades if t.setup_id == SetupId.FL_MOMENTUM_REBUILD]

    print(f"\n{HEADER}")
    print(DIVIDER)
    print(fmt_row("BDR Short (standalone in stack)", compute_metrics(bdr_clean)))
    print(fmt_row("FLR (standalone in stack)", compute_metrics(flr_clean)))
    print(fmt_row("CLEAN STACK (BDR + FLR combined)", compute_metrics(clean_trades)))

    # ══════════════════════════════════════════════════════════
    #  SECTION 4: CLEAN STACK CAPPED (max 3)
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("SECTION 4 — CLEAN STACK CAPPED (max 3 concurrent)")
    print("=" * 130)

    capped = _capped_portfolio(clean_trades, max_open=3)
    bdr_capped = [t for t in capped if t.setup_id == SetupId.BDR_SHORT]
    flr_capped = [t for t in capped if t.setup_id == SetupId.FL_MOMENTUM_REBUILD]

    print(f"\n{HEADER}")
    print(DIVIDER)
    print(fmt_row("Capped - BDR Short", compute_metrics(bdr_capped)))
    print(fmt_row("Capped - FLR", compute_metrics(flr_capped)))
    print(fmt_row("Capped - ALL", compute_metrics(capped)))

    # Verify cap
    capped_sorted = sorted(capped, key=lambda t: t.entry_time)
    max_capped = 0
    for i, t in enumerate(capped_sorted):
        concurrent = sum(1 for o in capped_sorted[:i+1] if o.exit_time > t.entry_time)
        if concurrent > max_capped:
            max_capped = concurrent
    print(f"\n  Peak concurrent (capped): {max_capped} (should be ≤ 3)")

    # Full detail
    _, clean_verdict = _print_setup_summary("CLEAN STACK (BDR + FLR)", clean_trades, date_range, n_symbols)
    _print_monthly("Clean Stack", clean_trades)
    _print_monthly("  BDR in stack", bdr_clean)
    _print_monthly("  FLR in stack", flr_clean)

    _export_csv(clean_trades, OUT_DIR / "replay_clean_stack.csv", "clean_stack")
    _export_csv(capped, OUT_DIR / "replay_clean_stack_capped.csv", "clean_stack_capped")

    # ══════════════════════════════════════════════════════════
    #  FINAL VERDICTS
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("FINAL VERDICTS")
    print("=" * 130)

    print(f"\n  SC / 2ND_CHANCE:     RETIRE (PF 0.48, exhaustively tested, no fix)")
    print(f"  EMA PULL SHORT:      {ep_verdict} (PF {pf_str(compute_metrics(ep_baseline_trades)['pf'])}, N={len(ep_baseline_trades)})")

    if fpip_baseline_trades:
        fpip_m = compute_metrics(fpip_baseline_trades)
        print(f"  EMA FPIP (baseline): {fpip_verdict} (PF {pf_str(fpip_m['pf'])}, N={fpip_m['n']})")
    else:
        print(f"  EMA FPIP (baseline): NO TRADES")

    if best_fpip_trades:
        best_m = compute_metrics(best_fpip_trades)
        print(f"  EMA FPIP (best):     PF {pf_str(best_m['pf'])}, N={best_m['n']} [{best_fpip_label}]")

    clean_m = compute_metrics(clean_trades)
    capped_m = compute_metrics(capped)
    print(f"\n  CLEAN PROMOTED STACK (BDR + FLR):")
    print(f"    Unconstrained:  N={clean_m['n']}, PF={pf_str(clean_m['pf'])}, "
          f"Exp={clean_m['exp']:+.3f}R, TotalR={clean_m['total_r']:+.1f}")
    print(f"    Capped (max 3): N={capped_m['n']}, PF={pf_str(capped_m['pf'])}, "
          f"Exp={capped_m['exp']:+.3f}R, TotalR={capped_m['total_r']:+.1f}")

    print("\n" + "=" * 130)
    print("DONE.")
    print("=" * 130)


if __name__ == "__main__":
    main()
