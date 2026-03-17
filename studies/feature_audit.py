"""
Feature Audit — Regime/Tape Ingredient Quality Assessment (RESEARCH ONLY).

Evaluates current regime/tape components AND candidate new features as
predictive features for trade outcomes in R-multiples.

For each feature:
  1. Pass/fail or bucket performance in R
  2. Effect size / separation (high-bucket exp vs low-bucket exp)
  3. Whether it adds information beyond the current regime model

Does NOT optimize weights. Does NOT replace the frozen system.
Goal: determine whether the regime model needs better ingredients.

Usage:
    python -m alert_overlay.feature_audit
"""

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import Bar, Signal, SetupId, SetupFamily, NaN, SETUP_DISPLAY_NAME
from ..market_context import (
    MarketEngine, MarketContext, MarketSnapshot,
    compute_market_context, get_sector_etf,
)
from ..tape_model import TapeReading, TapeWeights, read_tape

DATA_DIR = Path(__file__).parent.parent / "data"

# ═══════════════════════════════════════════════════════════════════
#  Data Loading (reuse proven patterns from tape_research)
# ═══════════════════════════════════════════════════════════════════

def _load_bars(symbol: str) -> Optional[List[Bar]]:
    path = DATA_DIR / f"{symbol}_5min.csv"
    if not path.exists():
        return None
    return load_bars_from_csv(str(path))


def _get_universe() -> List[str]:
    watchlist = Path(__file__).parent.parent / "watchlist.txt"
    if watchlist.exists():
        syms = []
        with open(watchlist) as f:
            for line in f:
                s = line.strip().upper()
                if s and not s.startswith("#") and s not in ("SPY", "QQQ"):
                    syms.append(s)
        return syms
    return []


def _find_nearest_snapshot(snapshots, target_ts):
    if target_ts in snapshots:
        return snapshots[target_ts]
    for delta_min in range(1, 11):
        candidate = target_ts - timedelta(minutes=delta_min)
        if candidate in snapshots:
            return snapshots[candidate]
    return None


def _find_day_open(bars, target_ts):
    target_date = target_ts.date()
    for bar in bars:
        if bar.timestamp.date() == target_date:
            return bar.open
    return 0.0


# ═══════════════════════════════════════════════════════════════════
#  Enriched Trade Record — current + candidate features
# ═══════════════════════════════════════════════════════════════════

class AuditRecord:
    """A trade annotated with ALL features for audit."""
    __slots__ = [
        "symbol", "signal_ts", "setup_name", "direction",
        "pnl_rr", "exit_reason", "quality", "hour",
        # ── Current regime/tape components (7 existing) ──
        "mkt_vwap", "mkt_ema", "mkt_pressure",
        "sec_vwap", "sec_ema", "rs_market", "rs_sector",
        "tape_score", "tape_permission",
        # ── SPY raw state ──
        "spy_pct_from_open", "spy_close_pos",
        # ── Candidate features (new) ──
        "trend_persistence",   # consecutive bars in same direction
        "chop_ratio",          # directional efficiency over lookback
        "opening_drive",       # % move from open in first hour
        "range_position",      # where current price sits in day range
        "range_atr_ratio",     # day range vs avg range (compression/expansion)
        "bar_body_ratio",      # body/range of signal bar (rejection quality)
        "bar_upper_wick_pct",  # upper wick as % of range
        "bar_lower_wick_pct",  # lower wick as % of range
        "volume_ratio",        # signal bar volume vs session avg
        "bars_from_open",      # number of bars since open
        "time_regime_bucket",  # time-of-day x regime interaction
    ]

    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, NaN if s not in (
                "symbol", "signal_ts", "setup_name", "direction",
                "exit_reason", "time_regime_bucket") else None)


# ═══════════════════════════════════════════════════════════════════
#  Candidate Feature Computations
# ═══════════════════════════════════════════════════════════════════

def _compute_trend_persistence(bars: List[Bar], idx: int, lookback: int = 6) -> float:
    """
    Count consecutive bars closing in the same direction (higher or lower).
    Positive = consecutive higher closes, negative = consecutive lower.
    Normalized to [-1, +1] over lookback window.
    """
    if idx < 1:
        return 0.0
    streak = 0
    direction = 0
    for i in range(idx, max(idx - lookback, 0), -1):
        if i < 1:
            break
        if bars[i].close > bars[i - 1].close:
            if direction == 0:
                direction = 1
            if direction == 1:
                streak += 1
            else:
                break
        elif bars[i].close < bars[i - 1].close:
            if direction == 0:
                direction = -1
            if direction == -1:
                streak += 1
            else:
                break
        else:
            break
    return max(-1.0, min(1.0, (streak * direction) / lookback))


