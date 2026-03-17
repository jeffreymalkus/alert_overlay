"""
1-Minute Execution-Adjusted Backtest — EMA_RECLAIM focus.

This module replays 5-minute signals against actual 1-minute bar data to test
realistic execution assumptions. The key differences from the intrabar_stress
module (which uses synthetic assumptions on 5-min bars):

1. **Real 1-minute bar path**: Actual recorded OHLCV at 1-min resolution.
   No need to guess whether low or high came first — we see the sequence.

2. **Delayed entry**: Signal fires at 5-min bar close. We enter 1-2 minutes
   later at the actual 1-min bar close (simulating reaction + order placement).

3. **Intrabar stop**: The stop is checked against every 1-min bar's extreme,
   so a quick wick that recovers within the 5-min bar WILL stop us out.

4. **TOD-adjusted slippage**: Wider slippage near open/close based on actual
   1-min bar spread (high-low) as a proxy for real-time spread.

5. **Exit resolution**: Targets/stops/EMA trails are evaluated at 1-min
   granularity, so we get much more accurate fill prices.

The pipeline:
  1. Run 5-min backtest → collect EMA_RECLAIM signals with entry/stop/target
  2. For each signal, locate the corresponding 1-min bars
  3. Replay the trade on 1-min bars with realistic execution
  4. Compare 5-min result vs 1-min result trade-by-trade

Usage:
    python -m alert_overlay.onemin_backtest
    python -m alert_overlay.onemin_backtest --delay 2  # 2-minute entry delay
    python -m alert_overlay.onemin_backtest --verbose   # per-trade detail
"""

import math
import os
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import Bar, SetupId, SetupFamily, SETUP_FAMILY_MAP, NaN
from ..market_context import get_sector_etf, SECTOR_MAP

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_1MIN_DIR = DATA_DIR / "1min"


# ── Data structures ──

@dataclass
class OneMinTradeResult:
    """Result of replaying a single trade on 1-minute data."""
    sym: str
    signal_time: datetime
    direction: int
    entry_5min: float       # original 5-min backtest entry
    entry_1min: float       # actual 1-min entry (after delay + slippage)
    stop: float             # stop price
    target: float           # target price
    risk: float             # |entry - stop|

    # 5-min result (from original backtest)
    pnl_5min: float = 0.0
    exit_reason_5min: str = ""
    bars_held_5min: int = 0

    # 1-min result
    pnl_1min: float = 0.0
    exit_reason_1min: str = ""
    exit_price_1min: float = 0.0
    bars_held_1min: int = 0  # 1-min bars held
    entry_delay_bars: int = 0  # how many 1-min bars delayed

    # Execution details
    entry_slippage: float = 0.0  # 1min_entry - 5min_entry (adverse direction)
    tod_mult: float = 1.0
    mae_1min: float = 0.0  # max adverse excursion on 1-min path
    mfe_1min: float = 0.0  # max favorable excursion on 1-min path

    @property
    def pnl_diff(self) -> float:
        return self.pnl_1min - self.pnl_5min

    @property
    def survived(self) -> bool:
        """Did the trade survive (positive PnL) on 1-min?"""
        return self.pnl_1min > 0

    @property
    def flipped(self) -> bool:
        """Was this a win on 5-min but a loss on 1-min?"""
        return self.pnl_5min > 0 and self.pnl_1min <= 0


# ── Utility functions ──

def _tod_slippage_mult(hhmm: int) -> float:
    """Time-of-day slippage multiplier based on typical spread patterns."""
    if hhmm < 940:
        return 1.8   # pre-market / first 10 min
    elif hhmm < 1000:
        return 1.4   # first 30 min
    elif hhmm < 1030:
        return 1.15
    elif hhmm < 1300:
        return 1.0   # midday
    elif hhmm < 1400:
        return 0.9   # lunch lull
    elif hhmm < 1530:
        return 1.0
    else:
        return 1.3   # near close


