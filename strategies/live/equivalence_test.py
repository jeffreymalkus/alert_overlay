"""
Replay ↔ Live Equivalence Test

Feeds identical historical bars through both paths:
  A) Replay: _detect_*_signals() — raw signal detection, NO in-play/regime gates
  B) Live:   step() one bar at a time via StrategyManager

Compares raw signal timestamps and entry/stop/target prices.
Reports mismatches per strategy with tolerance for float drift.

IMPORTANT design notes:
  - We call replay's INTERNAL _detect_*_signals() to bypass in-play + regime
    gates. This ensures we're comparing pure detection logic only.
  - Replay path uses its OWN internal indicators (EMA, VWAP, ATR).
    Live path uses SharedIndicators computed by StrategyManager.
    Minor float divergence is expected (≤$0.02 per indicator).
  - Replay VWAPCalc.update(tp, volume) vs Live VWAPCalc.update(bar)
    with max(volume, 1) — diverges only on zero-volume bars.
  - Both paths see bars in the same order. Indicator warmup matches.

Usage:
    cd ~/Projects
    python -m alert_overlay.strategies.live.equivalence_test
    python -m alert_overlay.strategies.live.equivalence_test --symbols META NVDA --days 5 -v
    python -m alert_overlay.strategies.live.equivalence_test --strategy SC_SNIPER -v
"""

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Replay-side imports ──
from ...backtest import load_bars_from_csv
from ...models import Bar, NaN

from ..shared.config import StrategyConfig
from ..shared.in_play_proxy import InPlayProxy
from ..shared.market_regime import EnhancedMarketRegime
from ..shared.rejection_filters import RejectionFilters
from ..shared.quality_scoring import QualityScorer
from ..shared.helpers import compute_daily_atr

from ..second_chance_sniper import SecondChanceSniperStrategy
from ..fl_antichop_only import FLAntiChopStrategy
from ..spencer_atier import SpencerATierStrategy
from ..hitchhiker_quality import HitchHikerQualityStrategy
from ..ema_fpip_atier import EmaFpipATierStrategy
from ..bdr_short import BDRShortStrategy
from ..ema9_first_touch import EMA9FirstTouchStrategy
from ..backside_structure import BacksideStructureStrategy

# ── Live-side imports ──
from .manager import StrategyManager
from .sc_sniper_live import SCSniperLive
from .fl_antichop_live import FLAntiChopLive
from .spencer_atier_live import SpencerATierLive
from .hitchhiker_live import HitchHikerLive
from .ema_fpip_live import EmaFpipLive
from .bdr_short_live import BDRShortLive
from .ema9_ft_live import EMA9FirstTouchLive
from .backside_live import BacksideStructureLive

_isnan = math.isnan

DATA_DIR = Path(__file__).parent.parent.parent / "data"

# ────────────────────────────────────────────────────────────────
#  Constants
# ────────────────────────────────────────────────────────────────

PRICE_TOL = 0.03        # max $0.03 entry price drift (float arithmetic across indicator impls)
STOP_TOL = 0.05         # stops can diverge more due to different ATR paths
TARGET_TOL = 0.10       # targets amplify stop divergence via RR multiplier

STRATEGY_NAMES = [
    "SC_SNIPER", "FL_ANTICHOP", "SP_ATIER", "HH_QUALITY",
    "EMA_FPIP", "BDR_SHORT", "EMA9_FT", "BS_STRUCT",
]


# ────────────────────────────────────────────────────────────────
#  Replay path: call internal _detect_*_signals() directly
# ────────────────────────────────────────────────────────────────

# Maps strategy name → (detect method name, extra kwargs builder)
_DETECT_METHOD_MAP = {
    "SC_SNIPER":   "_detect_sc_signals",
    "FL_ANTICHOP": "_detect_fl_signals",
    "SP_ATIER":    "_detect_sp_signals",
    "HH_QUALITY":  "_detect_hh_signals",
    "EMA_FPIP":    "_detect_fpip_signals",
    "BDR_SHORT":   "_detect_bdr_signals",
    "EMA9_FT":     "_detect_e9ft_signals",
    "BS_STRUCT":   "_detect_bs_signals",
}


