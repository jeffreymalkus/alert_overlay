"""
Layered Regime Study — Test the hierarchical permission framework against
the frozen Portfolio C system using historical trades.

Compares:
  1. Frozen Portfolio C (binary regime)
  2. Layered: conservative preset
  3. Layered: moderate preset
  4. Layered: relaxed preset
  5. Layered: custom sweep of L2 thresholds within fixed L1 envelope

All metrics in R.  Reports N, PF(R), Exp(R), TotalR, MaxDD(R), stop rate,
train/test split, robustness (excl best day, excl top sym).

Usage:
    python -m alert_overlay.layered_regime_study
"""

import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import Bar, Signal, NaN, SETUP_DISPLAY_NAME
from .market_context import (
    MarketEngine, MarketContext, MarketSnapshot,
    compute_market_context, get_sector_etf, SECTOR_MAP,
)
from .layered_regime import (
    LayeredRegime, LayeredRegimeConfig, RegimeDecision, EnvelopeConfig,
    PermissionWeights, Envelope, classify_envelope, envelope_allows,
    compute_permission, preset_conservative, preset_moderate, preset_relaxed,
)
from .tape_model import classify_tape_zone
from .breakdown_retest_study import (
    BDRTrade, compute_bar_contexts, scan_for_breakdowns,
    build_market_context_map,
)
from .validated_combined_system import (
    simulate_exit_dynamic_slip, classify_spy_days, UTrade,
    wrap_engine_trade, wrap_bdr_trade, split_train_test,
)

DATA_DIR = Path(__file__).parent / "data"


# ═══════════════════════════════════════════════════════════════════
#  Tagged Trade (UTrade + regime decision)
# ═══════════════════════════════════════════════════════════════════

class LTrade:
    """A UTrade annotated with layered regime decision."""
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
        print(f"{indent}Robustness: N<5, skipping")
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


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════

