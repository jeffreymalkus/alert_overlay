"""
VWAP Scoring Audit + Chop Ratio Additive Test — Long Side Only (RESEARCH ONLY).

Step 1: Diagnose why VWAP scoring collapses to zero separation
Step 2: Test alternative VWAP transforms
Step 3: Test chop ratio as an additive ingredient to current model
Step 4: Compare all candidates against frozen Portfolio D

Does NOT modify frozen Portfolio D. Long side only.

Usage:
    python -m alert_overlay.vwap_chop_study
"""

import math
import statistics
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
from ..tape_model import (
    TapeReading, TapeWeights, read_tape,
    _score_vwap_state, _score_ema_structure, _score_pressure, _score_rs,
)
from ..layered_regime import (
    PermissionWeights, compute_permission, _score_time_of_day,
)
from .validated_combined_system import (
    classify_spy_days, UTrade, wrap_engine_trade, split_train_test,
)

DATA_DIR = Path(__file__).parent.parent / "data"


# ═══════════════════════════════════════════════════════════════════
#  Metrics
# ═══════════════════════════════════════════════════════════════════

def metrics_r(trades) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "pf_r": 0, "exp_r": 0, "total_r": 0,
                "max_dd_r": 0, "stop_rate": 0, "wr": 0}
    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    wr = len(wins) / n * 100
    total_r = sum(t.pnl_rr for t in trades)
    exp_r = total_r / n
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float('inf')
    cum = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl_rr
        if cum > pk: pk = cum
        if pk - cum > dd: dd = pk - cum
    stops = sum(1 for t in trades if t.exit_reason == "stop")
    return {"n": n, "wr": round(wr, 1), "pf_r": round(pf, 2),
            "exp_r": round(exp_r, 3), "total_r": round(total_r, 2),
            "max_dd_r": round(dd, 2), "stop_rate": round(stops / n * 100, 1)}


def _pf(v): return f"{v:.2f}" if v < 999 else "inf"


def row(label, m, indent="  "):
    print(f"{indent}{label:55s} N={m['n']:>4}  WR={m['wr']:>5.1f}%  "
          f"PF(R)={_pf(m['pf_r']):>6}  Exp={m['exp_r']:>+7.3f}R  "
          f"TotR={m['total_r']:>+8.2f}  MaxDD={m['max_dd_r']:>6.2f}R  "
          f"Stop={m['stop_rate']:>5.1f}%")


def robustness(trades, label, indent="    "):
    if len(trades) < 5:
        print(f"{indent}{label}: N<5, skipping")
        return
    by_day = defaultdict(list)
    by_sym = defaultdict(list)
    for t in trades:
        if t.entry_date: by_day[t.entry_date].append(t)
        by_sym[t.sym].append(t)
    day_r = {d: sum(t.pnl_rr for t in ts) for d, ts in by_day.items()}
    sym_r = {s: sum(t.pnl_rr for t in ts) for s, ts in by_sym.items()}
    best_day = max(day_r, key=day_r.get)
    best_sym = max(sym_r, key=sym_r.get)
    m_full = metrics_r(trades)
    m_xd = metrics_r([t for t in trades if t.entry_date != best_day])
    m_xs = metrics_r([t for t in trades if t.sym != best_sym])
    train, test = split_train_test(trades)
    m_train = metrics_r(train)
    m_test = metrics_r(test)
    print(f"{indent}{label}:")
    print(f"{indent}  Full:          N={m_full['n']:>4}  PF={_pf(m_full['pf_r'])}  "
          f"Exp={m_full['exp_r']:>+.3f}R  TotR={m_full['total_r']:>+.2f}")
    print(f"{indent}  Excl best day: N={m_xd['n']:>4}  PF={_pf(m_xd['pf_r'])}  "
          f"Exp={m_xd['exp_r']:>+.3f}R  TotR={m_xd['total_r']:>+.2f}")
    print(f"{indent}  Excl top sym:  N={m_xs['n']:>4}  PF={_pf(m_xs['pf_r'])}  "
          f"Exp={m_xs['exp_r']:>+.3f}R  TotR={m_xs['total_r']:>+.2f}")
    print(f"{indent}  Train (odd):   N={m_train['n']:>4}  PF={_pf(m_train['pf_r'])}  "
          f"Exp={m_train['exp_r']:>+.3f}R  TotR={m_train['total_r']:>+.2f}")
    print(f"{indent}  Test  (even):  N={m_test['n']:>4}  PF={_pf(m_test['pf_r'])}  "
          f"Exp={m_test['exp_r']:>+.3f}R  TotR={m_test['total_r']:>+.2f}")


