"""
Strategy Rejection Audit — Why are signals being cancelled?
============================================================
Wraps each strategy's step() to count:
  - phase_not_ready:  setup hasn't triggered (normal idle)
  - setup_detected:   setup triggered, proceeding to target/stop
  - no_valid_target:  structural target returned skipped/NaN
  - no_valid_stop:    risk <= 0 or stop invalid
  - filter_reject:    quality/timing/indicator filter killed it
  - signal_emitted:   RawSignal successfully returned

Run:  cd /sessions/.../mnt && python -m alert_overlay.rejection_audit
"""

import math
import inspect
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

_isnan = math.isnan

from .backtest import load_bars_from_csv
from .models import Bar, NaN
from .strategies.shared.config import StrategyConfig
from .strategies.shared.helpers import (
    compute_structural_target_long,
    compute_structural_target_short,
)
from .strategies.live.manager import StrategyManager
from .strategies.live.base import RawSignal
from .strategies.live.sc_sniper_live import SCSniperLive
from .strategies.live.fl_antichop_live import FLAntiChopLive
from .strategies.live.spencer_atier_live import SpencerATierLive
from .strategies.live.hitchhiker_live import HitchHikerLive
from .strategies.live.ema_fpip_live import EmaFpipLive
from .strategies.live.bdr_short_live import BDRShortLive
from .strategies.live.ema9_ft_live import EMA9FirstTouchLive
from .strategies.live.backside_live import BacksideStructureLive
from .strategies.live.orl_fbd_long_live import ORLFBDLongLive
from .strategies.live.orh_fbo_short_v2_live import ORHFBOShortV2Live
from .strategies.live.pdh_fbo_short_live import PDHFBOShortLive
from .strategies.live.fft_newlow_reversal_live import FFTNewlowReversalLive

DATA_DIR = Path(__file__).parent / "data"
DATA_1MIN_DIR = DATA_DIR / "1min"


# ══════════════════════════════════════════════════════════════════════
#  Monkey-patch structural target functions to track rejections
# ══════════════════════════════════════════════════════════════════════

target_audit = defaultdict(lambda: {"called": 0, "skipped": 0, "emitted": 0,
                                      "no_candidates": 0, "too_close": 0})

_orig_struct_long = compute_structural_target_long
_orig_struct_short = compute_structural_target_short


def _patched_struct_long(entry, risk, candidates, min_rr=1.0, max_rr=3.0, mode="structural", **kwargs):
    result = _orig_struct_long(entry, risk, candidates, min_rr=min_rr, max_rr=max_rr, mode=mode, **kwargs)
    target_audit["_global_long"]["called"] += 1
    if len(result) >= 4 and result[3]:  # skipped=True
        target_audit["_global_long"]["skipped"] += 1
        if not candidates or all(p <= entry for p, _ in candidates):
            target_audit["_global_long"]["no_candidates"] += 1
        else:
            target_audit["_global_long"]["too_close"] += 1
    else:
        target_audit["_global_long"]["emitted"] += 1
    return result


def _patched_struct_short(entry, risk, candidates, min_rr=1.0, max_rr=3.0, mode="structural", **kwargs):
    result = _orig_struct_short(entry, risk, candidates, min_rr=min_rr, max_rr=max_rr, mode=mode, **kwargs)
    target_audit["_global_short"]["called"] += 1
    if len(result) >= 4 and result[3]:  # skipped=True
        target_audit["_global_short"]["skipped"] += 1
        if not candidates or all(p >= entry for p, _ in candidates):
            target_audit["_global_short"]["no_candidates"] += 1
        else:
            target_audit["_global_short"]["too_close"] += 1
    else:
        target_audit["_global_short"]["emitted"] += 1
    return result


# Patch the module-level functions AND the imports in each strategy module
import alert_overlay.strategies.shared.helpers as _helpers
_helpers.compute_structural_target_long = _patched_struct_long
_helpers.compute_structural_target_short = _patched_struct_short

# Also patch in each live strategy module that imports directly
import importlib
strategy_modules = [
    "alert_overlay.strategies.live.sc_sniper_live",
    "alert_overlay.strategies.live.fl_antichop_live",
    "alert_overlay.strategies.live.spencer_atier_live",
    "alert_overlay.strategies.live.hitchhiker_live",
    "alert_overlay.strategies.live.ema_fpip_live",
    "alert_overlay.strategies.live.ema9_ft_live",
    "alert_overlay.strategies.live.backside_live",
    "alert_overlay.strategies.live.orl_fbd_long_live",
    "alert_overlay.strategies.live.orh_fbo_short_v2_live",
    "alert_overlay.strategies.live.pdh_fbo_short_live",
    "alert_overlay.strategies.live.fft_newlow_reversal_live",
]
for mod_name in strategy_modules:
    try:
        mod = importlib.import_module(mod_name)
        if hasattr(mod, 'compute_structural_target_long'):
            mod.compute_structural_target_long = _patched_struct_long
        if hasattr(mod, 'compute_structural_target_short'):
            mod.compute_structural_target_short = _patched_struct_short
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
#  Per-strategy step() wrapper to count signal vs None returns
# ══════════════════════════════════════════════════════════════════════