def _compute_chop_ratio(bars: List[Bar], idx: int, lookback: int = 12) -> float:
    """
    Directional efficiency = |net move| / sum(|bar moves|) over lookback.
    1.0 = perfectly directional, 0.0 = pure chop.
    Returns signed value: positive if net up, negative if net down.
    """
    start = max(0, idx - lookback)
    if idx - start < 3:
        return 0.0
    net = bars[idx].close - bars[start].close
    total_path = sum(abs(bars[i].close - bars[i - 1].close)
                     for i in range(start + 1, idx + 1))
    if total_path == 0:
        return 0.0
    efficiency = net / total_path  # Already signed, range [-1, +1]
    return max(-1.0, min(1.0, efficiency))


def _compute_opening_drive(bars: List[Bar], signal_ts: datetime) -> float:
    """
    SPY % move from open to the end of the first hour (10:30).
    Captures whether the opening was strong directional or a fade.
    Returns raw pct, capped at ±1.0%.
    """
    target_date = signal_ts.date()
    day_open = None
    first_hour_close = None
    for bar in bars:
        if bar.timestamp.date() != target_date:
            continue
        if day_open is None:
            day_open = bar.open
        # Find last bar before or at 10:30
        if bar.timestamp.hour == 10 and bar.timestamp.minute <= 30:
            first_hour_close = bar.close
        elif bar.timestamp.hour < 10:
            first_hour_close = bar.close
    if day_open is None or day_open == 0 or first_hour_close is None:
        return NaN
    pct = (first_hour_close - day_open) / day_open * 100.0
    return max(-1.0, min(1.0, pct / 1.0))  # ±1% → ±1


def _compute_range_atr_ratio(bars: List[Bar], idx: int, lookback: int = 20) -> float:
    """
    Today's high-low range vs average of prior N days' ranges.
    >1 = expansion, <1 = compression.
    """
    current_date = bars[idx].timestamp.date()
    # Get today's range
    day_high = -1e9
    day_low = 1e9
    for i in range(idx, -1, -1):
        if bars[i].timestamp.date() != current_date:
            break
        day_high = max(day_high, bars[i].high)
        day_low = min(day_low, bars[i].low)
    today_range = day_high - day_low
    if today_range <= 0:
        return NaN

    # Get prior days' ranges
    prior_ranges = []
    seen_dates = set()
    for i in range(idx, -1, -1):
        d = bars[i].timestamp.date()
        if d == current_date:
            continue
        if d in seen_dates:
            continue
        # Compute that day's range
        dh = -1e9
        dl = 1e9
        for j in range(i, -1, -1):
            if bars[j].timestamp.date() != d:
                break
            dh = max(dh, bars[j].high)
            dl = min(dl, bars[j].low)
        if dh > dl:
            prior_ranges.append(dh - dl)
            seen_dates.add(d)
        if len(prior_ranges) >= lookback:
            break
    if not prior_ranges:
        return NaN
    avg_range = sum(prior_ranges) / len(prior_ranges)
    if avg_range == 0:
        return NaN
    return today_range / avg_range


def _compute_bar_anatomy(bar: Bar) -> Tuple[float, float, float]:
    """
    Signal bar anatomy:
    - body_ratio: |close-open| / (high-low), [0, 1]
    - upper_wick_pct: (high - max(open,close)) / (high-low), [0, 1]
    - lower_wick_pct: (min(open,close) - low) / (high-low), [0, 1]
    """
    rng = bar.high - bar.low
    if rng <= 0:
        return 0.0, 0.0, 0.0
    body = abs(bar.close - bar.open)
    body_ratio = body / rng
    upper_wick = (bar.high - max(bar.open, bar.close)) / rng
    lower_wick = (min(bar.open, bar.close) - bar.low) / rng
    return body_ratio, upper_wick, lower_wick


def _compute_volume_ratio(bars: List[Bar], idx: int) -> float:
    """
    Signal bar volume relative to session average so far.
    >1 = above average, <1 = below.
    """
    current_date = bars[idx].timestamp.date()
    session_vols = []
    for i in range(idx, -1, -1):
        if bars[i].timestamp.date() != current_date:
            break
        session_vols.append(bars[i].volume)
    if not session_vols or len(session_vols) < 2:
        return NaN
    avg = sum(session_vols) / len(session_vols)
    if avg == 0:
        return NaN
    return bars[idx].volume / avg


def _bars_from_open(bars: List[Bar], idx: int) -> int:
    """Count how many bars since session open."""
    current_date = bars[idx].timestamp.date()
    count = 0
    for i in range(idx, -1, -1):
        if bars[i].timestamp.date() != current_date:
            break
        count += 1
    return count