def _compute_1min_slippage(bar_1min: Bar, direction: int, base_bps: float = 0.0004) -> float:
    """
    Compute realistic slippage from a 1-min bar.

    Uses the 1-min bar's range as a proxy for the real-time spread.
    Slippage = max(base_bps * price, frac * bar_range) * tod_mult
    """
    price = bar_1min.close if bar_1min.close > 0 else 1.0
    bar_range = bar_1min.high - bar_1min.low
    hhmm = bar_1min.timestamp.hour * 100 + bar_1min.timestamp.minute
    tod = _tod_slippage_mult(hhmm)

    # Base: BPS of price
    slip_bps = price * base_bps
    # Range-based: fraction of the 1-min bar range (spread proxy)
    slip_range = bar_range * 0.15  # assume you lose ~15% of the 1-min bar range

    return max(slip_bps, slip_range) * tod


def _make_ema_iso_cfg() -> OverlayConfig:
    """EMA_RECLAIM isolated config."""
    cfg = OverlayConfig()
    cfg.show_reversal_setups = False
    cfg.show_trend_setups = False
    cfg.show_ema_retest = False
    cfg.show_ema_mean_rev = False
    cfg.show_ema_pullback = False
    cfg.show_second_chance = False
    cfg.show_spencer = False
    cfg.show_failed_bounce = False
    cfg.show_ema_scalp = True
    cfg.show_ema_confirm = False
    cfg.use_dynamic_slippage = True
    return cfg


def _load_market_data():
    """Load SPY, QQQ, sector ETF bars for market context."""
    spy = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv")) if (DATA_DIR / "SPY_5min.csv").exists() else None
    qqq = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv")) if (DATA_DIR / "QQQ_5min.csv").exists() else None
    sectors = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sectors[etf] = load_bars_from_csv(str(p))
    return spy, qqq, sectors


def _load_1min_bars(sym: str) -> List[Bar]:
    """Load 1-minute bars for a symbol from data/1min/."""
    csv_path = DATA_1MIN_DIR / f"{sym}_1min.csv"
    if not csv_path.exists():
        return []
    return load_bars_from_csv(str(csv_path))


def _find_1min_window(
    bars_1min: List[Bar],
    signal_time: datetime,
    exit_time: Optional[datetime],
    pre_bars: int = 5,
    post_bars: int = 200,
) -> List[Bar]:
    """
    Extract the 1-min bars corresponding to a 5-min trade window.

    Returns bars from (signal_time - pre_bars*1min) to
    (exit_time + post_bars*1min) to give context.
    """
    if not bars_1min:
        return []

    # Find the signal bar's approximate position
    start_search = signal_time - timedelta(minutes=pre_bars)
    end_search = (exit_time or signal_time) + timedelta(minutes=post_bars)

    window = [b for b in bars_1min if start_search <= b.timestamp <= end_search]
    return window


# ── Core replay engine ──

