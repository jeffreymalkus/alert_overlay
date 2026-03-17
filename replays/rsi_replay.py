"""
RSI Integration Replay — Corrected 4-section validation.

Sections:
  1. EXISTING-ONLY STANDALONE — baseline with promoted setups, RSI off
  2. RSI-ONLY STANDALONE — only RSI candidates, existing off
  3. COMBINED UNCONSTRAINED — existing + RSI, no position cap
  4. COMBINED CAPPED — existing + RSI, max 3 concurrent, exact exit timestamps

Usage:
    cd /path/to/project
    python -m alert_overlay.rsi_replay
"""

import csv
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import NaN, SetupId, SETUP_DISPLAY_NAME
from ..market_context import SECTOR_MAP, get_sector_etf

DATA_DIR = Path(__file__).parent.parent / "data"
OUT_DIR = Path(__file__).parent.parent / "outputs"


# ════════════════════════════════════════════════════════════════
#  Trade wrapper — now includes exit_time for accurate concurrency
# ════════════════════════════════════════════════════════════════

@dataclass
class PTrade:
    pnl_rr: float
    exit_reason: str
    bars_held: int
    entry_time: datetime
    exit_time: datetime          # actual exit timestamp from backtest
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
    }


def pf_str(v: float) -> str:
    return f"{v:.2f}" if v < 999 else "inf"


def fmt_row(label: str, m: dict) -> str:
    return (
        f"  {label:40s}  {m['n']:5d}  "
        f"{pf_str(m['pf']):>6s}  {m['exp']:+7.3f}  {m['total_r']:+8.1f}  "
        f"{m['max_dd_r']:7.1f}  {m['wr']:5.1f}%  {m['stop_rate']:5.1f}%  "
        f"{m['target_rate']:5.1f}%  {m['time_rate']:5.1f}%  "
        f"{pf_str(m['train_pf']):>6s}  {pf_str(m['test_pf']):>6s}  "
        f"{pf_str(m['ex_best_day_pf']):>6s}  {pf_str(m['ex_top_sym_pf']):>6s}  "
        f"{m['pct_pos_days']:5.1f}%"
    )


HEADER = (
    f"  {'Label':40s}  {'N':>5s}  "
    f"{'PF(R)':>6s}  {'Exp(R)':>7s}  {'TotalR':>8s}  "
    f"{'MaxDD':>7s}  {'WR%':>6s}  {'Stop%':>6s}  "
    f"{'Tgt%':>6s}  {'Time%':>6s}  "
    f"{'TrnPF':>6s}  {'TstPF':>6s}  "
    f"{'ExDay':>6s}  {'ExSym':>6s}  "
    f"{'%Pos':>6s}"
)
DIVIDER = "  " + "-" * 140


# ════════════════════════════════════════════════════════════════
#  Config helpers
# ════════════════════════════════════════════════════════════════

def _base_cfg() -> OverlayConfig:
    """Create config with ALL setups disabled."""
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


def _existing_only_cfg() -> OverlayConfig:
    """Config with only the currently promoted existing setups."""
    cfg = _base_cfg()
    cfg.show_second_chance = True
    cfg.sc_min_quality = 5
    cfg.sc_require_regime = False
    cfg.sc_long_only = True
    cfg.show_breakdown_retest = True
    cfg.bdr_require_red_trend = True
    cfg.bdr_am_only = True
    cfg.bdr_require_regime = True
    cfg.show_ema_pullback = True
    cfg.show_trend_setups = True  # needed for EMA_PULL engine path
    cfg.ep_time_end = 1400
    cfg.ep_short_only = True
    cfg.ep_require_regime = False
    return cfg


def _rsi_only_cfg() -> OverlayConfig:
    """Config with only RSI setups enabled."""
    cfg = _base_cfg()
    cfg.show_rsi_midline_long = True
    cfg.show_rsi_bouncefail_short = True
    cfg.rsi_require_regime = False
    cfg.require_regime = False
    return cfg


def _combined_cfg() -> OverlayConfig:
    """Config with existing promoted + RSI setups."""
    cfg = _existing_only_cfg()
    cfg.show_rsi_midline_long = True
    cfg.show_rsi_bouncefail_short = True
    cfg.rsi_require_regime = False
    return cfg


# ════════════════════════════════════════════════════════════════
#  Runner — converts backtest Trade → PTrade (with exit_time)
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

            # exit_time: use actual backtest exit_time if available
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
#  Capped portfolio — uses ACTUAL exit timestamps
# ════════════════════════════════════════════════════════════════

