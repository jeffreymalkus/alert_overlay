"""
Breakdown-Retest Short — Feature Study Framework.

Scans raw bars for the pattern:
  1. Identify support levels (OR low, VWAP, intraday swing low)
  2. Detect decisive break below support
  3. Detect weak retest toward the broken level
  4. Detect rejection / re-break below retest low
  5. Simulate short entry at rejection bar close, stop above retest high

This is a standalone scanner — independent of the engine's FB state machine.
Market context (trend, RS) is computed by running the engine in parallel.

For each candidate trade, logs:
  breakdown_size_atr, retest_depth_pct, retest_bar_count, retest_vol_ratio,
  retest_overlap, rejection_wick_pct, rejection_body_pct, rejection_bearish,
  dist_vwap_atr, dist_ema9_atr, market_trend, rs_market, rs_sector,
  time_bucket, follow_through_1b, level_type

Usage:
    python -m alert_overlay.breakdown_retest_study --universe all94
"""

import argparse
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from .backtest import load_bars_from_csv
from .config import OverlayConfig
from .models import NaN, Bar

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent / "data"
WATCHLIST_FILE = Path(__file__).parent / "watchlist.txt"

# ── Pattern parameters (coarse, not tuned) ──
OR_END_HHMM = 1000        # opening range ends at 10:00
SCAN_START_HHMM = 1000    # don't look for breakdowns before OR forms
SCAN_END_HHMM = 1500      # no new entries after 15:00
ATR_LOOKBACK = 14
VOLMA_LOOKBACK = 20
SWING_LOOKBACK = 10       # bars for swing low detection

# Breakdown requirements
BD_MIN_CLOSE_DIST_ATR = 0.15   # close must be ≥ 0.15 ATR below level
BD_MIN_RANGE_FRAC = 0.70       # bar range ≥ 70% of median range
BD_MIN_VOL_FRAC = 0.90         # volume ≥ 90% of vol_ma
BD_CLOSE_IN_LOWER_PCT = 0.60   # close in lower 60% of bar

# Retest requirements
RETEST_WINDOW = 8              # max bars after BD to detect retest
RETEST_MIN_APPROACH_ATR = 0.3  # retest high must get within 0.3 ATR of level
RETEST_MAX_RECLAIM_ATR = 0.3   # retest high can't exceed level by more than 0.3 ATR

# Rejection requirements
REJECT_WINDOW = 4              # max bars after retest peak to find rejection
REJECT_MUST_CLOSE_BELOW_RETEST_LOW = True

# Exit simulation
TIME_STOP_BARS = 8
STOP_BUFFER_ATR = 0.3          # stop above retest high + 0.3 ATR
SESSION_END_HHMM = 1555


def load_watchlist(path=WATCHLIST_FILE):
    symbols = []
    with open(path) as f:
        for line in f:
            sym = line.strip().upper()
            if sym and not sym.startswith("#"):
                symbols.append(sym)
    return symbols


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "∞"


# ══════════════════════════════════════════════════════════════
#  RUNNING INDICATORS (computed from bars)
# ══════════════════════════════════════════════════════════════

@dataclass
class BarContext:
    """Running indicators at each bar."""
    i_atr: float = 0.0
    vol_ma: float = 0.0
    med_range: float = 0.0
    vwap: float = 0.0
    ema9: float = 0.0
    ema20: float = 0.0
    or_low: float = NaN
    or_high: float = NaN
    or_ready: bool = False
    swing_low: float = NaN       # lowest low in last SWING_LOOKBACK bars


