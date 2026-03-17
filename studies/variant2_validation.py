"""
Variant 2 Validation — Full Portfolio D Validation Study

Portfolio D (Variant 2):
  Long book:  VK+SC, Q>=2, <15:30, tape permission >= 0.40
  Short book: BDR, RED+TREND, wick>=30%, AM-only (unchanged from Portfolio C)

Runs ALL the same checks as the original validated_combined_system but with
the Variant 2 long filter replacing Non-RED.

Usage:
    python -m alert_overlay.variant2_validation
"""

import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Tuple

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import Bar, NaN, SETUP_DISPLAY_NAME
from ..market_context import (
    MarketEngine, MarketContext, MarketSnapshot,
    compute_market_context, get_sector_etf, SECTOR_MAP,
)
from ..layered_regime import (
    LayeredRegime, LayeredRegimeConfig, EnvelopeConfig,
    PermissionWeights, compute_permission,
)
from .breakdown_retest_study import (
    BDRTrade, compute_bar_contexts, scan_for_breakdowns,
    build_market_context_map,
)
from .validated_combined_system import (
    simulate_exit_dynamic_slip, classify_spy_days, UTrade,
    wrap_engine_trade, wrap_bdr_trade, split_train_test,
)

DATA_DIR = Path(__file__).parent.parent / "data"
LONG_L2_THRESHOLD = 0.40


def metrics_r(trades) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "pf_r": 0, "exp_r": 0, "total_r": 0,
                "max_dd_r": 0, "stop_rate": 0, "wr": 0}
    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in trades)
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf_r = gw / gl if gl > 0 else float("inf")
    sorted_t = sorted(trades, key=lambda t: t.entry_time or datetime.min)
    cum = pk = dd = 0.0
    for t in sorted_t:
        cum += t.pnl_rr
        if cum > pk: pk = cum
        if pk - cum > dd: dd = pk - cum
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    return {
        "n": n, "pf_r": round(pf_r, 2), "exp_r": round(total_r / n, 3),
        "total_r": round(total_r, 2), "max_dd_r": round(dd, 2),
        "stop_rate": round(stopped / n * 100, 1),
        "wr": round(len(wins) / n * 100, 1),
    }


def _pf(v): return f"{v:.2f}" if v < 999 else "inf"


