"""
Execution-Degradation Attribution Study

Matches each 5-min idealized trade with its 1-min auto-execution counterpart
and decomposes the PnL gap into:

  1. Entry fill degradation — worse fill price vs 5-min assumed close
  2. Stop-timing degradation — 1-min path reveals stop hit earlier/later
  3. Exit-path degradation — different exit reason or timing on 1-min path
  4. Same-bar ambiguity — 5-min bar had stop+target on same bar (assumes stop),
     but 1-min path resolves the actual order
  5. Signal timing effects — trade killed before fill, or timing edge effects

Reports per-setup (VK, SC) and combined, with:
  - avg entry / exit degradation
  - % trades flipped winner→loser
  - % worsened by stop timing
  - % worsened by entry slippage
  - 10 worst examples with timestamps

Also documents the exact execution model.

Usage:
    python -m alert_overlay.exec_attribution
"""

import argparse
import math
import statistics
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Tuple
from zoneinfo import ZoneInfo

from .backtest import load_bars_from_csv, run_backtest, Trade, _compute_dynamic_slippage
from .config import OverlayConfig
from .models import (
    Bar, Signal, NaN, SetupId, SetupFamily,
    SETUP_FAMILY_MAP, SETUP_DISPLAY_NAME,
)
from .exec_sim_1min import (
    ExecTrade, simulate_1min_execution, extract_5min_signals,
    compute_metrics, pf_str,
    ONEMIN_DIR, DATA_DIR, EASTERN,
)
from .market_context import SECTOR_MAP, get_sector_etf

# ── Attribution dataclass ────────────────────────────────────────

@dataclass
class TradeAttribution:
    """Per-trade attribution of degradation from 5-min idealized to 1-min execution."""
    symbol: str
    signal_ts: datetime
    setup_name: str
    direction: int

    # 5-min idealized
    entry_5min: float = 0.0
    exit_5min: float = 0.0
    exit_reason_5min: str = ""
    pnl_5min: float = 0.0
    is_winner_5min: bool = False

    # 1-min execution
    entry_1min: float = 0.0
    exit_1min: float = 0.0
    exit_reason_1min: str = ""
    pnl_1min: float = 0.0
    is_winner_1min: bool = False

    # Decomposition (all in points, positive = degradation / cost)
    entry_degradation: float = 0.0    # adverse entry fill difference
    exit_degradation: float = 0.0     # adverse exit price difference
    total_degradation: float = 0.0    # = pnl_5min - pnl_1min

    # Classification flags
    flipped_to_loser: bool = False          # was winner on 5min, loser on 1min
    exit_reason_changed: bool = False       # different exit reason
    worsened_by_stop_timing: bool = False   # stop hit on 1min that wasn't on 5min, or earlier
    worsened_by_entry: bool = False         # entry degradation > 50% of total degradation
    same_bar_ambiguity: bool = False        # 5min exit was stop on same bar as target
    killed_before_fill: bool = False        # 1min trade doesn't exist (pre-fill stop)

    # For killed trades
    would_have_been_winner: bool = False    # 5min trade was a winner but got killed


