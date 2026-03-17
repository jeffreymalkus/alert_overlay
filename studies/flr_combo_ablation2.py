"""
FLR Combo Ablation Part 2 — remaining variants + best combos
"""
import sys
from pathlib import Path
from ..backtest import load_bars_from_csv, run_backtest
from ..config import OverlayConfig
from ..models import SetupId
from ..market_context import SECTOR_MAP, get_sector_etf
from ..replays.new_strategy_replay import (
    PTrade, compute_metrics, pf_str, _run_all_symbols,
)

DATA_DIR = Path(__file__).parent.parent / "data"
FLR_IDS = {SetupId.FL_MOMENTUM_REBUILD}


def _base_cfg() -> OverlayConfig:
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


def _verdict(m):
    if m["n"] < 10: return "SKIP"
    c1 = m["pf"] > 1.0
    c2 = m["exp"] > 0
    c3 = m["train_pf"] > 0.80
    c4 = m["test_pf"] > 0.80
    passed = sum([c1, c2, c3, c4])
    if passed == 4: return "★ PROMOTE ★"
    elif passed >= 3: return "CONTINUE"
    return "FAIL"


def main():
    wl_path = DATA_DIR.parent / "watchlist_expanded.txt"
    if not wl_path.exists():
        wl_path = DATA_DIR.parent / "watchlist.txt"
    symbols = [s.strip() for s in wl_path.read_text().splitlines()
               if s.strip() and not s.startswith("#")]
    print(f"Universe: {len(symbols)} symbols")

    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()):
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))
    print(f"Loaded. Running variants...\n")

    variants = [
        # Remaining from Part 1
        ("3-BAR + dec=2.5",
         {"flr_turn_confirm_bars": 3, "flr_min_decline_atr": 2.5}),
        ("3-BAR + Q>=6",
         {"flr_turn_confirm_bars": 3, "min_quality": 6}),
        ("4-BAR TURN",
         {"flr_turn_confirm_bars": 4}),
        ("3-BAR + ema_accel(0.001)",
         {"flr_turn_confirm_bars": 3, "flr_require_ema_accel": True, "flr_ema_accel_min": 0.001}),

        # Best combos from Part 1 findings
        ("3-BAR + stop=0.50 + body=0.65",
         {"flr_turn_confirm_bars": 3, "flr_stop_frac": 0.50, "flr_cross_body_pct": 0.65}),
        ("3-BAR + stop=0.50 + dec=3.5",
         {"flr_turn_confirm_bars": 3, "flr_stop_frac": 0.50, "flr_min_decline_atr": 3.5}),
        ("3-BAR + stop=0.45 + body=0.65",
         {"flr_turn_confirm_bars": 3, "flr_stop_frac": 0.45, "flr_cross_body_pct": 0.65}),

        # Time window experiments
        ("3-BAR + stop=0.50 + time 1030-1200",
         {"flr_turn_confirm_bars": 3, "flr_stop_frac": 0.50, "flr_time_end": 1200}),
        ("3-BAR + stop=0.50 + time 1030-1100",
         {"flr_turn_confirm_bars": 3, "flr_stop_frac": 0.50, "flr_time_end": 1100}),

        # Target experiments: disable measured move, use fixed R:R
        ("3-BAR + stop=0.50 + R:R=1.5",
         {"flr_turn_confirm_bars": 3, "flr_stop_frac": 0.50,
          "flr_target_measured_move": False, "flr_target_r": 1.5}),
        ("3-BAR + stop=0.50 + R:R=2.5",
         {"flr_turn_confirm_bars": 3, "flr_stop_frac": 0.50,
          "flr_target_measured_move": False, "flr_target_r": 2.5}),
    ]

    hdr = f"{'Variant':<42s} {'N':>5s} {'PF':>5s} {'Exp(R)':>7s} {'WR%':>5s} {'TrnPF':>6s} {'TstPF':>6s} {'Stp%':>5s} {'Verdict':>15s}"
    print(hdr)
    print("-" * len(hdr))

    for label, overrides in variants:
        cfg = _flr_cfg(**overrides)
        print(f"  Running {label}...", end="", flush=True)
        trades = _run_all_symbols(symbols, cfg, spy_bars, qqq_bars,
                                  sector_bars_dict, setup_filter=FLR_IDS)
        m = compute_metrics(trades)
        v = _verdict(m)
        n_stop = sum(1 for t in trades if t.exit_reason == "stop")
        stop_pct = (n_stop / len(trades) * 100) if trades else 0
        wr = m['wr'] * 100

        print(f"\r  {label:<42s} {m['n']:5d} {pf_str(m['pf']):>5s} {m['exp']:+7.3f} {wr:5.1f} "
              f"{pf_str(m['train_pf']):>6s} {pf_str(m['test_pf']):>6s} {stop_pct:5.1f} {v:>15s}")

    print("\nDone.")


if __name__ == "__main__":
    main()
