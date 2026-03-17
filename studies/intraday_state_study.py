"""
Intraday State Detection Study — RESEARCH ONLY.

The current regime model uses 2 crude inputs (pct_from_open + close position
in day range) to produce labels like RED+CHOPPY or GREEN+TREND.  This misses
important intraday transitions: a day that drops -2% then bounces to -0.5%
reads as RED+CHOPPY, but the state at signal time is actually RECOVERY.

This study builds a richer state classifier that runs INSIDE the broad
envelope (Layer 1), adding a Layer 1.5 that classifies the current intraday
market state using better inputs:

  Inputs (computed over rolling windows of SPY 5-min bars):
    1. Directional efficiency / chop ratio  — |net move| / total path
    2. Short-term slope / momentum          — EMA5 slope, normalized
    3. Rebound from day low / fade from high — positional
    4. Velocity of state change             — acceleration

  Output states (6):
    TREND_DOWN   — persistent selling, close near lows, high efficiency
    WASHOUT      — sharp drop followed by deceleration (capitulation zone)
    RECOVERY     — bouncing off lows, rising slope, improving efficiency
    SQUEEZE      — tight range compressing, very low efficiency, low volume
    SIDEWAYS     — mid-range, no direction, moderate path
    TREND_UP     — persistent buying, close near highs, high efficiency

  Evaluation:
    - For each historical trade, compute the intraday state at signal time
    - Compare R-outcomes across states within the same envelope
    - Identify which RED+CHOPPY trades were actually RECOVERY vs SIDEWAYS
    - Measure whether state adds predictive separation beyond current regime
"""

import csv
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..backtest import load_bars_from_csv, run_backtest
from ..config import OverlayConfig
from ..models import Bar, Signal, NaN, SETUP_DISPLAY_NAME
from ..market_context import get_sector_etf

DATA_DIR = Path(__file__).parent.parent / "data"


# ═══════════════════════════════════════════════════════════════════
#  Intraday State Definitions
# ═══════════════════════════════════════════════════════════════════

class IntradayState(Enum):
    TREND_DOWN = "TREND_DOWN"
    WASHOUT = "WASHOUT"
    RECOVERY = "RECOVERY"
    SQUEEZE = "SQUEEZE"
    SIDEWAYS = "SIDEWAYS"
    TREND_UP = "TREND_UP"
    UNKNOWN = "UNKNOWN"


@dataclass
class StateSnapshot:
    """All computed features for one bar, plus the classified state."""
    timestamp: datetime = None
    # ── Raw price context ────────────────────────────────────────
    pct_from_open: float = 0.0
    range_position: float = 0.5     # (close - day_low) / (day_high - day_low)
    # ── Directional efficiency (chop ratio) ──────────────────────
    efficiency_6: float = 0.0       # 6-bar (30 min) directional efficiency
    efficiency_12: float = 0.0      # 12-bar (60 min) directional efficiency
    # ── Short-term momentum / slope ──────────────────────────────
    slope_6: float = 0.0            # Avg bar-over-bar change, 6 bars
    slope_3: float = 0.0            # Avg bar-over-bar change, 3 bars
    momentum_accel: float = 0.0     # slope_3 - slope_6 (acceleration)
    # ── Positional recovery/fade ─────────────────────────────────
    rebound_from_low: float = 0.0   # (close - day_low) / day_open * 100
    fade_from_high: float = 0.0     # (day_high - close) / day_open * 100
    pct_from_low: float = 0.0       # (close - day_low) / day_low * 100
    pct_from_high: float = 0.0      # (day_high - close) / day_high * 100
    # ── Volume context ───────────────────────────────────────────
    vol_ratio_6: float = 1.0        # Avg vol last 6 bars / avg vol last 24 bars
    # ── Classified state ─────────────────────────────────────────
    state: IntradayState = IntradayState.UNKNOWN
    # ── Current regime (for comparison) ──────────────────────────
    current_regime: str = ""


# ═══════════════════════════════════════════════════════════════════
#  Feature Computation
# ═══════════════════════════════════════════════════════════════════

def compute_directional_efficiency(bars: List[Bar], idx: int, lookback: int) -> float:
    """
    |net move| / sum(|bar moves|) over lookback bars.
    Signed: positive if net up, negative if net down.
    Range: [-1, +1]. 0 = pure chop, ±1 = perfectly directional.
    """
    start = max(0, idx - lookback)
    if idx - start < 2:
        return 0.0
    net = bars[idx].close - bars[start].close
    total_path = sum(abs(bars[i].close - bars[i - 1].close)
                     for i in range(start + 1, idx + 1))
    if total_path == 0:
        return 0.0
    return max(-1.0, min(1.0, net / total_path))


