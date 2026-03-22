"""
Bar Recorder — Persists incoming live bars to CSV files.

Records 1-min bars for all symbols (SPY, QQQ, watchlist).
Provides aggregation to 5-min bars for backtesting.

Files are written incrementally (append mode) to avoid data loss.
Each day's bars are deduped by timestamp on write.

Data layout:
    data/1min/{SYMBOL}_1min.csv     — raw 1-min bars (primary recording)
    data/{SYMBOL}_5min.csv          — 5-min bars (aggregated, backtest-ready)

CSV format matches existing convention:
    datetime,open,high,low,close,volume
    2026-03-09 09:30:00,579.2500,579.8800,579.1200,579.5500,1234567
"""

import csv
import os
import threading
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional

from .models import Bar


# ═══════════════════════════════════════════════════════════════════
#  Bar Recorder
# ═══════════════════════════════════════════════════════════════════

class BarRecorder:
    """
    Records live bars to CSV files.

    Thread-safe: uses a lock for file writes since multiple
    symbol callbacks fire from IBKR's event loop.
    """

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or Path(__file__).parent / "data"
        self.one_min_dir = self.data_dir / "1min"
        self.one_min_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        # Track written timestamps per symbol to avoid duplicates
        # {symbol: set(datetime_str)}
        self._written: Dict[str, set] = defaultdict(set)
        # Buffer for 5-min aggregation: {symbol: [Bar, ...]}
        self._agg_buffer: Dict[str, List[Bar]] = defaultdict(list)

        self._initialized = False
        self._symbols_loaded: set = set()

    def _ensure_loaded(self, symbol: str):
        """Load existing timestamps for a symbol to avoid re-writing."""
        if symbol in self._symbols_loaded:
            return
        path = self.one_min_dir / f"{symbol}_1min.csv"
        if path.exists():
            with open(path, "r") as f:
                reader = csv.reader(f)
                next(reader, None)  # skip header
                for row in reader:
                    if row:
                        self._written[symbol].add(row[0])
        self._symbols_loaded.add(symbol)

    def record_bar(self, symbol: str, bar: Bar):
        """
        Record a single bar to the 1-min CSV file.
        Skips duplicates (same timestamp already recorded).
        """
        ts = bar.timestamp
        if ts is None:
            return

        # Strip timezone for consistent storage
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)

        dt_str = ts.strftime("%Y-%m-%d %H:%M:%S")

        with self._lock:
            self._ensure_loaded(symbol)

            # Skip if already written
            if dt_str in self._written[symbol]:
                return

            path = self.one_min_dir / f"{symbol}_1min.csv"
            write_header = not path.exists()

            with open(path, "a", newline="") as f:
                writer = csv.writer(f)
                if write_header:
                    writer.writerow(["datetime", "open", "high", "low", "close", "volume"])
                writer.writerow([
                    dt_str,
                    f"{bar.open:.4f}",
                    f"{bar.high:.4f}",
                    f"{bar.low:.4f}",
                    f"{bar.close:.4f}",
                    str(int(bar.volume)),
                ])

            self._written[symbol].add(dt_str)

    def get_stats(self) -> Dict:
        """Return recording statistics."""
        with self._lock:
            stats = {}
            for sym in sorted(self._symbols_loaded):
                path = self.one_min_dir / f"{sym}_1min.csv"
                if path.exists():
                    n = len(self._written[sym])
                    stats[sym] = n
            return stats


# ═══════════════════════════════════════════════════════════════════
#  Aggregation: 1-min → 5-min
# ═══════════════════════════════════════════════════════════════════

