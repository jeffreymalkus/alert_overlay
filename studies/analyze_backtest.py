"""
Deep backtest analysis — extracts the WHY behind performance differences.

Examines:
  1. Setup family performance (Reversal vs Trend vs Mean-Rev)
  2. Quality score edge (does higher quality = better results?)
  3. Time-of-day patterns (when do winners cluster?)
  4. Regime alignment (fits_regime = True vs False)
  5. VWAP bias (above vs below)
  6. Opening range direction (UP vs DN vs FLAT)
  7. Price characteristics (ATR, spread, volume profile)
  8. Stop distance analysis (too tight? what's the optimal?)
  9. R:R realized vs theoretical
 10. Symbol characteristics that predict profitability

Usage:
    python -m alert_overlay.analyze_backtest --no-download
    python -m alert_overlay.analyze_backtest --symbols TSLA,META,GOOG
"""

import argparse
import csv
import sys
import statistics
from collections import defaultdict, Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..backtest import load_bars_from_csv, run_backtest, BacktestResult, Trade
from ..config import OverlayConfig
from ..models import SetupId, SetupFamily, SETUP_DISPLAY_NAME

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent.parent / "data"
WATCHLIST_FILE = Path(__file__).parent.parent / "watchlist.txt"


def load_watchlist() -> list:
    if not WATCHLIST_FILE.exists():
        print(f"ERROR: {WATCHLIST_FILE} not found.")
        sys.exit(1)
    symbols = []
    with open(WATCHLIST_FILE) as f:
        for line in f:
            sym = line.strip().upper()
            if sym and not sym.startswith("#"):
                symbols.append(sym)
    return symbols


def gather_all_trades(symbols: list, cfg: OverlayConfig, session_end: int = 1555) -> dict:
    """Run backtests and collect all trades with metadata. Returns {symbol: (result, bars)}."""
    all_data = {}
    for sym in symbols:
        csv_path = DATA_DIR / f"{sym}_5min.csv"
        if not csv_path.exists():
            continue
        bars = load_bars_from_csv(str(csv_path))
        if not bars:
            continue
        result = run_backtest(bars, cfg=cfg, session_end_hhmm=session_end)
        all_data[sym] = (result, bars)
    return all_data


def compute_bar_stats(bars) -> dict:
    """Compute symbol-level characteristics from bar data."""
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    ranges = [b.high - b.low for b in bars if b.high - b.low > 0]

    avg_price = statistics.mean(closes) if closes else 0
    avg_volume = statistics.mean(volumes) if volumes else 0
    avg_range = statistics.mean(ranges) if ranges else 0
    avg_range_pct = (avg_range / avg_price * 100) if avg_price > 0 else 0

    # Intraday volatility (std of 5-min returns)
    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            returns.append((closes[i] - closes[i - 1]) / closes[i - 1])
    vol_5min = statistics.stdev(returns) * 100 if len(returns) > 2 else 0

    return {
        "avg_price": avg_price,
        "avg_volume": avg_volume,
        "avg_range": avg_range,
        "avg_range_pct": avg_range_pct,
        "volatility_5min": vol_5min,
    }


