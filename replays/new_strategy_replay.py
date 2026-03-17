"""
New Strategy Replay — Validates HITCHHIKER, FASHIONABLY_LATE, BACKSIDE, RUBBERBAND.

Sections:
  1. PER-SETUP STANDALONE — each new setup in isolation
  2. NEW-ONLY COMBINED — all 4 new setups together
  3. FULL COMBINED — existing promoted + all 4 new setups

Usage:
    cd /path/to/project
    python -m alert_overlay.replays.new_strategy_replay
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
    target_hit = sum(1 for t in trades if t.exit_reason == "target")
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
        "stop_rate": stopped / n * 100, "target_rate": target_hit / n * 100,
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
    cfg.show_hitchhiker = False
    cfg.show_fashionably_late = False
    cfg.show_backside = False
    cfg.show_rubberband = False
    cfg.require_regime = False
    return cfg


def _hitchhiker_cfg() -> OverlayConfig:
    cfg = _base_cfg()
    cfg.show_hitchhiker = True
    return cfg


def _fashionably_late_cfg() -> OverlayConfig:
    cfg = _base_cfg()
    cfg.show_fashionably_late = True
    return cfg


def _backside_cfg() -> OverlayConfig:
    cfg = _base_cfg()
    cfg.show_backside = True
    return cfg


def _rubberband_cfg() -> OverlayConfig:
    cfg = _base_cfg()
    cfg.show_rubberband = True
    return cfg


def _new_only_cfg() -> OverlayConfig:
    """All 4 new setups together."""
    cfg = _base_cfg()
    cfg.show_hitchhiker = True
    cfg.show_fashionably_late = True
    cfg.show_backside = True
    cfg.show_rubberband = True
    return cfg


def _existing_promoted_cfg() -> OverlayConfig:
    """Currently promoted existing setups (SC Long Q>=5, BDR Short, EP Short)."""
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
    cfg.show_trend_setups = True
    cfg.ep_time_end = 1400
    cfg.ep_short_only = True
    cfg.ep_require_regime = False
    return cfg


def _full_combined_cfg() -> OverlayConfig:
    """Existing promoted + all 4 new setups."""
    cfg = _existing_promoted_cfg()
    cfg.show_hitchhiker = True
    cfg.show_fashionably_late = True
    cfg.show_backside = True
    cfg.show_rubberband = True
    return cfg


# ════════════════════════════════════════════════════════════════
#  Runner
# ════════════════════════════════════════════════════════════════

NEW_SETUP_IDS = {
    SetupId.HITCHHIKER, SetupId.FASHIONABLY_LATE,
    SetupId.BACKSIDE, SetupId.RUBBERBAND,
}


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
#  Side-decomposition helper
# ════════════════════════════════════════════════════════════════

def _print_side_split(label: str, trades: List[PTrade]):
    longs = [t for t in trades if t.side == "LONG"]
    shorts = [t for t in trades if t.side == "SHORT"]
    if longs:
        print(fmt_row(f"  {label} LONG", compute_metrics(longs)))
    if shorts:
        print(fmt_row(f"  {label} SHORT", compute_metrics(shorts)))


# ════════════════════════════════════════════════════════════════
#  Symbol-level detail
# ════════════════════════════════════════════════════════════════

def _print_top_symbols(label: str, trades: List[PTrade], top_n: int = 10):
    sym_r: Dict[str, float] = defaultdict(float)
    sym_n: Dict[str, int] = defaultdict(int)
    for t in trades:
        sym_r[t.symbol] += t.pnl_rr
        sym_n[t.symbol] += 1
    if not sym_r:
        return
    ranked = sorted(sym_r.items(), key=lambda x: x[1], reverse=True)
    print(f"\n  Top {top_n} symbols for {label}:")
    for sym, r in ranked[:top_n]:
        n = sym_n[sym]
        pf = _pf([t for t in trades if t.symbol == sym])
        print(f"    {sym:6s}  N={n:3d}  TotalR={r:+7.2f}  PF={pf_str(pf)}")
    if len(ranked) > top_n:
        worst = ranked[-min(5, len(ranked)):]
        print(f"  Bottom {len(worst)} symbols:")
        for sym, r in worst:
            n = sym_n[sym]
            print(f"    {sym:6s}  N={n:3d}  TotalR={r:+7.2f}")


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    # Load universe
    wl_path = DATA_DIR.parent / "watchlist_expanded.txt"
    if not wl_path.exists():
        wl_path = DATA_DIR.parent / "watchlist.txt"
    if not wl_path.exists():
        print(f"ERROR: watchlist not found at {wl_path}")
        sys.exit(1)

    symbols = [s.strip() for s in wl_path.read_text().splitlines() if s.strip() and not s.startswith("#")]
    print(f"Universe: {len(symbols)} symbols")

    # Load SPY/QQQ
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    print(f"SPY bars: {len(spy_bars)}, QQQ bars: {len(qqq_bars)}")

    # Load sector ETFs
    sector_etfs = set(SECTOR_MAP.values())
    sector_bars_dict = {}
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))
    print(f"Sector ETFs loaded: {len(sector_bars_dict)}")

    spy_range = f"{spy_bars[0].timestamp.date()} → {spy_bars[-1].timestamp.date()}" if spy_bars else "N/A"
    print(f"Date range: {spy_range}\n")

    # ── Section 1: Per-setup standalone ──
    print("=" * 150)
    print("  SECTION 1: PER-SETUP STANDALONE (each new setup in isolation)")
    print("=" * 150)
    print(HEADER)
    print(DIVIDER)

    setup_configs = [
        ("HITCHHIKER", _hitchhiker_cfg(), {SetupId.HITCHHIKER}),
        ("FASHIONABLY_LATE", _fashionably_late_cfg(), {SetupId.FASHIONABLY_LATE}),
        ("BACKSIDE", _backside_cfg(), {SetupId.BACKSIDE}),
        ("RUBBERBAND", _rubberband_cfg(), {SetupId.RUBBERBAND}),
    ]

    all_new_trades: Dict[str, List[PTrade]] = {}

    for label, cfg, setup_ids in setup_configs:
        print(f"\n  Running {label}...", end="", flush=True)
        trades = _run_all_symbols(symbols, cfg, spy_bars, qqq_bars, sector_bars_dict,
                                  setup_filter=setup_ids)
        all_new_trades[label] = trades
        m = compute_metrics(trades)
        print(f"\r{fmt_row(label, m)}")
        _print_side_split(label, trades)

    # ── Section 2: New-only combined ──
    print(f"\n{'=' * 150}")
    print("  SECTION 2: NEW-ONLY COMBINED (all 4 new setups together)")
    print("=" * 150)
    print(HEADER)
    print(DIVIDER)

    new_cfg = _new_only_cfg()
    print(f"\n  Running NEW_COMBINED...", end="", flush=True)
    new_trades = _run_all_symbols(symbols, new_cfg, spy_bars, qqq_bars, sector_bars_dict,
                                  setup_filter=NEW_SETUP_IDS)
    m = compute_metrics(new_trades)
    print(f"\r{fmt_row('NEW_COMBINED (all 4)', m)}")
    _print_side_split("NEW_COMBINED", new_trades)

    # Per-setup breakdown within combined
    print(f"\n  Per-setup breakdown within combined:")
    for sid in [SetupId.HITCHHIKER, SetupId.FASHIONABLY_LATE, SetupId.BACKSIDE, SetupId.RUBBERBAND]:
        sub = [t for t in new_trades if t.setup_id == sid]
        if sub:
            label = SETUP_DISPLAY_NAME.get(sid, str(sid))
            print(fmt_row(f"  {label}", compute_metrics(sub)))

    # ── Section 3: Full combined (existing + new) ──
    print(f"\n{'=' * 150}")
    print("  SECTION 3: FULL COMBINED (existing promoted + 4 new setups)")
    print("=" * 150)
    print(HEADER)
    print(DIVIDER)

    full_cfg = _full_combined_cfg()
    print(f"\n  Running FULL_COMBINED...", end="", flush=True)
    full_trades = _run_all_symbols(symbols, full_cfg, spy_bars, qqq_bars, sector_bars_dict)
    m = compute_metrics(full_trades)
    print(f"\r{fmt_row('FULL_COMBINED', m)}")
    _print_side_split("FULL_COMBINED", full_trades)

    # Existing-only baseline for comparison
    exist_cfg = _existing_promoted_cfg()
    print(f"\n  Running EXISTING_BASELINE...", end="", flush=True)
    exist_trades = _run_all_symbols(symbols, exist_cfg, spy_bars, qqq_bars, sector_bars_dict)
    m_exist = compute_metrics(exist_trades)
    print(f"\r{fmt_row('EXISTING_BASELINE', m_exist)}")

    # ── Section 4: Promotion verdicts ──
    print(f"\n{'=' * 150}")
    print("  SECTION 4: PROMOTION VERDICTS")
    print("=" * 150)
    print(f"  Criteria: N >= 10, PF > 1.0, Exp > 0, Train PF > 0.80, Test PF > 0.80\n")

    for label, trades in all_new_trades.items():
        m = compute_metrics(trades)
        c1 = m["n"] >= 10
        c2 = m["pf"] > 1.0
        c3 = m["exp"] > 0
        c4 = m["train_pf"] > 0.80
        c5 = m["test_pf"] > 0.80
        passed = c1 and c2 and c3 and c4 and c5
        verdict = "PROMOTE" if passed else "FAIL"
        flags = f"N={m['n']:>3d}{'✓' if c1 else '✗'} PF={pf_str(m['pf'])}{'✓' if c2 else '✗'} " \
                f"Exp={m['exp']:+.3f}{'✓' if c3 else '✗'} TrnPF={pf_str(m['train_pf'])}{'✓' if c4 else '✗'} " \
                f"TstPF={pf_str(m['test_pf'])}{'✓' if c5 else '✗'}"
        print(f"  {label:25s} → {verdict:8s}  {flags}")

        # Per-side verdict
        for side_label, side_val in [("LONG", "LONG"), ("SHORT", "SHORT")]:
            side_trades = [t for t in trades if t.side == side_val]
            if side_trades:
                ms = compute_metrics(side_trades)
                sc1 = ms["n"] >= 10
                sc2 = ms["pf"] > 1.0
                sc3 = ms["exp"] > 0
                sc4 = ms["train_pf"] > 0.80
                sc5 = ms["test_pf"] > 0.80
                sp = sc1 and sc2 and sc3 and sc4 and sc5
                sv = "PROMOTE" if sp else "FAIL"
                sf = f"N={ms['n']:>3d}{'✓' if sc1 else '✗'} PF={pf_str(ms['pf'])}{'✓' if sc2 else '✗'} " \
                     f"Exp={ms['exp']:+.3f}{'✓' if sc3 else '✗'}"
                print(f"    {side_label:22s} → {sv:8s}  {sf}")

    # ── Top symbols for any promoted setup ──
    for label, trades in all_new_trades.items():
        m = compute_metrics(trades)
        if m["n"] >= 10 and m["pf"] > 1.0:
            _print_top_symbols(label, trades)

    # ── CSV output ──
    OUT_DIR.mkdir(exist_ok=True)
    csv_path = OUT_DIR / "replay_new_strategies.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "setup", "side", "symbol", "entry_time", "exit_time",
            "pnl_rr", "exit_reason", "bars_held", "quality"
        ])
        for label, trades in all_new_trades.items():
            for t in trades:
                writer.writerow([
                    label, t.side, t.symbol,
                    t.entry_time.strftime("%Y-%m-%d %H:%M"),
                    t.exit_time.strftime("%Y-%m-%d %H:%M"),
                    f"{t.pnl_rr:.4f}", t.exit_reason, t.bars_held, t.quality
                ])
    print(f"\n  CSV saved: {csv_path}")
    print(f"\n{'=' * 150}")
    print("  REPLAY COMPLETE")
    print("=" * 150)


if __name__ == "__main__":
    main()
