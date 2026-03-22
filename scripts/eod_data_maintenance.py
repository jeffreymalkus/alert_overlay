"""
End-of-Day Data Maintenance — keeps replay data in sync with the live universe.

Run this after market close each trading day (or on weekends to catch up).
It ensures that every symbol the live system trades has complete 5-min bar
data that replay.py can consume.

Three-step pipeline:
  1. AGGREGATE  — convert all 1-min recordings → 5-min CSVs
  2. BACKFILL   — pull missing history via Alpaca for new symbols
  3. MANIFEST   — write a universe manifest so replay knows which symbols
                  were active on which dates

The manifest solves the core problem: your live universe changes daily
(scanner picks new in-play names), but replay needs to know what the
universe WAS on each historical date to avoid survivorship bias.

Usage:
    # Standard EOD run (aggregate + manifest, no Alpaca pull)
    python -m alert_overlay.scripts.eod_data_maintenance

    # Full run with Alpaca backfill for new symbols
    python -m alert_overlay.scripts.eod_data_maintenance --backfill

    # Dry run (show what would happen)
    python -m alert_overlay.scripts.eod_data_maintenance --dry-run

    # Aggregate only (skip manifest + backfill)
    python -m alert_overlay.scripts.eod_data_maintenance --aggregate-only

    # Rebuild manifest from all available snapshots + data
    python -m alert_overlay.scripts.eod_data_maintenance --rebuild-manifest
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

# ── Paths ──
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_1MIN = DATA_DIR / "1min"
SNAPSHOT_DIR = BASE_DIR / "in_play_snapshots"
WATCHLIST_FILE = BASE_DIR / "watchlist.txt"
IN_PLAY_FILE = BASE_DIR / "in_play.txt"
IN_PLAY_HISTORY = BASE_DIR / "in_play_history.csv"
MANIFEST_FILE = DATA_DIR / "universe_manifest.json"
LOG_DIR = BASE_DIR / "session_logs"


# ═══════════════════════════════════════════════════════════════════
#  Step 1: Aggregate 1-min → 5-min
# ═══════════════════════════════════════════════════════════════════

def aggregate_all(dry_run: bool = False) -> dict:
    """
    Aggregate all 1-min CSVs to 5-min. Returns stats dict.

    Uses the existing bar_recorder.aggregate_1min_to_5min() logic but
    adds reporting on what changed.
    """
    from alert_overlay.bar_recorder import aggregate_1min_to_5min

    if not DATA_1MIN.exists():
        print("  No 1-min data directory found.")
        return {"symbols": 0, "new_bars": 0, "updated": []}

    symbols = sorted([
        f.stem.replace("_1min", "")
        for f in DATA_1MIN.glob("*_1min.csv")
    ])

    print(f"  Found {len(symbols)} symbols with 1-min data")

    total_new = 0
    updated = []

    for sym in symbols:
        if dry_run:
            # Check if 5-min file exists and is up to date
            one_min_path = DATA_1MIN / f"{sym}_1min.csv"
            five_min_path = DATA_DIR / f"{sym}_5min.csv"
            if not five_min_path.exists():
                print(f"    {sym}: NEW — would create 5-min file")
                updated.append(sym)
            else:
                # Compare last timestamps
                last_1m = _last_timestamp(one_min_path)
                last_5m = _last_timestamp(five_min_path)
                if last_1m and last_5m and last_1m > last_5m:
                    print(f"    {sym}: STALE — 1min ends {last_1m}, 5min ends {last_5m}")
                    updated.append(sym)
        else:
            new = aggregate_1min_to_5min(sym, DATA_DIR)
            if new > 0:
                print(f"    {sym}: +{new} new 5-min bars")
                total_new += new
                updated.append(sym)

    return {"symbols": len(symbols), "new_bars": total_new, "updated": updated}


def _last_timestamp(csv_path: Path) -> Optional[str]:
    """Get last timestamp from a CSV file (reads last line)."""
    if not csv_path.exists():
        return None
    try:
        with open(csv_path, "rb") as f:
            # Seek to near end for efficiency
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 500))
            lines = f.read().decode("utf-8", errors="replace").strip().split("\n")
            if len(lines) >= 2:
                return lines[-1].split(",")[0]
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════
#  Step 2: Universe Manifest
# ═══════════════════════════════════════════════════════════════════

def build_manifest(rebuild: bool = False) -> dict:
    """
    Build/update the universe manifest — records which symbols were
    tradeable on each date.

    Sources:
      1. in_play_snapshots/{date}.txt — daily in-play symbols
      2. watchlist.txt — static universe (assumed present every day)
      3. data/1min/ — symbols with actual bar data on each date
      4. in_play_history.csv — ADD/REMOVE/CLEAR events

    The manifest is a JSON file:
    {
        "last_updated": "2026-03-22",
        "static_universe": ["AAPL", "AMD", ...],
        "dates": {
            "2026-03-12": {
                "static": ["AAPL", ...],
                "in_play": ["AA", "ABNB", ...],
                "recorded": ["AA", "AAPL", ...]
            },
            ...
        }
    }
    """
    # Load existing manifest unless rebuilding
    manifest = {"last_updated": "", "static_universe": [], "dates": {}}
    if MANIFEST_FILE.exists() and not rebuild:
        try:
            manifest = json.loads(MANIFEST_FILE.read_text())
        except Exception:
            pass

    # Current static universe
    static = _load_file_list(WATCHLIST_FILE)
    manifest["static_universe"] = sorted(static)

    # Load all snapshots
    snapshot_dates = {}
    if SNAPSHOT_DIR.exists():
        for f in sorted(SNAPSHOT_DIR.glob("*.txt")):
            d = f.stem  # e.g., "2026-03-12"
            syms = _load_file_list(f)
            snapshot_dates[d] = sorted(syms)

    # Build per-date records from 1-min data
    # (which symbols actually had bars recorded on each date)
    recorded_by_date = defaultdict(set)
    if DATA_1MIN.exists():
        for csv_file in DATA_1MIN.glob("*_1min.csv"):
            sym = csv_file.stem.replace("_1min", "")
            dates = _get_dates_in_csv(csv_file)
            for d in dates:
                recorded_by_date[d].add(sym)

    # Also check 5-min data for symbols that predate 1-min recording
    for csv_file in DATA_DIR.glob("*_5min.csv"):
        sym = csv_file.stem.replace("_5min", "")
        dates = _get_dates_in_csv(csv_file)
        for d in dates:
            recorded_by_date[d].add(sym)

    # Merge into manifest
    all_dates = sorted(set(list(snapshot_dates.keys()) + list(recorded_by_date.keys())))
    for d in all_dates:
        if d in manifest["dates"] and not rebuild:
            # Only update if we have new info
            existing = manifest["dates"][d]
            if d in snapshot_dates:
                existing["in_play"] = snapshot_dates[d]
            if d in recorded_by_date:
                existing["recorded"] = sorted(recorded_by_date[d])
            continue

        entry = {
            "static": sorted(static),
            "in_play": snapshot_dates.get(d, []),
            "recorded": sorted(recorded_by_date.get(d, set())),
        }
        manifest["dates"][d] = entry

    manifest["last_updated"] = date.today().isoformat()

    return manifest


def _load_file_list(path: Path) -> List[str]:
    """Load one-symbol-per-line file."""
    if not path.exists():
        return []
    lines = path.read_text().strip().split("\n")
    return [s.strip().upper() for s in lines if s.strip() and not s.strip().startswith("#")]


def _get_dates_in_csv(csv_path: Path) -> Set[str]:
    """Extract unique date strings from a CSV's datetime column."""
    dates = set()
    try:
        with open(csv_path, "r") as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header
            for row in reader:
                if row:
                    # datetime is first column: "2026-03-12 09:30:00"
                    dates.add(row[0][:10])
    except Exception:
        pass
    return dates