def main():
    print("=" * 120)
    print("  LAYERED REGIME STUDY — Hierarchical Permission Framework")
    print("=" * 120)

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

    wl = Path(__file__).parent / "watchlist.txt"
    symbols = []
    with open(wl) as f:
        for line in f:
            s = line.strip().upper()
            if s and not s.startswith("#") and s not in ("SPY", "QQQ"):
                symbols.append(s)

    spy_day_info = classify_spy_days(spy_bars)

    # Pre-compute day opens
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

    # ═══════════════════════════════════════════════════════════
    #  Replay all trades (unfiltered by regime)
    # ═══════════════════════════════════════════════════════════
    print("  Replaying long book (engine, no regime filter)...")
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

    # Apply Layer 3 filters only (Q>=2, <15:30)
    def hhmm(ts):
        return ts.hour * 100 + ts.minute

    l3_longs = [(t, sym) for t, sym in all_longs_raw
                if t.signal.quality_score >= 2 and hhmm(t.signal.timestamp) < 1530]

    print("  Replaying BDR scanner (no regime filter)...")
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

    # Apply Layer 3 filters only (wick>=30%, AM <11:00)
    l3_shorts = [t for t in all_bdr
                 if t.rejection_wick_pct >= 0.30
                 and hhmm(t.entry_time) < 1100]

    print(f"  L3 longs (Q>=2, <15:30): {len(l3_longs)}")
    print(f"  L3 shorts (wick>=30%, AM): {len(l3_shorts)}")

    # ═══════════════════════════════════════════════════════════
    #  Build frozen baseline for comparison
    # ═══════════════════════════════════════════════════════════
    def is_red(t):
        return spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == "RED"

    frozen_longs = [(t, sym) for t, sym in l3_longs if not is_red(t)]
    frozen_long_uts = [wrap_engine_trade(t, sym) for t, sym in frozen_longs]

    rt_dates = set(d for d, info in spy_day_info.items()
                   if info["direction"] == "RED" and info["character"] == "TREND")
    frozen_shorts = [t for t in l3_shorts if t.entry_time.date() in rt_dates]
    frozen_short_uts = [wrap_bdr_trade(t) for t in frozen_shorts]

    frozen_combined = frozen_long_uts + frozen_short_uts
    print(f"  Frozen: {len(frozen_long_uts)} longs + {len(frozen_short_uts)} shorts = {len(frozen_combined)}")

    # ═══════════════════════════════════════════════════════════
    #  Evaluate each trade through the layered regime
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
    #  Run presets
    # ═══════════════════════════════════════════════════════════
    presets = [
        ("Conservative (L1:-0.05/+0.05, L2:0.40)", preset_conservative()),
        ("Moderate     (L1:-0.10/+0.10, L2:0.25)", preset_moderate()),
        ("Relaxed      (L1:-0.15/+0.15, L2:0.15)", preset_relaxed()),
    ]

    # Header
    print("\n" + "=" * 120)
    print("  SECTION 1: LONG SIDE — Frozen vs Layered Presets")
    print("=" * 120)

    row("Frozen long book (Non-RED, Q>=2, <15:30)", metrics_r(frozen_long_uts))

    for name, cfg_preset in presets:
        regime = LayeredRegime(cfg_preset)
        all_lt = evaluate_longs(regime)
        allowed = [t for t in all_lt if t.decision.allowed]
        blocked_l1 = [t for t in all_lt if not t.decision.envelope_allows]
        blocked_l2 = [t for t in all_lt if t.decision.envelope_allows and not t.decision.meets_threshold]
        row(f"{name}", metrics_r(allowed))
        print(f"    → {len(blocked_l1)} blocked L1, {len(blocked_l2)} blocked L2, "
              f"{len(allowed)} allowed")

    # Detailed robustness for frozen + moderate
    robustness(frozen_long_uts, "Frozen long")
    regime_mod = LayeredRegime(preset_moderate())
    mod_longs = [t for t in evaluate_longs(regime_mod) if t.decision.allowed]
    robustness(mod_longs, "Moderate long")

    # ── Short side ──
    print("\n" + "=" * 120)
    print("  SECTION 2: SHORT SIDE — Frozen vs Layered Presets")
    print("=" * 120)

    row("Frozen short book (RED+TREND, wick>=30%, AM)", metrics_r(frozen_short_uts))

    for name, cfg_preset in presets:
        regime = LayeredRegime(cfg_preset)
        all_st = evaluate_shorts(regime)
        allowed = [t for t in all_st if t.decision.allowed]
        blocked_l1 = [t for t in all_st if not t.decision.envelope_allows]
        blocked_l2 = [t for t in all_st if t.decision.envelope_allows and not t.decision.meets_threshold]
        row(f"{name}", metrics_r(allowed))
        print(f"    → {len(blocked_l1)} blocked L1, {len(blocked_l2)} blocked L2, "
              f"{len(allowed)} allowed")

    robustness(frozen_short_uts, "Frozen short")
    mod_shorts = [t for t in evaluate_shorts(regime_mod) if t.decision.allowed]
    robustness(mod_shorts, "Moderate short")

    # ── Combined ──
    print("\n" + "=" * 120)
    print("  SECTION 3: COMBINED — Frozen vs Layered Presets")
    print("=" * 120)

    row("Frozen Portfolio C", metrics_r(frozen_combined))

    for name, cfg_preset in presets:
        regime = LayeredRegime(cfg_preset)
        al = [t for t in evaluate_longs(regime) if t.decision.allowed]
        ash = [t for t in evaluate_shorts(regime) if t.decision.allowed]
        comb_uts = [t.ut for t in al] + [t.ut for t in ash]
        n_long = len(al)
        n_short = len(ash)
        row(f"{name}", metrics_r(comb_uts))
        print(f"    → {n_long}L + {n_short}S")

    robustness(frozen_combined, "Frozen combined")
    mod_comb = [t.ut for t in mod_longs] + [t.ut for t in mod_shorts]
    robustness(mod_comb, "Moderate combined")

    # ═══════════════════════════════════════════════════════════
    #  SECTION 4: L2 Threshold Sweep (fixed moderate L1 envelope)
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("  SECTION 4: L2 THRESHOLD SWEEP — Fixed L1 (-0.10/+0.10)")
    print("=" * 120)

    base_env = EnvelopeConfig(long_floor=-0.10, short_ceiling=0.10)

    print("\n  Long side:")
    print(f"  {'L2 Thresh':>10} {'N':>5} {'WR%':>6} {'PF(R)':>7} {'Exp(R)':>8} "
          f"{'TotR':>8} {'MaxDD':>7} {'Stop%':>6}")
    print("  " + "-" * 65)

    for thresh in [0.0, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]:
        lrc = LayeredRegimeConfig(envelope=base_env, weights=PermissionWeights(),
                                  long_min_permission=thresh, short_min_permission=thresh)
        regime = LayeredRegime(lrc)
        al = [t for t in evaluate_longs(regime) if t.decision.allowed]
        m = metrics_r(al)
        print(f"  {thresh:>+10.2f} {m['n']:>5} {m['wr']:>5.1f}% {_pf(m['pf_r']):>7} "
              f"{m['exp_r']:>+8.3f} {m['total_r']:>+8.2f} {m['max_dd_r']:>7.2f} "
              f"{m['stop_rate']:>5.1f}%")

    print("\n  Short side:")
    print(f"  {'L2 Thresh':>10} {'N':>5} {'WR%':>6} {'PF(R)':>7} {'Exp(R)':>8} "
          f"{'TotR':>8} {'MaxDD':>7} {'Stop%':>6}")
    print("  " + "-" * 65)

    for thresh in [0.0, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]:
        lrc = LayeredRegimeConfig(envelope=base_env, weights=PermissionWeights(),
                                  long_min_permission=thresh, short_min_permission=thresh)
        regime = LayeredRegime(lrc)
        ash = [t for t in evaluate_shorts(regime) if t.decision.allowed]
        m = metrics_r(ash)
        print(f"  {thresh:>+10.2f} {m['n']:>5} {m['wr']:>5.1f}% {_pf(m['pf_r']):>7} "
              f"{m['exp_r']:>+8.3f} {m['total_r']:>+8.2f} {m['max_dd_r']:>7.2f} "
              f"{m['stop_rate']:>5.1f}%")

    print("\n  Combined (long + short at same threshold):")
    print(f"  {'L2 Thresh':>10} {'NL':>4}+{'NS':>3} {'N':>5} {'PF(R)':>7} {'Exp(R)':>8} "
          f"{'TotR':>8} {'MaxDD':>7}")
    print("  " + "-" * 65)

    for thresh in [0.0, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]:
        lrc = LayeredRegimeConfig(envelope=base_env, weights=PermissionWeights(),
                                  long_min_permission=thresh, short_min_permission=thresh)
        regime = LayeredRegime(lrc)
        al = [t for t in evaluate_longs(regime) if t.decision.allowed]
        ash = [t for t in evaluate_shorts(regime) if t.decision.allowed]
        comb = [t.ut for t in al] + [t.ut for t in ash]
        m = metrics_r(comb)
        print(f"  {thresh:>+10.2f} {len(al):>4}+{len(ash):>3} {m['n']:>5} "
              f"{_pf(m['pf_r']):>7} {m['exp_r']:>+8.3f} {m['total_r']:>+8.2f} "
              f"{m['max_dd_r']:>7.2f}")

    # ═══════════════════════════════════════════════════════════
    #  SECTION 5: Layer rejection analysis
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("  SECTION 5: REJECTION ANALYSIS — What each layer filters out")
    print("=" * 120)

    regime_mod = LayeredRegime(preset_moderate())
    all_lt = evaluate_longs(regime_mod)
    all_st = evaluate_shorts(regime_mod)

    # Longs blocked by L1
    l1_blocked_longs = [t for t in all_lt if not t.decision.envelope_allows]
    l1_passed_l2_blocked = [t for t in all_lt if t.decision.envelope_allows and not t.decision.meets_threshold]
    l1_l2_passed = [t for t in all_lt if t.decision.allowed]

    print(f"\n  LONG TRADES (moderate preset, N_total={len(all_lt)}):")
    row("L1 blocked (envelope rejects)", metrics_r(l1_blocked_longs))
    row("L1 pass → L2 blocked (low permission)", metrics_r(l1_passed_l2_blocked))
    row("L1+L2 pass (allowed)", metrics_r(l1_l2_passed))

    print(f"\n    L1-blocked longs would have been: {metrics_r(l1_blocked_longs)['exp_r']:+.3f}R exp")
    print(f"    L2-blocked longs would have been: {metrics_r(l1_passed_l2_blocked)['exp_r']:+.3f}R exp")
    print(f"    → Both layers are correctly filtering negative-expectancy trades" if
          metrics_r(l1_blocked_longs)['exp_r'] <= 0 and metrics_r(l1_passed_l2_blocked)['exp_r'] <= 0
          else f"    → Check: one layer may be filtering positive trades")

    # Shorts
    s1_blocked = [t for t in all_st if not t.decision.envelope_allows]
    s1_pass_s2_blocked = [t for t in all_st if t.decision.envelope_allows and not t.decision.meets_threshold]
    s1_s2_passed = [t for t in all_st if t.decision.allowed]

    print(f"\n  SHORT TRADES (moderate preset, N_total={len(all_st)}):")
    row("L1 blocked (envelope rejects)", metrics_r(s1_blocked))
    row("L1 pass → L2 blocked (low permission)", metrics_r(s1_pass_s2_blocked))
    row("L1+L2 pass (allowed)", metrics_r(s1_s2_passed))

    print(f"\n    L1-blocked shorts would have been: {metrics_r(s1_blocked)['exp_r']:+.3f}R exp")
    print(f"    L2-blocked shorts would have been: {metrics_r(s1_pass_s2_blocked)['exp_r']:+.3f}R exp")

    # ═══════════════════════════════════════════════════════════
    #  SUMMARY
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("  SUMMARY")
    print("=" * 120)

    mf = metrics_r(frozen_combined)
    mm = metrics_r(mod_comb)

    # Find best combined from sweep
    best_thresh = 0
    best_total = -999
    for thresh in [0.0, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]:
        lrc = LayeredRegimeConfig(envelope=base_env, weights=PermissionWeights(),
                                  long_min_permission=thresh, short_min_permission=thresh)
        regime = LayeredRegime(lrc)
        al = [t for t in evaluate_longs(regime) if t.decision.allowed]
        ash = [t for t in evaluate_shorts(regime) if t.decision.allowed]
        comb = [t.ut for t in al] + [t.ut for t in ash]
        m = metrics_r(comb)
        # Prefer highest PF with N >= 50
        score = m['pf_r'] if m['n'] >= 50 else m['pf_r'] * 0.5
        if score > best_total:
            best_total = score
            best_thresh = thresh
            best_m = m
            best_n_l = len(al)
            best_n_s = len(ash)

    print(f"""
  Frozen Portfolio C:
    N={mf['n']:>4}  PF(R)={_pf(mf['pf_r'])}  Exp={mf['exp_r']:>+.3f}R  TotR={mf['total_r']:>+.2f}  MaxDD={mf['max_dd_r']:.2f}R

  Layered Moderate (L1:-0.10/+0.10, L2:0.25):
    N={mm['n']:>4}  PF(R)={_pf(mm['pf_r'])}  Exp={mm['exp_r']:>+.3f}R  TotR={mm['total_r']:>+.2f}  MaxDD={mm['max_dd_r']:.2f}R

  Best sweep (L2={best_thresh:.2f}, {best_n_l}L+{best_n_s}S):
    N={best_m['n']:>4}  PF(R)={_pf(best_m['pf_r'])}  Exp={best_m['exp_r']:>+.3f}R  TotR={best_m['total_r']:>+.2f}  MaxDD={best_m['max_dd_r']:.2f}R
""")

    print("=" * 120)
    print("  STUDY COMPLETE")
    print("=" * 120)


if __name__ == "__main__":
    main()