def build_attribution(
    baseline_5min: List[Tuple[str, Trade]],
    sim_1min: List[Tuple[str, ExecTrade]],
    killed_signals: List[Tuple[str, Signal, Trade]],  # (sym, signal, 5min_trade)
) -> List[TradeAttribution]:
    """Match 5-min and 1-min trades by signal timestamp and compute attribution."""

    # Index 1-min trades by (symbol, signal_timestamp)
    sim_map = {}
    for sym, et in sim_1min:
        key = (sym, et.signal.timestamp)
        sim_map[key] = et

    results = []

    for sym, bt in baseline_5min:
        sig = bt.signal
        key = (sym, sig.timestamp)
        setup_name = SETUP_DISPLAY_NAME.get(sig.setup_id, str(sig.setup_id))

        if key in sim_map:
            et = sim_map[key]
            attr = TradeAttribution(
                symbol=sym,
                signal_ts=sig.timestamp,
                setup_name=setup_name,
                direction=sig.direction,
                entry_5min=bt.signal.entry_price,
                exit_5min=bt.exit_price,
                exit_reason_5min=bt.exit_reason,
                pnl_5min=bt.pnl_points,
                is_winner_5min=bt.pnl_points > 0,
                entry_1min=et.filled_entry,
                exit_1min=et.exit_price,
                exit_reason_1min=et.exit_reason,
                pnl_1min=et.pnl_points,
                is_winner_1min=et.pnl_points > 0,
            )

            # Entry degradation: how much worse is the 1-min fill?
            # For longs: higher fill = worse. For shorts: lower fill = worse.
            # Convention: positive = degradation (cost to us)
            attr.entry_degradation = (et.filled_entry - sig.entry_price) * sig.direction

            # Exit degradation: how much worse is the 1-min exit?
            # For longs: lower exit = worse. For shorts: higher exit = worse.
            attr.exit_degradation = (bt.exit_price - et.exit_price) * sig.direction

            # Total
            attr.total_degradation = bt.pnl_points - et.pnl_points

            # Flags
            attr.flipped_to_loser = bt.pnl_points > 0 and et.pnl_points <= 0
            attr.exit_reason_changed = bt.exit_reason != et.exit_reason

            # Stop timing: 1min hit stop but 5min didn't, OR exit reason changed to stop
            attr.worsened_by_stop_timing = (
                (et.exit_reason == "stop" and bt.exit_reason != "stop") or
                (et.exit_reason == "stop" and bt.exit_reason == "stop" and et.pnl_points < bt.pnl_points)
            )

            # Entry-dominated: entry degradation is > 50% of total
            if attr.total_degradation > 0:
                attr.worsened_by_entry = attr.entry_degradation > 0.5 * attr.total_degradation

            # Same-bar ambiguity: 5min stopped but 1min resolved differently
            # We detect this when 5min exit was "stop" and 1min exit is different
            attr.same_bar_ambiguity = (
                bt.exit_reason == "stop" and et.exit_reason != "stop"
            )

            results.append(attr)
        else:
            # Trade was killed (stopped before 1-min fill)
            attr = TradeAttribution(
                symbol=sym,
                signal_ts=sig.timestamp,
                setup_name=setup_name,
                direction=sig.direction,
                entry_5min=sig.entry_price,
                exit_5min=bt.exit_price,
                exit_reason_5min=bt.exit_reason,
                pnl_5min=bt.pnl_points,
                is_winner_5min=bt.pnl_points > 0,
                pnl_1min=0.0,
                total_degradation=bt.pnl_points,  # lost entire 5min PnL
                killed_before_fill=True,
                would_have_been_winner=bt.pnl_points > 0,
            )
            results.append(attr)

    return results


# ── Reporting ────────────────────────────────────────────────────

