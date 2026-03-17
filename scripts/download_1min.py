"""
Download 1-minute historical bars from IBKR for EMA_RECLAIM validation.

1-minute data is larger and IBKR is stricter about it:
  - Max duration per request: "1 D" for 1-min bars
  - Rate limit: 60 requests per 10 minutes (same)
  - Data availability: ~30 calendar days back for 1-min

This script downloads 1-min bars and saves them alongside the 5-min CSVs.
By default it only downloads symbols that had EMA_RECLAIM trades in backtest.

Usage:
    # Download 1-min for all symbols with EMA_RECLAIM trades
    python -m alert_overlay.download_1min --port 7497

    # Download 1-min for specific symbols
    python -m alert_overlay.download_1min --symbols AAPL TSLA NVDA --port 7497

    # Download 1-min for ALL watchlist symbols (large)
    python -m alert_overlay.download_1min --all --port 7497

    # Dry run to see what would be downloaded
    python -m alert_overlay.download_1min --dry-run
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from ib_insync import IB, Stock, util
except ImportError:
    print("ERROR: ib_insync not installed. Run: pip install ib_insync")
    sys.exit(1)

try:
    from .download_data import download_range, save_csv
except ImportError:
    from download_data import download_range, save_csv

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_1MIN_DIR = DATA_DIR / "1min"


def get_ema_reclaim_symbols():
    """
    Run a quick backtest scan to find which symbols produced EMA_RECLAIM trades.
    Returns list of unique symbols.
    """
    from .backtest import load_bars_from_csv, run_backtest
    from .config import OverlayConfig
    from .models import SetupId
    from .market_context import get_sector_etf, SECTOR_MAP

    cfg = OverlayConfig()
    cfg.show_reversal_setups = False
    cfg.show_trend_setups = False
    cfg.show_ema_retest = False
    cfg.show_ema_mean_rev = False
    cfg.show_ema_pullback = False
    cfg.show_second_chance = False
    cfg.show_spencer = False
    cfg.show_failed_bounce = False
    cfg.show_ema_scalp = True
    cfg.show_ema_confirm = False

    # Load market data for context
    spy_bars = None
    qqq_bars = None
    sector_bars_dict = {}
    spy_path = DATA_DIR / "SPY_5min.csv"
    qqq_path = DATA_DIR / "QQQ_5min.csv"
    if spy_path.exists():
        spy_bars = load_bars_from_csv(str(spy_path))
    if qqq_path.exists():
        qqq_bars = load_bars_from_csv(str(qqq_path))
    sector_etfs = set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    symbols_with_trades = set()

    for f in sorted(os.listdir(DATA_DIR)):
        if not f.endswith('_5min.csv'):
            continue
        sym = f.replace('_5min.csv', '')
        bars = load_bars_from_csv(str(DATA_DIR / f))
        if not bars:
            continue

        kw = {"cfg": cfg}
        if spy_bars:
            kw["spy_bars"] = spy_bars
            kw["qqq_bars"] = qqq_bars
            sec_etf = get_sector_etf(sym)
            if sec_etf and sec_etf in sector_bars_dict:
                kw["sector_bars"] = sector_bars_dict[sec_etf]

        r = run_backtest(bars, **kw)
        for t in r.trades:
            if t.signal.setup_id == SetupId.EMA_RECLAIM:
                symbols_with_trades.add(sym)
                break

    # Always include SPY and QQQ for market context 1-min
    symbols_with_trades.add("SPY")
    symbols_with_trades.add("QQQ")

    return sorted(symbols_with_trades)


def main():
    parser = argparse.ArgumentParser(description="Download IBKR 1-minute bars for EMA validation")
    parser.add_argument("--symbols", nargs="+", help="Specific symbols to download")
    parser.add_argument("--all", action="store_true",
                        help="Download ALL symbols from watchlist (not just EMA_RECLAIM)")
    parser.add_argument("--days", type=int, default=30,
                        help="Trading days back (default: 30)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497)
    parser.add_argument("--client-id", type=int, default=11,
                        help="IBKR client ID (default: 11, different from dashboard)")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if 1-min CSV exists")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be downloaded")
    args = parser.parse_args()

    # Determine symbol list
    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
        print(f"Symbols from CLI: {len(symbols)}")
    elif args.all:
        wl_path = Path(__file__).parent.parent / "watchlist_expanded.txt"
        if not wl_path.exists():
            wl_path = Path(__file__).parent.parent / "watchlist.txt"
        with open(wl_path) as f:
            symbols = [l.strip().upper() for l in f if l.strip() and not l.strip().startswith("#")]
        # Always include SPY/QQQ
        for s in ["SPY", "QQQ"]:
            if s not in symbols:
                symbols.append(s)
        print(f"All watchlist symbols: {len(symbols)}")
    else:
        print("Scanning for symbols with EMA_RECLAIM trades...")
        symbols = get_ema_reclaim_symbols()
        print(f"Found {len(symbols)} symbols with EMA_RECLAIM trades (+ SPY/QQQ)")

    # Check which already exist
    DATA_1MIN_DIR.mkdir(parents=True, exist_ok=True)
    to_download = []
    already_have = []
    for sym in symbols:
        csv_path = DATA_1MIN_DIR / f"{sym}_1min.csv"
        if csv_path.exists() and not args.force:
            already_have.append(sym)
        else:
            to_download.append(sym)

    print(f"\nAlready have 1-min data: {len(already_have)}")
    print(f"Need to download:       {len(to_download)}")

    if to_download:
        print(f"Symbols: {', '.join(to_download)}")

    if not to_download:
        print("Nothing to download.")
        return

    if args.dry_run:
        print("\n[DRY RUN] — no connection made.")
        # Show estimated time
        est_requests = len(to_download) * (args.days + 5)
        est_minutes = est_requests / 6  # ~6 requests per minute to be safe
        print(f"Estimated requests: ~{est_requests}")
        print(f"Estimated time:     ~{est_minutes:.0f} minutes")
        return

    # Date range
    end_date = datetime.now(EASTERN).replace(hour=16, minute=0, second=0, microsecond=0)
    start_date = end_date - timedelta(days=int(args.days * 1.5))

    # Connect
    ib = IB()
    print(f"\nConnecting to IBKR at {args.host}:{args.port} (clientId={args.client_id})...")
    try:
        ib.connect(args.host, args.port, clientId=args.client_id)
    except Exception as e:
        print(f"Connection failed: {e}")
        print("Make sure TWS or IB Gateway is running with API enabled.")
        sys.exit(1)

    print(f"Connected. Downloading 1-min bars for {len(to_download)} symbols...\n")

    success = []
    failed = []
    request_count = 0
    batch_start = time.time()

    for i, sym in enumerate(to_download):
        print(f"[{i+1}/{len(to_download)}] {sym}...")

        # Rate limit check
        if request_count > 0 and request_count % 45 == 0:
            elapsed = time.time() - batch_start
            if elapsed < 600:
                wait = 600 - elapsed + 10
                print(f"  Rate limit pause: {wait:.0f}s...")
                time.sleep(wait)
                batch_start = time.time()
                request_count = 0

        try:
            contract = Stock(sym, "SMART", "USD")
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                print(f"  SKIP: Could not qualify {sym}")
                failed.append((sym, "contract not found"))
                continue

            rows = download_range(ib, contract, start_date, end_date, bar_size="1 min")
            request_count += args.days + 5

            if rows:
                csv_path = DATA_1MIN_DIR / f"{sym}_1min.csv"
                save_csv(rows, str(csv_path))
                days = len(set(r["datetime"][:10] for r in rows))
                print(f"  OK: {len(rows)} bars, {days} days")
                success.append(sym)
            else:
                print(f"  SKIP: No bars returned for {sym}")
                failed.append((sym, "no data"))

            time.sleep(1.5)  # slightly more cautious for 1-min

        except Exception as e:
            print(f"  ERROR: {e}")
            failed.append((sym, str(e)))
            time.sleep(3)

    ib.disconnect()

    print(f"\n{'='*60}")
    print(f"1-MINUTE DATA DOWNLOAD COMPLETE")
    print(f"{'='*60}")
    print(f"  Success:    {len(success)}")
    print(f"  Failed:     {len(failed)}")
    print(f"  Data dir:   {DATA_1MIN_DIR}")
    if failed:
        print(f"\n  Failed:")
        for sym, reason in failed:
            print(f"    {sym}: {reason}")

    total = len(already_have) + len(success)
    print(f"\n  Total symbols with 1-min data: {total}")


if __name__ == "__main__":
    main()