def compute_slope(bars: List[Bar], idx: int, lookback: int) -> float:
    """
    Average bar-over-bar % change over lookback bars.
    Positive = price rising, negative = falling.
    Normalized to make cross-day comparable.
    """
    start = max(0, idx - lookback)
    if idx - start < 1:
        return 0.0
    changes = []
    for i in range(start + 1, idx + 1):
        if bars[i - 1].close > 0:
            changes.append((bars[i].close - bars[i - 1].close) / bars[i - 1].close * 100.0)
    if not changes:
        return 0.0
    return sum(changes) / len(changes)


def compute_volume_ratio(bars: List[Bar], idx: int, short_win: int = 6, long_win: int = 24) -> float:
    """Ratio of recent volume to longer-term average volume."""
    short_start = max(0, idx - short_win)
    long_start = max(0, idx - long_win)

    short_vol = [bars[i].volume for i in range(short_start, idx + 1) if bars[i].volume > 0]
    long_vol = [bars[i].volume for i in range(long_start, idx + 1) if bars[i].volume > 0]

    if not short_vol or not long_vol:
        return 1.0
    avg_short = sum(short_vol) / len(short_vol)
    avg_long = sum(long_vol) / len(long_vol)
    if avg_long == 0:
        return 1.0
    return avg_short / avg_long


def compute_state_snapshot(
    bars: List[Bar],
    idx: int,
    day_open: float,
    day_high: float,
    day_low: float,
) -> StateSnapshot:
    """Compute all features for one bar and classify the state."""
    bar = bars[idx]
    snap = StateSnapshot(timestamp=bar.timestamp)

    # ── Price context ────────────────────────────────────────
    if day_open > 0:
        snap.pct_from_open = (bar.close - day_open) / day_open * 100.0
    day_range = day_high - day_low
    if day_range > 0:
        snap.range_position = (bar.close - day_low) / day_range
    else:
        snap.range_position = 0.5

    # ── Directional efficiency ───────────────────────────────
    snap.efficiency_6 = compute_directional_efficiency(bars, idx, 6)
    snap.efficiency_12 = compute_directional_efficiency(bars, idx, 12)

    # ── Slope / momentum ─────────────────────────────────────
    snap.slope_6 = compute_slope(bars, idx, 6)
    snap.slope_3 = compute_slope(bars, idx, 3)
    snap.momentum_accel = snap.slope_3 - snap.slope_6  # Positive = accelerating up

    # ── Positional ───────────────────────────────────────────
    if day_open > 0:
        snap.rebound_from_low = (bar.close - day_low) / day_open * 100.0
        snap.fade_from_high = (day_high - bar.close) / day_open * 100.0
    if day_low > 0:
        snap.pct_from_low = (bar.close - day_low) / day_low * 100.0
    if day_high > 0:
        snap.pct_from_high = (day_high - bar.close) / day_high * 100.0

    # ── Volume ───────────────────────────────────────────────
    snap.vol_ratio_6 = compute_volume_ratio(bars, idx)

    # ── Classify state ───────────────────────────────────────
    snap.state = classify_intraday_state(snap)

    # ── Current regime label (for comparison) ────────────────
    snap.current_regime = _regime_from_snapshot(snap)

    return snap


# ═══════════════════════════════════════════════════════════════════
#  State Classifier
# ═══════════════════════════════════════════════════════════════════

def classify_intraday_state(snap: StateSnapshot) -> IntradayState:
    """
    Classify the intraday state from computed features.

    Decision tree (evaluated in order):

    1. SQUEEZE:   Very low efficiency AND tight range AND low volume
    2. TREND_DOWN: Negative slope, high negative efficiency, close near lows
    3. WASHOUT:   Was trending down but momentum decelerating (capitulation)
    4. RECOVERY:  Positive short-term slope, positive acceleration, off lows
    5. TREND_UP:  Positive slope, high positive efficiency, close near highs
    6. SIDEWAYS:  Everything else (mid-range, low directional signal)
    """
    eff6 = snap.efficiency_6
    eff12 = snap.efficiency_12
    sl3 = snap.slope_3
    sl6 = snap.slope_6
    accel = snap.momentum_accel
    rp = snap.range_position  # 0=at lows, 1=at highs
    vr = snap.vol_ratio_6

    # ── SQUEEZE: tight, indecisive, often pre-breakout ───────
    # Very low efficiency on both windows + low volume
    if abs(eff6) < 0.15 and abs(eff12) < 0.15 and abs(sl6) < 0.010 and vr < 0.80:
        return IntradayState.SQUEEZE

    # ── TREND_DOWN: persistent selling ───────────────────────
    # Strong negative efficiency, negative slope, close in bottom quarter
    if eff6 < -0.40 and sl6 < -0.010 and rp < 0.30:
        return IntradayState.TREND_DOWN

    # ── WASHOUT: capitulation / exhaustion after selling ─────
    # Was trending down (eff12 negative) but short-term is decelerating
    # Slope still negative or flat, but acceleration is positive (selling slowing)
    # Close still near lows
    if eff12 < -0.25 and accel > 0.005 and rp < 0.35 and sl3 > sl6:
        return IntradayState.WASHOUT

    # ── RECOVERY: bouncing off lows ──────────────────────────
    # Positive short-term slope, positive acceleration, range_position rising
    # from lower half, longer efficiency still negative (we came from down)
    if sl3 > 0.010 and accel > 0.003 and rp > 0.35 and eff12 < 0.10:
        return IntradayState.RECOVERY

    # ── TREND_UP: persistent buying ──────────────────────────
    # Strong positive efficiency, positive slope, close in top quarter
    if eff6 > 0.40 and sl6 > 0.010 and rp > 0.70:
        return IntradayState.TREND_UP

    # ── SIDEWAYS: default / no clear signal ──────────────────
    return IntradayState.SIDEWAYS