def _capped_portfolio(trades: List[PTrade], max_open: int = 3) -> List[PTrade]:
    """Apply max concurrent position cap using real entry/exit timestamps.

    Priority rules:
      - existing promoted setups get priority over RSI setups
      - same-symbol exclusion: one position per symbol at a time
      - earlier entry time wins ties
    """
    rsi_ids = {SetupId.RSI_MIDLINE_LONG, SetupId.RSI_BOUNCEFAIL_SHORT}

    # Sort: existing first (priority), then by entry time
    def sort_key(t: PTrade):
        is_rsi = 1 if t.setup_id in rsi_ids else 0
        return (t.entry_time, is_rsi)

    trades_sorted = sorted(trades, key=sort_key)

    accepted: List[PTrade] = []
    # Track open positions as (exit_time, symbol)
    open_positions: List[Tuple[datetime, str]] = []

    for t in trades_sorted:
        # Expire positions whose exit_time <= this trade's entry_time
        open_positions = [
            (exit_t, sym) for (exit_t, sym) in open_positions
            if exit_t > t.entry_time
        ]

        # Same-symbol exclusion
        open_syms = {sym for (_, sym) in open_positions}
        if t.symbol in open_syms:
            continue

        # Cap check
        if len(open_positions) >= max_open:
            continue

        accepted.append(t)
        open_positions.append((t.exit_time, t.symbol))

    return accepted


# ════════════════════════════════════════════════════════════════
#  CSV export
# ════════════════════════════════════════════════════════════════

