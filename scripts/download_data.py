"""
Download historical intraday bars from IBKR and save as CSV.

Connects to TWS or IB Gateway, pulls 5-min bars for the specified
symbol and date range, and writes a CSV compatible with backtest.py.

Requirements:
    pip install ib_insync

Usage:
    python -m alert_overlay.download_data \
        --symbol SPY \
        --days 30 \
        --port 7497

    # Custom range
    python -m alert_overlay.download_data \
        --symbol SPY \
        --start 2025-01-02 \
        --end 2025-03-01 \
        --port 7497
"""

import argparse
import csv
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

EASTERN = ZoneInfo("US/Eastern")


def download_bars(ib: IB, contract, end_dt: datetime, duration: str,
                  bar_size: str = "5 mins") -> list:
    """Pull historical bars from IBKR for one chunk."""
    bars = ib.reqHistoricalData(
        contract,
        endDateTime=end_dt.strftime("%Y%m%d %H:%M:%S") + " US/Eastern",
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow="TRADES",
        useRTH=True,           # regular trading hours only
        formatDate=1,          # string format YYYYMMDD HH:MM:SS
    )
    return bars


def bars_to_rows(ib_bars) -> list:
    """Convert ib_insync BarData list to CSV-ready dicts."""
    rows = []
    for b in ib_bars:
        # Parse the date string from IBKR
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
            # datetime object
            if b.date.tzinfo is not None:
                dt = b.date.astimezone(EASTERN)
            else:
                dt = b.date.replace(tzinfo=EASTERN)

        rows.append({
            "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "open": f"{b.open:.4f}",
            "high": f"{b.high:.4f}",
            "low": f"{b.low:.4f}",
            "close": f"{b.close:.4f}",
            "volume": str(int(b.volume)),
        })
    return rows


def download_range(ib: IB, contract, start_date: datetime, end_date: datetime,
                   bar_size: str = "5 mins") -> list:
    """
    Download bars across a date range by chunking into 1-day requests.

    IBKR limits intraday bar requests to specific durations. For 5-min bars,
    we request 1 day at a time and stitch the results together.
    """
    all_rows = []
    current = end_date
    seen_dates = set()

    print(f"Downloading {contract.symbol} from {start_date.date()} to {end_date.date()}...")

    while current.date() >= start_date.date():
        try:
            bars = download_bars(ib, contract, current, "1 D", bar_size)
            rows = bars_to_rows(bars)

            # Deduplicate (overlapping edges between chunks)
            new_rows = []
            for r in rows:
                key = r["datetime"]
                if key not in seen_dates:
                    seen_dates.add(key)
                    new_rows.append(r)

            if new_rows:
                all_rows.extend(new_rows)
                earliest = new_rows[0]["datetime"][:10]
                latest = new_rows[-1]["datetime"][:10]
                print(f"  {earliest} → {latest}: {len(new_rows)} bars")

            # Move back one trading day
            current -= timedelta(days=1)
            # Skip weekends
            while current.weekday() >= 5:
                current -= timedelta(days=1)

            # IBKR rate limit: max 60 requests per 10 minutes
            time.sleep(0.5)

        except Exception as e:
            print(f"  Error on {current.date()}: {e}")
            current -= timedelta(days=1)
            time.sleep(2)

    # Sort chronologically
    all_rows.sort(key=lambda r: r["datetime"])
    return all_rows


def save_csv(rows: list, filepath: str):
    """Write rows to CSV."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["datetime", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nSaved {len(rows)} bars to {path}")


def main():
    parser = argparse.ArgumentParser(description="Download IBKR historical bars to CSV")
    parser.add_argument("--symbol", default="SPY", help="Ticker symbol (default: SPY)")
    parser.add_argument("--exchange", default="SMART", help="Exchange (default: SMART)")
    parser.add_argument("--currency", default="USD", help="Currency (default: USD)")
    parser.add_argument("--days", type=int, default=30, help="Number of trading days back (default: 30)")
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD (overrides --days)")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--bar-size", default="5 mins", help="Bar size (default: '5 mins')")
    parser.add_argument("--host", default="127.0.0.1", help="TWS/Gateway host")
    parser.add_argument("--port", type=int, default=7497, help="TWS port (7497=paper, 7496=live)")
    parser.add_argument("--client-id", type=int, default=10, help="IBKR client ID")
    parser.add_argument("--output", type=str, help="Output CSV path (default: ./data/{symbol}_5min.csv)")
    args = parser.parse_args()

    # Date range
    if args.end:
        end_date = datetime.strptime(args.end, "%Y-%m-%d").replace(
            hour=16, minute=0, tzinfo=EASTERN
        )
    else:
        end_date = datetime.now(EASTERN).replace(hour=16, minute=0, second=0, microsecond=0)

    if args.start:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").replace(
            hour=9, minute=30, tzinfo=EASTERN
        )
    else:
        start_date = end_date - timedelta(days=int(args.days * 1.5))  # pad for weekends/holidays

    # Output path
    output = args.output or f"./data/{args.symbol}_5min.csv"

    # Connect
    ib = IB()
    print(f"Connecting to IBKR at {args.host}:{args.port}...")
    try:
        ib.connect(args.host, args.port, clientId=args.client_id)
    except Exception as e:
        print(f"Connection failed: {e}")
        print("\nMake sure TWS or IB Gateway is running and API connections are enabled:")
        print("  TWS: File → Global Configuration → API → Settings → Enable ActiveX and Socket Clients")
        print(f"  Port should be {args.port} (7497=paper, 7496=live)")
        sys.exit(1)

    contract = Stock(args.symbol, args.exchange, args.currency)
    ib.qualifyContracts(contract)

    try:
        rows = download_range(ib, contract, start_date, end_date, args.bar_size)
        if rows:
            save_csv(rows, output)
            print(f"\nDate range: {rows[0]['datetime'][:10]} to {rows[-1]['datetime'][:10]}")
            print(f"Trading days: ~{len(set(r['datetime'][:10] for r in rows))}")
            print(f"Total bars: {len(rows)}")
        else:
            print("No bars returned. Check symbol/date range/market data subscription.")
    finally:
        ib.disconnect()
        print("Disconnected from IBKR.")


if __name__ == "__main__":
    main()
