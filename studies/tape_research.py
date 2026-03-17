"""
Tape Model Research Harness — RESEARCH ONLY, does not touch frozen system.

Replays all historical trades and computes a TapeReading at signal time for each.
Outputs analysis of how tape components correlate with trade outcomes (R-multiples).

Key questions this answers:
  - When is a long setup tradable, even on a mildly red day?
  - When is a short setup tradable, even on a mildly green day?
  - Which tape components best predict tradable conditions?
  - What permission thresholds maximize edge?

Usage:
    python -m alert_overlay.tape_research
    python -m alert_overlay.tape_research --universe all94
    python -m alert_overlay.tape_research --weights-sweep
"""

import argparse
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import Bar, Signal, SetupId, SetupFamily, NaN, SETUP_DISPLAY_NAME, SETUP_FAMILY_MAP
from ..market_context import (
    MarketEngine, MarketContext, MarketSnapshot,
    compute_market_context, get_sector_etf, SECTOR_MAP,
)
from ..tape_model import (
    TapeReading, TapeWeights, read_tape, classify_tape_zone,
    permission_for_direction,
)

DATA_DIR = Path(__file__).parent.parent / "data"


# ═══════════════════════════════════════════════════════════════════
#  Data Loading
# ═══════════════════════════════════════════════════════════════════

def _load_bars(symbol: str) -> Optional[List[Bar]]:
    """Load 5-min bars from CSV for a symbol."""
    path = DATA_DIR / f"{symbol}_5min.csv"
    if not path.exists():
        return None
    return load_bars_from_csv(str(path))


def _get_universe(mode: str) -> List[str]:
    """Get symbol list based on mode."""
    watchlist = Path(__file__).parent.parent / "watchlist.txt"
    if mode == "watchlist" and watchlist.exists():
        syms = []
        with open(watchlist) as f:
            for line in f:
                s = line.strip().upper()
                if s and not s.startswith("#") and s not in ("SPY", "QQQ"):
                    syms.append(s)
        return syms

    # Default: all symbols that have data files
    syms = []
    for p in sorted(DATA_DIR.glob("*_5min.csv")):
        sym = p.stem.replace("_5min", "")
        if sym not in ("SPY", "QQQ") and not sym.startswith("XL"):
            syms.append(sym)
    return syms


# ═══════════════════════════════════════════════════════════════════
#  Per-Trade Tape Snapshot
# ═══════════════════════════════════════════════════════════════════

class TradeTapeRecord:
    """A completed trade annotated with its tape state at signal time."""
    __slots__ = [
        "symbol", "signal_ts", "setup_name", "direction",
        "entry_price", "stop_price", "target_price",
        "pnl_rr", "pnl_points", "exit_reason", "quality",
        "tape", "tape_zone", "permission",
        # Old regime for comparison
        "old_regime",
    ]

    def __init__(self, trade: Trade, tape: TapeReading,
                 spy_snap: MarketSnapshot, symbol: str):
        sig = trade.signal
        self.symbol = symbol
        self.signal_ts = sig.timestamp
        self.setup_name = SETUP_DISPLAY_NAME.get(sig.setup_id, str(sig.setup_id))
        self.direction = sig.direction
        self.entry_price = sig.entry_price
        self.stop_price = sig.stop_price
        self.target_price = sig.target_price
        self.pnl_rr = trade.pnl_rr
        self.pnl_points = trade.pnl_points
        self.exit_reason = trade.exit_reason
        self.quality = sig.quality_score
        self.tape = tape
        self.tape_zone = classify_tape_zone(tape.tape_score)
        self.permission = permission_for_direction(tape, sig.direction)

        # Compute old coarse regime for comparison
        self.old_regime = _old_regime_label(spy_snap)


