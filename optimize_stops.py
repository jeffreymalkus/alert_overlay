"""
Stop width optimizer — tests multiple stop multipliers while keeping
dollar risk constant (wider stop = fewer shares = same risk).

Runs the engine as-is to generate signals, then overrides the stop/target
in the backtest simulation to test different risk geometries.

Usage:
    python -m alert_overlay.optimize_stops
    python -m alert_overlay.optimize_stops --symbols TSLA,META,SPY
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

from .backtest import load_bars_from_csv, BacktestResult, Trade
from .config import OverlayConfig
from .engine import SignalEngine
from .models import Bar, Signal, SetupId, NaN

import math

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent / "data"
WATCHLIST_FILE = Path(__file__).parent / "watchlist.txt"


def load_watchlist():
    symbols = []
    with open(WATCHLIST_FILE) as f:
        for line in f:
            sym = line.strip().upper()
            if sym and not sym.startswith("#"):
                symbols.append(sym)
    return symbols


def run_with_stop_mult(bars, cfg, stop_mult=1.0, target_mult=1.0,
                       min_quality=1, require_regime=False,
                       session_end=1555):
    """
    Run backtest with modified stop/target distances.

    stop_mult: multiplier on original stop distance (1.5 = 50% wider stop)
    target_mult: multiplier on original target distance (0.75 = 25% closer target)
    min_quality: filter signals below this quality
    require_regime: filter signals where fits_regime=False
    """
    engine = SignalEngine(cfg)
    slip = cfg.slippage_per_side
    comm = cfg.commission_per_share
    cost_per_side = slip + comm

    result = BacktestResult()
    open_trade = None

    for i, bar in enumerate(bars):
        signals = engine.process_bar(bar)

        # Filter signals
        filtered = []
        for sig in signals:
            if sig.quality_score < min_quality:
                continue
            if require_regime and not sig.fits_regime:
                continue
            filtered.append(sig)

        result.signals_total += len(filtered)

        # Check open trade exits
        if open_trade is not None:
            sig = open_trade["signal"]
            adj_stop = open_trade["adj_stop"]
            adj_target = open_trade["adj_target"]
            filled_entry = open_trade["filled_entry"]
            exited = False

            # EOD
            if bar.time_hhmm >= session_end:
                trade = _close(sig, filled_entry, bar.close, bar.timestamp,
                               "eod", i - open_trade["idx"], cost_per_side)
                result.trades.append(trade)
                open_trade = None
                exited = True

            # Target / Stop with adjusted levels
            if not exited:
                if sig.direction == 1:
                    hit_target = bar.high >= adj_target
                    hit_stop = bar.low <= adj_stop
                else:
                    hit_target = bar.low <= adj_target
                    hit_stop = bar.high >= adj_stop

                if hit_target and hit_stop:
                    exit_reason = "stop"
                    exit_price = adj_stop
                elif hit_stop:
                    exit_reason = "stop"
                    exit_price = adj_stop
                elif hit_target:
                    exit_reason = "target"
                    exit_price = adj_target
                else:
                    exit_reason = None
                    exit_price = 0.0

                if exit_reason:
                    trade = _close(sig, filled_entry, exit_price, bar.timestamp,
                                   exit_reason, i - open_trade["idx"], cost_per_side)
                    result.trades.append(trade)
                    open_trade = None
                    exited = True

        # New signals
        for sig in filtered:
            # Close opposing
            if open_trade is not None:
                old = open_trade["signal"]
                if old.direction != sig.direction:
                    trade = _close(old, open_trade["filled_entry"], bar.close,
                                   bar.timestamp, "opposing",
                                   i - open_trade["idx"], cost_per_side)
                    result.trades.append(trade)
                    open_trade = None

            if open_trade is None:
                # Adjust stop and target
                orig_risk = sig.risk
                adj_risk = orig_risk * stop_mult
                orig_reward = sig.reward
                adj_reward = orig_reward * target_mult

                if sig.direction == 1:
                    adj_stop = sig.entry_price - adj_risk
                    adj_target = sig.entry_price + adj_reward
                else:
                    adj_stop = sig.entry_price + adj_risk
                    adj_target = sig.entry_price - adj_reward

                filled_entry = sig.entry_price + (cost_per_side * sig.direction)
                open_trade = {
                    "signal": sig,
                    "idx": i,
                    "filled_entry": filled_entry,
                    "adj_stop": adj_stop,
                    "adj_target": adj_target,
                }

    # Close remaining
    if open_trade is not None:
        sig = open_trade["signal"]
        last = bars[-1]
        trade = _close(sig, open_trade["filled_entry"], last.close,
                       last.timestamp, "eod", len(bars) - 1 - open_trade["idx"],
                       cost_per_side)
        result.trades.append(trade)

    return result


def _close(sig, filled_entry, exit_price, exit_time, exit_reason, bars_held, cost_per_side):
    adjusted_exit = exit_price - (cost_per_side * sig.direction)
    trade = Trade(
        signal=sig,
        exit_price=exit_price,
        exit_time=exit_time,
        exit_reason=exit_reason,
        bars_held=bars_held,
    )
    trade.pnl_points = (adjusted_exit - filled_entry) * sig.direction
    trade.pnl_rr = trade.pnl_points / sig.risk if sig.risk > 0 else 0
    return trade


def main():
    parser = argparse.ArgumentParser(description="Stop width optimizer")
    parser.add_argument("--symbols", type=str, default=None)
    parser.add_argument("--session-end", type=int, default=1555)
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        symbols = load_watchlist()

    cfg = OverlayConfig()

    # Load all bar data
    all_bars = {}
    for sym in symbols:
        csv_path = DATA_DIR / f"{sym}_5min.csv"
        if csv_path.exists():
            bars = load_bars_from_csv(str(csv_path))
            if bars:
                all_bars[sym] = bars

    print(f"Testing {len(all_bars)} symbols\n")

    # ══════════════════════════════════════════════════════════════
    # TEST MATRIX
    # ══════════════════════════════════════════════════════════════
    scenarios = [
        # (label, stop_mult, target_mult, min_quality, require_regime)
        ("Baseline (current)",         1.0, 1.0, 1, False),
        ("Regime only",                1.0, 1.0, 1, True),
        ("Q>=4 only",                  1.0, 1.0, 4, False),
        ("Q>=4 + Regime",              1.0, 1.0, 4, True),
        ("Stop 1.25x",                 1.25, 1.0, 1, False),
        ("Stop 1.5x",                  1.5, 1.0, 1, False),
        ("Stop 2.0x",                  2.0, 1.0, 1, False),
        ("Stop 1.5x + Q>=4",          1.5, 1.0, 4, False),
        ("Stop 1.5x + Q>=4 + Regime", 1.5, 1.0, 4, True),
        ("Stop 2.0x + Q>=4 + Regime", 2.0, 1.0, 4, True),
        ("Target 0.75x (closer)",      1.0, 0.75, 1, False),
        ("Target 0.75x + Q>=4",        1.0, 0.75, 4, False),
        ("Stop 1.5x + Target 0.75x",  1.5, 0.75, 1, False),
        ("S1.5x + T0.75x + Q>=4",     1.5, 0.75, 4, False),
        ("S1.5x + T0.75x + Q4 + Reg", 1.5, 0.75, 4, True),
        ("S2.0x + T0.75x + Q4 + Reg", 2.0, 0.75, 4, True),
    ]

    print(f"{'Scenario':<32} {'Trds':>5} {'WR%':>6} {'PF':>6} {'PnL(pts)':>10} "
          f"{'AvgR':>7} {'Stop%':>6} {'Tgt%':>5} {'EOD%':>5}")
    print("-" * 100)

    for label, s_mult, t_mult, min_q, req_reg in scenarios:
        total_trades = 0
        total_wins = 0
        total_pnl = 0.0
        total_rr = 0.0
        gross_wins = 0.0
        gross_losses = 0.0
        exit_counts = defaultdict(int)

        for sym, bars in all_bars.items():
            result = run_with_stop_mult(
                bars, cfg,
                stop_mult=s_mult,
                target_mult=t_mult,
                min_quality=min_q,
                require_regime=req_reg,
                session_end=args.session_end,
            )
            for t in result.trades:
                total_trades += 1
                total_pnl += t.pnl_points
                total_rr += t.pnl_rr
                exit_counts[t.exit_reason] += 1
                if t.pnl_points > 0:
                    total_wins += 1
                    gross_wins += t.pnl_points
                else:
                    gross_losses += abs(t.pnl_points)

        if total_trades == 0:
            print(f"  {label:<32} {'0':>5}")
            continue

        wr = total_wins / total_trades * 100
        pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")
        pf_str = f"{pf:.2f}" if pf < 999 else "∞"
        avg_rr = total_rr / total_trades
        stop_pct = exit_counts.get("stop", 0) / total_trades * 100
        tgt_pct = exit_counts.get("target", 0) / total_trades * 100
        eod_pct = exit_counts.get("eod", 0) / total_trades * 100

        print(f"  {label:<32} {total_trades:>5} {wr:>5.1f}% {pf_str:>6} {total_pnl:>+10.2f} "
              f"{avg_rr:>+7.3f} {stop_pct:>5.1f}% {tgt_pct:>4.1f}% {eod_pct:>4.1f}%")

    # ══════════════════════════════════════════════════════════════
    # POSITION SIZING EXAMPLE
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  POSITION SIZING IMPACT (assuming $100 max risk per trade)")
    print(f"{'=' * 100}")
    print(f"\n  {'Stop Width':<15} {'Avg Risk/sh':>12} {'Shares@$100':>13} {'$ per pt':>10} {'Net Effect':>12}")
    print(f"  {'-' * 64}")

    # Get average risk from baseline
    avg_risks = []
    for sym, bars in all_bars.items():
        result = run_with_stop_mult(bars, cfg, stop_mult=1.0, min_quality=4, require_regime=True)
        for t in result.trades:
            if t.signal.risk > 0:
                avg_risks.append(t.signal.risk)

    if avg_risks:
        base_risk = sum(avg_risks) / len(avg_risks)
        max_dollar_risk = 100.0

        for mult in [1.0, 1.25, 1.5, 2.0]:
            adj_risk = base_risk * mult
            shares = int(max_dollar_risk / adj_risk) if adj_risk > 0 else 0
            dollar_per_pt = shares * 1.0  # $1 per point per share
            # Compare: wider stop loses more per stop-out but wins more often
            print(f"  {mult:.2f}x            ${adj_risk:>10.4f}   {shares:>12}   ${dollar_per_pt:>8.2f}   "
                  f"{'baseline' if mult == 1.0 else f'{shares/int(max_dollar_risk/base_risk)*100:.0f}% shares'}")

    print(f"\n{'=' * 100}")


if __name__ == "__main__":
    main()
