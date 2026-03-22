"""
Alpaca Historical Data Collector — Pull 1-min bar history (FREE, fast).

Uses Alpaca's free market data API to download 1-min bars for all symbols.
Much faster than IBKR: no pacing limits, batch multiple symbols per request,
and 1-min data goes back 5+ years.

Setup:
    1. Sign up at https://alpaca.markets (free paper trading account)
    2. Get API key + secret from https://app.alpaca.markets/paper/dashboard/overview
    3. Set environment variables:
         export ALPACA_API_KEY="your-key"
         export ALPACA_API_SECRET="your-secret"
       Or pass via CLI:
         --api-key YOUR_KEY --api-secret YOUR_SECRET

Usage:
    # Pull 3 months of 1-min bars for all symbols
    python3 -m alert_overlay.scripts.collect_history_alpaca

    # Pull 6 months
    python3 -m alert_overlay.scripts.collect_history_alpaca --months 6

    # Specific symbols only
    python3 -m alert_overlay.scripts.collect_history_alpaca --symbols SPY QQQ AAPL

    # Also pull 5-min bars
    python3 -m alert_overlay.scripts.collect_history_alpaca --bar-size 5min

    # Dry run
    python3 -m alert_overlay.scripts.collect_history_alpaca --dry-run

    # Pull both 1-min AND 5-min in one run
    python3 -m alert_overlay.scripts.collect_history_alpaca --bar-size both
"""

import argparse
import csv
import os
import sys
import time
import json
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import List, Optional, Set, Dict
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError

# ── Constants ────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent.parent / "data"
ALPACA_BASE_URL = "https://data.alpaca.markets/v2/stocks/bars"
MAX_LIMIT = 10000  # Alpaca max per page

# Alpaca timeframe strings
BAR_CONFIGS = {
    "1min": {"timeframe": "1Min", "subdir": "1min", "suffix": "1min"},
    "5min": {"timeframe": "5Min", "subdir": "",     "suffix": "5min"},
}

# Rate limit: Alpaca free tier = 200 req/min. We'll be well under this.
# With 10k bars per request and batching symbols, we need very few requests.

# ── Symbols (reuse from collect_history.py) ──────────────────────────

SECTOR_ETFS = [
    "XLK", "XLC", "XLY", "XLF", "XLV", "XLE", "XLI",
    "XLP", "XLB", "XLU", "XLRE",
]
MARKET_ETFS = ["SPY", "QQQ"]


def _load_symbol_file(path: Path) -> List[str]:
    """Load one-symbol-per-line file, skip comments and blanks."""
    if not path.exists():
        return []
    symbols = []
    for line in path.read_text().splitlines():
        sym = line.strip()
        if sym and not sym.startswith("#"):
            symbols.append(sym.upper())
    return symbols


def load_watchlist() -> List[str]:
    """Load symbols from watchlist.txt."""
    return _load_symbol_file(Path(__file__).parent.parent / "watchlist.txt")


def load_in_play() -> List[str]:
    """Load current in-play symbols from in_play.txt."""
    return _load_symbol_file(Path(__file__).parent.parent / "in_play.txt")


def load_in_play_snapshots() -> List[str]:
    """Load ALL symbols that have appeared in any in_play_snapshot."""
    snap_dir = Path(__file__).parent.parent / "in_play_snapshots"
    if not snap_dir.exists():
        return []
    all_syms = set()
    for f in snap_dir.glob("*.txt"):
        all_syms.update(_load_symbol_file(f))
    return sorted(all_syms)


def load_recorded_symbols() -> List[str]:
    """Load symbols that already have 1-min data on disk."""
    one_min_dir = DATA_DIR / "1min"
    if not one_min_dir.exists():
        return []
    return sorted([
        f.stem.replace("_1min", "")
        for f in one_min_dir.glob("*_1min.csv")
    ])


def get_all_symbols() -> List[str]:
    """Deduplicated list: market + sector ETFs + watchlist + in-play + recorded.

    This ensures Alpaca pulls cover the FULL live universe, not just
    the static watchlist. In-play symbols rotate daily; snapshot history
    captures every symbol that was ever in-play; recorded symbols include
    anything the dashboard bar-recorded from IBKR.
    """
    seen: Set[str] = set()
    ordered = []
    sources = (
        MARKET_ETFS
        + SECTOR_ETFS
        + load_watchlist()
        + load_in_play()
        + load_in_play_snapshots()
        + load_recorded_symbols()
    )
    for sym in sources:
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