def _regime_from_snapshot(snap: StateSnapshot) -> str:
    """Reproduce the current dashboard regime label from snapshot data."""
    pct = snap.pct_from_open
    if pct > 0.05:
        direction = "GREEN"
    elif pct < -0.05:
        direction = "RED"
    else:
        direction = "FLAT"

    rp = snap.range_position
    if direction == "RED":
        character = "TREND" if rp <= 0.25 else "CHOPPY"
    elif direction == "GREEN":
        character = "TREND" if rp >= 0.75 else "CHOPPY"
    else:
        character = "CHOPPY"
    return f"{direction}+{character}"


# ═══════════════════════════════════════════════════════════════════
#  Data Loading
# ═══════════════════════════════════════════════════════════════════

def _load_bars(symbol: str) -> Optional[List[Bar]]:
    path = DATA_DIR / f"{symbol}_5min.csv"
    if not path.exists():
        return None
    return load_bars_from_csv(str(path))


def _get_universe() -> List[str]:
    watchlist = Path(__file__).parent.parent / "watchlist.txt"
    if watchlist.exists():
        syms = []
        with open(watchlist) as f:
            for line in f:
                s = line.strip().upper()
                if s and not s.startswith("#") and s not in ("SPY", "QQQ"):
                    syms.append(s)
        return syms
    return []


def _group_bars_by_day(bars: List[Bar]) -> Dict[date, List[Tuple[int, Bar]]]:
    """Group bars by date, preserving indices into the original list."""
    by_day = defaultdict(list)
    for i, bar in enumerate(bars):
        by_day[bar.timestamp.date()].append((i, bar))
    return by_day


# ═══════════════════════════════════════════════════════════════════
#  Section 1: SPY State Timeline — classify every bar
# ═══════════════════════════════════════════════════════════════════