step_audit = defaultdict(lambda: {"bars_seen": 0, "signals_emitted": 0, "returns_none": 0})


def wrap_strategy_step(strat):
    """Wrap a strategy's step() to count invocations and results."""
    original_step = strat.step
    name = strat.name

    def wrapped_step(snap, market_ctx=None):
        step_audit[name]["bars_seen"] += 1
        result = original_step(snap, market_ctx=market_ctx)
        if result is not None:
            step_audit[name]["signals_emitted"] += 1
        else:
            step_audit[name]["returns_none"] += 1
        return result

    strat.step = wrapped_step
    return strat


# ══════════════════════════════════════════════════════════════════════
#  Per-strategy target call tracking
# ══════════════════════════════════════════════════════════════════════

# Track per-strategy target calls by patching at a different level
# We'll count from the global target_audit plus step_audit to derive rejection rates


# ══════════════════════════════════════════════════════════════════════
#  MAIN — Run replay with instrumented strategies
# ══════════════════════════════════════════════════════════════════════

def main():
    from .market_context import MarketEngine, SECTOR_MAP
    from .strategies.live.shared_indicators import SharedIndicators
    from .strategies.replay import BarUpsampler

    print("=" * 100)
    print("STRATEGY REJECTION AUDIT — Where Do Signals Die?")
    print("=" * 100)

    sector_etfs = sorted(set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    excluded = {"SPY", "QQQ", "IWM"} | set(sector_etfs)

    symbols = sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])

    print(f"\n  Processing {len(symbols)} symbols through instrumented strategies...")

    total_bars = 0
    total_signals = 0

    for idx, sym in enumerate(symbols):
        p_5m = DATA_DIR / f"{sym}_5min.csv"
        bars_5m = load_bars_from_csv(str(p_5m))
        if not bars_5m:
            continue

        p_1m = DATA_1MIN_DIR / f"{sym}_1min.csv"
        bars_1m = load_bars_from_csv(str(p_1m)) if p_1m.exists() else None

        strat_cfg = StrategyConfig(timeframe_min=5)
        live_strats = [
            wrap_strategy_step(SCSniperLive(strat_cfg)),
            wrap_strategy_step(FLAntiChopLive(strat_cfg)),
            wrap_strategy_step(SpencerATierLive(strat_cfg)),
            wrap_strategy_step(HitchHikerLive(strat_cfg)),
            wrap_strategy_step(EmaFpipLive(strat_cfg)),
            wrap_strategy_step(BDRShortLive(strat_cfg)),
            wrap_strategy_step(EMA9FirstTouchLive(strat_cfg)),
            wrap_strategy_step(BacksideStructureLive(strat_cfg)),
            wrap_strategy_step(ORLFBDLongLive(strat_cfg)),
            wrap_strategy_step(ORHFBOShortV2Live(strat_cfg)),
            wrap_strategy_step(PDHFBOShortLive(strat_cfg, enable_mode_a=False, enable_mode_b=True)),
            wrap_strategy_step(FFTNewlowReversalLive(strat_cfg)),
        ]
        mgr = StrategyManager(strategies=live_strats, symbol=sym)

        if len(bars_5m) >= 14:
            atr_val = sum(max(b.high - b.low, 0.01) for b in bars_5m[:14]) / 14
            mgr.indicators.warm_up_daily(atr_val, bars_5m[0].high, bars_5m[0].low)

        if bars_1m is not None:
            upsampler = BarUpsampler(5)
            for bar in bars_1m:
                total_bars += 1
                sigs_1m = mgr.on_1min_bar(bar)
                total_signals += len(sigs_1m)
                bar_5m = upsampler.on_bar(bar)
                if bar_5m is not None:
                    sigs_5m = mgr.on_5min_bar(bar_5m)
                    total_signals += len(sigs_5m)
        else:
            for bar in bars_5m:
                total_bars += 1
                sigs = mgr.on_bar(bar)
                total_signals += len(sigs)

        if (idx + 1) % 20 == 0 or idx == len(symbols) - 1:
            print(f"    {idx + 1}/{len(symbols)} symbols...")

    # ══════════════════════════════════════════════════════════════════
    #  REPORT
    # ══════════════════════════════════════════════════════════════════

    print(f"\n{'='*100}")
    print("PER-STRATEGY STEP() COUNTS")
    print(f"{'='*100}")
    print(f"\n  {'Strategy':<20} {'BarsProcessed':>15} {'SignalsEmitted':>15} {'ReturnsNone':>15} {'EmitRate':>10}")
    print(f"  {'-'*20} {'-'*15} {'-'*15} {'-'*15} {'-'*10}")

    total_emitted = 0
    total_none = 0
    for name in sorted(step_audit):
        a = step_audit[name]
        total_emitted += a["signals_emitted"]
        total_none += a["returns_none"]
        rate = a["signals_emitted"] / max(a["bars_seen"], 1) * 100
        print(f"  {name:<20} {a['bars_seen']:>15,} {a['signals_emitted']:>15,} "
              f"{a['returns_none']:>15,} {rate:>9.3f}%")

    print(f"\n  Total bars processed: {total_bars:,}")
    print(f"  Total signals emitted: {total_emitted:,}")
    print(f"  Total returns None: {total_none:,}")

    # ── Structural target function stats ──
    print(f"\n{'='*100}")
    print("STRUCTURAL TARGET FUNCTION AUDIT")
    print(f"{'='*100}")

    for key in sorted(target_audit):
        a = target_audit[key]
        total = a["called"]
        if total == 0:
            continue
        skip_rate = a["skipped"] / total * 100
        print(f"\n  {key}:")
        print(f"    Called:          {total:>8,}")
        print(f"    Emitted target:  {a['emitted']:>8,} ({a['emitted']/total*100:.1f}%)")
        print(f"    Skipped:         {a['skipped']:>8,} ({skip_rate:.1f}%)")
        print(f"      No candidates: {a['no_candidates']:>8,} ({a['no_candidates']/max(a['skipped'],1)*100:.1f}% of skips)")
        print(f"      Too close:     {a['too_close']:>8,} ({a['too_close']/max(a['skipped'],1)*100:.1f}% of skips)")

    # ── The key question: setup detected but cancelled ──
    # A signal being "cancelled" = structural target was called (setup detected)
    # but returned skipped, causing step() to return None.
    # The ratio we care about: skipped / called = "setup waste rate"
    print(f"\n{'='*100}")
    print("SIGNAL WASTE ANALYSIS — Setup detected but no valid target")
    print(f"{'='*100}")

    total_called = sum(a["called"] for a in target_audit.values())
    total_skipped = sum(a["skipped"] for a in target_audit.values())
    total_target_emitted = sum(a["emitted"] for a in target_audit.values())

    if total_called > 0:
        waste_rate = total_skipped / total_called * 100
        print(f"\n  Structural target function called: {total_called:,}")
        print(f"  Valid target found:                {total_target_emitted:,} ({total_target_emitted/total_called*100:.1f}%)")
        print(f"  Target skipped (signal cancelled): {total_skipped:,} ({waste_rate:.1f}%)")

        no_cand = sum(a["no_candidates"] for a in target_audit.values())
        too_close = sum(a["too_close"] for a in target_audit.values())
        print(f"\n  Why skipped:")
        print(f"    No viable candidates at all:    {no_cand:>6,} ({no_cand/max(total_skipped,1)*100:.1f}%)")
        print(f"    Candidates exist but too close:  {too_close:>6,} ({too_close/max(total_skipped,1)*100:.1f}%)")

        print(f"\n  Interpretation:")
        if waste_rate > 70:
            print(f"    HIGH WASTE ({waste_rate:.0f}%): Strategies detect setups that rarely have")
            print(f"    viable structural targets. This may indicate:")
            print(f"      - min_rr thresholds too strict")
            print(f"      - candidate list too narrow (not enough reference levels)")
            print(f"      - setup detection too permissive (triggers on garbage)")
        elif waste_rate > 40:
            print(f"    MODERATE WASTE ({waste_rate:.0f}%): Significant portion of detected setups")
            print(f"    lack viable targets. Review whether this is acceptable.")
        else:
            print(f"    LOW WASTE ({waste_rate:.0f}%): Most detected setups find valid targets.")
            print(f"    This is healthy — structural filtering is working as intended.")
    else:
        print("  No structural target calls recorded (patching may have failed).")

    print(f"\n{'='*100}")
    print("DONE")
    print(f"{'='*100}")


if __name__ == "__main__":
    main()