def _old_regime_label(spy_snap: MarketSnapshot) -> str:
    """Replicate the frozen system's coarse regime classification."""
    if not spy_snap.ready or math.isnan(spy_snap.pct_from_open):
        return "UNKNOWN"
    pfo = spy_snap.pct_from_open
    if pfo > 0.05:
        direction = "GREEN"
    elif pfo < -0.05:
        direction = "RED"
    else:
        direction = "FLAT"

    dh = spy_snap.day_high if not math.isnan(spy_snap.day_high) else 0
    dl = spy_snap.day_low if not math.isnan(spy_snap.day_low) else 0
    day_range = dh - dl
    if day_range > 0:
        close_pos = (spy_snap.close - dl) / day_range
        if direction == "RED":
            character = "TREND" if close_pos <= 0.25 else "CHOPPY"
        else:
            character = "TREND" if close_pos >= 0.75 else "CHOPPY"
    else:
        character = "CHOPPY"

    return f"{direction}+{character}"


# ═══════════════════════════════════════════════════════════════════
#  Main Research Engine
# ═══════════════════════════════════════════════════════════════════

def run_tape_research(
    universe: List[str],
    weights: Optional[TapeWeights] = None,
    cfg: Optional[OverlayConfig] = None,
    verbose: bool = True,
) -> List[TradeTapeRecord]:
    """
    Replay backtests for all symbols, compute TapeReading at each signal,
    and return annotated trade records.
    """
    if cfg is None:
        cfg = OverlayConfig()
        # Unlock all setups for research (don't apply frozen filters)
        cfg.use_market_context = True

    if weights is None:
        weights = TapeWeights()

    # Load market data
    spy_bars = _load_bars("SPY")
    qqq_bars = _load_bars("QQQ")
    if not spy_bars or not qqq_bars:
        print("ERROR: SPY or QQQ data missing.")
        return []

    # Build SPY/QQQ engines for snapshot lookup
    spy_engine = MarketEngine()
    qqq_engine = MarketEngine()
    spy_snapshots: Dict[datetime, MarketSnapshot] = {}
    qqq_snapshots: Dict[datetime, MarketSnapshot] = {}

    for bar in spy_bars:
        snap = spy_engine.process_bar(bar)
        spy_snapshots[bar.timestamp] = snap
    for bar in qqq_bars:
        snap = qqq_engine.process_bar(bar)
        qqq_snapshots[bar.timestamp] = snap

    # Load all sector ETF data
    sector_engines: Dict[str, MarketEngine] = {}
    sector_snapshots: Dict[str, Dict[datetime, MarketSnapshot]] = {}
    needed_sectors = set()
    for sym in universe:
        sec = get_sector_etf(sym)
        if sec and sec not in ("SPY", "QQQ") and sec not in needed_sectors:
            needed_sectors.add(sec)

    for sec_sym in needed_sectors:
        sec_bars = _load_bars(sec_sym)
        if sec_bars:
            eng = MarketEngine()
            snaps = {}
            for bar in sec_bars:
                snaps[bar.timestamp] = eng.process_bar(bar)
            sector_engines[sec_sym] = eng
            sector_snapshots[sec_sym] = snaps
            if verbose:
                print(f"  Loaded sector {sec_sym}: {len(sec_bars)} bars")

    if verbose:
        print(f"\n  SPY: {len(spy_bars)} bars, QQQ: {len(qqq_bars)} bars")
        print(f"  Sectors loaded: {len(sector_snapshots)}")
        print(f"  Universe: {len(universe)} symbols\n")

    # Process each symbol
    all_records: List[TradeTapeRecord] = []
    symbols_processed = 0
    symbols_skipped = 0

    for sym in universe:
        sym_bars = _load_bars(sym)
        if not sym_bars:
            symbols_skipped += 1
            continue

        # Get sector for this symbol
        sec_etf = get_sector_etf(sym)
        sec_bars_list = None
        if sec_etf and sec_etf in sector_snapshots:
            sec_path = DATA_DIR / f"{sec_etf}_5min.csv"
            if sec_path.exists():
                sec_bars_list = _load_bars(sec_etf)

        # Run backtest with full market context
        result = run_backtest(
            sym_bars, cfg=cfg,
            spy_bars=spy_bars, qqq_bars=qqq_bars,
            sector_bars=sec_bars_list,
        )

        if not result.trades:
            symbols_processed += 1
            continue

        # For each trade, find the closest SPY/QQQ/sector snapshot
        # and compute TapeReading
        for trade in result.trades:
            sig_ts = trade.signal.timestamp
            if sig_ts is None:
                continue

            # Find closest market snapshots (exact or nearest prior)
            spy_snap = _find_nearest_snapshot(spy_snapshots, sig_ts)
            qqq_snap = _find_nearest_snapshot(qqq_snapshots, sig_ts)

            if spy_snap is None or qqq_snap is None:
                continue

            # Sector snapshot
            sec_snap = None
            if sec_etf and sec_etf in sector_snapshots:
                sec_snap = _find_nearest_snapshot(
                    sector_snapshots[sec_etf], sig_ts
                )

            # Compute stock RS
            stock_pct = NaN
            if not math.isnan(spy_snap.pct_from_open):
                # Approximate stock pct from open using entry price
                # (signal fires at bar close, so entry ≈ close)
                sym_day_open = _find_day_open(sym_bars, sig_ts)
                if sym_day_open > 0:
                    stock_pct = (trade.signal.entry_price - sym_day_open) / sym_day_open * 100.0

            # Build MarketContext
            mkt_ctx = compute_market_context(
                spy_snap, qqq_snap,
                sector_snapshot=sec_snap,
                stock_pct_from_open=stock_pct,
            )

            # Compute tape reading
            tape = read_tape(mkt_ctx, weights)

            # Build annotated record
            record = TradeTapeRecord(trade, tape, spy_snap, sym)
            all_records.append(record)

        symbols_processed += 1
        if verbose and symbols_processed % 10 == 0:
            print(f"  Processed {symbols_processed}/{len(universe)} "
                  f"symbols, {len(all_records)} trades so far")

    if verbose:
        print(f"\n  Total: {symbols_processed} symbols processed, "
              f"{symbols_skipped} skipped, {len(all_records)} trades annotated\n")

    return all_records