# ═══════════════════════════════════════════════════════════════════
#  Alternative VWAP Scoring Functions
# ═══════════════════════════════════════════════════════════════════

def _vwap_current(snap: MarketSnapshot) -> float:
    """Current: linear clamp ±0.5%."""
    if not snap.ready or math.isnan(snap.vwap) or snap.vwap <= 0:
        return 0.0
    pct = (snap.close - snap.vwap) / snap.vwap * 100.0
    return max(-1.0, min(1.0, pct / 0.5))


def _vwap_wider_bounds(snap: MarketSnapshot) -> float:
    """Wider normalization: ±1.0% instead of ±0.5%."""
    if not snap.ready or math.isnan(snap.vwap) or snap.vwap <= 0:
        return 0.0
    pct = (snap.close - snap.vwap) / snap.vwap * 100.0
    return max(-1.0, min(1.0, pct / 1.0))


def _vwap_very_wide(snap: MarketSnapshot) -> float:
    """Very wide: ±2.0%."""
    if not snap.ready or math.isnan(snap.vwap) or snap.vwap <= 0:
        return 0.0
    pct = (snap.close - snap.vwap) / snap.vwap * 100.0
    return max(-1.0, min(1.0, pct / 2.0))


def _vwap_bucket(snap: MarketSnapshot) -> float:
    """Directional bucket: above=+1, at=0, below=-1. No gradient."""
    if not snap.ready or math.isnan(snap.vwap) or snap.vwap <= 0:
        return 0.0
    pct = (snap.close - snap.vwap) / snap.vwap * 100.0
    if pct > 0.10:
        return 1.0
    elif pct < -0.10:
        return -1.0
    return 0.0


def _vwap_no_ready_gate(snap: MarketSnapshot) -> float:
    """Remove the snap.ready gate — use VWAP even before EMA warmup."""
    if math.isnan(snap.vwap) or snap.vwap <= 0:
        return 0.0
    pct = (snap.close - snap.vwap) / snap.vwap * 100.0
    return max(-1.0, min(1.0, pct / 0.5))


def _vwap_no_ready_wider(snap: MarketSnapshot) -> float:
    """No ready gate + wider bounds (±1.0%)."""
    if math.isnan(snap.vwap) or snap.vwap <= 0:
        return 0.0
    pct = (snap.close - snap.vwap) / snap.vwap * 100.0
    return max(-1.0, min(1.0, pct / 1.0))


# ═══════════════════════════════════════════════════════════════════
#  Chop Ratio Scoring
# ═══════════════════════════════════════════════════════════════════

def _compute_chop_score(spy_bars: List[Bar], signal_ts: datetime,
                        lookback: int = 12) -> float:
    """
    Directional efficiency of SPY over last N bars.
    Positive = trending up, negative = trending down.
    """
    relevant = [b for b in spy_bars if b.timestamp <= signal_ts]
    if len(relevant) < lookback + 1:
        return 0.0
    window = relevant[-lookback:]
    if len(window) < 3:
        return 0.0
    net = window[-1].close - window[0].close
    total_path = sum(abs(window[i].close - window[i - 1].close)
                     for i in range(1, len(window)))
    if total_path == 0:
        return 0.0
    return max(-1.0, min(1.0, net / total_path))


# ═══════════════════════════════════════════════════════════════════
#  Permission Computation with Swappable VWAP + Optional Chop
# ═══════════════════════════════════════════════════════════════════