def replay_trade_on_1min(
    trade: Trade,
    bars_1min: List[Bar],
    entry_delay_minutes: int = 1,
    use_ema9_trail: bool = True,
    ema9_trail_after_bars: int = 10,  # 10 × 1min = ~2 five-min bars
) -> Optional[OneMinTradeResult]:
    """
    Replay a single 5-min trade on 1-minute data with realistic execution.

    Process:
    1. Signal fires at signal_time (5-min bar close)
    2. Wait entry_delay_minutes 1-min bars before entering
    3. Enter at the delayed 1-min bar's close + slippage
    4. Check stop/target on every subsequent 1-min bar
    5. For EMA_SCALP: also trail with 9EMA on 1-min bars

    Returns None if 1-min data doesn't cover the trade window.
    """
    sig = trade.signal
    signal_time = sig.timestamp
    direction = sig.direction
    stop_price = sig.stop_price
    target_price = sig.target_price
    risk = sig.risk

    # Get the trade's exit parameters
    family = SETUP_FAMILY_MAP.get(sig.setup_id, SetupFamily.EMA_SCALP)
    is_ema_scalp = family == SetupFamily.EMA_SCALP

    # Find 1-min bars starting from the signal time
    trade_bars = [b for b in bars_1min if b.timestamp >= signal_time]
    if len(trade_bars) < entry_delay_minutes + 2:
        return None  # not enough 1-min bars

    # ── Step 1: Delayed entry ──
    # Skip entry_delay_minutes bars, then enter at that bar's close
    if entry_delay_minutes >= len(trade_bars):
        return None

    entry_bar = trade_bars[entry_delay_minutes]
    entry_hhmm = entry_bar.timestamp.hour * 100 + entry_bar.timestamp.minute

    # Don't enter after 15:50 — session about to end
    if entry_hhmm >= 1550:
        return None

    # Compute entry with slippage
    slip = _compute_1min_slippage(entry_bar, direction)
    tod_mult = _tod_slippage_mult(entry_hhmm)

    # Entry is at 1-min bar close + slippage in adverse direction
    entry_1min = entry_bar.close + (slip * direction)

    # Adjust stop for the worse entry if needed
    # (stop stays at original level — the worse entry just means more risk)
    actual_risk = abs(entry_1min - stop_price)

    # ── Step 2: Walk 1-min bars, check stop/target/trail ──
    remaining_bars = trade_bars[entry_delay_minutes + 1:]

    # Simple EMA9 for trailing (on 1-min bars)
    ema9_val = entry_bar.close  # initialize
    ema9_mult = 2.0 / (9 + 1)

    mae = 0.0
    mfe = 0.0
    exit_price = 0.0
    exit_reason = ""
    bars_held = 0
    session_end_hhmm = 1555

    for b in remaining_bars:
        bars_held += 1
        b_hhmm = b.timestamp.hour * 100 + b.timestamp.minute

        # Update EMA9
        ema9_val = b.close * ema9_mult + ema9_val * (1 - ema9_mult)

        # Track MAE/MFE
        if direction == 1:
            adverse = entry_1min - b.low
            favorable = b.high - entry_1min
        else:
            adverse = b.high - entry_1min
            favorable = entry_1min - b.low

        mae = max(mae, adverse)
        mfe = max(mfe, favorable)

        # ── Check exits in priority order ──

        # 1. End of day
        if b_hhmm >= session_end_hhmm:
            exit_price = b.close
            exit_reason = "eod"
            break

        # 2. Stop hit (checked on bar extreme — the key advantage of 1-min)
        if direction == 1:
            if b.low <= stop_price:
                exit_price = stop_price
                exit_reason = "stop"
                break
        else:
            if b.high >= stop_price:
                exit_price = stop_price
                exit_reason = "stop"
                break

        # 3. For non-trail setups: target hit
        if not is_ema_scalp:
            if direction == 1:
                if b.high >= target_price:
                    exit_price = target_price
                    exit_reason = "target"
                    break
            else:
                if b.low <= target_price:
                    exit_price = target_price
                    exit_reason = "target"
                    break

        # 4. EMA9 trail (for EMA_SCALP, after minimum bars)
        if is_ema_scalp and use_ema9_trail and bars_held >= ema9_trail_after_bars:
            if direction == 1 and b.close < ema9_val:
                exit_price = b.close
                exit_reason = "ema9trail"
                break
            elif direction == -1 and b.close > ema9_val:
                exit_price = b.close
                exit_reason = "ema9trail"
                break

    else:
        # Ran out of bars — exit at last bar close
        if remaining_bars:
            exit_price = remaining_bars[-1].close
            exit_reason = "data_end"
        else:
            return None

    # Compute exit slippage
    exit_bar = remaining_bars[min(bars_held - 1, len(remaining_bars) - 1)] if remaining_bars else entry_bar
    exit_slip = _compute_1min_slippage(exit_bar, -direction)  # exit slip is opposite direction
    adjusted_exit = exit_price - (exit_slip * direction)

    # PnL
    pnl_1min = (adjusted_exit - entry_1min) * direction

    entry_slippage = abs(entry_1min - sig.entry_price)

    return OneMinTradeResult(
        sym="",  # filled by caller
        signal_time=signal_time,
        direction=direction,
        entry_5min=sig.entry_price,
        entry_1min=entry_1min,
        stop=stop_price,
        target=target_price,
        risk=risk,
        pnl_5min=trade.pnl_points,
        exit_reason_5min=trade.exit_reason,
        bars_held_5min=trade.bars_held,
        pnl_1min=pnl_1min,
        exit_reason_1min=exit_reason,
        exit_price_1min=exit_price,
        bars_held_1min=bars_held,
        entry_delay_bars=entry_delay_minutes,
        entry_slippage=entry_slippage,
        tod_mult=tod_mult,
        mae_1min=mae,
        mfe_1min=mfe,
    )


# ── Batch runner ──