# ═══════════════════════════════════════════════════════════════════
#  Main Audit Engine
# ═══════════════════════════════════════════════════════════════════

def run_feature_audit(verbose: bool = True) -> List[AuditRecord]:
    """
    Replay all trades, compute current + candidate features for each,
    return annotated records.
    """
    cfg = OverlayConfig()
    cfg.use_market_context = True
    weights = TapeWeights()

    universe = _get_universe()
    if verbose:
        print(f"  Universe: {len(universe)} symbols")

    # Load market data
    spy_bars = _load_bars("SPY")
    qqq_bars = _load_bars("QQQ")
    if not spy_bars or not qqq_bars:
        print("ERROR: SPY or QQQ data missing.")
        return []

    # Build snapshot maps
    spy_engine = MarketEngine()
    qqq_engine = MarketEngine()
    spy_snapshots = {}
    qqq_snapshots = {}
    for bar in spy_bars:
        spy_snapshots[bar.timestamp] = spy_engine.process_bar(bar)
    for bar in qqq_bars:
        qqq_snapshots[bar.timestamp] = qqq_engine.process_bar(bar)

    # Load sector ETFs
    sector_snapshots = {}
    needed_sectors = set()
    for sym in universe:
        sec = get_sector_etf(sym)
        if sec and sec not in ("SPY", "QQQ"):
            needed_sectors.add(sec)
    for sec_sym in needed_sectors:
        sec_bars = _load_bars(sec_sym)
        if sec_bars:
            eng = MarketEngine()
            snaps = {}
            for bar in sec_bars:
                snaps[bar.timestamp] = eng.process_bar(bar)
            sector_snapshots[sec_sym] = snaps

    if verbose:
        print(f"  SPY: {len(spy_bars)} bars, QQQ: {len(qqq_bars)} bars")
        print(f"  Sectors: {len(sector_snapshots)}")

    # Pre-compute SPY opening drives by date
    spy_opening_drives = {}
    spy_by_date = defaultdict(list)
    for bar in spy_bars:
        spy_by_date[bar.timestamp.date()].append(bar)
    for date, day_bars in spy_by_date.items():
        if not day_bars:
            continue
        day_open = day_bars[0].open
        first_hour_close = day_bars[0].close
        for b in day_bars:
            if b.timestamp.hour < 10 or (b.timestamp.hour == 10 and b.timestamp.minute <= 30):
                first_hour_close = b.close
        if day_open > 0:
            spy_opening_drives[date] = (first_hour_close - day_open) / day_open * 100.0

    # Build SPY bar index for candidate features
    spy_bar_by_ts = {b.timestamp: (i, b) for i, b in enumerate(spy_bars)}

    all_records = []
    symbols_processed = 0

    for sym in universe:
        sym_bars = _load_bars(sym)
        if not sym_bars:
            continue

        sec_etf = get_sector_etf(sym)
        sec_bars_list = _load_bars(sec_etf) if sec_etf and sec_etf in sector_snapshots else None

        result = run_backtest(
            sym_bars, cfg=cfg,
            spy_bars=spy_bars, qqq_bars=qqq_bars,
            sector_bars=sec_bars_list,
        )
        if not result.trades:
            symbols_processed += 1
            continue

        # Build sym bar index for per-stock features
        sym_bar_idx = {b.timestamp: i for i, b in enumerate(sym_bars)}

        for trade in result.trades:
            sig_ts = trade.signal.timestamp
            if sig_ts is None:
                continue

            spy_snap = _find_nearest_snapshot(spy_snapshots, sig_ts)
            qqq_snap = _find_nearest_snapshot(qqq_snapshots, sig_ts)
            if spy_snap is None or qqq_snap is None:
                continue

            sec_snap = None
            if sec_etf and sec_etf in sector_snapshots:
                sec_snap = _find_nearest_snapshot(sector_snapshots[sec_etf], sig_ts)

            stock_pct = NaN
            sym_day_open = _find_day_open(sym_bars, sig_ts)
            if sym_day_open > 0 and not math.isnan(spy_snap.pct_from_open):
                stock_pct = (trade.signal.entry_price - sym_day_open) / sym_day_open * 100.0

            mkt_ctx = compute_market_context(
                spy_snap, qqq_snap,
                sector_snapshot=sec_snap,
                stock_pct_from_open=stock_pct,
            )
            tape = read_tape(mkt_ctx, weights)

            # ── Build audit record ──
            rec = AuditRecord()
            rec.symbol = sym
            rec.signal_ts = sig_ts
            rec.setup_name = SETUP_DISPLAY_NAME.get(trade.signal.setup_id, str(trade.signal.setup_id))
            rec.direction = trade.signal.direction
            rec.pnl_rr = trade.pnl_rr
            rec.exit_reason = trade.exit_reason
            rec.quality = trade.signal.quality_score
            rec.hour = sig_ts.hour

            # Current tape components
            rec.mkt_vwap = tape.mkt_vwap
            rec.mkt_ema = tape.mkt_ema
            rec.mkt_pressure = tape.mkt_pressure
            rec.sec_vwap = tape.sec_vwap
            rec.sec_ema = tape.sec_ema
            rec.rs_market = tape.rs_market
            rec.rs_sector = tape.rs_sector
            rec.tape_score = tape.tape_score
            rec.tape_permission = tape.long_permission if trade.signal.direction == 1 else tape.short_permission

            # SPY raw state
            rec.spy_pct_from_open = spy_snap.pct_from_open if not math.isnan(spy_snap.pct_from_open) else 0
            dh = spy_snap.day_high if not math.isnan(spy_snap.day_high) else 0
            dl = spy_snap.day_low if not math.isnan(spy_snap.day_low) else 0
            if (dh - dl) > 0:
                rec.spy_close_pos = (spy_snap.close - dl) / (dh - dl)
            else:
                rec.spy_close_pos = 0.5

            # ── Candidate features from SPY bars ──
            spy_match = _find_nearest_bar_idx(spy_bars, sig_ts)
            if spy_match is not None:
                rec.trend_persistence = _compute_trend_persistence(spy_bars, spy_match)
                rec.chop_ratio = _compute_chop_ratio(spy_bars, spy_match)
                rec.range_atr_ratio = _compute_range_atr_ratio(spy_bars, spy_match)
                rec.bars_from_open = _bars_from_open(spy_bars, spy_match)

            # Opening drive (pre-computed)
            sig_date = sig_ts.date()
            if sig_date in spy_opening_drives:
                od = spy_opening_drives[sig_date]
                rec.opening_drive = max(-1.0, min(1.0, od / 1.0))

            # ── Candidate features from stock bars ──
            sym_match = _find_nearest_bar_idx(sym_bars, sig_ts)
            if sym_match is not None:
                body_r, uw, lw = _compute_bar_anatomy(sym_bars[sym_match])
                rec.bar_body_ratio = body_r
                rec.bar_upper_wick_pct = uw
                rec.bar_lower_wick_pct = lw
                rec.volume_ratio = _compute_volume_ratio(sym_bars, sym_match)

            # Range position — where stock price sits in its own day range
            sym_day_high = -1e9
            sym_day_low = 1e9
            if sym_match is not None:
                for i in range(sym_match, -1, -1):
                    if sym_bars[i].timestamp.date() != sig_date:
                        break
                    sym_day_high = max(sym_day_high, sym_bars[i].high)
                    sym_day_low = min(sym_day_low, sym_bars[i].low)
                if sym_day_high > sym_day_low:
                    rec.range_position = (trade.signal.entry_price - sym_day_low) / (sym_day_high - sym_day_low)

            # Time × regime interaction bucket
            hour = sig_ts.hour
            if rec.spy_pct_from_open > 0.05:
                regime = "GREEN"
            elif rec.spy_pct_from_open < -0.05:
                regime = "RED"
            else:
                regime = "FLAT"
            if hour < 10:
                time_slot = "AM1"
            elif hour < 11:
                time_slot = "AM2"
            elif hour < 13:
                time_slot = "MID"
            else:
                time_slot = "PM"
            rec.time_regime_bucket = f"{regime}_{time_slot}"

            all_records.append(rec)

        symbols_processed += 1
        if verbose and symbols_processed % 20 == 0:
            print(f"  Processed {symbols_processed}/{len(universe)}, "
                  f"{len(all_records)} trades")

    if verbose:
        print(f"\n  Total: {symbols_processed} symbols, {len(all_records)} trades\n")
    return all_records