def _find_nearest_snapshot(
    snapshots: Dict[datetime, MarketSnapshot],
    target_ts: datetime,
) -> Optional[MarketSnapshot]:
    """Find the snapshot at or just before the target timestamp."""
    if target_ts in snapshots:
        return snapshots[target_ts]
    # Search backward up to 10 minutes
    for delta_min in range(1, 11):
        candidate = target_ts - timedelta(minutes=delta_min)
        if candidate in snapshots:
            return snapshots[candidate]
    return None


def _find_day_open(bars: List[Bar], target_ts: datetime) -> float:
    """Find the day open price for a given timestamp."""
    target_date = target_ts.date()
    for bar in bars:
        if bar.timestamp.date() == target_date:
            return bar.open
    return 0.0


# ═══════════════════════════════════════════════════════════════════
#  Analysis & Reporting
# ═══════════════════════════════════════════════════════════════════

def _stats(records: List[TradeTapeRecord]) -> dict:
    """Compute summary stats for a group of trade records."""
    if not records:
        return {"n": 0, "wr": 0, "exp_r": 0, "pf_r": 0, "total_r": 0}
    n = len(records)
    wins = [r for r in records if r.pnl_rr > 0]
    losses = [r for r in records if r.pnl_rr <= 0]
    wr = len(wins) / n * 100 if n else 0
    total_r = sum(r.pnl_rr for r in records)
    exp_r = total_r / n if n else 0
    gross_w = sum(r.pnl_rr for r in wins)
    gross_l = abs(sum(r.pnl_rr for r in losses))
    pf_r = gross_w / gross_l if gross_l > 0 else float('inf')
    return {"n": n, "wr": round(wr, 1), "exp_r": round(exp_r, 3),
            "pf_r": round(pf_r, 2), "total_r": round(total_r, 2)}


def _pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


