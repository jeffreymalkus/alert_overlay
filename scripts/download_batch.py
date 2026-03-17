"""
Batch download historical 5-min bars from IBKR for all symbols in a watchlist.

Connects to TWS or IB Gateway, pulls 30 days of 5-min bars for each symbol
that doesn't already have a CSV in ./data/, and saves them.

Respects IBKR rate limits (max 60 requests per 10 minutes).

Usage:
    python -m alert_overlay.download_batch --watchlist watchlist_expanded.txt
    python -m alert_overlay.download_batch --watchlist watchlist_expanded.txt --days 30
    python -m alert_overlay.download_batch --watchlist watchlist_expanded.txt --force
"""

import argparse
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


def load_watchlist(filepath: str) -> list:
    """Load symbols from watchlist file, skipping comments and blanks."""
    symbols = []
    seen = set()
    with open(filepath) as f:
        for line in f:
            s = line.strip().upper()
            if s and not s.startswith("#") and s not in seen:
                seen.add(s)
                symbols.append(s)
    return symbols


def main():
    parser = argparse.ArgumentParser(description="Batch download IBKR 5-min bars")
    parser.add_argument("--watchlist", default="watchlist_expanded.txt",
                        help="Watchlist file (default: watchlist_expanded.txt)")
    parser.add_argument("--days", type=int, default=30,
                        help="Trading days to download (default: 30)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497,
                        help="TWS port (7497=paper, 7496=live)")
    parser.add_argument("--client-id", type=int, default=10)
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if CSV exists")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be downloaded without connecting")
    args = parser.parse_args()

    watchlist_path = Path(__file__).parent.parent / args.watchlist
    symbols = load_watchlist(str(watchlist_path))
    print(f"Loaded {len(symbols)} symbols from {args.watchlist}")

    # Determine which need downloading
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    to_download = []
    already_have = []
    for sym in symbols:
        csv_path = DATA_DIR / f"{sym}_5min.csv"
        if csv_path.exists() and not args.force:
            already_have.append(sym)
        else:
            to_download.append(sym)

    print(f"Already have data: {len(already_have)}")
    print(f"Need to download:  {len(to_download)}")

    if not to_download:
        print("Nothing to download.")
        return

    print(f"\nWill download: {', '.join(to_download)}")

    if args.dry_run:
        print("\n[DRY RUN] — no connection made.")
        return

    # Date range
    end_date = datetime.now(EASTERN).replace(hour=16, minute=0, second=0, microsecond=0)
    start_date = end_date - timedelta(days=int(args.days * 1.5))  # pad for weekends

    # Connect
    ib = IB()
    print(f"\nConnecting to IBKR at {args.host}:{args.port}...")
    try:
        ib.connect(args.host, args.port, clientId=args.client_id)
    except Exception as e:
        print(f"Connection failed: {e}")
        print("Make sure TWS or IB Gateway is running with API enabled.")
        sys.exit(1)

    print(f"Connected. Downloading {len(to_download)} symbols...\n")

    success = []
    failed = []
    request_count = 0
    batch_start = time.time()

    for i, sym in enumerate(to_download):
        print(f"[{i+1}/{len(to_download)}] {sym}...")

        # IBKR rate limit: 60 requests per 10 minutes
        # Each symbol uses ~30 requests (1 per day for 30 days)
        # Be conservative: pause between symbols
        if request_count > 0 and request_count % 50 == 0:
            elapsed = time.time() - batch_start
            if elapsed < 600:  # less than 10 min
                wait = 600 - elapsed + 5
                print(f"  Rate limit pause: {wait:.0f}s...")
                time.sleep(wait)
                batch_start = time.time()
                request_count = 0

        try:
            contract = Stock(sym, "SMART", "USD")
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                print(f"  SKIP: Could not qualify contract for {sym}")
                failed.append((sym, "contract not found"))
                continue

            rows = download_range(ib, contract, start_date, end_date)
            request_count += args.days + 5  # approximate requests used

            if rows:
                csv_path = DATA_DIR / f"{sym}_5min.csv"
                save_csv(rows, str(csv_path))
                days = len(set(r["datetime"][:10] for r in rows))
                print(f"  OK: {len(rows)} bars, {days} days")
                success.append(sym)
            else:
                print(f"  SKIP: No bars returned for {sym}")
                failed.append((sym, "no data"))

            # Brief pause between symbols
            time.sleep(1)

        except Exception as e:
            print(f"  ERROR: {e}")
            failed.append((sym, str(e)))
            time.sleep(2)

    ib.disconnect()

    # Summary
    print(f"\n{'='*50}")
    print(f"BATCH DOWNLOAD COMPLETE")
    print(f"{'='*50}")
    print(f"  Success: {len(success)}")
    print(f"  Failed:  {len(failed)}")
    if failed:
        print(f"\n  Failed symbols:")
        for sym, reason in failed:
            print(f"    {sym}: {reason}")

    total_have = len(already_have) + len(success)
    print(f"\n  Total symbols with data: {total_have} / {len(symbols)}")


if __name__ == "__main__":
    main()