def load_existing_csv(symbol: str, bar_cfg: dict) -> dict:
    """Load existing CSV data as {datetime_str: row_dict}."""
    path = _csv_path(symbol, bar_cfg)
    rows = {}
    if not path.exists():
        return rows
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows[row["datetime"]] = row
    return rows


def get_existing_date_range(symbol: str, bar_cfg: dict) -> tuple:
    """Return (earliest_date, latest_date) from existing CSV, or (None, None)."""
    path = _csv_path(symbol, bar_cfg)
    if not path.exists():
        return None, None
    first_dt = last_dt = None
    with open(path, "r") as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        for row in reader:
            if row:
                if first_dt is None:
                    first_dt = row[0]
                last_dt = row[0]
    return first_dt, last_dt


def write_merged_csv(symbol: str, rows: dict, bar_cfg: dict):
    """Write merged rows to CSV, sorted by datetime."""
    path = _csv_path(symbol, bar_cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
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


# ── Alpaca API ───────────────────────────────────────────────────────

def alpaca_request(url: str, api_key: str, api_secret: str) -> dict:
    """Make an authenticated GET request to Alpaca API."""
    req = Request(url)
    req.add_header("APCA-API-KEY-ID", api_key)
    req.add_header("APCA-API-SECRET-KEY", api_secret)
    req.add_header("Accept", "application/json")

    try:
        with urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        if e.code == 429:
            print(f"    Rate limited — waiting 30s...")
            time.sleep(30)
            return alpaca_request(url, api_key, api_secret)  # retry
        raise RuntimeError(f"Alpaca API error {e.code}: {body}") from e


def fetch_symbol_bars(
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    api_key: str,
    api_secret: str,
) -> dict:
    """Fetch all bars for a single symbol between start and end dates.

    Uses pagination to get all data. Returns {datetime_str: row_dict}.
    """
    all_rows = {}
    page_token = None
    pages = 0

    while True:
        params = {
            "symbols": symbol,
            "timeframe": timeframe,
            "start": start,
            "end": end,
            "limit": MAX_LIMIT,
            "adjustment": "split",
            "feed": "iex",
            "sort": "asc",
        }
        if page_token:
            params["page_token"] = page_token

        url = f"{ALPACA_BASE_URL}?{urlencode(params)}"
        data = alpaca_request(url, api_key, api_secret)
        pages += 1

        # Parse bars — response format: {"bars": {"SYMBOL": [...]}, "next_page_token": ...}
        bars = data.get("bars", {}).get(symbol, [])

        for bar in bars:
            # bar["t"] = "2026-01-22T09:30:00Z" (RFC-3339 UTC)
            # Convert to Eastern for consistency with IBKR data
            ts_utc = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
            ts_eastern = ts_utc.astimezone(
                __import__("zoneinfo").ZoneInfo("US/Eastern")
            )
            dt_str = ts_eastern.strftime("%Y-%m-%d %H:%M:%S")

            all_rows[dt_str] = {
                "datetime": dt_str,
                "open": f"{bar['o']:.4f}",
                "high": f"{bar['h']:.4f}",
                "low": f"{bar['l']:.4f}",
                "close": f"{bar['c']:.4f}",
                "volume": str(int(bar["v"])),
            }

        # Check for more pages
        page_token = data.get("next_page_token")
        if not page_token or not bars:
            break

    return all_rows


# ── Main Collection Logic ────────────────────────────────────────────

def collect(
    symbols: List[str],
    months_back: int,
    api_key: str,
    api_secret: str,
    dry_run: bool = False,
    bar_size_key: str = "1min",
):
    """Main collection routine."""
    bar_cfg = BAR_CONFIGS[bar_size_key]
    end_date = date.today()
    start_date = end_date - timedelta(days=months_back * 30)
    start_str = start_date.isoformat()
    end_str = end_date.isoformat()

    print(f"\n{'='*60}")
    print(f"Alpaca Historical Data Collection")
    print(f"{'='*60}")
    print(f"Symbols:      {len(symbols)}")
    print(f"Date range:   {start_str} → {end_str}")
    print(f"Bar size:     {bar_cfg['timeframe']}")
    print(f"Data dir:     {DATA_DIR}")
    print(f"{'='*60}\n")

    if dry_run:
        for sym in symbols:
            first, last = get_existing_date_range(sym, bar_cfg)
            status = f"existing: {first} → {last}" if first else "NO DATA"
            print(f"  {sym:6s}  {status}")
        print(f"\nDry run complete. {len(symbols)} symbols would be fetched.")
        return

    total_new_bars = 0
    errors = []
    start_time = time.time()

    for i, symbol in enumerate(symbols):
        first_existing, last_existing = get_existing_date_range(symbol, bar_cfg)
        existing_rows = load_existing_csv(symbol, bar_cfg)
        existing_count = len(existing_rows)

        print(f"\n[{i+1}/{len(symbols)}] {symbol}", end="")
        if first_existing:
            print(f"  (existing: {existing_count} bars, {first_existing[:10]} → {last_existing[:10]})")
        else:
            print(f"  (no existing data)")

        try:
            new_rows = fetch_symbol_bars(
                symbol, bar_cfg["timeframe"],
                start_str, end_str,
                api_key, api_secret,
            )

            if new_rows:
                # Merge: existing data takes priority for overlapping timestamps
                merged = {**new_rows, **existing_rows}
                new_bar_count = len(merged) - existing_count

                write_merged_csv(symbol, merged, bar_cfg)
                total_new_bars += max(0, new_bar_count)

                sorted_dts = sorted(merged.keys())
                print(f"  → {len(merged)} bars ({sorted_dts[0][:10]} → {sorted_dts[-1][:10]}), "
                      f"+{max(0, new_bar_count)} new")
            else:
                print(f"  → no bars returned (symbol may not exist on Alpaca)")

        except Exception as e:
            errors.append((symbol, str(e)))
            print(f"  → ERROR: {e}")

        # Brief pause to stay well under rate limit
        if (i + 1) % 10 == 0:
            elapsed = time.time() - start_time
            per_sym = elapsed / (i + 1)
            remaining = per_sym * (len(symbols) - i - 1)
            print(f"  [{i+1}/{len(symbols)} done, ~{remaining/60:.1f} min remaining]")

    # Summary
    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"COLLECTION COMPLETE")
    print(f"{'='*60}")
    print(f"Symbols processed: {len(symbols)}")
    print(f"New bars added:    {total_new_bars}")
    print(f"Elapsed time:      {elapsed/60:.1f} min")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for sym, err in errors:
            print(f"  {sym}: {err}")
    print()


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Collect historical bars from Alpaca (free, fast)"
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
        "--bar-size", choices=["1min", "5min", "both"], default="1min",
        help="Bar size to collect (default: 1min)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be fetched without connecting"
    )
    parser.add_argument(
        "--api-key", default=None,
        help="Alpaca API key (or set ALPACA_API_KEY env var)"
    )
    parser.add_argument(
        "--api-secret", default=None,
        help="Alpaca API secret (or set ALPACA_API_SECRET env var)"
    )
    args = parser.parse_args()

    # Resolve API credentials
    api_key = args.api_key or os.environ.get("ALPACA_API_KEY")
    api_secret = args.api_secret or os.environ.get("ALPACA_API_SECRET")

    if not args.dry_run and (not api_key or not api_secret):
        print("ERROR: Alpaca API credentials required.")
        print()
        print("Option 1 — environment variables:")
        print("  export ALPACA_API_KEY='your-key'")
        print("  export ALPACA_API_SECRET='your-secret'")
        print()
        print("Option 2 — CLI args:")
        print("  --api-key YOUR_KEY --api-secret YOUR_SECRET")
        print()
        print("Sign up free at https://alpaca.markets")
        sys.exit(1)

    # Ensure data dirs exist
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "1min").mkdir(parents=True, exist_ok=True)

    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        symbols = get_all_symbols()

    bar_sizes = ["1min", "5min"] if args.bar_size == "both" else [args.bar_size]

    for bs in bar_sizes:
        collect(
            symbols=symbols,
            months_back=args.months,
            api_key=api_key,
            api_secret=api_secret,
            dry_run=args.dry_run,
            bar_size_key=bs,
        )


if __name__ == "__main__":
    main()