def print_tape_zone_analysis(records: List[TradeTapeRecord]):
    """Break down trade performance by tape zone."""
    print("\n" + "=" * 80)
    print("  TAPE ZONE ANALYSIS — Performance by Directional Tape State")
    print("=" * 80)

    zones = ["STRONG_BULL", "MILD_BULL", "NEUTRAL", "MILD_BEAR", "STRONG_BEAR"]

    # ── All trades ──
    print("\n  ALL TRADES:")
    print(f"  {'Zone':<16} {'N':>5} {'WR%':>7} {'Exp(R)':>8} {'PF(R)':>7} {'TotalR':>8}")
    print("  " + "-" * 55)
    for zone in zones:
        group = [r for r in records if r.tape_zone == zone]
        s = _stats(group)
        if s["n"] > 0:
            print(f"  {zone:<16} {s['n']:>5} {s['wr']:>6.1f}% {s['exp_r']:>+8.3f} "
                  f"{_pf_str(s['pf_r']):>7} {s['total_r']:>+8.2f}")

    # ── Longs only ──
    longs = [r for r in records if r.direction == 1]
    print(f"\n  LONG TRADES ONLY (N={len(longs)}):")
    print(f"  {'Zone':<16} {'N':>5} {'WR%':>7} {'Exp(R)':>8} {'PF(R)':>7} {'TotalR':>8}")
    print("  " + "-" * 55)
    for zone in zones:
        group = [r for r in longs if r.tape_zone == zone]
        s = _stats(group)
        if s["n"] > 0:
            print(f"  {zone:<16} {s['n']:>5} {s['wr']:>6.1f}% {s['exp_r']:>+8.3f} "
                  f"{_pf_str(s['pf_r']):>7} {s['total_r']:>+8.2f}")

    # ── Shorts only ──
    shorts = [r for r in records if r.direction == -1]
    if shorts:
        print(f"\n  SHORT TRADES ONLY (N={len(shorts)}):")
        print(f"  {'Zone':<16} {'N':>5} {'WR%':>7} {'Exp(R)':>8} {'PF(R)':>7} {'TotalR':>8}")
        print("  " + "-" * 55)
        for zone in zones:
            group = [r for r in shorts if r.tape_zone == zone]
            s = _stats(group)
            if s["n"] > 0:
                print(f"  {zone:<16} {s['n']:>5} {s['wr']:>6.1f}% {s['exp_r']:>+8.3f} "
                      f"{_pf_str(s['pf_r']):>7} {s['total_r']:>+8.2f}")


def print_permission_threshold_scan(records: List[TradeTapeRecord]):
    """Scan permission thresholds to find optimal cutoffs."""
    print("\n" + "=" * 80)
    print("  PERMISSION THRESHOLD SCAN — Find Optimal Cutoffs")
    print("=" * 80)

    thresholds = [-0.6, -0.4, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5]

    # ── Long permission scan ──
    longs = [r for r in records if r.direction == 1]
    print(f"\n  LONG PERMISSION SCAN (N_total={len(longs)}):")
    print(f"  {'Threshold':>10} {'Trades':>7} {'WR%':>7} {'Exp(R)':>8} {'PF(R)':>7} "
          f"{'TotalR':>8} {'Kept%':>6}")
    print("  " + "-" * 65)
    for thresh in thresholds:
        group = [r for r in longs if r.permission >= thresh]
        s = _stats(group)
        kept_pct = len(group) / len(longs) * 100 if longs else 0
        if s["n"] > 0:
            print(f"  {thresh:>+10.2f} {s['n']:>7} {s['wr']:>6.1f}% {s['exp_r']:>+8.3f} "
                  f"{_pf_str(s['pf_r']):>7} {s['total_r']:>+8.2f} {kept_pct:>5.0f}%")

    # ── Short permission scan ──
    shorts = [r for r in records if r.direction == -1]
    if shorts:
        print(f"\n  SHORT PERMISSION SCAN (N_total={len(shorts)}):")
        print(f"  {'Threshold':>10} {'Trades':>7} {'WR%':>7} {'Exp(R)':>8} {'PF(R)':>7} "
              f"{'TotalR':>8} {'Kept%':>6}")
        print("  " + "-" * 65)
        for thresh in thresholds:
            group = [r for r in shorts if r.permission >= thresh]
            s = _stats(group)
            kept_pct = len(group) / len(shorts) * 100 if shorts else 0
            if s["n"] > 0:
                print(f"  {thresh:>+10.2f} {s['n']:>7} {s['wr']:>6.1f}% {s['exp_r']:>+8.3f} "
                      f"{_pf_str(s['pf_r']):>7} {s['total_r']:>+8.2f} {kept_pct:>5.0f}%")