def main():
    W = 120
    print("=" * W)
    print("  PORTFOLIO D (VARIANT 2) — FULL VALIDATION")
    print("=" * W)

    # ── Load data ──
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    spy_engine = MarketEngine()
    qqq_engine = MarketEngine()
    spy_snaps: Dict[datetime, MarketSnapshot] = {}
    qqq_snaps: Dict[datetime, MarketSnapshot] = {}
    for b in spy_bars: spy_snaps[b.timestamp] = spy_engine.process_bar(b)
    for b in qqq_bars: qqq_snaps[b.timestamp] = qqq_engine.process_bar(b)

    sector_bars_dict: Dict[str, List[Bar]] = {}
    sector_snap_dict: Dict[str, Dict[datetime, MarketSnapshot]] = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            bars = load_bars_from_csv(str(p))
            sector_bars_dict[etf] = bars
            eng = MarketEngine()
            snaps = {}
            for b in bars: snaps[b.timestamp] = eng.process_bar(b)
            sector_snap_dict[etf] = snaps

    wl = Path(__file__).parent.parent / "watchlist.txt"
    symbols = []
    with open(wl) as f:
        for line in f:
            s = line.strip().upper()
            if s and not s.startswith("#") and s not in ("SPY", "QQQ"):
                symbols.append(s)

    spy_day_info = classify_spy_days(spy_bars)

    day_opens: Dict[Tuple[str, object], float] = {}
    all_sym_bars: Dict[str, List[Bar]] = {}
    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists(): continue
        bars = load_bars_from_csv(str(p))
        if not bars: continue
        all_sym_bars[sym] = bars
        cd = None
        for b in bars:
            d = b.timestamp.date()
            if d != cd:
                cd = d
                day_opens[(sym, d)] = b.open

    print(f"  Universe: {len(symbols)} symbols, {len(all_sym_bars)} with data")

    def find_snap(sd, ts):
        if ts in sd: return sd[ts]
        for d in range(1, 11):
            c = ts - timedelta(minutes=d)
            if c in sd: return sd[c]
        return None

    def build_ctx(sym, entry_ts, entry_price):
        spy_s = find_snap(spy_snaps, entry_ts)
        qqq_s = find_snap(qqq_snaps, entry_ts)
        if not spy_s or not qqq_s: return None
        sec_etf = get_sector_etf(sym)
        sec_s = None
        if sec_etf and sec_etf in sector_snap_dict:
            sec_s = find_snap(sector_snap_dict[sec_etf], entry_ts)
        stock_pct = NaN
        if not math.isnan(spy_s.pct_from_open):
            so = day_opens.get((sym, entry_ts.date()))
            if so and so > 0:
                stock_pct = (entry_price - so) / so * 100.0
        return compute_market_context(spy_s, qqq_s, sector_snapshot=sec_s,
                                      stock_pct_from_open=stock_pct)

    def hhmm(ts):
        return ts.hour * 100 + ts.minute

    # ═══════════════════════════════════════════════════════════
    #  Replay all trades
    # ═══════════════════════════════════════════════════════════
    print("  Replaying long book...")
    long_cfg = OverlayConfig()
    long_cfg.show_ema_scalp = False
    long_cfg.show_failed_bounce = False
    long_cfg.show_spencer = False
    long_cfg.show_ema_fpip = False
    long_cfg.show_sc_v2 = False

    all_longs_raw: List[Tuple[Trade, str]] = []
    for sym in symbols:
        if sym not in all_sym_bars: continue
        bars = all_sym_bars[sym]
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf)
        result = run_backtest(bars, cfg=long_cfg, spy_bars=spy_bars,
                              qqq_bars=qqq_bars, sector_bars=sec_bars)
        for t in result.trades:
            all_longs_raw.append((t, sym))

    # L3 filters: Q>=2, <15:30
    l3_longs = [(t, sym) for t, sym in all_longs_raw
                if t.signal.quality_score >= 2 and hhmm(t.signal.timestamp) < 1530]

    print("  Replaying BDR scanner...")
    cfg = OverlayConfig()
    all_bdr: List[BDRTrade] = []
    for sym in symbols:
        if sym not in all_sym_bars: continue
        bars = all_sym_bars[sym]
        if len(bars) < 30: continue
        ctxs = compute_bar_contexts(bars)
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf)
        mkt_map = build_market_context_map(spy_bars, qqq_bars, bars, sec_bars)
        candidates = scan_for_breakdowns(bars, ctxs, mkt_map, sym)
        for t in candidates:
            simulate_exit_dynamic_slip(t, bars, ctxs, cfg)
        all_bdr.extend(candidates)

    l3_shorts = [t for t in all_bdr
                 if t.rejection_wick_pct >= 0.30
                 and hhmm(t.entry_time) < 1100]

    # ═══════════════════════════════════════════════════════════
    #  Apply Variant 2 filters
    # ═══════════════════════════════════════════════════════════

    # LONG: tape permission >= 0.40
    weights = PermissionWeights()
    v2_longs = []
    v2_long_blocked = []
    for t, sym in l3_longs:
        ctx = build_ctx(sym, t.signal.timestamp, t.signal.entry_price)
        if ctx is None: continue
        h = hhmm(t.signal.timestamp)
        perm = compute_permission(ctx, direction=1, bar_time_hhmm=h, weights=weights)
        ut = wrap_engine_trade(t, sym)
        if perm.permission >= LONG_L2_THRESHOLD:
            v2_longs.append(ut)
        else:
            v2_long_blocked.append(ut)

    # SHORT: RED+TREND (frozen, unchanged)
    rt_dates = set(d for d, info in spy_day_info.items()
                   if info["direction"] == "RED" and info["character"] == "TREND")
    v2_shorts = [wrap_bdr_trade(t) for t in l3_shorts if t.entry_time.date() in rt_dates]
    v2_short_blocked = [wrap_bdr_trade(t) for t in l3_shorts if t.entry_time.date() not in rt_dates]

    v2_combined = v2_longs + v2_shorts

    print(f"  Variant 2: {len(v2_longs)} longs + {len(v2_shorts)} shorts = {len(v2_combined)}")
    print(f"  Blocked: {len(v2_long_blocked)} longs (tape < 0.40), {len(v2_short_blocked)} shorts (not RED+TREND)")

    # ═══════════════════════════════════════════════════════════
    #  CHECK 1: Portfolio Summary
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  CHECK 1: PORTFOLIO SUMMARY")
    print("=" * W)

    m_comb = metrics_r(v2_combined)
    m_long = metrics_r(v2_longs)
    m_short = metrics_r(v2_shorts)

    for label, m in [("Combined", m_comb), ("Long Book", m_long), ("Short Book", m_short)]:
        print(f"  {label:15s}  N={m['n']:>4}  WR={m['wr']:>5.1f}%  "
              f"PF(R)={_pf(m['pf_r']):>6}  Exp={m['exp_r']:>+7.3f}R  "
              f"TotR={m['total_r']:>+8.2f}  MaxDD={m['max_dd_r']:>6.2f}R  "
              f"Stop={m['stop_rate']:>5.1f}%")

    # ═══════════════════════════════════════════════════════════
    #  CHECK 2: Core Performance (Tier 1)
    # ═══════════════════════════════════════════════════════════
    checks = []
    print("\n" + "=" * W)
    print("  CHECK 2: TIER 1 — Core Performance")
    print("=" * W)

    t1_checks = [
        ("Combined PF(R) >= 1.3", m_comb['pf_r'] >= 1.3, m_comb['pf_r']),
        ("Combined Exp(R) > +0.10", m_comb['exp_r'] > 0.10, m_comb['exp_r']),
        ("Combined TotalR > 0", m_comb['total_r'] > 0, m_comb['total_r']),
        ("Combined MaxDD(R) < 15", m_comb['max_dd_r'] < 15, m_comb['max_dd_r']),
        ("Combined Stop Rate < 30%", m_comb['stop_rate'] < 30, m_comb['stop_rate']),
    ]
    for name, passed, val in t1_checks:
        status = "PASS" if passed else "FAIL"
        checks.append(passed)
        print(f"  [{status}] {name:40s} = {val}")

    # ═══════════════════════════════════════════════════════════
    #  CHECK 3: Book-Level Performance (Tier 2)
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  CHECK 3: TIER 2 — Book-Level Performance")
    print("=" * W)

    t2_checks = [
        ("Long Exp(R) > 0", m_long['exp_r'] > 0, m_long['exp_r']),
        ("Short Exp(R) > 0", m_short['exp_r'] > 0, m_short['exp_r']),
        ("Long PF(R) >= 1.0", m_long['pf_r'] >= 1.0, m_long['pf_r']),
        ("Short PF(R) >= 1.0", m_short['pf_r'] >= 1.0, m_short['pf_r']),
    ]
    for name, passed, val in t2_checks:
        status = "PASS" if passed else "FAIL"
        checks.append(passed)
        print(f"  [{status}] {name:40s} = {val}")

    # ═══════════════════════════════════════════════════════════
    #  CHECK 4: Robustness (Tier 3)
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  CHECK 4: TIER 3 — Robustness")
    print("=" * W)

    by_day = defaultdict(list)
    by_sym = defaultdict(list)
    for t in v2_combined:
        if t.entry_date: by_day[t.entry_date].append(t)
        by_sym[t.sym].append(t)

    day_r = {d: sum(t.pnl_rr for t in ts) for d, ts in by_day.items()}
    sym_r = {s: sum(t.pnl_rr for t in ts) for s, ts in by_sym.items()}
    best_day = max(day_r, key=day_r.get) if day_r else None
    best_sym = max(sym_r, key=sym_r.get) if sym_r else None

    m_xd = metrics_r([t for t in v2_combined if t.entry_date != best_day])
    m_xs = metrics_r([t for t in v2_combined if t.sym != best_sym])

    # Profitable days
    profitable_days = sum(1 for r in day_r.values() if r > 0)
    total_days = len(day_r)
    pct_profitable_days = (profitable_days / total_days * 100) if total_days > 0 else 0

    # Profitable symbols
    profitable_syms = sum(1 for r in sym_r.values() if r > 0)
    total_syms = len(sym_r)
    pct_profitable_syms = (profitable_syms / total_syms * 100) if total_syms > 0 else 0

    # Concentration
    max_sym_pct = (max(sym_r.values()) / m_comb['total_r'] * 100) if m_comb['total_r'] > 0 else 0
    max_day_pct = (max(day_r.values()) / m_comb['total_r'] * 100) if m_comb['total_r'] > 0 else 0

    t3_checks = [
        (f"Excl best day ({best_day}): PF(R) >= 1.0", m_xd['pf_r'] >= 1.0, m_xd['pf_r']),
        (f"Excl top sym ({best_sym}): PF(R) >= 1.0", m_xs['pf_r'] >= 1.0, m_xs['pf_r']),
        (f">=50% days profitable ({profitable_days}/{total_days})", pct_profitable_days >= 50, f"{pct_profitable_days:.0f}%"),
        (f">=50% syms with +R ({profitable_syms}/{total_syms})", pct_profitable_syms >= 50, f"{pct_profitable_syms:.0f}%"),
        (f"No sym > 30% of totalR (max={best_sym})", max_sym_pct < 30, f"{max_sym_pct:.1f}%"),
        (f"No day > 30% of totalR (max={best_day})", max_day_pct < 30, f"{max_day_pct:.1f}%"),
    ]
    for name, passed, val in t3_checks:
        status = "PASS" if passed else "FAIL"
        checks.append(passed)
        print(f"  [{status}] {name:55s} = {val}")

    print(f"\n  Robustness detail:")
    print(f"    Full:                 N={m_comb['n']:>4}  PF(R)={_pf(m_comb['pf_r'])}  "
          f"Exp={m_comb['exp_r']:>+.3f}R  TotR={m_comb['total_r']:>+.2f}")
    print(f"    Excl best day ({best_day}): N={m_xd['n']:>4}  PF(R)={_pf(m_xd['pf_r'])}  "
          f"Exp={m_xd['exp_r']:>+.3f}R  TotR={m_xd['total_r']:>+.2f}")
    print(f"    Excl top sym ({best_sym:>5}):  N={m_xs['n']:>4}  PF(R)={_pf(m_xs['pf_r'])}  "
          f"Exp={m_xs['exp_r']:>+.3f}R  TotR={m_xs['total_r']:>+.2f}")

    # ═══════════════════════════════════════════════════════════
    #  CHECK 5: Train/Test Stability (Tier 4)
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  CHECK 5: TIER 4 — Train/Test & Consistency")
    print("=" * W)

    train, test = split_train_test(v2_combined)
    m_train = metrics_r(train)
    m_test = metrics_r(test)

    t4_checks = [
        ("Train PF(R) >= 1.0", m_train['pf_r'] >= 1.0, m_train['pf_r']),
        ("Test PF(R) >= 1.0", m_test['pf_r'] >= 1.0, m_test['pf_r']),
        ("Train Exp(R) > 0", m_train['exp_r'] > 0, m_train['exp_r']),
        ("Test Exp(R) > 0", m_test['exp_r'] > 0, m_test['exp_r']),
        ("Combined MaxDD < 15R", m_comb['max_dd_r'] < 15, m_comb['max_dd_r']),
        (f"WR in range 37%-67% (={m_comb['wr']:.1f}%)", 37 <= m_comb['wr'] <= 67, m_comb['wr']),
    ]
    for name, passed, val in t4_checks:
        status = "PASS" if passed else "FAIL"
        checks.append(passed)
        print(f"  [{status}] {name:45s} = {val}")

    print(f"\n  Train/test detail:")
    print(f"    Train (odd):  N={m_train['n']:>4}  PF(R)={_pf(m_train['pf_r'])}  "
          f"Exp={m_train['exp_r']:>+.3f}R  TotR={m_train['total_r']:>+.2f}")
    print(f"    Test (even):  N={m_test['n']:>4}  PF(R)={_pf(m_test['pf_r'])}  "
          f"Exp={m_test['exp_r']:>+.3f}R  TotR={m_test['total_r']:>+.2f}")

    # ═══════════════════════════════════════════════════════════
    #  CHECK 6: Blocked Trade Analysis
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  CHECK 6: BLOCKED TRADE ANALYSIS")
    print("=" * W)

    m_lb = metrics_r(v2_long_blocked)
    m_sb = metrics_r(v2_short_blocked)

    print(f"  Long blocked (tape < 0.40):     N={m_lb['n']:>3}  Exp={m_lb['exp_r']:>+.3f}R  TotR={m_lb['total_r']:>+.2f}")
    print(f"  Short blocked (not RED+TREND):   N={m_sb['n']:>3}  Exp={m_sb['exp_r']:>+.3f}R  TotR={m_sb['total_r']:>+.2f}")

    long_block_ok = m_lb['exp_r'] <= 0.05  # blocked trades should be near-zero or negative
    short_block_ok = m_sb['exp_r'] <= 0.05
    checks.append(long_block_ok)
    checks.append(short_block_ok)
    print(f"  [{'PASS' if long_block_ok else 'WARN'}] Long blocked Exp <= +0.05R     = {m_lb['exp_r']:+.3f}R")
    print(f"  [{'PASS' if short_block_ok else 'WARN'}] Short blocked Exp <= +0.05R    = {m_sb['exp_r']:+.3f}R")

    # ═══════════════════════════════════════════════════════════
    #  CHECK 7: Per-Setup Breakdown
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  CHECK 7: PER-SETUP BREAKDOWN")
    print("=" * W)

    by_setup = defaultdict(list)
    for t in v2_combined:
        by_setup[t.source].append(t)

    print(f"  {'Setup':20s} {'N':>4} {'WR%':>6} {'PF(R)':>7} {'Exp(R)':>8} {'TotR':>8}")
    print("  " + "-" * 60)
    for setup in sorted(by_setup.keys()):
        m = metrics_r(by_setup[setup])
        print(f"  {setup:20s} {m['n']:>4} {m['wr']:>5.1f}% {_pf(m['pf_r']):>7} "
              f"{m['exp_r']:>+8.3f} {m['total_r']:>+8.2f}")

    # ═══════════════════════════════════════════════════════════
    #  SUMMARY
    # ═══════════════════════════════════════════════════════════
    passed_count = sum(checks)
    total_checks = len(checks)
    all_pass = all(checks)

    print("\n" + "=" * W)
    print(f"  VALIDATION RESULT: {passed_count}/{total_checks} CHECKS {'ALL PASS' if all_pass else 'SOME FAILED'}")
    print("=" * W)

    print(f"""
  Portfolio D (Variant 2) — Frozen Candidate
  ═══════════════════════════════════════════
  Long book:  VK+SC, Q>=2, <15:30, tape permission >= 0.40
  Short book: BDR, RED+TREND, wick>=30%, AM-only

  Combined:   N={m_comb['n']}  PF(R)={_pf(m_comb['pf_r'])}  Exp={m_comb['exp_r']:+.3f}R  TotR={m_comb['total_r']:+.2f}  MaxDD={m_comb['max_dd_r']:.2f}R
  Long:       N={m_long['n']}  PF(R)={_pf(m_long['pf_r'])}  Exp={m_long['exp_r']:+.3f}R
  Short:      N={m_short['n']}  PF(R)={_pf(m_short['pf_r'])}  Exp={m_short['exp_r']:+.3f}R

  Train:      N={m_train['n']}  PF(R)={_pf(m_train['pf_r'])}  Exp={m_train['exp_r']:+.3f}R
  Test:       N={m_test['n']}  PF(R)={_pf(m_test['pf_r'])}  Exp={m_test['exp_r']:+.3f}R
""")

    print("=" * W)
    print("  VALIDATION COMPLETE")
    print("=" * W)


if __name__ == "__main__":
    main()
