"""
Backtest all symbols in watchlist.txt and produce a summary table.

Downloads historical data from IBKR (if not already cached in ./data/),
runs the signal engine on each symbol, and prints a consolidated report.

Requirements:
    pip install ib_insync

Usage:
    # Backtest all 35 symbols (30 days, default)
    python -m alert_overlay.run_backtest_all --port 7497

    # Custom date range
    python -m alert_overlay.run_backtest_all --days 60 --port 7497

    # Skip download (use cached CSVs only)
    python -m alert_overlay.run_backtest_all --no-download

    # Only specific symbols
    python -m alert_overlay.run_backtest_all --symbols SPY,TSLA,NVDA --port 7497
"""

import argparse
import csv
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .backtest import load_bars_from_csv, run_backtest, BacktestResult
from .config import OverlayConfig

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent / "data"
WATCHLIST_FILE = Path(__file__).parent / "watchlist.txt"


def load_watchlist() -> list:
    """Load symbols from watchlist.txt."""
    if not WATCHLIST_FILE.exists():
        print(f"ERROR: {WATCHLIST_FILE} not found.")
        sys.exit(1)
    symbols = []
    with open(WATCHLIST_FILE) as f:
        for line in f:
            sym = line.strip().upper()
            if sym and not sym.startswith("#"):
                symbols.append(sym)
    return symbols