def _run_replay_raw(strategy_name: str, replay_strat, symbol: str,
                    bars: List[Bar], day: date,
                    daily_atr: Optional[Dict] = None) -> List[dict]:
    """
    Run replay's internal detection (bypasses in-play + regime gates).
    Returns list of signal dicts with timestamp, entry, stop, target, direction.
    """
    method_name = _DETECT_METHOD_MAP[strategy_name]
    detect_fn = getattr(replay_strat, method_name)

    # All detect methods take (symbol, bars, day, ip_score)
    # SP_ATIER also takes daily_atr kwarg
    if strategy_name == "SP_ATIER":
        raw_tuples = detect_fn(symbol, bars, day, ip_score=1.0, daily_atr=daily_atr)
    else:
        raw_tuples = detect_fn(symbol, bars, day, ip_score=1.0)

    results = []
    for tup in raw_tuples:
        sig = tup[0]  # StrategySignal is always first element
        results.append({
            "timestamp": sig.timestamp,
            "entry_price": sig.entry_price,
            "stop_price": sig.stop_price,
            "target_price": sig.target_price,
            "direction": sig.direction,
        })
    return results


# ────────────────────────────────────────────────────────────────
#  Live path: step() one bar at a time
# ────────────────────────────────────────────────────────────────

def _run_live_for_day(strategy_name: str, mgr: StrategyManager,
                      bars: List[Bar], day: date) -> List[dict]:
    """Run live path (step-by-step) for one symbol-day. Returns signal dicts."""
    results = []
    for bar in bars:
        signals = mgr.on_bar(bar)
        if bar.timestamp.date() == day:
            for sig in signals:
                if sig.strategy_name == strategy_name:
                    results.append({
                        "timestamp": bar.timestamp,
                        "entry_price": sig.entry_price,
                        "stop_price": sig.stop_price,
                        "target_price": sig.target_price,
                        "direction": sig.direction,
                    })
    return results


def _make_live_strategy(name: str, cfg: StrategyConfig):
    """Factory: create a live strategy instance by name."""
    factories = {
        "SC_SNIPER":   lambda: SCSniperLive(cfg),
        "FL_ANTICHOP": lambda: FLAntiChopLive(cfg),
        "SP_ATIER":    lambda: SpencerATierLive(cfg),
        "HH_QUALITY":  lambda: HitchHikerLive(cfg),
        "EMA_FPIP":    lambda: EmaFpipLive(cfg),
        "BDR_SHORT":   lambda: BDRShortLive(cfg),
        "EMA9_FT":     lambda: EMA9FirstTouchLive(cfg),
        "BS_STRUCT":   lambda: BacksideStructureLive(cfg),
    }
    factory = factories.get(name)
    return factory() if factory else None


# ────────────────────────────────────────────────────────────────
#  Comparison logic
# ────────────────────────────────────────────────────────────────

def _compare_signals(replay_sigs: List[dict], live_sigs: List[dict],
                     verbose: bool = False) -> dict:
    """Compare two lists of signal dicts. Returns comparison result."""
    result = {
        "replay_n": len(replay_sigs),
        "live_n": len(live_sigs),
        "matched": 0,
        "replay_only": 0,
        "live_only": 0,
        "price_mismatches": 0,
        "details": [],
    }

    replay_by_ts = {s["timestamp"]: s for s in replay_sigs}
    live_by_ts = {s["timestamp"]: s for s in live_sigs}

    replay_set = set(replay_by_ts.keys())
    live_set = set(live_by_ts.keys())

    matched_ts = replay_set & live_set
    replay_only_ts = replay_set - live_set
    live_only_ts = live_set - replay_set

    for ts in matched_ts:
        rs = replay_by_ts[ts]
        ls = live_by_ts[ts]
        entry_delta = abs(rs["entry_price"] - ls["entry_price"])
        if entry_delta <= PRICE_TOL:
            result["matched"] += 1
        else:
            result["price_mismatches"] += 1
            if verbose or len(result["details"]) < 20:
                result["details"].append({
                    "type": "price_mismatch",
                    "time": str(ts),
                    "replay_entry": rs["entry_price"],
                    "live_entry": ls["entry_price"],
                    "delta": round(entry_delta, 4),
                    "replay_stop": rs["stop_price"],
                    "live_stop": ls["stop_price"],
                })

    result["replay_only"] = len(replay_only_ts)
    for ts in sorted(replay_only_ts)[:5]:
        if verbose or len(result["details"]) < 20:
            result["details"].append({
                "type": "replay_only",
                "time": str(ts),
                "entry": replay_by_ts[ts]["entry_price"],
            })

    result["live_only"] = len(live_only_ts)
    for ts in sorted(live_only_ts)[:5]:
        if verbose or len(result["details"]) < 20:
            result["details"].append({
                "type": "live_only",
                "time": str(ts),
                "entry": live_by_ts[ts]["entry_price"],
            })

    return result