def compute_bar_contexts(bars: List[Bar]) -> List[BarContext]:
    """Compute running indicators for each bar."""
    contexts = []
    range_buf = []
    vol_buf = []
    vwap_cum_pv = 0.0
    vwap_cum_vol = 0.0
    ema9 = 0.0
    ema20 = 0.0
    ema9_mult = 2.0 / (9 + 1)
    ema20_mult = 2.0 / (20 + 1)
    ema9_init = False
    ema20_init = False

    day_date = None
    day_or_high = NaN
    day_or_low = NaN
    day_or_ready = False

    for i, bar in enumerate(bars):
        # Day reset
        cur_date = bar.timestamp.date()
        if cur_date != day_date:
            day_date = cur_date
            day_or_high = NaN
            day_or_low = NaN
            day_or_ready = False
            vwap_cum_pv = 0.0
            vwap_cum_vol = 0.0
            ema9_init = False
            ema20_init = False

        # OR tracking
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute
        if not day_or_ready:
            if math.isnan(day_or_high) or bar.high > day_or_high:
                day_or_high = bar.high
            if math.isnan(day_or_low) or bar.low < day_or_low:
                day_or_low = bar.low
            if hhmm >= OR_END_HHMM:
                day_or_ready = True

        # VWAP
        typical = (bar.high + bar.low + bar.close) / 3.0
        vwap_cum_pv += typical * bar.volume
        vwap_cum_vol += bar.volume
        vwap = vwap_cum_pv / vwap_cum_vol if vwap_cum_vol > 0 else bar.close

        # EMA
        if not ema9_init:
            ema9 = bar.close
            ema9_init = True
        else:
            ema9 = bar.close * ema9_mult + ema9 * (1 - ema9_mult)

        if not ema20_init:
            ema20 = bar.close
            ema20_init = True
        else:
            ema20 = bar.close * ema20_mult + ema20 * (1 - ema20_mult)

        # ATR / range / vol
        rng = bar.high - bar.low
        range_buf.append(rng)
        if len(range_buf) > ATR_LOOKBACK:
            range_buf.pop(0)
        vol_buf.append(bar.volume)
        if len(vol_buf) > VOLMA_LOOKBACK:
            vol_buf.pop(0)

        i_atr = statistics.mean(range_buf) if range_buf else 0.01
        vol_ma = statistics.mean(vol_buf) if vol_buf else 1.0
        med_range = statistics.median(range_buf) if len(range_buf) >= 5 else i_atr

        # Swing low
        lookback_start = max(0, i - SWING_LOOKBACK)
        swing_low = min(bars[j].low for j in range(lookback_start, i + 1))

        ctx = BarContext(
            i_atr=max(i_atr, 0.001),
            vol_ma=max(vol_ma, 1.0),
            med_range=max(med_range, 0.001),
            vwap=vwap,
            ema9=ema9,
            ema20=ema20,
            or_low=day_or_low if day_or_ready else NaN,
            or_high=day_or_high if day_or_ready else NaN,
            or_ready=day_or_ready,
            swing_low=swing_low,
        )
        contexts.append(ctx)

    return contexts


# ══════════════════════════════════════════════════════════════
#  PATTERN SCANNER
# ══════════════════════════════════════════════════════════════

@dataclass
class BDRTrade:
    """A breakdown-retest short candidate."""
    sym: str
    entry_idx: int
    entry_price: float
    entry_time: object
    stop_price: float
    risk: float
    level_type: str           # "ORL", "VWAP", "SWING"
    level_price: float
    # Breakdown features
    bd_idx: int
    bd_range_atr: float
    bd_vol_ratio: float
    # Retest features
    retest_depth_pct: float   # how far retest went toward level (0-1+)
    retest_bar_count: int
    retest_vol_ratio: float   # retest vol / BD vol
    retest_overlap: float     # grindiness
    retest_slope_atr: float
    # Rejection features
    rejection_wick_pct: float
    rejection_body_pct: float
    rejection_bearish: bool
    # Context
    dist_vwap_atr: float
    dist_ema9_atr: float
    market_trend: int
    rs_market: float
    rs_sector: float
    time_bucket: str
    # Simulated outcome (filled after sim_exit)
    exit_price: float = 0.0
    exit_time: object = None
    exit_reason: str = ""
    pnl_points: float = 0.0
    pnl_rr: float = 0.0
    bars_held: int = 0
    follow_through_1b: float = 0.0