def report_spy_state_timeline(spy_bars: List[Bar]):
    """
    For each trading day, compute intraday state at every bar.
    Show how states evolve throughout the day.
    Report the distribution of states across all days.
    """
    print("\n" + "=" * 90)
    print("  SECTION 1: SPY INTRADAY STATE TIMELINE")
    print("=" * 90)

    days = _group_bars_by_day(spy_bars)
    all_states = []
    day_summaries = []

    for day_date in sorted(days.keys()):
        day_bars = days[day_date]
        if len(day_bars) < 6:
            continue

        # Track day open/high/low
        day_open = day_bars[0][1].open
        day_high = day_open
        day_low = day_open

        day_states = []
        for idx, bar in day_bars:
            day_high = max(day_high, bar.high)
            day_low = min(day_low, bar.low)
            snap = compute_state_snapshot(spy_bars, idx, day_open, day_high, day_low)
            day_states.append(snap)
            all_states.append(snap)

        # Day summary: count state occurrences
        state_counts = defaultdict(int)
        for s in day_states:
            state_counts[s.state.value] += 1

        # Final state and regime
        final = day_states[-1]
        dominant = max(state_counts, key=state_counts.get)

        # Show transitions
        transitions = []
        prev_state = None
        for s in day_states:
            if s.state != prev_state:
                hhmm = s.timestamp.strftime("%H:%M")
                transitions.append(f"{hhmm}={s.state.value}")
                prev_state = s.state
        trans_str = " → ".join(transitions[:8])
        if len(transitions) > 8:
            trans_str += f" ... (+{len(transitions)-8})"

        pfo = final.pct_from_open
        day_summaries.append({
            "date": day_date,
            "pct_from_open": pfo,
            "regime": final.current_regime,
            "dominant_state": dominant,
            "n_transitions": len(transitions),
            "states": state_counts,
            "transitions": trans_str,
        })

    # Print daily summaries
    print(f"\n  {len(day_summaries)} trading days analyzed\n")
    print(f"  {'Date':12s} {'PctOpen':>8s} {'Regime':14s} {'Dominant':14s} {'Trans':>5s}  State Flow")
    print(f"  {'-'*12} {'-'*8} {'-'*14} {'-'*14} {'-'*5}  {'-'*40}")

    for ds in day_summaries:
        print(f"  {ds['date']}  {ds['pct_from_open']:+7.2f}%  {ds['regime']:14s} "
              f"{ds['dominant_state']:14s} {ds['n_transitions']:5d}  {ds['transitions']}")

    # Overall state distribution
    print(f"\n  OVERALL STATE DISTRIBUTION (across all bars)")
    print(f"  {'-'*50}")
    state_totals = defaultdict(int)
    for s in all_states:
        state_totals[s.state.value] += 1
    total = len(all_states)
    for state in ["TREND_DOWN", "WASHOUT", "RECOVERY", "SQUEEZE", "SIDEWAYS", "TREND_UP"]:
        ct = state_totals.get(state, 0)
        pct = ct / total * 100 if total else 0
        bar = "█" * int(pct / 2)
        print(f"    {state:14s}  {ct:5d}  ({pct:5.1f}%)  {bar}")

    # State distribution by regime
    print(f"\n  STATE DISTRIBUTION BY CURRENT REGIME")
    print(f"  {'-'*70}")
    regime_state = defaultdict(lambda: defaultdict(int))
    for s in all_states:
        regime_state[s.current_regime][s.state.value] += 1

    for regime in sorted(regime_state.keys()):
        counts = regime_state[regime]
        total_r = sum(counts.values())
        parts = []
        for state in ["TREND_DOWN", "WASHOUT", "RECOVERY", "SQUEEZE", "SIDEWAYS", "TREND_UP"]:
            ct = counts.get(state, 0)
            if ct > 0:
                parts.append(f"{state}={ct}({ct/total_r*100:.0f}%)")
        print(f"    {regime:16s}  N={total_r:5d}  {', '.join(parts)}")

    return all_states, day_summaries


# ═══════════════════════════════════════════════════════════════════
#  Section 2: RED+CHOPPY Decomposition — what are these really?
# ═══════════════════════════════════════════════════════════════════

def report_red_choppy_decomposition(all_states: List[StateSnapshot]):
    """
    Focus on bars currently labeled RED+CHOPPY.
    Show the distribution of new intraday states within this bucket.
    This answers: "what did RED+CHOPPY actually contain?"
    """
    print("\n" + "=" * 90)
    print("  SECTION 2: RED+CHOPPY DECOMPOSITION — What's hiding inside?")
    print("=" * 90)

    red_choppy = [s for s in all_states if s.current_regime == "RED+CHOPPY"]
    if not red_choppy:
        print("  No RED+CHOPPY bars found.\n")
        return

    print(f"\n  Total RED+CHOPPY bars: {len(red_choppy)}\n")

    # State breakdown
    state_counts = defaultdict(int)
    for s in red_choppy:
        state_counts[s.state.value] += 1

    print(f"  {'State':14s}  {'Count':>6s}  {'Pct':>6s}  {'AvgSlope3':>10s}  {'AvgEff6':>9s}  {'AvgRP':>7s}")
    print(f"  {'-'*14}  {'-'*6}  {'-'*6}  {'-'*10}  {'-'*9}  {'-'*7}")

    for state in ["TREND_DOWN", "WASHOUT", "RECOVERY", "SQUEEZE", "SIDEWAYS", "TREND_UP"]:
        matches = [s for s in red_choppy if s.state.value == state]
        if not matches:
            continue
        ct = len(matches)
        pct = ct / len(red_choppy) * 100
        avg_sl3 = statistics.mean([s.slope_3 for s in matches])
        avg_eff6 = statistics.mean([s.efficiency_6 for s in matches])
        avg_rp = statistics.mean([s.range_position for s in matches])
        print(f"  {state:14s}  {ct:6d}  {pct:5.1f}%  {avg_sl3:+10.4f}  {avg_eff6:+9.3f}  {avg_rp:7.3f}")

    # Same for GREEN+CHOPPY
    green_choppy = [s for s in all_states if s.current_regime == "GREEN+CHOPPY"]
    if green_choppy:
        print(f"\n  GREEN+CHOPPY comparison: {len(green_choppy)} bars")
        gc_counts = defaultdict(int)
        for s in green_choppy:
            gc_counts[s.state.value] += 1
        parts = [f"{st}={ct}" for st, ct in sorted(gc_counts.items(), key=lambda x: -x[1])]
        print(f"    {', '.join(parts)}")