def run_1min_comparison(
    entry_delay_minutes: int = 1,
    symbols: Optional[List[str]] = None,
) -> List[OneMinTradeResult]:
    """
    Run full comparison: 5-min signals replayed on 1-min data.

    1. Run 5-min backtest (EMA_RECLAIM isolated) → collect trades
    2. For each trade, load 1-min data for that symbol
    3. Replay on 1-min bars
    4. Return paired results
    """
    spy, qqq, sectors = _load_market_data()
    cfg = _make_ema_iso_cfg()

    results = []
    symbols_checked = 0
    symbols_with_1min = 0

    for f in sorted(os.listdir(DATA_DIR)):
        if not f.endswith('_5min.csv') or f in ('SPY_5min.csv', 'QQQ_5min.csv'):
            continue
        sym = f.replace('_5min.csv', '')

        if symbols and sym not in symbols:
            continue

        symbols_checked += 1

        # Load 1-min data
        bars_1min = _load_1min_bars(sym)
        if not bars_1min:
            continue
        symbols_with_1min += 1

        # Run 5-min backtest
        bars_5min = load_bars_from_csv(str(DATA_DIR / f))
        if not bars_5min:
            continue

        kw = {"cfg": cfg}
        if spy:
            kw["spy_bars"] = spy
            kw["qqq_bars"] = qqq
            sec_etf = get_sector_etf(sym)
            if sec_etf and sectors and sec_etf in sectors:
                kw["sector_bars"] = sectors[sec_etf]

        bt_result = run_backtest(bars_5min, **kw)

        # Replay each EMA_RECLAIM trade on 1-min data
        for trade in bt_result.trades:
            if trade.signal.setup_id != SetupId.EMA_RECLAIM:
                continue

            result = replay_trade_on_1min(
                trade, bars_1min,
                entry_delay_minutes=entry_delay_minutes,
            )
            if result:
                result.sym = sym
                results.append(result)

    return results


# ── Reporting ──

def _stats_line(label: str, results: List[OneMinTradeResult], use_1min: bool = True) -> str:
    """Format a single stats line for a group of results."""
    n = len(results)
    if n == 0:
        return f"  {label:<30} {'(no trades)':>40}"

    if use_1min:
        pnls = [r.pnl_1min for r in results]
    else:
        pnls = [r.pnl_5min for r in results]

    wins = sum(1 for p in pnls if p > 0)
    wr = wins / n * 100
    total = sum(pnls)
    exp = total / n
    gw = sum(p for p in pnls if p > 0)
    gl = abs(sum(p for p in pnls if p <= 0))
    pf = gw / gl if gl > 0 else float("inf")
    pf_s = f"{pf:.2f}" if pf < 999 else "inf"

    return (f"  {label:<30} N={n:>3}  WR={wr:>5.1f}%  PF={pf_s:>5}  "
            f"PnL={total:>+8.2f}  Exp={exp:>+6.3f}")


