"""
Exit Logic Audit — Diagnose win rate and exit realism issues.

Analyzes:
1. Exit reason distribution per strategy
2. Average P&L by exit reason
3. Stop fill assumptions (are stops always -1R?)
4. Target fill assumptions (do targets always get full RR?)
5. EOD exit P&L distribution (are there phantom profits?)
6. Intra-bar overlap (bars where both stop AND target were in range)
7. Target reachability (did bar high JUST touch target vs blow through?)
"""

import sys
import os
import math
from collections import defaultdict
from typing import Dict, List

# Add parent to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from alert_overlay.strategies.replay_live_path import (
    STRATEGY_TARGET_RR, STRATEGY_MAX_BARS,
    MIN_CONFLUENCE_DEFAULT, MIN_CONFLUENCE_BY_STRATEGY,
)
from alert_overlay.strategies.shared.signal_schema import StrategyTrade


def audit_exit_realism(all_trades: List[StrategyTrade]):
    """Run full exit audit on trade list."""

    print("\n" + "=" * 80)
    print("EXIT LOGIC AUDIT — Win Rate & Realism Investigation")
    print("=" * 80)

    # ── 1. Overall exit reason distribution ──
    by_reason = defaultdict(list)
    by_strat = defaultdict(list)
    for t in all_trades:
        by_reason[t.exit_reason].append(t)
        by_strat[t.signal.strategy_name].append(t)

    print(f"\n{'─' * 60}")
    print(f"1. OVERALL EXIT REASON DISTRIBUTION (N={len(all_trades)})")
    print(f"{'─' * 60}")
    for reason in ["target", "stop", "eod", "trail", "invalid"]:
        trades = by_reason.get(reason, [])
        n = len(trades)
        if n == 0:
            continue
        pct = n / len(all_trades) * 100
        avg_rr = sum(t.pnl_rr for t in trades) / n
        wins = sum(1 for t in trades if t.pnl_rr > 0)
        print(f"  {reason:8s}: N={n:4d} ({pct:5.1f}%)  avg_rr={avg_rr:+.2f}  "
              f"wins={wins}/{n} ({wins/n*100:.0f}%)")

    # ── 2. Per-strategy exit breakdown ──
    print(f"\n{'─' * 60}")
    print(f"2. PER-STRATEGY EXIT BREAKDOWN")
    print(f"{'─' * 60}")
    print(f"  {'Strategy':<16s} {'N':>4s}  {'WR%':>5s}  {'PF':>5s}  "
          f"{'%tgt':>5s} {'%stp':>5s} {'%eod':>5s} {'%trl':>5s}  "
          f"{'avg_W':>6s} {'avg_L':>6s} {'W/L':>5s}")

    for strat in sorted(by_strat.keys()):
        trades = by_strat[strat]
        n = len(trades)
        if n < 3:
            continue

        wins = [t for t in trades if t.pnl_rr > 0]
        losses = [t for t in trades if t.pnl_rr <= 0]
        wr = len(wins) / n * 100

        gross_w = sum(t.pnl_rr for t in wins) if wins else 0
        gross_l = abs(sum(t.pnl_rr for t in losses)) if losses else 0.001
        pf = gross_w / gross_l if gross_l > 0 else 999

        avg_w = gross_w / len(wins) if wins else 0
        avg_l = gross_l / len(losses) if losses else 0
        wl_ratio = avg_w / avg_l if avg_l > 0 else 999

        n_tgt = sum(1 for t in trades if t.exit_reason == "target")
        n_stp = sum(1 for t in trades if t.exit_reason == "stop")
        n_eod = sum(1 for t in trades if t.exit_reason == "eod")
        n_trl = sum(1 for t in trades if t.exit_reason == "trail")

        print(f"  {strat:<16s} {n:4d}  {wr:5.1f}  {pf:5.2f}  "
              f"{n_tgt/n*100:5.1f} {n_stp/n*100:5.1f} {n_eod/n*100:5.1f} {n_trl/n*100:5.1f}  "
              f"{avg_w:+6.2f} {avg_l:+6.2f} {wl_ratio:5.2f}")

    # ── 3. Stop fill analysis — are stops always exactly -1R? ──
    print(f"\n{'─' * 60}")
    print(f"3. STOP FILL ANALYSIS — Is every stop exactly -1.0R?")
    print(f"{'─' * 60}")
    stop_trades = by_reason.get("stop", [])
    if stop_trades:
        stop_pnls = [t.pnl_rr for t in stop_trades]
        print(f"  Stop exits: N={len(stop_trades)}")
        print(f"  Min P&L:    {min(stop_pnls):+.3f}R")
        print(f"  Max P&L:    {max(stop_pnls):+.3f}R")
        print(f"  Mean P&L:   {sum(stop_pnls)/len(stop_pnls):+.3f}R")
        print(f"  Exact -1.0R: {sum(1 for p in stop_pnls if abs(p + 1.0) < 0.01)}/{len(stop_pnls)}")
        # Distribution buckets
        buckets = defaultdict(int)
        for p in stop_pnls:
            if p < -1.05:
                buckets["< -1.05R"] += 1
            elif p < -0.95:
                buckets["-1.05 to -0.95R"] += 1
            elif p < -0.5:
                buckets["-0.95 to -0.50R"] += 1
            else:
                buckets["> -0.50R"] += 1
        print(f"  Distribution:")
        for bucket, count in sorted(buckets.items()):
            print(f"    {bucket}: {count}")

    # ── 4. Target fill analysis — are targets always exactly target_rr? ──
    print(f"\n{'─' * 60}")
    print(f"4. TARGET FILL ANALYSIS — Is every target exactly target_rr?")
    print(f"{'─' * 60}")
    tgt_trades = by_reason.get("target", [])
    if tgt_trades:
        tgt_pnls = [t.pnl_rr for t in tgt_trades]
        print(f"  Target exits: N={len(tgt_trades)}")
        print(f"  Min P&L:      {min(tgt_pnls):+.3f}R")
        print(f"  Max P&L:      {max(tgt_pnls):+.3f}R")
        print(f"  Mean P&L:     {sum(tgt_pnls)/len(tgt_pnls):+.3f}R")
        # Check per strategy
        print(f"\n  Per-strategy target R:R (expected vs actual):")
        for strat in sorted(by_strat.keys()):
            strat_tgts = [t for t in tgt_trades if t.signal.strategy_name == strat]
            if not strat_tgts:
                continue
            expected = STRATEGY_TARGET_RR.get(strat, 2.0)
            actual_mean = sum(t.pnl_rr for t in strat_tgts) / len(strat_tgts)
            actual_max = max(t.pnl_rr for t in strat_tgts)
            actual_min = min(t.pnl_rr for t in strat_tgts)
            print(f"    {strat:<16s}: expected={expected:.1f}R  "
                  f"actual_mean={actual_mean:.2f}R  "
                  f"range=[{actual_min:.2f}, {actual_max:.2f}]  N={len(strat_tgts)}")

    # ── 5. EOD exit P&L distribution ──
    print(f"\n{'─' * 60}")
    print(f"5. EOD EXIT P&L DISTRIBUTION — Where do time exits land?")
    print(f"{'─' * 60}")
    eod_trades = by_reason.get("eod", [])
    if eod_trades:
        eod_pnls = [t.pnl_rr for t in eod_trades]
        eod_wins = [p for p in eod_pnls if p > 0]
        eod_losses = [p for p in eod_pnls if p <= 0]
        print(f"  EOD exits: N={len(eod_trades)}")
        print(f"  Profitable:  {len(eod_wins)} ({len(eod_wins)/len(eod_trades)*100:.0f}%)")
        print(f"  Unprofitable: {len(eod_losses)} ({len(eod_losses)/len(eod_trades)*100:.0f}%)")
        if eod_wins:
            print(f"  Avg EOD win:  {sum(eod_wins)/len(eod_wins):+.2f}R")
        if eod_losses:
            print(f"  Avg EOD loss: {sum(eod_losses)/len(eod_losses):+.2f}R")
        print(f"  Overall avg:  {sum(eod_pnls)/len(eod_pnls):+.2f}R")

        # Per-strategy EOD breakdown
        print(f"\n  Per-strategy EOD P&L:")
        for strat in sorted(by_strat.keys()):
            strat_eod = [t for t in eod_trades if t.signal.strategy_name == strat]
            if not strat_eod:
                continue
            avg_eod = sum(t.pnl_rr for t in strat_eod) / len(strat_eod)
            eod_w = sum(1 for t in strat_eod if t.pnl_rr > 0)
            print(f"    {strat:<16s}: N={len(strat_eod):3d}  "
                  f"avg={avg_eod:+.2f}R  wins={eod_w}/{len(strat_eod)}")

    # ── 6. THE KEY METRIC: What does each strategy need to be profitable? ──
    print(f"\n{'─' * 60}")
    print(f"6. PROFITABILITY MATH — Required WR vs Actual WR")
    print(f"{'─' * 60}")
    print(f"  {'Strategy':<16s} {'tgt_RR':>6s} {'bkeven':>6s} {'WR%':>5s}  "
          f"{'avgW':>5s} {'avgL':>5s} {'rWR%':>5s} {'edge':>6s}  verdict")

    for strat in sorted(by_strat.keys()):
        trades = by_strat[strat]
        n = len(trades)
        if n < 3:
            continue
        target_rr = STRATEGY_TARGET_RR.get(strat, 2.0)
        breakeven_wr = 1.0 / (1.0 + target_rr) * 100

        wins = [t for t in trades if t.pnl_rr > 0]
        losses = [t for t in trades if t.pnl_rr <= 0]
        wr = len(wins) / n * 100
        avg_w = sum(t.pnl_rr for t in wins) / len(wins) if wins else 0
        avg_l = abs(sum(t.pnl_rr for t in losses) / len(losses)) if losses else 0

        # Real breakeven using actual avg W/L
        real_breakeven = avg_l / (avg_w + avg_l) * 100 if (avg_w + avg_l) > 0 else 50
        edge = wr - real_breakeven

        verdict = "OK" if edge > 3 else "MARGINAL" if edge > 0 else "NEGATIVE"
        print(f"  {strat:<16s} {target_rr:6.1f} {breakeven_wr:6.1f} {wr:5.1f}  "
              f"{avg_w:+5.2f} {avg_l:+5.2f} {real_breakeven:5.1f} {edge:+6.1f}  {verdict}")

    # ── 7. SMOKING GUN: Unrealistic target fills ──
    # Check if target was BARELY touched (bar high == target within 0.5%)
    print(f"\n{'─' * 60}")
    print(f"7. TARGET FILL REALISM — How far did price go past target?")
    print(f"{'─' * 60}")
    print(f"  (Checking: did the bar that 'hit target' actually trade through it,")
    print(f"   or did the high BARELY touch target? Requires bar data access.)")
    print(f"  → This analysis requires bar-level data at exit time.")
    print(f"  → See section 8 below for structural analysis instead.\n")

    # ── 8. Win Rate Decomposition ──
    print(f"{'─' * 60}")
    print(f"8. WIN RATE DECOMPOSITION — Where do wins come from?")
    print(f"{'─' * 60}")
    print(f"  {'Strategy':<16s} {'N':>4s}  {'WR':>5s}  "
          f"{'tgt_W':>5s} {'eod_W':>5s} {'trl_W':>5s}  "
          f"{'stp_L':>5s} {'eod_L':>5s}  {'tgt%ofW':>7s}")

    for strat in sorted(by_strat.keys()):
        trades = by_strat[strat]
        n = len(trades)
        if n < 3:
            continue

        wr = sum(1 for t in trades if t.pnl_rr > 0) / n * 100
        tgt_wins = sum(1 for t in trades if t.exit_reason == "target" and t.pnl_rr > 0)
        eod_wins = sum(1 for t in trades if t.exit_reason == "eod" and t.pnl_rr > 0)
        trl_wins = sum(1 for t in trades if t.exit_reason == "trail" and t.pnl_rr > 0)
        stp_losses = sum(1 for t in trades if t.exit_reason == "stop" and t.pnl_rr <= 0)
        eod_losses = sum(1 for t in trades if t.exit_reason == "eod" and t.pnl_rr <= 0)

        total_wins = tgt_wins + eod_wins + trl_wins
        tgt_pct = tgt_wins / total_wins * 100 if total_wins > 0 else 0

        print(f"  {strat:<16s} {n:4d}  {wr:5.1f}  "
              f"{tgt_wins:5d} {eod_wins:5d} {trl_wins:5d}  "
              f"{stp_losses:5d} {eod_losses:5d}  {tgt_pct:7.0f}%")

    # ── 9. CRITICAL: Stop P&L vs Expected ──
    print(f"\n{'─' * 60}")
    print(f"9. STOP LOSS FIDELITY — Are stops always near -1.0R?")
    print(f"{'─' * 60}")
    for strat in sorted(by_strat.keys()):
        stops = [t for t in by_strat[strat] if t.exit_reason == "stop"]
        if len(stops) < 3:
            continue
        pnls = [t.pnl_rr for t in stops]
        mean_stop = sum(pnls) / len(pnls)
        max_stop = max(pnls)
        min_stop = min(pnls)
        near_1r = sum(1 for p in pnls if abs(p + 1.0) < 0.05)
        print(f"  {strat:<16s}: N={len(stops):3d}  "
              f"mean={mean_stop:+.3f}R  range=[{min_stop:+.3f}, {max_stop:+.3f}]  "
              f"near_-1R={near_1r}/{len(stops)}")

    # ── 10. Bars held analysis ──
    print(f"\n{'─' * 60}")
    print(f"10. BARS HELD — How long do trades last?")
    print(f"{'─' * 60}")
    for strat in sorted(by_strat.keys()):
        trades = by_strat[strat]
        n = len(trades)
        if n < 3:
            continue
        max_bars = STRATEGY_MAX_BARS.get(strat, 78)
        avg_bars = sum(t.bars_held for t in trades) / n
        wins = [t for t in trades if t.pnl_rr > 0]
        losses = [t for t in trades if t.pnl_rr <= 0]
        avg_bars_w = sum(t.bars_held for t in wins) / len(wins) if wins else 0
        avg_bars_l = sum(t.bars_held for t in losses) / len(losses) if losses else 0
        print(f"  {strat:<16s}: max_allowed={max_bars:3d}  "
              f"avg_held={avg_bars:.0f}  avg_W={avg_bars_w:.0f}  avg_L={avg_bars_l:.0f}")

    # ── Summary ──
    print(f"\n{'=' * 80}")
    print(f"KEY FINDINGS SUMMARY")
    print(f"{'=' * 80}")

    # Count strategies with low WR but positive PF
    suspect = []
    for strat in sorted(by_strat.keys()):
        trades = by_strat[strat]
        n = len(trades)
        if n < 3:
            continue
        wr = sum(1 for t in trades if t.pnl_rr > 0) / n * 100
        wins = [t for t in trades if t.pnl_rr > 0]
        losses = [t for t in trades if t.pnl_rr <= 0]
        gross_w = sum(t.pnl_rr for t in wins)
        gross_l = abs(sum(t.pnl_rr for t in losses)) if losses else 0.001
        pf = gross_w / gross_l
        if wr < 40 and pf > 1.0:
            suspect.append((strat, wr, pf, n))

    if suspect:
        print(f"\n  SUSPECT: {len(suspect)} strategies have WR<40% but PF>1.0:")
        for strat, wr, pf, n in suspect:
            print(f"    {strat}: WR={wr:.1f}%, PF={pf:.2f}, N={n}")
        print(f"\n  This CAN be mathematically valid with high R:R targets,")
        print(f"  but is PRACTICALLY suspicious for real-world trading.")