def print_component_correlation(records: List[TradeTapeRecord]):
    """Show which tape components correlate most with R outcomes."""
    print("\n" + "=" * 80)
    print("  COMPONENT CONTRIBUTION — Which Tape Factors Predict R?")
    print("=" * 80)

    components = ["mkt_vwap", "mkt_ema", "mkt_pressure",
                  "sec_vwap", "sec_ema", "rs_market", "rs_sector"]

    def _split_analysis(label: str, recs: List[TradeTapeRecord]):
        if len(recs) < 10:
            return
        print(f"\n  {label} (N={len(recs)}):")
        print(f"  {'Component':<16} {'Pos N':>6} {'Pos Exp(R)':>10} "
              f"{'Neg N':>6} {'Neg Exp(R)':>10} {'Delta':>8}")
        print("  " + "-" * 65)

        for comp in components:
            pos = [r for r in recs if getattr(r.tape, comp) > 0]
            neg = [r for r in recs if getattr(r.tape, comp) < 0]
            neut = [r for r in recs if getattr(r.tape, comp) == 0]

            if not pos or not neg:
                continue
            pos_exp = sum(r.pnl_rr for r in pos) / len(pos)
            neg_exp = sum(r.pnl_rr for r in neg) / len(neg)
            delta = pos_exp - neg_exp

            print(f"  {comp:<16} {len(pos):>6} {pos_exp:>+10.3f} "
                  f"{len(neg):>6} {neg_exp:>+10.3f} {delta:>+8.3f}")

    longs = [r for r in records if r.direction == 1]
    shorts = [r for r in records if r.direction == -1]

    _split_analysis("LONG TRADES", longs)
    if shorts:
        _split_analysis("SHORT TRADES", shorts)


def print_old_vs_new_comparison(records: List[TradeTapeRecord]):
    """Compare the old coarse regime model with the new tape model."""
    print("\n" + "=" * 80)
    print("  OLD REGIME vs NEW TAPE MODEL — Side-by-Side Comparison")
    print("=" * 80)

    old_regimes = sorted(set(r.old_regime for r in records))
    tape_zones = ["STRONG_BULL", "MILD_BULL", "NEUTRAL", "MILD_BEAR", "STRONG_BEAR"]

    # ── Old model: performance by coarse regime ──
    longs = [r for r in records if r.direction == 1]
    print(f"\n  OLD MODEL — Long Trades by Coarse Regime:")
    print(f"  {'Regime':<18} {'N':>5} {'WR%':>7} {'Exp(R)':>8} {'PF(R)':>7}")
    print("  " + "-" * 50)
    for regime in old_regimes:
        group = [r for r in longs if r.old_regime == regime]
        s = _stats(group)
        if s["n"] > 0:
            print(f"  {regime:<18} {s['n']:>5} {s['wr']:>6.1f}% "
                  f"{s['exp_r']:>+8.3f} {_pf_str(s['pf_r']):>7}")

    print(f"\n  NEW TAPE MODEL — Long Trades by Tape Zone:")
    print(f"  {'Zone':<18} {'N':>5} {'WR%':>7} {'Exp(R)':>8} {'PF(R)':>7}")
    print("  " + "-" * 50)
    for zone in tape_zones:
        group = [r for r in longs if r.tape_zone == zone]
        s = _stats(group)
        if s["n"] > 0:
            print(f"  {zone:<18} {s['n']:>5} {s['wr']:>6.1f}% "
                  f"{s['exp_r']:>+8.3f} {_pf_str(s['pf_r']):>7}")

    # ── Key question: Longs on RED days that the tape model says are OK ──
    red_longs = [r for r in longs if r.old_regime.startswith("RED")]
    if red_longs:
        print(f"\n  KEY INSIGHT: Long Trades on RED Days (N={len(red_longs)})")
        print(f"  {'Filter':<30} {'N':>5} {'WR%':>7} {'Exp(R)':>8} {'PF(R)':>7}")
        print("  " + "-" * 60)

        # All RED longs (old model blocks these)
        s = _stats(red_longs)
        print(f"  {'All RED longs (blocked)':<30} {s['n']:>5} {s['wr']:>6.1f}% "
              f"{s['exp_r']:>+8.3f} {_pf_str(s['pf_r']):>7}")

        # RED longs with positive tape permission
        tape_ok = [r for r in red_longs if r.permission > 0]
        s = _stats(tape_ok)
        if s["n"] > 0:
            print(f"  {'RED + tape_perm > 0':<30} {s['n']:>5} {s['wr']:>6.1f}% "
                  f"{s['exp_r']:>+8.3f} {_pf_str(s['pf_r']):>7}")

        # RED longs with strong RS
        rs_strong = [r for r in red_longs if r.tape.rs_market > 0.3]
        s = _stats(rs_strong)
        if s["n"] > 0:
            print(f"  {'RED + strong RS (>0.3)':<30} {s['n']:>5} {s['wr']:>6.1f}% "
                  f"{s['exp_r']:>+8.3f} {_pf_str(s['pf_r']):>7}")

    # ── Key question: Shorts on GREEN days ──
    shorts = [r for r in records if r.direction == -1]
    green_shorts = [r for r in shorts if r.old_regime.startswith("GREEN")]
    if green_shorts:
        print(f"\n  KEY INSIGHT: Short Trades on GREEN Days (N={len(green_shorts)})")
        print(f"  {'Filter':<30} {'N':>5} {'WR%':>7} {'Exp(R)':>8} {'PF(R)':>7}")
        print("  " + "-" * 60)

        s = _stats(green_shorts)
        print(f"  {'All GREEN shorts (blocked)':<30} {s['n']:>5} {s['wr']:>6.1f}% "
              f"{s['exp_r']:>+8.3f} {_pf_str(s['pf_r']):>7}")

        tape_ok = [r for r in green_shorts if r.permission > 0]
        s = _stats(tape_ok)
        if s["n"] > 0:
            print(f"  {'GREEN + tape_perm > 0':<30} {s['n']:>5} {s['wr']:>6.1f}% "
                  f"{s['exp_r']:>+8.3f} {_pf_str(s['pf_r']):>7}")

        rw_strong = [r for r in green_shorts if r.tape.rs_market < -0.3]
        s = _stats(rw_strong)
        if s["n"] > 0:
            print(f"  {'GREEN + strong RW (<-0.3)':<30} {s['n']:>5} {s['wr']:>6.1f}% "
                  f"{s['exp_r']:>+8.3f} {_pf_str(s['pf_r']):>7}")