def _find_nearest_bar_idx(bars: List[Bar], target_ts: datetime) -> Optional[int]:
    """Find the bar index at or just before target timestamp."""
    best = None
    for i, b in enumerate(bars):
        if b.timestamp <= target_ts:
            best = i
        elif b.timestamp > target_ts:
            break
    return best


# ═══════════════════════════════════════════════════════════════════
#  Analysis Functions
# ═══════════════════════════════════════════════════════════════════

def _stats(records):
    if not records:
        return {"n": 0, "wr": 0, "exp_r": 0, "pf_r": 0, "total_r": 0}
    n = len(records)
    wins = [r for r in records if r.pnl_rr > 0]
    losses = [r for r in records if r.pnl_rr <= 0]
    wr = len(wins) / n * 100
    total_r = sum(r.pnl_rr for r in records)
    exp_r = total_r / n
    gross_w = sum(r.pnl_rr for r in wins)
    gross_l = abs(sum(r.pnl_rr for r in losses))
    pf_r = gross_w / gross_l if gross_l > 0 else float('inf')
    return {"n": n, "wr": round(wr, 1), "exp_r": round(exp_r, 3),
            "pf_r": round(pf_r, 2), "total_r": round(total_r, 2)}


def _pf(v):
    return f"{v:.2f}" if v < 999 else "inf"


