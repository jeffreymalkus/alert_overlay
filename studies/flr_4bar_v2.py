"""FLR 4-BAR + stop=0.50 — Deep robustness check"""
import sys
from collections import defaultdict
from pathlib import Path
from ..backtest import load_bars_from_csv
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
    cfg.flr_turn_confirm_bars = 4
    cfg.flr_stop_frac = 0.50
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
    print(f"Universe: {len(all_symbols)} symbols\n")

    cfg = _flr_cfg()
    trades = _run_all_symbols(all_symbols, cfg, spy_bars, qqq_bars,
                              sector_bars_dict, setup_filter=FLR_IDS)
    m = compute_metrics(trades)
    print(f"═══ 4-BAR + stop=0.50 FULL UNIVERSE ═══")
    print(f"  N={m['n']}  PF={pf_str(m['pf'])}  Exp={m['exp']:+.3f}  WR={m['wr']*100:.1f}%  "
          f"trn={pf_str(m['train_pf'])} tst={pf_str(m['test_pf'])}")

    # ═══ Ex-best-day ═══
    print(f"\n═══ ROBUSTNESS ═══")
    day_r = defaultdict(float)
    day_n = defaultdict(int)
    for t in trades:
        day_r[t.entry_date] += t.pnl_rr
        day_n[t.entry_date] += 1
    best_day = max(day_r, key=day_r.get)
    worst_day = min(day_r, key=day_r.get)
    ex_best = [t for t in trades if t.entry_date != best_day]
    ex_worst = [t for t in trades if t.entry_date != worst_day]
    m_exb = compute_metrics(ex_best)
    m_exw = compute_metrics(ex_worst)
    print(f"  Best day:  {best_day}  {day_r[best_day]:+.1f}R  ({day_n[best_day]} trades)")
    print(f"  Ex-best-day:   PF={pf_str(m_exb['pf'])}  Exp={m_exb['exp']:+.3f}")
    print(f"  Worst day: {worst_day}  {day_r[worst_day]:+.1f}R  ({day_n[worst_day]} trades)")
    print(f"  Ex-worst-day:  PF={pf_str(m_exw['pf'])}  Exp={m_exw['exp']:+.3f}")

    sym_r = defaultdict(float)
    sym_n = defaultdict(int)
    for t in trades:
        sym_r[t.symbol] += t.pnl_rr
        sym_n[t.symbol] += 1
    best_sym = max(sym_r, key=sym_r.get)
    worst_sym = min(sym_r, key=sym_r.get)
    ex_bs = [t for t in trades if t.symbol != best_sym]
    ex_ws = [t for t in trades if t.symbol != worst_sym]
    m_ebs = compute_metrics(ex_bs)
    m_ews = compute_metrics(ex_ws)
    print(f"  Best sym:  {best_sym}  {sym_r[best_sym]:+.1f}R  ({sym_n[best_sym]} trades)")
    print(f"  Ex-best-sym:   PF={pf_str(m_ebs['pf'])}  Exp={m_ebs['exp']:+.3f}")
    print(f"  Worst sym: {worst_sym}  {sym_r[worst_sym]:+.1f}R  ({sym_n[worst_sym]} trades)")
    print(f"  Ex-worst-sym:  PF={pf_str(m_ews['pf'])}  Exp={m_ews['exp']:+.3f}")

    # ═══ Monthly ═══
    print(f"\n═══ MONTHLY BREAKDOWN ═══")
    month_trades = defaultdict(list)
    for t in trades:
        month_trades[t.entry_date.strftime("%Y-%m")].append(t)
    pos = neg = 0
    for month in sorted(month_trades.keys()):
        mt = month_trades[month]
        total_r = sum(t.pnl_rr for t in mt)
        pf_m = compute_metrics(mt)['pf']
        if total_r >= 0: pos += 1
        else: neg += 1
        print(f"  {month}  N={len(mt):3d}  PF={pf_str(pf_m)}  TotalR={total_r:+6.1f}")
    print(f"  Positive: {pos}  Negative: {neg}")

    # ═══ Exit analysis ═══
    print(f"\n═══ EXIT ANALYSIS ═══")
    exit_groups = defaultdict(list)
    for t in trades:
        exit_groups[t.exit_reason].append(t)
    for reason in sorted(exit_groups.keys()):
        grp = exit_groups[reason]
        n = len(grp)
        wr = sum(1 for t in grp if t.pnl_rr > 0) / n * 100
        avg_r = sum(t.pnl_rr for t in grp) / n
        pct = n / len(trades) * 100
        print(f"  {reason:<15s}  N={n:4d} ({pct:4.1f}%)  WR={wr:.1f}%  AvgR={avg_r:+.3f}")

    # ═══ Top/bottom symbols ═══
    print(f"\n═══ TOP 10 / BOTTOM 10 SYMBOLS ═══")
    sorted_syms = sorted(sym_r.items(), key=lambda x: x[1], reverse=True)
    print("  TOP 10:")
    for sym, tr in sorted_syms[:10]:
        wr = sum(1 for t in trades if t.symbol == sym and t.pnl_rr > 0) / sym_n[sym] * 100
        print(f"    {sym:<6s}  N={sym_n[sym]:3d}  TotalR={tr:+6.1f}  WR={wr:.0f}%")
    print("  BOTTOM 10:")
    for sym, tr in sorted_syms[-10:]:
        wr = sum(1 for t in trades if t.symbol == sym and t.pnl_rr > 0) / sym_n[sym] * 100
        print(f"    {sym:<6s}  N={sym_n[sym]:3d}  TotalR={tr:+6.1f}  WR={wr:.0f}%")

    print(f"\nDone.")

if __name__ == "__main__":
    main()
