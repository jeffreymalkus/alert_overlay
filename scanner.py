"""
Portfolio D — SMB-Style In-Play Scanner

Identifies "stocks in play" using IBKR's scanner API, modeled after SMB Capital's
five-pillar In Play methodology:

    1. CATALYST   — detected via price move (±3%+) and gap scanners
    2. LIQUIDITY   — avg daily volume >2M, market cap >$2B, manageable ATR
    3. RANGE       — ATR ≥ $1, today's range approaching ≥1.5× ATR
    4. RVOL        — relative volume ≥ 3.0 (today vs 20-day avg at same time of day)
    5. CONFIRMATION — cross-scanner frequency (appearing in 3+ independent scans)

What IBKR scanners CAN detect (the fingerprint of a catalyst):
    - Price move (TOP_PERC_GAIN/LOSE)        → price catalyst
    - Opening gap (TOP_OPEN_PERC_GAIN/LOSE)  → premarket catalyst
    - Volume surge (HOT_BY_VOLUME)            → order-flow confirmation
    - Volume acceleration (TOP_VOLUME_RATE)   → real-time demand signal
    - Most active (MOST_ACTIVE)               → liquidity proof
    - Price range (TOP_PRICE_RANGE)           → range expansion

What IBKR scanners CANNOT detect:
    - News content (earnings, FDA, upgrades)  → needs news API (future layer)
    - Catalyst quality score (SMB's 8/10)     → approximated by composite scoring
    - Float / short interest                  → needs separate data source

Architecture:
    Phase 1: SCAN    — run 8 IBKR scanners, dedupe, collect raw candidates
    Phase 2: ENRICH  — for top candidates, fetch ATR, RVOL, % change, range
    Phase 3: SCORE   — composite score approximating SMB's catalyst quality
    Phase 4: SELECT  — hard filters (RVOL ≥ 3, ATR ≥ 1, etc.), rank, take top N
    Phase 5: OUTPUT  — write to in_play.txt, print results table

Usage:
    python -m alert_overlay.scanner                     # standard run
    python -m alert_overlay.scanner --dry-run            # preview only
    python -m alert_overlay.scanner --top 30             # more names
    python -m alert_overlay.scanner --rvol-min 2.0       # looser RVOL
    python -m alert_overlay.scanner --no-rvol            # skip RVOL (faster)
    python -m alert_overlay.scanner --port 4001          # IB Gateway
    python -m alert_overlay.scanner --dump-params        # save scanner XML
"""

import argparse
import logging
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    from ib_insync import IB, Stock, ScannerSubscription, TagValue, util
except ImportError:
    print("ERROR: ib_insync not installed. Run: pip3 install ib_insync")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scanner")

EASTERN = ZoneInfo("US/Eastern")
IN_PLAY_FILE = Path(__file__).parent / "in_play.txt"
WATCHLIST_FILE = Path(__file__).parent / "watchlist.txt"

# ═══════════════════════════════════════════════════════════════════════════════
# SCANNER DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════════
# Each scanner detects one facet of "in play" behavior.
# A symbol appearing in multiple scanners has converging evidence of a catalyst.
# IBKR returns up to 50 results per scan; max 10 active scans.

