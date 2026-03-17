"""
CLEAN PROMOTED STACK — BDR Short + FLR only.
SC RETIRED, EP unresolved, FPIP dead.

Sections:
  1. BDR Short standalone
  2. FLR standalone
  3. BDR + FLR combined (unconstrained)
  4. BDR + FLR combined capped (max 3)

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.replays.clean_stack_replay
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
        return {"n":0,"wr":0,"pf":0,"exp":0,"total_r":0,"max_dd_r":0,
                "train_pf":0,"test_pf":0,"ex_best_day_pf":0,"ex_top_sym_pf":0,
                "stop_rate":0,"target_rate":0,"time_rate":0,"avg_bars_held":0,
                "days_active":0,"avg_per_day":0,"pct_pos_days":0}
    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in trades)
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")

    cum = 0.0; peak = 0.0; max_dd = 0.0
    for t in sorted(trades, key=lambda x: (x.entry_date, x.entry_time)):
        cum += t.pnl_rr
        if cum > peak: peak = cum
        dd = peak - cum
        if dd > max_dd: max_dd = dd

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
    days_active = len(set(t.entry_date for t in trades))
    avg_per_day = n / days_active if days_active else 0
    day_pnl = list(day_r.values())
    pos_days = sum(1 for d in day_pnl if d > 0)
    pct_pos = pos_days / len(day_pnl) * 100 if day_pnl else 0
    avg_bars = sum(t.bars_held for t in trades) / n

    return {"n":n, "wr":len(wins)/n*100, "pf":pf, "exp":total_r/n,
            "total_r":total_r, "max_dd_r":max_dd,
            "train_pf":_pf(train), "test_pf":_pf(test),
            "ex_best_day_pf":_pf(ex_best), "ex_top_sym_pf":_pf(ex_top),
            "stop_rate":stopped/n*100, "target_rate":target/n*100, "time_rate":timed/n*100,
            "avg_bars_held":avg_bars, "days_active":days_active,
            "avg_per_day":avg_per_day, "pct_pos_days":pct_pos}

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

def _capped(trades, max_open=3):
    trades_sorted = sorted(trades, key=lambda t: t.entry_time)
    accepted = []; open_pos = []
    for t in trades_sorted:
        open_pos = [(et,s) for et,s in open_pos if et > t.entry_time]
        open_syms = {s for _,s in open_pos}
        if t.symbol in open_syms: continue
        if len(open_pos) >= max_open: continue
        accepted.append(t)
        open_pos.append((t.exit_time, t.symbol))
    return accepted

def _export_csv(trades, path, label):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["section","date","entry_time","exit_time","symbol","side","setup",
                     "pnl_rr","exit_reason","bars_held","quality"])
        for t in sorted(trades, key=lambda x: x.entry_time):
            w.writerow([label, str(t.entry_date),
                        t.entry_time.strftime("%Y-%m-%d %H:%M"),
                        t.exit_time.strftime("%Y-%m-%d %H:%M"),
                        t.symbol, t.side, t.setup, f"{t.pnl_rr:+.4f}",
                        t.exit_reason, t.bars_held, t.quality])
    print(f"  → Exported {len(trades)} trades to {path.name}")

def _print_detail(label, trades):
    m = compute_metrics(trades)
    print(f"\n  ── {label} ──")
    print(f"  N={m['n']}  PF={pf_str(m['pf'])}  Exp={m['exp']:+.3f}R  TotalR={m['total_r']:+.1f}  "
          f"WR={m['wr']:.1f}%  MaxDD={m['max_dd_r']:.1f}R")
    print(f"  Train={pf_str(m['train_pf'])}  Test={pf_str(m['test_pf'])}  "
          f"ExDay={pf_str(m['ex_best_day_pf'])}  ExSym={pf_str(m['ex_top_sym_pf'])}")
    print(f"  Stop={m['stop_rate']:.1f}%  Tgt={m['target_rate']:.1f}%  Time={m['time_rate']:.1f}%  "
          f"AvgBars={m['avg_bars_held']:.1f}")
    print(f"  Days={m['days_active']}  Avg/day={m['avg_per_day']:.2f}  %Pos={m['pct_pos_days']:.1f}%")

    # Promotion criteria
    c1 = m["pf"] > 1.0; c2 = m["exp"] > 0; c3 = m["train_pf"] > 0.80
    c4 = m["test_pf"] > 0.80; n_ok = m["n"] >= 10
    all_pass = c1 and c2 and c3 and c4 and n_ok
    print(f"  Criteria: N≥10={'P' if n_ok else 'F'}  PF>1.0={'P' if c1 else 'F'}  "
          f"Exp>0={'P' if c2 else 'F'}  TrnPF>0.80={'P' if c3 else 'F'}  "
          f"TstPF>0.80={'P' if c4 else 'F'}  → {'SURVIVES' if all_pass else 'FAIL'}")

    # Monthly
    monthly_r = defaultdict(float); monthly_n = defaultdict(int)
    for t in trades:
        key = t.entry_date.strftime("%Y-%m")
        monthly_r[key] += t.pnl_rr; monthly_n[key] += 1
    cum = 0.0
    print(f"  Monthly:")
    for mo in sorted(monthly_r):
        cum += monthly_r[mo]
        print(f"    {mo}  N={monthly_n[mo]:3d}  R={monthly_r[mo]:+7.1f}  cum={cum:+.1f}")

    return m

def main():
    print("="*100)
    print("CLEAN PROMOTED STACK — BDR Short + FLR")
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
    spy_dates = sorted(set(b.timestamp.date() for b in spy_bars))
    date_range = f"{spy_dates[0]} → {spy_dates[-1]} ({len(spy_dates)} days)"
    print(f"  Universe: {len(symbols)} symbols, {date_range}")

    # ── BDR + FLR combined ──
    print("\n  Running BDR + FLR combined...")
    cfg = _base_cfg()
    cfg.show_breakdown_retest = True
    cfg.bdr_require_red_trend = True
    cfg.bdr_am_only = True
    cfg.bdr_require_regime = True
    cfg.show_fl_momentum_rebuild = True
    # FLR uses config defaults (4-bar turn, stop=0.50)

    clean_ids = {SetupId.BDR_SHORT, SetupId.FL_MOMENTUM_REBUILD}
    all_trades = _run(symbols, cfg, spy_bars, qqq_bars, sector_bars_dict, setup_filter=clean_ids)

    bdr = [t for t in all_trades if t.setup_id == SetupId.BDR_SHORT]
    flr = [t for t in all_trades if t.setup_id == SetupId.FL_MOMENTUM_REBUILD]

    print(f"\n{'='*100}")
    print("SECTION 1 — STANDALONE IN COMBINED ENGINE")
    print(f"{'='*100}")
    bdr_m = _print_detail("BDR SHORT (in combined engine)", bdr)
    flr_m = _print_detail("FLR (in combined engine)", flr)
    all_m = _print_detail("COMBINED (BDR + FLR)", all_trades)

    # ── Capped ──
    print(f"\n{'='*100}")
    print("SECTION 2 — CAPPED (max 3 concurrent)")
    print(f"{'='*100}")
    capped = _capped(all_trades, max_open=3)
    bdr_c = [t for t in capped if t.setup_id == SetupId.BDR_SHORT]
    flr_c = [t for t in capped if t.setup_id == SetupId.FL_MOMENTUM_REBUILD]
    _print_detail("Capped BDR SHORT", bdr_c)
    _print_detail("Capped FLR", flr_c)
    capped_m = _print_detail("Capped COMBINED", capped)

    # Peak concurrent
    cs = sorted(capped, key=lambda t: t.entry_time)
    mc = 0
    for i, t in enumerate(cs):
        c = sum(1 for o in cs[:i+1] if o.exit_time > t.entry_time)
        if c > mc: mc = c
    print(f"\n  Peak concurrent (capped): {mc}")

    # Export
    _export_csv(all_trades, OUT_DIR / "replay_clean_stack.csv", "clean_stack")
    _export_csv(capped, OUT_DIR / "replay_clean_stack_capped.csv", "clean_stack_capped")

    # ── Top symbols ──
    print(f"\n{'='*100}")
    print("TOP SYMBOLS")
    print(f"{'='*100}")
    for label, trades in [("BDR SHORT", bdr), ("FLR", flr)]:
        sym_r = defaultdict(float); sym_n = defaultdict(int)
        for t in trades: sym_r[t.symbol] += t.pnl_rr; sym_n[t.symbol] += 1
        print(f"\n  {label} Top 10:")
        for sym in sorted(sym_r, key=sym_r.get, reverse=True)[:10]:
            print(f"    {sym:>8s}  N={sym_n[sym]:3d}  R={sym_r[sym]:+.1f}")

    # ── Final summary ──
    print(f"\n{'='*100}")
    print("FINAL SUMMARY")
    print(f"{'='*100}")
    print(f"  BDR SHORT:  N={bdr_m['n']}, PF={pf_str(bdr_m['pf'])}, Exp={bdr_m['exp']:+.3f}R, "
          f"TotalR={bdr_m['total_r']:+.1f}")
    print(f"  FLR:        N={flr_m['n']}, PF={pf_str(flr_m['pf'])}, Exp={flr_m['exp']:+.3f}R, "
          f"TotalR={flr_m['total_r']:+.1f}")
    print(f"  COMBINED:   N={all_m['n']}, PF={pf_str(all_m['pf'])}, Exp={all_m['exp']:+.3f}R, "
          f"TotalR={all_m['total_r']:+.1f}")
    print(f"  CAPPED:     N={capped_m['n']}, PF={pf_str(capped_m['pf'])}, Exp={capped_m['exp']:+.3f}R, "
          f"TotalR={capped_m['total_r']:+.1f}")

    print(f"\n{'='*100}")
    print("DONE.")
    print(f"{'='*100}")

if __name__ == "__main__":
    main()
