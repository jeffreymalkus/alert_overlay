"""
5-min signal / 1-min execution simulation.

Detects setups on 5-min bars (the real engine), then replays the trade
lifecycle on 1-min bars for more realistic execution modelling:

  1. Entry delay:  after the 5-min signal bar closes, the trader needs
     1–2 minutes to see the alert, evaluate, and place the order.
     We simulate fills at the close of the 1st or 2nd 1-min bar after
     signal time (configurable ENTRY_DELAY_MINUTES).

  2. Intra-bar stop:  on 5-min bars, a stop and target can both be
     hit in the same candle. On 1-min bars we see the real path and
     know which was hit first.

  3. Trail / time-stop:  same logic but on 1-min bars for more
     granular exit timing.

  4. Fill quality:  fast-bar slippage — if the 1-min bar where we
     fill has a range > 2× recent 1-min ATR, we model extra adverse
     slippage.

Comparison report:
  A. 5-min baseline (standard backtest)
  B. 5-min signals → 1-min execution (delay=1 min)
  C. 5-min signals → 1-min execution (delay=2 min)

Usage:
    python -m alert_overlay.exec_sim_1min
"""

import argparse
import math
import statistics
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Tuple
from zoneinfo import ZoneInfo

from ..backtest import load_bars_from_csv, run_backtest, Trade, _compute_dynamic_slippage
from ..config import OverlayConfig
from ..models import (
    Bar, Signal, NaN, SetupId, SetupFamily,
    SETUP_FAMILY_MAP, SETUP_DISPLAY_NAME,
)

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent.parent / "data"
ONEMIN_DIR = DATA_DIR / "1min"


# ── 1-min execution simulator ──────────────────────────────────────

@dataclass
class ExecTrade:
    """Trade result from the 1-min execution simulator."""
    signal: Signal
    filled_entry: float = 0.0
    entry_time: object = None        # actual 1-min bar of fill
    exit_price: float = 0.0
    exit_time: object = None
    exit_reason: str = ""
    pnl_points: float = 0.0
    pnl_rr: float = 0.0
    bars_held_1min: int = 0
    bars_held_5min_equiv: int = 0    # for comparison
    entry_delay_bars: int = 0        # how many 1-min bars after signal
    entry_slip_actual: float = 0.0   # total adverse move from signal close


