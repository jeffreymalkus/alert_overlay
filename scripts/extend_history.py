"""
Extend historical 5-min bars from IBKR backward to build a 6+ month dataset.

Purpose: Validate the locked Gate B long candidate on out-of-sample data.
This script extends EXISTING CSVs backward in time, preserving current data.

How it works:
  1. Reads existing CSV to find the earliest date already on disk
  2. Downloads 5-min bars from IBKR going back to the requested start date
  3. Merges new bars with existing, deduplicating by timestamp
  4. Writes the merged result back to the same CSV

IBKR limits:
  - 5-min bars: max ~180 calendar days lookback (varies by data subscription)
  - Rate limit: max 60 historical data requests per 10 minutes
  - Each day = 1 request; 100 symbols × ~120 new days = ~12,000 requests
  - At 6 req/min with pauses, full run takes ~35-40 minutes

Current data: 2025-12-08 → 2026-03-09 (62 trading days)
Target:       2025-06-01 → 2026-03-09 (~190 trading days, ~9 months)

Usage:
    # Extend all symbols backward to 2025-06-01
    cd alert_overlay
    python extend_history.py --start 2025-06-01

    # Extend just SPY first (test connectivity)
    python extend_history.py --start 2025-06-01 --symbol SPY

    # Dry run (show what would be downloaded)
    python extend_history.py --start 2025-06-01 --dry-run

    # Custom port
    python extend_history.py --start 2025-06-01 --port 7496
"""

import argparse
import csv
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from ib_insync import IB, Stock, util
except ImportError:
    print("ERROR: ib_insync not installed. Run: pip install ib_insync")
    sys.exit(1)

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent.parent / "data"
FIELDNAMES = ["datetime", "open", "high", "low", "close", "volume"]


# ════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════

def load_existing_csv(filepath: Path) -> list:
    """Load existing CSV rows. Returns list of dicts."""
    if not filepath.exists():
        return []
    with open(filepath) as f:
        return list(csv.DictReader(f))


def get_earliest_date(rows: list) -> str:
    """Get earliest date string from CSV rows."""
    if not rows:
        return None
    return min(r["datetime"][:10] for r in rows)


def get_latest_date(rows: list) -> str:
    if not rows:
        return None
    return max(r["datetime"][:10] for r in rows)


def save_csv(rows: list, filepath: Path):
    """Write rows to CSV, sorted by datetime."""
    rows.sort(key=lambda r: r["datetime"])
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def merge_rows(existing: list, new: list) -> list:
    """Merge two row lists, deduplicating by datetime."""
    seen = {}
    for r in existing:
        seen[r["datetime"]] = r
    for r in new:
        if r["datetime"] not in seen:
            seen[r["datetime"]] = r
    return sorted(seen.values(), key=lambda r: r["datetime"])


def load_all_symbols() -> list:
    """Get all symbols that currently have data files."""
    return sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
    ])


def bars_to_rows(ib_bars) -> list:
    """Convert ib_insync BarData list to CSV-ready dicts."""
    rows = []
    for b in ib_bars:
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

        rows.append({
            "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "open": f"{b.open:.4f}",
            "high": f"{b.high:.4f}",
            "low": f"{b.low:.4f}",
            "close": f"{b.close:.4f}",
            "volume": str(int(b.volume)),
        })
    return rows


# ════════════════════════════════════════════════════════════════
#  IBKR download — chunked by week for efficiency
# ════════════════════════════════════════════════════════════════

def download_chunk(ib: IB, contract, end_dt: datetime, duration: str = "1 W") -> list:
    """Download one chunk of historical bars."""
    bars = ib.reqHistoricalData(
        contract,
        endDateTime=end_dt.strftime("%Y%m%d %H:%M:%S") + " US/Eastern",
        durationStr=duration,
        barSizeSetting="5 mins",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
    )
    return bars_to_rows(bars)