# ═══════════════════════════════════════════════════════════════════
#  Section 3: Trade Outcome by Intraday State
# ═══════════════════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    """Minimal trade info for state → outcome analysis."""
    symbol: str = ""
    timestamp: datetime = None
    direction: int = 1          # +1 long, -1 short
    r_result: float = 0.0
    setup_type: str = ""
    quality: int = 0
    regime: str = ""
    intraday_state: IntradayState = IntradayState.UNKNOWN
    # Features at signal time
    snap: StateSnapshot = None


def _find_signal_bar(spy_bars: List[Bar], signal_ts: datetime) -> Optional[int]:
    """Find the SPY bar index closest to a signal timestamp."""
    target = signal_ts
    best_idx = None
    best_diff = timedelta(minutes=10)  # Max 10 min tolerance

    for i, bar in enumerate(spy_bars):
        diff = abs(bar.timestamp - target)
        if diff < best_diff:
            best_diff = diff
            best_idx = i
    return best_idx


def _compute_spy_day_context(spy_bars: List[Bar]) -> Dict[date, Dict]:
    """Pre-compute day open/high/low for SPY by date, cumulative at each bar."""
    day_ctx = {}
    current_date = None
    day_open = 0.0
    day_high = -math.inf
    day_low = math.inf

    for i, bar in enumerate(spy_bars):
        bd = bar.timestamp.date()
        if bd != current_date:
            current_date = bd
            day_open = bar.open
            day_high = bar.high
            day_low = bar.low
        else:
            day_high = max(day_high, bar.high)
            day_low = min(day_low, bar.low)

        # Store cumulative context at each bar index
        day_ctx[i] = {"open": day_open, "high": day_high, "low": day_low}

    return day_ctx


def load_trades_with_state(spy_bars: List[Bar]) -> List[TradeRecord]:
    """
    Run backtest engine over all symbols, collect trades,
    compute SPY intraday state at each signal time.
    """
    cfg = OverlayConfig()
    cfg.use_market_context = True

    # Load market data
    qqq_bars = _load_bars("QQQ")
    if not qqq_bars:
        print("  QQQ data missing.")
        return []

    # Load sector ETFs
    sector_bars_dict = {}
    needed = set()
    for sym_file in DATA_DIR.glob("*_5min.csv"):
        sym = sym_file.stem.replace("_5min", "")
        sec = get_sector_etf(sym)
        if sec and sec not in ("SPY", "QQQ"):
            needed.add(sec)
    for sec_sym in needed:
        sec_bars = _load_bars(sec_sym)
        if sec_bars:
            sector_bars_dict[sec_sym] = sec_bars

    # Get universe
    universe = _get_universe()
    print(f"  Running backtest on {len(universe)} symbols...")

    # Pre-compute SPY day context
    spy_day_ctx = _compute_spy_day_context(spy_bars)

    # Classify SPY days for regime labeling
    spy_by_date = defaultdict(list)
    for bar in spy_bars:
        spy_by_date[bar.timestamp.date()].append(bar)
    spy_day_info = {}
    for d, day_bars in spy_by_date.items():
        if not day_bars:
            continue
        day_open = day_bars[0].open
        pfo = (day_bars[-1].close - day_open) / day_open * 100 if day_open else 0
        dh = max(b.high for b in day_bars)
        dl = min(b.low for b in day_bars)
        rng = dh - dl
        close_pos = (day_bars[-1].close - dl) / rng if rng > 0 else 0.5
        if pfo > 0.05:
            direction = "GREEN"
        elif pfo < -0.05:
            direction = "RED"
        else:
            direction = "FLAT"
        if direction == "RED":
            character = "TREND" if close_pos <= 0.25 else "CHOPPY"
        elif direction == "GREEN":
            character = "TREND" if close_pos >= 0.75 else "CHOPPY"
        else:
            character = "CHOPPY"
        spy_day_info[d] = f"{direction}+{character}"

    records = []
    for sym in universe:
        sym_bars = _load_bars(sym)
        if not sym_bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None

        result = run_backtest(
            sym_bars, cfg=cfg,
            spy_bars=spy_bars, qqq_bars=qqq_bars,
            sector_bars=sec_bars,
        )
        if not result.trades:
            continue

        for trade in result.trades:
            sig_ts = trade.signal.timestamp
            if sig_ts is None:
                continue

            # Determine regime for this trade's day
            trade_date = sig_ts.date()
            regime = spy_day_info.get(trade_date, "UNKNOWN")

            setup_name = SETUP_DISPLAY_NAME.get(trade.signal.setup_id, str(trade.signal.setup_id))

            tr = TradeRecord(
                symbol=sym,
                timestamp=sig_ts,
                direction=trade.signal.direction,
                r_result=trade.pnl_rr,
                setup_type=setup_name,
                quality=trade.signal.quality_score,
                regime=regime,
            )

            # Find SPY state at signal time
            spy_idx = _find_signal_bar(spy_bars, sig_ts)
            if spy_idx is not None and spy_idx in spy_day_ctx:
                ctx = spy_day_ctx[spy_idx]
                snap = compute_state_snapshot(
                    spy_bars, spy_idx,
                    ctx["open"], ctx["high"], ctx["low"],
                )
                tr.intraday_state = snap.state
                tr.snap = snap

            records.append(tr)

    print(f"  {len(records)} trades collected")
    return records