def _bucket_analysis(records: List[AuditRecord], feature_name: str,
                     getter, buckets: List[Tuple[str, float, float]],
                     direction_filter: Optional[int] = None):
    """
    Bucket trades by feature value, report stats per bucket.
    buckets: list of (label, low, high) — inclusive on low, exclusive on high.
    Returns list of (label, stats_dict) + effect_size.
    """
    recs = records
    if direction_filter is not None:
        recs = [r for r in records if r.direction == direction_filter]

    results = []
    for label, lo, hi in buckets:
        group = []
        for r in recs:
            val = getter(r)
            if isinstance(val, float) and math.isnan(val):
                continue
            if lo <= val < hi:
                group.append(r)
        results.append((label, _stats(group)))

    # Effect size: exp_r of best bucket - exp_r of worst bucket
    exps = [s["exp_r"] for _, s in results if s["n"] >= 5]
    effect = max(exps) - min(exps) if len(exps) >= 2 else 0
    return results, round(effect, 3)


def _tercile_analysis(records: List[AuditRecord], feature_name: str,
                      getter, direction_filter: Optional[int] = None):
    """
    Auto-split into terciles based on feature values.
    """
    recs = records
    if direction_filter is not None:
        recs = [r for r in records if r.direction == direction_filter]

    # Get all non-NaN values
    vals = []
    for r in recs:
        v = getter(r)
        if isinstance(v, float) and math.isnan(v):
            continue
        vals.append((v, r))
    if len(vals) < 15:
        return [], 0

    vals.sort(key=lambda x: x[0])
    n = len(vals)
    t1 = n // 3
    t2 = 2 * n // 3

    lo_recs = [r for _, r in vals[:t1]]
    mid_recs = [r for _, r in vals[t1:t2]]
    hi_recs = [r for _, r in vals[t2:]]

    lo_bound = vals[t1][0] if t1 < n else 0
    hi_bound = vals[t2][0] if t2 < n else 0

    results = [
        (f"Low (<{lo_bound:.2f})", _stats(lo_recs)),
        (f"Mid", _stats(mid_recs)),
        (f"High (>{hi_bound:.2f})", _stats(hi_recs)),
    ]
    exps = [s["exp_r"] for _, s in results if s["n"] >= 5]
    effect = max(exps) - min(exps) if len(exps) >= 2 else 0
    return results, round(effect, 3)


# ═══════════════════════════════════════════════════════════════════
#  Reporting
# ═══════════════════════════════════════════════════════════════════

def _print_feature_table(title, results, effect, extra_note=""):
    print(f"\n  {title}")
    if extra_note:
        print(f"  {extra_note}")
    print(f"  {'Bucket':<22} {'N':>5} {'WR%':>7} {'Exp(R)':>8} {'PF(R)':>7} {'TotalR':>8}")
    print("  " + "-" * 62)
    for label, s in results:
        if s["n"] > 0:
            marker = ""
            if s["n"] >= 5:
                if s["exp_r"] > 0.2:
                    marker = "  ←EDGE"
                elif s["exp_r"] < -0.15:
                    marker = "  ←DRAIN"
            print(f"  {label:<22} {s['n']:>5} {s['wr']:>6.1f}% {s['exp_r']:>+8.3f} "
                  f"{_pf(s['pf_r']):>7} {s['total_r']:>+8.2f}{marker}")
    print(f"  Effect size: {effect:+.3f}R")