if __name__ == "__main__":
    # Run the full replay to generate trades, then audit
    print("Running replay to generate trade data...")
    from alert_overlay.strategies.replay_live_path import main as replay_main
    import io
    from contextlib import redirect_stdout

    # We need to capture the trades from replay
    # Monkey-patch to capture trades
    import alert_overlay.strategies.replay_live_path as rlp

    _orig_main = rlp.main

    # We'll run a modified version that returns trades
    print("Use: python -m alert_overlay.strategies.exit_audit")
    print("(Running replay internally...)\n")

    # Import and run replay, capturing trades
    from alert_overlay.strategies.replay_live_path import (
        _load_symbols, _build_pipeline_components,
    )

    # Actually, let's just modify replay to also run our audit
    # Simpler: run replay_live_path and parse the CSV output
    import subprocess
    import csv

    # Run replay with CSV export
    result = subprocess.run(
        [sys.executable, "-m", "alert_overlay.strategies.replay_live_path"],
        capture_output=True, text=True, cwd=os.path.join(os.path.dirname(__file__), "..", ".."),
        timeout=300,
    )

    print(result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout)
    if result.returncode != 0:
        print("STDERR:", result.stderr[-2000:])
        sys.exit(1)

    # Find CSV file
    csv_path = None
    for line in result.stdout.split("\n"):
        if "CSV exported" in line or ".csv" in line.lower():
            # Try to extract path
            parts = line.split()
            for p in parts:
                if p.endswith(".csv"):
                    csv_path = p
                    break

    # Try default location
    if not csv_path:
        import glob
        csvs = sorted(glob.glob(os.path.expanduser("~/Desktop/replay_live_*.csv")),
                       key=os.path.getmtime, reverse=True)
        if csvs:
            csv_path = csvs[0]

    if not csv_path or not os.path.exists(csv_path):
        print("Could not find CSV output. Running inline analysis...")
        sys.exit(1)

    print(f"\nLoading trades from: {csv_path}")

    # Parse CSV into pseudo-trade objects
    class PseudoSignal:
        def __init__(self, row):
            self.strategy_name = row.get("strategy", "")
            self.direction = 1 if row.get("side", "") == "LONG" else -1

    class PseudoTrade:
        def __init__(self, row):
            self.signal = PseudoSignal(row)
            self.pnl_rr = float(row.get("pnl_rr", 0))
            self.exit_reason = row.get("exit_reason", "")
            self.bars_held = int(row.get("bars_held", 0))

    trades = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append(PseudoTrade(row))

    print(f"Loaded {len(trades)} trades from CSV")
    audit_exit_realism(trades)
