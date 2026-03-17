"""
EMA PULL SHORT — Ablation Audit.
Baseline: PF 0.94, N=26 (from promoted_replay).
Test: time extension, both sides, regime gate.

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.replays.ep_audit
"""

import csv
from collections import defaultdict
from datetime import date, datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ..backtest import load_bars_from_csv, run_backtest
from ..config import OverlayConfig
from ..models import SetupId, SETUP_DISPLAY_NAME
from ..market_context import SECTOR_MAP, get_sector_etf

DATA_DIR = Path(__file__).parent.parent / "data"
OUT_DIR = Path(__file__).parent.parent / "outputs"

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

def _pf(trades):
    if not trades: return 0.0
    gw = sum(t.pnl_rr for t in trades if t.pnl_rr > 0)
    gl = abs(sum(t.pnl_rr for t in trades if t.pnl_rr <= 0))
    return gw / gl if gl > 0 else float("inf")

def compute_metrics(trades):
    n = len(trades)
    if n == 0:
        return {"n":0,"wr":0,"pf":0,"exp":0,"total_r":0,"train_pf":0,"test_pf":0,
                "ex_best_day_pf":0,"ex_top_sym_pf":0,"stop_rate":0,"target_rate":0,"time_rate":0}
    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in trades)
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    train = [t for t in trades if t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date.day % 2 == 0]
    day_r = defaultdict(float)
    for t in trades: day_r[t.entry_date] += t.pnl_rr
    best_day = max(day_r, key=day_r.get) if day_r else None
    ex_best = [t for t in trades if t.entry_date != best_day] if best_day else trades
    sym_r = defaultdict(float)
    for t in trades: sym_r[t.symbol] += t.pnl_rr
    top_sym = max(sym_r, key=sym_r.get) if sym_r else None
    ex_top = [t for t in trades if t.symbol != top_sym] if top_sym else trades
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    target = sum(1 for t in trades if t.exit_reason == "target")
    timed = sum(1 for t in trades if t.exit_reason in ("time","ema9trail","eod"))
    return {"n":n, "wr":len(wins)/n*100, "pf":pf, "exp":total_r/n,
            "total_r":total_r, "train_pf":_pf(train), "test_pf":_pf(test),
            "ex_best_day_pf":_pf(ex_best), "ex_top_sym_pf":_pf(ex_top),
            "stop_rate":stopped/n*100, "target_rate":target/n*100, "time_rate":timed/n*100}

def pf_str(v): return f"{v:.2f}" if v < 999 else "inf"

def _base_cfg():
    cfg = OverlayConfig()
    for attr in dir(cfg):
        if attr.startswith("show_"): setattr(cfg, attr, False)
    return cfg

def _run(symbols, cfg, spy_bars, qqq_bars, sector_bars_dict, setup_filter=None):
    trades = []
    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists(): continue
        bars = load_bars_from_csv(str(p))
        if not bars: continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars, sector_bars=sec_bars)
        for t in result.trades:
            if setup_filter and t.signal.setup_id not in setup_filter: continue
            et = t.exit_time if t.exit_time else t.signal.timestamp
            trades.append(PTrade(
                pnl_rr=t.pnl_rr, exit_reason=t.exit_reason, bars_held=t.bars_held,
                entry_time=t.signal.timestamp, exit_time=et,
                entry_date=t.signal.timestamp.date(),
                side="LONG" if t.signal.direction == 1 else "SHORT",
                setup=SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id)),
                setup_id=t.signal.setup_id, symbol=sym, quality=t.signal.quality_score))
    return trades