def download_symbol(ib, symbol: str, start_date: datetime, end_date: datetime,
                    bar_size: str = "5 mins") -> Path:
    """Download historical bars for one symbol and save to CSV. Returns CSV path."""
    from ib_insync import Stock

    csv_path = DATA_DIR / f"{symbol}_5min.csv"

    contract = Stock(symbol, "SMART", "USD")
    try:
        ib.qualifyContracts(contract)
    except Exception as e:
        print(f"  {symbol}: Failed to qualify contract — {e}")
        return None

    all_rows = []
    current = end_date
    seen_dates = set()

    while current.date() >= start_date.date():
        try:
            bars = ib.reqHistoricalData(
                contract,
                endDateTime=current.strftime("%Y%m%d %H:%M:%S") + " US/Eastern",
                durationStr="1 D",
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
            for b in bars:
                if isinstance(b.date, str):
                    try:
                        dt = datetime.strptime(b.date, "%Y%m%d  %H:%M:%S")
                    except ValueError:
                        try:
                            dt = datetime.strptime(b.date, "%Y%m%d %H:%M:%S")
                        except ValueError:
                            continue
                    dt = dt.replace(tzinfo=EASTERN)
                else:
                    if b.date.tzinfo is not None:
                        dt = b.date.astimezone(EASTERN)
                    else:
                        dt = b.date.replace(tzinfo=EASTERN)

                key = dt.strftime("%Y-%m-%d %H:%M:%S")
                if key not in seen_dates:
                    seen_dates.add(key)
                    all_rows.append({
                        "datetime": key,
                        "open": f"{b.open:.4f}",
                        "high": f"{b.high:.4f}",
                        "low": f"{b.low:.4f}",
                        "close": f"{b.close:.4f}",
                        "volume": str(int(b.volume)),
                    })

            current -= timedelta(days=1)
            while current.weekday() >= 5:
                current -= timedelta(days=1)

            time.sleep(0.5)  # IBKR rate limit

        except Exception as e:
            current -= timedelta(days=1)
            time.sleep(2)

    if not all_rows:
        return None

    all_rows.sort(key=lambda r: r["datetime"])

    # Save CSV
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["datetime", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(all_rows)

    return csv_path


def backtest_symbol(symbol: str, csv_path: Path, cfg: OverlayConfig,
                    session_end: int = 1555) -> dict:
    """Run backtest on one symbol. Returns a stats dict."""
    bars = load_bars_from_csv(str(csv_path))
    if not bars:
        return None

    result = run_backtest(bars, cfg=cfg, session_end_hhmm=session_end)
    trades = result.trades

    if not trades:
        return {
            "symbol": symbol,
            "bars": len(bars),
            "days": len(set(b.timestamp.strftime("%Y-%m-%d") for b in bars)),
            "signals": result.signals_total,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "total_pnl": 0.0,
            "avg_win_r": 0.0,
            "avg_loss_r": 0.0,
            "max_dd": 0.0,
            "long_trades": 0,
            "long_wr": 0.0,
            "long_pnl": 0.0,
            "short_trades": 0,
            "short_wr": 0.0,
            "short_pnl": 0.0,
            "best_setup": "-",
            "worst_setup": "-",
            "exit_stop_pct": 0.0,
            "exit_target_pct": 0.0,
            "exit_eod_pct": 0.0,
        }

    wins = [t for t in trades if t.pnl_points > 0]
    losses = [t for t in trades if t.pnl_points <= 0]
    gross_wins = sum(t.pnl_points for t in wins)
    gross_losses = abs(sum(t.pnl_points for t in losses))
    pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    # Direction breakdown
    longs = [t for t in trades if t.signal.direction == 1]
    shorts = [t for t in trades if t.signal.direction == -1]
    long_pnl = sum(t.pnl_points for t in longs)
    short_pnl = sum(t.pnl_points for t in shorts)
    long_wr = sum(1 for t in longs if t.pnl_points > 0) / len(longs) * 100 if longs else 0
    short_wr = sum(1 for t in shorts if t.pnl_points > 0) / len(shorts) * 100 if shorts else 0

    # Setup breakdown
    setup_pnl = defaultdict(float)
    for t in trades:
        setup_pnl[t.signal.setup_name] += t.pnl_points
    best_setup = max(setup_pnl, key=setup_pnl.get) if setup_pnl else "-"
    worst_setup = min(setup_pnl, key=setup_pnl.get) if setup_pnl else "-"

    # Exit reason breakdown
    from collections import Counter
    exit_counts = Counter(t.exit_reason for t in trades)
    total_t = len(trades)

    # Max drawdown
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cum += t.pnl_points
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    # Avg R
    avg_win_r = sum(t.pnl_rr for t in wins) / len(wins) if wins else 0
    avg_loss_r = sum(t.pnl_rr for t in losses) / len(losses) if losses else 0

    return {
        "symbol": symbol,
        "bars": len(bars),
        "days": len(set(b.timestamp.strftime("%Y-%m-%d") for b in bars)),
        "signals": result.signals_total,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100,
        "profit_factor": pf,
        "total_pnl": sum(t.pnl_points for t in trades),
        "avg_win_r": avg_win_r,
        "avg_loss_r": avg_loss_r,
        "max_dd": max_dd,
        "long_trades": len(longs),
        "long_wr": long_wr,
        "long_pnl": long_pnl,
        "short_trades": len(shorts),
        "short_wr": short_wr,
        "short_pnl": short_pnl,
        "best_setup": best_setup,
        "worst_setup": worst_setup,
        "exit_stop_pct": exit_counts.get("stop", 0) / total_t * 100,
        "exit_target_pct": exit_counts.get("target", 0) / total_t * 100,
        "exit_eod_pct": exit_counts.get("eod", 0) / total_t * 100,
    }


def print_summary(results: list):
    """Print the consolidated summary table."""
    # Filter out None results
    results = [r for r in results if r is not None]
    if not results:
        print("\nNo results to display.")
        return

    # Sort by total PnL descending
    results.sort(key=lambda r: r["total_pnl"], reverse=True)

    # ── MAIN TABLE ──
    print("\n" + "=" * 100)
    print("  MULTI-SYMBOL BACKTEST RESULTS")
    print("=" * 100)

    header = (f"  {'Symbol':<7} {'Days':>4} {'Sigs':>5} {'Trds':>5} "
              f"{'WR%':>6} {'PF':>6} {'PnL(pts)':>10} "
              f"{'AvgW(R)':>8} {'AvgL(R)':>8} {'MaxDD':>8} {'Best Setup':<16}")
    print(header)
    print("  " + "-" * 96)

    total_trades = 0
    total_wins = 0
    total_pnl = 0.0
    total_signals = 0
    symbols_with_trades = 0

    for r in results:
        total_trades += r["trades"]
        total_wins += r["wins"]
        total_pnl += r["total_pnl"]
        total_signals += r["signals"]
        if r["trades"] > 0:
            symbols_with_trades += 1

        pf_str = f"{r['profit_factor']:.2f}" if r["profit_factor"] < 999 else "∞"
        print(f"  {r['symbol']:<7} {r['days']:>4} {r['signals']:>5} {r['trades']:>5} "
              f"{r['win_rate']:>5.1f}% {pf_str:>6} {r['total_pnl']:>+10.2f} "
              f"{r['avg_win_r']:>+8.2f} {r['avg_loss_r']:>+8.2f} {r['max_dd']:>8.2f} "
              f"{r['best_setup']:<16}")

    # ── TOTALS ──
    print("  " + "-" * 96)
    overall_wr = total_wins / total_trades * 100 if total_trades > 0 else 0
    print(f"  {'TOTAL':<7} {'':>4} {total_signals:>5} {total_trades:>5} "
          f"{overall_wr:>5.1f}% {'':>6} {total_pnl:>+10.2f}")
    print(f"\n  Symbols tested: {len(results)} | "
          f"Symbols with trades: {symbols_with_trades} | "
          f"Symbols with no trades: {len(results) - symbols_with_trades}")

    # ── DIRECTION SUMMARY ──
    print("\n  DIRECTION BREAKDOWN:")
    print(f"  {'Symbol':<7} {'Long#':>6} {'LongWR%':>8} {'LongPnL':>10} "
          f"{'Short#':>7} {'ShortWR%':>9} {'ShortPnL':>10}")
    print("  " + "-" * 63)

    total_long = 0
    total_short = 0
    total_long_pnl = 0.0
    total_short_pnl = 0.0

    for r in results:
        if r["trades"] == 0:
            continue
        total_long += r["long_trades"]
        total_short += r["short_trades"]
        total_long_pnl += r["long_pnl"]
        total_short_pnl += r["short_pnl"]
        print(f"  {r['symbol']:<7} {r['long_trades']:>6} {r['long_wr']:>7.1f}% {r['long_pnl']:>+10.2f} "
              f"{r['short_trades']:>7} {r['short_wr']:>8.1f}% {r['short_pnl']:>+10.2f}")

    print("  " + "-" * 63)
    print(f"  {'TOTAL':<7} {total_long:>6} {'':>8} {total_long_pnl:>+10.2f} "
          f"{total_short:>7} {'':>9} {total_short_pnl:>+10.2f}")

    # ── TOP / BOTTOM 5 ──
    with_trades = [r for r in results if r["trades"] > 0]
    if len(with_trades) >= 3:
        print("\n  TOP 5 by PnL:")
        for r in with_trades[:5]:
            print(f"    {r['symbol']:<7} {r['total_pnl']:>+10.2f} pts  "
                  f"({r['trades']} trades, {r['win_rate']:.1f}% WR, PF {r['profit_factor']:.2f})")

        print("\n  BOTTOM 5 by PnL:")
        for r in with_trades[-5:][::-1]:
            print(f"    {r['symbol']:<7} {r['total_pnl']:>+10.2f} pts  "
                  f"({r['trades']} trades, {r['win_rate']:.1f}% WR, PF {r['profit_factor']:.2f})")

    # ── EXIT REASON SUMMARY ──
    print("\n  EXIT REASON AVERAGES (across symbols with trades):")
    if with_trades:
        avg_stop = sum(r["exit_stop_pct"] for r in with_trades) / len(with_trades)
        avg_target = sum(r["exit_target_pct"] for r in with_trades) / len(with_trades)
        avg_eod = sum(r["exit_eod_pct"] for r in with_trades) / len(with_trades)
        print(f"    Stop:   {avg_stop:.1f}%")
        print(f"    Target: {avg_target:.1f}%")
        print(f"    EOD:    {avg_eod:.1f}%")

    print("\n" + "=" * 100)


def export_csv(results: list, output_path: Path):
    """Export results to CSV for further analysis."""
    results = [r for r in results if r is not None]
    if not results:
        return

    results.sort(key=lambda r: r["total_pnl"], reverse=True)
    fieldnames = list(results[0].keys())
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    print(f"\nResults exported to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Backtest all watchlist symbols")
    parser.add_argument("--symbols", type=str, default=None,
                        help="Comma-separated symbols (default: all from watchlist.txt)")
    parser.add_argument("--days", type=int, default=30,
                        help="Number of trading days of history (default: 30)")
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD (overrides --days)")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--bar-size", default="5 mins", help="Bar size (default: '5 mins')")
    parser.add_argument("--host", default="127.0.0.1", help="TWS/Gateway host")
    parser.add_argument("--port", type=int, default=7497, help="TWS port (7497=paper, 7496=live)")
    parser.add_argument("--client-id", type=int, default=20,
                        help="IBKR client ID (default: 20, separate from dashboard)")
    parser.add_argument("--min-quality", type=int, default=None, help="Override min_quality")
    parser.add_argument("--min-rr", type=float, default=None, help="Override min_rr")
    parser.add_argument("--slippage", type=float, default=None, help="Override slippage_per_side")
    parser.add_argument("--session-end", type=int, default=1555, help="Session end HHMM (default: 1555)")
    parser.add_argument("--no-download", action="store_true",
                        help="Skip download, use cached CSVs only")
    parser.add_argument("--export", type=str, default=None,
                        help="Export results to CSV (e.g. --export results.csv)")
    args = parser.parse_args()

    # Symbols
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        symbols = load_watchlist()

    print(f"Backtesting {len(symbols)} symbols...")

    # Config
    cfg = OverlayConfig()
    if args.min_quality is not None:
        cfg.min_quality = args.min_quality
    if args.min_rr is not None:
        cfg.min_rr = args.min_rr
    if args.slippage is not None:
        cfg.slippage_per_side = args.slippage

    print(f"Config: min_quality={cfg.min_quality}, min_rr={cfg.min_rr}, "
          f"slippage={cfg.slippage_per_side}, bar_interval={cfg.bar_interval_minutes}min")

    # Date range
    if args.end:
        end_date = datetime.strptime(args.end, "%Y-%m-%d").replace(
            hour=16, minute=0, tzinfo=EASTERN)
    else:
        end_date = datetime.now(EASTERN).replace(hour=16, minute=0, second=0, microsecond=0)

    if args.start:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").replace(
            hour=9, minute=30, tzinfo=EASTERN)
    else:
        start_date = end_date - timedelta(days=int(args.days * 1.5))

    # Download phase
    ib = None
    if not args.no_download:
        try:
            from ib_insync import IB
        except ImportError:
            print("ERROR: ib_insync not installed. Run: pip install ib_insync")
            print("       Or use --no-download to backtest cached CSVs only.")
            sys.exit(1)

        ib = IB()
        print(f"\nConnecting to IBKR at {args.host}:{args.port}...")
        try:
            ib.connect(args.host, args.port, clientId=args.client_id)
        except Exception as e:
            print(f"Connection failed: {e}")
            print("\nMake sure TWS/IB Gateway is running with API enabled.")
            print(f"Port should be {args.port} (7497=paper, 7496=live)")
            print("\nOr use --no-download to backtest existing CSVs in ./data/")
            sys.exit(1)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {args.days} days of {args.bar_size} bars for {len(symbols)} symbols...")
        print(f"Date range: {start_date.date()} to {end_date.date()}")
        print(f"Saving to: {DATA_DIR}/\n")

        for i, sym in enumerate(symbols, 1):
            csv_path = DATA_DIR / f"{sym}_5min.csv"
            # Skip if recently downloaded (within last hour)
            if csv_path.exists():
                age_hours = (time.time() - csv_path.stat().st_mtime) / 3600
                if age_hours < 1:
                    print(f"  [{i}/{len(symbols)}] {sym}: cached (< 1hr old)")
                    continue

            print(f"  [{i}/{len(symbols)}] {sym}: downloading...", end=" ", flush=True)
            result_path = download_symbol(ib, sym, start_date, end_date, args.bar_size)
            if result_path:
                # Count bars
                with open(result_path) as f:
                    bar_count = sum(1 for _ in f) - 1
                print(f"✓ {bar_count} bars")
            else:
                print("✗ no data")

            time.sleep(0.3)  # Extra rate-limit buffer between symbols

        ib.disconnect()
        print("\nDownload complete. Disconnected from IBKR.\n")

    # Backtest phase
    print("Running backtests...\n")
    results = []

    for i, sym in enumerate(symbols, 1):
        csv_path = DATA_DIR / f"{sym}_5min.csv"
        if not csv_path.exists():
            print(f"  [{i}/{len(symbols)}] {sym}: no data file — skipped")
            results.append({
                "symbol": sym, "bars": 0, "days": 0, "signals": 0, "trades": 0,
                "wins": 0, "losses": 0, "win_rate": 0, "profit_factor": 0,
                "total_pnl": 0, "avg_win_r": 0, "avg_loss_r": 0, "max_dd": 0,
                "long_trades": 0, "long_wr": 0, "long_pnl": 0,
                "short_trades": 0, "short_wr": 0, "short_pnl": 0,
                "best_setup": "-", "worst_setup": "-",
                "exit_stop_pct": 0, "exit_target_pct": 0, "exit_eod_pct": 0,
            })
            continue

        print(f"  [{i}/{len(symbols)}] {sym}...", end=" ", flush=True)
        try:
            stats = backtest_symbol(sym, csv_path, cfg, args.session_end)
            if stats:
                results.append(stats)
                if stats["trades"] > 0:
                    print(f"{stats['trades']} trades, {stats['win_rate']:.1f}% WR, "
                          f"{stats['total_pnl']:+.2f} pts")
                else:
                    print(f"{stats['signals']} signals, 0 trades")
            else:
                print("no bars loaded")
        except Exception as e:
            print(f"ERROR: {e}")
            results.append(None)

    # Print summary
    print_summary(results)

    # Export if requested
    if args.export:
        export_csv(results, Path(args.export))


if __name__ == "__main__":
    main()
