"""
FL_MOMENTUM_REBUILD — Final Combo Ablation

3-bar turn reached PF 0.97 on full universe. Test combos to push past 1.0:
  1. 3-bar turn + adaptive stop
  2. 3-bar turn + wider stop (0.45, 0.50)
  3. 3-bar turn + body 65%
  4. 3-bar turn + decline 3.5 ATR
  5. 3-bar turn + higher quality floor (Q>=6)
  6. 3-bar turn + adaptive stop + wider stop range
  7. 4-bar turn (more selective)
  8. 3-bar turn + late window (1030-1130)  -- confirm already baked in
  9. 3-bar turn + ema accel (relaxed threshold 0.001)

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.studies.flr_combo_ablation
"""

import sys
from collections import defaultdict
from pathlib import Path
from typing import List, Set

from ..backtest import load_bars_from_csv, run_backtest
from ..config import OverlayConfig
from ..models import NaN, SetupId
from ..market_context import SECTOR_MAP, get_sector_etf

from ..replays.new_strategy_replay import (
    PTrade, compute_metrics, fmt_row, pf_str, HEADER, DIVIDER,
    _pf, _run_all_symbols,
)

DATA_DIR = Path(__file__).parent.parent / "data"

FLR_IDS = {SetupId.FL_MOMENTUM_REBUILD}


def _base_cfg() -> OverlayConfig:
    """All setups off."""
    cfg = OverlayConfig()
    for attr in dir(cfg):
        if attr.startswith("show_"):
            setattr(cfg, attr, False)
    cfg.require_regime = False
    return cfg


def _flr_cfg(**overrides) -> OverlayConfig:
    cfg = _base_cfg()
    cfg.show_fl_momentum_rebuild = True
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _verdict(m: dict) -> str:
    if m["n"] < 10:
        return "SKIP"
    c1 = m["pf"] > 1.0
    c2 = m["exp"] > 0
    c3 = m["train_pf"] > 0.80
    c4 = m["test_pf"] > 0.80
    passed = sum([c1, c2, c3, c4])
    if passed == 4:
        return "★ PROMOTE ★"
    elif passed >= 3:
        return "CONTINUE"
    else:
        return "FAIL"


def main():
    # Load universe
    wl_path = DATA_DIR.parent / "watchlist_expanded.txt"
    if not wl_path.exists():
        wl_path = DATA_DIR.parent / "watchlist.txt"
    symbols = [s.strip() for s in wl_path.read_text().splitlines()
               if s.strip() and not s.startswith("#")]
    print(f"Universe: {len(symbols)} symbols")

    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    print(f"SPY bars: {len(spy_bars)}, QQQ bars: {len(qqq_bars)}")

    sector_etfs = set(SECTOR_MAP.values())
    sector_bars_dict = {}
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))
    print(f"Sector ETFs: {len(sector_bars_dict)}")

    spy_range = (f"{spy_bars[0].timestamp.date()} → {spy_bars[-1].timestamp.date()}"
                 if spy_bars else "N/A")
    print(f"Date range: {spy_range}\n")

    # ── Variant definitions ──
    # Each: (label, config_overrides_dict)
    variants = [
        # Baseline: current optimized defaults (1-bar turn)
        ("BASELINE (1-bar turn)",
         {}),

        # 3-bar turn (the PF 0.97 result)
        ("3-BAR TURN",
         {"flr_turn_confirm_bars": 3}),

        # 3-bar turn + adaptive stop
        ("3-BAR + adaptive_stop",
         {"flr_turn_confirm_bars": 3, "flr_adaptive_stop": True,
          "flr_stop_min_frac": 0.25, "flr_stop_max_frac": 0.50}),

        # 3-bar turn + wider stop 0.45
        ("3-BAR + stop=0.45",
         {"flr_turn_confirm_bars": 3, "flr_stop_frac": 0.45}),

        # 3-bar turn + wider stop 0.50
        ("3-BAR + stop=0.50",
         {"flr_turn_confirm_bars": 3, "flr_stop_frac": 0.50}),

        # 3-bar turn + body 65%
        ("3-BAR + body=0.65",
         {"flr_turn_confirm_bars": 3, "flr_cross_body_pct": 0.65}),

        # 3-bar turn + decline 3.5 ATR
        ("3-BAR + dec=3.5",
         {"flr_turn_confirm_bars": 3, "flr_min_decline_atr": 3.5}),

        # 3-bar turn + decline 2.5 ATR (looser)
        ("3-BAR + dec=2.5",
         {"flr_turn_confirm_bars": 3, "flr_min_decline_atr": 2.5}),

        # 3-bar turn + higher quality floor
        ("3-BAR + Q>=6",
         {"flr_turn_confirm_bars": 3, "min_quality": 6}),

        # 4-bar turn (even more selective)
        ("4-BAR TURN",
         {"flr_turn_confirm_bars": 4}),

        # 3-bar turn + adaptive stop wider range
        ("3-BAR + adapt(0.30-0.55)",
         {"flr_turn_confirm_bars": 3, "flr_adaptive_stop": True,
          "flr_stop_min_frac": 0.30, "flr_stop_max_frac": 0.55}),

        # 3-bar turn + relaxed ema accel
        ("3-BAR + ema_accel(0.001)",
         {"flr_turn_confirm_bars": 3, "flr_require_ema_accel": True,
          "flr_ema_accel_min": 0.001}),

        # 3-bar turn + body 65% + stop 0.45
        ("3-BAR + body65 + stop45",
         {"flr_turn_confirm_bars": 3, "flr_cross_body_pct": 0.65,
          "flr_stop_frac": 0.45}),

        # 3-bar turn + dec 3.5 + stop 0.45
        ("3-BAR + dec3.5 + stop45",
         {"flr_turn_confirm_bars": 3, "flr_min_decline_atr": 3.5,
          "flr_stop_frac": 0.45}),

        # 2-bar turn (for comparison)
        ("2-BAR TURN",
         {"flr_turn_confirm_bars": 2}),
    ]

    # ── Run all variants ──
    print("=" * 130)
    print("  FL_MOMENTUM_REBUILD COMBO ABLATION — Full Universe")
    print("=" * 130)
    print(f"{'Variant':<40s}  {'N':>5s}  {'PF':>5s}  {'Exp(R)':>7s}  {'WR%':>5s}  "
          f"{'TrnPF':>5s}  {'TstPF':>5s}  {'Stop%':>5s}  {'Verdict':>15s}")
    print("-" * 130)

    for label, overrides in variants:
        cfg = _flr_cfg(**overrides)
        print(f"  Running {label}...", end="", flush=True)
        trades = _run_all_symbols(symbols, cfg, spy_bars, qqq_bars,
                                  sector_bars_dict, setup_filter=FLR_IDS)
        m = compute_metrics(trades)
        v = _verdict(m)

        # Compute stop %
        n_stop = sum(1 for t in trades if t.exit_reason == "stop")
        stop_pct = (n_stop / len(trades) * 100) if trades else 0

        print(f"\r  {label:<40s}  {m['n']:5d}  {pf_str(m['pf']):>5s}  {m['exp']:+7.3f}  "
              f"{m['wr']*100:5.1f}  {pf_str(m['train_pf']):>5s}  {pf_str(m['test_pf']):>5s}  "
              f"{stop_pct:5.1f}  {v:>15s}")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