def print_comparison_report(results: List[OneMinTradeResult], entry_delay: int = 1):
    """Print full comparison report: 5-min vs 1-min results."""
    n = len(results)
    if n == 0:
        print("\n  No trades with 1-min data coverage found.")
        print("  Run download_1min.py first to collect 1-minute data.\n")
        return

    print(f"\n{'='*95}")
    print(f"  1-MINUTE EXECUTION-ADJUSTED BACKTEST — EMA_RECLAIM")
    print(f"  Entry delay: {entry_delay} minute(s)")
    print(f"{'='*95}")

    # ── Section 1: Side-by-side aggregate comparison ──
    print(f"\n  {'─'*85}")
    print(f"  AGGREGATE COMPARISON")
    print(f"  {'─'*85}")
    print(_stats_line("5-min backtest (original)", results, use_1min=False))
    print(_stats_line("1-min replay (realistic)", results, use_1min=True))

    # Compute deltas
    total_5 = sum(r.pnl_5min for r in results)
    total_1 = sum(r.pnl_1min for r in results)
    delta = total_1 - total_5
    wins_5 = sum(1 for r in results if r.pnl_5min > 0)
    wins_1 = sum(1 for r in results if r.pnl_1min > 0)
    flipped = sum(1 for r in results if r.flipped)
    rescued = sum(1 for r in results if r.pnl_5min <= 0 and r.pnl_1min > 0)

    print(f"\n  Delta:")
    print(f"    PnL change:        {delta:>+8.2f} ({delta/abs(total_5)*100 if total_5 != 0 else 0:>+.1f}%)")
    print(f"    Win count:         {wins_5} → {wins_1} (Δ{wins_1 - wins_5:+d})")
    print(f"    Wins flipped:      {flipped} (5-min win → 1-min loss)")
    print(f"    Losses rescued:    {rescued} (5-min loss → 1-min win)")

    # ── Section 2: Exit reason comparison ──
    print(f"\n  {'─'*85}")
    print(f"  EXIT REASON COMPARISON")
    print(f"  {'─'*85}")

    exit_5 = defaultdict(int)
    exit_1 = defaultdict(int)
    for r in results:
        exit_5[r.exit_reason_5min] += 1
        exit_1[r.exit_reason_1min] += 1

    all_reasons = sorted(set(exit_5.keys()) | set(exit_1.keys()))
    print(f"    {'Reason':<15} {'5-min':>6} {'1-min':>6} {'Δ':>6}")
    for reason in all_reasons:
        c5 = exit_5.get(reason, 0)
        c1 = exit_1.get(reason, 0)
        print(f"    {reason:<15} {c5:>6} {c1:>6} {c1-c5:>+6}")

    # ── Section 3: Execution quality ──
    print(f"\n  {'─'*85}")
    print(f"  EXECUTION QUALITY (1-min)")
    print(f"  {'─'*85}")

    slippages = [r.entry_slippage for r in results]
    mae_vals = [r.mae_1min for r in results if r.risk > 0]
    mfe_vals = [r.mfe_1min for r in results if r.risk > 0]
    mae_r = [r.mae_1min / r.risk for r in results if r.risk > 0]
    mfe_r = [r.mfe_1min / r.risk for r in results if r.risk > 0]

    print(f"    Entry slippage vs 5-min:")
    print(f"      Mean:    ${statistics.mean(slippages):.4f}")
    print(f"      Median:  ${statistics.median(slippages):.4f}")
    if len(slippages) > 1:
        print(f"      Max:     ${max(slippages):.4f}")

    if mae_r:
        print(f"    MAE (1-min path, in R):")
        print(f"      Mean:    {statistics.mean(mae_r):.2f}R")
        print(f"      Median:  {statistics.median(mae_r):.2f}R")
        print(f"      P90:     {sorted(mae_r)[int(len(mae_r)*0.9)]:.2f}R")

    if mfe_r:
        print(f"    MFE (1-min path, in R):")
        print(f"      Mean:    {statistics.mean(mfe_r):.2f}R")
        print(f"      Median:  {statistics.median(mfe_r):.2f}R")

    # ── Section 4: TOD breakdown ──
    print(f"\n  {'─'*85}")
    print(f"  TIME-OF-DAY BREAKDOWN")
    print(f"  {'─'*85}")

    tod_buckets = {
        "09:30-10:00": (930, 1000),
        "10:00-11:00": (1000, 1100),
        "11:00-13:00": (1100, 1300),
        "13:00-15:00": (1300, 1500),
        "15:00-16:00": (1500, 1600),
    }

    print(f"    {'Period':<15} {'N':>4} {'5m WR':>7} {'1m WR':>7} {'5m PnL':>8} {'1m PnL':>8} {'Δ PnL':>8}")
    for label, (start, end) in tod_buckets.items():
        bucket = [r for r in results
                  if start <= (r.signal_time.hour * 100 + r.signal_time.minute) < end]
        if not bucket:
            continue
        bn = len(bucket)
        wr5 = sum(1 for r in bucket if r.pnl_5min > 0) / bn * 100
        wr1 = sum(1 for r in bucket if r.pnl_1min > 0) / bn * 100
        pnl5 = sum(r.pnl_5min for r in bucket)
        pnl1 = sum(r.pnl_1min for r in bucket)
        print(f"    {label:<15} {bn:>4} {wr5:>6.1f}% {wr1:>6.1f}% {pnl5:>+7.2f} {pnl1:>+7.2f} {pnl1-pnl5:>+7.2f}")

    # ── Section 5: Per-symbol breakdown ──
    print(f"\n  {'─'*85}")
    print(f"  PER-SYMBOL COMPARISON")
    print(f"  {'─'*85}")

    sym_results = defaultdict(list)
    for r in results:
        sym_results[r.sym].append(r)

    print(f"    {'Sym':<6} {'N':>3} {'5m PnL':>8} {'1m PnL':>8} {'Δ':>8} {'5m WR':>7} {'1m WR':>7} {'Flipped':>8}")
    for sym in sorted(sym_results.keys()):
        sr = sym_results[sym]
        sn = len(sr)
        pnl5 = sum(r.pnl_5min for r in sr)
        pnl1 = sum(r.pnl_1min for r in sr)
        wr5 = sum(1 for r in sr if r.pnl_5min > 0) / sn * 100
        wr1 = sum(1 for r in sr if r.pnl_1min > 0) / sn * 100
        flip = sum(1 for r in sr if r.flipped)
        print(f"    {sym:<6} {sn:>3} {pnl5:>+7.2f} {pnl1:>+7.2f} {pnl1-pnl5:>+7.2f} {wr5:>6.1f}% {wr1:>6.1f}% {flip:>5}/{sn}")

    # ── Section 6: Worst flipped trades ──
    flipped_trades = [r for r in results if r.flipped]
    if flipped_trades:
        print(f"\n  {'─'*85}")
        print(f"  WORST FLIPPED TRADES (5-min win → 1-min loss)")
        print(f"  {'─'*85}")
        flipped_trades.sort(key=lambda r: r.pnl_diff)
        for r in flipped_trades[:10]:
            dir_s = "LONG" if r.direction == 1 else "SHRT"
            print(f"    {r.sym:<6} {str(r.signal_time)[:19]} {dir_s} "
                  f"5m:{r.pnl_5min:>+6.2f} → 1m:{r.pnl_1min:>+6.2f} "
                  f"(Δ{r.pnl_diff:>+6.2f})  "
                  f"5mExit:{r.exit_reason_5min:<9} 1mExit:{r.exit_reason_1min:<9} "
                  f"slip:{r.entry_slippage:.3f}")

    # ── Section 7: Delay sensitivity ──
    # (This is computed separately — see run_delay_sensitivity below)

    # ── Summary verdict ──
    print(f"\n{'='*95}")
    if total_1 > 0 and total_5 > 0:
        pct_retained = total_1 / total_5 * 100 if total_5 > 0 else 0
        print(f"  VERDICT: 1-min execution retains {pct_retained:.0f}% of 5-min PnL")
        if pct_retained >= 70:
            print(f"  ✓ EMA_RECLAIM is VIABLE under realistic execution ({entry_delay}min delay)")
        elif pct_retained >= 40:
            print(f"  ⚠ EMA_RECLAIM shows MODERATE degradation — entry timing matters")
        else:
            print(f"  ✗ EMA_RECLAIM shows SEVERE degradation — edge may not survive execution")
    elif total_5 > 0 and total_1 <= 0:
        print(f"  VERDICT: 5-min edge DOES NOT SURVIVE 1-min execution")
        print(f"  ✗ EMA_RECLAIM is NOT VIABLE — the 5-min backtest overstates the edge")
    elif total_5 <= 0:
        print(f"  VERDICT: EMA_RECLAIM is already negative on 5-min — 1-min confirms")
    else:
        print(f"  VERDICT: EMA_RECLAIM loses on 5-min but gains on 1-min — unusual, inspect manually")
    print(f"{'='*95}\n")