def simulate_1min_execution(
    signal: Signal,
    onemin_bars: List[Bar],
    cfg: OverlayConfig,
    entry_delay_minutes: int = 1,
    session_end_hhmm: int = 1555,
) -> Optional[ExecTrade]:
    """
    Given a 5-min signal, simulate realistic execution on 1-min bars.

    Args:
        signal: the Signal from the 5-min engine
        onemin_bars: ALL 1-min bars for the symbol (full day or multi-day)
        cfg: config for exit rules
        entry_delay_minutes: how many 1-min bars to wait before filling
        session_end_hhmm: end of session time

    Returns:
        ExecTrade or None if signal can't be filled (e.g. no 1-min data)
    """
    sig = signal
    direction = sig.direction

    # Find the 1-min bar index at or immediately after signal timestamp.
    # The 5-min signal fires at the close of a 5-min candle.  Its timestamp
    # is the candle's open time (e.g. 10:00 for the 10:00-10:04 candle).
    # The corresponding 1-min bars span start_idx … start_idx+4.
    # The signal is confirmed when that 5-min bar CLOSES, i.e. at the end
    # of the 5th 1-min bar (start_idx + 4).
    sig_ts = sig.timestamp
    sig_date = sig_ts.date()
    start_idx = None
    for idx, b in enumerate(onemin_bars):
        if b.timestamp >= sig_ts:
            start_idx = idx
            break

    if start_idx is None:
        return None

    # The 5-min candle close corresponds to start_idx + 4 (the last 1-min
    # bar in that 5-min window).  For automated execution the fill is at
    # the close of the NEXT 1-min bar (start_idx + 5 = +0 delay after
    # candle close).  Human delay adds 1-2 bars on top of that.
    candle_close_idx = start_idx + 4          # last 1-min bar of the 5-min candle
    fill_idx = candle_close_idx + 1 + entry_delay_minutes  # +1 for next bar, then delay

    if fill_idx >= len(onemin_bars):
        return None

    fill_bar = onemin_bars[fill_idx]

    # CRITICAL: fill bar must be on the same trading day as the signal.
    # Late-day signals (e.g. 15:55) may roll fill_idx into the next day
    # due to the +1 delay after candle close.  Reject these — they can't
    # be filled within the session.
    if fill_bar.timestamp.date() != sig_date:
        return None

    # Check: if stop was already hit between candle close and fill, trade
    # is dead on arrival.  For delay=0 (auto), we only check the single
    # transition bar; for delay>0, we check the full delay window.
    for delay_idx in range(candle_close_idx + 1, fill_idx + 1):
        if delay_idx >= len(onemin_bars):
            break
        db = onemin_bars[delay_idx]
        # Also reject if bar rolled to next day
        if db.timestamp.date() != sig_date:
            return None
        if direction == 1 and db.low <= sig.stop_price:
            return None  # stop hit before fill
        if direction == -1 and db.high >= sig.stop_price:
            return None  # stop hit before fill

    # Compute entry fill with slippage
    # Base: fill at close of the delay bar
    base_fill = fill_bar.close

    # Fast-bar extra slippage: if 1-min bar range is large, add adverse slippage
    fill_range = fill_bar.high - fill_bar.low
    # Compute a simple 1-min ATR from recent bars
    recent_ranges = []
    for rb in onemin_bars[max(0, fill_idx-20):fill_idx]:
        recent_ranges.append(rb.high - rb.low)
    avg_1m_range = statistics.mean(recent_ranges) if recent_ranges else fill_range
    fast_bar_mult = min(fill_range / avg_1m_range, 3.0) if avg_1m_range > 0 else 1.0

    # Dynamic slippage (same model as 5-min but on 1-min bar)
    family = SETUP_FAMILY_MAP.get(sig.setup_id, SetupFamily.BREAKOUT)
    if cfg.use_dynamic_slippage:
        base_slip = max(cfg.slip_min, base_fill * cfg.slip_bps)
        # Use fast_bar_mult instead of bar_range/ATR since we're on 1-min
        vol_mult = min(max(fast_bar_mult, 0.5), cfg.slip_vol_mult_cap)
        family_mult_map = {
            SetupFamily.REVERSAL: cfg.slip_family_mult_reversal,
            SetupFamily.TREND: cfg.slip_family_mult_trend,
            SetupFamily.MEAN_REV: cfg.slip_family_mult_reversal,
            SetupFamily.BREAKOUT: cfg.slip_family_mult_breakout,
            SetupFamily.SHORT_STRUCT: cfg.slip_family_mult_short_struct,
            SetupFamily.EMA_SCALP: cfg.slip_family_mult_ema_scalp,
        }
        family_mult = family_mult_map.get(family, 1.0)
        entry_slip = base_slip * vol_mult * family_mult
    else:
        entry_slip = cfg.slippage_per_side

    comm = cfg.commission_per_share
    entry_cost = entry_slip + comm
    filled_entry = base_fill + (entry_cost * direction)

    # Compute adverse move from the 5-min signal close
    slip_from_signal = abs(filled_entry - sig.entry_price)

    # ── Exit simulation on 1-min bars ──
    # Determine exit mode from config (same logic as backtest.py)
    sig_family = SETUP_FAMILY_MAP.get(sig.setup_id)
    is_breakout = sig_family == SetupFamily.BREAKOUT
    is_short_struct = sig_family == SetupFamily.SHORT_STRUCT
    is_ema_scalp = sig_family == SetupFamily.EMA_SCALP
    uses_trail_exit = is_breakout or is_short_struct or is_ema_scalp

    if is_ema_scalp and sig.setup_id == SetupId.EMA_FPIP:
        exit_mode = cfg.ema_fpip_exit_mode
        time_stop_bars_5min = cfg.ema_fpip_time_stop_bars
    elif is_ema_scalp:
        exit_mode = cfg.ema_scalp_exit_mode
        time_stop_bars_5min = cfg.ema_scalp_time_stop_bars
    elif is_short_struct:
        exit_mode = cfg.fb_exit_mode
        time_stop_bars_5min = cfg.fb_time_stop_bars
    elif is_breakout and sig.setup_id == SetupId.SC_V2:
        exit_mode = cfg.sc2_exit_mode
        time_stop_bars_5min = cfg.sc2_time_stop_bars
    elif is_breakout:
        exit_mode = cfg.breakout_exit_mode
        time_stop_bars_5min = cfg.breakout_time_stop_bars
    else:
        exit_mode = "target"
        time_stop_bars_5min = 999

    # Convert 5-min time stop to 1-min bars (×5)
    time_stop_bars_1min = time_stop_bars_5min * 5

    # Simple EMA9 proxy for trail exit (exponential smoothing on 1-min closes)
    # EMA9 on 5-min ≈ EMA45 on 1-min
    ema_period = 45
    ema_mult = 2.0 / (ema_period + 1)
    ema_val = base_fill  # seed with entry price

    # Pre-warm EMA from recent bars before entry
    for wb in onemin_bars[max(0, fill_idx - ema_period * 2):fill_idx]:
        ema_val = wb.close * ema_mult + ema_val * (1.0 - ema_mult)

    # Walk forward from fill bar
    exit_price = 0.0
    exit_time = None
    exit_reason = ""
    bars_held = 0

    for j in range(fill_idx + 1, len(onemin_bars)):
        b = onemin_bars[j]
        bars_held = j - fill_idx

        # Update EMA
        ema_val = b.close * ema_mult + ema_val * (1.0 - ema_mult)

        # EOD exit — compute time_hhmm from timestamp since 1-min bars
        # don't go through the engine (which normally sets time_hhmm)
        bar_hhmm = b.timestamp.hour * 100 + b.timestamp.minute
        if bar_hhmm >= session_end_hhmm:
            exit_price = b.close
            exit_time = b.timestamp
            exit_reason = "eod"
            break

        # New day — force close if carried overnight (shouldn't happen for intraday)
        if b.timestamp.date() != fill_bar.timestamp.date():
            # Close at previous bar's close
            prev_b = onemin_bars[j - 1]
            exit_price = prev_b.close
            exit_time = prev_b.timestamp
            exit_reason = "eod"
            bars_held = j - 1 - fill_idx
            break

        # Stop check (exact: on 1-min we know the path)
        hit_stop = ((direction == 1 and b.low <= sig.stop_price) or
                    (direction == -1 and b.high >= sig.stop_price))

        if uses_trail_exit:
            hit_time_stop = (exit_mode in ("time", "hybrid") and
                             bars_held >= time_stop_bars_1min)

            # EMA trail: need at least 10 1-min bars (≈ 2 × 5-min bars)
            ema9_exit = False
            if exit_mode in ("ema9_trail", "hybrid") and bars_held >= 10 and ema_val > 0:
                ema9_exit = ((direction == 1 and b.close < ema_val) or
                             (direction == -1 and b.close > ema_val))

            if hit_stop:
                exit_price = sig.stop_price
                exit_time = b.timestamp
                exit_reason = "stop"
                break
            elif hit_time_stop:
                exit_price = b.close
                exit_time = b.timestamp
                exit_reason = "time"
                break
            elif ema9_exit:
                exit_price = b.close
                exit_time = b.timestamp
                exit_reason = "ema9trail"
                break
        else:
            hit_target = ((direction == 1 and b.high >= sig.target_price) or
                          (direction == -1 and b.low <= sig.target_price))
            if hit_stop:
                exit_price = sig.stop_price
                exit_time = b.timestamp
                exit_reason = "stop"
                break
            elif hit_target:
                exit_price = sig.target_price
                exit_time = b.timestamp
                exit_reason = "target"
                break

    # If we never exited, close at last bar
    if exit_reason == "":
        last = onemin_bars[-1]
        exit_price = last.close
        exit_time = last.timestamp
        exit_reason = "eod"
        bars_held = len(onemin_bars) - 1 - fill_idx

    # Compute exit slippage
    if cfg.use_dynamic_slippage:
        exit_bar_idx = fill_idx + bars_held
        if exit_bar_idx < len(onemin_bars):
            eb = onemin_bars[exit_bar_idx]
        else:
            eb = onemin_bars[-1]
        eb_range = eb.high - eb.low
        eb_fast = min(eb_range / avg_1m_range, 3.0) if avg_1m_range > 0 else 1.0
        exit_slip = max(cfg.slip_min, exit_price * cfg.slip_bps) * min(max(eb_fast, 0.5), cfg.slip_vol_mult_cap) * family_mult_map.get(family, 1.0)
    else:
        exit_slip = cfg.slippage_per_side

    exit_cost = exit_slip + comm
    adjusted_exit = exit_price - (exit_cost * direction)

    pnl_points = (adjusted_exit - filled_entry) * direction
    pnl_rr = pnl_points / sig.risk if sig.risk > 0 else 0.0

    return ExecTrade(
        signal=sig,
        filled_entry=filled_entry,
        entry_time=fill_bar.timestamp,
        exit_price=exit_price,
        exit_time=exit_time,
        exit_reason=exit_reason,
        pnl_points=pnl_points,
        pnl_rr=pnl_rr,
        bars_held_1min=bars_held,
        bars_held_5min_equiv=round(bars_held / 5),
        entry_delay_bars=entry_delay_minutes,
        entry_slip_actual=slip_from_signal,
    )


