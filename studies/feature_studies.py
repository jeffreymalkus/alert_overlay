"""
Feature studies on the corrected baseline (VK + SC_VOL, day-boundary fixed).

Four studies:
  1. Regime breakdown — SPY day type vs trade outcomes
  2. Candle anatomy — signal bar shape vs outcomes
  3. EMA feature study — EMA9/EMA20 positioning vs outcomes
  4. VWAP_KISS feature study — VWAP distance, OR context, confluence

All evaluated using the corrected 5-min backtest (day-boundary fix).
Uses full 94-symbol universe.

Usage:
    python -m alert_overlay.feature_studies --universe all94
"""

import argparse
import math
import statistics
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path
from typing import List, Optional, Dict, Tuple
from zoneinfo import ZoneInfo

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import (
    Bar, Signal, NaN, SetupId, SetupFamily,
    SETUP_FAMILY_MAP, SETUP_DISPLAY_NAME,
)

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent.parent / "data"
WATCHLIST_FILE = Path(__file__).parent.parent / "watchlist.txt"


# ── helpers ──────────────────────────────────────────────────────────

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


def compute_metrics(trades):
    """Compute standard metrics for a list of Trade objects."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "exp": 0.0,
                "stop_rate": 0.0, "max_dd": 0.0, "avg_hold": 0.0}
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
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum
    avg_hold = statistics.mean(t.bars_held for t in trades) if trades else 0
    return {"n": n, "wr": len(wins) / n * 100, "pf": pf, "pnl": pnl,
            "exp": avg_rr, "stop_rate": stopped / n * 100, "max_dd": dd,
            "avg_hold": avg_hold}


def print_group_table(groups: Dict[str, List[Trade]], title: str, sort_by_pnl=False):
    """Print metrics for each group in a table."""
    print(f"\n  {title}")
    print(f"  {'Group':<25} │ {'N':>4} {'WR%':>5} {'PF':>5} {'Exp':>7} "
          f"{'PnL':>8} {'StpR':>5} {'AvgH':>5}")
    print(f"  {'─' * 25}─┼{'─' * 45}")

    items = list(groups.items())
    if sort_by_pnl:
        items.sort(key=lambda x: -sum(t.pnl_points for t in x[1]))

    for name, trades in items:
        m = compute_metrics(trades)
        if m["n"] == 0:
            continue
        print(f"  {name:<25} │ {m['n']:>4} {m['wr']:>5.1f} {pf_str(m['pf']):>5} "
              f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['stop_rate']:>5.0f}% {m['avg_hold']:>5.1f}")


# ── SPY day classification ──────────────────────────────────────────

def classify_spy_days(spy_bars):
    daily = defaultdict(list)
    for b in spy_bars:
        daily[b.timestamp.date()].append(b)

    sorted_dates = sorted(daily.keys())
    day_info = {}
    ranges_10d = []

    for d in sorted_dates:
        bars = daily[d]
        day_open = bars[0].open
        day_close = bars[-1].close
        day_high = max(b.high for b in bars)
        day_low = min(b.low for b in bars)
        day_range = day_high - day_low

        change_pct = (day_close - day_open) / day_open * 100 if day_open > 0 else 0
        if change_pct > 0.05:
            direction = "GREEN"
        elif change_pct < -0.05:
            direction = "RED"
        else:
            direction = "FLAT"

        avg_range_10d = statistics.mean(ranges_10d[-10:]) if ranges_10d else day_range
        volatility = "HIGH_VOL" if day_range > 1.5 * avg_range_10d else "NORMAL"

        if day_range > 0:
            close_pos = (day_close - day_low) / day_range
            character = "TREND" if (close_pos > 0.7 or close_pos < 0.3) else "CHOPPY"
        else:
            character = "FLAT"

        ranges_10d.append(day_range)
        day_info[d] = {
            "direction": direction,
            "volatility": volatility,
            "character": character,
            "spy_change_pct": change_pct,
        }

    return day_info


# ── Candle anatomy features at signal bar ────────────────────────────

def candle_features(bars: List[Bar], sig: Signal) -> dict:
    """Compute candle anatomy features at the signal bar."""
    # Find signal bar
    bar_idx = None
    for i, b in enumerate(bars):
        if b.timestamp == sig.timestamp:
            bar_idx = i
            break
    if bar_idx is None:
        return {}

    bar = bars[bar_idx]
    body = bar.close - bar.open
    body_abs = abs(body)
    full_range = bar.high - bar.low
    if full_range <= 0:
        return {}

    # Body ratio: how much of the bar is body vs wick
    body_ratio = body_abs / full_range

    # Upper wick ratio
    upper_wick = bar.high - max(bar.open, bar.close)
    lower_wick = min(bar.open, bar.close) - bar.low
    upper_wick_ratio = upper_wick / full_range
    lower_wick_ratio = lower_wick / full_range

    # Bar direction
    bar_bullish = bar.close > bar.open

    # Volume relative to recent (20-bar lookback)
    recent = bars[max(0, bar_idx - 20):bar_idx]
    avg_vol = statistics.mean(b.volume for b in recent) if recent else bar.volume
    rvol = bar.volume / avg_vol if avg_vol > 0 else 1.0

    # ATR context: bar range vs recent ATR
    recent_ranges = [b.high - b.low for b in recent]
    avg_range = statistics.mean(recent_ranges) if recent_ranges else full_range
    range_vs_atr = full_range / avg_range if avg_range > 0 else 1.0

    # Close position within bar (0=low, 1=high)
    close_position = (bar.close - bar.low) / full_range

    # Bar-to-bar momentum: close vs prior close
    prior = bars[bar_idx - 1] if bar_idx > 0 else bar
    momentum = (bar.close - prior.close) / prior.close * 100 if prior.close > 0 else 0

    return {
        "body_ratio": body_ratio,
        "upper_wick_ratio": upper_wick_ratio,
        "lower_wick_ratio": lower_wick_ratio,
        "bar_bullish": bar_bullish,
        "rvol": rvol,
        "range_vs_atr": range_vs_atr,
        "close_position": close_position,
        "momentum_pct": momentum,
        "full_range": full_range,
    }


# ── EMA features at signal bar ──────────────────────────────────────

def ema_features(bars: List[Bar], sig: Signal) -> dict:
    """Compute EMA positioning features at signal bar."""
    bar_idx = None
    for i, b in enumerate(bars):
        if b.timestamp == sig.timestamp:
            bar_idx = i
            break
    if bar_idx is None:
        return {}

    bar = bars[bar_idx]
    e9 = bar._e9
    e20 = bar._e20
    vwap = bar._vwap
    price = bar.close

    if e9 <= 0 or e20 <= 0:
        return {}

    # Distance from EMAs (as % of price)
    dist_e9_pct = (price - e9) / price * 100
    dist_e20_pct = (price - e20) / price * 100

    # EMA spread: e9 vs e20 (positive = bullish stack)
    ema_spread_pct = (e9 - e20) / e20 * 100

    # Price vs VWAP
    dist_vwap_pct = (price - vwap) / price * 100 if vwap > 0 else 0.0

    # EMA slope (using 3-bar lookback if available)
    if bar_idx >= 3:
        e9_3ago = bars[bar_idx - 3]._e9
        e20_3ago = bars[bar_idx - 3]._e20
        e9_slope = (e9 - e9_3ago) / e9_3ago * 100 if e9_3ago > 0 else 0
        e20_slope = (e20 - e20_3ago) / e20_3ago * 100 if e20_3ago > 0 else 0
    else:
        e9_slope = 0.0
        e20_slope = 0.0

    # EMA stack: bullish = price > e9 > e20, bearish = reverse
    if price > e9 > e20:
        ema_stack = "BULL"
    elif price < e9 < e20:
        ema_stack = "BEAR"
    elif e9 > e20:
        ema_stack = "MIXED_BULL"
    else:
        ema_stack = "MIXED_BEAR"

    return {
        "dist_e9_pct": dist_e9_pct,
        "dist_e20_pct": dist_e20_pct,
        "ema_spread_pct": ema_spread_pct,
        "dist_vwap_pct": dist_vwap_pct,
        "e9_slope": e9_slope,
        "e20_slope": e20_slope,
        "ema_stack": ema_stack,
    }


# ── VWAP KISS specific features ─────────────────────────────────────

def vk_features(bars: List[Bar], sig: Signal) -> dict:
    """Compute VWAP KISS specific features at signal bar."""
    bar_idx = None
    for i, b in enumerate(bars):
        if b.timestamp == sig.timestamp:
            bar_idx = i
            break
    if bar_idx is None:
        return {}

    bar = bars[bar_idx]
    vwap = bar._vwap
    price = bar.close
    e9 = bar._e9

    if vwap <= 0:
        return {}

    # Distance from VWAP at signal (as % of price)
    vwap_dist_pct = abs(price - vwap) / price * 100

    # VWAP touch quality: how close did the bar get to VWAP?
    # For longs: low should touch/near VWAP. For shorts: high.
    if sig.direction == 1:
        touch_dist = abs(bar.low - vwap) / price * 100
    else:
        touch_dist = abs(bar.high - vwap) / price * 100

    # Time of day (bars from open)
    time_hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute
    minutes_from_open = (bar.timestamp.hour - 9) * 60 + bar.timestamp.minute - 30
    if minutes_from_open < 0:
        minutes_from_open = 0

    # Time bucket
    if minutes_from_open <= 30:
        time_bucket = "FIRST_30"
    elif minutes_from_open <= 60:
        time_bucket = "30-60"
    elif minutes_from_open <= 120:
        time_bucket = "60-120"
    elif minutes_from_open <= 240:
        time_bucket = "120-240"
    else:
        time_bucket = "LATE"

    # Quality score
    quality = sig.quality_score

    # Confluence tags
    conf_tags = sig.confluence_tags

    # RR ratio
    rr = sig.rr_ratio

    # Market trend at signal
    mkt_trend = sig.market_trend

    return {
        "vwap_dist_pct": vwap_dist_pct,
        "touch_dist_pct": touch_dist,
        "time_bucket": time_bucket,
        "minutes_from_open": minutes_from_open,
        "quality": quality,
        "confluence_count": len(conf_tags),
        "confluence_tags": conf_tags,
        "rr": rr,
        "market_trend": mkt_trend,
        "or_direction": sig.or_direction,
        "vwap_bias": sig.vwap_bias,
    }


# ── Feature bucketing ───────────────────────────────────────────────

def bucket_numeric(value, thresholds, labels):
    """Bucket a numeric value into labels based on thresholds."""
    for i, thresh in enumerate(thresholds):
        if value <= thresh:
            return labels[i]
    return labels[-1]


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Feature studies on corrected baseline")
    parser.add_argument("--universe", default="all94")
    args = parser.parse_args()

    # Load data
    if args.universe == "all94":
        symbols = load_watchlist()
    else:
        symbols = args.universe.split(",")

    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    from .market_context import SECTOR_MAP, get_sector_etf
    sector_bars_dict = {}
    sector_etfs = set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    print(f"SPY bars: {len(spy_bars)}")

    # Config: frozen baseline
    cfg = OverlayConfig()
    cfg.show_ema_scalp = False
    cfg.show_failed_bounce = False
    cfg.show_spencer = False
    cfg.show_ema_fpip = False
    cfg.show_sc_v2 = False

    # Run backtest and collect trades with their bars
    all_trades = []    # (sym, Trade, bars_5min)
    all_bars = {}

    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue

        sec_bars = None
        se = get_sector_etf(sym)
        if se and se in sector_bars_dict:
            sec_bars = sector_bars_dict[se]

        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        for t in result.trades:
            all_trades.append((sym, t, bars))
        all_bars[sym] = bars

    all_dates = sorted(set(
        b.timestamp.date() for bars_list in all_bars.values() for b in bars_list
    ))
    n_days = len(all_dates)
    just_trades = [t for _, t, _ in all_trades]

    m_all = compute_metrics(just_trades)
    print(f"\nBaseline: N={m_all['n']}  WR={m_all['wr']:.1f}%  PF={pf_str(m_all['pf'])}  "
          f"PnL={m_all['pnl']:+.2f}  Exp={m_all['exp']:+.3f}R  MaxDD={m_all['max_dd']:.1f}")
    print(f"Period: {min(all_dates)} → {max(all_dates)} ({n_days} days)")
    print(f"Symbols: {len(all_bars)}  |  Trades: {len(all_trades)}")

    # SPY day classification
    spy_day_info = classify_spy_days(spy_bars)

    # ══════════════════════════════════════════════════════════════
    #  STUDY 1: REGIME BREAKDOWN
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  STUDY 1: REGIME BREAKDOWN (SPY day classification)")
    print(f"{'=' * 100}")

    # By direction
    groups_dir = defaultdict(list)
    for sym, t, bars in all_trades:
        d = t.signal.timestamp.date()
        info = spy_day_info.get(d, {"direction": "UNK"})
        groups_dir[info["direction"]].append(t)
    print_group_table(groups_dir, "BY SPY DIRECTION")

    # By volatility
    groups_vol = defaultdict(list)
    for sym, t, bars in all_trades:
        d = t.signal.timestamp.date()
        info = spy_day_info.get(d, {"volatility": "UNK"})
        groups_vol[info["volatility"]].append(t)
    print_group_table(groups_vol, "BY SPY VOLATILITY")

    # By character
    groups_char = defaultdict(list)
    for sym, t, bars in all_trades:
        d = t.signal.timestamp.date()
        info = spy_day_info.get(d, {"character": "UNK"})
        groups_char[info["character"]].append(t)
    print_group_table(groups_char, "BY SPY CHARACTER")

    # Composites
    groups_comp = defaultdict(list)
    for sym, t, bars in all_trades:
        d = t.signal.timestamp.date()
        info = spy_day_info.get(d, {"direction": "UNK", "character": "UNK"})
        key = f"{info['direction']}+{info['character']}"
        groups_comp[key].append(t)
    print_group_table(groups_comp, "BY SPY DIRECTION × CHARACTER")

    # By setup × direction
    groups_setup_dir = defaultdict(list)
    for sym, t, bars in all_trades:
        d = t.signal.timestamp.date()
        info = spy_day_info.get(d, {"direction": "UNK"})
        setup = SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id))
        key = f"{setup} / {info['direction']}"
        groups_setup_dir[key].append(t)
    print_group_table(groups_setup_dir, "BY SETUP × SPY DIRECTION", sort_by_pnl=True)

    # By quality score
    groups_q = defaultdict(list)
    for sym, t, bars in all_trades:
        q = t.signal.quality_score
        groups_q[f"Q={q}"].append(t)
    print_group_table(groups_q, "BY QUALITY SCORE")

    # ══════════════════════════════════════════════════════════════
    #  STUDY 2: CANDLE ANATOMY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  STUDY 2: CANDLE ANATOMY (signal bar shape vs outcome)")
    print(f"{'=' * 100}")

    # Compute candle features for all trades
    candle_data = []
    for sym, t, bars in all_trades:
        cf = candle_features(bars, t.signal)
        if cf:
            candle_data.append((t, cf))

    if candle_data:
        # By body ratio
        groups_body = defaultdict(list)
        for t, cf in candle_data:
            label = bucket_numeric(cf["body_ratio"],
                                   [0.3, 0.5, 0.7],
                                   ["DOJI (<30%)", "SMALL (30-50%)", "MEDIUM (50-70%)", "FULL (>70%)"])
            groups_body[label].append(t)
        print_group_table(groups_body, "BY SIGNAL BAR BODY RATIO (body/range)")

        # By close position
        groups_cp = defaultdict(list)
        for t, cf in candle_data:
            label = bucket_numeric(cf["close_position"],
                                   [0.3, 0.5, 0.7],
                                   ["NEAR LOW (0-30%)", "LOWER MID (30-50%)", "UPPER MID (50-70%)", "NEAR HIGH (70-100%)"])
            groups_cp[label].append(t)
        print_group_table(groups_cp, "BY SIGNAL BAR CLOSE POSITION (0=low, 1=high)")

        # By RVOL
        groups_rvol = defaultdict(list)
        for t, cf in candle_data:
            label = bucket_numeric(cf["rvol"],
                                   [0.5, 1.0, 1.5, 2.5],
                                   ["LOW (<0.5x)", "BELOW AVG (0.5-1x)", "NORMAL (1-1.5x)",
                                    "STRONG (1.5-2.5x)", "SURGE (>2.5x)"])
            groups_rvol[label].append(t)
        print_group_table(groups_rvol, "BY SIGNAL BAR RELATIVE VOLUME")

        # By range vs ATR
        groups_rng = defaultdict(list)
        for t, cf in candle_data:
            label = bucket_numeric(cf["range_vs_atr"],
                                   [0.5, 0.8, 1.2, 2.0],
                                   ["SMALL (<0.5x ATR)", "BELOW AVG (0.5-0.8x)", "NORMAL (0.8-1.2x)",
                                    "WIDE (1.2-2x)", "EXTREME (>2x)"])
            groups_rng[label].append(t)
        print_group_table(groups_rng, "BY SIGNAL BAR RANGE vs ATR")

        # By bar direction match (for longs: bullish bar = aligned)
        groups_align = defaultdict(list)
        for t, cf in candle_data:
            bullish = cf["bar_bullish"]
            is_long = t.signal.direction == 1
            aligned = (is_long and bullish) or (not is_long and not bullish)
            label = "BAR ALIGNED" if aligned else "BAR OPPOSED"
            groups_align[label].append(t)
        print_group_table(groups_align, "BY SIGNAL BAR DIRECTION vs TRADE DIRECTION")

        # By upper wick (for longs, high upper wick = rejection)
        groups_uw = defaultdict(list)
        for t, cf in candle_data:
            if t.signal.direction == 1:
                # For longs: high upper wick = potential rejection overhead
                label = bucket_numeric(cf["upper_wick_ratio"],
                                       [0.15, 0.30],
                                       ["SMALL UPPER WICK (<15%)", "MODERATE UPPER (15-30%)", "HEAVY UPPER (>30%)"])
            else:
                label = bucket_numeric(cf["lower_wick_ratio"],
                                       [0.15, 0.30],
                                       ["SMALL LOWER WICK (<15%)", "MODERATE LOWER (15-30%)", "HEAVY LOWER (>30%)"])
            groups_uw[label].append(t)
        print_group_table(groups_uw, "BY ADVERSE WICK SIZE (rejection overhead for longs)")

    # ══════════════════════════════════════════════════════════════
    #  STUDY 3: EMA FEATURES
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  STUDY 3: EMA POSITIONING (at signal bar)")
    print(f"{'=' * 100}")

    ema_data = []
    for sym, t, bars in all_trades:
        ef = ema_features(bars, t.signal)
        if ef:
            ema_data.append((t, ef))

    if ema_data:
        # By EMA stack
        groups_stack = defaultdict(list)
        for t, ef in ema_data:
            groups_stack[ef["ema_stack"]].append(t)
        print_group_table(groups_stack, "BY EMA STACK (price vs EMA9 vs EMA20)")

        # By distance from EMA9
        groups_d9 = defaultdict(list)
        for t, ef in ema_data:
            # For longs: positive dist = above EMA9
            dist = ef["dist_e9_pct"] * t.signal.direction  # normalize: positive = favorable
            label = bucket_numeric(dist,
                                   [-0.5, 0.0, 0.3, 0.8],
                                   ["FAR BELOW E9 (<-0.5%)", "NEAR BELOW E9 (-0.5-0%)",
                                    "NEAR ABOVE E9 (0-0.3%)", "ABOVE E9 (0.3-0.8%)",
                                    "WELL ABOVE E9 (>0.8%)"])
            groups_d9[label].append(t)
        print_group_table(groups_d9, "BY DISTANCE FROM EMA9 (normalized by direction)")

        # By EMA spread
        groups_spread = defaultdict(list)
        for t, ef in ema_data:
            spread = ef["ema_spread_pct"] * t.signal.direction  # positive = favorable
            label = bucket_numeric(spread,
                                   [-0.3, 0.0, 0.3],
                                   ["BEARISH SPREAD (<-0.3%)", "FLAT SPREAD (-0.3-0%)",
                                    "MILD BULL SPREAD (0-0.3%)", "BULLISH SPREAD (>0.3%)"])
            groups_spread[label].append(t)
        print_group_table(groups_spread, "BY EMA9/EMA20 SPREAD (normalized by direction)")

        # By EMA9 slope
        groups_slope = defaultdict(list)
        for t, ef in ema_data:
            slope = ef["e9_slope"] * t.signal.direction  # positive = favorable
            label = bucket_numeric(slope,
                                   [-0.1, 0.0, 0.1, 0.3],
                                   ["DECLINING E9 (<-0.1%)", "FLAT DOWN (-0.1-0%)",
                                    "FLAT UP (0-0.1%)", "RISING E9 (0.1-0.3%)",
                                    "STEEP RISE (>0.3%)"])
            groups_slope[label].append(t)
        print_group_table(groups_slope, "BY EMA9 SLOPE (3-bar, normalized by direction)")

    # ══════════════════════════════════════════════════════════════
    #  STUDY 4: VWAP_KISS FEATURES
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  STUDY 4: VWAP_KISS SPECIFIC FEATURES")
    print(f"{'=' * 100}")

    vk_trades = [(sym, t, bars) for sym, t, bars in all_trades
                 if t.signal.setup_id == SetupId.VWAP_KISS]

    if vk_trades:
        vk_data = []
        for sym, t, bars in vk_trades:
            vf = vk_features(bars, t.signal)
            if vf:
                vk_data.append((t, vf))

        if vk_data:
            # By time of day
            groups_time = defaultdict(list)
            for t, vf in vk_data:
                groups_time[vf["time_bucket"]].append(t)
            print_group_table(groups_time, "VK: BY TIME OF DAY")

            # By quality
            groups_vk_q = defaultdict(list)
            for t, vf in vk_data:
                groups_vk_q[f"Q={vf['quality']}"].append(t)
            print_group_table(groups_vk_q, "VK: BY QUALITY SCORE")

            # By VWAP distance
            groups_vwap = defaultdict(list)
            for t, vf in vk_data:
                label = bucket_numeric(vf["vwap_dist_pct"],
                                       [0.1, 0.3, 0.6],
                                       ["TIGHT (<0.1%)", "CLOSE (0.1-0.3%)",
                                        "MODERATE (0.3-0.6%)", "WIDE (>0.6%)"])
                groups_vwap[label].append(t)
            print_group_table(groups_vwap, "VK: BY VWAP DISTANCE AT SIGNAL")

            # By VWAP touch quality
            groups_touch = defaultdict(list)
            for t, vf in vk_data:
                label = bucket_numeric(vf["touch_dist_pct"],
                                       [0.05, 0.15, 0.3],
                                       ["PRECISE TOUCH (<0.05%)", "CLOSE TOUCH (0.05-0.15%)",
                                        "NEAR TOUCH (0.15-0.3%)", "LOOSE TOUCH (>0.3%)"])
                groups_touch[label].append(t)
            print_group_table(groups_touch, "VK: BY VWAP TOUCH PRECISION (bar low/high to VWAP)")

            # By confluence count
            groups_conf = defaultdict(list)
            for t, vf in vk_data:
                cc = vf["confluence_count"]
                groups_conf[f"CONF={cc}"].append(t)
            print_group_table(groups_conf, "VK: BY CONFLUENCE COUNT")

            # By OR direction
            groups_or = defaultdict(list)
            for t, vf in vk_data:
                groups_or[f"OR={vf['or_direction'] or 'N/A'}"].append(t)
            print_group_table(groups_or, "VK: BY OPENING RANGE DIRECTION")

            # By market trend
            groups_mkt = defaultdict(list)
            for t, vf in vk_data:
                mt = vf["market_trend"]
                label = {1: "BULL", 0: "NEUTRAL", -1: "BEAR"}.get(mt, "UNK")
                groups_mkt[label].append(t)
            print_group_table(groups_mkt, "VK: BY MARKET TREND AT SIGNAL")

            # By RR ratio
            groups_rr = defaultdict(list)
            for t, vf in vk_data:
                label = bucket_numeric(vf["rr"],
                                       [1.5, 2.0, 3.0],
                                       ["LOW RR (<1.5)", "MED RR (1.5-2.0)",
                                        "GOOD RR (2.0-3.0)", "HIGH RR (>3.0)"])
                groups_rr[label].append(t)
            print_group_table(groups_rr, "VK: BY R:R RATIO")

    # ══════════════════════════════════════════════════════════════
    #  STUDY 4b: SECOND_CHANCE FEATURES
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  STUDY 4b: SECOND_CHANCE SPECIFIC FEATURES")
    print(f"{'=' * 100}")

    sc_trades = [(sym, t, bars) for sym, t, bars in all_trades
                 if t.signal.setup_id == SetupId.SECOND_CHANCE]

    if sc_trades:
        sc_data = []
        for sym, t, bars in sc_trades:
            vf = vk_features(bars, t.signal)  # reuse same feature extraction
            cf = candle_features(bars, t.signal)
            if vf and cf:
                sc_data.append((t, vf, cf))

        if sc_data:
            # By time of day
            groups_time = defaultdict(list)
            for t, vf, cf in sc_data:
                groups_time[vf["time_bucket"]].append(t)
            print_group_table(groups_time, "SC: BY TIME OF DAY")

            # By quality
            groups_sc_q = defaultdict(list)
            for t, vf, cf in sc_data:
                groups_sc_q[f"Q={vf['quality']}"].append(t)
            print_group_table(groups_sc_q, "SC: BY QUALITY SCORE")

            # By RVOL
            groups_rvol = defaultdict(list)
            for t, vf, cf in sc_data:
                label = bucket_numeric(cf["rvol"],
                                       [0.5, 1.0, 1.5, 2.5],
                                       ["LOW (<0.5x)", "BELOW AVG (0.5-1x)", "NORMAL (1-1.5x)",
                                        "STRONG (1.5-2.5x)", "SURGE (>2.5x)"])
                groups_rvol[label].append(t)
            print_group_table(groups_rvol, "SC: BY SIGNAL BAR RELATIVE VOLUME")

            # By market trend
            groups_mkt = defaultdict(list)
            for t, vf, cf in sc_data:
                mt = vf["market_trend"]
                label = {1: "BULL", 0: "NEUTRAL", -1: "BEAR"}.get(mt, "UNK")
                groups_mkt[label].append(t)
            print_group_table(groups_mkt, "SC: BY MARKET TREND AT SIGNAL")

            # By exit reason
            groups_exit = defaultdict(list)
            for t, vf, cf in sc_data:
                groups_exit[t.exit_reason].append(t)
            print_group_table(groups_exit, "SC: BY EXIT REASON")

    # ══════════════════════════════════════════════════════════════
    #  CROSS-STUDY: STRONGEST FILTERS
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  CROSS-STUDY: COMBINED FILTER CANDIDATES")
    print(f"{'=' * 100}")

    # Test composite filters
    filters = {}

    # Filter 1: GREEN day only
    f1 = [t for sym, t, bars in all_trades
          if spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == "GREEN"]
    filters["GREEN day only"] = f1

    # Filter 2: Non-RED day
    f2 = [t for sym, t, bars in all_trades
          if spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") != "RED"]
    filters["Non-RED day"] = f2

    # Filter 3: GREEN+TREND day
    f3 = [t for sym, t, bars in all_trades
          if spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == "GREEN"
          and spy_day_info.get(t.signal.timestamp.date(), {}).get("character") == "TREND"]
    filters["GREEN+TREND day"] = f3

    # Filter 4: Q >= 2
    f4 = [t for sym, t, bars in all_trades if t.signal.quality_score >= 2]
    filters["Q >= 2"] = f4

    # Filter 5: Q >= 3
    f5 = [t for sym, t, bars in all_trades if t.signal.quality_score >= 3]
    filters["Q >= 3"] = f5

    # Filter 6: Non-RED + Q >= 2
    f6 = [t for sym, t, bars in all_trades
          if spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") != "RED"
          and t.signal.quality_score >= 2]
    filters["Non-RED + Q >= 2"] = f6

    # Filter 7: Exclude LATE signals (last 30 min)
    f7 = []
    for sym, t, bars in all_trades:
        hhmm = t.signal.timestamp.hour * 100 + t.signal.timestamp.minute
        if hhmm < 1530:
            f7.append(t)
    filters["Before 15:30 only"] = f7

    # Filter 8: RVOL >= 1.0 on signal bar
    f8 = []
    for sym, t, bars in all_trades:
        cf = candle_features(bars, t.signal)
        if cf and cf.get("rvol", 0) >= 1.0:
            f8.append(t)
    filters["RVOL >= 1.0 on signal bar"] = f8

    # Filter 9: Bullish EMA stack for longs
    f9 = []
    for sym, t, bars in all_trades:
        ef = ema_features(bars, t.signal)
        if ef:
            stack = ef["ema_stack"]
            if t.signal.direction == 1 and stack in ("BULL", "MIXED_BULL"):
                f9.append(t)
            elif t.signal.direction == -1 and stack in ("BEAR", "MIXED_BEAR"):
                f9.append(t)
    filters["Favorable EMA stack"] = f9

    # Filter 10: Non-RED + Q >= 2 + Before 15:30
    f7_sigs = {t.signal.timestamp for t in f7}
    f10 = [t for t in f6 if t.signal.timestamp in f7_sigs]
    filters["Non-RED + Q>=2 + <15:30"] = f10

    print(f"\n  {'Filter':<35} │ {'N':>4} {'WR%':>5} {'PF':>5} {'Exp':>7} "
          f"{'PnL':>8} {'StpR':>5}")
    print(f"  {'─' * 35}─┼{'─' * 40}")

    # Baseline row
    print(f"  {'BASELINE (no filter)':<35} │ {m_all['n']:>4} {m_all['wr']:>5.1f} "
          f"{pf_str(m_all['pf']):>5} {m_all['exp']:>+6.3f}R {m_all['pnl']:>+8.2f} "
          f"{m_all['stop_rate']:>5.0f}%")

    for fname, ftrades in filters.items():
        if not ftrades:
            continue
        m = compute_metrics(ftrades)
        marker = " ★" if m["pf"] > m_all["pf"] and m["n"] >= 10 else ""
        print(f"  {fname:<35} │ {m['n']:>4} {m['wr']:>5.1f} "
              f"{pf_str(m['pf']):>5} {m['exp']:>+6.3f}R {m['pnl']:>+8.2f} "
              f"{m['stop_rate']:>5.0f}%{marker}")

    print(f"\n  ★ = PF better than baseline with N >= 10")

    # ══════════════════════════════════════════════════════════════
    #  SUMMARY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  FEATURE STUDY SUMMARY")
    print(f"{'=' * 100}")
    print(f"  Baseline: VK + SC_VOL (corrected backtest, day-boundary fix)")
    print(f"  Period: {min(all_dates)} → {max(all_dates)} ({n_days} days)")
    print(f"  Universe: {len(all_bars)} symbols, {len(all_trades)} trades")
    print(f"  PnL={m_all['pnl']:+.2f}  PF={pf_str(m_all['pf'])}  WR={m_all['wr']:.1f}%")
    print(f"{'=' * 100}\n")


if __name__ == "__main__":
    main()