def run_delay_sensitivity(delays: List[int] = None) -> List[dict]:
    """
    Run 1-min comparison at multiple entry delay values to see sensitivity.

    Default delays: 0 (immediate), 1, 2, 3, 5 minutes.
    """
    if delays is None:
        delays = [0, 1, 2, 3, 5]

    rows = []
    for delay in delays:
        results = run_1min_comparison(entry_delay_minutes=delay)
        if not results:
            continue

        n = len(results)
        pnl_5 = sum(r.pnl_5min for r in results)
        pnl_1 = sum(r.pnl_1min for r in results)
        wins_1 = sum(1 for r in results if r.pnl_1min > 0)
        flipped = sum(1 for r in results if r.flipped)
        gw = sum(r.pnl_1min for r in results if r.pnl_1min > 0)
        gl = abs(sum(r.pnl_1min for r in results if r.pnl_1min <= 0))
        pf = gw / gl if gl > 0 else float("inf")

        rows.append({
            "delay": delay,
            "n": n,
            "pnl_5min": pnl_5,
            "pnl_1min": pnl_1,
            "retained_pct": pnl_1 / pnl_5 * 100 if pnl_5 != 0 else 0,
            "wr": wins_1 / n * 100,
            "pf": pf,
            "flipped": flipped,
        })

    return rows


