"""
Regime/Tape Model v2 — Component Substitution Study (RESEARCH ONLY).

Tests whether replacing weak ingredients (VWAP) with stronger ones
(range ATR ratio, chop ratio, opening drive) improves the regime/tape
model BEFORE any weight calibration.

Variants tested:
  V1: Current model (baseline with VWAP)
  V2: Current minus VWAP (remove dead weight)
  V3: VWAP replaced by range ATR ratio
  V4: VWAP replaced by chop ratio
  V5: VWAP replaced by opening drive
  V6: Compact v2 (EMA + pressure + RS_sector + range_ATR + chop)

For shorts, also tests incremental value of:
  - Time-from-open gating
  - Lower wick % filter
  - Volume ratio filter

Does NOT modify frozen Portfolio D. Does NOT optimize weights.
All evaluation in R-multiples.

Usage:
    python -m alert_overlay.v2_ingredient_study
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
from .tape_model import (
    TapeReading, TapeWeights, read_tape,
    _score_ema_structure, _score_pressure, _score_rs, _score_vwap_state,
)
from .layered_regime import (
    PermissionWeights, compute_permission, _score_time_of_day,
)
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
#  New Ingredient Scoring Functions (each → [-1, +1])
# ═══════════════════════════════════════════════════════════════════

def _score_range_atr_ratio(spy_bars: List[Bar], signal_ts: datetime,
                           _cache: Dict = {}) -> float:
    """
    Today's SPY range vs average prior-day range.
    Compressed (<0.6) → +1 (favorable for longs)
    Normal (0.6-1.0) → 0
    Expanded (>1.4) → -1 (unfavorable — volatility too high)
    Directionally neutral — scored by magnitude, not sign.
    """
    sig_date = signal_ts.date()
    cache_key = sig_date

    if cache_key in _cache:
        ratio = _cache[cache_key]
    else:
        day_high, day_low = -1e9, 1e9
        prior_ranges = []
        seen_dates = set()
        in_today = False

        for b in spy_bars:
            bd = b.timestamp.date()
            if bd == sig_date:
                in_today = True
                day_high = max(day_high, b.high)
                day_low = min(day_low, b.low)
            elif bd < sig_date:
                if bd not in seen_dates:
                    # Compute this day's range
                    dh, dl = -1e9, 1e9
                    for b2 in spy_bars:
                        if b2.timestamp.date() == bd:
                            dh = max(dh, b2.high)
                            dl = min(dl, b2.low)
                    if dh > dl:
                        prior_ranges.append(dh - dl)
                    seen_dates.add(bd)

        if not in_today or day_high <= day_low or not prior_ranges:
            _cache[cache_key] = None
            return 0.0

        avg_range = sum(prior_ranges[-20:]) / len(prior_ranges[-20:])
        ratio = (day_high - day_low) / avg_range if avg_range > 0 else None
        _cache[cache_key] = ratio

    if ratio is None:
        return 0.0

    # Compressed = favorable, expanded = unfavorable
    # ratio < 0.6 → +1, ratio = 1.0 → 0, ratio > 1.4 → -1
    score = 1.0 - (ratio - 0.6) / 0.4  # linear: 0.6→+1, 1.0→0
    return max(-1.0, min(1.0, score))


def _score_chop_ratio(spy_bars: List[Bar], signal_ts: datetime,
                      lookback_bars: int = 12) -> float:
    """
    Directional efficiency of SPY over last N bars.
    |net move| / sum(|bar moves|).
    Positive efficiency = trending up → +1
    Negative efficiency = trending down → -1
    Choppy → near 0.
    """
    # Find bars up to signal_ts
    relevant = [b for b in spy_bars if b.timestamp <= signal_ts]
    if len(relevant) < lookback_bars + 1:
        return 0.0

    window = relevant[-lookback_bars:]
    if len(window) < 3:
        return 0.0

    net = window[-1].close - window[0].close
    total_path = sum(abs(window[i].close - window[i - 1].close)
                     for i in range(1, len(window)))
    if total_path == 0:
        return 0.0

    efficiency = net / total_path  # range [-1, +1]
    return max(-1.0, min(1.0, efficiency))


def _score_opening_drive(spy_bars: List[Bar], signal_ts: datetime,
                         _cache: Dict = {}) -> float:
    """
    SPY % move from open to end of first hour.
    Strong buy (+0.5%+) → +1
    Mild buy → +0.5
    Mild sell → -0.5
    Strong sell (-0.5%+) → -1
    """
    sig_date = signal_ts.date()
    if sig_date in _cache:
        pct = _cache[sig_date]
    else:
        day_open = None
        first_hour_close = None
        for b in spy_bars:
            if b.timestamp.date() != sig_date:
                continue
            if day_open is None:
                day_open = b.open
            if b.timestamp.hour < 10 or (b.timestamp.hour == 10 and b.timestamp.minute <= 30):
                first_hour_close = b.close

        if day_open is None or day_open == 0 or first_hour_close is None:
            _cache[sig_date] = None
            pct = None
        else:
            pct = (first_hour_close - day_open) / day_open * 100.0
            _cache[sig_date] = pct

    if pct is None:
        return 0.0
    # ±0.5% → ±1
    return max(-1.0, min(1.0, pct / 0.5))


# ═══════════════════════════════════════════════════════════════════
#  Short-Side Candidate Feature Computations
# ═══════════════════════════════════════════════════════════════════

def _bars_from_open_count(sym_bars: List[Bar], signal_ts: datetime) -> int:
    sig_date = signal_ts.date()
    count = 0
    for b in sym_bars:
        if b.timestamp.date() == sig_date and b.timestamp <= signal_ts:
            count += 1
    return count


def _signal_bar_lower_wick(sym_bars: List[Bar], signal_ts: datetime) -> float:
    """Lower wick as fraction of bar range. For BDR shorts, find nearest bar."""
    best = None
    for b in sym_bars:
        if b.timestamp <= signal_ts:
            best = b
        else:
            break
    if best is None:
        return 0.0
    rng = best.high - best.low
    if rng <= 0:
        return 0.0
    return (min(best.open, best.close) - best.low) / rng


def _signal_bar_volume_ratio(sym_bars: List[Bar], signal_ts: datetime) -> float:
    """Signal bar volume / session average volume."""
    sig_date = signal_ts.date()
    session_vols = []
    signal_vol = None
    for b in sym_bars:
        if b.timestamp.date() != sig_date:
            continue
        if b.timestamp <= signal_ts:
            session_vols.append(b.volume)
            signal_vol = b.volume
    if not session_vols or signal_vol is None or len(session_vols) < 2:
        return 1.0
    avg = sum(session_vols) / len(session_vols)
    return signal_vol / avg if avg > 0 else 1.0


# ═══════════════════════════════════════════════════════════════════
#  V2 Permission Computation — swappable components
# ═══════════════════════════════════════════════════════════════════

def compute_v2_permission(
    market_ctx: MarketContext,
    direction: int,
    bar_time_hhmm: int,
    spy_bars: List[Bar],
    signal_ts: datetime,
    variant: str = "current",
) -> float:
    """
    Compute permission score with swappable ingredients.

    Variants:
      "current"    — V1 baseline (includes VWAP)
      "no_vwap"    — V2 remove VWAP, keep rest
      "range_atr"  — V3 replace VWAP with range ATR ratio
      "chop"       — V4 replace VWAP with chop ratio
      "open_drive" — V5 replace VWAP with opening drive
      "compact"    — V6 EMA + pressure + RS_sector + range_ATR + chop
    """
    # Score all components
    mkt_vwap = _score_vwap_state(market_ctx.spy) if market_ctx.spy.ready else 0.0
    mkt_ema = _score_ema_structure(market_ctx.spy) if market_ctx.spy.ready else 0.0
    mkt_pressure = _score_pressure(market_ctx.spy) if market_ctx.spy.ready else 0.0
    sec_vwap = _score_vwap_state(market_ctx.sector) if market_ctx.sector and market_ctx.sector.ready else 0.0
    sec_ema = _score_ema_structure(market_ctx.sector) if market_ctx.sector and market_ctx.sector.ready else 0.0
    rs_market = _score_rs(market_ctx.rs_market) if not math.isnan(market_ctx.rs_market) else 0.0
    rs_sector = _score_rs(market_ctx.rs_sector) if not math.isnan(market_ctx.rs_sector) else 0.0
    time_score = _score_time_of_day(bar_time_hhmm, direction)

    # New candidate scores
    range_atr = _score_range_atr_ratio(spy_bars, signal_ts)
    chop = _score_chop_ratio(spy_bars, signal_ts)
    open_drive = _score_opening_drive(spy_bars, signal_ts)

    # Build component list based on variant
    if variant == "current":
        # V1: full current model
        components = [
            (mkt_vwap, 1.0), (mkt_ema, 0.8), (mkt_pressure, 0.6),
            (sec_vwap, 0.5), (sec_ema, 0.4),
            (rs_market, 0.7), (rs_sector, 0.3),
            (time_score, 0.3),
        ]
    elif variant == "no_vwap":
        # V2: remove VWAP (mkt + sector)
        components = [
            (mkt_ema, 0.8), (mkt_pressure, 0.6),
            (sec_ema, 0.4),
            (rs_market, 0.7), (rs_sector, 0.3),
            (time_score, 0.3),
        ]
    elif variant == "range_atr":
        # V3: replace VWAP with range ATR ratio
        components = [
            (range_atr, 1.0), (mkt_ema, 0.8), (mkt_pressure, 0.6),
            (range_atr, 0.5), (sec_ema, 0.4),  # sector VWAP → range_atr (shared)
            (rs_market, 0.7), (rs_sector, 0.3),
            (time_score, 0.3),
        ]
    elif variant == "chop":
        # V4: replace VWAP with chop ratio
        components = [
            (chop, 1.0), (mkt_ema, 0.8), (mkt_pressure, 0.6),
            (chop, 0.5), (sec_ema, 0.4),  # sector VWAP → chop (shared)
            (rs_market, 0.7), (rs_sector, 0.3),
            (time_score, 0.3),
        ]
    elif variant == "open_drive":
        # V5: replace VWAP with opening drive
        components = [
            (open_drive, 1.0), (mkt_ema, 0.8), (mkt_pressure, 0.6),
            (open_drive, 0.5), (sec_ema, 0.4),
            (rs_market, 0.7), (rs_sector, 0.3),
            (time_score, 0.3),
        ]
    elif variant == "compact":
        # V6: compact model — only strong ingredients
        # EMA + pressure + RS_sector + range_ATR + chop
        components = [
            (mkt_ema, 1.0),       # top performer
            (mkt_pressure, 0.8),  # second strongest
            (rs_sector, 0.5),     # moderate but consistent
            (range_atr, 0.7),     # strongest candidate
            (chop, 0.6),          # strong candidate
            (time_score, 0.3),
        ]
    else:
        raise ValueError(f"Unknown variant: {variant}")

    # Weighted average
    total_w = sum(w for _, w in components if w > 0)
    weighted_sum = sum(s * w for s, w in components if w > 0)
    raw = weighted_sum / total_w if total_w > 0 else 0.0

    # Direction-adjust
    if direction == 1:
        return raw
    elif direction == -1:
        return -raw
    return 0.0


# ═══════════════════════════════════════════════════════════════════
#  Metrics & Reporting
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
    # Max drawdown in R
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
#  Main Study
# ═══════════════════════════════════════════════════════════════════

def main():
    W = 120
    print("=" * W)
    print("  REGIME/TAPE MODEL v2 — Component Substitution Study")
    print("  Goal: better ingredients, not better weights")
    print("  Frozen Portfolio D is UNTOUCHED")
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

    wl = Path(__file__).parent / "watchlist.txt"
    symbols = []
    with open(wl) as f:
        for line in f:
            s = line.strip().upper()
            if s and not s.startswith("#") and s not in ("SPY", "QQQ"):
                symbols.append(s)

    spy_day_info = classify_spy_days(spy_bars)

    all_sym_bars: Dict[str, List[Bar]] = {}
    day_opens: Dict[Tuple[str, object], float] = {}
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

    # ── Helpers ──
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

    # ── Replay L3-qualified trades ──
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

    print("  Replaying BDR scanner...")
    cfg = OverlayConfig()
    all_bdr = []
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

    # ── Frozen baselines ──
    def is_red(t):
        return spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == "RED"

    frozen_longs = [wrap_engine_trade(t, sym) for t, sym in l3_longs if not is_red(t)]
    rt_dates = set(d for d, info in spy_day_info.items()
                   if info["direction"] == "RED" and info["character"] == "TREND")
    frozen_shorts = [wrap_bdr_trade(t) for t in l3_shorts if t.entry_time.date() in rt_dates]

    # Portfolio D baseline: tape >= 0.40 for longs, frozen shorts
    d_longs = []
    for t, sym in l3_longs:
        ctx = build_ctx(sym, t.signal.timestamp, t.signal.entry_price)
        if ctx is None: continue
        h = hhmm(t.signal.timestamp)
        perm = compute_v2_permission(ctx, 1, h, spy_bars, t.signal.timestamp, "current")
        if perm >= 0.40:
            d_longs.append(wrap_engine_trade(t, sym))

    portfolio_d = d_longs + frozen_shorts

    print(f"  Portfolio D baseline: {len(d_longs)} longs + {len(frozen_shorts)} shorts = {len(portfolio_d)}")

    # ═══════════════════════════════════════════════════════════
    #  SECTION 1: LONG BOOK — Component Substitution
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  SECTION 1: LONG BOOK — Component Substitution (threshold = 0.40)")
    print("=" * W)

    LONG_THRESHOLD = 0.40
    variants = [
        ("V1: Current (with VWAP)",       "current"),
        ("V2: Remove VWAP",               "no_vwap"),
        ("V3: VWAP → Range ATR Ratio",    "range_atr"),
        ("V4: VWAP → Chop Ratio",         "chop"),
        ("V5: VWAP → Opening Drive",      "open_drive"),
        ("V6: Compact (EMA+Press+RS+ATR+Chop)", "compact"),
    ]

    print(f"\n  Frozen long book (Non-RED, Q>=2, <15:30):")
    row("Frozen Portfolio C longs", metrics_r(frozen_longs))
    row("Portfolio D longs (tape≥0.40)", metrics_r(d_longs))
    print()

    # Clear caches
    _score_range_atr_ratio.__defaults__[0].clear()
    _score_opening_drive.__defaults__[0].clear()

    long_results = {}
    for label, variant_key in variants:
        allowed = []
        blocked = []
        for t, sym in l3_longs:
            ctx = build_ctx(sym, t.signal.timestamp, t.signal.entry_price)
            if ctx is None: continue
            h = hhmm(t.signal.timestamp)
            perm = compute_v2_permission(ctx, 1, h, spy_bars, t.signal.timestamp, variant_key)
            ut = wrap_engine_trade(t, sym)
            if perm >= LONG_THRESHOLD:
                allowed.append(ut)
            else:
                blocked.append(ut)
        row(label, metrics_r(allowed))
        long_results[variant_key] = (allowed, blocked)

    # Component contribution analysis for each variant
    print(f"\n  Component Contribution — What each variant blocks vs allows:")
    print(f"  {'Variant':<45} {'Allowed':>4} {'Blocked':>4} {'Blk Exp':>8} {'Alw Exp':>8} {'Delta':>7}")
    print("  " + "-" * 80)
    for label, variant_key in variants:
        allowed, blocked = long_results[variant_key]
        ma = metrics_r(allowed)
        mb = metrics_r(blocked)
        delta = ma['exp_r'] - mb['exp_r']
        print(f"  {label:<45} {ma['n']:>4} {mb['n']:>4} {mb['exp_r']:>+8.3f} "
              f"{ma['exp_r']:>+8.3f} {delta:>+7.3f}")

    # Robustness for top 3
    print("\n  Robustness Checks:")
    for label, variant_key in variants:
        allowed, _ = long_results[variant_key]
        if len(allowed) >= 5:
            robustness(allowed, label)
            print()

    # ═══════════════════════════════════════════════════════════
    #  SECTION 2: SHORT BOOK — Component Substitution
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  SECTION 2: SHORT BOOK — Component Substitution")
    print("=" * W)

    SHORT_THRESHOLD = 0.40  # Same threshold for comparison
    print(f"\n  Frozen short book (RED+TREND, wick>=30%, AM):")
    row("Frozen Portfolio D shorts", metrics_r(frozen_shorts))
    print()

    # Clear caches
    _score_range_atr_ratio.__defaults__[0].clear()
    _score_opening_drive.__defaults__[0].clear()

    short_results = {}
    for label, variant_key in variants:
        allowed = []
        blocked = []
        for t in l3_shorts:
            ctx = build_ctx(t.sym, t.entry_time, t.entry_price)
            if ctx is None: continue
            h = hhmm(t.entry_time)
            perm = compute_v2_permission(ctx, -1, h, spy_bars, t.entry_time, variant_key)
            ut = wrap_bdr_trade(t)
            if perm >= SHORT_THRESHOLD:
                allowed.append(ut)
            else:
                blocked.append(ut)
        row(label, metrics_r(allowed))
        short_results[variant_key] = (allowed, blocked)

    # Contribution
    print(f"\n  Component Contribution:")
    print(f"  {'Variant':<45} {'Allowed':>4} {'Blocked':>4} {'Blk Exp':>8} {'Alw Exp':>8} {'Delta':>7}")
    print("  " + "-" * 80)
    for label, variant_key in variants:
        allowed, blocked = short_results[variant_key]
        ma = metrics_r(allowed)
        mb = metrics_r(blocked)
        delta = ma['exp_r'] - mb['exp_r']
        print(f"  {label:<45} {ma['n']:>4} {mb['n']:>4} {mb['exp_r']:>+8.3f} "
              f"{ma['exp_r']:>+8.3f} {delta:>+7.3f}")

    # ═══════════════════════════════════════════════════════════
    #  SECTION 3: SHORT BOOK — Incremental Feature Gates
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  SECTION 3: SHORT BOOK — Incremental Feature Gates")
    print("  Starting from frozen shorts, test if adding candidate features helps")
    print("=" * W)

    # Start from frozen shorts
    print(f"\n  Baseline frozen shorts: N={len(frozen_shorts)}")
    row("Frozen (RED+TREND, wick>=30%, AM)", metrics_r(frozen_shorts))

    # For each frozen short trade, compute candidate features
    class ShortFeatureRecord:
        __slots__ = ("ut", "bars_from_open", "lower_wick", "volume_ratio")

    short_enriched = []
    for t in l3_shorts:
        if t.entry_time.date() not in rt_dates:
            continue
        ut = wrap_bdr_trade(t)
        rec = ShortFeatureRecord()
        rec.ut = ut
        sym_bars = all_sym_bars.get(t.sym, [])
        rec.bars_from_open = _bars_from_open_count(sym_bars, t.entry_time)
        rec.lower_wick = _signal_bar_lower_wick(sym_bars, t.entry_time)
        rec.volume_ratio = _signal_bar_volume_ratio(sym_bars, t.entry_time)
        short_enriched.append(rec)

    # Test time-from-open gates
    print("\n  A. Time-from-open gate (bars since session open):")
    for max_bars in [18, 15, 12, 999]:
        filtered = [r.ut for r in short_enriched if r.bars_from_open <= max_bars]
        label = f"Bars <= {max_bars}" if max_bars < 999 else "No filter"
        row(label, metrics_r(filtered))

    # Test lower wick gates
    print("\n  B. Lower wick % gate:")
    for min_wick in [0.0, 0.10, 0.20, 0.30]:
        filtered = [r.ut for r in short_enriched if r.lower_wick >= min_wick]
        label = f"Lower wick >= {min_wick:.0%}"
        row(label, metrics_r(filtered))

    # Test volume ratio gates
    print("\n  C. Volume ratio gate (signal bar vs session avg):")
    for max_vol in [3.0, 2.0, 1.5, 1.0]:
        filtered = [r.ut for r in short_enriched if r.volume_ratio <= max_vol]
        label = f"Volume ratio <= {max_vol:.1f}"
        row(label, metrics_r(filtered))

    # Combined: best of each
    print("\n  D. Combined short feature gates:")
    combos = [
        ("Frozen only",                    lambda r: True),
        ("+ Bars<=18",                     lambda r: r.bars_from_open <= 18),
        ("+ Bars<=15",                     lambda r: r.bars_from_open <= 15),
        ("+ VolRatio<=2.0",                lambda r: r.volume_ratio <= 2.0),
        ("+ Bars<=18 + VolRatio<=2.0",     lambda r: r.bars_from_open <= 18 and r.volume_ratio <= 2.0),
        ("+ Bars<=15 + VolRatio<=2.0",     lambda r: r.bars_from_open <= 15 and r.volume_ratio <= 2.0),
    ]
    for label, filt in combos:
        filtered = [r.ut for r in short_enriched if filt(r)]
        row(label, metrics_r(filtered))

    # ═══════════════════════════════════════════════════════════
    #  SECTION 4: COMBINED PORTFOLIO — Best Variant
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * W)
    print("  SECTION 4: COMBINED PORTFOLIO — Portfolio D vs Best v2 Variants")
    print("=" * W)

    print(f"\n  Portfolio D (current frozen):")
    row("Portfolio D", metrics_r(portfolio_d))
    robustness(portfolio_d, "Portfolio D")

    # Build combined portfolios with each long variant + frozen shorts
    print(f"\n  Combined with frozen shorts:")
    for label, variant_key in variants:
        allowed, _ = long_results[variant_key]
        combined = allowed + frozen_shorts
        row(label + " + frozen shorts", metrics_r(combined))

    # Build combined with best short improvement
    best_short_combo = [r.ut for r in short_enriched
                        if r.bars_from_open <= 18 and r.volume_ratio <= 2.0]
    print(f"\n  Best short improvement: Bars<=18 + VolRatio<=2.0")
    row("Improved shorts only", metrics_r(best_short_combo))

    # Combine best long variant(s) with improved shorts
    print(f"\n  Full v2 candidates (best long + improved short):")
    for label, variant_key in variants:
        allowed, _ = long_results[variant_key]
        combined = allowed + best_short_combo
        row(label + " + improved shorts", metrics_r(combined))

    print(f"\n  Robustness of best combinations:")
    for label, variant_key in [("V3: Range ATR", "range_atr"),
                                ("V6: Compact", "compact"),
                                ("V2: No VWAP", "no_vwap")]:
        allowed, _ = long_results[variant_key]
        combined = allowed + frozen_shorts
        if len(combined) >= 5:
            robustness(combined, f"{label} + frozen shorts")
            print()

    print("\n" + "=" * W)
    print("  STUDY COMPLETE")
    print("  Compare: which ingredient set produces the best separation")
    print("  between allowed and blocked trades, with robust out-of-sample?")
    print("=" * W + "\n")


if __name__ == "__main__":
    main()
