"""
Run backtest from CSV and print results.

Usage:
    python -m alert_overlay.run_backtest_cli --data ./data/SPY_5min.csv
    python -m alert_overlay.run_backtest_cli --data ./data/SPY_5min.csv --min-quality 3
"""

import argparse
import sys
from .backtest import load_bars_from_csv, run_backtest, BacktestResult
from .config import OverlayConfig


def print_results(result: BacktestResult):
    trades = result.trades
    if not trades:
        print("\n  NO TRADES GENERATED")
        print(f"  Signals emitted: {result.signals_total}")
        print("  Check min_quality, min_rr, and data range.")
        return

    wins = [t for t in trades if t.pnl_points > 0]
    losses = [t for t in trades if t.pnl_points <= 0]
    total_pnl = sum(t.pnl_points for t in trades)
    gross_wins = sum(t.pnl_points for t in wins) if wins else 0
    gross_losses = abs(sum(t.pnl_points for t in losses)) if losses else 0
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")
    win_rate = len(wins) / len(trades) * 100

    # Max drawdown (cumulative PnL)
    cum_pnl = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cum_pnl += t.pnl_points
        peak = max(peak, cum_pnl)
        dd = peak - cum_pnl
        max_dd = max(max_dd, dd)

    # Average R:R
    avg_rr_win = sum(t.pnl_rr for t in wins) / len(wins) if wins else 0
    avg_rr_loss = sum(t.pnl_rr for t in losses) / len(losses) if losses else 0

    print("\n" + "=" * 60)
    print("  BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Signals emitted:   {result.signals_total}")
    print(f"  Trades taken:      {len(trades)}")
    print(f"  Win rate:          {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Profit factor:     {profit_factor:.2f}")
    print(f"  Total PnL (pts):   {total_pnl:+.2f}")
    print(f"  Avg win (R):       {avg_rr_win:+.2f}R")
    print(f"  Avg loss (R):      {avg_rr_loss:+.2f}R")
    print(f"  Max drawdown (pts):{max_dd:.2f}")

    # Breakdown by setup
    from collections import defaultdict
    setup_stats = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    for t in trades:
        name = t.signal.setup_name
        if t.pnl_points > 0:
            setup_stats[name]["w"] += 1
        else:
            setup_stats[name]["l"] += 1
        setup_stats[name]["pnl"] += t.pnl_points

    print("\n  BY SETUP:")
    print(f"  {'Setup':<16} {'W':>4} {'L':>4} {'WR%':>6} {'PnL':>8}")
    print(f"  {'-'*40}")
    for name, s in sorted(setup_stats.items(), key=lambda x: -x[1]["pnl"]):
        total = s["w"] + s["l"]
        wr = s["w"] / total * 100 if total > 0 else 0
        print(f"  {name:<16} {s['w']:>4} {s['l']:>4} {wr:>5.1f}% {s['pnl']:>+8.2f}")

    # Breakdown by direction
    longs = [t for t in trades if t.signal.direction == 1]
    shorts = [t for t in trades if t.signal.direction == -1]
    long_pnl = sum(t.pnl_points for t in longs)
    short_pnl = sum(t.pnl_points for t in shorts)
    long_wr = sum(1 for t in longs if t.pnl_points > 0) / len(longs) * 100 if longs else 0
    short_wr = sum(1 for t in shorts if t.pnl_points > 0) / len(shorts) * 100 if shorts else 0

    print(f"\n  BY DIRECTION:")
    print(f"  LONG:  {len(longs)} trades, {long_wr:.1f}% WR, {long_pnl:+.2f} pts")
    print(f"  SHORT: {len(shorts)} trades, {short_wr:.1f}% WR, {short_pnl:+.2f} pts")

    # Exit reason breakdown
    from collections import Counter
    reasons = Counter(t.exit_reason for t in trades)
    print(f"\n  EXIT REASONS:")
    for reason, count in reasons.most_common():
        pnl = sum(t.pnl_points for t in trades if t.exit_reason == reason)
        print(f"  {reason:<16} {count:>4} trades  {pnl:>+8.2f} pts")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Run backtest on historical CSV data")
    parser.add_argument("--data", required=True, help="Path to CSV file with 5-min bars")
    parser.add_argument("--min-quality", type=int, default=None, help="Override min_quality (default: 1)")
    parser.add_argument("--min-rr", type=float, default=None, help="Override min_rr")
    parser.add_argument("--slippage", type=float, default=None, help="Override slippage_per_side")
    parser.add_argument("--session-end", type=int, default=1555, help="Session end HHMM (default: 1555)")
    args = parser.parse_args()

    cfg = OverlayConfig()
    if args.min_quality is not None:
        cfg.min_quality = args.min_quality
    if args.min_rr is not None:
        cfg.min_rr = args.min_rr
    if args.slippage is not None:
        cfg.slippage_per_side = args.slippage

    print(f"Loading bars from {args.data}...")
    bars = load_bars_from_csv(args.data)
    print(f"Loaded {len(bars)} bars")

    if not bars:
        print("No bars loaded. Check CSV format (needs: datetime, open, high, low, close, volume)")
        sys.exit(1)

    date_range = set(b.timestamp.strftime("%Y-%m-%d") for b in bars)
    print(f"Date range: {min(date_range)} to {max(date_range)} ({len(date_range)} days)")
    print(f"Config: min_quality={cfg.min_quality}, min_rr={cfg.min_rr}, "
          f"slippage={cfg.slippage_per_side}, bar_interval={cfg.bar_interval_minutes}min")

    print("\nRunning backtest...")
    result = run_backtest(bars, cfg=cfg, session_end_hhmm=args.session_end)
    print_results(result)


if __name__ == "__main__":
    main()