def print_delay_sensitivity(rows: List[dict]):
    """Print delay sensitivity table."""
    if not rows:
        print("  No data for delay sensitivity.")
        return

    print(f"\n  {'─'*85}")
    print(f"  ENTRY DELAY SENSITIVITY")
    print(f"  {'─'*85}")
    print(f"    {'Delay':>5} {'N':>4} {'1m PnL':>8} {'Retained':>9} {'WR':>6} {'PF':>6} {'Flipped':>8}")
    for r in rows:
        pf_s = f"{r['pf']:.2f}" if r['pf'] < 999 else "inf"
        print(f"    {r['delay']:>4}m {r['n']:>4} {r['pnl_1min']:>+7.2f} {r['retained_pct']:>+7.1f}% "
              f"{r['wr']:>5.1f}% {pf_s:>6} {r['flipped']:>5}")


# ── CLI entry point ──

def main():
    import argparse
    parser = argparse.ArgumentParser(description="1-min execution-adjusted backtest for EMA_RECLAIM")
    parser.add_argument("--delay", type=int, default=1,
                        help="Entry delay in minutes (default: 1)")
    parser.add_argument("--symbols", nargs="+",
                        help="Specific symbols to test")
    parser.add_argument("--verbose", action="store_true",
                        help="Show per-trade detail")
    parser.add_argument("--sensitivity", action="store_true",
                        help="Run delay sensitivity analysis (0-5 min delays)")
    args = parser.parse_args()

    # Check if 1-min data exists
    if not DATA_1MIN_DIR.exists() or not any(DATA_1MIN_DIR.glob("*_1min.csv")):
        print(f"\n  ERROR: No 1-min data found in {DATA_1MIN_DIR}")
        print(f"  Run download_1min.py first to collect 1-minute data from IBKR.")
        print(f"  Usage: python -m alert_overlay.download_1min --port 7497\n")
        sys.exit(1)

    syms = [s.upper() for s in args.symbols] if args.symbols else None

    # Main comparison
    results = run_1min_comparison(
        entry_delay_minutes=args.delay,
        symbols=syms,
    )

    print_comparison_report(results, entry_delay=args.delay)

    # Verbose per-trade log
    if args.verbose and results:
        print(f"\n  {'─'*95}")
        print(f"  TRADE-BY-TRADE LOG")
        print(f"  {'─'*95}")
        print(f"  {'#':>3} {'Sym':<6} {'Time':<20} {'Dir':>4} "
              f"{'E5m':>8} {'E1m':>8} {'PnL5':>7} {'PnL1':>7} "
              f"{'5mExit':<9} {'1mExit':<9} {'MAE_R':>5} {'MFE_R':>5}")
        for i, r in enumerate(results, 1):
            dir_s = "LONG" if r.direction == 1 else "SHRT"
            mae_r = r.mae_1min / r.risk if r.risk > 0 else 0
            mfe_r = r.mfe_1min / r.risk if r.risk > 0 else 0
            print(f"  {i:>3} {r.sym:<6} {str(r.signal_time)[:19]} {dir_s:>4} "
                  f"{r.entry_5min:>8.2f} {r.entry_1min:>8.2f} "
                  f"{r.pnl_5min:>+6.2f} {r.pnl_1min:>+6.2f} "
                  f"{r.exit_reason_5min:<9} {r.exit_reason_1min:<9} "
                  f"{mae_r:>5.2f} {mfe_r:>5.2f}")

    # Delay sensitivity
    if args.sensitivity:
        print(f"\n  Running delay sensitivity analysis...")
        delay_rows = run_delay_sensitivity()
        print_delay_sensitivity(delay_rows)


if __name__ == "__main__":
    main()