def print_execution_model():
    """Document the exact execution model."""
    print("""
╔══════════════════════════════════════════════════════════════════════════════════╗
║                          EXECUTION MODEL DOCUMENTATION                          ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║                                                                                  ║
║  SIGNAL DETECTION (5-min bars)                                                   ║
║  ─────────────────────────────                                                   ║
║  • Engine processes 5-min bars sequentially                                      ║
║  • Signal fires at the CLOSE of a 5-min bar                                      ║
║  • Signal timestamp = candle OPEN time (e.g., 10:00 for 10:00–10:04)             ║
║  • Entry price (idealized) = candle close price + dynamic slippage               ║
║                                                                                  ║
║  5-MIN IDEALIZED BACKTEST                                                        ║
║  ────────────────────────                                                        ║
║  • Entry: signal bar close + slippage (adverse direction)                        ║
║  • Exit priority: EOD > Stop > Time-stop > EMA9-trail > Target                   ║
║  • Same-bar ambiguity: if stop AND target both hit on same 5-min bar,            ║
║    assumes STOP hit first (conservative)                                         ║
║  • Slippage: dynamic model = base_slip × vol_mult × family_mult                 ║
║  • Commission: per-share on both entry and exit                                  ║
║                                                                                  ║
║  1-MIN EXECUTION SIMULATION (Auto, 0-delay)                                     ║
║  ───────────────────────────────────────────                                     ║
║  • Signal confirmed at 5-min candle close = 1-min bar at (start_idx + 4)         ║
║  • Fill bar = start_idx + 5 (first 1-min bar AFTER candle close)                 ║
║  • Pre-fill kill check: if stop hit between candle close and fill bar → no trade ║
║  • Fill price = fill bar close + dynamic slippage + fast-bar adjustment          ║
║  • Fast-bar slippage: if fill bar range > 2× recent 1-min ATR, extra cost       ║
║                                                                                  ║
║  EXIT SIMULATION (1-min path)                                                    ║
║  ──────────────────────────                                                      ║
║  • Walks 1-min bars forward from fill bar                                        ║
║  • Stop: exact 1-min low/high vs stop price (no ambiguity)                       ║
║  • EMA trail: EMA45 on 1-min ≈ EMA9 on 5-min, with 10-bar warm-up              ║
║  • Time stop: 5-min bars × 5 = 1-min bars                                       ║
║  • EOD: forced close at 15:55                                                    ║
║  • Exit slippage: same dynamic model on exit bar                                 ║
║                                                                                  ║
║  KEY DIFFERENCES FROM 5-MIN                                                      ║
║  ─────────────────────────                                                       ║
║  1. Entry is 1 bar later (fill at T+5min instead of T+0min close)                ║
║  2. Stop path resolves same-bar ambiguity (stop may or may not hit first)        ║
║  3. EMA trail computed on 1-min = slightly different values than 5-min EMA9      ║
║  4. Fast-bar slippage adds extra cost on volatile 1-min fills                    ║
║  5. Pre-fill kill removes some trades entirely                                   ║
║                                                                                  ║
╚══════════════════════════════════════════════════════════════════════════════════╝
""")


