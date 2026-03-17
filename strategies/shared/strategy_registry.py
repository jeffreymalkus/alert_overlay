"""
Strategy Registry — canonical status labels and metadata for all strategies.

Status definitions:
  ACTIVE       — promoted, in live pipeline, full conviction
  PAPER        — promoted replay metrics, active in live pipeline for paper tracking
  PROBATIONARY — passes promotion criteria but marginal metrics; live but lower conviction
  SHELVED      — built and evaluated, held back (does not meet criteria or thesis weak)
  RETIRED      — previously active, removed due to degradation
  DEFERRED     — design assessed, not built

This registry is the single source of truth for which strategies are deployed
and their current operational status.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class StrategyStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAPER = "PAPER"
    PROBATIONARY = "PROBATIONARY"
    SHELVED = "SHELVED"
    RETIRED = "RETIRED"
    DEFERRED = "DEFERRED"


@dataclass
class StrategyEntry:
    """Registry entry for one strategy."""
    name: str
    status: StrategyStatus
    direction: int          # 1=LONG, -1=SHORT
    timeframe: int          # 1 or 5
    regime_gate: str        # e.g. "GREEN-only", "RED/FLAT-only", "any"
    family: str             # strategy family grouping
    version: str            # "v1", "v2", etc.
    replay_pf: float        # replay profit factor
    replay_n: int           # replay trade count
    replay_r: float         # replay total R
    notes: str = ""


# ── CANONICAL REGISTRY ──────────────────────────────────────────────

STRATEGY_REGISTRY = [
    # ── 5-min strategies (LONG, GREEN-day) ── [v3: structural targets w/ fixed fallback]
    StrategyEntry(
        name="SC_SNIPER", status=StrategyStatus.ACTIVE,
        direction=1, timeframe=5, regime_gate="GREEN-only",
        family="breakout_retest", version="v3",
        replay_pf=1.21, replay_n=25, replay_r=3.0,
        notes="Breakout → retest → confirmation. v3: structural targets (session_high, PDH) "
              "with fixed_rr fallback when no structural level above entry.",
    ),
    StrategyEntry(
        name="FL_ANTICHOP", status=StrategyStatus.ACTIVE,
        direction=1, timeframe=5, regime_gate="GREEN-only",
        family="trend_turn", version="v3",
        replay_pf=1.32, replay_n=111, replay_r=12.0,
        notes="Decline → turn → EMA9 crosses VWAP. v3: structural targets "
              "(decline_high, session_high, PDH). Highest N strategy. PROMOTED.",
    ),
    StrategyEntry(
        name="SP_ATIER", status=StrategyStatus.ACTIVE,
        direction=1, timeframe=5, regime_gate="GREEN-only",
        family="consolidation_breakout", version="v3",
        replay_pf=0.98, replay_n=13, replay_r=-0.1,
        notes="Uptrend → tight box → breakout. v3: structural targets "
              "(measured_move, session_high, PDH).",
    ),
    StrategyEntry(
        name="HH_QUALITY", status=StrategyStatus.ACTIVE,
        direction=1, timeframe=5, regime_gate="GREEN-only",
        family="momentum_continuation", version="v3",
        replay_pf=2.09, replay_n=85, replay_r=25.9,
        notes="Opening drive → consolidation → breakout. v3: structural targets "
              "(drive_high, session_high). PROMOTED 2026-03-16: dual-tf replay "
              "PF=2.09, 64.7% WR, passes all criteria incl train/test/walkfwd.",
    ),
    StrategyEntry(
        name="EMA_FPIP", status=StrategyStatus.ACTIVE,
        direction=1, timeframe=5, regime_gate="GREEN-only",
        family="pullback_continuation", version="v3",
        replay_pf=2.00, replay_n=24, replay_r=6.0,
        notes="Expansion → pullback → trigger. v3: structural targets "
              "(exp_high, session_high, PDH). Best PF of 5-min strategies.",
    ),
    StrategyEntry(
        name="BDR_SHORT", status=StrategyStatus.ACTIVE,
        direction=-1, timeframe=5, regime_gate="RED/FLAT",
        family="breakdown_retest", version="v3",
        replay_pf=float("inf"), replay_n=1, replay_r=0.0,
        notes="Breakdown → retest → rejection wick. v3: structural targets "
              "(session_low, PDL, bd_extension). Min 1.0R geometry, cap 3.0R. "
              "Low N in replay — needs more data.",
    ),
    StrategyEntry(
        name="BS_STRUCT", status=StrategyStatus.ACTIVE,
        direction=1, timeframe=5, regime_gate="GREEN-only",
        family="structure_breakout", version="v3",
        replay_pf=8.00, replay_n=10, replay_r=7.0,
        notes="Decline → HH/HL structure → range → breakout. v3: structural targets "
              "(VWAP, session_high). Strong PF.",
    ),

    # ── 1-min strategies ──
    StrategyEntry(
        name="EMA9_FT", status=StrategyStatus.ACTIVE,
        direction=1, timeframe=1, regime_gate="GREEN-only",
        family="pullback_continuation", version="v3",
        replay_pf=3.41, replay_n=4, replay_r=2.4,
        notes="Drive → first pullback to E9 → reclaim. v3: structural targets "
              "(drive_high, session_high). In-play gated replay: N=4. "
              "Live: daily cap=3 (first-in-time), SPY>=+0.50% gate.",
    ),

    # ── ORL family ──
    StrategyEntry(
        name="ORL_FBD_LONG", status=StrategyStatus.ACTIVE,
        direction=1, timeframe=5, regime_gate="GREEN-only",
        family="failed_move_long", version="v3",
        replay_pf=0.82, replay_n=36, replay_r=-1.1,
        notes="Breakdown below ORL → reclaim → HH confirm. v3: structural targets "
              "(VWAP, ORH, session_high). Under review — negative expectancy.",
    ),
    StrategyEntry(
        name="ORL_FBD_V2", status=StrategyStatus.SHELVED,
        direction=1, timeframe=1, regime_gate="GREEN-only",
        family="failed_move_long", version="v3",
        replay_pf=0.00, replay_n=19, replay_r=-3.0,
        notes="Hybrid 1m sequencing. v3: structural targets. v1 superior. SHELVED.",
    ),

    # ── Failed-Breakout-Short family ── [v3: structural targets]
    # Family core: ORH_ALL + PDH_ALL (PF=1.36, N=54, +8.7R)
    # Zero day+symbol overlap between ORH and PDH.
    StrategyEntry(
        name="ORH_FBO_V2_A", status=StrategyStatus.ACTIVE,
        direction=-1, timeframe=1, regime_gate="RED/FLAT-only",
        family="failed_breakout_short", version="v3",
        replay_pf=1.24, replay_n=16, replay_r=2.2,
        notes="FAMILY CORE. ORH failed retest short. v3: structural targets "
              "(VWAP, ORL, session_low). 100% RED-day trades.",
    ),
    StrategyEntry(
        name="ORH_FBO_V2_B", status=StrategyStatus.PROBATIONARY,
        direction=-1, timeframe=1, regime_gate="RED/FLAT-only",
        family="failed_breakout_short", version="v3",
        replay_pf=1.37, replay_n=18, replay_r=2.6,
        notes="ORH continuation short. v3: structural targets. "
              "Improved vs v2 fixed targets (PF 1.23→1.37).",
    ),
    StrategyEntry(
        name="ORH_FBO_SHORT_V1", status=StrategyStatus.SHELVED,
        direction=-1, timeframe=5, regime_gate="any",
        family="failed_breakout_short", version="v1",
        replay_pf=0.86, replay_n=16, replay_r=-1.2,
        notes="5-min only. PF < 1.0. Superseded by v2/v3 hybrid.",
    ),
    StrategyEntry(
        name="PDH_FBO_A", status=StrategyStatus.SHELVED,
        direction=-1, timeframe=1, regime_gate="RED/FLAT-only",
        family="failed_breakout_short", version="v3",
        replay_pf=1.18, replay_n=9, replay_r=1.1,
        notes="Held back. N=9 (below N≥10). v3: structural targets improved PF 1.11→1.18.",
    ),
    StrategyEntry(
        name="PDH_FBO_B", status=StrategyStatus.ACTIVE,
        direction=-1, timeframe=1, regime_gate="RED/FLAT-only",
        family="failed_breakout_short", version="v3",
        replay_pf=2.40, replay_n=11, replay_r=2.8,
        notes="FAMILY CORE. PDH continuation short. v3: structural targets "
              "(VWAP, ORL, session_low). PF=2.40. Zero overlap with ORH.",
    ),

    # ── FFT New-Low Reversal (1-min, LONG, failed-long regime) ──
    StrategyEntry(
        name="FFT_NEWLOW_REV", status=StrategyStatus.PAPER,
        direction=1, timeframe=1, regime_gate="GREEN+FLAT",
        family="failed_move_long", version="v1",
        replay_pf=0.0, replay_n=0, replay_r=0.0,
        notes="Failed follow-through new-low reversal. Flush below session low / PDL, "
              "spring bar reclaims, confirmation clears spring high. 1-min trigger. "
              "Dedup: only fires below OR_low. Max stop 2.5 ATR. PAPER until replay eval.",
    ),

    # ── Deferred ──
    StrategyEntry(
        name="NEUTRAL_VWAP_FADE", status=StrategyStatus.DEFERRED,
        direction=0, timeframe=1, regime_gate="FLAT-only",
        family="mean_reversion", version="design",
        replay_pf=0.0, replay_n=0, replay_r=0.0,
        notes="Range-edge VWAP rejection fade on FLAT days. Thesis too weak to build.",
    ),
]


def get_active_strategies() -> list:
    """Return strategies that should be in the live pipeline."""
    return [s for s in STRATEGY_REGISTRY if s.status in (
        StrategyStatus.ACTIVE, StrategyStatus.PAPER, StrategyStatus.PROBATIONARY
    )]


def get_by_family(family: str) -> list:
    """Return all strategies in a family."""
    return [s for s in STRATEGY_REGISTRY if s.family == family]


def print_registry():
    """Print the full registry in a readable format."""
    families = {}
    for s in STRATEGY_REGISTRY:
        families.setdefault(s.family, []).append(s)

    for fam, entries in sorted(families.items()):
        print(f"\n  ── {fam.upper()} ──")
        for s in entries:
            dir_str = {1: "LONG", -1: "SHORT", 0: "BOTH"}.get(s.direction, "?")
            status_pad = f"[{s.status.value}]"
            print(f"    {s.name:<22} {status_pad:<16} {dir_str:<6} {s.timeframe}m  "
                  f"PF={s.replay_pf:>5.2f}  N={s.replay_n:>3}  R={s.replay_r:>+6.1f}  "
                  f"regime={s.regime_gate}")
            if s.notes:
                print(f"      {s.notes}")


if __name__ == "__main__":
    print("STRATEGY REGISTRY")
    print("=" * 100)
    print_registry()

    print(f"\n\n{'=' * 100}")
    print("ACTIVE/PAPER/PROBATIONARY (in live pipeline):")
    for s in get_active_strategies():
        dir_str = {1: "LONG", -1: "SHORT", 0: "BOTH"}.get(s.direction, "?")
        print(f"  [{s.status.value:<13}] {s.name:<22} {dir_str:<6} {s.timeframe}m  PF={s.replay_pf:.2f}  N={s.replay_n}")

    print(f"\nFailed Breakout Short family:")
    for s in get_by_family("failed_breakout_short"):
        print(f"  [{s.status.value:<13}] {s.name:<22} v={s.version}  PF={s.replay_pf:.2f}  N={s.replay_n}")