def _export_csv(trades: List[PTrade], path: Path, label: str):
    """Write trade log CSV."""
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
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    print("=" * 150)
    print("RSI INTEGRATION REPLAY — Corrected 4-section validation")
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
    print(f"  SPY date range: {spy_dates[0]} → {spy_dates[-1]} ({len(spy_dates)} trading days)")

    excluded = {"SPY", "QQQ", "IWM"} | set(sector_etfs)
    all_data_files = sorted(DATA_DIR.glob("*_5min.csv"))
    symbols = sorted([
        p.stem.replace("_5min", "")
        for p in all_data_files
        if p.stem.replace("_5min", "") not in excluded
    ])
    print(f"  Trading symbols: {len(symbols)}")

    existing_ids = {SetupId.SECOND_CHANCE, SetupId.BDR_SHORT, SetupId.EMA_PULL}
    rsi_ids = {SetupId.RSI_MIDLINE_LONG, SetupId.RSI_BOUNCEFAIL_SHORT}

    # ══════════════════════════════════════════════════════════
    #  SECTION 1: EXISTING-ONLY STANDALONE REPLAY
    # ══════════════════════════════════════════════════════════
    print("\n  Running SECTION 1: Existing-only standalone...")
    cfg_existing = _existing_only_cfg()
    existing_trades = _run_all_symbols(symbols, cfg_existing, spy_bars, qqq_bars,
                                        sector_bars_dict, setup_filter=existing_ids)

    print("\n" + "=" * 150)
    print("SECTION 1 — EXISTING-ONLY STANDALONE REPLAY (baseline)")
    print("  Judge existing setups from THIS section.")
    print("=" * 150)
    print(HEADER)
    print(DIVIDER)
    for label, trades in [
        ("SC Long (Q≥5)", [t for t in existing_trades if t.setup_id == SetupId.SECOND_CHANCE]),
        ("BDR SHORT", [t for t in existing_trades if t.setup_id == SetupId.BDR_SHORT]),
        ("EMA PULL SHORT", [t for t in existing_trades if t.setup_id == SetupId.EMA_PULL]),
        ("ALL EXISTING", existing_trades),
    ]:
        print(fmt_row(label, compute_metrics(trades)))

    _export_csv(existing_trades, OUT_DIR / "replay_existing_only.csv", "existing_only")

    # ══════════════════════════════════════════════════════════
    #  SECTION 2: RSI-ONLY STANDALONE REPLAY
    # ══════════════════════════════════════════════════════════
    print("\n  Running SECTION 2: RSI-only standalone...")
    cfg_rsi = _rsi_only_cfg()
    rsi_trades = _run_all_symbols(symbols, cfg_rsi, spy_bars, qqq_bars,
                                   sector_bars_dict, setup_filter=rsi_ids)
    rsi_long = [t for t in rsi_trades if t.setup_id == SetupId.RSI_MIDLINE_LONG]
    rsi_short = [t for t in rsi_trades if t.setup_id == SetupId.RSI_BOUNCEFAIL_SHORT]

    print("\n" + "=" * 150)
    print("SECTION 2 — RSI-ONLY STANDALONE REPLAY")
    print("  Judge RSI setups from THIS section.")
    print("=" * 150)
    print(HEADER)
    print(DIVIDER)
    for label, trades in [
        ("RSI_MIDLINE_LONG (isolated)", rsi_long),
        ("RSI_BOUNCEFAIL_SHORT (isolated)", rsi_short),
        ("RSI Combined (isolated)", rsi_trades),
    ]:
        print(fmt_row(label, compute_metrics(trades)))

    # Sample trades
    for label, trades in [("RSI LONG", rsi_long), ("RSI SHORT", rsi_short)]:
        if trades:
            print(f"\n  {label} — Sample trades (first 10):")
            print(f"    {'Date':>12s}  {'Entry':>16s}  {'Exit':>16s}  {'Symbol':>6s}  "
                  f"{'Side':>5s}  {'R':>7s}  {'Exit':>8s}  {'Bars':>4s}  {'Q':>2s}")
            for t in sorted(trades, key=lambda x: x.entry_time)[:10]:
                print(f"    {str(t.entry_date):>12s}  "
                      f"{t.entry_time.strftime('%Y-%m-%d %H:%M'):>16s}  "
                      f"{t.exit_time.strftime('%Y-%m-%d %H:%M'):>16s}  "
                      f"{t.symbol:>6s}  {t.side:>5s}  {t.pnl_rr:+7.3f}  "
                      f"{t.exit_reason:>8s}  {t.bars_held:4d}  {t.quality:2d}")

    _export_csv(rsi_trades, OUT_DIR / "replay_rsi_only.csv", "rsi_only")

    # ══════════════════════════════════════════════════════════
    #  SECTION 3: COMBINED UNCONSTRAINED REPLAY
    # ══════════════════════════════════════════════════════════
    print("\n  Running SECTION 3: Combined unconstrained...")
    cfg_comb = _combined_cfg()
    combined_all = _run_all_symbols(symbols, cfg_comb, spy_bars, qqq_bars, sector_bars_dict)

    combined_existing = [t for t in combined_all if t.setup_id in existing_ids]
    combined_rsi = [t for t in combined_all if t.setup_id in rsi_ids]

    print("\n" + "=" * 150)
    print("SECTION 3 — COMBINED UNCONSTRAINED REPLAY (overlap/interaction inspection)")
    print("  Do NOT use this to judge individual setup quality.")
    print("=" * 150)
    print(HEADER)
    print(DIVIDER)
    for label, trades in [
        ("Combined - SC Long", [t for t in combined_existing if t.setup_id == SetupId.SECOND_CHANCE]),
        ("Combined - BDR SHORT", [t for t in combined_existing if t.setup_id == SetupId.BDR_SHORT]),
        ("Combined - EMA PULL SHORT", [t for t in combined_existing if t.setup_id == SetupId.EMA_PULL]),
        ("Combined - RSI_MIDLINE_LONG", [t for t in combined_rsi if t.setup_id == SetupId.RSI_MIDLINE_LONG]),
        ("Combined - RSI_BOUNCEFAIL_SHORT", [t for t in combined_rsi if t.setup_id == SetupId.RSI_BOUNCEFAIL_SHORT]),
        ("Combined - ALL", combined_all),
    ]:
        print(fmt_row(label, compute_metrics(trades)))

    # Overlap analysis
    print(f"\n  OVERLAP ANALYSIS:")
    existing_entries = set()
    for t in combined_existing:
        existing_entries.add((t.entry_date, t.symbol))

    rsi_overlaps = 0
    rsi_overlap_details = []
    for t in combined_rsi:
        key = (t.entry_date, t.symbol)
        if key in existing_entries:
            rsi_overlaps += 1
            rsi_overlap_details.append(f"    {t.entry_date} {t.symbol} {t.setup}")

    print(f"  RSI trades with same-day same-symbol as existing: {rsi_overlaps} / {len(combined_rsi)}")
    if rsi_overlap_details[:10]:
        print("  First 10 overlaps:")
        for d in rsi_overlap_details[:10]:
            print(d)

    # Exact timestamp collisions
    existing_ts = set()
    for t in combined_existing:
        existing_ts.add((t.entry_time, t.symbol))
    ts_collisions = sum(1 for t in combined_rsi if (t.entry_time, t.symbol) in existing_ts)
    print(f"  Exact timestamp collisions: {ts_collisions}")

    # Concurrent position analysis
    all_sorted = sorted(combined_all, key=lambda t: t.entry_time)
    max_concurrent = 0
    for i, t in enumerate(all_sorted):
        concurrent = sum(
            1 for other in all_sorted[:i+1]
            if other.exit_time > t.entry_time
        )
        if concurrent > max_concurrent:
            max_concurrent = concurrent
    print(f"  Peak concurrent positions (unconstrained): {max_concurrent}")

    _export_csv(combined_all, OUT_DIR / "replay_combined_unconstrained.csv", "combined_unconstrained")

    # ══════════════════════════════════════════════════════════
    #  SECTION 4: COMBINED CAPPED REPLAY (max 3, exact exit timestamps)
    # ══════════════════════════════════════════════════════════
    print("\n  Running SECTION 4: Combined capped (max 3 concurrent)...")
    capped = _capped_portfolio(combined_all, max_open=3)

    capped_existing = [t for t in capped if t.setup_id in existing_ids]
    capped_rsi = [t for t in capped if t.setup_id in rsi_ids]

    print("\n" + "=" * 150)
    print("SECTION 4 — COMBINED CAPPED REPLAY (max 3 concurrent, actual exit timestamps)")
    print("  Existing promoted setups have priority over RSI candidates.")
    print("=" * 150)
    print(HEADER)
    print(DIVIDER)
    for label, trades in [
        ("Capped - SC Long", [t for t in capped_existing if t.setup_id == SetupId.SECOND_CHANCE]),
        ("Capped - BDR SHORT", [t for t in capped_existing if t.setup_id == SetupId.BDR_SHORT]),
        ("Capped - EMA PULL SHORT", [t for t in capped_existing if t.setup_id == SetupId.EMA_PULL]),
        ("Capped - ALL EXISTING", capped_existing),
        ("Capped - RSI Long", [t for t in capped_rsi if t.setup_id == SetupId.RSI_MIDLINE_LONG]),
        ("Capped - RSI Short", [t for t in capped_rsi if t.setup_id == SetupId.RSI_BOUNCEFAIL_SHORT]),
        ("Capped - ALL RSI", capped_rsi),
        ("Capped - ALL", capped),
    ]:
        print(fmt_row(label, compute_metrics(trades)))

    # Verify capping
    capped_sorted = sorted(capped, key=lambda t: t.entry_time)
    max_capped_concurrent = 0
    for i, t in enumerate(capped_sorted):
        concurrent = sum(
            1 for other in capped_sorted[:i+1]
            if other.exit_time > t.entry_time
        )
        if concurrent > max_capped_concurrent:
            max_capped_concurrent = concurrent
    print(f"\n  Peak concurrent positions (capped): {max_capped_concurrent} (should be ≤ 3)")

    _export_csv(capped, OUT_DIR / "replay_combined_capped.csv", "combined_capped")

    # ══════════════════════════════════════════════════════════
    #  MONTHLY BREAKDOWN (RSI candidates, from Section 2)
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 150)
    print("MONTHLY P&L (from RSI-only standalone replay)")
    print("=" * 150)
    for label, trades in [("RSI Long", rsi_long), ("RSI Short", rsi_short)]:
        if not trades:
            print(f"\n  {label}: no trades")
            continue
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

    # ══════════════════════════════════════════════════════════
    #  PROMOTION RECOMMENDATION
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 150)
    print("PROMOTION RECOMMENDATION (from RSI-only standalone replay)")
    print("=" * 150)

    for label, trades_sub in [
        ("RSI_MIDLINE_LONG", rsi_long),
        ("RSI_BOUNCEFAIL_SHORT", rsi_short),
    ]:
        m = compute_metrics(trades_sub)
        print(f"\n  {label}:")
        c1 = m["pf"] > 1.0
        c2 = m["exp"] > 0
        c3 = m["train_pf"] > 0.80
        c4 = m["test_pf"] > 0.80
        n_ok = m["n"] >= 10
        print(f"    N = {m['n']}  (min 10: {'PASS' if n_ok else 'FAIL'})")
        print(f"    PF(R) = {pf_str(m['pf'])}  (>1.0: {'PASS' if c1 else 'FAIL'})")
        print(f"    Exp(R) = {m['exp']:+.3f}  (>0: {'PASS' if c2 else 'FAIL'})")
        print(f"    Train PF = {pf_str(m['train_pf'])}  (>0.80: {'PASS' if c3 else 'FAIL'})")
        print(f"    Test PF = {pf_str(m['test_pf'])}  (>0.80: {'PASS' if c4 else 'FAIL'})")
        all_pass = c1 and c2 and c3 and c4 and n_ok
        verdict = "PROMOTE" if all_pass else "DO NOT PROMOTE"
        print(f"    → {verdict}")

    print("\n" + "=" * 150)
    print("DONE.")
    print("=" * 150)


if __name__ == "__main__":
    main()