def scan_for_breakdowns(bars: List[Bar], ctxs: List[BarContext],
                        mkt_at: Dict[int, dict], sym: str) -> List[BDRTrade]:
    """
    Scan bars for breakdown→retest→rejection short setups.
    Returns list of candidate trades (not yet simulated for exit).
    """
    trades = []
    n = len(bars)
    cooldown_until = -1  # prevent overlapping signals

    for i in range(20, n):
        if i <= cooldown_until:
            continue

        bar = bars[i]
        ctx = ctxs[i]
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute

        if hhmm < SCAN_START_HHMM or hhmm > SCAN_END_HHMM:
            continue
        if ctx.i_atr <= 0:
            continue

        rng = bar.high - bar.low
        bearish = bar.close < bar.open

        # ── Step 1: Is this bar a breakdown of a support level? ──
        levels = []
        if ctx.or_ready and not math.isnan(ctx.or_low):
            levels.append(("ORL", ctx.or_low))
        if ctx.vwap > 0:
            levels.append(("VWAP", ctx.vwap))
        if not math.isnan(ctx.swing_low) and ctx.swing_low < bar.open:
            # Only count swing low if it's been established (not the current bar's low)
            prev_swing = min(bars[j].low for j in range(max(0, i - SWING_LOOKBACK), i))
            if prev_swing < bar.open:
                levels.append(("SWING", prev_swing))

        for level_type, level_price in levels:
            # Close decisively below level
            if bar.close >= level_price - BD_MIN_CLOSE_DIST_ATR * ctx.i_atr:
                continue
            # Bar must be bearish with range
            if not bearish:
                continue
            if rng < BD_MIN_RANGE_FRAC * ctx.med_range:
                continue
            if bar.volume < BD_MIN_VOL_FRAC * ctx.vol_ma:
                continue
            # Close in lower portion
            if rng > 0:
                close_pct = (bar.high - bar.close) / rng
                if close_pct < BD_CLOSE_IN_LOWER_PCT:
                    continue

            # ── Step 2: Look for retest in next RETEST_WINDOW bars ──
            retest_end = min(i + RETEST_WINDOW + 1, n)
            retest_bars = []
            retest_peak_idx = None
            retest_peak_high = -999

            for j in range(i + 1, retest_end):
                rb = bars[j]
                # Same day check
                if rb.timestamp.date() != bar.timestamp.date():
                    break
                if rb.time_hhmm >= SESSION_END_HHMM:
                    break

                retest_bars.append((j, rb))

                if rb.high > retest_peak_high:
                    retest_peak_high = rb.high
                    retest_peak_idx = j

                # Check if retest approached the broken level
                dist_to_level = level_price - rb.high  # negative = exceeded level
                within_approach = dist_to_level <= RETEST_MIN_APPROACH_ATR * ctx.i_atr
                not_reclaimed = rb.close < level_price  # close stays below
                not_blasted = rb.high <= level_price + RETEST_MAX_RECLAIM_ATR * ctx.i_atr

                if within_approach and not_reclaimed and not_blasted and len(retest_bars) >= 1:
                    # Retest found — now look for rejection
                    retest_high = rb.high

                    # ── Step 3: Rejection bar in next REJECT_WINDOW bars ──
                    reject_end = min(j + REJECT_WINDOW + 1, n)
                    retest_low = min(bars[k].low for k, _ in retest_bars)

                    for rj in range(j + 1, reject_end):
                        rej_bar = bars[rj]
                        if rej_bar.timestamp.date() != bar.timestamp.date():
                            break
                        if rej_bar.time_hhmm >= SESSION_END_HHMM:
                            break

                        rej_rng = rej_bar.high - rej_bar.low
                        rej_bearish = rej_bar.close < rej_bar.open
                        rej_closes_below = rej_bar.close < retest_low

                        if rej_bearish and rej_closes_below:
                            # Track highest since retest for stop
                            highest_since_retest = max(
                                bars[k].high for k in range(j, rj + 1))

                            rej_ctx = ctxs[rj]
                            stop = highest_since_retest + STOP_BUFFER_ATR * rej_ctx.i_atr
                            entry_price = rej_bar.close
                            risk = abs(entry_price - stop)
                            if risk <= 0:
                                continue

                            # ── Compute features ──
                            bd_range_atr = rng / ctx.i_atr
                            bd_vol_ratio = bar.volume / ctx.vol_ma if ctx.vol_ma > 0 else 1.0

                            # Retest features
                            retest_bar_count = rj - i - 1  # bars between BD and rejection
                            selloff_range = bar.open - bar.close  # BD bar range (directional)
                            if selloff_range > 0:
                                retest_depth_pct = (retest_high - bar.close) / selloff_range
                            else:
                                retest_depth_pct = 0.0

                            # Retest volume
                            retest_vol = sum(bars[k].volume for k in range(i + 1, rj))
                            bd_vol = bar.volume
                            retest_vol_ratio = retest_vol / bd_vol if bd_vol > 0 else 1.0

                            # Overlap (grindiness)
                            if retest_bar_count > 1:
                                overlap_count = 0
                                for oi in range(i + 2, rj):
                                    if bars[oi].low < bars[oi - 1].high:
                                        overlap_count += 1
                                retest_overlap = overlap_count / (retest_bar_count - 1)
                            else:
                                retest_overlap = 1.0

                            # Slope
                            if retest_bar_count > 0 and ctx.i_atr > 0:
                                retest_slope = (retest_high - bar.close) / (retest_bar_count * ctx.i_atr)
                            else:
                                retest_slope = 0.0

                            # Rejection bar anatomy
                            if rej_rng > 0:
                                upper_wick = rej_bar.high - max(rej_bar.open, rej_bar.close)
                                rej_wick_pct = upper_wick / rej_rng
                                body = abs(rej_bar.close - rej_bar.open)
                                rej_body_pct = body / rej_rng
                            else:
                                rej_wick_pct = 0.0
                                rej_body_pct = 0.0

                            # Distance from VWAP/EMA
                            dist_vwap = (entry_price - rej_ctx.vwap) / rej_ctx.i_atr if rej_ctx.vwap > 0 else 0.0
                            dist_ema9 = (entry_price - rej_ctx.ema9) / rej_ctx.i_atr if rej_ctx.ema9 > 0 else 0.0

                            # Market context
                            mkt = mkt_at.get(rj, {})

                            # Time bucket
                            rej_hhmm = rej_bar.timestamp.hour * 100 + rej_bar.timestamp.minute
                            if rej_hhmm < 1100:
                                tbucket = "AM"
                            elif rej_hhmm < 1330:
                                tbucket = "MID"
                            else:
                                tbucket = "LATE"

                            # Follow-through
                            ft_1b = 0.0
                            if rj + 1 < n:
                                next_bar = bars[rj + 1]
                                ft_1b = (entry_price - next_bar.close) / rej_ctx.i_atr

                            trade = BDRTrade(
                                sym=sym,
                                entry_idx=rj,
                                entry_price=entry_price,
                                entry_time=rej_bar.timestamp,
                                stop_price=stop,
                                risk=risk,
                                level_type=level_type,
                                level_price=level_price,
                                bd_idx=i,
                                bd_range_atr=bd_range_atr,
                                bd_vol_ratio=bd_vol_ratio,
                                retest_depth_pct=retest_depth_pct,
                                retest_bar_count=retest_bar_count,
                                retest_vol_ratio=retest_vol_ratio,
                                retest_overlap=retest_overlap,
                                retest_slope_atr=retest_slope,
                                rejection_wick_pct=rej_wick_pct,
                                rejection_body_pct=rej_body_pct,
                                rejection_bearish=rej_bearish,
                                dist_vwap_atr=dist_vwap,
                                dist_ema9_atr=dist_ema9,
                                market_trend=mkt.get("trend", 0),
                                rs_market=mkt.get("rs_mkt", 0.0),
                                rs_sector=mkt.get("rs_sec", 0.0),
                                time_bucket=tbucket,
                                follow_through_1b=ft_1b,
                            )
                            trades.append(trade)
                            cooldown_until = rj + TIME_STOP_BARS
                            break  # found rejection, stop looking
                    if cooldown_until >= i + 1:
                        break  # found a trade from this BD, move on
            # Only take the first level match per bar
            if cooldown_until >= i + 1:
                break

    return trades