def classify_trade(trade: Trade) -> dict:
    """Extract all analyzable attributes from a single trade."""
    sig = trade.signal
    t = sig.timestamp

    # Time of day bucket
    hhmm = t.hour * 100 + t.minute
    if hhmm < 1000:
        time_bucket = "9:30-10:00"
    elif hhmm < 1030:
        time_bucket = "10:00-10:30"
    elif hhmm < 1100:
        time_bucket = "10:30-11:00"
    elif hhmm < 1200:
        time_bucket = "11:00-12:00"
    elif hhmm < 1300:
        time_bucket = "12:00-13:00"
    elif hhmm < 1400:
        time_bucket = "13:00-14:00"
    elif hhmm < 1500:
        time_bucket = "14:00-15:00"
    else:
        time_bucket = "15:00-16:00"

    # Time period (broader)
    if hhmm < 1000:
        time_period = "OPEN"
    elif hhmm < 1130:
        time_period = "MORNING"
    elif hhmm < 1330:
        time_period = "MIDDAY"
    else:
        time_period = "AFTERNOON"

    return {
        "setup_id": sig.setup_id,
        "setup_name": sig.setup_name,
        "family": sig.family,
        "direction": sig.direction,
        "direction_str": "LONG" if sig.direction == 1 else "SHORT",
        "quality": sig.quality_score,
        "fits_regime": sig.fits_regime,
        "vwap_bias": sig.vwap_bias,
        "or_direction": sig.or_direction,
        "rr_theoretical": sig.rr_ratio,
        "risk": sig.risk,
        "pnl_points": trade.pnl_points,
        "pnl_rr": trade.pnl_rr,
        "exit_reason": trade.exit_reason,
        "bars_held": trade.bars_held,
        "is_winner": trade.pnl_points > 0,
        "time_bucket": time_bucket,
        "time_period": time_period,
        "hhmm": hhmm,
        "confluence_tags": sig.confluence_tags,
        "confluence_count": len(sig.confluence_tags),
    }


def print_section(title):
    print(f"\n{'─' * 70}")
    print(f"  {title}")
    print(f"{'─' * 70}")


def analyze_dimension(trades_classified, key_name, label):
    """Analyze performance by a single dimension."""
    buckets = defaultdict(list)
    for tc in trades_classified:
        buckets[tc[key_name]].append(tc)

    print(f"\n  {label}:")
    print(f"  {'Value':<20} {'Trades':>6} {'WR%':>7} {'AvgPnL':>9} {'TotPnL':>10} {'AvgR':>7} {'PF':>6}")
    print(f"  {'-' * 67}")

    rows = []
    for val, trades in sorted(buckets.items(), key=lambda x: sum(t["pnl_points"] for t in x[1]), reverse=True):
        n = len(trades)
        wins = sum(1 for t in trades if t["is_winner"])
        wr = wins / n * 100 if n > 0 else 0
        total_pnl = sum(t["pnl_points"] for t in trades)
        avg_pnl = total_pnl / n if n > 0 else 0
        avg_rr = sum(t["pnl_rr"] for t in trades) / n if n > 0 else 0
        gross_w = sum(t["pnl_points"] for t in trades if t["is_winner"])
        gross_l = abs(sum(t["pnl_points"] for t in trades if not t["is_winner"]))
        pf = gross_w / gross_l if gross_l > 0 else float("inf")
        pf_str = f"{pf:.2f}" if pf < 999 else "∞"

        display_val = str(val)
        if isinstance(val, SetupFamily):
            display_val = {1: "REVERSAL", 2: "TREND", 3: "MEAN_REV"}.get(val, str(val))

        print(f"  {display_val:<20} {n:>6} {wr:>6.1f}% {avg_pnl:>+9.4f} {total_pnl:>+10.2f} "
              f"{avg_rr:>+7.3f} {pf_str:>6}")
        rows.append((val, n, wr, total_pnl, pf))

    return rows


