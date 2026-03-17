"""FLR 4-BAR TURN — Part 1: variants + cross-sample"""
import sys, random
from collections import defaultdict
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

def _base_cfg():
    cfg = OverlayConfig()
    for attr in dir(cfg):
        if attr.startswith("show_"):
            setattr(cfg, attr, False)
    cfg.require_regime = False
    return cfg

def _flr_cfg(**ov):
    cfg = _base_cfg()
    cfg.show_fl_momentum_rebuild = True
    for k, v in ov.items():
        setattr(cfg, k, v)
    return cfg

def main():
    wl_path = DATA_DIR.parent / "watchlist_expanded.txt"
    if not wl_path.exists():
        wl_path = DATA_DIR.parent / "watchlist.txt"
    all_symbols = [s.strip() for s in wl_path.read_text().splitlines()
                   if s.strip() and not s.startswith("#")]
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()):
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))
    print(f"Universe: {len(all_symbols)} symbols. Running...\n")

    # ── Section 1: 4-bar combos ──
    print("═══ 4-BAR TURN VARIANTS ═══")
    variants = [
        ("4-BAR TURN (baseline)", {"flr_turn_confirm_bars": 4}),
        ("4-BAR + stop=0.45", {"flr_turn_confirm_bars": 4, "flr_stop_frac": 0.45}),
        ("4-BAR + stop=0.50", {"flr_turn_confirm_bars": 4, "flr_stop_frac": 0.50}),
        ("4-BAR + body=0.65", {"flr_turn_confirm_bars": 4, "flr_cross_body_pct": 0.65}),
        ("5-BAR TURN", {"flr_turn_confirm_bars": 5}),
    ]
    for label, ov in variants:
        cfg = _flr_cfg(**ov)
        trades = _run_all_symbols(all_symbols, cfg, spy_bars, qqq_bars,
                                  sector_bars_dict, setup_filter=FLR_IDS)
        m = compute_metrics(trades)
        n_stop = sum(1 for t in trades if t.exit_reason == "stop")
        stp = (n_stop / len(trades) * 100) if trades else 0
        print(f"  {label:<35s}  N={m['n']:5d}  PF={pf_str(m['pf'])}  Exp={m['exp']:+.3f}  "
              f"WR={m['wr']*100:.1f}%  trn={pf_str(m['train_pf'])} tst={pf_str(m['test_pf'])}  stp={stp:.0f}%")

    # ── Section 2: Cross-sample ──
    print(f"\n═══ CROSS-SAMPLE (4 groups) ═══")
    random.seed(42)
    shuffled = list(all_symbols)
    random.shuffle(shuffled)
    g = len(shuffled) // 4
    groups = [shuffled[:g], shuffled[g:2*g], shuffled[2*g:3*g], shuffled[3*g:]]
    cfg_4bar = _flr_cfg(flr_turn_confirm_bars=4)
    for gi, grp in enumerate(groups, 1):
        trades = _run_all_symbols(grp, cfg_4bar, spy_bars, qqq_bars,
                                  sector_bars_dict, setup_filter=FLR_IDS)
        m = compute_metrics(trades)
        print(f"  G{gi} ({len(grp):2d} syms): N={m['n']:4d}  PF={pf_str(m['pf'])}  Exp={m['exp']:+.3f}  WR={m['wr']*100:.1f}%")

    print("\nDone.")

if __name__ == "__main__":
    main()