def simulate_exit(trade: BDRTrade, bars: List[Bar], ctxs: List[BarContext]):
    """Simulate exit for a BDR short trade."""
    entry_idx = trade.entry_idx
    entry_date = bars[entry_idx].timestamp.date()
    cfg_slip = 0.02  # simplified slippage

    filled_entry = trade.entry_price - cfg_slip  # short: fill slightly worse

    for j in range(entry_idx + 1, len(bars)):
        b = bars[j]
        held = j - entry_idx

        if b.timestamp.date() != entry_date:
            prev = bars[j - 1]
            adj_exit = prev.close + cfg_slip
            trade.exit_price = prev.close
            trade.exit_time = prev.timestamp
            trade.exit_reason = "eod"
            trade.bars_held = held - 1
            trade.pnl_points = filled_entry - adj_exit
            trade.pnl_rr = trade.pnl_points / trade.risk if trade.risk > 0 else 0
            return

        if b.time_hhmm >= SESSION_END_HHMM:
            adj_exit = b.close + cfg_slip
            trade.exit_price = b.close
            trade.exit_time = b.timestamp
            trade.exit_reason = "eod"
            trade.bars_held = held
            trade.pnl_points = filled_entry - adj_exit
            trade.pnl_rr = trade.pnl_points / trade.risk if trade.risk > 0 else 0
            return

        if b.high >= trade.stop_price:
            adj_exit = trade.stop_price + cfg_slip
            trade.exit_price = trade.stop_price
            trade.exit_time = b.timestamp
            trade.exit_reason = "stop"
            trade.bars_held = held
            trade.pnl_points = filled_entry - adj_exit
            trade.pnl_rr = trade.pnl_points / trade.risk if trade.risk > 0 else 0
            return

        if held >= TIME_STOP_BARS:
            adj_exit = b.close + cfg_slip
            trade.exit_price = b.close
            trade.exit_time = b.timestamp
            trade.exit_reason = "time"
            trade.bars_held = held
            trade.pnl_points = filled_entry - adj_exit
            trade.pnl_rr = trade.pnl_points / trade.risk if trade.risk > 0 else 0
            return

    # End of data
    last = bars[-1]
    adj_exit = last.close + cfg_slip
    trade.exit_price = last.close
    trade.exit_time = last.timestamp
    trade.exit_reason = "eod"
    trade.bars_held = len(bars) - 1 - entry_idx
    trade.pnl_points = filled_entry - adj_exit
    trade.pnl_rr = trade.pnl_points / trade.risk if trade.risk > 0 else 0


# ══════════════════════════════════════════════════════════════
#  MARKET CONTEXT EXTRACTION (lightweight)
# ══════════════════════════════════════════════════════════════