def compute_long_permission(
    market_ctx: MarketContext,
    bar_time_hhmm: int,
    spy_bars: List[Bar],
    signal_ts: datetime,
    vwap_fn=None,        # None = use current, callable = alternative
    add_chop: bool = False,
    chop_weight: float = 0.0,
) -> float:
    """
    Compute long permission with optional VWAP swap and chop addition.
    Returns raw permission score (positive = favorable for longs).
    """
    # Standard components
    mkt_ema = _score_ema_structure(market_ctx.spy) if market_ctx.spy.ready else 0.0
    mkt_pressure = _score_pressure(market_ctx.spy) if market_ctx.spy.ready else 0.0
    sec_ema = _score_ema_structure(market_ctx.sector) if market_ctx.sector and market_ctx.sector.ready else 0.0
    rs_market = _score_rs(market_ctx.rs_market) if not math.isnan(market_ctx.rs_market) else 0.0
    rs_sector = _score_rs(market_ctx.rs_sector) if not math.isnan(market_ctx.rs_sector) else 0.0
    time_score = _score_time_of_day(bar_time_hhmm, 1)

    # VWAP component — swappable
    if vwap_fn is not None:
        mkt_vwap = vwap_fn(market_ctx.spy)
        sec_vwap = vwap_fn(market_ctx.sector) if market_ctx.sector and market_ctx.sector.ready else 0.0
    else:
        mkt_vwap = _score_vwap_state(market_ctx.spy) if market_ctx.spy.ready else 0.0
        sec_vwap = _score_vwap_state(market_ctx.sector) if market_ctx.sector and market_ctx.sector.ready else 0.0

    # Build components with frozen weights
    components = [
        (mkt_vwap, 1.0),
        (mkt_ema, 0.8),
        (mkt_pressure, 0.6),
        (sec_vwap, 0.5),
        (sec_ema, 0.4),
        (rs_market, 0.7),
        (rs_sector, 0.3),
        (time_score, 0.3),
    ]

    # Optionally add chop ratio
    if add_chop and chop_weight > 0:
        chop_score = _compute_chop_score(spy_bars, signal_ts)
        components.append((chop_score, chop_weight))

    total_w = sum(w for _, w in components if w > 0)
    weighted_sum = sum(s * w for s, w in components if w > 0)
    return weighted_sum / total_w if total_w > 0 else 0.0


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════