def aggregate_1min_to_5min(symbol: str, data_dir: Optional[Path] = None):
    """
    Read 1-min CSV, aggregate to 5-min bars, write/merge to 5-min CSV.

    5-min bar boundaries: 09:30, 09:35, 09:40, ...
    Each 5-min bar aggregates the 5 one-minute bars starting at that time.
    E.g., the 09:30 bar = 09:30, 09:31, 09:32, 09:33, 09:34.
    """
    data_dir = data_dir or Path(__file__).parent / "data"
    one_min_path = data_dir / "1min" / f"{symbol}_1min.csv"
    five_min_path = data_dir / f"{symbol}_5min.csv"

    if not one_min_path.exists():
        return 0

    # Load 1-min bars
    one_min_bars = []
    with open(one_min_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            one_min_bars.append(row)

    if not one_min_bars:
        return 0

    # Group by 5-min bucket
    # Bucket key = datetime rounded down to nearest 5 min
    buckets = defaultdict(list)
    for row in one_min_bars:
        dt = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S")
        # Round down to 5-min boundary
        minute_bucket = (dt.minute // 5) * 5
        bucket_dt = dt.replace(minute=minute_bucket, second=0)
        bucket_key = bucket_dt.strftime("%Y-%m-%d %H:%M:%S")
        buckets[bucket_key].append(row)

    # Aggregate each bucket
    new_5min = {}
    for bucket_key in sorted(buckets.keys()):
        rows = buckets[bucket_key]
        o = float(rows[0]["open"])
        h = max(float(r["high"]) for r in rows)
        l = min(float(r["low"]) for r in rows)
        c = float(rows[-1]["close"])
        v = sum(int(float(r["volume"])) for r in rows)
        new_5min[bucket_key] = {
            "datetime": bucket_key,
            "open": f"{o:.4f}",
            "high": f"{h:.4f}",
            "low": f"{l:.4f}",
            "close": f"{c:.4f}",
            "volume": str(v),
        }

    # Load existing 5-min bars
    existing_5min = {}
    if five_min_path.exists():
        with open(five_min_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_5min[row["datetime"]] = row

    # Merge: new overwrites existing for overlapping timestamps
    merged = {**existing_5min, **new_5min}

    # Write merged
    with open(five_min_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["datetime", "open", "high", "low", "close", "volume"])
        for dt_key in sorted(merged.keys()):
            r = merged[dt_key]
            writer.writerow([
                r["datetime"], r["open"], r["high"], r["low"],
                r["close"], r["volume"],
            ])

    new_count = len(new_5min) - len(set(new_5min.keys()) & set(existing_5min.keys()))
    return max(0, new_count)


def aggregate_all(data_dir: Optional[Path] = None):
    """Aggregate all 1-min CSVs to 5-min."""
    data_dir = data_dir or Path(__file__).parent / "data"
    one_min_dir = data_dir / "1min"

    if not one_min_dir.exists():
        print("No 1-min data directory found.")
        return

    total_new = 0
    symbols = sorted([
        f.stem.replace("_1min", "")
        for f in one_min_dir.glob("*_1min.csv")
    ])

    print(f"Aggregating {len(symbols)} symbols from 1-min → 5-min...")
    for sym in symbols:
        new = aggregate_1min_to_5min(sym, data_dir)
        if new > 0:
            print(f"  {sym}: +{new} new 5-min bars")
            total_new += new

    print(f"Done. {total_new} new 5-min bars added across {len(symbols)} symbols.")
    return total_new


# ═══════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Bar recorder utilities")
    parser.add_argument("--aggregate", action="store_true",
                        help="Aggregate all 1-min bars to 5-min")
    parser.add_argument("--stats", action="store_true",
                        help="Show recording stats for 1-min data")
    parser.add_argument("--symbol", type=str, default=None,
                        help="Aggregate a single symbol")
    args = parser.parse_args()

    data_dir = Path(__file__).parent / "data"

    if args.aggregate:
        if args.symbol:
            n = aggregate_1min_to_5min(args.symbol.upper(), data_dir)
            print(f"{args.symbol.upper()}: {n} new 5-min bars")
        else:
            aggregate_all(data_dir)
    elif args.stats:
        one_min_dir = data_dir / "1min"
        if one_min_dir.exists():
            total = 0
            for f in sorted(one_min_dir.glob("*_1min.csv")):
                with open(f) as fh:
                    n = sum(1 for _ in fh) - 1
                sym = f.stem.replace("_1min", "")
                print(f"  {sym:6s}: {n:6d} bars")
                total += n
            print(f"\n  Total: {total} bars across {len(list(one_min_dir.glob('*_1min.csv')))} symbols")
        else:
            print("No 1-min data found.")
    else:
        parser.print_help()