def build_market_context_map(spy_bars: List[Bar], qqq_bars: List[Bar],
                              stock_bars: List[Bar], sector_bars: Optional[List[Bar]]) -> Dict[int, dict]:
    """
    Build a per-bar-index map of market context for the stock.
    Uses a simplified approach: SPY trend from EMA9 vs EMA20,
    RS from pct_from_open comparison.
    """
    mkt_map = {}

    # Build SPY EMA9/EMA20 and day opens
    spy_by_ts = {}
    spy_ema9 = 0.0
    spy_ema20 = 0.0
    spy_day_open = 0.0
    spy_day_date = None
    m9 = 2.0 / 10
    m20 = 2.0 / 21
    for sb in spy_bars:
        d = sb.timestamp.date()
        if d != spy_day_date:
            spy_day_date = d
            spy_day_open = sb.open
            spy_ema9 = sb.close
            spy_ema20 = sb.close
        else:
            spy_ema9 = sb.close * m9 + spy_ema9 * (1 - m9)
            spy_ema20 = sb.close * m20 + spy_ema20 * (1 - m20)
        spy_pct = (sb.close - spy_day_open) / spy_day_open * 100 if spy_day_open > 0 else 0
        spy_by_ts[sb.timestamp] = {
            "trend": 1 if spy_ema9 > spy_ema20 else (-1 if spy_ema9 < spy_ema20 else 0),
            "pct": spy_pct,
        }

    # Sector
    sec_by_ts = {}
    if sector_bars:
        sec_day_open = 0.0
        sec_day_date = None
        for sb in sector_bars:
            d = sb.timestamp.date()
            if d != sec_day_date:
                sec_day_date = d
                sec_day_open = sb.open
            sec_pct = (sb.close - sec_day_open) / sec_day_open * 100 if sec_day_open > 0 else 0
            sec_by_ts[sb.timestamp] = sec_pct

    # Map stock bar index to context
    stock_day_open = 0.0
    stock_day_date = None
    for i, bar in enumerate(stock_bars):
        d = bar.timestamp.date()
        if d != stock_day_date:
            stock_day_date = d
            stock_day_open = bar.open

        stock_pct = (bar.close - stock_day_open) / stock_day_open * 100 if stock_day_open > 0 else 0
        spy_info = spy_by_ts.get(bar.timestamp, {"trend": 0, "pct": 0.0})
        sec_pct = sec_by_ts.get(bar.timestamp, 0.0)

        mkt_map[i] = {
            "trend": spy_info["trend"],
            "rs_mkt": (stock_pct - spy_info["pct"]) / 100.0,
            "rs_sec": (stock_pct - sec_pct) / 100.0,
        }

    return mkt_map


# ══════════════════════════════════════════════════════════════
#  METRICS & REPORTING
# ══════════════════════════════════════════════════════════════

