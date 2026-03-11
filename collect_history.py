"""
IBKR Historical Data Collector — Pull extended 5-min bar history.

Connects to TWS/Gateway, downloads 5-min bars for all watchlist + sector ETF
symbols, and merges with existing CSV data (no duplicates).

IBKR pacing rules (as of 2025):
  - Max 60 historical data requests in 10 minutes
  - Each request can pull up to 1 trading week of 5-min bars efficiently
  - For longer durations, we chunk into ~5-day requests

Usage:
    # Pull 3 months of history for all symbols (default)
    python3 -m alert_overlay.collect_history

    # Pull specific duration
    python3 -m alert_overlay.collect_history --months 4

    # Pull only specific symbols
    python3 -m alert_overlay.collect_history --symbols SPY QQQ AAPL

    # Dry run — show what would be fetched
    python3 -m alert_overlay.collect_history --dry-run

    # Use live port (default is paper 7497)
    python3 -m alert_overlay.collect_history --port 7496
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Set

from ib_insync import IB, Stock, Contract, util

# ── Constants ────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_PORT = 7497        # Paper trading; use 7496 for live
DEFAULT_CLIENT_ID = 20     # Separate from dashboard (client_id=1)
BAR_SIZE = "5 mins"
WHAT_TO_SHOW = "TRADES"
USE_RTH = True             # Regular trading hours only

# IBKR pacing: max 60 requests / 10 min ≈ 1 request per 10 sec
# We use 11 sec between requests for safety margin
PACING_DELAY = 11

# Maximum duration per request for 5-min bars: "1 W" is efficient
# For 1-min bars IBKR allows max "1 D" per request, so we chunk daily
CHUNK_DURATION = "1 W"     # 1 trading week per request (5-min)
CHUNK_DURATION_1MIN = "1 D"  # 1 day per request (1-min)

# Bar size config: suffix for filenames and IBKR request
BAR_CONFIGS = {
    "5min": {"bar_size": "5 mins", "chunk": "1 W", "suffix": "5min", "subdir": ""},
    "1min": {"bar_size": "1 min",  "chunk": "1 D", "suffix": "1min", "subdir": "1min"},
}

# ── Symbols ──────────────────────────────────────────────────────────

# Sector ETFs referenced by SECTOR_MAP (some missing from watchlist)
SECTOR_ETFS = [
    "XLK", "XLC", "XLY", "XLF", "XLV", "XLE", "XLI",
    "XLP", "XLB", "XLU", "XLRE",
]

# Market ETFs
MARKET_ETFS = ["SPY", "QQQ"]


def load_watchlist() -> List[str]:
    """Load symbols from watchlist.txt."""
    wl_path = Path(__file__).parent / "watchlist.txt"
    symbols = []
    if wl_path.exists():
        for line in wl_path.read_text().splitlines():
            sym = line.strip()
            if sym and not sym.startswith("#"):
                symbols.append(sym)
    return symbols


def get_all_symbols() -> List[str]:
    """Deduplicated list: market + sector ETFs + watchlist."""
    seen: Set[str] = set()
    ordered = []
    # Priority order: market ETFs first, then sector, then watchlist
    for sym in MARKET_ETFS + SECTOR_ETFS + load_watchlist():
        if sym not in seen:
            seen.add(sym)
            ordered.append(sym)
    return ordered


# ── CSV I/O ──────────────────────────────────────────────────────────

def _csv_path(symbol: str, bar_cfg: dict) -> Path:
    """Get CSV path for a symbol + bar config."""
    subdir = bar_cfg.get("subdir", "")
    suffix = bar_cfg["suffix"]
    base = DATA_DIR / subdir if subdir else DATA_DIR
    return base / f"{symbol}_{suffix}.csv"


def load_existing_csv(symbol: str, bar_cfg: Optional[dict] = None) -> dict:
    """
    Load existing CSV data as {datetime_str: row_dict}.
    Returns empty dict if file doesn't exist.
    """
    if bar_cfg is None:
        bar_cfg = BAR_CONFIGS["5min"]
    path = _csv_path(symbol, bar_cfg)
    rows = {}
    if not path.exists():
        return rows
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows[row["datetime"]] = row
    return rows


def get_existing_date_range(symbol: str, bar_cfg: Optional[dict] = None) -> tuple:
    """Return (earliest_date, latest_date) from existing CSV, or (None, None)."""
    if bar_cfg is None:
        bar_cfg = BAR_CONFIGS["5min"]
    path = _csv_path(symbol, bar_cfg)
    if not path.exists():
        return None, None

    first_dt = last_dt = None
    with open(path, "r") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return None, None
        for row in reader:
            if row:
                dt_str = row[0]
                if first_dt is None:
                    first_dt = dt_str
                last_dt = dt_str

    return first_dt, last_dt


def write_merged_csv(symbol: str, rows: dict, bar_cfg: Optional[dict] = None):
    """Write merged rows to CSV, sorted by datetime."""
    if bar_cfg is None:
        bar_cfg = BAR_CONFIGS["5min"]
    path = _csv_path(symbol, bar_cfg)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Sort by datetime string (works because format is YYYY-MM-DD HH:MM:SS)
    sorted_dts = sorted(rows.keys())

    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["datetime", "open", "high", "low", "close", "volume"])
        for dt in sorted_dts:
            r = rows[dt]
            writer.writerow([
                r["datetime"], r["open"], r["high"], r["low"],
                r["close"], r["volume"],
            ])


# ── IBKR Connection ─────────────────────────────────────────────────

def connect_ibkr(port: int = DEFAULT_PORT, client_id: int = DEFAULT_CLIENT_ID) -> IB:
    """Connect to TWS/Gateway."""
    ib = IB()
    ib.connect("127.0.0.1", port, clientId=client_id)
    print(f"Connected to IBKR on port {port}, clientId={client_id}")
    return ib


def make_contract(symbol: str) -> Contract:
    """Create stock/ETF contract. Handles most US-listed securities."""
    contract = Stock(symbol, "SMART", "USD")
    return contract


# ── Data Fetching ────────────────────────────────────────────────────

def fetch_bars_chunk(
    ib: IB,
    contract: Contract,
    end_dt: datetime,
    duration: str = CHUNK_DURATION,
    bar_size: str = BAR_SIZE,
) -> list:
    """
    Fetch one chunk of historical bars.

    Args:
        ib: Connected IB instance
        contract: IBKR contract
        end_dt: End datetime for the request
        duration: Duration string (e.g., "1 W" or "1 D")
        bar_size: Bar size string (e.g., "5 mins" or "1 min")

    Returns:
        List of BarData objects, or empty list on error
    """
    end_str = end_dt.strftime("%Y%m%d %H:%M:%S") + " US/Eastern"
    try:
        bars = ib.reqHistoricalData(
            contract,
            endDateTime=end_str,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=WHAT_TO_SHOW,
            useRTH=USE_RTH,
            formatDate=1,  # String format
        )
        return bars if bars else []
    except Exception as e:
        print(f"    Error fetching {contract.symbol} ending {end_str}: {e}")
        return []


def bars_to_rows(bars: list) -> dict:
    """Convert BarData list to {datetime_str: row_dict}."""
    rows = {}
    for bar in bars:
        # ib_insync returns bar.date as datetime object when formatDate=1
        if hasattr(bar.date, "strftime"):
            dt_str = bar.date.strftime("%Y-%m-%d %H:%M:%S")
        else:
            dt_str = str(bar.date)

        rows[dt_str] = {
            "datetime": dt_str,
            "open": f"{bar.open:.4f}",
            "high": f"{bar.high:.4f}",
            "low": f"{bar.low:.4f}",
            "close": f"{bar.close:.4f}",
            "volume": str(int(bar.volume)),
        }
    return rows


def fetch_symbol_history(
    ib: IB,
    symbol: str,
    target_start: datetime,
    request_count: int,
    bar_cfg: Optional[dict] = None,
) -> tuple:
    """
    Fetch full history for one symbol from target_start to now.

    Walks backward from now in weekly chunks until we pass target_start
    or get empty responses.

    Returns:
        (new_rows_dict, updated_request_count)
    """
    contract = make_contract(symbol)

    # Qualify the contract first
    try:
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            print(f"  {symbol}: Could not qualify contract — skipping")
            return {}, request_count
    except Exception as e:
        print(f"  {symbol}: Qualify error: {e} — skipping")
        return {}, request_count

    if bar_cfg is None:
        bar_cfg = BAR_CONFIGS["5min"]
    chunk_dur = bar_cfg["chunk"]
    bar_size_str = bar_cfg["bar_size"]
    # For 1-min bars, step back by 1 day; for 5-min, by 7 days
    step_back_days = 1 if "1 min" in bar_size_str else 7

    all_rows = {}
    end_dt = datetime.now()  # Start from now, walk backward
    empty_streak = 0
    chunks_fetched = 0

    while end_dt > target_start:
        # Pacing delay
        time.sleep(PACING_DELAY)
        request_count += 1

        bars = fetch_bars_chunk(ib, contract, end_dt, duration=chunk_dur, bar_size=bar_size_str)

        if not bars:
            empty_streak += 1
            if empty_streak >= 2:
                break  # No more data available
            # Move back anyway
            end_dt -= timedelta(days=step_back_days)
            continue

        empty_streak = 0
        chunk_rows = bars_to_rows(bars)
        all_rows.update(chunk_rows)
        chunks_fetched += 1

        # Find earliest bar in this chunk to set next end_dt
        earliest_bar_dt = min(chunk_rows.keys())
        earliest = datetime.strptime(earliest_bar_dt, "%Y-%m-%d %H:%M:%S")

        # If we've already reached or passed our target, stop
        if earliest <= target_start:
            break

        # Next chunk ends just before the earliest bar we got
        end_dt = earliest - timedelta(minutes=1)

        # Progress
        if chunks_fetched % 3 == 0:
            print(f"    {symbol}: {len(all_rows)} bars so far, "
                  f"earliest={earliest_bar_dt}")

    return all_rows, request_count


# ── Main Collection Logic ────────────────────────────────────────────

def collect(
    symbols: List[str],
    months_back: int = 3,
    port: int = DEFAULT_PORT,
    dry_run: bool = False,
    bar_size_key: str = "5min",
):
    """
    Main collection routine.

    For each symbol:
      1. Load existing CSV data
      2. Determine how far back to fetch (target_start)
      3. Fetch weekly/daily chunks from IBKR
      4. Merge new bars with existing (dedup by datetime)
      5. Write merged CSV
    """
    bar_cfg = BAR_CONFIGS[bar_size_key]
    target_start = datetime.now() - timedelta(days=months_back * 30)
    print(f"\n{'='*60}")
    print(f"IBKR Historical Data Collection")
    print(f"{'='*60}")
    print(f"Symbols:      {len(symbols)}")
    print(f"Target start: {target_start.strftime('%Y-%m-%d')}")
    print(f"Bar size:     {bar_cfg['bar_size']}")
    print(f"RTH only:     {USE_RTH}")
    print(f"Data dir:     {DATA_DIR}")
    print(f"Port:         {port}")
    print(f"{'='*60}\n")

    if dry_run:
        for sym in symbols:
            first, last = get_existing_date_range(sym, bar_cfg)
            status = f"existing: {first} → {last}" if first else "NO DATA"
            print(f"  {sym:6s}  {status}")
        print(f"\nDry run complete. {len(symbols)} symbols would be fetched.")
        return

    # Connect
    ib = connect_ibkr(port)

    request_count = 0
    total_new_bars = 0
    errors = []
    start_time = time.time()

    for i, symbol in enumerate(symbols):
        first_existing, last_existing = get_existing_date_range(symbol, bar_cfg)
        existing_rows = load_existing_csv(symbol, bar_cfg)
        existing_count = len(existing_rows)

        print(f"\n[{i+1}/{len(symbols)}] {symbol}")
        if first_existing:
            print(f"  Existing: {first_existing} → {last_existing} ({existing_count} bars)")
        else:
            print(f"  Existing: none")

        try:
            new_rows, request_count = fetch_symbol_history(
                ib, symbol, target_start, request_count, bar_cfg=bar_cfg
            )

            if new_rows:
                # Merge: existing data takes priority for overlapping timestamps
                merged = {**new_rows, **existing_rows}  # existing overwrites new
                new_bar_count = len(merged) - existing_count

                write_merged_csv(symbol, merged, bar_cfg)
                total_new_bars += max(0, new_bar_count)

                # Report
                sorted_dts = sorted(merged.keys())
                print(f"  Result:   {sorted_dts[0]} → {sorted_dts[-1]} "
                      f"({len(merged)} bars, +{max(0, new_bar_count)} new)")
            else:
                print(f"  Result:   no new bars fetched")

        except Exception as e:
            errors.append((symbol, str(e)))
            print(f"  ERROR: {e}")

        # Progress estimate
        elapsed = time.time() - start_time
        if i > 0:
            per_sym = elapsed / (i + 1)
            remaining = per_sym * (len(symbols) - i - 1)
            print(f"  [{request_count} requests, ~{remaining/60:.0f} min remaining]")

    # Disconnect
    ib.disconnect()

    # Summary
    print(f"\n{'='*60}")
    print(f"COLLECTION COMPLETE")
    print(f"{'='*60}")
    print(f"Symbols processed: {len(symbols)}")
    print(f"Total requests:    {request_count}")
    print(f"New bars added:    {total_new_bars}")
    print(f"Elapsed time:      {(time.time()-start_time)/60:.1f} min")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for sym, err in errors:
            print(f"  {sym}: {err}")
    print()


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Collect historical bars from IBKR"
    )
    parser.add_argument(
        "--months", type=int, default=3,
        help="Months of history to pull (default: 3)"
    )
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Specific symbols to fetch (default: all)"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"TWS/Gateway port (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be fetched without connecting"
    )
    parser.add_argument(
        "--client-id", type=int, default=DEFAULT_CLIENT_ID,
        help=f"IBKR client ID (default: {DEFAULT_CLIENT_ID})"
    )
    parser.add_argument(
        "--bar-size", choices=["5min", "1min"], default="5min",
        help="Bar size to collect: 5min (default) or 1min"
    )
    args = parser.parse_args()

    # Ensure data dir exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if args.bar_size == "1min":
        (DATA_DIR / "1min").mkdir(parents=True, exist_ok=True)

    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        symbols = get_all_symbols()

    collect(
        symbols=symbols,
        months_back=args.months,
        port=args.port,
        dry_run=args.dry_run,
        bar_size_key=args.bar_size,
    )


if __name__ == "__main__":
    main()