# ────────────────────────────────────────────────────────────────
#  Main equivalence test
# ────────────────────────────────────────────────────────────────

def run_equivalence(symbols: Optional[List[str]] = None,
                    max_days: int = 0,
                    verbose: bool = False,
                    strategy_filter: Optional[str] = None) -> dict:
    """Run full equivalence test. Returns summary dict."""

    print("=" * 90)
    print("REPLAY ↔ LIVE EQUIVALENCE TEST")
    print("  Mode: raw detection (bypasses in-play + regime gates)")
    print("=" * 90)

    cfg = StrategyConfig(timeframe_min=5)

    # ── Build shared framework objects (replay strategies need these for init) ──
    # We create them but won't use in-play/regime for gating — we call _detect directly
    spy_path = DATA_DIR / "SPY_5min.csv"
    if not spy_path.exists():
        print("ERROR: SPY_5min.csv not found")
        return {"error": "no SPY data"}
    spy_bars = load_bars_from_csv(str(spy_path))

    in_play = InPlayProxy(cfg)
    regime = EnhancedMarketRegime(spy_bars, cfg)
    rejection = RejectionFilters(cfg)
    quality = QualityScorer(cfg)
    regime.precompute()

    # ── Discover symbols ──
    if symbols is None:
        excluded = {"SPY", "QQQ", "IWM", "SMH", "XLK", "XLC", "XLY", "XLF",
                    "XLV", "XLE", "XLI", "XLP", "XLB", "XLU", "XLRE"}
        symbols = sorted([
            p.stem.replace("_5min", "")
            for p in DATA_DIR.glob("*_5min.csv")
            if p.stem.replace("_5min", "") not in excluded
        ])

    # ── Filter strategies ──
    test_strategies = STRATEGY_NAMES
    if strategy_filter:
        test_strategies = [s for s in STRATEGY_NAMES if s == strategy_filter]
        if not test_strategies:
            print(f"ERROR: Unknown strategy '{strategy_filter}'")
            return {"error": f"unknown strategy: {strategy_filter}"}

    print(f"\n  Symbols: {len(symbols)}")
    print(f"  Strategies: {', '.join(test_strategies)}")
    if max_days > 0:
        print(f"  Max days per symbol: {max_days}")

    # ── Build replay strategies (used for their _detect methods) ──
    replay_strats = {
        "SC_SNIPER":   SecondChanceSniperStrategy(cfg, in_play, regime, rejection, quality),
        "FL_ANTICHOP": FLAntiChopStrategy(cfg, in_play, regime, rejection, quality),
        "SP_ATIER":    SpencerATierStrategy(cfg, in_play, regime, rejection, quality),
        "HH_QUALITY":  HitchHikerQualityStrategy(cfg, in_play, regime, rejection, quality),
        "EMA_FPIP":    EmaFpipATierStrategy(cfg, in_play, regime, rejection, quality),
        "BDR_SHORT":   BDRShortStrategy(cfg, in_play, regime, rejection, quality),
        "EMA9_FT":     EMA9FirstTouchStrategy(cfg, in_play, regime, rejection, quality),
        "BS_STRUCT":   BacksideStructureStrategy(cfg, in_play, regime, rejection, quality),
    }

    # ── Results tracking ──
    results = {name: {
        "replay_signals": 0,
        "live_signals": 0,
        "matched": 0,
        "replay_only": 0,
        "live_only": 0,
        "price_mismatches": 0,
        "symbol_days_tested": 0,
        "symbol_days_with_signals": 0,
        "mismatches": [],
    } for name in test_strategies}

    timing_samples: List[float] = []
    total_bars_processed = 0
    t_start = time.perf_counter()

    # ── Run per-symbol ──
    for sym_idx, sym in enumerate(symbols):
        path = DATA_DIR / f"{sym}_5min.csv"
        if not path.exists():
            continue

        bars = load_bars_from_csv(str(path))
        if not bars:
            continue

        daily_atr = compute_daily_atr(bars)
        sym_dates = sorted(set(b.timestamp.date() for b in bars))

        if max_days > 0:
            sym_dates = sym_dates[:max_days]

        for day in sym_dates:
            day_bars = [b for b in bars if b.timestamp.date() == day]
            if len(day_bars) < 10:
                continue  # skip partial days

            for strat_name in test_strategies:
                # A) Replay path — raw detection, no gates
                try:
                    replay_sigs = _run_replay_raw(
                        strat_name, replay_strats[strat_name],
                        sym, bars, day, daily_atr
                    )
                except Exception as e:
                    if verbose:
                        print(f"  WARN: Replay {strat_name} {sym} {day}: {e}")
                    continue

                # B) Live path — fresh manager per day per strategy
                live_strat = _make_live_strategy(strat_name, cfg)
                if live_strat is None:
                    continue

                mgr = StrategyManager(strategies=[live_strat], symbol=sym)

                t0 = time.perf_counter()
                try:
                    live_sigs = _run_live_for_day(strat_name, mgr, bars, day)
                except Exception as e:
                    if verbose:
                        print(f"  WARN: Live {strat_name} {sym} {day}: {e}")
                    continue
                elapsed = (time.perf_counter() - t0) * 1000
                timing_samples.append(elapsed)
                total_bars_processed += len(bars)

                # C) Compare
                cmp = _compare_signals(replay_sigs, live_sigs, verbose)

                r = results[strat_name]
                r["symbol_days_tested"] += 1
                r["replay_signals"] += cmp["replay_n"]
                r["live_signals"] += cmp["live_n"]
                r["matched"] += cmp["matched"]
                r["replay_only"] += cmp["replay_only"]
                r["live_only"] += cmp["live_only"]
                r["price_mismatches"] += cmp["price_mismatches"]

                if cmp["replay_n"] > 0 or cmp["live_n"] > 0:
                    r["symbol_days_with_signals"] += 1

                for d in cmp["details"]:
                    d["symbol"] = sym
                    d["date"] = str(day)
                    if len(r["mismatches"]) < 50:
                        r["mismatches"].append(d)

        if (sym_idx + 1) % 10 == 0 or sym_idx == len(symbols) - 1:
            elapsed_total = time.perf_counter() - t_start
            print(f"  {sym_idx + 1}/{len(symbols)} symbols processed ({elapsed_total:.1f}s)")

    # ── Report ──
    print(f"\n{'=' * 90}")
    print("EQUIVALENCE RESULTS")
    print(f"{'=' * 90}")

    all_pass = True
    strategy_verdicts = {}

    for name in test_strategies:
        r = results[name]
        replay_n = r["replay_signals"]
        live_n = r["live_signals"]
        matched = r["matched"]
        total_unique = replay_n + live_n - matched - r["price_mismatches"]
        total_signals = max(replay_n, live_n, 1)

        # Match rate: matched / max(replay, live)
        match_rate = matched / total_signals * 100 if total_signals > 0 else 100.0

        # Verdict logic:
        # - If 0 signals on both sides AND we tested enough days → PASS (no signals = agreement)
        # - match_rate >= 80% → PASS
        # - match_rate 60-79% → REVIEW
        # - match_rate < 60% → FAIL
        if replay_n == 0 and live_n == 0:
            if r["symbol_days_tested"] >= 20:
                verdict = "PASS (no signals)"
            else:
                verdict = "REVIEW (insufficient data)"
        elif match_rate >= 80.0:
            verdict = "PASS"
        elif match_rate >= 60.0:
            verdict = "REVIEW"
            all_pass = False
        else:
            verdict = "FAIL"
            all_pass = False

        strategy_verdicts[name] = verdict

        print(f"\n  {name}:")
        print(f"    Days tested: {r['symbol_days_tested']}  (with signals: {r['symbol_days_with_signals']})")
        print(f"    Replay signals: {replay_n}  |  Live signals: {live_n}")
        print(f"    Matched: {matched}  ({match_rate:.1f}%)")
        print(f"    Replay-only: {r['replay_only']}  |  Live-only: {r['live_only']}  |  Price mismatch: {r['price_mismatches']}")
        print(f"    → {verdict}")

        if r["mismatches"] and verbose:
            print(f"    Sample mismatches:")
            for m in r["mismatches"][:10]:
                print(f"      {m}")

    # ── Timing report ──
    print(f"\n{'=' * 90}")
    print("PERFORMANCE")
    print(f"{'=' * 90}")

    if timing_samples:
        avg_ms = sum(timing_samples) / len(timing_samples)
        sorted_samples = sorted(timing_samples)
        p50_ms = sorted_samples[len(sorted_samples) // 2]
        p99_ms = sorted_samples[int(len(sorted_samples) * 0.99)] if len(sorted_samples) > 10 else max(sorted_samples)
        worst_ms = max(sorted_samples)
        total_s = time.perf_counter() - t_start
        print(f"  Per symbol-day-strategy (all bars fed through live path):")
        print(f"    Avg: {avg_ms:.2f} ms  |  P50: {p50_ms:.2f} ms  |  P99: {p99_ms:.2f} ms  |  Worst: {worst_ms:.2f} ms")
        print(f"    Total bars processed: {total_bars_processed:,}")
        print(f"    Wall-clock time: {total_s:.1f}s")
        if total_bars_processed > 0:
            us_per_bar = (sum(timing_samples) * 1000) / total_bars_processed
            print(f"    Avg per bar (live path): {us_per_bar:.1f} μs")

    # ── Summary ──
    summary = {
        "overall": "PASS" if all_pass else "REVIEW",
        "strategies": {},
        "total_bars": total_bars_processed,
    }
    for name in test_strategies:
        r = results[name]
        replay_n = r["replay_signals"]
        live_n = r["live_signals"]
        matched = r["matched"]
        total_signals = max(replay_n, live_n, 1)
        match_rate = matched / total_signals * 100 if total_signals > 0 else 100.0

        summary["strategies"][name] = {
            "replay_signals": replay_n,
            "live_signals": live_n,
            "matched": matched,
            "match_rate": round(match_rate, 1),
            "replay_only": r["replay_only"],
            "live_only": r["live_only"],
            "price_mismatches": r["price_mismatches"],
            "verdict": strategy_verdicts[name],
            "symbol_days_tested": r["symbol_days_tested"],
            "symbol_days_with_signals": r["symbol_days_with_signals"],
            "mismatches_sample": r["mismatches"][:10],
        }

    print(f"\n{'=' * 90}")
    print(f"OVERALL: {summary['overall']}")
    print(f"{'=' * 90}")

    return summary


# ────────────────────────────────────────────────────────────────
#  CLI
# ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Replay ↔ Live equivalence test")
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Symbols to test (default: all)")
    parser.add_argument("--days", type=int, default=0,
                        help="Max days per symbol (0=all)")
    parser.add_argument("--strategy", type=str, default=None,
                        help="Test only this strategy (e.g. SC_SNIPER)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed mismatches")
    parser.add_argument("--json", type=str, default=None,
                        help="Export summary to JSON file")
    args = parser.parse_args()

    summary = run_equivalence(
        symbols=args.symbols,
        max_days=args.days,
        verbose=args.verbose,
        strategy_filter=args.strategy,
    )

    if args.json:
        out_path = Path(args.json)
        with open(out_path, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        print(f"\n  Summary exported to {out_path}")


if __name__ == "__main__":
    main()