def report_current_features(records: List[AuditRecord]):
    """SECTION 1: Evaluate current 7 tape components as predictive features."""
    print("\n" + "=" * 80)
    print("  SECTION 1: CURRENT REGIME/TAPE COMPONENTS — Predictive Separation")
    print("=" * 80)

    components = [
        ("mkt_vwap",     "Market VWAP State",     lambda r: r.mkt_vwap),
        ("mkt_ema",      "Market EMA Structure",   lambda r: r.mkt_ema),
        ("mkt_pressure", "Market Pressure (%Open)", lambda r: r.mkt_pressure),
        ("sec_vwap",     "Sector VWAP State",      lambda r: r.sec_vwap),
        ("sec_ema",      "Sector EMA Structure",   lambda r: r.sec_ema),
        ("rs_market",    "RS vs Market",           lambda r: r.rs_market),
        ("rs_sector",    "RS vs Sector",           lambda r: r.rs_sector),
        ("tape_score",   "Aggregate Tape Score",   lambda r: r.tape_score),
    ]

    sign_buckets = [
        ("Negative (<0)",  -1.01, 0.0),
        ("Neutral (=0)",   -0.001, 0.001),
        ("Positive (>0)",   0.0, 1.01),
    ]

    graded_buckets = [
        ("Strong Neg (<-0.5)", -1.01, -0.5),
        ("Mild Neg",           -0.5, -0.15),
        ("Neutral",            -0.15, 0.15),
        ("Mild Pos",            0.15, 0.5),
        ("Strong Pos (>0.5)",   0.5, 1.01),
    ]

    for direction, dir_label in [(1, "LONG"), (-1, "SHORT")]:
        print(f"\n  {'─' * 70}")
        print(f"  {dir_label} TRADES")
        print(f"  {'─' * 70}")

        dir_recs = [r for r in records if r.direction == direction]
        overall = _stats(dir_recs)
        print(f"  Baseline: N={overall['n']}, Exp={overall['exp_r']:+.3f}R, "
              f"PF={_pf(overall['pf_r'])}")

        feature_effects = []

        for comp_name, display, getter in components:
            results, effect = _bucket_analysis(
                records, comp_name, getter, graded_buckets,
                direction_filter=direction
            )
            _print_feature_table(f"{display} ({comp_name})", results, effect)
            feature_effects.append((comp_name, display, effect))

        # Summary ranking
        print(f"\n  {'─' * 50}")
        print(f"  {dir_label} FEATURE RANKING (by effect size):")
        print(f"  {'─' * 50}")
        feature_effects.sort(key=lambda x: abs(x[2]), reverse=True)
        for i, (name, display, eff) in enumerate(feature_effects, 1):
            grade = "STRONG" if abs(eff) > 0.3 else "MODERATE" if abs(eff) > 0.15 else "WEAK"
            print(f"  {i}. {display:<30} effect={eff:+.3f}R  [{grade}]")