def main():
    parser = argparse.ArgumentParser(description="Deep backtest analysis")
    parser.add_argument("--symbols", type=str, default=None)
    parser.add_argument("--min-quality", type=int, default=None)
    parser.add_argument("--min-rr", type=float, default=None)
    parser.add_argument("--slippage", type=float, default=None)
    parser.add_argument("--session-end", type=int, default=1555)
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        symbols = load_watchlist()

    cfg = OverlayConfig()
    if args.min_quality is not None:
        cfg.min_quality = args.min_quality
    if args.min_rr is not None:
        cfg.min_rr = args.min_rr
    if args.slippage is not None:
        cfg.slippage_per_side = args.slippage

    print(f"Analyzing {len(symbols)} symbols...")
    print(f"Config: min_quality={cfg.min_quality}, min_rr={cfg.min_rr}, slippage={cfg.slippage_per_side}")

    # ── Gather all trades ──
    all_data = gather_all_trades(symbols, cfg, args.session_end)
    print(f"Loaded data for {len(all_data)} symbols\n")

    # ── Classify every trade ──
    all_trades_classified = []
    symbol_stats = {}

    for sym, (result, bars) in all_data.items():
        bar_stats = compute_bar_stats(bars)
        symbol_stats[sym] = bar_stats
        for trade in result.trades:
            tc = classify_trade(trade)
            tc["symbol"] = sym
            tc["avg_price"] = bar_stats["avg_price"]
            tc["volatility_5min"] = bar_stats["volatility_5min"]
            tc["avg_range_pct"] = bar_stats["avg_range_pct"]
            all_trades_classified.append(tc)

    total = len(all_trades_classified)
    winners = [t for t in all_trades_classified if t["is_winner"]]
    losers = [t for t in all_trades_classified if not t["is_winner"]]
    print(f"Total trades: {total} | Winners: {len(winners)} | Losers: {len(losers)}")

    # ══════════════════════════════════════════════════════════════
    # 1. SETUP TYPE PERFORMANCE
    # ══════════════════════════════════════════════════════════════
    print_section("1. PERFORMANCE BY SETUP TYPE")
    analyze_dimension(all_trades_classified, "setup_name", "Setup")

    # ══════════════════════════════════════════════════════════════
    # 2. SETUP FAMILY PERFORMANCE
    # ══════════════════════════════════════════════════════════════
    print_section("2. PERFORMANCE BY SETUP FAMILY")
    analyze_dimension(all_trades_classified, "family", "Family")

    # ══════════════════════════════════════════════════════════════
    # 3. QUALITY SCORE EDGE
    # ══════════════════════════════════════════════════════════════
    print_section("3. QUALITY SCORE ANALYSIS")
    analyze_dimension(all_trades_classified, "quality", "Quality Score")

    # ══════════════════════════════════════════════════════════════
    # 4. REGIME ALIGNMENT
    # ══════════════════════════════════════════════════════════════
    print_section("4. REGIME ALIGNMENT (fits_regime)")
    analyze_dimension(all_trades_classified, "fits_regime", "Fits Regime?")

    # ══════════════════════════════════════════════════════════════
    # 5. DIRECTION
    # ══════════════════════════════════════════════════════════════
    print_section("5. DIRECTION ANALYSIS")
    analyze_dimension(all_trades_classified, "direction_str", "Direction")

    # Cross: direction x regime
    print("\n  Direction × Regime Cross:")
    for d in ["LONG", "SHORT"]:
        for r in [True, False]:
            subset = [t for t in all_trades_classified if t["direction_str"] == d and t["fits_regime"] == r]
            if not subset:
                continue
            n = len(subset)
            wr = sum(1 for t in subset if t["is_winner"]) / n * 100
            pnl = sum(t["pnl_points"] for t in subset)
            print(f"    {d:<6} regime={str(r):<6}: {n:>5} trades, {wr:>5.1f}% WR, {pnl:>+10.2f} PnL")

    # ══════════════════════════════════════════════════════════════
    # 6. VWAP BIAS
    # ══════════════════════════════════════════════════════════════
    print_section("6. VWAP BIAS")
    analyze_dimension(all_trades_classified, "vwap_bias", "VWAP Position")

    # Cross: direction x vwap
    print("\n  Direction × VWAP Cross:")
    for d in ["LONG", "SHORT"]:
        for v in ["ABV", "BLW", ""]:
            subset = [t for t in all_trades_classified if t["direction_str"] == d and t["vwap_bias"] == v]
            if not subset:
                continue
            n = len(subset)
            wr = sum(1 for t in subset if t["is_winner"]) / n * 100
            pnl = sum(t["pnl_points"] for t in subset)
            label = v if v else "NONE"
            print(f"    {d:<6} VWAP={label:<4}: {n:>5} trades, {wr:>5.1f}% WR, {pnl:>+10.2f} PnL")

    # ══════════════════════════════════════════════════════════════
    # 7. OPENING RANGE DIRECTION
    # ══════════════════════════════════════════════════════════════
    print_section("7. OPENING RANGE DIRECTION")
    analyze_dimension(all_trades_classified, "or_direction", "OR Direction")

    # Cross: direction x OR
    print("\n  Trade Direction × OR Direction Cross:")
    for d in ["LONG", "SHORT"]:
        for o in ["UP", "DN", "FLAT", ""]:
            subset = [t for t in all_trades_classified if t["direction_str"] == d and t["or_direction"] == o]
            if not subset:
                continue
            n = len(subset)
            wr = sum(1 for t in subset if t["is_winner"]) / n * 100
            pnl = sum(t["pnl_points"] for t in subset)
            label = o if o else "NONE"
            print(f"    {d:<6} OR={label:<5}: {n:>5} trades, {wr:>5.1f}% WR, {pnl:>+10.2f} PnL")

    # ══════════════════════════════════════════════════════════════
    # 8. TIME OF DAY
    # ══════════════════════════════════════════════════════════════
    print_section("8. TIME OF DAY ANALYSIS")
    analyze_dimension(all_trades_classified, "time_bucket", "Time Window")

    print("\n  Broader Time Periods:")
    analyze_dimension(all_trades_classified, "time_period", "Period")

    # ══════════════════════════════════════════════════════════════
    # 9. EXIT REASON BREAKDOWN
    # ══════════════════════════════════════════════════════════════
    print_section("9. EXIT REASON ANALYSIS")
    analyze_dimension(all_trades_classified, "exit_reason", "Exit Reason")

    # Stop analysis: how far does price go against before stopping?
    stops = [t for t in all_trades_classified if t["exit_reason"] == "stop"]
    targets = [t for t in all_trades_classified if t["exit_reason"] == "target"]
    eods = [t for t in all_trades_classified if t["exit_reason"] == "eod"]

    if stops:
        avg_stop_risk = statistics.mean(t["risk"] for t in stops if t["risk"] > 0)
        print(f"\n  Stop trades avg risk (pts): {avg_stop_risk:.4f}")
    if eods:
        eod_winners = sum(1 for t in eods if t["is_winner"])
        eod_wr = eod_winners / len(eods) * 100
        eod_pnl = sum(t["pnl_points"] for t in eods)
        print(f"  EOD trades: {len(eods)} ({eod_wr:.1f}% WR, {eod_pnl:+.2f} PnL)")
        print(f"  → EOD winners means price moved favorably but didn't hit target before close")

    # ══════════════════════════════════════════════════════════════
    # 10. R:R THEORETICAL VS REALIZED
    # ══════════════════════════════════════════════════════════════
    print_section("10. R:R ANALYSIS")
    rr_buckets = defaultdict(list)
    for t in all_trades_classified:
        rr = t["rr_theoretical"]
        if rr < 2.0:
            bucket = "1.5-2.0"
        elif rr < 2.5:
            bucket = "2.0-2.5"
        elif rr < 3.0:
            bucket = "2.5-3.0"
        elif rr < 4.0:
            bucket = "3.0-4.0"
        else:
            bucket = "4.0+"
        rr_buckets[bucket].append(t)

    print(f"  {'Theor R:R':<12} {'Trades':>6} {'WR%':>7} {'AvgRealR':>9} {'TotPnL':>10}")
    print(f"  {'-' * 48}")
    for bucket in ["1.5-2.0", "2.0-2.5", "2.5-3.0", "3.0-4.0", "4.0+"]:
        trades = rr_buckets.get(bucket, [])
        if not trades:
            continue
        n = len(trades)
        wr = sum(1 for t in trades if t["is_winner"]) / n * 100
        avg_real_rr = sum(t["pnl_rr"] for t in trades) / n
        total_pnl = sum(t["pnl_points"] for t in trades)
        print(f"  {bucket:<12} {n:>6} {wr:>6.1f}% {avg_real_rr:>+9.3f} {total_pnl:>+10.2f}")

    # ══════════════════════════════════════════════════════════════
    # 11. HOLD TIME ANALYSIS
    # ══════════════════════════════════════════════════════════════
    print_section("11. HOLD TIME ANALYSIS")
    hold_buckets = defaultdict(list)
    for t in all_trades_classified:
        bars = t["bars_held"]
        if bars <= 2:
            bucket = "1-2 bars"
        elif bars <= 5:
            bucket = "3-5 bars"
        elif bars <= 10:
            bucket = "6-10 bars"
        elif bars <= 20:
            bucket = "11-20 bars"
        else:
            bucket = "21+ bars"
        hold_buckets[bucket].append(t)

    print(f"  {'Hold Time':<12} {'Trades':>6} {'WR%':>7} {'AvgPnL':>9} {'TotPnL':>10}")
    print(f"  {'-' * 48}")
    for bucket in ["1-2 bars", "3-5 bars", "6-10 bars", "11-20 bars", "21+ bars"]:
        trades = hold_buckets.get(bucket, [])
        if not trades:
            continue
        n = len(trades)
        wr = sum(1 for t in trades if t["is_winner"]) / n * 100
        avg_pnl = sum(t["pnl_points"] for t in trades) / n
        total_pnl = sum(t["pnl_points"] for t in trades)
        print(f"  {bucket:<12} {n:>6} {wr:>6.1f}% {avg_pnl:>+9.4f} {total_pnl:>+10.2f}")

    # ══════════════════════════════════════════════════════════════
    # 12. SYMBOL CHARACTERISTICS vs PERFORMANCE
    # ══════════════════════════════════════════════════════════════
    print_section("12. SYMBOL CHARACTERISTICS vs PROFITABILITY")

    sym_perf = []
    for sym, (result, bars) in all_data.items():
        stats = symbol_stats[sym]
        trades = result.trades
        if not trades:
            continue
        total_pnl = sum(t.pnl_points for t in trades)
        wr = sum(1 for t in trades if t.pnl_points > 0) / len(trades) * 100
        sym_perf.append({
            "symbol": sym,
            "trades": len(trades),
            "total_pnl": total_pnl,
            "win_rate": wr,
            "avg_price": stats["avg_price"],
            "volatility_5min": stats["volatility_5min"],
            "avg_range_pct": stats["avg_range_pct"],
        })

    # Split into profitable vs unprofitable
    profitable = [s for s in sym_perf if s["total_pnl"] > 0]
    unprofitable = [s for s in sym_perf if s["total_pnl"] <= 0]

    if profitable and unprofitable:
        def avg_stat(group, key):
            return statistics.mean(s[key] for s in group) if group else 0

        print(f"\n  {'Characteristic':<25} {'Profitable ({})'.format(len(profitable)):>20} "
              f"{'Unprofitable ({})'.format(len(unprofitable)):>20}")
        print(f"  {'-' * 67}")

        for label, key in [
            ("Avg Price ($)", "avg_price"),
            ("5min Volatility (%)", "volatility_5min"),
            ("Avg Range (%)", "avg_range_pct"),
            ("Avg Trades/Symbol", "trades"),
            ("Avg Win Rate (%)", "win_rate"),
        ]:
            p_val = avg_stat(profitable, key)
            u_val = avg_stat(unprofitable, key)
            print(f"  {label:<25} {p_val:>20.3f} {u_val:>20.3f}")

    # ══════════════════════════════════════════════════════════════
    # 13. CONFLUENCE TAG ANALYSIS
    # ══════════════════════════════════════════════════════════════
    print_section("13. CONFLUENCE ANALYSIS")
    conf_count_buckets = defaultdict(list)
    for t in all_trades_classified:
        cc = t["confluence_count"]
        conf_count_buckets[cc].append(t)

    print(f"  {'Confluence#':<12} {'Trades':>6} {'WR%':>7} {'AvgPnL':>9} {'TotPnL':>10}")
    print(f"  {'-' * 48}")
    for cc in sorted(conf_count_buckets.keys()):
        trades = conf_count_buckets[cc]
        n = len(trades)
        wr = sum(1 for t in trades if t["is_winner"]) / n * 100
        avg_pnl = sum(t["pnl_points"] for t in trades) / n
        total_pnl = sum(t["pnl_points"] for t in trades)
        print(f"  {cc:<12} {n:>6} {wr:>6.1f}% {avg_pnl:>+9.4f} {total_pnl:>+10.2f}")

    # What specific tags appear most in winners vs losers?
    win_tags = Counter()
    lose_tags = Counter()
    for t in all_trades_classified:
        tags = t["confluence_tags"]
        if t["is_winner"]:
            for tag in tags:
                win_tags[tag] += 1
        else:
            for tag in tags:
                lose_tags[tag] += 1

    if win_tags or lose_tags:
        all_tags = set(list(win_tags.keys()) + list(lose_tags.keys()))
        print(f"\n  {'Tag':<20} {'In Wins':>8} {'In Losses':>10} {'Win Ratio':>10}")
        print(f"  {'-' * 50}")
        for tag in sorted(all_tags):
            w = win_tags.get(tag, 0)
            l = lose_tags.get(tag, 0)
            ratio = w / (w + l) * 100 if (w + l) > 0 else 0
            print(f"  {tag:<20} {w:>8} {l:>10} {ratio:>9.1f}%")

    # ══════════════════════════════════════════════════════════════
    # 14. COMBINED FILTER: THE "EDGE" PROFILE
    # ══════════════════════════════════════════════════════════════
    print_section("14. EDGE PROFILE — BEST FILTER COMBINATIONS")

    # Test various filter combos
    filters = [
        ("All trades", lambda t: True),
        ("Quality >= 3", lambda t: t["quality"] >= 3),
        ("Quality >= 4", lambda t: t["quality"] >= 4),
        ("Quality >= 5", lambda t: t["quality"] >= 5),
        ("Regime=True only", lambda t: t["fits_regime"]),
        ("Q>=3 + Regime", lambda t: t["quality"] >= 3 and t["fits_regime"]),
        ("Q>=4 + Regime", lambda t: t["quality"] >= 4 and t["fits_regime"]),
        ("9EMA RETEST only", lambda t: t["setup_name"] == "9EMA RETEST"),
        ("VWAP KISS only", lambda t: t["setup_name"] == "VWAP KISS"),
        ("Trend family only", lambda t: t["family"] == SetupFamily.TREND),
        ("LONG + above VWAP", lambda t: t["direction_str"] == "LONG" and t["vwap_bias"] == "ABV"),
        ("SHORT + below VWAP", lambda t: t["direction_str"] == "SHORT" and t["vwap_bias"] == "BLW"),
        ("LONG above + SHORT below", lambda t: (t["direction_str"] == "LONG" and t["vwap_bias"] == "ABV") or
                                               (t["direction_str"] == "SHORT" and t["vwap_bias"] == "BLW")),
        ("Q>=3 + VWAP-aligned", lambda t: t["quality"] >= 3 and (
            (t["direction_str"] == "LONG" and t["vwap_bias"] == "ABV") or
            (t["direction_str"] == "SHORT" and t["vwap_bias"] == "BLW"))),
        ("Q>=3 + Regime + VWAP", lambda t: t["quality"] >= 3 and t["fits_regime"] and (
            (t["direction_str"] == "LONG" and t["vwap_bias"] == "ABV") or
            (t["direction_str"] == "SHORT" and t["vwap_bias"] == "BLW"))),
        ("OPEN period only", lambda t: t["time_period"] == "OPEN"),
        ("MORNING only", lambda t: t["time_period"] == "MORNING"),
        ("No MIDDAY", lambda t: t["time_period"] != "MIDDAY"),
        ("Q>=3 + No MIDDAY", lambda t: t["quality"] >= 3 and t["time_period"] != "MIDDAY"),
    ]

    print(f"  {'Filter':<35} {'Trades':>6} {'WR%':>7} {'PF':>6} {'TotPnL':>10} {'AvgR':>7}")
    print(f"  {'-' * 73}")

    for name, fn in filters:
        subset = [t for t in all_trades_classified if fn(t)]
        if not subset:
            print(f"  {name:<35} {'0':>6}")
            continue
        n = len(subset)
        wr = sum(1 for t in subset if t["is_winner"]) / n * 100
        pnl = sum(t["pnl_points"] for t in subset)
        avg_rr = sum(t["pnl_rr"] for t in subset) / n
        gross_w = sum(t["pnl_points"] for t in subset if t["is_winner"])
        gross_l = abs(sum(t["pnl_points"] for t in subset if not t["is_winner"]))
        pf = gross_w / gross_l if gross_l > 0 else float("inf")
        pf_str = f"{pf:.2f}" if pf < 999 else "∞"
        print(f"  {name:<35} {n:>6} {wr:>6.1f}% {pf_str:>6} {pnl:>+10.2f} {avg_rr:>+7.3f}")

    # ══════════════════════════════════════════════════════════════
    # 15. PER-SYMBOL BEST DIRECTION
    # ══════════════════════════════════════════════════════════════
    print_section("15. OPTIMAL DIRECTION PER SYMBOL")
    print(f"  {'Symbol':<7} {'Long PnL':>10} {'Short PnL':>10} {'Best Dir':>10} {'Edge':>10}")
    print(f"  {'-' * 49}")

    for sym in sorted(all_data.keys()):
        sym_trades = [t for t in all_trades_classified if t["symbol"] == sym]
        longs = [t for t in sym_trades if t["direction_str"] == "LONG"]
        shorts = [t for t in sym_trades if t["direction_str"] == "SHORT"]
        long_pnl = sum(t["pnl_points"] for t in longs)
        short_pnl = sum(t["pnl_points"] for t in shorts)
        best = "LONG" if long_pnl > short_pnl else "SHORT"
        edge = max(long_pnl, short_pnl) - min(long_pnl, short_pnl)
        print(f"  {sym:<7} {long_pnl:>+10.2f} {short_pnl:>+10.2f} {best:>10} {edge:>+10.2f}")

    # Filter to best direction only
    best_dir_pnl = 0.0
    for sym in all_data.keys():
        sym_trades = [t for t in all_trades_classified if t["symbol"] == sym]
        longs = [t for t in sym_trades if t["direction_str"] == "LONG"]
        shorts = [t for t in sym_trades if t["direction_str"] == "SHORT"]
        long_pnl = sum(t["pnl_points"] for t in longs)
        short_pnl = sum(t["pnl_points"] for t in shorts)
        best_dir_pnl += max(long_pnl, short_pnl)

    total_pnl_all = sum(t["pnl_points"] for t in all_trades_classified)
    print(f"\n  Total PnL (both dirs):     {total_pnl_all:>+10.2f}")
    print(f"  Total PnL (best dir only): {best_dir_pnl:>+10.2f}")
    print(f"  Direction filtering gain:  {best_dir_pnl - total_pnl_all:>+10.2f}")

    print("\n" + "=" * 70)
    print("  ANALYSIS COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