def report_attribution(attrs: List[TradeAttribution], label: str):
    """Print attribution report for a group of trades."""
    if not attrs:
        print(f"\n  {label}: No trades")
        return

    n = len(attrs)
    matched = [a for a in attrs if not a.killed_before_fill]
    killed = [a for a in attrs if a.killed_before_fill]

    print(f"\n  {'─'*80}")
    print(f"  {label}  (N={n}, matched={len(matched)}, killed={len(killed)})")
    print(f"  {'─'*80}")

    if not matched:
        print(f"    All {n} trades were killed before fill.")
        return

    # Aggregate PnL
    pnl_5min = sum(a.pnl_5min for a in matched)
    pnl_1min = sum(a.pnl_1min for a in matched)
    total_gap = pnl_5min - pnl_1min
    killed_pnl = sum(a.pnl_5min for a in killed)

    print(f"\n    PnL Summary:")
    print(f"      5-min idealized:  {pnl_5min:+.2f} pts")
    print(f"      1-min auto exec:  {pnl_1min:+.2f} pts")
    print(f"      Matched gap:      {total_gap:+.2f} pts")
    print(f"      Killed trade PnL: {killed_pnl:+.2f} pts (lost from pre-fill kills)")
    print(f"      TOTAL gap:        {total_gap + killed_pnl:+.2f} pts")

    # Entry vs exit decomposition (matched trades only)
    total_entry_deg = sum(a.entry_degradation for a in matched)
    total_exit_deg = sum(a.exit_degradation for a in matched)
    avg_entry_deg = statistics.mean(a.entry_degradation for a in matched)
    avg_exit_deg = statistics.mean(a.exit_degradation for a in matched)
    med_entry_deg = statistics.median(a.entry_degradation for a in matched)
    med_exit_deg = statistics.median(a.exit_degradation for a in matched)

    print(f"\n    Degradation Decomposition (matched trades):")
    print(f"      {'Component':<30} {'Total':>8} {'Avg/trade':>10} {'Median':>10}")
    print(f"      {'─'*30} {'─'*8} {'─'*10} {'─'*10}")
    print(f"      {'Entry fill degradation':<30} {total_entry_deg:>+8.2f} {avg_entry_deg:>+10.4f} {med_entry_deg:>+10.4f}")
    print(f"      {'Exit path degradation':<30} {total_exit_deg:>+8.2f} {avg_exit_deg:>+10.4f} {med_exit_deg:>+10.4f}")

    # The residual (interaction / rounding)
    residual = total_gap - total_entry_deg - total_exit_deg
    print(f"      {'Interaction / slippage diff':<30} {residual:>+8.2f}")
    print(f"      {'─'*30} {'─'*8}")
    print(f"      {'TOTAL matched gap':<30} {total_gap:>+8.2f}")

    # Percentage attribution
    if abs(total_gap) > 0.01:
        pct_entry = total_entry_deg / total_gap * 100
        pct_exit = total_exit_deg / total_gap * 100
        pct_killed = killed_pnl / (total_gap + killed_pnl) * 100 if abs(total_gap + killed_pnl) > 0.01 else 0
        print(f"\n    Attribution %:")
        print(f"      Entry fills:       {pct_entry:>6.1f}% of matched gap")
        print(f"      Exit paths:        {pct_exit:>6.1f}% of matched gap")
        if killed:
            print(f"      Pre-fill kills:    {pct_killed:>6.1f}% of total gap")

    # Categorical counts
    flipped = sum(1 for a in matched if a.flipped_to_loser)
    exit_changed = sum(1 for a in matched if a.exit_reason_changed)
    stop_worsened = sum(1 for a in matched if a.worsened_by_stop_timing)
    entry_worsened = sum(1 for a in matched if a.worsened_by_entry)
    same_bar = sum(1 for a in matched if a.same_bar_ambiguity)
    improved = sum(1 for a in matched if a.pnl_1min > a.pnl_5min)

    nm = len(matched)
    print(f"\n    Trade-Level Impact (of {nm} matched trades):")
    print(f"      Flipped winner → loser:      {flipped:>3} ({flipped/nm*100:>5.1f}%)")
    print(f"      Exit reason changed:         {exit_changed:>3} ({exit_changed/nm*100:>5.1f}%)")
    print(f"      Worsened by stop timing:      {stop_worsened:>3} ({stop_worsened/nm*100:>5.1f}%)")
    print(f"      Worsened mainly by entry:     {entry_worsened:>3} ({entry_worsened/nm*100:>5.1f}%)")
    print(f"      Same-bar ambiguity resolved:  {same_bar:>3} ({same_bar/nm*100:>5.1f}%)")
    print(f"      IMPROVED by 1-min path:       {improved:>3} ({improved/nm*100:>5.1f}%)")

    # Exit reason transition matrix
    print(f"\n    Exit Reason Transitions (5min → 1min):")
    transitions = defaultdict(int)
    for a in matched:
        transitions[(a.exit_reason_5min, a.exit_reason_1min)] += 1
    print(f"      {'5min → 1min':<25} {'Count':>5} {'Avg ΔPnL':>10}")
    print(f"      {'─'*25} {'─'*5} {'─'*10}")
    for (r5, r1), cnt in sorted(transitions.items(), key=lambda x: -x[1]):
        subset = [a for a in matched if a.exit_reason_5min == r5 and a.exit_reason_1min == r1]
        avg_delta = statistics.mean(a.pnl_1min - a.pnl_5min for a in subset)
        marker = " ←" if r5 != r1 else ""
        print(f"      {r5:>10} → {r1:<10}    {cnt:>5} {avg_delta:>+10.3f}{marker}")

    # Entry degradation distribution
    entry_degs = sorted([a.entry_degradation for a in matched])
    print(f"\n    Entry Degradation Distribution:")
    print(f"      Min:  {entry_degs[0]:+.4f}")
    print(f"      P25:  {entry_degs[len(entry_degs)//4]:+.4f}")
    print(f"      Med:  {statistics.median(entry_degs):+.4f}")
    print(f"      P75:  {entry_degs[3*len(entry_degs)//4]:+.4f}")
    print(f"      Max:  {entry_degs[-1]:+.4f}")
    positive_entry = sum(1 for e in entry_degs if e > 0)
    print(f"      Trades with adverse entry: {positive_entry}/{nm} ({positive_entry/nm*100:.0f}%)")