# ═══════════════════════════════════════════════════════════════════
#  Step 3: Gap Report
# ═══════════════════════════════════════════════════════════════════

def gap_report(manifest: dict) -> dict:
    """
    Analyze the manifest to find symbols that were in-play but lack
    5-min data for replay.

    Returns dict with actionable info:
      - missing_5min: symbols with 1-min but no 5-min data
      - missing_any: in-play symbols with NO data at all (need Alpaca pull)
      - coverage_by_date: {date: {total_universe, has_5min, missing}}
    """
    missing_5min = set()  # have 1-min, no 5-min
    missing_any = set()   # in-play, no data at all
    coverage = {}

    all_5min_symbols = set(
        f.stem.replace("_5min", "")
        for f in DATA_DIR.glob("*_5min.csv")
    )
    all_1min_symbols = set(
        f.stem.replace("_1min", "")
        for f in DATA_1MIN.glob("*_1min.csv")
    ) if DATA_1MIN.exists() else set()

    # Check each date
    for d, info in sorted(manifest.get("dates", {}).items()):
        universe = set(info.get("static", [])) | set(info.get("in_play", []))
        recorded = set(info.get("recorded", []))
        has_5min = universe & all_5min_symbols
        has_1min_only = (universe & all_1min_symbols) - all_5min_symbols
        no_data = universe - all_5min_symbols - all_1min_symbols

        missing_5min |= has_1min_only
        missing_any |= no_data

        coverage[d] = {
            "universe": len(universe),
            "has_5min": len(has_5min),
            "has_1min_only": len(has_1min_only),
            "no_data": len(no_data),
        }

    return {
        "missing_5min": sorted(missing_5min),
        "missing_any": sorted(missing_any),
        "coverage_by_date": coverage,
    }