def compute_metrics(trades: List[BDRTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "exp": 0.0,
                "stop_rate": 0.0, "qstop_rate": 0.0, "avg_hold": 0.0, "max_dd": 0.0}
    wins = [t for t in trades if t.pnl_points > 0]
    losses = [t for t in trades if t.pnl_points <= 0]
    pnl = sum(t.pnl_points for t in trades)
    gw = sum(t.pnl_points for t in wins)
    gl = abs(sum(t.pnl_points for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    qstop = sum(1 for t in trades if t.exit_reason == "stop" and t.bars_held <= 3)
    cum = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl_points
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum
    return {"n": n, "wr": len(wins) / n * 100, "pf": pf, "pnl": pnl,
            "exp": sum(t.pnl_rr for t in trades) / n,
            "stop_rate": stopped / n * 100,
            "qstop_rate": qstop / n * 100,
            "avg_hold": statistics.mean(t.bars_held for t in trades),
            "max_dd": dd}


def print_metrics(label, m):
    print(f"  {label:40s}  N={m['n']:3d}  WR={m['wr']:5.1f}%  PF={pf_str(m['pf']):>6s}  "
          f"PnL={m['pnl']:+7.2f}  Exp={m['exp']:+.2f}R  Stop={m['stop_rate']:4.1f}%  "
          f"QStop={m['qstop_rate']:4.1f}%")


# Feature tests
FEATURE_TESTS = [
    ("big_bd", "bd_range_atr", ">=", 1.5, "Breakdown ≥ 1.5 ATR"),
    ("small_bd", "bd_range_atr", "<=", 1.0, "Breakdown ≤ 1.0 ATR"),
    ("shallow_retest", "retest_depth_pct", "<=", 0.5, "Retest ≤ 50% of BD"),
    ("deep_retest", "retest_depth_pct", ">", 1.0, "Retest > 100% of BD"),
    ("quick_retest", "retest_bar_count", "<=", 3, "Retest ≤ 3 bars"),
    ("slow_retest", "retest_bar_count", ">=", 6, "Retest ≥ 6 bars"),
    ("weak_vol_retest", "retest_vol_ratio", "<=", 0.8, "Retest vol ≤ 80% of BD vol"),
    ("strong_vol_retest", "retest_vol_ratio", ">", 1.5, "Retest vol > 150% of BD vol"),
    ("grindy_retest", "retest_overlap", ">=", 0.8, "Retest ≥ 80% overlapping"),
    ("impulsive_retest", "retest_overlap", "<=", 0.3, "Retest ≤ 30% overlapping"),
    ("big_rej_wick", "rejection_wick_pct", ">=", 0.30, "Rejection wick ≥ 30%"),
    ("small_rej_wick", "rejection_wick_pct", "<=", 0.10, "Rejection wick ≤ 10%"),
    ("rej_bearish", "rejection_bearish", "==", True, "Rejection bar bearish"),
    ("below_vwap", "dist_vwap_atr", "<=", -0.3, "Entry ≥ 0.3 ATR below VWAP"),
    ("near_vwap", "dist_vwap_atr", ">", -0.3, "Entry near or above VWAP"),
    ("below_ema9", "dist_ema9_atr", "<=", -0.3, "Entry ≥ 0.3 ATR below EMA9"),
    ("mkt_bearish", "market_trend", "<=", 0, "Market trend ≤ 0"),
    ("mkt_bullish", "market_trend", "==", 1, "Market trend = bullish"),
    ("rs_weak", "rs_market", "<=", -0.003, "RS vs SPY ≤ -0.3%"),
    ("am_entry", "time_bucket", "==", "AM", "Entry before 11:00"),
    ("mid_entry", "time_bucket", "==", "MID", "Entry 11:00-13:30"),
    ("late_entry", "time_bucket", "==", "LATE", "Entry after 13:30"),
    ("orl_level", "level_type", "==", "ORL", "Breakdown of OR low"),
    ("vwap_level", "level_type", "==", "VWAP", "Breakdown of VWAP"),
    ("swing_level", "level_type", "==", "SWING", "Breakdown of swing low"),
    ("good_ft", "follow_through_1b", ">=", 0.3, "1-bar follow-through ≥ 0.3 ATR"),
]


def test_feature(trades, field, op, threshold):
    p, f = [], []
    for t in trades:
        val = getattr(t, field, None)
        if val is None:
            f.append(t)
            continue
        if op == "<=":
            passed = val <= threshold
        elif op == ">=":
            passed = val >= threshold
        elif op == ">":
            passed = val > threshold
        elif op == "==":
            passed = val == threshold
        else:
            passed = False
        (p if passed else f).append(t)
    return p, f


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="all94")
    args = parser.parse_args()

    from .market_context import SECTOR_MAP, get_sector_etf

    if args.universe == "all94":
        excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
        symbols = sorted([
            p.stem.replace("_5min", "")
            for p in DATA_DIR.glob("*_5min.csv")
            if p.stem.replace("_5min", "") not in excluded
        ])
    elif args.universe == "watchlist":
        symbols = load_watchlist()
    else:
        symbols = [s.strip().upper() for s in args.universe.split(",")]

    print("=" * 95)
    print("BREAKDOWN-RETEST SHORT — FEATURE STUDY")
    print("=" * 95)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Pattern: Break of support → weak retest → rejection → short entry")
    print(f"Levels: OR low, VWAP, swing low")
    print(f"Stop: retest high + {STOP_BUFFER_ATR} ATR  |  Time stop: {TIME_STOP_BARS} bars\n")

    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    all_trades: List[BDRTrade] = []

    for sym in symbols:
        fpath = DATA_DIR / f"{sym}_5min.csv"
        if not fpath.exists():
            continue
        bars = load_bars_from_csv(str(fpath))
        if not bars or len(bars) < 30:
            continue

        ctxs = compute_bar_contexts(bars)
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        mkt_map = build_market_context_map(spy_bars, qqq_bars, bars, sec_bars)

        candidates = scan_for_breakdowns(bars, ctxs, mkt_map, sym)
        for t in candidates:
            simulate_exit(t, bars, ctxs)
        all_trades.extend(candidates)

    print(f"Total BDR short candidates: {len(all_trades)}\n")
    if not all_trades:
        print("NO TRADES FOUND.")
        return

    winners = [t for t in all_trades if t.pnl_points > 0]
    losers = [t for t in all_trades if t.pnl_points <= 0]

    # ══════════════════════════════════════════════════════════
    # SECTION 1: BASELINE PERFORMANCE
    # ══════════════════════════════════════════════════════════
    print("=" * 95)
    print("SECTION 1: BASELINE PERFORMANCE")
    print("=" * 95)

    m = compute_metrics(all_trades)
    print_metrics("All BDR shorts", m)
    print(f"  Winners: {len(winners)}  Losers: {len(losers)}")

    # By level type
    print(f"\n  By level type:")
    for lt in ["ORL", "VWAP", "SWING"]:
        subset = [t for t in all_trades if t.level_type == lt]
        if subset:
            ml = compute_metrics(subset)
            print_metrics(f"  {lt}", ml)

    # By exit reason
    print(f"\n  By exit reason:")
    for reason in ["stop", "time", "eod"]:
        subset = [t for t in all_trades if t.exit_reason == reason]
        if subset:
            pnl = sum(t.pnl_points for t in subset)
            print(f"    {reason:8s}  N={len(subset):3d}  PnL={pnl:+.2f}")

    # ══════════════════════════════════════════════════════════
    # SECTION 2: WINNER vs LOSER ANATOMY
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 95)
    print("SECTION 2: WINNER vs LOSER ANATOMY")
    print("=" * 95)

    features = [
        ("bd_range_atr", "BD range (ATR)"),
        ("retest_depth_pct", "Retest depth (% of BD)"),
        ("retest_bar_count", "Retest bar count"),
        ("retest_vol_ratio", "Retest vol / BD vol"),
        ("retest_overlap", "Retest overlap"),
        ("retest_slope_atr", "Retest slope (ATR/bar)"),
        ("rejection_wick_pct", "Rejection wick %"),
        ("rejection_body_pct", "Rejection body %"),
        ("dist_vwap_atr", "Dist from VWAP (ATR)"),
        ("dist_ema9_atr", "Dist from EMA9 (ATR)"),
        ("market_trend", "Market trend"),
        ("rs_market", "RS vs SPY"),
        ("rs_sector", "RS vs sector"),
        ("follow_through_1b", "Follow-through 1b"),
    ]

    print(f"\n  {'Feature':30s}  {'Winners':>10s}  {'Losers':>10s}  {'Delta':>10s}")
    print(f"  {'-'*30}  {'-'*10}  {'-'*10}  {'-'*10}")

    for field, label in features:
        w_vals = [getattr(t, field) for t in winners if isinstance(getattr(t, field), (int, float))]
        l_vals = [getattr(t, field) for t in losers if isinstance(getattr(t, field), (int, float))]
        w_mean = statistics.mean(w_vals) if w_vals else 0.0
        l_mean = statistics.mean(l_vals) if l_vals else 0.0
        print(f"  {label:30s}  {w_mean:10.3f}  {l_mean:10.3f}  {w_mean - l_mean:+10.3f}")

    # ══════════════════════════════════════════════════════════
    # SECTION 3: FEATURE PASS/FAIL TESTS
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 95)
    print("SECTION 3: FEATURE PASS/FAIL TESTS")
    print("=" * 95)

    print(f"\n  {'Feature':25s}  {'Pass':>5s}  {'P_WR':>6s}  {'P_PF':>6s}  {'P_PnL':>8s}  "
          f"{'Fail':>5s}  {'F_WR':>6s}  {'F_PF':>6s}  {'F_PnL':>8s}  {'ΔWR':>7s}")
    print(f"  {'-'*25}  {'-'*5}  {'-'*6}  {'-'*6}  {'-'*8}  "
          f"{'-'*5}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*7}")

    feat_results = []
    for name, field, op, thresh, desc in FEATURE_TESTS:
        p, f = test_feature(all_trades, field, op, thresh)
        mp = compute_metrics(p)
        mf = compute_metrics(f)
        dwr = mp["wr"] - mf["wr"] if mf["n"] > 0 else 0.0

        print(f"  {name:25s}  {mp['n']:5d}  {mp['wr']:5.1f}%  {pf_str(mp['pf']):>6s}  {mp['pnl']:+8.2f}  "
              f"{mf['n']:5d}  {mf['wr']:5.1f}%  {pf_str(mf['pf']):>6s}  {mf['pnl']:+8.2f}  {dwr:+6.1f}%")

        feat_results.append({
            "name": name, "desc": desc,
            "pass_n": mp["n"], "pass_wr": mp["wr"], "pass_pf": mp["pf"], "pass_pnl": mp["pnl"],
            "fail_n": mf["n"], "fail_wr": mf["wr"], "fail_pf": mf["pf"], "fail_pnl": mf["pnl"],
            "delta_wr": dwr,
        })

    # ══════════════════════════════════════════════════════════
    # SECTION 4: TOP DISCRIMINATORS
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 95)
    print("SECTION 4: TOP DISCRIMINATORS (min 5 per subset)")
    print("=" * 95)

    valid = [r for r in feat_results if r["pass_n"] >= 5 and r["fail_n"] >= 5]
    valid.sort(key=lambda r: abs(r["delta_wr"]), reverse=True)

    print(f"\n  {'Rk':>3s}  {'Feature':25s}  {'ΔWR':>7s}  {'Pass':>5s} {'P_WR':>6s} {'P_PF':>6s}  "
          f"{'Fail':>5s} {'F_WR':>6s} {'F_PF':>6s}  {'Description'}")
    print(f"  {'-'*3}  {'-'*25}  {'-'*7}  {'-'*5} {'-'*6} {'-'*6}  "
          f"{'-'*5} {'-'*6} {'-'*6}  {'-'*40}")

    for rank, r in enumerate(valid[:12], 1):
        print(f"  {rank:3d}  {r['name']:25s}  {r['delta_wr']:+6.1f}%  "
              f"{r['pass_n']:5d} {r['pass_wr']:5.1f}% {pf_str(r['pass_pf']):>6s}  "
              f"{r['fail_n']:5d} {r['fail_wr']:5.1f}% {pf_str(r['fail_pf']):>6s}  "
              f"{r['desc']}")

    # ══════════════════════════════════════════════════════════
    # SECTION 5: TRADE LOG (first 50)
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 95)
    print("SECTION 5: TRADE LOG (chronological, first 60)")
    print("=" * 95)

    sorted_trades = sorted(all_trades, key=lambda t: t.entry_time)
    print(f"\n  {'Sym':>6s}  {'Date':>10s}  {'Time':>5s}  {'PnL':>7s}  {'Exit':>5s}  {'Held':>4s}  "
          f"{'Level':>5s}  {'BD_ATR':>6s}  {'RtDp':>5s}  {'RtBr':>4s}  {'RtVR':>5s}  "
          f"{'RejWk':>5s}  {'DstVW':>6s}  {'MkTr':>4s}  {'RS_M':>6s}  {'FT1b':>5s}")
    print(f"  {'-'*6}  {'-'*10}  {'-'*5}  {'-'*7}  {'-'*5}  {'-'*4}  "
          f"{'-'*5}  {'-'*6}  {'-'*5}  {'-'*4}  {'-'*5}  "
          f"{'-'*5}  {'-'*6}  {'-'*4}  {'-'*6}  {'-'*5}")

    for t in sorted_trades[:60]:
        w = "W" if t.pnl_points > 0 else "L"
        print(f"  {t.sym:>6s}  {t.entry_time.strftime('%Y-%m-%d'):>10s}  "
              f"{t.entry_time.strftime('%H:%M'):>5s}  {t.pnl_points:+7.2f}  "
              f"{t.exit_reason:>5s}  {t.bars_held:4d}  "
              f"{t.level_type:>5s}  {t.bd_range_atr:6.2f}  "
              f"{t.retest_depth_pct:5.2f}  {t.retest_bar_count:4d}  "
              f"{t.retest_vol_ratio:5.2f}  {t.rejection_wick_pct:5.2f}  "
              f"{t.dist_vwap_atr:+6.2f}  {t.market_trend:+4d}  "
              f"{t.rs_market:+6.3f}  {t.follow_through_1b:+5.2f}  {w}")

    # ══════════════════════════════════════════════════════════
    # SECTION 6: VERDICT
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 95)
    print("SECTION 6: VERDICT")
    print("=" * 95)

    m = compute_metrics(all_trades)
    print(f"\n  Overall: N={m['n']}  WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  PnL={m['pnl']:+.2f}  "
          f"Stop={m['stop_rate']:.1f}%  QStop={m['qstop_rate']:.1f}%")

    # Viable features
    viable = [r for r in feat_results if r["pass_pf"] >= 1.0 and r["pass_n"] >= 10]
    if viable:
        print(f"\n  {len(viable)} feature(s) produce PF >= 1.0 on N >= 10:")
        for r in sorted(viable, key=lambda r: r["pass_pf"], reverse=True)[:5]:
            print(f"    {r['name']:25s}  N={r['pass_n']:3d}  WR={r['pass_wr']:5.1f}%  "
                  f"PF={pf_str(r['pass_pf'])}  PnL={r['pass_pnl']:+.2f}  ({r['desc']})")
    else:
        print(f"\n  No individual feature produces PF >= 1.0 on N >= 10.")

    if m["pf"] >= 0.8:
        print(f"\n  BDR shows promising baseline (PF={pf_str(m['pf'])}).")
        print(f"  Worth refining with top 1-2 feature filters.")
    elif viable:
        print(f"\n  BDR baseline is weak but viable subsets exist.")
        print(f"  Next step: test top feature as a single filter gate.")
    else:
        print(f"\n  BDR does not show a viable subset. Consider alternative short structures.")


if __name__ == "__main__":
    main()