def report_worst_trades(attrs: List[TradeAttribution], n_worst: int = 10):
    """Print the N worst degradation examples."""
    matched = [a for a in attrs if not a.killed_before_fill]
    if not matched:
        return

    # Sort by total degradation (worst first)
    worst = sorted(matched, key=lambda a: -a.total_degradation)[:n_worst]

    print(f"\n    {'='*100}")
    print(f"    TOP {n_worst} WORST DEGRADATION EXAMPLES")
    print(f"    {'='*100}")
    print(f"    {'#':>2} {'Symbol':<6} {'Timestamp':<20} {'Setup':<12} "
          f"{'5min PnL':>9} {'1min PnL':>9} {'ΔPnL':>8} │ "
          f"{'ΔEntry':>8} {'ΔExit':>8} {'5m Exit':>8} {'1m Exit':>8} {'Flags'}")
    print(f"    {'─'*2} {'─'*6} {'─'*20} {'─'*12} "
          f"{'─'*9} {'─'*9} {'─'*8} │ "
          f"{'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*20}")

    for i, a in enumerate(worst, 1):
        flags = []
        if a.flipped_to_loser:
            flags.append("FLIP")
        if a.worsened_by_stop_timing:
            flags.append("STOP")
        if a.worsened_by_entry:
            flags.append("ENTRY")
        if a.same_bar_ambiguity:
            flags.append("AMBIG")
        if a.exit_reason_changed:
            flags.append("EXIT_CHG")
        flags_str = ",".join(flags) if flags else "—"

        ts_str = a.signal_ts.strftime("%Y-%m-%d %H:%M")
        print(f"    {i:>2} {a.symbol:<6} {ts_str:<20} {a.setup_name:<12} "
              f"{a.pnl_5min:>+9.3f} {a.pnl_1min:>+9.3f} {a.total_degradation:>+8.3f} │ "
              f"{a.entry_degradation:>+8.4f} {a.exit_degradation:>+8.4f} "
              f"{a.exit_reason_5min:>8} {a.exit_reason_1min:>8} {flags_str}")

    # Summary of worst trades
    avg_deg = statistics.mean(a.total_degradation for a in worst)
    flipped_count = sum(1 for a in worst if a.flipped_to_loser)
    stop_count = sum(1 for a in worst if a.worsened_by_stop_timing)
    entry_count = sum(1 for a in worst if a.worsened_by_entry)
    print(f"\n    Worst-{n_worst} summary: avg degradation {avg_deg:+.3f}pts  |  "
          f"flipped: {flipped_count}  |  stop-timing: {stop_count}  |  entry-dominated: {entry_count}")