def report_candidate_features(records: List[AuditRecord]):
    """SECTION 2: Evaluate candidate new features."""
    print("\n" + "=" * 80)
    print("  SECTION 2: CANDIDATE NEW FEATURES — Predictive Separation")
    print("=" * 80)

    candidates = [
        ("Trend Persistence",
         lambda r: r.trend_persistence,
         [("Downtrend (<-0.2)", -1.01, -0.2),
          ("Mild Down",         -0.2, -0.01),
          ("Flat/Mixed",        -0.01, 0.01),
          ("Mild Up",            0.01, 0.2),
          ("Uptrend (>0.2)",     0.2, 1.01)],
         "SPY consecutive directional bars, normalized"),

        ("Chop Ratio (Directional Efficiency)",
         lambda r: r.chop_ratio,
         [("Strong Down (<-0.3)", -1.01, -0.3),
          ("Mild Down",           -0.3, -0.1),
          ("Chop (-0.1 to 0.1)",  -0.1, 0.1),
          ("Mild Up",              0.1, 0.3),
          ("Strong Up (>0.3)",     0.3, 1.01)],
         "|net move| / sum(|moves|) over 12 bars"),

        ("Opening Drive",
         lambda r: r.opening_drive,
         [("Strong Sell (<-0.3)", -1.01, -0.3),
          ("Mild Sell",           -0.3, 0.0),
          ("Mild Buy",             0.0, 0.3),
          ("Strong Buy (>0.3)",    0.3, 1.01)],
         "SPY % move from open to 10:30, capped ±1%"),

        ("Range ATR Ratio (Compression/Expansion)",
         lambda r: r.range_atr_ratio if not isinstance(r.range_atr_ratio, float) or not math.isnan(r.range_atr_ratio) else NaN,
         [("Compressed (<0.6)",   0.0, 0.6),
          ("Normal (0.6-1.0)",    0.6, 1.0),
          ("Mild Expansion",      1.0, 1.5),
          ("Strong Expansion",    1.5, 99.0)],
         "Today range / avg prior day range"),

        ("Signal Bar Body Ratio",
         lambda r: r.bar_body_ratio,
         [("Doji/Rejection (<0.3)",  0.0, 0.3),
          ("Mixed (0.3-0.6)",        0.3, 0.6),
          ("Strong Body (>0.6)",     0.6, 1.01)],
         "|body|/range of signal bar"),

        ("Signal Bar Upper Wick %",
         lambda r: r.bar_upper_wick_pct,
         [("No Wick (<0.1)",     0.0, 0.1),
          ("Small (0.1-0.3)",    0.1, 0.3),
          ("Large (>0.3)",       0.3, 1.01)],
         "Upper wick / range"),

        ("Signal Bar Lower Wick %",
         lambda r: r.bar_lower_wick_pct,
         [("No Wick (<0.1)",     0.0, 0.1),
          ("Small (0.1-0.3)",    0.1, 0.3),
          ("Large (>0.3)",       0.3, 1.01)],
         "Lower wick / range"),

        ("Volume Ratio (vs session avg)",
         lambda r: r.volume_ratio if not isinstance(r.volume_ratio, float) or not math.isnan(r.volume_ratio) else NaN,
         [("Low (<0.7)",       0.0, 0.7),
          ("Normal (0.7-1.3)", 0.7, 1.3),
          ("High (1.3-2.0)",   1.3, 2.0),
          ("Spike (>2.0)",     2.0, 99.0)],
         "Signal bar volume / session avg"),

        ("Range Position (stock in day range)",
         lambda r: r.range_position if not isinstance(r.range_position, float) or not math.isnan(r.range_position) else NaN,
         [("Bottom 25%",     0.0, 0.25),
          ("Lower Mid",      0.25, 0.50),
          ("Upper Mid",      0.50, 0.75),
          ("Top 25%",        0.75, 1.01)],
         "Where entry sits in day's high-low range"),

        ("Bars From Open",
         lambda r: r.bars_from_open,
         [("First 30min (1-6)",   0, 7),
          ("30-60min (7-12)",     7, 13),
          ("60-90min (13-18)",   13, 19),
          ("90min+ (19+)",       19, 999)],
         "Number of 5-min bars since session open"),
    ]

    for direction, dir_label in [(1, "LONG"), (-1, "SHORT")]:
        print(f"\n  {'─' * 70}")
        print(f"  {dir_label} TRADES — Candidate Features")
        print(f"  {'─' * 70}")

        dir_recs = [r for r in records if r.direction == direction]
        overall = _stats(dir_recs)
        print(f"  Baseline: N={overall['n']}, Exp={overall['exp_r']:+.3f}R")

        feature_effects = []

        for name, getter, buckets, desc in candidates:
            results, effect = _bucket_analysis(
                records, name, getter, buckets,
                direction_filter=direction
            )
            has_data = any(s["n"] > 0 for _, s in results)
            if has_data:
                _print_feature_table(name, results, effect, extra_note=desc)
                feature_effects.append((name, effect))

        # Summary
        print(f"\n  {'─' * 50}")
        print(f"  {dir_label} CANDIDATE RANKING (by effect size):")
        print(f"  {'─' * 50}")
        feature_effects.sort(key=lambda x: abs(x[1]), reverse=True)
        for i, (name, eff) in enumerate(feature_effects, 1):
            grade = "STRONG" if abs(eff) > 0.3 else "MODERATE" if abs(eff) > 0.15 else "WEAK"
            print(f"  {i}. {name:<40} effect={eff:+.3f}R  [{grade}]")


def report_time_regime_interaction(records: List[AuditRecord]):
    """SECTION 3: Time-of-day × regime interaction."""
    print("\n" + "=" * 80)
    print("  SECTION 3: TIME × REGIME INTERACTION")
    print("=" * 80)

    for direction, dir_label in [(1, "LONG"), (-1, "SHORT")]:
        dir_recs = [r for r in records if r.direction == direction]
        if not dir_recs:
            continue

        print(f"\n  {dir_label} TRADES:")
        buckets = defaultdict(list)
        for r in dir_recs:
            if r.time_regime_bucket:
                buckets[r.time_regime_bucket].append(r)

        print(f"  {'Bucket':<18} {'N':>5} {'WR%':>7} {'Exp(R)':>8} {'PF(R)':>7} {'TotalR':>8}")
        print("  " + "-" * 60)
        for key in sorted(buckets.keys()):
            s = _stats(buckets[key])
            if s["n"] >= 3:
                marker = ""
                if s["n"] >= 5 and s["exp_r"] > 0.2:
                    marker = "  ←EDGE"
                elif s["n"] >= 5 and s["exp_r"] < -0.15:
                    marker = "  ←DRAIN"
                print(f"  {key:<18} {s['n']:>5} {s['wr']:>6.1f}% {s['exp_r']:>+8.3f} "
                      f"{_pf(s['pf_r']):>7} {s['total_r']:>+8.2f}{marker}")