def report_trade_outcomes_by_state(spy_bars: List[Bar]):
    """
    Load all trades, classify SPY intraday state at signal time,
    report R-outcomes grouped by state.
    """
    print("\n" + "=" * 90)
    print("  SECTION 3: TRADE OUTCOMES BY INTRADAY STATE")
    print("=" * 90)

    records = load_trades_with_state(spy_bars)
    if not records:
        print("  No trades loaded.\n")
        return records

    # ── Overall state × outcome ──────────────────────────────
    print(f"\n  {len(records)} trades loaded\n")

    # Group by state
    by_state = defaultdict(list)
    for r in records:
        by_state[r.intraday_state.value].append(r)

    print(f"  {'State':14s}  {'N':>4s}  {'WR':>6s}  {'ExpR':>8s}  {'TotalR':>8s}  {'AvgRP':>7s}  {'AvgEff6':>8s}")
    print(f"  {'-'*14}  {'-'*4}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*8}")

    for state in ["TREND_DOWN", "WASHOUT", "RECOVERY", "SQUEEZE", "SIDEWAYS", "TREND_UP", "UNKNOWN"]:
        trades = by_state.get(state, [])
        if not trades:
            continue
        n = len(trades)
        wins = sum(1 for t in trades if t.r_result > 0)
        wr = wins / n * 100 if n else 0
        total_r = sum(t.r_result for t in trades)
        exp_r = total_r / n if n else 0
        avg_rp = statistics.mean([t.snap.range_position for t in trades if t.snap]) if trades else 0
        avg_eff = statistics.mean([t.snap.efficiency_6 for t in trades if t.snap]) if trades else 0
        print(f"  {state:14s}  {n:4d}  {wr:5.1f}%  {exp_r:+7.3f}R  {total_r:+7.2f}R  {avg_rp:7.3f}  {avg_eff:+8.3f}")

    # ── Longs only ───────────────────────────────────────────
    longs = [r for r in records if r.direction == 1]
    if longs:
        print(f"\n  LONG TRADES BY STATE (N={len(longs)})")
        print(f"  {'-'*70}")
        by_state_l = defaultdict(list)
        for r in longs:
            by_state_l[r.intraday_state.value].append(r)

        for state in ["TREND_DOWN", "WASHOUT", "RECOVERY", "SQUEEZE", "SIDEWAYS", "TREND_UP", "UNKNOWN"]:
            trades = by_state_l.get(state, [])
            if not trades:
                continue
            n = len(trades)
            wins = sum(1 for t in trades if t.r_result > 0)
            wr = wins / n * 100
            total_r = sum(t.r_result for t in trades)
            exp_r = total_r / n
            print(f"    {state:14s}  N={n:3d}  WR={wr:5.1f}%  Exp={exp_r:+.3f}R  Total={total_r:+.2f}R")

    # ── Shorts only ──────────────────────────────────────────
    shorts = [r for r in records if r.direction == -1]
    if shorts:
        print(f"\n  SHORT TRADES BY STATE (N={len(shorts)})")
        print(f"  {'-'*70}")
        by_state_s = defaultdict(list)
        for r in shorts:
            by_state_s[r.intraday_state.value].append(r)

        for state in ["TREND_DOWN", "WASHOUT", "RECOVERY", "SQUEEZE", "SIDEWAYS", "TREND_UP", "UNKNOWN"]:
            trades = by_state_s.get(state, [])
            if not trades:
                continue
            n = len(trades)
            wins = sum(1 for t in trades if t.r_result > 0)
            wr = wins / n * 100
            total_r = sum(t.r_result for t in trades)
            exp_r = total_r / n
            print(f"    {state:14s}  N={n:3d}  WR={wr:5.1f}%  Exp={exp_r:+.3f}R  Total={total_r:+.2f}R")

    return records