# ── Main ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Execution-degradation attribution study")
    parser.parse_args()

    # ── Load data ──
    onemin_syms = sorted(set(
        p.stem.replace("_1min", "") for p in ONEMIN_DIR.glob("*_1min.csv")
    ))
    common_syms = [s for s in onemin_syms if s not in ("SPY", "QQQ")]

    bars_5min = {}
    for sym in common_syms:
        p = DATA_DIR / f"{sym}_5min.csv"
        if p.exists():
            bars_5min[sym] = load_bars_from_csv(str(p))

    bars_1min = {}
    for sym in common_syms:
        p = ONEMIN_DIR / f"{sym}_1min.csv"
        if p.exists():
            bars_1min[sym] = load_bars_from_csv(str(p))

    common_syms = sorted(set(bars_5min.keys()) & set(bars_1min.keys()))
    print(f"Symbols with both 5-min and 1-min data: {len(common_syms)}")

    spy_5m = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv")) if (DATA_DIR / "SPY_5min.csv").exists() else None
    qqq_5m = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv")) if (DATA_DIR / "QQQ_5min.csv").exists() else None

    sector_bars_dict = {}
    sector_etfs = set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    all_dates = sorted(set(
        b.timestamp.date() for bars in bars_5min.values() for b in bars if bars
    ))
    n_days = len(all_dates)
    print(f"Period: {min(all_dates)} → {max(all_dates)} ({n_days} days)")

    # Config: locked baseline
    cfg = OverlayConfig()
    cfg.show_ema_scalp = False
    cfg.show_failed_bounce = False
    cfg.show_spencer = False
    cfg.show_ema_fpip = False
    cfg.show_sc_v2 = False

    # ══════════════════════════════════════════════════════════════
    #  STEP 1: Run both 5-min backtest and 1-min auto execution
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*100}")
    print("  STEP 1: RUNNING MATCHED SIMULATIONS")
    print(f"{'='*100}")

    baseline_trades = []  # (sym, Trade)
    sim_trades = []       # (sym, ExecTrade)
    killed_count = 0
    total_signals = 0

    for sym in common_syms:
        sec_bars = None
        se = get_sector_etf(sym)
        if se and se in sector_bars_dict:
            sec_bars = sector_bars_dict[se]

        # 5-min backtest
        result = run_backtest(bars_5min[sym], cfg=cfg,
                              spy_bars=spy_5m, qqq_bars=qqq_5m,
                              sector_bars=sec_bars)
        for t in result.trades:
            baseline_trades.append((sym, t))

        # 1-min execution (auto, 0 delay)
        signals = extract_5min_signals(bars_5min[sym], cfg,
                                        spy_bars=spy_5m, qqq_bars=qqq_5m,
                                        sector_bars=sec_bars)
        total_signals += len(signals)
        onemin = bars_1min.get(sym, [])
        if not onemin:
            continue
        for sig in signals:
            et = simulate_1min_execution(sig, onemin, cfg,
                                          entry_delay_minutes=0,
                                          session_end_hhmm=1555)
            if et is None:
                killed_count += 1
            else:
                sim_trades.append((sym, et))

    print(f"  5-min baseline: {len(baseline_trades)} trades")
    print(f"  1-min auto exec: {len(sim_trades)} trades  ({killed_count} killed before fill)")
    print(f"  Total signals: {total_signals}")

    # ══════════════════════════════════════════════════════════════
    #  STEP 2: Build attribution
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*100}")
    print("  STEP 2: EXECUTION MODEL DOCUMENTATION")
    print(f"{'='*100}")

    print_execution_model()

    # ══════════════════════════════════════════════════════════════
    #  STEP 3: Attribution analysis
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*100}")
    print("  STEP 3: DEGRADATION ATTRIBUTION")
    print(f"{'='*100}")

    attrs = build_attribution(baseline_trades, sim_trades, [])

    # All trades
    report_attribution(attrs, "COMBINED SYSTEM")
    report_worst_trades(attrs, 10)

    # By setup
    setup_groups = defaultdict(list)
    for a in attrs:
        setup_groups[a.setup_name].append(a)

    for setup_name in sorted(setup_groups.keys()):
        group = setup_groups[setup_name]
        report_attribution(group, f"SETUP: {setup_name}")
        report_worst_trades(group, 5)

    # ══════════════════════════════════════════════════════════════
    #  STEP 4: Deep-dive on flipped trades
    # ══════════════════════════════════════════════════════════════
    matched = [a for a in attrs if not a.killed_before_fill]
    flipped = [a for a in matched if a.flipped_to_loser]

    if flipped:
        print(f"\n{'='*100}")
        print(f"  STEP 4: FLIPPED TRADES — WINNER ON 5-MIN, LOSER ON 1-MIN ({len(flipped)} trades)")
        print(f"{'='*100}")

        print(f"\n    {'Symbol':<6} {'Timestamp':<20} {'Setup':<12} "
              f"{'5min PnL':>9} {'1min PnL':>9} │ "
              f"{'ΔEntry':>8} {'ΔExit':>8} {'5m Exit':>8} {'1m Exit':>8}")
        print(f"    {'─'*6} {'─'*20} {'─'*12} "
              f"{'─'*9} {'─'*9} │ "
              f"{'─'*8} {'─'*8} {'─'*8} {'─'*8}")

        for a in sorted(flipped, key=lambda x: -x.total_degradation):
            ts_str = a.signal_ts.strftime("%Y-%m-%d %H:%M")
            print(f"    {a.symbol:<6} {ts_str:<20} {a.setup_name:<12} "
                  f"{a.pnl_5min:>+9.3f} {a.pnl_1min:>+9.3f} │ "
                  f"{a.entry_degradation:>+8.4f} {a.exit_degradation:>+8.4f} "
                  f"{a.exit_reason_5min:>8} {a.exit_reason_1min:>8}")

        # Aggregate the flipped trades
        flip_entry_total = sum(a.entry_degradation for a in flipped)
        flip_exit_total = sum(a.exit_degradation for a in flipped)
        flip_pnl_lost = sum(a.pnl_5min for a in flipped) - sum(a.pnl_1min for a in flipped)
        print(f"\n    Flipped summary:")
        print(f"      Total PnL swing: {flip_pnl_lost:+.2f} pts")
        print(f"      From entry: {flip_entry_total:+.2f}  |  From exit: {flip_exit_total:+.2f}")
        n_flip_stop = sum(1 for a in flipped if a.worsened_by_stop_timing)
        n_flip_entry = sum(1 for a in flipped if a.worsened_by_entry)
        print(f"      Stop-timing flips: {n_flip_stop}  |  Entry-dominated flips: {n_flip_entry}")

    # ══════════════════════════════════════════════════════════════
    #  STEP 5: What-if — entry degradation removed
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*100}")
    print("  STEP 5: WHAT-IF SCENARIOS")
    print(f"{'='*100}")

    # What if entry fill was perfect (5-min close)?
    pnl_1min_actual = sum(a.pnl_1min for a in matched)
    pnl_if_perfect_entry = sum(a.pnl_1min + a.entry_degradation for a in matched)
    pnl_if_perfect_exit = sum(a.pnl_1min + a.exit_degradation for a in matched)
    pnl_5min_matched = sum(a.pnl_5min for a in matched)

    print(f"\n    Scenario analysis (matched trades only):")
    print(f"      5-min idealized PnL:            {pnl_5min_matched:>+8.2f} pts")
    print(f"      1-min actual PnL:               {pnl_1min_actual:>+8.2f} pts")
    print(f"      If perfect entry (no fill cost): {pnl_if_perfect_entry:>+8.2f} pts  "
          f"(recovers {pnl_if_perfect_entry - pnl_1min_actual:+.2f})")
    print(f"      If perfect exit (5-min path):    {pnl_if_perfect_exit:>+8.2f} pts  "
          f"(recovers {pnl_if_perfect_exit - pnl_1min_actual:+.2f})")

    # ══════════════════════════════════════════════════════════════
    #  SUMMARY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*100}")
    print("  EXECUTIVE SUMMARY")
    print(f"{'='*100}")

    total_gap_all = sum(a.pnl_5min for a in attrs) - sum(a.pnl_1min for a in attrs)
    total_entry = sum(a.entry_degradation for a in matched)
    total_exit = sum(a.exit_degradation for a in matched)
    n_flipped = sum(1 for a in matched if a.flipped_to_loser)
    n_stop_worse = sum(1 for a in matched if a.worsened_by_stop_timing)
    n_entry_worse = sum(1 for a in matched if a.worsened_by_entry)

    print(f"""
    Total PnL gap (5min - 1min):  {total_gap_all:+.2f} pts

    Matched trades:               {len(matched)}
    Killed before fill:           {len([a for a in attrs if a.killed_before_fill])}

    ATTRIBUTION (matched):
      Entry fill cost:            {total_entry:+.2f} pts ({total_entry/abs(total_gap_all)*100 if abs(total_gap_all)>0.01 else 0:.0f}% of gap)
      Exit path difference:       {total_exit:+.2f} pts ({total_exit/abs(total_gap_all)*100 if abs(total_gap_all)>0.01 else 0:.0f}% of gap)

    TRADE IMPACT:
      Flipped winner→loser:       {n_flipped}/{len(matched)} ({n_flipped/len(matched)*100:.0f}%)
      Worsened by stop timing:    {n_stop_worse}/{len(matched)} ({n_stop_worse/len(matched)*100:.0f}%)
      Worsened mainly by entry:   {n_entry_worse}/{len(matched)} ({n_entry_worse/len(matched)*100:.0f}%)
    """)

    print(f"{'='*100}\n")


if __name__ == "__main__":
    main()
