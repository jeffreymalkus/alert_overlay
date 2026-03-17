"""
9EMA Long Strategy Replay — Validates FL_MOMENTUM_REBUILD, EMA9_FIRST_PB, EMA9_BACKSIDE_RB.

Sections:
  1. PER-SETUP STANDALONE — each new setup in isolation
  2. NEW-ONLY COMBINED — all 3 new setups together
  3. PROMOTION VERDICTS

Usage:
    cd /path/to/project
    python -m alert_overlay.replays.ema9_long_replay
"""

import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from ..backtest import load_bars_from_csv, run_backtest
from ..config import OverlayConfig
from ..models import NaN, SetupId, SETUP_DISPLAY_NAME
from ..market_context import SECTOR_MAP, get_sector_etf

# Reuse metrics from new_strategy_replay
from .new_strategy_replay import (
    PTrade, compute_metrics, fmt_row, pf_str, HEADER, DIVIDER,
    _pf, _print_side_split, _print_top_symbols, _run_all_symbols,
)

DATA_DIR = Path(__file__).parent.parent / "data"


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
    cfg.show_fl_momentum_rebuild = False
    cfg.show_ema9_first_pb = False
    cfg.show_ema9_backside_rb = False
    cfg.require_regime = False
    return cfg


def _flr_cfg() -> OverlayConfig:
    cfg = _base_cfg()
    cfg.show_fl_momentum_rebuild = True
    return cfg


def _e9pb_cfg() -> OverlayConfig:
    cfg = _base_cfg()
    cfg.show_ema9_first_pb = True
    return cfg


def _e9rb_cfg() -> OverlayConfig:
    cfg = _base_cfg()
    cfg.show_ema9_backside_rb = True
    return cfg


def _all_3_cfg() -> OverlayConfig:
    """All 3 new 9EMA long setups together."""
    cfg = _base_cfg()
    cfg.show_fl_momentum_rebuild = True
    cfg.show_ema9_first_pb = True
    cfg.show_ema9_backside_rb = True
    return cfg


NEW_9EMA_IDS = {
    SetupId.FL_MOMENTUM_REBUILD,
    SetupId.EMA9_FIRST_PB,
    SetupId.EMA9_BACKSIDE_RB,
}


# ════════════════════════════════════════════════════════════════
#  Promotion verdict
# ════════════════════════════════════════════════════════════════

def _verdict(m: dict) -> str:
    """C1: PF>1.0, C2: Exp>0, C3: TrainPF>0.80, C4: TestPF>0.80, N>=10."""
    if m["n"] < 10:
        return "SKIP (N<10)"
    c1 = m["pf"] > 1.0
    c2 = m["exp"] > 0
    c3 = m["train_pf"] > 0.80
    c4 = m["test_pf"] > 0.80
    passed = sum([c1, c2, c3, c4])
    if passed == 4:
        return "PROMOTE"
    elif passed >= 3:
        return "CONTINUE"
    else:
        return "FAIL"


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

    symbols = [s.strip() for s in wl_path.read_text().splitlines()
               if s.strip() and not s.startswith("#")]
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

    spy_range = (f"{spy_bars[0].timestamp.date()} → {spy_bars[-1].timestamp.date()}"
                 if spy_bars else "N/A")
    print(f"Date range: {spy_range}\n")

    # ── Section 1: Per-setup standalone ──
    print("=" * 150)
    print("  SECTION 1: PER-SETUP STANDALONE (each 9EMA long setup in isolation)")
    print("=" * 150)
    print(HEADER)
    print(DIVIDER)

    setup_configs = [
        ("FL_MOMENTUM_REBUILD", _flr_cfg(), {SetupId.FL_MOMENTUM_REBUILD}),
        ("EMA9_FIRST_PB", _e9pb_cfg(), {SetupId.EMA9_FIRST_PB}),
        ("EMA9_BACKSIDE_RB", _e9rb_cfg(), {SetupId.EMA9_BACKSIDE_RB}),
    ]

    all_trades: Dict[str, List[PTrade]] = {}
    all_metrics: Dict[str, dict] = {}

    for label, cfg, setup_ids in setup_configs:
        print(f"\n  Running {label}...", end="", flush=True)
        trades = _run_all_symbols(symbols, cfg, spy_bars, qqq_bars, sector_bars_dict,
                                  setup_filter=setup_ids)
        all_trades[label] = trades
        m = compute_metrics(trades)
        all_metrics[label] = m
        print(f"\r{fmt_row(label, m)}")
        # Long-only, but print side split for sanity
        _print_side_split(label, trades)
        _print_top_symbols(label, trades, top_n=5)

    # ── Section 2: All 3 combined ──
    print(f"\n{'=' * 150}")
    print("  SECTION 2: ALL 3 9EMA LONG SETUPS COMBINED")
    print("=" * 150)
    print(HEADER)
    print(DIVIDER)

    combo_cfg = _all_3_cfg()
    print(f"\n  Running 9EMA_LONG_COMBINED...", end="", flush=True)
    combo_trades = _run_all_symbols(symbols, combo_cfg, spy_bars, qqq_bars,
                                    sector_bars_dict, setup_filter=NEW_9EMA_IDS)
    combo_m = compute_metrics(combo_trades)
    print(f"\r{fmt_row('9EMA_LONG_COMBINED (all 3)', combo_m)}")

    # Per-setup breakdown
    print(f"\n  Per-setup breakdown:")
    for sid in [SetupId.FL_MOMENTUM_REBUILD, SetupId.EMA9_FIRST_PB, SetupId.EMA9_BACKSIDE_RB]:
        sub = [t for t in combo_trades if t.setup_id == sid]
        if sub:
            label = SETUP_DISPLAY_NAME.get(sid, str(sid))
            print(fmt_row(f"  {label}", compute_metrics(sub)))

    # ── Section 3: Promotion Verdicts ──
    print(f"\n{'=' * 150}")
    print("  SECTION 3: PROMOTION VERDICTS")
    print("=" * 150)
    print(f"\n  Criteria: PF(R)>1.0, Exp(R)>0, TrainPF>0.80, TestPF>0.80, N>=10")
    print(f"  Baseline: SC Long Q>=5 → PF 1.19, Exp +0.173R")
    print()

    for label, m in all_metrics.items():
        v = _verdict(m)
        beats_baseline = m["pf"] > 1.19 and m["exp"] > 0.173 and m["n"] >= 30
        beat_str = " ★ BEATS BASELINE" if beats_baseline else ""
        print(f"  {label:35s}  N={m['n']:5d}  PF={pf_str(m['pf'])}  "
              f"Exp={m['exp']:+.3f}  TrnPF={pf_str(m['train_pf'])}  "
              f"TstPF={pf_str(m['test_pf'])}  → {v}{beat_str}")

    # Combined verdict
    v_combo = _verdict(combo_m)
    print(f"\n  {'9EMA_LONG_COMBINED':35s}  N={combo_m['n']:5d}  PF={pf_str(combo_m['pf'])}  "
          f"Exp={combo_m['exp']:+.3f}  TrnPF={pf_str(combo_m['train_pf'])}  "
          f"TstPF={pf_str(combo_m['test_pf'])}  → {v_combo}")

    print(f"\n  Done.")


if __name__ == "__main__":
    main()