# ═══════════════════════════════════════════════════════════════════
#  Step 4: Alpaca Backfill (optional)
# ═══════════════════════════════════════════════════════════════════

def backfill_missing(missing_symbols: List[str], months: int = 3,
                     dry_run: bool = False) -> dict:
    """
    Pull historical bars from Alpaca for symbols that are missing data.
    Requires ALPACA_API_KEY and ALPACA_API_SECRET env vars.
    """
    api_key = os.environ.get("ALPACA_API_KEY", "")
    api_secret = os.environ.get("ALPACA_API_SECRET", "")

    if not api_key or not api_secret:
        print("  ⚠ ALPACA_API_KEY and ALPACA_API_SECRET not set.")
        print("    Set them to enable automatic backfill:")
        print("      export ALPACA_API_KEY='your-key'")
        print("      export ALPACA_API_SECRET='your-secret'")
        return {"status": "skipped", "reason": "no_credentials"}

    if dry_run:
        print(f"  Would backfill {len(missing_symbols)} symbols via Alpaca ({months} months)")
        for sym in missing_symbols[:20]:
            print(f"    {sym}")
        if len(missing_symbols) > 20:
            print(f"    ... and {len(missing_symbols) - 20} more")
        return {"status": "dry_run", "symbols": len(missing_symbols)}

    # Import and run the Alpaca collector
    try:
        from alert_overlay.scripts.collect_history_alpaca import collect
        print(f"  Backfilling {len(missing_symbols)} symbols via Alpaca...")
        for bar_size in ["1min", "5min"]:
            collect(
                symbols=missing_symbols,
                months_back=months,
                api_key=api_key,
                api_secret=api_secret,
                dry_run=False,
                bar_size_key=bar_size,
            )
        return {"status": "complete", "symbols": len(missing_symbols)}
    except Exception as e:
        print(f"  ERROR during Alpaca backfill: {e}")
        return {"status": "error", "error": str(e)}


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="End-of-Day Data Maintenance — sync replay data with live universe")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without making changes")
    parser.add_argument("--backfill", action="store_true",
                        help="Pull missing history via Alpaca (requires API keys)")
    parser.add_argument("--backfill-months", type=int, default=3,
                        help="Months of history to backfill (default: 3)")
    parser.add_argument("--aggregate-only", action="store_true",
                        help="Only run 1min→5min aggregation, skip manifest")
    parser.add_argument("--rebuild-manifest", action="store_true",
                        help="Rebuild universe manifest from scratch")
    args = parser.parse_args()

    today = date.today().isoformat()
    print("=" * 80)
    print(f"  EOD Data Maintenance — {today}")
    print("=" * 80)

    # ── Step 1: Aggregate 1-min → 5-min ──
    print(f"\n[Step 1] Aggregating 1-min → 5-min bars {'(dry run)' if args.dry_run else ''}...")
    agg_stats = aggregate_all(dry_run=args.dry_run)
    print(f"  Result: {agg_stats['symbols']} symbols, "
          f"{len(agg_stats['updated'])} updated, "
          f"{agg_stats['new_bars']} new bars")

    if args.aggregate_only:
        print("\n  --aggregate-only: skipping manifest and backfill.")
        return

    # ── Step 2: Build/update universe manifest ──
    print(f"\n[Step 2] Building universe manifest {'(rebuild)' if args.rebuild_manifest else '(incremental)'}...")
    manifest = build_manifest(rebuild=args.rebuild_manifest)

    n_dates = len(manifest.get("dates", {}))
    n_static = len(manifest.get("static_universe", []))
    print(f"  Manifest: {n_dates} dates, {n_static} static symbols")

    if not args.dry_run:
        MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_FILE.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        print(f"  Saved: {MANIFEST_FILE}")

    # ── Step 3: Gap report ──
    print(f"\n[Step 3] Analyzing coverage gaps...")
    gaps = gap_report(manifest)

    print(f"  Symbols with 1-min data but NO 5-min: {len(gaps['missing_5min'])}")
    if gaps['missing_5min'][:10]:
        print(f"    Examples: {', '.join(gaps['missing_5min'][:10])}")

    print(f"  In-play symbols with NO data at all:  {len(gaps['missing_any'])}")
    if gaps['missing_any'][:10]:
        print(f"    Examples: {', '.join(gaps['missing_any'][:10])}")

    # Coverage summary for recent dates
    recent = sorted(gaps["coverage_by_date"].items())[-5:]
    if recent:
        print(f"\n  Recent coverage:")
        print(f"  {'Date':>12s} | {'Universe':>8s} | {'Has 5min':>8s} | {'1min only':>9s} | {'No data':>7s}")
        print(f"  {'-'*12}-+-{'-'*8}-+-{'-'*8}-+-{'-'*9}-+-{'-'*7}")
        for d, c in recent:
            print(f"  {d:>12s} | {c['universe']:>8d} | {c['has_5min']:>8d} | "
                  f"{c['has_1min_only']:>9d} | {c['no_data']:>7d}")

    # ── Step 4: Backfill (if requested) ──
    if args.backfill:
        all_missing = sorted(set(gaps['missing_5min']) | set(gaps['missing_any']))
        if all_missing:
            print(f"\n[Step 4] Backfilling {len(all_missing)} symbols via Alpaca...")
            bf_result = backfill_missing(all_missing, months=args.backfill_months,
                                          dry_run=args.dry_run)
            print(f"  Backfill result: {bf_result['status']}")

            # Re-aggregate after backfill
            if bf_result.get("status") == "complete" and not args.dry_run:
                print("\n  Re-aggregating after backfill...")
                aggregate_all(dry_run=False)
        else:
            print(f"\n[Step 4] No symbols need backfill — all covered.")
    else:
        all_missing = sorted(set(gaps['missing_5min']) | set(gaps['missing_any']))
        if all_missing:
            print(f"\n  TIP: {len(all_missing)} symbols need data. Run with --backfill to pull via Alpaca.")

    # ── Write log ──
    if not args.dry_run:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"eod_maintenance_{today.replace('-', '')}.json"
        log_data = {
            "date": today,
            "aggregation": {
                "symbols": agg_stats["symbols"],
                "updated": agg_stats["updated"],
                "new_bars": agg_stats["new_bars"],
            },
            "manifest": {
                "dates": n_dates,
                "static_symbols": n_static,
            },
            "gaps": {
                "missing_5min": len(gaps["missing_5min"]),
                "missing_any": len(gaps["missing_any"]),
                "missing_5min_symbols": gaps["missing_5min"],
                "missing_any_symbols": gaps["missing_any"],
            },
        }
        log_path.write_text(json.dumps(log_data, indent=2))
        print(f"\n  Log: {log_path}")

    print(f"\n{'='*80}")
    print(f"  EOD maintenance complete.")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