def report_incremental_value(records: List[AuditRecord]):
    """SECTION 4: Does each candidate add info beyond current tape score?"""
    print("\n" + "=" * 80)
    print("  SECTION 4: INCREMENTAL VALUE — Beyond Current Tape Score")
    print("=" * 80)
    print("  For trades where tape_permission > 0.40 (current long gate),")
    print("  do candidate features further separate winners from losers?")

    # Longs that pass the current gate
    passed = [r for r in records if r.direction == 1 and r.tape_permission >= 0.40]
    baseline = _stats(passed)
    print(f"\n  Passed longs (tape≥0.40): N={baseline['n']}, "
          f"Exp={baseline['exp_r']:+.3f}R, PF={_pf(baseline['pf_r'])}")

    if baseline["n"] < 10:
        print("  Too few trades for incremental analysis.")
        return

    candidates = [
        ("Trend Persistence",    lambda r: r.trend_persistence, 0.0),
        ("Chop Ratio",           lambda r: r.chop_ratio, 0.0),
        ("Opening Drive",        lambda r: r.opening_drive, 0.0),
        ("Bar Body Ratio",       lambda r: r.bar_body_ratio, 0.5),
        ("Volume Ratio",         lambda r: r.volume_ratio, 1.0),
        ("Range Position",       lambda r: r.range_position, 0.5),
    ]

    print(f"\n  {'Feature':<25} {'Above N':>7} {'Above Exp':>9} "
          f"{'Below N':>7} {'Below Exp':>9} {'Delta':>8} {'Grade':>8}")
    print("  " + "-" * 80)

    for name, getter, split_at in candidates:
        above = [r for r in passed
                 if not (isinstance(getter(r), float) and math.isnan(getter(r)))
                 and getter(r) >= split_at]
        below = [r for r in passed
                 if not (isinstance(getter(r), float) and math.isnan(getter(r)))
                 and getter(r) < split_at]
        if len(above) < 3 or len(below) < 3:
            print(f"  {name:<25}  insufficient data")
            continue
        a_exp = sum(r.pnl_rr for r in above) / len(above)
        b_exp = sum(r.pnl_rr for r in below) / len(below)
        delta = a_exp - b_exp
        grade = "STRONG" if abs(delta) > 0.3 else "MODERATE" if abs(delta) > 0.15 else "WEAK"
        print(f"  {name:<25} {len(above):>7} {a_exp:>+9.3f} "
              f"{len(below):>7} {b_exp:>+9.3f} {delta:>+8.3f} {grade:>8}")

    # Same for shorts — trades where RED+TREND is active
    shorts_passed = [r for r in records if r.direction == -1
                     and r.spy_pct_from_open < -0.05
                     and r.spy_close_pos <= 0.25]
    s_baseline = _stats(shorts_passed)
    print(f"\n  Passed shorts (RED+TREND): N={s_baseline['n']}, "
          f"Exp={s_baseline['exp_r']:+.3f}R, PF={_pf(s_baseline['pf_r'])}")

    if s_baseline["n"] >= 10:
        print(f"\n  {'Feature':<25} {'Above N':>7} {'Above Exp':>9} "
              f"{'Below N':>7} {'Below Exp':>9} {'Delta':>8} {'Grade':>8}")
        print("  " + "-" * 80)

        for name, getter, split_at in candidates:
            above = [r for r in shorts_passed
                     if not (isinstance(getter(r), float) and math.isnan(getter(r)))
                     and getter(r) >= split_at]
            below = [r for r in shorts_passed
                     if not (isinstance(getter(r), float) and math.isnan(getter(r)))
                     and getter(r) < split_at]
            if len(above) < 3 or len(below) < 3:
                print(f"  {name:<25}  insufficient data")
                continue
            a_exp = sum(r.pnl_rr for r in above) / len(above)
            b_exp = sum(r.pnl_rr for r in below) / len(below)
            delta = a_exp - b_exp
            grade = "STRONG" if abs(delta) > 0.3 else "MODERATE" if abs(delta) > 0.15 else "WEAK"
            print(f"  {name:<25} {len(above):>7} {a_exp:>+9.3f} "
                  f"{len(below):>7} {b_exp:>+9.3f} {delta:>+8.3f} {grade:>8}")


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════

def main():
    print("=" * 80)
    print("  FEATURE AUDIT — Regime/Tape Ingredient Quality Assessment")
    print("  Goal: evaluate current components + test candidate new features")
    print("  Does NOT optimize weights or change frozen system")
    print("=" * 80)

    records = run_feature_audit(verbose=True)
    if not records:
        print("\n  No trades. Check data directory.")
        return

    longs = [r for r in records if r.direction == 1]
    shorts = [r for r in records if r.direction == -1]
    print(f"  Total: {len(records)} trades ({len(longs)} long, {len(shorts)} short)")

    # Run all 4 sections
    report_current_features(records)
    report_candidate_features(records)
    report_time_regime_interaction(records)
    report_incremental_value(records)

    print("\n" + "=" * 80)
    print("  AUDIT COMPLETE")
    print("  Next: review effect sizes to determine if ingredients need upgrading")
    print("  before more calibration work.")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