def main():
    print("="*100)
    print("EMA PULL SHORT — ABLATION AUDIT")
    print("="*100)
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    sector_bars_dict = {}
    sector_etfs = sorted(set(SECTOR_MAP.values()) - {"SPY","QQQ","IWM"})
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists(): sector_bars_dict[etf] = load_bars_from_csv(str(p))
    excluded = {"SPY","QQQ","IWM"} | set(sector_etfs)
    symbols = sorted([p.stem.replace("_5min","") for p in DATA_DIR.glob("*_5min.csv")
                       if p.stem.replace("_5min","") not in excluded])
    print(f"  Universe: {len(symbols)} symbols")

    configs = []

    # Variant 1: baseline
    c = _base_cfg(); c.show_ema_pullback = True; c.ep_time_end = 1400
    c.ep_short_only = True; c.ep_require_regime = False
    configs.append(("EP baseline (short, ≤14:00)", c))

    # Variant 2: extended time
    c = _base_cfg(); c.show_ema_pullback = True; c.ep_time_end = 1500
    c.ep_short_only = True; c.ep_require_regime = False
    configs.append(("EP extended (short, ≤15:00)", c))

    # Variant 3: full day
    c = _base_cfg(); c.show_ema_pullback = True; c.ep_time_end = 1530
    c.ep_short_only = True; c.ep_require_regime = False
    configs.append(("EP full day (short, ≤15:30)", c))

    # Variant 4: both sides
    c = _base_cfg(); c.show_ema_pullback = True; c.ep_time_end = 1400
    c.ep_short_only = False; c.ep_require_regime = False
    configs.append(("EP both sides (≤14:00)", c))

    # Variant 5: regime gate
    c = _base_cfg(); c.show_ema_pullback = True; c.ep_time_end = 1400
    c.ep_short_only = True; c.ep_require_regime = True
    configs.append(("EP with regime gate", c))

    print(f"\n  {'Label':45s}  {'N':>5s}  {'PF':>6s}  {'Exp':>7s}  {'TotalR':>8s}  "
          f"{'WR%':>6s}  {'TrnPF':>6s}  {'TstPF':>6s}  {'ExDay':>6s}  {'ExSym':>6s}")
    print("  " + "-"*115)

    for label, cfg in configs:
        trades = _run(symbols, cfg, spy_bars, qqq_bars, sector_bars_dict, {SetupId.EMA_PULL})
        m = compute_metrics(trades)
        print(f"  {label:45s}  {m['n']:5d}  {pf_str(m['pf']):>6s}  {m['exp']:+7.3f}  "
              f"{m['total_r']:+8.1f}  {m['wr']:5.1f}%  {pf_str(m['train_pf']):>6s}  "
              f"{pf_str(m['test_pf']):>6s}  {pf_str(m['ex_best_day_pf']):>6s}  "
              f"{pf_str(m['ex_top_sym_pf']):>6s}")

    # Both-sides breakdown
    c = _base_cfg(); c.show_ema_pullback = True; c.ep_time_end = 1400
    c.ep_short_only = False; c.ep_require_regime = False
    both = _run(symbols, c, spy_bars, qqq_bars, sector_bars_dict, {SetupId.EMA_PULL})
    longs = [t for t in both if t.side == "LONG"]
    shorts = [t for t in both if t.side == "SHORT"]
    print(f"\n  Both sides breakdown:")
    print(f"    Longs:  N={len(longs)}, PF={pf_str(_pf(longs))}, TotalR={sum(t.pnl_rr for t in longs):+.1f}")
    print(f"    Shorts: N={len(shorts)}, PF={pf_str(_pf(shorts))}, TotalR={sum(t.pnl_rr for t in shorts):+.1f}")

    # Monthly for baseline
    baseline = _run(symbols, configs[0][1], spy_bars, qqq_bars, sector_bars_dict, {SetupId.EMA_PULL})
    monthly_r = defaultdict(float)
    monthly_n = defaultdict(int)
    for t in baseline:
        key = t.entry_date.strftime("%Y-%m")
        monthly_r[key] += t.pnl_rr; monthly_n[key] += 1
    print(f"\n  EP Baseline Monthly:")
    print(f"    {'Month':>8s}  {'N':>4s}  {'R':>8s}")
    cum = 0
    for mo in sorted(monthly_r):
        cum += monthly_r[mo]
        print(f"    {mo:>8s}  {monthly_n[mo]:4d}  {monthly_r[mo]:+8.1f}  cum={cum:+.1f}")

    # Top symbols
    sym_r = defaultdict(float); sym_n = defaultdict(int)
    for t in baseline: sym_r[t.symbol] += t.pnl_rr; sym_n[t.symbol] += 1
    print(f"\n  EP Baseline — Top symbols:")
    for sym in sorted(sym_r, key=sym_r.get, reverse=True)[:10]:
        print(f"    {sym:>8s}  N={sym_n[sym]:3d}  R={sym_r[sym]:+.1f}")

    print("\n" + "="*100)
    print("DONE — EMA PULL SHORT AUDIT")
    print("="*100)

if __name__ == "__main__":
    main()