def main():
    W = 120
    print("=" * W)
    print("  VWAP SCORING AUDIT + CHOP RATIO ADDITIVE TEST — Long Side Only")
    print("  Frozen Portfolio D is UNTOUCHED")
    print("=" * W)

    # ── Load data ──
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    spy_engine = MarketEngine()
    qqq_engine = MarketEngine()
    spy_snaps = {}
    qqq_snaps = {}
    for b in spy_bars: spy_snaps[b.timestamp] = spy_engine.process_bar(b)
    for b in qqq_bars: qqq_snaps[b.timestamp] = qqq_engine.process_bar(b)

    sector_bars_dict = {}
    sector_snap_dict = {}
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
    all_sym_bars = {}
    day_opens = {}
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

    # ── Replay L3-qualified longs ──
    print("  Replaying long book...")
    long_cfg = OverlayConfig()
    long_cfg.show_ema_scalp = False
    long_cfg.show_failed_bounce = False
    long_cfg.show_spencer = False
    long_cfg.show_ema_fpip = False
    long_cfg.show_sc_v2 = False

    all_longs_raw = []
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

    print(f"  L3 longs: {len(l3_longs)}")

    # ═══════════════════════════════════════════════════════════
    #  SECTION 1: RAW VWAP DISTRIBUTION DIAGNOSTIC
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  SECTION 1: VWAP SCORING DIAGNOSTIC — Why does it collapse?")
    print("=" * W)

    # For each L3 long, compute raw VWAP distance at signal time
    raw_mkt_vwap_pcts = []
    raw_sec_vwap_pcts = []
    vwap_scores_current = []
    snap_ready_count = 0
    snap_not_ready_count = 0
    vwap_nan_count = 0

    for t, sym in l3_longs:
        ctx = build_ctx(sym, t.signal.timestamp, t.signal.entry_price)
        if ctx is None: continue

        spy_s = find_snap(spy_snaps, t.signal.timestamp)
        if spy_s is None: continue

        # Check readiness
        if spy_s.ready:
            snap_ready_count += 1
        else:
            snap_not_ready_count += 1

        if math.isnan(spy_s.vwap) or spy_s.vwap <= 0:
            vwap_nan_count += 1
            continue

        pct = (spy_s.close - spy_s.vwap) / spy_s.vwap * 100.0
        raw_mkt_vwap_pcts.append(pct)

        # Current score
        score = _vwap_current(spy_s)
        vwap_scores_current.append(score)

        # Sector VWAP
        sec_etf = get_sector_etf(sym)
        if sec_etf and sec_etf in sector_snap_dict:
            sec_s = find_snap(sector_snap_dict[sec_etf], t.signal.timestamp)
            if sec_s and not math.isnan(sec_s.vwap) and sec_s.vwap > 0:
                sec_pct = (sec_s.close - sec_s.vwap) / sec_s.vwap * 100.0
                raw_sec_vwap_pcts.append(sec_pct)

    print(f"\n  Snapshot readiness: {snap_ready_count} ready, "
          f"{snap_not_ready_count} NOT ready, {vwap_nan_count} VWAP=NaN")

    if raw_mkt_vwap_pcts:
        print(f"\n  Raw SPY VWAP distance (% from VWAP) at signal times:")
        print(f"    N={len(raw_mkt_vwap_pcts)}")
        print(f"    Min={min(raw_mkt_vwap_pcts):+.4f}%")
        print(f"    Max={max(raw_mkt_vwap_pcts):+.4f}%")
        print(f"    Mean={statistics.mean(raw_mkt_vwap_pcts):+.4f}%")
        print(f"    Median={statistics.median(raw_mkt_vwap_pcts):+.4f}%")
        if len(raw_mkt_vwap_pcts) > 1:
            print(f"    StdDev={statistics.stdev(raw_mkt_vwap_pcts):.4f}%")

        # Distribution buckets
        print(f"\n  Distribution of raw SPY VWAP distance:")
        buckets = [
            ("< -1.0%",     lambda x: x < -1.0),
            ("-1.0 to -0.5%", lambda x: -1.0 <= x < -0.5),
            ("-0.5 to -0.2%", lambda x: -0.5 <= x < -0.2),
            ("-0.2 to -0.1%", lambda x: -0.2 <= x < -0.1),
            ("-0.1 to +0.1%", lambda x: -0.1 <= x < 0.1),
            ("+0.1 to +0.2%", lambda x: 0.1 <= x < 0.2),
            ("+0.2 to +0.5%", lambda x: 0.2 <= x < 0.5),
            ("+0.5 to +1.0%", lambda x: 0.5 <= x < 1.0),
            ("> +1.0%",       lambda x: x >= 1.0),
        ]
        print(f"    {'Bucket':<18} {'Count':>6} {'Pct':>6}")
        print(f"    {'-'*35}")
        for label, filt in buckets:
            count = sum(1 for x in raw_mkt_vwap_pcts if filt(x))
            pct = count / len(raw_mkt_vwap_pcts) * 100
            bar = "█" * int(pct / 2)
            print(f"    {label:<18} {count:>6} {pct:>5.1f}%  {bar}")

    if vwap_scores_current:
        print(f"\n  Current VWAP scores (after ±0.5% normalization):")
        score_buckets = defaultdict(int)
        for s in vwap_scores_current:
            if s <= -0.99: score_buckets["-1.0 (capped)"] += 1
            elif s >= 0.99: score_buckets["+1.0 (capped)"] += 1
            elif abs(s) < 0.01: score_buckets["~0.0 (dead)"] += 1
            else: score_buckets["between"] += 1
        for k, v in sorted(score_buckets.items()):
            pct = v / len(vwap_scores_current) * 100
            print(f"    {k:<18}: {v:>4} ({pct:.1f}%)")

        # The key question: how many score exactly 0 because snap not ready?
        zero_scores = sum(1 for s in vwap_scores_current if abs(s) < 0.001)
        print(f"\n  ⚠ Scores that are exactly 0.0: {zero_scores}/{len(vwap_scores_current)} "
              f"({zero_scores/len(vwap_scores_current)*100:.1f}%)")

    if raw_sec_vwap_pcts:
        print(f"\n  Raw Sector VWAP distance:")
        print(f"    N={len(raw_sec_vwap_pcts)}, "
              f"Mean={statistics.mean(raw_sec_vwap_pcts):+.4f}%, "
              f"Median={statistics.median(raw_sec_vwap_pcts):+.4f}%")

    # ═══════════════════════════════════════════════════════════
    #  SECTION 2: ALTERNATIVE VWAP TRANSFORMS
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  SECTION 2: ALTERNATIVE VWAP TRANSFORMS — Does better scoring help?")
    print("=" * W)

    THRESHOLD = 0.40  # Portfolio D long threshold

    vwap_variants = [
        ("A: Current (±0.5% clamp)",     _vwap_current),
        ("B: Wider bounds (±1.0%)",       _vwap_wider_bounds),
        ("C: Very wide (±2.0%)",          _vwap_very_wide),
        ("D: Bucket (above/below/at)",    _vwap_bucket),
        ("E: No ready gate (±0.5%)",      _vwap_no_ready_gate),
        ("F: No ready gate + wider",      _vwap_no_ready_wider),
    ]

    # Also test: remove VWAP entirely (weight=0 equivalent)
    # by setting vwap_fn to always return 0
    def _vwap_zero(snap): return 0.0

    all_vwap_variants = vwap_variants + [("G: VWAP disabled (weight→0)", _vwap_zero)]

    print(f"\n  Testing each VWAP transform with long threshold = {THRESHOLD}")
    print(f"  All other components unchanged (frozen weights)")

    variant_results = {}
    for label, vwap_fn in all_vwap_variants:
        allowed = []
        blocked = []
        for t, sym in l3_longs:
            ctx = build_ctx(sym, t.signal.timestamp, t.signal.entry_price)
            if ctx is None: continue
            h = hhmm(t.signal.timestamp)
            perm = compute_long_permission(ctx, h, spy_bars, t.signal.timestamp,
                                           vwap_fn=vwap_fn)
            ut = wrap_engine_trade(t, sym)
            if perm >= THRESHOLD:
                allowed.append(ut)
            else:
                blocked.append(ut)
        row(label, metrics_r(allowed))
        variant_results[label] = (allowed, blocked)

    # Contribution analysis
    print(f"\n  Separation quality (allowed exp - blocked exp):")
    print(f"  {'Variant':<42} {'N_ok':>5} {'N_blk':>5} {'Ok Exp':>8} {'Blk Exp':>8} {'Delta':>7}")
    print("  " + "-" * 82)
    for label, _ in all_vwap_variants:
        allowed, blocked = variant_results[label]
        ma = metrics_r(allowed)
        mb = metrics_r(blocked)
        delta = ma['exp_r'] - mb['exp_r']
        print(f"  {label:<42} {ma['n']:>5} {mb['n']:>5} {ma['exp_r']:>+8.3f} "
              f"{mb['exp_r']:>+8.3f} {delta:>+7.3f}")

    # ═══════════════════════════════════════════════════════════
    #  SECTION 3: CHOP RATIO AS ADDITIVE INGREDIENT
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  SECTION 3: CHOP RATIO — Additive to Current Model (not replacement)")
    print("=" * W)

    # Test: current model + chop at various weights
    chop_tests = [
        ("Current model (no chop)",         None,  False, 0.0),
        ("Current + chop (weight=0.3)",     None,  True,  0.3),
        ("Current + chop (weight=0.5)",     None,  True,  0.5),
        ("Current + chop (weight=0.7)",     None,  True,  0.7),
        ("Current + chop (weight=1.0)",     None,  True,  1.0),
    ]

    print(f"\n  Adding chop ratio to current model (VWAP stays, chop added)")
    print(f"  Threshold = {THRESHOLD}")

    chop_results = {}
    for label, vwap_fn, add_chop, chop_w in chop_tests:
        allowed = []
        blocked = []
        for t, sym in l3_longs:
            ctx = build_ctx(sym, t.signal.timestamp, t.signal.entry_price)
            if ctx is None: continue
            h = hhmm(t.signal.timestamp)
            perm = compute_long_permission(ctx, h, spy_bars, t.signal.timestamp,
                                           vwap_fn=vwap_fn,
                                           add_chop=add_chop,
                                           chop_weight=chop_w)
            ut = wrap_engine_trade(t, sym)
            if perm >= THRESHOLD:
                allowed.append(ut)
            else:
                blocked.append(ut)
        row(label, metrics_r(allowed))
        chop_results[label] = (allowed, blocked)

    # Also test: best VWAP variant + chop
    print(f"\n  Best VWAP fix + chop ratio:")
    best_vwap_chop_tests = [
        ("No-ready-gate VWAP + chop(0.5)", _vwap_no_ready_gate, True, 0.5),
        ("No-ready-gate VWAP + chop(0.7)", _vwap_no_ready_gate, True, 0.7),
        ("Wider VWAP + chop(0.5)",         _vwap_wider_bounds,  True, 0.5),
        ("Wider VWAP + chop(0.7)",         _vwap_wider_bounds,  True, 0.7),
        ("VWAP disabled + chop(0.5)",      _vwap_zero,          True, 0.5),
        ("VWAP disabled + chop(0.7)",      _vwap_zero,          True, 0.7),
    ]

    for label, vwap_fn, add_chop, chop_w in best_vwap_chop_tests:
        allowed = []
        blocked = []
        for t, sym in l3_longs:
            ctx = build_ctx(sym, t.signal.timestamp, t.signal.entry_price)
            if ctx is None: continue
            h = hhmm(t.signal.timestamp)
            perm = compute_long_permission(ctx, h, spy_bars, t.signal.timestamp,
                                           vwap_fn=vwap_fn,
                                           add_chop=add_chop,
                                           chop_weight=chop_w)
            ut = wrap_engine_trade(t, sym)
            if perm >= THRESHOLD:
                allowed.append(ut)
            else:
                blocked.append(ut)
        row(label, metrics_r(allowed))
        chop_results[label] = (allowed, blocked)

    # ═══════════════════════════════════════════════════════════
    #  SECTION 4: HEAD-TO-HEAD vs PORTFOLIO D
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  SECTION 4: HEAD-TO-HEAD vs Portfolio D (long book only)")
    print("=" * W)

    # Portfolio D longs = current model at threshold 0.40
    d_longs, _ = variant_results["A: Current (±0.5% clamp)"]
    print(f"\n  Portfolio D long book:")
    row("Portfolio D longs", metrics_r(d_longs))
    robustness(d_longs, "Portfolio D longs")

    # Find candidates that beat Portfolio D
    print(f"\n  Candidates that produce different trade counts:")
    all_candidates = {}
    all_candidates.update(variant_results)
    all_candidates.update(chop_results)

    d_metrics = metrics_r(d_longs)
    d_n = d_metrics["n"]

    for label in sorted(all_candidates.keys()):
        allowed, blocked = all_candidates[label]
        m = metrics_r(allowed)
        if m["n"] != d_n and m["n"] >= 5:
            beats_pf = m["pf_r"] >= d_metrics["pf_r"]
            beats_exp = m["exp_r"] >= d_metrics["exp_r"]
            beats_dd = m["max_dd_r"] <= d_metrics["max_dd_r"]
            beats_all = beats_pf and beats_exp and beats_dd
            flag = " ★ BEATS D" if beats_all else ""
            row(label, m)
            if m["n"] >= 8:
                robustness(allowed, label)
            print()

    print("\n" + "=" * W)
    print("  STUDY COMPLETE")
    print("  If no variant beats Portfolio D on PF + Exp + MaxDD + stability,")
    print("  the current model stays frozen.")
    print("=" * W + "\n")


if __name__ == "__main__":
    main()