def print_setup_by_tape(records: List[TradeTapeRecord]):
    """Show each setup's performance across tape zones."""
    print("\n" + "=" * 80)
    print("  PER-SETUP TAPE ANALYSIS — Which Setups Work in Which Tape?")
    print("=" * 80)

    zones = ["STRONG_BULL", "MILD_BULL", "NEUTRAL", "MILD_BEAR", "STRONG_BEAR"]

    # Group by setup
    by_setup = defaultdict(list)
    for r in records:
        by_setup[r.setup_name].append(r)

    for setup_name in sorted(by_setup.keys()):
        recs = by_setup[setup_name]
        if len(recs) < 5:
            continue
        overall = _stats(recs)
        dir_label = "LONG" if recs[0].direction == 1 else "SHORT"
        print(f"\n  {setup_name} ({dir_label}, N={overall['n']}, "
              f"Exp={overall['exp_r']:+.3f}, PF={_pf_str(overall['pf_r'])}):")
        print(f"  {'Zone':<16} {'N':>5} {'WR%':>7} {'Exp(R)':>8} {'PF(R)':>7}")
        print("  " + "-" * 50)
        for zone in zones:
            group = [r for r in recs if r.tape_zone == zone]
            s = _stats(group)
            if s["n"] > 0:
                marker = " ***" if s["exp_r"] > 0.1 and s["n"] >= 5 else ""
                marker = " !!!" if s["exp_r"] < -0.2 and s["n"] >= 5 else marker
                print(f"  {zone:<16} {s['n']:>5} {s['wr']:>6.1f}% "
                      f"{s['exp_r']:>+8.3f} {_pf_str(s['pf_r']):>7}{marker}")


