"""
Targeted Recalibration Study — Layered Regime Framework

Addresses two specific calibration problems found in layered_regime_study:
  1. Long side: L1 envelope too tight — rejecting profitable longs
     Fix: keep L2 fixed at 0.40, test coarser L1 long_floor values
  2. Short side: L2 permission too tight — rejecting profitable shorts
     Fix: keep L1 fixed, test lower L2 short_min_permission values

Constraints:
  - One layer changed at a time
  - Small number of settings (not broad sweeps)
  - All metrics in R
  - Frozen Portfolio C is the baseline

Output:
  1. Frozen Portfolio C
  2. Long-side recalibrated only
  3. Short-side recalibrated only
  4. Both recalibrated
  + rejection analysis per variant

Usage:
    python -m alert_overlay.recalibration_study
"""

import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import Bar, Signal, NaN, SETUP_DISPLAY_NAME
from ..market_context import (
    MarketEngine, MarketContext, MarketSnapshot,
    compute_market_context, get_sector_etf, SECTOR_MAP,
)
from ..layered_regime import (
    LayeredRegime, LayeredRegimeConfig, RegimeDecision, EnvelopeConfig,
    PermissionWeights, Envelope, classify_envelope, envelope_allows,
    compute_permission,
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


# ═══════════════════════════════════════════════════════════════════
#  Tagged Trade
# ═══════════════════════════════════════════════════════════════════

class LTrade:
    __slots__ = ("ut", "decision", "sym", "entry_hhmm")

    def __init__(self, ut: UTrade, decision: RegimeDecision, sym: str, entry_hhmm: int):
        self.ut = ut
        self.decision = decision
        self.sym = sym
        self.entry_hhmm = entry_hhmm


# ═══════════════════════════════════════════════════════════════════
#  Metrics
# ═══════════════════════════════════════════════════════════════════

def metrics_r(trades) -> dict:
    tlist = [t.ut if isinstance(t, LTrade) else t for t in trades]
    n = len(tlist)
    if n == 0:
        return {"n": 0, "pf_r": 0, "exp_r": 0, "total_r": 0,
                "max_dd_r": 0, "stop_rate": 0, "wr": 0}
    wins = [t for t in tlist if t.pnl_rr > 0]
    losses = [t for t in tlist if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in tlist)
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf_r = gw / gl if gl > 0 else float("inf")
    sorted_t = sorted(tlist, key=lambda t: t.entry_time or datetime.min)
    cum = pk = dd = 0.0
    for t in sorted_t:
        cum += t.pnl_rr
        if cum > pk: pk = cum
        if pk - cum > dd: dd = pk - cum
    stopped = sum(1 for t in tlist if t.exit_reason == "stop")
    return {
        "n": n, "pf_r": round(pf_r, 2), "exp_r": round(total_r / n, 3),
        "total_r": round(total_r, 2), "max_dd_r": round(dd, 2),
        "stop_rate": round(stopped / n * 100, 1),
        "wr": round(len(wins) / n * 100, 1),
    }


def _pf(v): return f"{v:.2f}" if v < 999 else "inf"


def row(label, m, indent="  "):
    print(f"{indent}{label:55s} N={m['n']:>4}  WR={m['wr']:>5.1f}%  "
          f"PF(R)={_pf(m['pf_r']):>6}  Exp={m['exp_r']:>+7.3f}R  "
          f"TotR={m['total_r']:>+8.2f}  MaxDD={m['max_dd_r']:>6.2f}R  "
          f"Stop={m['stop_rate']:>5.1f}%")


def robustness(trades, label, indent="    "):
    tlist = [t.ut if isinstance(t, LTrade) else t for t in trades]
    if len(tlist) < 5:
        print(f"{indent}{label}: N<5, skipping robustness")
        return

    by_day = defaultdict(list)
    by_sym = defaultdict(list)
    for t in tlist:
        if t.entry_date: by_day[t.entry_date].append(t)
        by_sym[t.sym].append(t)

    day_r = {d: sum(t.pnl_rr for t in ts) for d, ts in by_day.items()}
    sym_r = {s: sum(t.pnl_rr for t in ts) for s, ts in by_sym.items()}
    best_day = max(day_r, key=day_r.get)
    best_sym = max(sym_r, key=sym_r.get)

    m_full = metrics_r(tlist)
    m_xd = metrics_r([t for t in tlist if t.entry_date != best_day])
    m_xs = metrics_r([t for t in tlist if t.sym != best_sym])
    train, test = split_train_test(tlist)
    m_train = metrics_r(train)
    m_test = metrics_r(test)

    print(f"{indent}{label}:")
    print(f"{indent}  Full:                 N={m_full['n']:>4}  PF(R)={_pf(m_full['pf_r'])}  "
          f"Exp={m_full['exp_r']:>+.3f}R  TotR={m_full['total_r']:>+.2f}")
    print(f"{indent}  Excl best day ({best_day}): N={m_xd['n']:>4}  PF(R)={_pf(m_xd['pf_r'])}  "
          f"Exp={m_xd['exp_r']:>+.3f}R  TotR={m_xd['total_r']:>+.2f}")
    print(f"{indent}  Excl top sym ({best_sym:>5}):  N={m_xs['n']:>4}  PF(R)={_pf(m_xs['pf_r'])}  "
          f"Exp={m_xs['exp_r']:>+.3f}R  TotR={m_xs['total_r']:>+.2f}")
    print(f"{indent}  Train (odd):          N={m_train['n']:>4}  PF(R)={_pf(m_train['pf_r'])}  "
          f"Exp={m_train['exp_r']:>+.3f}R  TotR={m_train['total_r']:>+.2f}")
    print(f"{indent}  Test  (even):         N={m_test['n']:>4}  PF(R)={_pf(m_test['pf_r'])}  "
          f"Exp={m_test['exp_r']:>+.3f}R  TotR={m_test['total_r']:>+.2f}")


def rejection_analysis(all_trades, label, indent="    "):
    """Show what each layer blocked and the expectancy of blocked trades."""
    l1_blocked = [t for t in all_trades if not t.decision.envelope_allows]
    l2_blocked = [t for t in all_trades if t.decision.envelope_allows and not t.decision.meets_threshold]
    allowed = [t for t in all_trades if t.decision.allowed]

    m_l1 = metrics_r(l1_blocked)
    m_l2 = metrics_r(l2_blocked)
    m_ok = metrics_r(allowed)

    print(f"{indent}{label}:")
    print(f"{indent}  L1 blocked: N={m_l1['n']:>3}  Exp={m_l1['exp_r']:>+.3f}R  TotR={m_l1['total_r']:>+.2f}")
    print(f"{indent}  L2 blocked: N={m_l2['n']:>3}  Exp={m_l2['exp_r']:>+.3f}R  TotR={m_l2['total_r']:>+.2f}")
    print(f"{indent}  Allowed:    N={m_ok['n']:>3}  Exp={m_ok['exp_r']:>+.3f}R  TotR={m_ok['total_r']:>+.2f}")

    # Flag if either layer is blocking positive-expectancy trades
    issues = []
    if m_l1['n'] > 0 and m_l1['exp_r'] > 0.05:
        issues.append(f"L1 blocking +{m_l1['exp_r']:.3f}R trades")
    if m_l2['n'] > 0 and m_l2['exp_r'] > 0.05:
        issues.append(f"L2 blocking +{m_l2['exp_r']:.3f}R trades")
    if issues:
        print(f"{indent}  ⚠ {', '.join(issues)}")
    else:
        print(f"{indent}  ✓ Both layers filtering correctly")


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════

def main():
    W = 120
    print("=" * W)
    print("  TARGETED RECALIBRATION STUDY — One Layer at a Time")
    print("=" * W)

    # ── Load data (same as layered_regime_study) ──
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

    # ── Snapshot lookup ──
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
    #  Replay all L3-qualified trades
    # ═══════════════════════════════════════════════════════════
    print("  Replaying long book (engine, L3 filters only)...")
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

    l3_longs = [(t, sym) for t, sym in all_longs_raw
                if t.signal.quality_score >= 2 and hhmm(t.signal.timestamp) < 1530]

    print("  Replaying BDR scanner (L3 filters only)...")
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

    print(f"  L3 longs: {len(l3_longs)}")
    print(f"  L3 shorts: {len(l3_shorts)}")

    # ═══════════════════════════════════════════════════════════
    #  Frozen baseline
    # ═══════════════════════════════════════════════════════════
    def is_red(t):
        return spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == "RED"

    frozen_longs = [wrap_engine_trade(t, sym) for t, sym in l3_longs if not is_red(t)]
    rt_dates = set(d for d, info in spy_day_info.items()
                   if info["direction"] == "RED" and info["character"] == "TREND")
    frozen_shorts = [wrap_bdr_trade(t) for t in l3_shorts if t.entry_time.date() in rt_dates]
    frozen_combined = frozen_longs + frozen_shorts

    print(f"  Frozen: {len(frozen_longs)} longs + {len(frozen_shorts)} shorts = {len(frozen_combined)}")

    # ═══════════════════════════════════════════════════════════
    #  Evaluation helpers
    # ═══════════════════════════════════════════════════════════
    def evaluate_longs(regime: LayeredRegime) -> List[LTrade]:
        results = []
        for t, sym in l3_longs:
            ctx = build_ctx(sym, t.signal.timestamp, t.signal.entry_price)
            if ctx is None: continue
            h = hhmm(t.signal.timestamp)
            decision = regime.evaluate(direction=1, market_ctx=ctx, bar_time_hhmm=h)
            ut = wrap_engine_trade(t, sym)
            results.append(LTrade(ut, decision, sym, h))
        return results

    def evaluate_shorts(regime: LayeredRegime) -> List[LTrade]:
        results = []
        for t in l3_shorts:
            ctx = build_ctx(t.sym, t.entry_time, t.entry_price)
            if ctx is None: continue
            h = hhmm(t.entry_time)
            decision = regime.evaluate(direction=-1, market_ctx=ctx, bar_time_hhmm=h)
            ut = wrap_bdr_trade(t)
            results.append(LTrade(ut, decision, t.sym, h))
        return results

    # ═══════════════════════════════════════════════════════════
    #  SECTION 1: LONG SIDE RECALIBRATION
    #  Fix L2 at 0.40, test L1 long_floor: -0.05, -0.10, -0.15, -0.20, -0.30
    #  short_ceiling stays at +0.05 (conservative, irrelevant for longs)
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  SECTION 1: LONG SIDE — Relax L1 envelope only (L2 fixed at 0.40)")
    print("=" * W)

    LONG_L2 = 0.40  # best L2 for longs from prior study

    row("Frozen long book (Non-RED, Q>=2, <15:30)", metrics_r(frozen_longs))
    print()

    long_variants = {}
    for l1_floor in [-0.05, -0.10, -0.15, -0.20, -0.30]:
        cfg_v = LayeredRegimeConfig(
            envelope=EnvelopeConfig(long_floor=l1_floor, short_ceiling=0.05),
            weights=PermissionWeights(),
            long_min_permission=LONG_L2,
            short_min_permission=0.40,  # irrelevant here
        )
        regime = LayeredRegime(cfg_v)
        all_lt = evaluate_longs(regime)
        allowed = [t for t in all_lt if t.decision.allowed]
        label = f"L1 floor={l1_floor:+.2f}%, L2=0.40"
        row(label, metrics_r(allowed))
        rejection_analysis(all_lt, label)
        long_variants[l1_floor] = (all_lt, allowed)

    # Robustness for frozen + best long variant
    print()
    robustness(frozen_longs, "Frozen long")

    # Find best long variant by total_r (among those with PF >= 1.3)
    best_long_floor = None
    best_long_total = -999
    for l1_floor, (all_lt, allowed) in long_variants.items():
        m = metrics_r(allowed)
        if m['pf_r'] >= 1.3 and m['total_r'] > best_long_total:
            best_long_total = m['total_r']
            best_long_floor = l1_floor
            best_long_allowed = allowed

    if best_long_floor is not None:
        robustness(best_long_allowed, f"Best long (L1={best_long_floor:+.2f})")
    print(f"\n  → Best long L1 floor: {best_long_floor}")

    # ═══════════════════════════════════════════════════════════
    #  SECTION 2: SHORT SIDE RECALIBRATION
    #  Fix L1 at short_ceiling=+0.05, test L2 short_min: 0.10, 0.15, 0.20, 0.25, 0.30
    #  long_floor stays at -0.05 (conservative, irrelevant for shorts)
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  SECTION 2: SHORT SIDE — Relax L2 permission only (L1 fixed at +0.05)")
    print("=" * W)

    SHORT_L1_CEIL = 0.05  # L1 works well for shorts at this level

    row("Frozen short book (RED+TREND, wick>=30%, AM)", metrics_r(frozen_shorts))
    print()

    short_variants = {}
    for s2_min in [0.10, 0.15, 0.20, 0.25, 0.30, 0.40]:
        cfg_v = LayeredRegimeConfig(
            envelope=EnvelopeConfig(long_floor=-0.05, short_ceiling=SHORT_L1_CEIL),
            weights=PermissionWeights(),
            long_min_permission=0.40,  # irrelevant here
            short_min_permission=s2_min,
        )
        regime = LayeredRegime(cfg_v)
        all_st = evaluate_shorts(regime)
        allowed = [t for t in all_st if t.decision.allowed]
        label = f"L1 ceil=+0.05%, L2={s2_min:.2f}"
        row(label, metrics_r(allowed))
        rejection_analysis(all_st, label)
        short_variants[s2_min] = (all_st, allowed)

    print()
    robustness(frozen_shorts, "Frozen short")

    # Find best short variant by total_r (among those with PF >= 2.0)
    best_short_min = None
    best_short_total = -999
    for s2_min, (all_st, allowed) in short_variants.items():
        m = metrics_r(allowed)
        if m['pf_r'] >= 2.0 and m['total_r'] > best_short_total:
            best_short_total = m['total_r']
            best_short_min = s2_min
            best_short_allowed = allowed

    if best_short_min is not None:
        robustness(best_short_allowed, f"Best short (L2={best_short_min:.2f})")
    print(f"\n  → Best short L2 min: {best_short_min}")

    # ═══════════════════════════════════════════════════════════
    #  SECTION 3: COMBINED COMPARISON
    #  1. Frozen Portfolio C
    #  2. Long-side recalibrated only (best L1 floor + L2=0.40 for longs, frozen shorts)
    #  3. Short-side recalibrated only (frozen longs, best L2 for shorts)
    #  4. Both recalibrated
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  SECTION 3: COMBINED COMPARISON")
    print("=" * W)

    # Variant 2: long recalibrated + frozen shorts
    if best_long_floor is not None:
        long_recal_cfg = LayeredRegimeConfig(
            envelope=EnvelopeConfig(long_floor=best_long_floor, short_ceiling=0.05),
            weights=PermissionWeights(),
            long_min_permission=LONG_L2,
            short_min_permission=0.40,
        )
        long_recal_regime = LayeredRegime(long_recal_cfg)
        long_recal_allowed = [t.ut for t in evaluate_longs(long_recal_regime)
                              if t.decision.allowed]
    else:
        long_recal_allowed = frozen_longs

    # Variant 3: frozen longs + short recalibrated
    if best_short_min is not None:
        short_recal_cfg = LayeredRegimeConfig(
            envelope=EnvelopeConfig(long_floor=-0.05, short_ceiling=SHORT_L1_CEIL),
            weights=PermissionWeights(),
            long_min_permission=0.40,
            short_min_permission=best_short_min,
        )
        short_recal_regime = LayeredRegime(short_recal_cfg)
        short_recal_allowed = [t.ut for t in evaluate_shorts(short_recal_regime)
                               if t.decision.allowed]
    else:
        short_recal_allowed = frozen_shorts

    # Variant 4: both recalibrated
    both_combined = long_recal_allowed + short_recal_allowed

    # Print all 4 variants
    print()
    row("1. Frozen Portfolio C", metrics_r(frozen_combined))
    print(f"       {len(frozen_longs)}L + {len(frozen_shorts)}S")

    row("2. Long recalibrated + frozen shorts",
        metrics_r(long_recal_allowed + frozen_shorts))
    print(f"       {len(long_recal_allowed)}L + {len(frozen_shorts)}S"
          f"  [L1={best_long_floor:+.2f}%, L2=0.40]")

    row("3. Frozen longs + short recalibrated",
        metrics_r(frozen_longs + short_recal_allowed))
    print(f"       {len(frozen_longs)}L + {len(short_recal_allowed)}S"
          f"  [L1=+0.05%, L2={best_short_min:.2f}]")

    row("4. Both recalibrated",
        metrics_r(both_combined))
    print(f"       {len(long_recal_allowed)}L + {len(short_recal_allowed)}S"
          f"  [Long: L1={best_long_floor:+.2f}%/L2=0.40, "
          f"Short: L1=+0.05%/L2={best_short_min:.2f}]")

    # ═══════════════════════════════════════════════════════════
    #  SECTION 4: FULL ROBUSTNESS ON ALL 4 VARIANTS
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  SECTION 4: ROBUSTNESS — All 4 Variants")
    print("=" * W)

    robustness(frozen_combined, "1. Frozen Portfolio C")
    robustness(long_recal_allowed + frozen_shorts, "2. Long recal + frozen shorts")
    robustness(frozen_longs + short_recal_allowed, "3. Frozen longs + short recal")
    robustness(both_combined, "4. Both recalibrated")

    # ═══════════════════════════════════════════════════════════
    #  SECTION 5: REJECTION ANALYSIS ON BEST COMBINED
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  SECTION 5: REJECTION ANALYSIS — Best Combined Config")
    print("=" * W)

    both_cfg = LayeredRegimeConfig(
        envelope=EnvelopeConfig(
            long_floor=best_long_floor if best_long_floor else -0.05,
            short_ceiling=SHORT_L1_CEIL,
        ),
        weights=PermissionWeights(),
        long_min_permission=LONG_L2,
        short_min_permission=best_short_min if best_short_min else 0.40,
    )
    both_regime = LayeredRegime(both_cfg)
    all_lt = evaluate_longs(both_regime)
    all_st = evaluate_shorts(both_regime)

    print(f"\n  Config: L1 long_floor={best_long_floor:+.2f}%, "
          f"L1 short_ceil=+0.05%, "
          f"L2 long=0.40, L2 short={best_short_min:.2f}")

    print(f"\n  LONGS (N_total={len(all_lt)}):")
    rejection_analysis(all_lt, "Long trades")

    print(f"\n  SHORTS (N_total={len(all_st)}):")
    rejection_analysis(all_st, "Short trades")

    # ═══════════════════════════════════════════════════════════
    #  SUMMARY
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  SUMMARY")
    print("=" * W)

    m1 = metrics_r(frozen_combined)
    m2 = metrics_r(long_recal_allowed + frozen_shorts)
    m3 = metrics_r(frozen_longs + short_recal_allowed)
    m4 = metrics_r(both_combined)

    print(f"""
  1. Frozen Portfolio C:
     N={m1['n']:>4}  PF(R)={_pf(m1['pf_r'])}  Exp={m1['exp_r']:>+.3f}R  TotR={m1['total_r']:>+.2f}  MaxDD={m1['max_dd_r']:.2f}R

  2. Long recalibrated + frozen shorts [L1={best_long_floor:+.2f}%, L2=0.40]:
     N={m2['n']:>4}  PF(R)={_pf(m2['pf_r'])}  Exp={m2['exp_r']:>+.3f}R  TotR={m2['total_r']:>+.2f}  MaxDD={m2['max_dd_r']:.2f}R

  3. Frozen longs + short recalibrated [L1=+0.05%, L2={best_short_min:.2f}]:
     N={m3['n']:>4}  PF(R)={_pf(m3['pf_r'])}  Exp={m3['exp_r']:>+.3f}R  TotR={m3['total_r']:>+.2f}  MaxDD={m3['max_dd_r']:.2f}R

  4. Both recalibrated:
     N={m4['n']:>4}  PF(R)={_pf(m4['pf_r'])}  Exp={m4['exp_r']:>+.3f}R  TotR={m4['total_r']:>+.2f}  MaxDD={m4['max_dd_r']:.2f}R

  Verdict: {"LAYERED BEATS FROZEN" if m4['total_r'] > m1['total_r'] and m4['pf_r'] >= m1['pf_r'] * 0.9 else "FROZEN STILL BEST" if m1['total_r'] >= m4['total_r'] else "MIXED — review tradeoffs"}
""")

    print("=" * W)
    print("  STUDY COMPLETE")
    print("=" * W)


if __name__ == "__main__":
    main()