# ── metrics ──────────────────────────────────────────────────────────

def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "∞"

def compute_metrics(trades, n_days=None):
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "exp": 0.0,
                "stop_rate": 0.0, "max_dd": 0.0, "avg_hold": 0.0, "tpw": 0.0}
    wins = [t for t in trades if t.pnl_points > 0]
    losses = [t for t in trades if t.pnl_points <= 0]
    pnl = sum(t.pnl_points for t in trades)
    gw = sum(t.pnl_points for t in wins)
    gl = abs(sum(t.pnl_points for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    avg_rr = sum(t.pnl_rr for t in trades) / n
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    cum = pk = dd = 0.0
    for t in trades:
        cum += t.pnl_points
        if cum > pk: pk = cum
        if pk - cum > dd: dd = pk - cum
    td = len(set(str(t.signal.timestamp)[:10] for t in trades))
    weeks = (n_days or td) / 5.0

    if hasattr(trades[0], 'bars_held_5min_equiv'):
        avg_hold = statistics.mean(t.bars_held_5min_equiv for t in trades)
    elif hasattr(trades[0], 'bars_held'):
        avg_hold = statistics.mean(t.bars_held for t in trades)
    else:
        avg_hold = 0

    return {"n": n, "wr": len(wins)/n*100, "pf": pf, "pnl": pnl,
            "exp": avg_rr, "stop_rate": stopped/n*100, "max_dd": dd,
            "avg_hold": avg_hold, "tpw": n/weeks if weeks > 0 else 0}


# ── signal extraction from 5-min backtest ────────────────────────────

def extract_5min_signals(bars_5min, cfg, spy_bars=None, qqq_bars=None, sector_bars=None):
    """Run the 5-min backtest and extract all emitted signals with their trades."""
    from .engine import SignalEngine
    from .market_context import MarketEngine, compute_market_context

    engine = SignalEngine(cfg)
    spy_engine = MarketEngine() if spy_bars else None
    qqq_engine = MarketEngine() if qqq_bars else None
    sector_engine = MarketEngine() if sector_bars else None

    spy_ts_map = {b.timestamp: i for i, b in enumerate(spy_bars)} if spy_bars else {}
    qqq_ts_map = {b.timestamp: i for i, b in enumerate(qqq_bars)} if qqq_bars else {}
    sector_ts_map = {b.timestamp: i for i, b in enumerate(sector_bars)} if sector_bars else {}

    spy_idx = qqq_idx = sector_idx = 0
    spy_snap = qqq_snap = sector_snap = None

    all_signals = []

    for i, bar in enumerate(bars_5min):
        # Market context
        market_ctx = None
        if cfg.use_market_context and spy_bars and qqq_bars:
            ts = bar.timestamp
            if ts in spy_ts_map and spy_ts_map[ts] >= spy_idx:
                while spy_idx <= spy_ts_map[ts]:
                    spy_snap = spy_engine.process_bar(spy_bars[spy_idx])
                    spy_idx += 1
            if ts in qqq_ts_map and qqq_ts_map[ts] >= qqq_idx:
                while qqq_idx <= qqq_ts_map[ts]:
                    qqq_snap = qqq_engine.process_bar(qqq_bars[qqq_idx])
                    qqq_idx += 1
            if sector_bars and ts in sector_ts_map and sector_ts_map[ts] >= sector_idx:
                while sector_idx <= sector_ts_map[ts]:
                    sector_snap = sector_engine.process_bar(sector_bars[sector_idx])
                    sector_idx += 1

            stock_day_open = getattr(engine, '_day_open_for_rs', NaN)
            date_int = bar.timestamp.year * 10000 + bar.timestamp.month * 100 + bar.timestamp.day
            if not hasattr(engine, '_rs_date') or engine._rs_date != date_int:
                engine._rs_date = date_int
                engine._day_open_for_rs = bar.open
                stock_day_open = bar.open
            else:
                stock_day_open = engine._day_open_for_rs
            stock_pct = (bar.close - stock_day_open) / stock_day_open * 100.0 if stock_day_open > 0 else NaN

            from .market_context import MarketSnapshot
            if spy_snap and qqq_snap:
                market_ctx = compute_market_context(
                    spy_snap, qqq_snap,
                    sector_snapshot=sector_snap,
                    stock_pct_from_open=stock_pct)

        signals = engine.process_bar(bar, market_ctx=market_ctx)
        for sig in signals:
            all_signals.append(sig)

    return all_signals


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="5-min signal / 1-min execution simulation")
    args = parser.parse_args()

    # Discover 1-min symbols
    onemin_syms = sorted(set(
        p.stem.replace("_1min", "") for p in ONEMIN_DIR.glob("*_1min.csv")
    ))
    common_syms = [s for s in onemin_syms if s not in ("SPY", "QQQ")]

    print(f"Symbols with 1-min data: {len(common_syms)}")

    # Load 5-min bars
    bars_5min = {}
    for sym in common_syms:
        p = DATA_DIR / f"{sym}_5min.csv"
        if p.exists():
            bars_5min[sym] = load_bars_from_csv(str(p))

    # Load 1-min bars
    bars_1min = {}
    for sym in common_syms:
        p = ONEMIN_DIR / f"{sym}_1min.csv"
        if p.exists():
            bars_1min[sym] = load_bars_from_csv(str(p))

    # Only process symbols with both
    common_syms = sorted(set(bars_5min.keys()) & set(bars_1min.keys()))
    print(f"Common symbols: {len(common_syms)}")

    # Market context bars (5-min for signal detection)
    spy_5m = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv")) if (DATA_DIR / "SPY_5min.csv").exists() else None
    qqq_5m = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv")) if (DATA_DIR / "QQQ_5min.csv").exists() else None

    # Sector ETFs
    sector_bars_dict = {}
    from .market_context import SECTOR_MAP, get_sector_etf
    sector_etfs = set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    all_dates = sorted(set(
        b.timestamp.date() for bars in bars_5min.values()
        for b in bars if bars
    ))
    n_days = len(all_dates)
    print(f"Period: {min(all_dates)} → {max(all_dates)} ({n_days} days)")

    # Config: locked baseline
    cfg = OverlayConfig()
    cfg.show_ema_scalp = False
    cfg.show_failed_bounce = False
    cfg.show_spencer = False
    cfg.show_ema_fpip = False
    cfg.show_sc_v2 = False

    # ══════════════════════════════════════════════════════════════
    #  Helper: run one execution-sim pass
    # ══════════════════════════════════════════════════════════════
    def run_sim_pass(delay_minutes):
        """Run 1-min execution sim for all symbols at given delay.
        Returns (sim_trades_with_sym, total_signals, killed, no_1min)."""
        sim_trades = []
        killed = 0
        no_1min = 0
        total_sigs = 0
        for sym in common_syms:
            sec_bars = None
            se = get_sector_etf(sym)
            if se and se in sector_bars_dict:
                sec_bars = sector_bars_dict[se]
            signals = extract_5min_signals(bars_5min[sym], cfg,
                                            spy_bars=spy_5m, qqq_bars=qqq_5m,
                                            sector_bars=sec_bars)
            total_sigs += len(signals)
            onemin = bars_1min.get(sym, [])
            if not onemin:
                no_1min += len(signals)
                continue
            for sig in signals:
                et = simulate_1min_execution(sig, onemin, cfg,
                                             entry_delay_minutes=delay_minutes,
                                             session_end_hhmm=1555)
                if et is None:
                    killed += 1
                else:
                    sim_trades.append((sym, et))
        return sim_trades, total_sigs, killed, no_1min

    # ══════════════════════════════════════════════════════════════
    #  STEP 1: 5-min idealized baseline
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print("  STEP 1: 5-MIN IDEALIZED BASELINE (standard backtest)")
    print(f"{'='*110}")

    baseline_trades = []
    for sym in common_syms:
        sec_bars = None
        se = get_sector_etf(sym)
        if se and se in sector_bars_dict:
            sec_bars = sector_bars_dict[se]
        result = run_backtest(bars_5min[sym], cfg=cfg,
                              spy_bars=spy_5m, qqq_bars=qqq_5m,
                              sector_bars=sec_bars)
        for t in result.trades:
            baseline_trades.append((sym, t))

    baseline = [t for _, t in baseline_trades]
    total_5min_signals = sum(1 for _ in baseline)  # all became trades

    # ══════════════════════════════════════════════════════════════
    #  STEP 2: Three 1-min execution modes
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print("  STEP 2: 1-MIN EXECUTION SIMULATION — THREE MODES")
    print(f"{'='*110}")

    modes = [
        ("Auto (0m delay)",  0),
        ("Human (1m delay)", 1),
        ("Human (2m delay)", 2),
    ]

    mode_results = {}
    for mode_label, delay in modes:
        sim_raw, total_sigs, killed, no_1min = run_sim_pass(delay)
        trades = [t for _, t in sim_raw]
        valid_sigs = total_sigs - no_1min
        fill_rate = len(trades) / valid_sigs * 100 if valid_sigs > 0 else 0
        kill_rate = killed / valid_sigs * 100 if valid_sigs > 0 else 0

        avg_slip = statistics.mean(t.entry_slip_actual for t in trades) if trades else 0
        max_slip = max((t.entry_slip_actual for t in trades), default=0)

        mode_results[mode_label] = {
            "trades": trades,
            "raw": sim_raw,
            "total_sigs": total_sigs,
            "killed": killed,
            "fill_rate": fill_rate,
            "kill_rate": kill_rate,
            "avg_slip": avg_slip,
            "max_slip": max_slip,
        }

        m = compute_metrics(trades, n_days)
        print(f"\n  ── {mode_label} ──")
        print(f"    5-min signals: {total_sigs}  |  Filled: {len(trades)}  |  "
              f"Killed before fill: {killed} ({kill_rate:.0f}%)  |  Fill rate: {fill_rate:.0f}%")
        print(f"    N={m['n']}  WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  "
              f"Exp={m['exp']:+.3f}R  PnL={m['pnl']:+.2f}  "
              f"MaxDD={m['max_dd']:.1f}  StpR={m['stop_rate']:.0f}%  "
              f"Hold={m['avg_hold']:.1f}bars(5m-eq)")
        print(f"    Avg slip from signal: ${avg_slip:.4f}  Max: ${max_slip:.4f}")

        # Exit reasons
        exit_dist = defaultdict(int)
        for t in trades:
            exit_dist[t.exit_reason] += 1
        exits_str = "  ".join(f"{k}:{v}" for k, v in sorted(exit_dist.items()))
        print(f"    Exits: {exits_str}")

        # Per-setup
        by_setup = defaultdict(list)
        for sym, t in sim_raw:
            name = SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id))
            by_setup[name].append(t)
        for name in sorted(by_setup):
            ms = compute_metrics(by_setup[name])
            print(f"      {name:<18}: N={ms['n']:>3}  WR={ms['wr']:.1f}%  "
                  f"PF={pf_str(ms['pf'])}  Exp={ms['exp']:+.3f}R  PnL={ms['pnl']:+.2f}")

    # ══════════════════════════════════════════════════════════════
    #  STEP 3: SIDE-BY-SIDE COMPARISON TABLE
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print("  STEP 3: SIDE-BY-SIDE COMPARISON")
    print(f"{'='*110}")

    m_base = compute_metrics(baseline, n_days)

    hdr = (f"  {'Mode':<25} │ {'N':>4} {'WR%':>5} {'PF':>5} {'Exp':>7} "
           f"{'PnL':>8} {'MaxDD':>5} {'StpR':>4} {'Hold':>5} │ "
           f"{'Kill%':>5} {'AvgSlip':>7}")
    print(f"\n{hdr}")
    print(f"  {'─'*25}─┼{'─'*50}─┼{'─'*14}")

    # Baseline row
    print(f"  {'1. Idealized 5-min':<25} │ {m_base['n']:>4} {m_base['wr']:>5.1f}% "
          f"{pf_str(m_base['pf']):>5} {m_base['exp']:>+6.3f}R {m_base['pnl']:>+8.2f} "
          f"{m_base['max_dd']:>5.1f} {m_base['stop_rate']:>4.0f}% {m_base['avg_hold']:>5.1f} │ "
          f"{'0%':>5} {'$0.00':>7}")

    # Sim rows
    for i, (mode_label, delay) in enumerate(modes):
        r = mode_results[mode_label]
        m = compute_metrics(r["trades"], n_days)
        print(f"  {f'{i+2}. {mode_label}':<25} │ {m['n']:>4} {m['wr']:>5.1f}% "
              f"{pf_str(m['pf']):>5} {m['exp']:>+6.3f}R {m['pnl']:>+8.2f} "
              f"{m['max_dd']:>5.1f} {m['stop_rate']:>4.0f}% {m['avg_hold']:>5.1f} │ "
              f"{r['kill_rate']:>4.0f}% ${r['avg_slip']:>.4f}")

    # ══════════════════════════════════════════════════════════════
    #  STEP 4: DEGRADATION TABLE
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print("  STEP 4: DEGRADATION FROM IDEALIZED 5-MIN BASELINE")
    print(f"{'='*110}")

    print(f"\n  {'Mode':<25} │ {'ΔN':>4} {'ΔWR':>6} {'ΔPF':>6} {'ΔExp':>7} "
          f"{'ΔPnL':>8} {'ΔDD':>6} {'ΔStpR':>6}")
    print(f"  {'─'*25}─┼{'─'*48}")

    for mode_label, delay in modes:
        r = mode_results[mode_label]
        m = compute_metrics(r["trades"], n_days)
        dn = m['n'] - m_base['n']
        dwr = m['wr'] - m_base['wr']
        dpf = m['pf'] - m_base['pf'] if m_base['pf'] < 999 and m['pf'] < 999 else 0
        dexp = m['exp'] - m_base['exp']
        dpnl = m['pnl'] - m_base['pnl']
        ddd = m['max_dd'] - m_base['max_dd']
        dstp = m['stop_rate'] - m_base['stop_rate']
        print(f"  {mode_label:<25} │ {dn:>+4d} {dwr:>+5.1f}% {dpf:>+5.2f} "
              f"{dexp:>+6.3f}R {dpnl:>+8.2f} {ddd:>+5.1f} {dstp:>+5.1f}%")

    # ══════════════════════════════════════════════════════════════
    #  STEP 5: KILLED TRADES ANALYSIS
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print("  STEP 5: PRE-FILL KILL ANALYSIS (trades stopped before entry)")
    print(f"{'='*110}")

    # For this we compare baseline trades to auto (0-delay).
    # Any baseline trade NOT in the auto set was killed.
    auto_r = mode_results["Auto (0m delay)"]
    auto_sigs = {t.signal.timestamp for t in auto_r["trades"]}

    # Find which baseline trades would have been killed
    killed_trades = [t for t in baseline if t.signal.timestamp not in auto_sigs]
    surviving_trades = [t for t in baseline if t.signal.timestamp in auto_sigs]

    if killed_trades:
        m_killed = compute_metrics(killed_trades)
        m_surviving = compute_metrics(surviving_trades)
        print(f"\n  Killed trades (stopped before 1-min fill):")
        print(f"    N={m_killed['n']}  WR={m_killed['wr']:.1f}%  PF={pf_str(m_killed['pf'])}  "
              f"Exp={m_killed['exp']:+.3f}R  PnL={m_killed['pnl']:+.2f}")
        print(f"  Surviving trades:")
        print(f"    N={m_surviving['n']}  WR={m_surviving['wr']:.1f}%  PF={pf_str(m_surviving['pf'])}  "
              f"Exp={m_surviving['exp']:+.3f}R  PnL={m_surviving['pnl']:+.2f}")
        print(f"\n  → Killed trades were {'profitable' if m_killed['pnl'] > 0 else 'net losers'} "
              f"({m_killed['pnl']:+.2f}pts). "
              f"{'Losing them hurts.' if m_killed['pnl'] > 0 else 'Good riddance.'}")
    else:
        print(f"\n  No trades killed at 0-delay — all 5-min signals fillable on 1-min.")

    # ══════════════════════════════════════════════════════════════
    #  SUMMARY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print("  EXECUTION SIMULATION SUMMARY")
    print(f"{'='*110}")
    print(f"  Method: 5-min signal detection → 1-min execution simulation")
    print(f"  Symbols: {len(common_syms)}")
    print(f"  Period: {min(all_dates)} → {max(all_dates)} ({n_days} days)")
    print(f"  Modes: Idealized 5-min | Auto (0m) | Human (1m) | Human (2m)")
    print(f"  Entry: signal at 5-min bar close → fill at 1-min bar close after delay")
    print(f"  Stops: exact 1-min path (resolves 5-min same-bar ambiguity)")
    print(f"  Trail: EMA45 on 1-min ≈ EMA9 on 5-min")
    print(f"  Slippage: dynamic model on 1-min fill bar + fast-bar multiplier")
    print(f"{'='*110}\n")


if __name__ == "__main__":
    main()