# ═══════════════════════════════════════════════════════════════════
#  Section 4: Regime Mislabeling Analysis
# ═══════════════════════════════════════════════════════════════════

def report_mislabeling_analysis(records: List[TradeRecord]):
    """
    Identify trades where the old regime label hid a meaningful state.
    Focus: RED+CHOPPY trades that were actually RECOVERY or SQUEEZE.
    """
    print("\n" + "=" * 90)
    print("  SECTION 4: REGIME MISLABELING — Did coarse labels hide good/bad states?")
    print("=" * 90)

    if not records:
        print("  No trades to analyze.\n")
        return

    # Cross-tab: old regime × new state
    regime_state = defaultdict(lambda: defaultdict(list))
    for r in records:
        regime_state[r.regime][r.intraday_state.value].append(r)

    for regime in sorted(regime_state.keys()):
        states = regime_state[regime]
        total_n = sum(len(v) for v in states.values())
        total_r = sum(t.r_result for v in states.values() for t in v)

        print(f"\n  {regime} (N={total_n}, TotalR={total_r:+.2f})")
        print(f"  {'State':14s}  {'N':>4s}  {'WR':>6s}  {'ExpR':>8s}  {'TotalR':>8s}  Assessment")
        print(f"  {'-'*14}  {'-'*4}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*25}")

        for state in ["TREND_DOWN", "WASHOUT", "RECOVERY", "SQUEEZE", "SIDEWAYS", "TREND_UP", "UNKNOWN"]:
            trades = states.get(state, [])
            if not trades:
                continue
            n = len(trades)
            wins = sum(1 for t in trades if t.r_result > 0)
            wr = wins / n * 100
            total = sum(t.r_result for t in trades)
            exp = total / n

            # Assessment
            if exp > 0.3:
                assessment = "✓ TRADABLE"
            elif exp > 0:
                assessment = "~ marginal"
            elif exp > -0.3:
                assessment = "⚠ weak"
            else:
                assessment = "✗ AVOID"

            print(f"  {state:14s}  {n:4d}  {wr:5.1f}%  {exp:+7.3f}R  {total:+7.2f}R  {assessment}")


# ═══════════════════════════════════════════════════════════════════
#  Section 5: State Transition Detection
# ═══════════════════════════════════════════════════════════════════

def report_state_transitions(spy_bars: List[Bar], day_summaries: List[Dict]):
    """
    Analyze state transitions within days.
    Identify patterns like: TREND_DOWN → WASHOUT → RECOVERY
    Show which transition sequences are common.
    """
    print("\n" + "=" * 90)
    print("  SECTION 5: STATE TRANSITION PATTERNS")
    print("=" * 90)

    days = _group_bars_by_day(spy_bars)
    transition_seqs = []

    for day_date in sorted(days.keys()):
        day_bars = days[day_date]
        if len(day_bars) < 12:  # Need at least 1 hour
            continue

        day_open = day_bars[0][1].open
        day_high = day_open
        day_low = day_open

        states_seq = []
        for idx, bar in day_bars:
            day_high = max(day_high, bar.high)
            day_low = min(day_low, bar.low)
            snap = compute_state_snapshot(spy_bars, idx, day_open, day_high, day_low)
            states_seq.append(snap.state)

        # Extract unique transition sequence (collapse consecutive same states)
        transitions = [states_seq[0]]
        for s in states_seq[1:]:
            if s != transitions[-1]:
                transitions.append(s)

        trans_key = " → ".join(s.value for s in transitions[:6])
        transition_seqs.append({
            "date": day_date,
            "sequence": trans_key,
            "n_transitions": len(transitions),
            "pct_from_open": day_bars[-1][1].close / day_open * 100 - 100 if day_open else 0,
        })

    # Count sequence patterns
    seq_counts = defaultdict(list)
    for ts in transition_seqs:
        seq_counts[ts["sequence"]].append(ts)

    print(f"\n  {len(transition_seqs)} trading days analyzed\n")
    print(f"  Most common transition patterns:\n")

    for seq, days_list in sorted(seq_counts.items(), key=lambda x: -len(x[1]))[:15]:
        n = len(days_list)
        avg_pfo = statistics.mean([d["pct_from_open"] for d in days_list])
        dates = ", ".join(str(d["date"]) for d in days_list[:3])
        if n > 3:
            dates += f" +{n-3} more"
        print(f"    [{n:2d}x]  {seq}")
        print(f"           Avg PFO: {avg_pfo:+.2f}%   Dates: {dates}\n")