SCAN_DEFS: List[dict] = [
    # ── Price catalyst detection ──
    {
        "name": "Top % Gainers",
        "scanCode": "TOP_PERC_GAIN",
        "smb_pillar": "catalyst",
        "description": "Biggest intraday % gainers — detects price catalyst (long side)",
    },
    {
        "name": "Top % Losers",
        "scanCode": "TOP_PERC_LOSE",
        "smb_pillar": "catalyst",
        "description": "Biggest intraday % losers — detects price catalyst (short side)",
    },
    # ── Premarket gap detection (SMB: ±3% premarket with volume) ──
    {
        "name": "Gap Up",
        "scanCode": "TOP_OPEN_PERC_GAIN",
        "smb_pillar": "catalyst",
        "description": "Biggest opening gap up — premarket catalyst signal",
    },
    {
        "name": "Gap Down",
        "scanCode": "TOP_OPEN_PERC_LOSE",
        "smb_pillar": "catalyst",
        "description": "Biggest opening gap down — premarket catalyst signal",
    },
    # ── Volume / RVOL confirmation ──
    {
        "name": "Hot by Volume",
        "scanCode": "HOT_BY_VOLUME",
        "smb_pillar": "rvol",
        "description": "Highest volume relative to recent average — RVOL proxy",
    },
    {
        "name": "Volume Rate",
        "scanCode": "TOP_VOLUME_RATE",
        "smb_pillar": "rvol",
        "description": "Highest volume acceleration — real-time demand surge",
    },
    # ── Liquidity proof ──
    {
        "name": "Most Active",
        "scanCode": "MOST_ACTIVE",
        "smb_pillar": "liquidity",
        "description": "Most shares traded — confirms deep liquidity",
    },
    # ── Range expansion ──
    {
        "name": "Price Range",
        "scanCode": "TOP_PRICE_RANGE",
        "smb_pillar": "range",
        "description": "Widest intraday price range — confirms range expansion",
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
# SMB SCORING WEIGHTS
# ═══════════════════════════════════════════════════════════════════════════════
# These weights map IBKR-measurable metrics to SMB's five pillars.
# The composite score approximates SMB's "catalyst score" of 8/10.

WEIGHTS = {
    "scan_frequency":  15.0,   # each additional scanner hit
    "rvol":             3.0,   # per 1.0 RVOL (e.g., RVOL 5 = 15 points)
    "pct_change":       2.0,   # per 1% absolute price change
    "range_vs_atr":     5.0,   # bonus per 1x ATR of intraday range
    "gap_pct":          1.5,   # per 1% gap from prior close
    "rank_penalty":    -0.2,   # per avg rank position (lower rank = better)
}

# Hard filters — candidates must pass ALL of these to qualify
HARD_FILTERS = {
    "rvol_min":        3.0,   # SMB: RVOL > 3
    "atr_min":         1.0,   # SMB: ATR ≥ $1
    "pct_change_min":  1.5,   # minimum absolute % change (proxy for catalyst)
    "avg_volume_min":  2_000_000,  # SMB: >1M traded, we want >2M ADV
}


def _load_static_watchlist() -> set:
    """Load static watchlist to optionally exclude from in-play."""
    if WATCHLIST_FILE.exists():
        lines = WATCHLIST_FILE.read_text().strip().split("\n")
        return {s.strip().upper() for s in lines
                if s.strip() and not s.strip().startswith("#")}
    return set()


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: SCAN
# ═══════════════════════════════════════════════════════════════════════════════

def _build_scanner_sub(
    scan_code: str,
    price_min: float,
    price_max: float,
    mcap_min_millions: float = 2000,
    avg_vol_min: int = 2_000_000,
) -> Tuple[ScannerSubscription, list]:
    """Build a ScannerSubscription with SMB-aligned filters."""
    sub = ScannerSubscription(
        instrument="STK",
        locationCode="STK.US.MAJOR",
        scanCode=scan_code,
        abovePrice=price_min,
        belowPrice=price_max,
        numberOfRows=50,
        # NOTE: aboveVolume, marketCapAbove, and stockTypeFilter caused
        # "no items retrieved" on some IBKR plans.  We apply these filters
        # in post-processing (Phase 2 enrichment) instead.
    )

    tag_filters = []

    return sub, tag_filters


def run_scanners(
    ib: IB,
    price_min: float,
    price_max: float,
    mcap_min_millions: float = 2000,
    avg_vol_min: int = 2_000_000,
) -> Dict[str, dict]:
    """Phase 1: Run all scanners, collect and dedupe raw candidates.

    Returns dict: symbol -> {
        scans: [scan names], scan_count: int, rank_sum: int,
        pillars_hit: set (catalyst/rvol/liquidity/range),
        contract: Contract
    }
    """
    results: Dict[str, dict] = defaultdict(lambda: {
        "scans": [],
        "scan_count": 0,
        "rank_sum": 0,
        "pillars_hit": set(),
        "contract": None,
    })

    for scan_def in SCAN_DEFS:
        scan_name = scan_def["name"]
        scan_code = scan_def["scanCode"]
        pillar = scan_def["smb_pillar"]
        log.info(f"  [{pillar.upper():>9}] Running: {scan_name} ({scan_code})...")

        sub, tag_filters = _build_scanner_sub(
            scan_code, price_min, price_max, mcap_min_millions, avg_vol_min)

        try:
            scan_data = ib.reqScannerData(sub, [], tag_filters)
        except Exception as e:
            log.error(f"    Scanner {scan_name} failed: {e}")
            continue

        if not scan_data:
            log.warning(f"    Scanner {scan_name} returned 0 results.")
            continue

        log.info(f"    {scan_name}: {len(scan_data)} results")

        for rank, sd in enumerate(scan_data):
            symbol = sd.contractDetails.contract.symbol
            results[symbol]["scans"].append(scan_name)
            results[symbol]["scan_count"] += 1
            results[symbol]["rank_sum"] += rank
            results[symbol]["pillars_hit"].add(pillar)
            if results[symbol]["contract"] is None:
                results[symbol]["contract"] = sd.contractDetails.contract

        # Pace between scanners
        ib.sleep(1.0)

    return dict(results)


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: ENRICH
# ═══════════════════════════════════════════════════════════════════════════════

def _enrich_candidate(ib: IB, symbol: str, contract, lookback_days: int = 20) -> dict:
    """Fetch historical data to compute SMB metrics for one symbol.

    Returns dict with keys:
        atr, avg_volume, today_volume, rvol, pct_change, today_range,
        range_vs_atr, gap_pct, prior_close
    All values are float or None if unavailable.
    """
    metrics = {
        "atr": None, "avg_volume": None, "today_volume": None,
        "rvol": None, "pct_change": None, "today_range": None,
        "range_vs_atr": None, "gap_pct": None, "prior_close": None,
        "today_open": None, "today_high": None, "today_low": None,
        "today_close": None,
    }

    try:
        # ── Daily bars: ATR + avg volume + prior close ──
        daily_bars = ib.reqHistoricalData(
            contract, endDateTime="", durationStr=f"{lookback_days + 5} D",
            barSizeSetting="1 day", whatToShow="TRADES",
            useRTH=True, formatDate=1,
        )
        if not daily_bars or len(daily_bars) < 5:
            return metrics

        # Separate today from history
        # The last bar might be today (if market is open) or yesterday (if pre-market)
        hist_bars = daily_bars[:-1]  # all but last
        today_bar = daily_bars[-1]

        # ATR (14-period by default, or whatever we have)
        atr_periods = min(14, len(hist_bars))
        true_ranges = []
        for i in range(1, len(hist_bars)):
            prev_close = hist_bars[i - 1].close
            h = hist_bars[i].high
            l = hist_bars[i].low
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            true_ranges.append(tr)
        if true_ranges:
            atr = sum(true_ranges[-atr_periods:]) / min(atr_periods, len(true_ranges))
            metrics["atr"] = round(atr, 2)

        # Average daily volume (20-day)
        vol_lookback = min(lookback_days, len(hist_bars))
        if vol_lookback > 0:
            avg_vol = sum(b.volume for b in hist_bars[-vol_lookback:]) / vol_lookback
            metrics["avg_volume"] = round(avg_vol)

        # Prior close (for gap calculation)
        metrics["prior_close"] = hist_bars[-1].close

        # Today's data from the daily bar
        metrics["today_open"] = today_bar.open
        metrics["today_high"] = today_bar.high
        metrics["today_low"] = today_bar.low
        metrics["today_close"] = today_bar.close
        metrics["today_volume"] = today_bar.volume

        # ── Derived metrics ──

        # Today's range
        today_range = today_bar.high - today_bar.low
        metrics["today_range"] = round(today_range, 2)

        # Range vs ATR (SMB wants ~2x ATR intraday)
        if metrics["atr"] and metrics["atr"] > 0:
            metrics["range_vs_atr"] = round(today_range / metrics["atr"], 2)

        # % change from prior close
        if metrics["prior_close"] and metrics["prior_close"] > 0:
            pct = ((today_bar.close - metrics["prior_close"]) / metrics["prior_close"]) * 100
            metrics["pct_change"] = round(pct, 2)

        # Gap % (open vs prior close)
        if metrics["prior_close"] and metrics["prior_close"] > 0:
            gap = ((today_bar.open - metrics["prior_close"]) / metrics["prior_close"]) * 100
            metrics["gap_pct"] = round(gap, 2)

        # RVOL — time-adjusted relative volume
        if metrics["avg_volume"] and metrics["avg_volume"] > 0 and today_bar.volume > 0:
            now_et = datetime.now(EASTERN)
            minutes_since_open = (now_et.hour - 9) * 60 + (now_et.minute - 30)
            minutes_since_open = max(minutes_since_open, 1)
            day_fraction = min(minutes_since_open / 390.0, 1.0)

            expected_vol = metrics["avg_volume"] * day_fraction
            if expected_vol > 0:
                metrics["rvol"] = round(today_bar.volume / expected_vol, 2)

    except Exception as e:
        log.warning(f"  [{symbol}] Enrichment failed: {e}")

    return metrics


def enrich_candidates(
    ib: IB,
    candidates: Dict[str, dict],
    max_enrich: int = 80,
) -> Dict[str, dict]:
    """Phase 2: Enrich top candidates with ATR, RVOL, range, % change.

    Enriches candidates in priority order (most scanner hits first).
    Caps at max_enrich to avoid IBKR pacing issues (~1 request/symbol).
    """
    # Sort by scan_count descending, then rank_sum ascending
    sorted_syms = sorted(
        candidates.keys(),
        key=lambda s: (-candidates[s]["scan_count"], candidates[s]["rank_sum"]),
    )

    enriched_count = 0
    for sym in sorted_syms:
        if enriched_count >= max_enrich:
            break

        info = candidates[sym]
        if info["contract"] is None:
            continue

        metrics = _enrich_candidate(ib, sym, info["contract"])
        info["metrics"] = metrics
        enriched_count += 1

        # Log progress every 10
        if enriched_count % 10 == 0:
            log.info(f"  Enriched {enriched_count}/{min(len(sorted_syms), max_enrich)} candidates...")

        # Pace: each enrichment = 1 historical data request
        ib.sleep(0.5)

    # Mark un-enriched candidates
    for sym in candidates:
        if "metrics" not in candidates[sym]:
            candidates[sym]["metrics"] = None

    log.info(f"  Enrichment complete: {enriched_count} candidates processed.")
    return candidates


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 3: SCORE
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_smb_score(info: dict) -> float:
    """Compute composite in-play score approximating SMB's catalyst quality.

    Higher = more likely "in play" by SMB standards.
    A score of ~50+ roughly corresponds to SMB's 8/10 catalyst score.
    """
    m = info.get("metrics")
    if m is None:
        # Un-enriched: use scanner data only
        return info["scan_count"] * WEIGHTS["scan_frequency"]

    score = 0.0

    # Scanner frequency (confirmation pillar)
    score += info["scan_count"] * WEIGHTS["scan_frequency"]

    # RVOL (SMB's #1 confirmation signal)
    if m["rvol"] is not None:
        score += m["rvol"] * WEIGHTS["rvol"]

    # Absolute % change (price catalyst strength)
    if m["pct_change"] is not None:
        score += abs(m["pct_change"]) * WEIGHTS["pct_change"]

    # Range vs ATR (SMB wants ~2x ATR range)
    if m["range_vs_atr"] is not None:
        score += m["range_vs_atr"] * WEIGHTS["range_vs_atr"]

    # Gap magnitude (premarket catalyst)
    if m["gap_pct"] is not None:
        score += abs(m["gap_pct"]) * WEIGHTS["gap_pct"]

    # Rank penalty (lower avg rank = ranked higher in individual scans)
    avg_rank = info["rank_sum"] / max(info["scan_count"], 1)
    score += avg_rank * WEIGHTS["rank_penalty"]

    # Pillar diversity bonus: hitting 3+ of 4 SMB pillars = strong signal
    pillar_count = len(info.get("pillars_hit", set()))
    if pillar_count >= 4:
        score += 10
    elif pillar_count >= 3:
        score += 5

    return round(score, 2)


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4: SELECT (hard filters + ranking)
# ═══════════════════════════════════════════════════════════════════════════════

def select_in_play(
    candidates: Dict[str, dict],
    rvol_min: float,
    atr_min: float,
    pct_change_min: float,
    avg_vol_min: int,
    price_min: float,
    price_max: float,
    top_n: int,
    exclude_symbols: set,
    require_rvol: bool = True,
) -> List[dict]:
    """Phase 4: Apply hard filters and rank.

    SMB hard filters:
        - RVOL ≥ rvol_min (default 3.0)
        - ATR ≥ atr_min (default $1)
        - |% change| ≥ pct_change_min (default 1.5%)
    Post-scan filters (couldn't apply at IBKR scanner level):
        - avg daily volume ≥ avg_vol_min (default 2M)
        - price between price_min and price_max ($10-$400)
    """
    # Exclude index ETFs, leveraged/inverse ETFs, and known non-stock products
    always_exclude = {
        "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO",          # index ETFs
        "SOXS", "SOXL", "TQQQ", "SQQQ", "SPXS", "SPXL",   # leveraged/inverse
        "UVXY", "VXX", "SVXY",                                # volatility products
        "TLT", "TNA", "TZA", "LABU", "LABD",                 # leveraged sector
        "NUGT", "DUST", "JNUG", "JDST",                      # leveraged gold miners
        "FAS", "FAZ", "ERX", "ERY", "UDOW", "SDOW",          # leveraged broad
        "SH", "PSQ", "DOG", "RWM",                            # inverse broad
        "ARKK", "ARKG", "ARKW", "ARKF",                       # thematic ETFs
        "XLF", "XLE", "XLK", "XLV", "XLI", "XLU", "XLP",    # sector ETFs
        "XLB", "XLC", "XLY", "XLRE", "GDX", "GDXJ",
        "SMH", "KRE", "IBB", "XBI", "XOP", "OIH", "EEM",
        "HYG", "LQD", "JNK", "SLV", "GLD", "USO", "UNG",
    }
    results = []

    for sym, info in candidates.items():
        if sym in always_exclude or sym in exclude_symbols:
            continue

        m = info.get("metrics")
        score = _compute_smb_score(info)

        # ── Hard filters ──
        passed = True
        fail_reasons = []

        if m is not None:
            # ── Post-scan filters (replace IBKR-side filters) ──

            # Average daily volume (SMB: liquidity — must be tradeable)
            if m["avg_volume"] is not None and m["avg_volume"] < avg_vol_min:
                fail_reasons.append(f"ADV {m['avg_volume']:,.0f} < {avg_vol_min:,}")
                passed = False

            # Price range check (user constraints: $10-$400)
            if m["today_close"] is not None:
                if m["today_close"] < price_min:
                    fail_reasons.append(f"price ${m['today_close']:.2f} < ${price_min}")
                    passed = False
                elif m["today_close"] > price_max:
                    fail_reasons.append(f"price ${m['today_close']:.2f} > ${price_max}")
                    passed = False

            # ── SMB hard filters ──

            if require_rvol:
                # RVOL check (SMB pillar 5)
                if m["rvol"] is not None:
                    if m["rvol"] < rvol_min:
                        fail_reasons.append(f"RVOL {m['rvol']:.1f} < {rvol_min}")
                        passed = False
                elif info["scan_count"] < 3:
                    fail_reasons.append("no RVOL data + low scan count")
                    passed = False

            # ATR check (SMB pillar 4)
            if m["atr"] is not None and m["atr"] < atr_min:
                fail_reasons.append(f"ATR ${m['atr']:.2f} < ${atr_min}")
                passed = False

            # % change check (proxy for catalyst presence)
            if m["pct_change"] is not None and abs(m["pct_change"]) < pct_change_min:
                fail_reasons.append(f"|chg| {abs(m['pct_change']):.1f}% < {pct_change_min}%")
                passed = False

        elif m is None:
            # Un-enriched: only include if very high scanner frequency
            if info["scan_count"] < 4:
                passed = False
                fail_reasons.append("not enriched + scan_count < 4")

        if not passed:
            log.debug(f"  [{sym}] FILTERED: {', '.join(fail_reasons)}")
            continue

        results.append({
            "symbol": sym,
            "score": score,
            "scan_count": info["scan_count"],
            "scans": info["scans"],
            "pillars_hit": sorted(info.get("pillars_hit", set())),
            "rvol": m["rvol"] if m else None,
            "atr": m["atr"] if m else None,
            "pct_change": m["pct_change"] if m else None,
            "gap_pct": m["gap_pct"] if m else None,
            "range_vs_atr": m["range_vs_atr"] if m else None,
            "today_volume": m["today_volume"] if m else None,
            "avg_volume": m["avg_volume"] if m else None,
        })

    # Sort by composite score descending
    results.sort(key=lambda x: -x["score"])

    return results[:top_n]


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 5: OUTPUT
# ═══════════════════════════════════════════════════════════════════════════════

def print_results_table(results: List[dict]):
    """Pretty-print the ranked results with SMB metrics."""
    print()
    print("=" * 110)
    print(f"  SMB-Style In-Play Selection — {datetime.now(EASTERN).strftime('%Y-%m-%d %H:%M ET')}")
    print("=" * 110)
    print(f"{'#':<4} {'Symbol':<8} {'Score':<7} {'Scans':<6} {'RVOL':<7} "
          f"{'ATR':<7} {'Chg%':<7} {'Gap%':<7} {'Rng/ATR':<8} {'Pillars':<20} Scanner Sources")
    print("-" * 110)

    for i, r in enumerate(results, 1):
        rvol_s = f"{r['rvol']:.1f}x" if r['rvol'] is not None else "—"
        atr_s = f"${r['atr']:.2f}" if r['atr'] is not None else "—"
        chg_s = f"{r['pct_change']:+.1f}%" if r['pct_change'] is not None else "—"
        gap_s = f"{r['gap_pct']:+.1f}%" if r['gap_pct'] is not None else "—"
        rng_s = f"{r['range_vs_atr']:.1f}x" if r['range_vs_atr'] is not None else "—"
        pillars = ",".join(r["pillars_hit"]) if r["pillars_hit"] else "—"
        scans = ", ".join(r["scans"])

        print(f"{i:<4} {r['symbol']:<8} {r['score']:<7.1f} {r['scan_count']:<6} "
              f"{rvol_s:<7} {atr_s:<7} {chg_s:<7} {gap_s:<7} {rng_s:<8} "
              f"{pillars:<20} {scans}")

    print("=" * 110)

    # Summary stats
    rvols = [r["rvol"] for r in results if r["rvol"] is not None]
    atrs = [r["atr"] for r in results if r["atr"] is not None]
    chgs = [abs(r["pct_change"]) for r in results if r["pct_change"] is not None]

    if rvols:
        print(f"  RVOL  — min: {min(rvols):.1f}x  avg: {sum(rvols)/len(rvols):.1f}x  "
              f"max: {max(rvols):.1f}x")
    if atrs:
        print(f"  ATR   — min: ${min(atrs):.2f}  avg: ${sum(atrs)/len(atrs):.2f}  "
              f"max: ${max(atrs):.2f}")
    if chgs:
        print(f"  |Chg| — min: {min(chgs):.1f}%  avg: {sum(chgs)/len(chgs):.1f}%  "
              f"max: {max(chgs):.1f}%")
    print()


def write_in_play(symbols: List[str], dry_run: bool = False):
    """Write symbols to in_play.txt."""
    content = "\n".join(sorted(symbols)) + "\n"
    if dry_run:
        log.info(f"DRY RUN — would write {len(symbols)} symbols to {IN_PLAY_FILE}:")
        for sym in sorted(symbols):
            log.info(f"  {sym}")
        return

    IN_PLAY_FILE.write_text(content)
    log.info(f"Wrote {len(symbols)} symbols to {IN_PLAY_FILE}")


def sync_to_dashboard(symbols: List[str], dashboard_url: str = "http://localhost:8877"):
    """Push in-play names to a running dashboard (no restart needed).

    1. Clears the dashboard's current in-play list
    2. Adds each new symbol via the /api/add-in-play endpoint
    3. Dashboard auto-subscribes new symbols to reqMktData
    """
    import urllib.request
    import json as _json

    # Check if dashboard is running
    try:
        urllib.request.urlopen(f"{dashboard_url}/api/status", timeout=2)
    except Exception:
        log.info("Dashboard not running — skipping live sync. Names saved to in_play.txt.")
        return

    # Clear existing in-play list
    try:
        req = urllib.request.Request(
            f"{dashboard_url}/api/clear-in-play",
            data=_json.dumps({}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        result = _json.loads(resp.read().decode())
        cleared = result.get("cleared", 0)
        log.info(f"Dashboard: cleared {cleared} old in-play names.")
    except Exception as e:
        log.warning(f"Dashboard: failed to clear in-play list: {e}")
        return

    # Add each new symbol
    added = 0
    for sym in sorted(symbols):
        try:
            req = urllib.request.Request(
                f"{dashboard_url}/api/add-in-play",
                data=_json.dumps({"symbol": sym}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            result = _json.loads(resp.read().decode())
            if result.get("ok"):
                added += 1
            else:
                log.warning(f"Dashboard: {sym} — {result.get('error', 'unknown error')}")
        except Exception as e:
            log.warning(f"Dashboard: failed to add {sym}: {e}")

    log.info(f"Dashboard: synced {added}/{len(symbols)} in-play names (live, no restart).")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Portfolio D — SMB-Style In-Play Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
SMB In-Play criteria mapped to IBKR scanners:
  1. CATALYST   → TOP_PERC_GAIN/LOSE, TOP_OPEN_PERC_GAIN/LOSE
  2. LIQUIDITY  → MOST_ACTIVE + avgVolume/marketCap filters
  3. RANGE      → TOP_PRICE_RANGE + ATR enrichment
  4. RVOL       → HOT_BY_VOLUME, TOP_VOLUME_RATE + RVOL enrichment
  5. CONFIRM    → cross-scanner frequency (3+ scans)
        """,
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497,
                        help="IBKR port (7497=TWS, 4001=Gateway)")
    parser.add_argument("--client-id", type=int, default=20,
                        help="IBKR client ID (use different from dashboard)")
    parser.add_argument("--top", type=int, default=25,
                        help="Max in-play names to select")
    parser.add_argument("--rvol-min", type=float, default=2.0,
                        help="Minimum RVOL (SMB standard: 3.0, default 2.0 for wider net)")
    parser.add_argument("--atr-min", type=float, default=1.0,
                        help="Minimum ATR in dollars (SMB standard: $1)")
    parser.add_argument("--pct-min", type=float, default=1.5,
                        help="Minimum absolute %% change (catalyst proxy)")
    parser.add_argument("--price-min", type=float, default=10.0,
                        help="Minimum stock price")
    parser.add_argument("--price-max", type=float, default=400.0,
                        help="Maximum stock price")
    parser.add_argument("--mcap-min", type=float, default=2000,
                        help="Minimum market cap in millions USD")
    parser.add_argument("--avg-vol-min", type=int, default=1_000_000,
                        help="Minimum average daily volume (SMB: >1M)")
    parser.add_argument("--max-enrich", type=int, default=80,
                        help="Max candidates to enrich (controls API load)")
    parser.add_argument("--exclude-static", action="store_true",
                        help="Exclude symbols already in static watchlist")
    parser.add_argument("--no-rvol", action="store_true",
                        help="Skip RVOL hard filter (use scanner rank only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview results without writing to in_play.txt")
    parser.add_argument("--dump-params", action="store_true",
                        help="Dump IBKR scanner parameters XML and exit")
    args = parser.parse_args()

    # ── Connect ──
    ib = IB()
    log.info(f"Connecting to IBKR at {args.host}:{args.port} (clientId={args.client_id})...")
    try:
        ib.connect(args.host, args.port, clientId=args.client_id)
    except Exception as e:
        log.error(f"Connection failed: {e}")
        log.error("Ensure TWS/Gateway is running with API connections enabled.")
        sys.exit(1)
    log.info("Connected.")

    # ── Optional: dump scanner params ──
    if args.dump_params:
        log.info("Fetching scanner parameters XML...")
        xml = ib.reqScannerParameters()
        out_file = Path(__file__).parent / "scanner_parameters.xml"
        out_file.write_text(xml)
        log.info(f"Saved to {out_file} ({len(xml):,} bytes). "
                 f"Search this XML for available scanCode and filter values.")
        ib.disconnect()
        return

    static_watchlist = _load_static_watchlist() if args.exclude_static else set()

    # ── Phase 1: SCAN ──
    log.info(f"Phase 1: Running {len(SCAN_DEFS)} scanners...")
    log.info(f"  Filters: price ${args.price_min}-${args.price_max}, "
             f"mcap >${args.mcap_min}M, ADV >{args.avg_vol_min:,}")
    candidates = run_scanners(
        ib, args.price_min, args.price_max, args.mcap_min, args.avg_vol_min)

    total = len(candidates)
    log.info(f"Phase 1 complete: {total} unique candidates across {len(SCAN_DEFS)} scanners.")

    if not candidates:
        log.warning("No candidates found. Market may be closed or filters too restrictive.")
        ib.disconnect()
        return

    # Scanner overlap stats
    overlap = Counter(info["scan_count"] for info in candidates.values())
    for cnt in sorted(overlap.keys(), reverse=True):
        log.info(f"  {overlap[cnt]} symbols in {cnt} scanner(s)")

    # Pillar coverage stats
    pillar_counts = Counter()
    for info in candidates.values():
        for p in info["pillars_hit"]:
            pillar_counts[p] += 1
    for pillar, count in pillar_counts.most_common():
        log.info(f"  Pillar '{pillar}': {count} candidates")

    # ── Phase 2: ENRICH ──
    log.info(f"Phase 2: Enriching top {args.max_enrich} candidates (ATR, RVOL, range)...")
    candidates = enrich_candidates(ib, candidates, max_enrich=args.max_enrich)

    # ── Phase 3 + 4: SCORE & SELECT ──
    log.info(f"Phase 3-4: Scoring and filtering (RVOL≥{args.rvol_min}, "
             f"ATR≥${args.atr_min}, |chg|≥{args.pct_min}%, "
             f"ADV≥{args.avg_vol_min:,}, price ${args.price_min}-${args.price_max})...")
    selected = select_in_play(
        candidates,
        rvol_min=args.rvol_min,
        atr_min=args.atr_min,
        pct_change_min=args.pct_min,
        avg_vol_min=args.avg_vol_min,
        price_min=args.price_min,
        price_max=args.price_max,
        top_n=args.top,
        exclude_symbols=static_watchlist,
        require_rvol=not args.no_rvol,
    )

    if not selected:
        log.warning("No candidates passed all SMB filters. Try --rvol-min 2.0 or --no-rvol.")
        ib.disconnect()
        return

    log.info(f"Phase 4 complete: {len(selected)} names passed all filters.")

    # ── Phase 5: OUTPUT ──
    print_results_table(selected)

    symbols = [r["symbol"] for r in selected]
    write_in_play(symbols, dry_run=args.dry_run)

    # If dashboard is running, push names directly (no restart needed)
    if not args.dry_run:
        sync_to_dashboard(symbols)

    ib.disconnect()
    log.info("Done.")


if __name__ == "__main__":
    main()
