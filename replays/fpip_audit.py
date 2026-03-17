"""
EMA FPIP — Standalone Audit + Gate Relaxation (slim version).
Run 4 key variants to determine if FPIP has any edge.

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.replays.fpip_audit
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
    print("EMA FPIP — STANDALONE AUDIT + GATE RELAXATION")
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

    filt = {SetupId.EMA_FPIP}
    configs = []

    # 1. Baseline (all gates on)
    c = _base_cfg(); c.show_ema_scalp = True; c.show_ema_fpip = True
    configs.append(("FPIP baseline (all gates)", c))

    # 2. No context gates (market + VWAP + EMA20 all off)
    c = _base_cfg(); c.show_ema_scalp = True; c.show_ema_fpip = True
    c.ema_fpip_require_market_align = False
    c.ema_fpip_require_vwap_align = False
    c.ema_fpip_require_ema20_slope = False
    configs.append(("FPIP no context gates", c))

    # 3. Wide open (no context + RVOL 1.0 + Q0)
    c = _base_cfg(); c.show_ema_scalp = True; c.show_ema_fpip = True
    c.ema_fpip_require_market_align = False
    c.ema_fpip_require_vwap_align = False
    c.ema_fpip_require_ema20_slope = False
    c.ema_fpip_rvol_tod_min = 1.0
    c.ema_fpip_min_quality = 0
    configs.append(("FPIP wide open (all relaxed)", c))

    # 4. Allow subsequent PBs + no context
    c = _base_cfg(); c.show_ema_scalp = True; c.show_ema_fpip = True
    c.ema_fpip_require_market_align = False
    c.ema_fpip_require_vwap_align = False
    c.ema_fpip_require_ema20_slope = False
    c.ema_fpip_first_pullback_only = False
    configs.append(("FPIP no ctx + multi-PB", c))

    print(f"\n  {'Label':45s}  {'N':>5s}  {'PF':>6s}  {'Exp':>7s}  {'TotalR':>8s}  "
          f"{'WR%':>6s}  {'TrnPF':>6s}  {'TstPF':>6s}  {'ExDay':>6s}  {'ExSym':>6s}")
    print("  " + "-"*115)

    for label, cfg in configs:
        print(f"  Running {label}...", flush=True)
        trades = _run(symbols, cfg, spy_bars, qqq_bars, sector_bars_dict, filt)
        m = compute_metrics(trades)
        print(f"  {label:45s}  {m['n']:5d}  {pf_str(m['pf']):>6s}  {m['exp']:+7.3f}  "
              f"{m['total_r']:+8.1f}  {m['wr']:5.1f}%  {pf_str(m['train_pf']):>6s}  "
              f"{pf_str(m['test_pf']):>6s}  {pf_str(m['ex_best_day_pf']):>6s}  "
              f"{pf_str(m['ex_top_sym_pf']):>6s}")

        # Detail for each
        if trades:
            monthly_r = defaultdict(float); monthly_n = defaultdict(int)
            for t in trades:
                key = t.entry_date.strftime("%Y-%m")
                monthly_r[key] += t.pnl_rr; monthly_n[key] += 1
            longs = [t for t in trades if t.side == "LONG"]
            shorts = [t for t in trades if t.side == "SHORT"]
            print(f"    Side: Longs={len(longs)} PF={pf_str(_pf(longs))}, "
                  f"Shorts={len(shorts)} PF={pf_str(_pf(shorts))}")

    print("\n" + "="*100)
    print("DONE — EMA FPIP AUDIT")
    print("="*100)

if __name__ == "__main__":
    main()