# ═══════════════════════════════════════════════════════════════════
#  CLI Entry Point
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Tape Model Research")
    parser.add_argument("--universe", default="watchlist",
                        help="'watchlist' or 'all' (all data files)")
    parser.add_argument("--weights-sweep", action="store_true",
                        help="Run a sweep across weight configurations")
    args = parser.parse_args()

    universe = _get_universe(args.universe)
    print(f"{'=' * 80}")
    print(f"  TAPE MODEL RESEARCH — Directional Permission Framework")
    print(f"  Universe: {len(universe)} symbols")
    print(f"{'=' * 80}")

    weights = TapeWeights()
    print(f"\n  Default weights: {weights.as_dict()}")

    records = run_tape_research(universe, weights=weights)

    if not records:
        print("\n  No trades found. Check data directory.")
        return

    # ── Summary ──
    total = _stats(records)
    print(f"\n  TOTAL TRADES: {total['n']}")
    print(f"  Overall: WR={total['wr']}%, Exp(R)={total['exp_r']:+.3f}, "
          f"PF(R)={_pf_str(total['pf_r'])}")

    # ── Run all analyses ──
    print_tape_zone_analysis(records)
    print_permission_threshold_scan(records)
    print_component_correlation(records)
    print_old_vs_new_comparison(records)
    print_setup_by_tape(records)

    # ── Weight sweep (optional) ──
    if args.weights_sweep:
        print("\n" + "=" * 80)
        print("  WEIGHT SWEEP — Testing Alternative Weight Configurations")
        print("=" * 80)

        longs = [r for r in records if r.direction == 1]

        sweep_configs = [
            ("Default",           TapeWeights()),
            ("Market-heavy",      TapeWeights(mkt_vwap=1.5, mkt_ema=1.2, mkt_pressure=1.0,
                                              sec_vwap=0.2, sec_ema=0.2, rs_market=0.3, rs_sector=0.1)),
            ("RS-heavy",          TapeWeights(mkt_vwap=0.5, mkt_ema=0.4, mkt_pressure=0.3,
                                              sec_vwap=0.3, sec_ema=0.2, rs_market=1.5, rs_sector=0.8)),
            ("Sector-heavy",      TapeWeights(mkt_vwap=0.6, mkt_ema=0.5, mkt_pressure=0.4,
                                              sec_vwap=1.2, sec_ema=1.0, rs_market=0.5, rs_sector=0.5)),
            ("VWAP-only",         TapeWeights(mkt_vwap=1.0, mkt_ema=0.0, mkt_pressure=0.0,
                                              sec_vwap=0.6, sec_ema=0.0, rs_market=0.5, rs_sector=0.3)),
            ("EMA-only",          TapeWeights(mkt_vwap=0.0, mkt_ema=1.0, mkt_pressure=0.5,
                                              sec_vwap=0.0, sec_ema=0.6, rs_market=0.5, rs_sector=0.3)),
        ]

        print(f"\n  Testing with permission threshold > 0.0 on LONG trades (N={len(longs)}):")
        print(f"  {'Config':<20} {'Kept':>5} {'WR%':>7} {'Exp(R)':>8} {'PF(R)':>7} {'TotalR':>8}")
        print("  " + "-" * 60)

        for name, w in sweep_configs:
            # Recompute tape scores with new weights
            for r in longs:
                # We need the MarketContext to recompute — but we don't store it
                # So for the sweep, we re-score from stored components
                tape = r.tape
                # Weighted sum from stored component scores
                total_w = 0.0
                wsum = 0.0
                for comp, wt in [("mkt_vwap", w.mkt_vwap), ("mkt_ema", w.mkt_ema),
                                 ("mkt_pressure", w.mkt_pressure), ("sec_vwap", w.sec_vwap),
                                 ("sec_ema", w.sec_ema), ("rs_market", w.rs_market),
                                 ("rs_sector", w.rs_sector)]:
                    val = getattr(tape, comp)
                    if wt > 0 and val != 0:
                        wsum += val * wt
                        total_w += wt
                    elif wt > 0:
                        total_w += wt
                new_score = wsum / total_w if total_w > 0 else 0
                r._sweep_perm = new_score  # Temp attribute

            kept = [r for r in longs if r._sweep_perm > 0]
            s = _stats(kept)
            print(f"  {name:<20} {s['n']:>5} {s['wr']:>6.1f}% {s['exp_r']:>+8.3f} "
                  f"{_pf_str(s['pf_r']):>7} {s['total_r']:>+8.2f}")

    print(f"\n{'=' * 80}")
    print(f"  RESEARCH COMPLETE")
    print(f"{'=' * 80}\n")


if __name__ == "__main__":
    main()