# ═══════════════════════════════════════════════════════════════════
#  Section 6: Feature Distribution at Signal Times
# ═══════════════════════════════════════════════════════════════════

def report_feature_distributions(records: List[TradeRecord]):
    """
    Show distributions of all state features at trade signal times.
    This validates the classifier thresholds are reasonable.
    """
    print("\n" + "=" * 90)
    print("  SECTION 6: FEATURE DISTRIBUTIONS AT SIGNAL TIMES")
    print("=" * 90)

    valid = [r for r in records if r.snap is not None]
    if not valid:
        print("  No valid records.\n")
        return

    features = [
        ("efficiency_6", [r.snap.efficiency_6 for r in valid]),
        ("efficiency_12", [r.snap.efficiency_12 for r in valid]),
        ("slope_3", [r.snap.slope_3 for r in valid]),
        ("slope_6", [r.snap.slope_6 for r in valid]),
        ("momentum_accel", [r.snap.momentum_accel for r in valid]),
        ("range_position", [r.snap.range_position for r in valid]),
        ("rebound_from_low", [r.snap.rebound_from_low for r in valid]),
        ("fade_from_high", [r.snap.fade_from_high for r in valid]),
        ("vol_ratio_6", [r.snap.vol_ratio_6 for r in valid]),
        ("pct_from_open", [r.snap.pct_from_open for r in valid]),
    ]

    print(f"\n  {len(valid)} trades with valid snapshots\n")
    print(f"  {'Feature':20s}  {'Min':>9s}  {'P25':>9s}  {'Median':>9s}  {'P75':>9s}  {'Max':>9s}  {'StdDev':>9s}")
    print(f"  {'-'*20}  {'-'*9}  {'-'*9}  {'-'*9}  {'-'*9}  {'-'*9}  {'-'*9}")

    for name, vals in features:
        if not vals:
            continue
        vals_s = sorted(vals)
        n = len(vals_s)
        p25 = vals_s[n // 4]
        p50 = vals_s[n // 2]
        p75 = vals_s[3 * n // 4]
        sd = statistics.stdev(vals) if len(vals) > 1 else 0
        print(f"  {name:20s}  {min(vals):+9.4f}  {p25:+9.4f}  {p50:+9.4f}  "
              f"{p75:+9.4f}  {max(vals):+9.4f}  {sd:9.4f}")

    # Winners vs losers comparison
    print(f"\n  WINNERS vs LOSERS feature comparison")
    print(f"  {'-'*70}")
    winners = [r for r in valid if r.r_result > 0]
    losers = [r for r in valid if r.r_result <= 0]

    print(f"  Winners: {len(winners)}, Losers: {len(losers)}\n")

    for name, _ in features:
        w_vals = [getattr(r.snap, name) for r in winners]
        l_vals = [getattr(r.snap, name) for r in losers]
        if not w_vals or not l_vals:
            continue
        w_mean = statistics.mean(w_vals)
        l_mean = statistics.mean(l_vals)
        sep = w_mean - l_mean
        print(f"    {name:20s}  Win={w_mean:+8.4f}  Lose={l_mean:+8.4f}  Sep={sep:+8.4f}")


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════

def main():
    print("\n" + "=" * 90)
    print("  INTRADAY STATE DETECTION STUDY — Research Framework")
    print("  Classifies 6 intraday states inside the broad regime envelope")
    print("=" * 90)

    # Load SPY bars
    spy_bars = _load_bars("SPY")
    if not spy_bars:
        print("ERROR: Could not load SPY data.")
        return

    print(f"\n  SPY bars: {len(spy_bars)}")
    print(f"  Date range: {spy_bars[0].timestamp.date()} → {spy_bars[-1].timestamp.date()}")

    # Section 1: SPY state timeline
    all_states, day_summaries = report_spy_state_timeline(spy_bars)

    # Section 2: RED+CHOPPY decomposition
    report_red_choppy_decomposition(all_states)

    # Section 3: Trade outcomes by state
    try:
        records = report_trade_outcomes_by_state(spy_bars)
    except Exception as e:
        print(f"\n  Section 3 error (trade loading): {e}")
        print("  Skipping trade-dependent sections.\n")
        records = []

    # Section 4: Mislabeling analysis
    if records:
        report_mislabeling_analysis(records)

    # Section 5: State transitions
    report_state_transitions(spy_bars, day_summaries)

    # Section 6: Feature distributions
    if records:
        report_feature_distributions(records)

    print("\n" + "=" * 90)
    print("  STUDY COMPLETE")
    print("=" * 90)
    print()


if __name__ == "__main__":
    main()