def download_range_chunked(ib: IB, contract, start_date: datetime,
                           end_date: datetime, request_tracker: dict) -> list:
    """
    Download bars from start_date to end_date using monthly chunks.

    IBKR supports "1 M" (1 month) duration for 5-min bars, which is
    much more efficient than weekly: ~7 requests per symbol for 6 months
    instead of ~27.
    """
    all_rows = []
    seen = set()
    current_end = end_date

    while current_end.date() >= start_date.date():
        # Rate limit check
        request_tracker["count"] += 1
        if request_tracker["count"] % 55 == 0:
            elapsed = time.time() - request_tracker["batch_start"]
            if elapsed < 600:
                wait = max(600 - elapsed + 10, 10)
                print(f"      Rate limit pause: {wait:.0f}s (sent {request_tracker['count']} requests)...")
                time.sleep(wait)
            request_tracker["batch_start"] = time.time()
            request_tracker["count"] = 0

        try:
            rows = download_chunk(ib, contract, current_end, "1 M")
            new_rows = []
            for r in rows:
                if r["datetime"] not in seen:
                    seen.add(r["datetime"])
                    new_rows.append(r)

            if new_rows:
                all_rows.extend(new_rows)
                earliest = new_rows[0]["datetime"][:10]
                latest = new_rows[-1]["datetime"][:10]
                print(f"      chunk {earliest} → {latest}: {len(new_rows)} bars")

            # Move back one month
            current_end -= timedelta(days=30)
            time.sleep(1.0)  # Pause between requests

        except Exception as e:
            err_str = str(e)
            if "pacing" in err_str.lower() or "162" in err_str:
                print(f"      Pacing violation — waiting 60s...")
                time.sleep(60)
                continue
            elif "no data" in err_str.lower() or "HMDS query" in err_str:
                print(f"      No data available for this period, moving back...")
                current_end -= timedelta(days=30)
                time.sleep(1)
            else:
                print(f"      Error: {e}")
                current_end -= timedelta(days=30)
                time.sleep(2)

    return sorted(all_rows, key=lambda r: r["datetime"])


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Extend IBKR 5-min bar history backward")
    parser.add_argument("--start", required=True,
                        help="Target start date YYYY-MM-DD (e.g. 2025-06-01)")
    parser.add_argument("--symbol", type=str, default=None,
                        help="Single symbol to extend (default: all symbols with existing data)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497,
                        help="TWS port (7497=paper, 7496=live)")
    parser.add_argument("--client-id", type=int, default=11,
                        help="Client ID (default 11 to avoid conflict with dashboard)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show plan without downloading")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if start date is already covered")
    args = parser.parse_args()

    target_start = datetime.strptime(args.start, "%Y-%m-%d").replace(
        hour=9, minute=30, tzinfo=EASTERN
    )

    # Determine symbols to process
    if args.symbol:
        symbols = [args.symbol.upper()]
    else:
        symbols = load_all_symbols()

    if not symbols:
        print("No symbols found in data directory.")
        sys.exit(1)

    print(f"{'='*70}")
    print("EXTEND HISTORY — Download older 5-min bars from IBKR")
    print(f"{'='*70}")
    print(f"  Target start:  {args.start}")
    print(f"  Symbols:       {len(symbols)}")
    print(f"  Data dir:      {DATA_DIR}")
    print()

    # Analyze what needs downloading
    plan = []
    skip = []
    for sym in symbols:
        csv_path = DATA_DIR / f"{sym}_5min.csv"
        existing = load_existing_csv(csv_path)
        earliest = get_earliest_date(existing)
        latest = get_latest_date(existing)

        if earliest and earliest <= args.start and not args.force:
            skip.append((sym, earliest, latest, len(existing)))
        else:
            # Need to download from target_start to (earliest - 1 day) or to target_start
            if earliest:
                dl_end = datetime.strptime(earliest, "%Y-%m-%d").replace(
                    hour=16, minute=0, tzinfo=EASTERN
                ) - timedelta(days=1)
            else:
                dl_end = datetime.now(EASTERN).replace(hour=16, minute=0, second=0)

            plan.append({
                "symbol": sym,
                "existing_start": earliest,
                "existing_end": latest,
                "existing_bars": len(existing),
                "download_end": dl_end,
                "csv_path": csv_path,
            })

    print(f"  Already covered:  {len(skip)} symbols")
    print(f"  Need extension:   {len(plan)} symbols")

    if skip:
        print(f"\n  Skipping (already have data from {args.start} or earlier):")
        for sym, earliest, latest, nbars in skip[:10]:
            print(f"    {sym:6s}: {earliest} → {latest} ({nbars} bars)")
        if len(skip) > 10:
            print(f"    ... and {len(skip) - 10} more")

    if plan:
        # Estimate time (monthly chunks: ~1 request per month per symbol)
        months_per_sym = max(1, (plan[0]["download_end"] - target_start).days // 30 + 1)
        total_requests = len(plan) * months_per_sym
        est_minutes = total_requests * 1.5 / 60  # ~1.5s per request average with pauses
        rate_pauses = total_requests // 55
        est_minutes += rate_pauses * 10

        print(f"\n  Download plan:")
        print(f"    Symbols to extend: {len(plan)}")
        print(f"    Months per symbol: ~{months_per_sym}")
        print(f"    Est. requests:     ~{total_requests}")
        print(f"    Est. time:         ~{est_minutes:.0f} minutes")
        print(f"\n  First 10 symbols:")
        for item in plan[:10]:
            sym = item["symbol"]
            ex_s = item["existing_start"] or "N/A"
            ex_e = item["existing_end"] or "N/A"
            print(f"    {sym:6s}: existing {ex_s} → {ex_e} ({item['existing_bars']} bars), "
                  f"will download {args.start} → {item['download_end'].strftime('%Y-%m-%d')}")
        if len(plan) > 10:
            print(f"    ... and {len(plan) - 10} more")

    if args.dry_run:
        print(f"\n  [DRY RUN] — no connection made.")
        return

    if not plan:
        print("\n  Nothing to download. All symbols already have data from the target start date.")
        return

    # Connect
    ib = IB()
    print(f"\n  Connecting to IBKR at {args.host}:{args.port} (clientId={args.client_id})...")
    try:
        ib.connect(args.host, args.port, clientId=args.client_id)
    except Exception as e:
        print(f"\n  Connection failed: {e}")
        print(f"\n  Make sure TWS or IB Gateway is running with API connections enabled.")
        print(f"  TWS: File → Global Configuration → API → Settings")
        print(f"    ✓ Enable ActiveX and Socket Clients")
        print(f"    ✓ Socket port = {args.port}")
        print(f"    ✓ Trusted IPs includes 127.0.0.1")
        print(f"\n  Common ports: 7497 (paper TWS), 7496 (live TWS), 4002 (paper Gateway), 4001 (live Gateway)")
        sys.exit(1)

    print("  Connected.\n")

    request_tracker = {"count": 0, "batch_start": time.time()}
    success = []
    failed = []

    for i, item in enumerate(plan):
        sym = item["symbol"]
        csv_path = item["csv_path"]
        dl_end = item["download_end"]

        print(f"  [{i+1}/{len(plan)}] {sym} — extending back to {args.start}...")

        try:
            contract = Stock(sym, "SMART", "USD")
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                print(f"    SKIP: Could not qualify {sym}")
                failed.append((sym, "contract not found"))
                continue

            new_rows = download_range_chunked(
                ib, contract, target_start, dl_end, request_tracker
            )

            if not new_rows:
                print(f"    SKIP: No new bars for {sym}")
                failed.append((sym, "no data returned"))
                continue

            # Merge with existing
            existing = load_existing_csv(csv_path)
            merged = merge_rows(existing, new_rows)
            save_csv(merged, csv_path)

            new_start = get_earliest_date(merged)
            new_end = get_latest_date(merged)
            days = len(set(r["datetime"][:10] for r in merged))

            print(f"    OK: {len(new_rows)} new bars + {len(existing)} existing = "
                  f"{len(merged)} total ({days} days: {new_start} → {new_end})")
            success.append(sym)

            time.sleep(1)  # Pause between symbols

        except Exception as e:
            print(f"    ERROR: {e}")
            failed.append((sym, str(e)))
            time.sleep(3)

    ib.disconnect()
    print(f"\n{'='*70}")
    print("EXTEND HISTORY COMPLETE")
    print(f"{'='*70}")
    print(f"  Extended:  {len(success)} symbols")
    print(f"  Failed:    {len(failed)} symbols")

    if failed:
        print(f"\n  Failed symbols:")
        for sym, reason in failed:
            print(f"    {sym}: {reason}")

    if success:
        # Verify date range of SPY as reference
        spy_rows = load_existing_csv(DATA_DIR / "SPY_5min.csv")
        if spy_rows:
            spy_start = get_earliest_date(spy_rows)
            spy_end = get_latest_date(spy_rows)
            spy_days = len(set(r["datetime"][:10] for r in spy_rows))
            print(f"\n  SPY reference: {spy_start} → {spy_end} ({spy_days} trading days)")

    print(f"\n  Next step: re-run the locked regime-gated study on the expanded dataset.")
    print(f"    cd /sessions/inspiring-clever-meitner/mnt")
    print(f"    python -c \"from alert_overlay.regime_gated_long_study import main; main()\"")


if __name__ == "__main__":
    main()
