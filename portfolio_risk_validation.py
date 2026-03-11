"""
Portfolio Risk Validation for Integrated Two-Sided System.

Evaluates whether the integrated long+short candidate is practically
tradable as a portfolio, not just statistically positive trade-by-trade.

Reports:
  1. Position concurrency (max, avg simultaneous)
  2. Gross long / short / net exposure over time
  3. Per-symbol concentration
  4. Worst day / worst week
  5. Daily stop frequency
  6. Contribution by setup and by regime
  7. Portfolio-control variant comparison:
     - No cap (baseline)
     - Max 5 open positions
     - Max 8 open positions
     - Max 10 gross exposure (long + short)
     - Max daily loss stop (-5R)

Usage:
    python -m alert_overlay.portfolio_risk_validation
"""

import argparse
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import NaN, Bar, SetupId, SetupFamily, SETUP_FAMILY_MAP, SETUP_DISPLAY_NAME
from .market_context import SECTOR_MAP, get_sector_etf

DATA_DIR = Path(__file__).parent / "data"


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


# ── UTrade wrapper ──

class UTrade:
    __slots__ = ("pnl_points", "pnl_rr", "exit_reason", "bars_held",
                 "entry_time", "exit_time", "entry_date", "side", "setup",
                 "sym", "quality", "risk", "direction")

    def __init__(self, t: Trade, sym: str):
        self.pnl_points = t.pnl_points
        self.pnl_rr = t.pnl_rr
        self.exit_reason = t.exit_reason
        self.bars_held = t.bars_held
        self.entry_time = t.signal.timestamp
        self.exit_time = t.exit_time if t.exit_time else t.signal.timestamp
        self.entry_date = t.signal.timestamp.date()
        self.side = "LONG" if t.signal.direction == 1 else "SHORT"
        self.direction = t.signal.direction
        self.setup = t.signal.setup_id
        self.sym = sym
        self.quality = t.signal.quality_score
        self.risk = abs(t.signal.entry_price - t.signal.stop_price) if t.signal.stop_price else 0


# ── SPY regime classifier ──

def classify_spy_days(spy_bars):
    daily = defaultdict(list)
    for b in spy_bars:
        daily[b.timestamp.date()].append(b)
    day_info = {}
    for d in sorted(daily.keys()):
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
        if day_range > 0:
            close_pos = (day_close - day_low) / day_range
            character = "TREND" if (close_pos >= 0.75 or close_pos <= 0.25) else "CHOPPY"
        else:
            character = "CHOPPY"
        day_info[d] = {"direction": direction, "character": character,
                       "spy_change_pct": change_pct}
    return day_info


# ── Core metrics ──

def compute_metrics(trades: List[UTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0, "pf": 0, "exp": 0, "pnl": 0,
                "max_dd": 0, "stop_rate": 0, "qstop_rate": 0}
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
    return {
        "n": n, "wr": len(wins) / n * 100, "pf": pf,
        "exp": sum(t.pnl_rr for t in trades) / n,
        "pnl": pnl, "max_dd": dd,
        "stop_rate": stopped / n * 100,
        "qstop_rate": qstop / n * 100,
    }


# ── Concurrency snapshot analysis ──

def compute_concurrency_timeseries(trades: List[UTrade]):
    """Return per-5min-bar snapshots of open positions."""
    if not trades:
        return []

    intervals = [(t.entry_time, t.exit_time, t.side, t.sym) for t in trades]
    min_t = min(i[0] for i in intervals)
    max_t = max(i[1] for i in intervals)

    delta = timedelta(minutes=5)
    cursor = min_t.replace(second=0, microsecond=0)
    cursor = cursor - timedelta(minutes=cursor.minute % 5)

    snapshots = []
    while cursor <= max_t:
        hhmm = cursor.hour * 100 + cursor.minute
        if 930 <= hhmm <= 1555:
            n_long = 0
            n_short = 0
            sym_set = set()
            for entry, exit_t, side, sym in intervals:
                if entry <= cursor < exit_t:
                    if side == "LONG":
                        n_long += 1
                    else:
                        n_short += 1
                    sym_set.add(sym)
            snapshots.append({
                "time": cursor,
                "date": cursor.date(),
                "long": n_long,
                "short": n_short,
                "total": n_long + n_short,
                "net": n_long - n_short,
                "gross": n_long + n_short,
                "unique_syms": len(sym_set),
            })
        cursor += delta

    return snapshots


# ── Portfolio cap simulation ──

def simulate_portfolio_cap(trades: List[UTrade], max_positions: int = None,
                            max_gross: int = None,
                            max_daily_loss_rr: float = None) -> List[UTrade]:
    """
    Simulate portfolio-level caps by rejecting trades that would exceed limits.
    Trades are processed in entry_time order. Open trades tracked via intervals.
    """
    sorted_trades = sorted(trades, key=lambda t: t.entry_time)
    accepted = []
    daily_pnl_rr = defaultdict(float)  # running daily PnL in R

    for t in sorted_trades:
        # Daily loss stop check
        if max_daily_loss_rr is not None:
            if daily_pnl_rr[t.entry_date] <= max_daily_loss_rr:
                continue  # day is stopped out

        # Count currently open positions at entry time
        n_open = 0
        n_long = 0
        n_short = 0
        for at in accepted:
            if at.entry_time <= t.entry_time < at.exit_time:
                n_open += 1
                if at.side == "LONG":
                    n_long += 1
                else:
                    n_short += 1

        # Max positions check
        if max_positions is not None and n_open >= max_positions:
            continue

        # Max gross exposure check
        if max_gross is not None and (n_long + n_short) >= max_gross:
            continue

        accepted.append(t)
        daily_pnl_rr[t.entry_date] += t.pnl_rr

    return accepted


# ── Daily analysis ──

def compute_daily_stats(trades: List[UTrade]):
    """Per-day PnL, stop count, trade count."""
    daily = defaultdict(lambda: {"trades": [], "pnl": 0.0, "stops": 0,
                                  "n": 0, "n_long": 0, "n_short": 0})
    for t in trades:
        d = t.entry_date
        daily[d]["trades"].append(t)
        daily[d]["pnl"] += t.pnl_points
        daily[d]["n"] += 1
        if t.side == "LONG":
            daily[d]["n_long"] += 1
        else:
            daily[d]["n_short"] += 1
        if t.exit_reason == "stop":
            daily[d]["stops"] += 1
    return dict(daily)


def compute_weekly_stats(trades: List[UTrade]):
    """Per-week PnL."""
    weekly = defaultdict(lambda: {"pnl": 0.0, "n": 0})
    for t in trades:
        iso = t.entry_date.isocalendar()
        wk = f"{iso[0]}-W{iso[1]:02d}"
        weekly[wk]["pnl"] += t.pnl_points
        weekly[wk]["n"] += 1
    return dict(weekly)


# ═════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="all94")
    args = parser.parse_args()

    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    symbols = sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])

    # Integrated config
    cfg = OverlayConfig()
    cfg.show_ema_scalp = False
    cfg.show_failed_bounce = False
    cfg.show_spencer = False
    cfg.show_ema_fpip = False
    cfg.show_sc_v2 = False
    cfg.show_breakdown_retest = True

    print("=" * 120)
    print("PORTFOLIO RISK VALIDATION — INTEGRATED TWO-SIDED SYSTEM")
    print("=" * 120)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Long: VK + SC (locked baseline)  |  Short: BDR + big wick")

    # Load index/sector bars
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    spy_day_info = classify_spy_days(spy_bars)

    # ── Run backtest ──
    print("\nRunning integrated backtest...")
    all_raw_trades: List[Trade] = []
    sym_for_trade: Dict[int, str] = {}

    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        for t in result.trades:
            all_raw_trades.append(t)
            sym_for_trade[id(t)] = sym

    all_utrades = [UTrade(t, sym_for_trade[id(t)]) for t in all_raw_trades]
    print(f"  Raw trades: {len(all_utrades)}")

    # Apply locked long baseline filters
    def is_red(t):
        return spy_day_info.get(t.entry_date, {}).get("direction") == "RED"

    filtered = []
    for t in all_utrades:
        if t.side == "LONG":
            hhmm = t.entry_time.hour * 100 + t.entry_time.minute
            if is_red(t):
                continue
            if t.quality < 2:
                continue
            if hhmm >= 1530:
                continue
        filtered.append(t)

    longs = [t for t in filtered if t.side == "LONG"]
    shorts = [t for t in filtered if t.side == "SHORT"]
    print(f"  After filters: {len(filtered)} ({len(longs)}L / {len(shorts)}S)\n")

    def get_regime(t):
        info = spy_day_info.get(t.entry_date, {})
        return f"{info.get('direction', 'UNK')}+{info.get('character', 'UNK')}"

    # ═════════════════════════════════════════════════════════════
    #  SECTION 1: POSITION CONCURRENCY
    # ═════════════════════════════════════════════════════════════
    print("=" * 120)
    print("SECTION 1: POSITION CONCURRENCY")
    print("=" * 120)

    snaps = compute_concurrency_timeseries(filtered)
    if snaps:
        active = [s for s in snaps if s["total"] > 0]
        all_total = [s["total"] for s in snaps]
        all_long = [s["long"] for s in snaps]
        all_short = [s["short"] for s in snaps]
        all_net = [s["net"] for s in snaps]
        all_gross = [s["gross"] for s in snaps]

        print(f"\n  Simultaneous positions (5-min snapshots across all trading bars):")
        print(f"    Max simultaneous open:   {max(all_total)}")
        print(f"    Avg simultaneous open:   {statistics.mean(all_total):.2f}")
        print(f"    Median simultaneous:     {statistics.median(all_total):.0f}")
        print(f"    P95 simultaneous:        {sorted(all_total)[int(len(all_total)*0.95)]}")
        print(f"    Max long:                {max(all_long)}")
        print(f"    Max short:               {max(all_short)}")
        print(f"    Avg long:                {statistics.mean(all_long):.2f}")
        print(f"    Avg short:               {statistics.mean(all_short):.2f}")
        flat_pct = sum(1 for s in snaps if s["total"] == 0) / len(snaps) * 100
        print(f"    Pct flat (no positions): {flat_pct:.1f}%")

        # Per-day max
        day_maxes = defaultdict(int)
        day_max_long = defaultdict(int)
        day_max_short = defaultdict(int)
        for s in snaps:
            d = s["date"]
            if s["total"] > day_maxes[d]:
                day_maxes[d] = s["total"]
            if s["long"] > day_max_long[d]:
                day_max_long[d] = s["long"]
            if s["short"] > day_max_short[d]:
                day_max_short[d] = s["short"]

        print(f"\n  Per-day max concurrency distribution:")
        for threshold in [1, 2, 3, 4, 5, 8, 10]:
            days_above = sum(1 for v in day_maxes.values() if v >= threshold)
            print(f"    Days with >= {threshold:2d} concurrent:  {days_above} "
                  f"({days_above/len(day_maxes)*100:.0f}%)")

    # ═════════════════════════════════════════════════════════════
    #  SECTION 2: GROSS LONG / SHORT / NET EXPOSURE OVER TIME
    # ═════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 2: EXPOSURE OVER TIME")
    print("=" * 120)

    if snaps:
        print(f"\n  Gross exposure (long + short count):")
        print(f"    Max gross:   {max(all_gross)}")
        print(f"    Avg gross:   {statistics.mean(all_gross):.2f}")
        print(f"    P95 gross:   {sorted(all_gross)[int(len(all_gross)*0.95)]}")

        print(f"\n  Net exposure (long - short count):")
        print(f"    Mean net:    {statistics.mean(all_net):+.2f}")
        print(f"    Min net:     {min(all_net):+d} (most short-heavy)")
        print(f"    Max net:     {max(all_net):+d} (most long-heavy)")
        print(f"    Std net:     {statistics.stdev(all_net) if len(all_net) > 1 else 0:.2f}")

        # Per-day net summary
        day_nets = defaultdict(list)
        for s in snaps:
            day_nets[s["date"]].append(s["net"])
        print(f"\n  Daily net exposure (end-of-day snapshot, last bar):")
        day_end_nets = []
        for d in sorted(day_nets.keys()):
            day_end_nets.append(day_nets[d][-1])
        if day_end_nets:
            print(f"    Mean EOD net: {statistics.mean(day_end_nets):+.2f}")
            print(f"    Days net long:  {sum(1 for n in day_end_nets if n > 0)}")
            print(f"    Days net short: {sum(1 for n in day_end_nets if n < 0)}")
            print(f"    Days flat:      {sum(1 for n in day_end_nets if n == 0)}")

    # ═════════════════════════════════════════════════════════════
    #  SECTION 3: PER-SYMBOL CONCENTRATION
    # ═════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 3: PER-SYMBOL CONCENTRATION")
    print("=" * 120)

    sym_counts = defaultdict(lambda: {"n": 0, "long": 0, "short": 0, "pnl": 0.0})
    for t in filtered:
        sym_counts[t.sym]["n"] += 1
        sym_counts[t.sym][t.side.lower()] += 1
        sym_counts[t.sym]["pnl"] += t.pnl_points

    sorted_syms = sorted(sym_counts.items(), key=lambda x: x[1]["n"], reverse=True)
    print(f"\n  Total symbols traded: {len(sym_counts)}")
    print(f"  Avg trades per symbol: {len(filtered)/len(sym_counts):.1f}")
    top_5_pnl = sum(s[1]["pnl"] for s in sorted_syms[:5])
    print(f"  Top 5 symbols PnL concentration: {top_5_pnl:+.2f} "
          f"({top_5_pnl/compute_metrics(filtered)['pnl']*100:.0f}% of total)")

    print(f"\n  {'Sym':<8}  {'N':>4}  {'L':>3}  {'S':>3}  {'PnL':>8}  {'PF':>6}")
    print(f"  {'-'*8}  {'-'*4}  {'-'*3}  {'-'*3}  {'-'*8}  {'-'*6}")
    for sym, stats in sorted_syms[:15]:
        sym_trades = [t for t in filtered if t.sym == sym]
        m = compute_metrics(sym_trades)
        print(f"  {sym:<8}  {stats['n']:4d}  {stats['long']:3d}  {stats['short']:3d}  "
              f"{stats['pnl']:+8.2f}  {pf_str(m['pf']):>6s}")

    # ═════════════════════════════════════════════════════════════
    #  SECTION 4: WORST DAY / WORST WEEK
    # ═════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 4: WORST DAY / WORST WEEK")
    print("=" * 120)

    daily_stats = compute_daily_stats(filtered)
    weekly_stats = compute_weekly_stats(filtered)

    sorted_days = sorted(daily_stats.items(), key=lambda x: x[1]["pnl"])
    print(f"\n  5 worst days:")
    print(f"    {'Date':<12}  {'N':>3}  {'PnL':>8}  {'Stops':>5}  {'L':>3}  {'S':>3}  Regime")
    print(f"    {'-'*12}  {'-'*3}  {'-'*8}  {'-'*5}  {'-'*3}  {'-'*3}  {'-'*15}")
    for d, st in sorted_days[:5]:
        regime_info = spy_day_info.get(d, {})
        regime = f"{regime_info.get('direction', 'UNK')}+{regime_info.get('character', 'UNK')}"
        print(f"    {d}  {st['n']:3d}  {st['pnl']:+8.2f}  {st['stops']:5d}  "
              f"{st['n_long']:3d}  {st['n_short']:3d}  {regime}")

    print(f"\n  5 best days:")
    for d, st in sorted_days[-5:]:
        regime_info = spy_day_info.get(d, {})
        regime = f"{regime_info.get('direction', 'UNK')}+{regime_info.get('character', 'UNK')}"
        print(f"    {d}  {st['n']:3d}  {st['pnl']:+8.2f}  {st['stops']:5d}  "
              f"{st['n_long']:3d}  {st['n_short']:3d}  {regime}")

    sorted_weeks = sorted(weekly_stats.items(), key=lambda x: x[1]["pnl"])
    print(f"\n  3 worst weeks:")
    print(f"    {'Week':<10}  {'N':>4}  {'PnL':>8}")
    print(f"    {'-'*10}  {'-'*4}  {'-'*8}")
    for wk, st in sorted_weeks[:3]:
        print(f"    {wk:<10}  {st['n']:4d}  {st['pnl']:+8.2f}")

    print(f"\n  3 best weeks:")
    for wk, st in sorted_weeks[-3:]:
        print(f"    {wk:<10}  {st['n']:4d}  {st['pnl']:+8.2f}")

    # Daily PnL distribution
    daily_pnls = [st["pnl"] for st in daily_stats.values()]
    if daily_pnls:
        print(f"\n  Daily PnL distribution:")
        print(f"    Mean:   {statistics.mean(daily_pnls):+.2f}")
        print(f"    Median: {statistics.median(daily_pnls):+.2f}")
        print(f"    Stdev:  {statistics.stdev(daily_pnls):.2f}" if len(daily_pnls) > 1 else "")
        print(f"    Win days:  {sum(1 for p in daily_pnls if p > 0)}/{len(daily_pnls)} "
              f"({sum(1 for p in daily_pnls if p > 0)/len(daily_pnls)*100:.0f}%)")
        print(f"    Losing days: {sum(1 for p in daily_pnls if p <= 0)}/{len(daily_pnls)}")

    # ═════════════════════════════════════════════════════════════
    #  SECTION 5: DAILY STOP FREQUENCY
    # ═════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 5: DAILY STOP FREQUENCY")
    print("=" * 120)

    daily_stops = [(d, st["stops"], st["n"]) for d, st in daily_stats.items()]
    daily_stops.sort(key=lambda x: x[1], reverse=True)

    stop_rates = [st["stops"] / st["n"] * 100 if st["n"] > 0 else 0
                  for st in daily_stats.values()]
    total_stops = sum(1 for t in filtered if t.exit_reason == "stop")
    total_qstops = sum(1 for t in filtered if t.exit_reason == "stop" and t.bars_held <= 3)

    print(f"\n  Overall stop rate: {total_stops}/{len(filtered)} ({total_stops/len(filtered)*100:.1f}%)")
    print(f"  Quick-stop rate (<=3 bars): {total_qstops}/{len(filtered)} ({total_qstops/len(filtered)*100:.1f}%)")
    print(f"  Avg daily stop rate: {statistics.mean(stop_rates):.1f}%")

    print(f"\n  Days with most stops:")
    print(f"    {'Date':<12}  {'Stops':>5}  {'Trades':>6}  {'Rate':>5}")
    print(f"    {'-'*12}  {'-'*5}  {'-'*6}  {'-'*5}")
    for d, stops, n in daily_stops[:8]:
        print(f"    {d}  {stops:5d}  {n:6d}  {stops/n*100:4.0f}%")

    # Stop distribution by side
    long_stops = sum(1 for t in longs if t.exit_reason == "stop")
    short_stops = sum(1 for t in shorts if t.exit_reason == "stop")
    print(f"\n  Stop rate by side:")
    print(f"    Long:  {long_stops}/{len(longs)} ({long_stops/len(longs)*100:.1f}%)" if longs else "")
    print(f"    Short: {short_stops}/{len(shorts)} ({short_stops/len(shorts)*100:.1f}%)" if shorts else "")

    # ═════════════════════════════════════════════════════════════
    #  SECTION 6: CONTRIBUTION BY SETUP AND REGIME
    # ═════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 6: CONTRIBUTION BY SETUP AND REGIME")
    print("=" * 120)

    # By setup
    setup_groups = defaultdict(list)
    for t in filtered:
        name = SETUP_DISPLAY_NAME.get(t.setup, str(t.setup))
        setup_groups[name].append(t)

    print(f"\n  By setup:")
    print(f"    {'Setup':<16}  {'N':>4}  {'WR':>6}  {'PF':>6}  {'PnL':>8}  {'MaxDD':>7}  {'StpR':>5}")
    print(f"    {'-'*16}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*7}  {'-'*5}")
    for name in sorted(setup_groups.keys()):
        m = compute_metrics(setup_groups[name])
        print(f"    {name:<16}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  "
              f"{m['pnl']:+8.2f}  {m['max_dd']:7.2f}  {m['stop_rate']:4.1f}%")

    # By regime
    regimes = ["GREEN+TREND", "GREEN+CHOPPY", "RED+TREND", "RED+CHOPPY",
               "FLAT+TREND", "FLAT+CHOPPY"]

    print(f"\n  By regime (combined):")
    print(f"    {'Regime':<16}  {'N':>4}  {'WR':>6}  {'PF':>6}  {'PnL':>8}  {'L':>3}  {'S':>3}")
    print(f"    {'-'*16}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*3}  {'-'*3}")
    for regime in regimes:
        rg = [t for t in filtered if get_regime(t) == regime]
        if not rg:
            continue
        m = compute_metrics(rg)
        nl = sum(1 for t in rg if t.side == "LONG")
        ns = sum(1 for t in rg if t.side == "SHORT")
        print(f"    {regime:<16}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  "
              f"{m['pnl']:+8.2f}  {nl:3d}  {ns:3d}")

    # ═════════════════════════════════════════════════════════════
    #  SECTION 7: PORTFOLIO-CONTROL VARIANT COMPARISON
    # ═════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 7: PORTFOLIO-CONTROL VARIANT COMPARISON")
    print("=" * 120)

    variants = [
        ("No cap (baseline)",        {"max_positions": None, "max_gross": None, "max_daily_loss_rr": None}),
        ("Max 5 positions",          {"max_positions": 5,    "max_gross": None, "max_daily_loss_rr": None}),
        ("Max 8 positions",          {"max_positions": 8,    "max_gross": None, "max_daily_loss_rr": None}),
        ("Max 10 gross exposure",    {"max_positions": None, "max_gross": 10,   "max_daily_loss_rr": None}),
        ("Daily loss stop -5R",      {"max_positions": None, "max_gross": None, "max_daily_loss_rr": -5.0}),
        ("Max 5 pos + daily -5R",    {"max_positions": 5,    "max_gross": None, "max_daily_loss_rr": -5.0}),
        ("Max 8 pos + daily -5R",    {"max_positions": 8,    "max_gross": None, "max_daily_loss_rr": -5.0}),
    ]

    print(f"\n  {'Variant':<28}  {'N':>4}  {'WR':>6}  {'PF':>6}  {'Exp':>6}  "
          f"{'PnL':>8}  {'MaxDD':>7}  {'Rejected':>8}  {'PnL/DD':>7}")
    print(f"  {'-'*28}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*6}  "
          f"{'-'*8}  {'-'*7}  {'-'*8}  {'-'*7}")

    for name, params in variants:
        capped = simulate_portfolio_cap(filtered, **params)
        m = compute_metrics(capped)
        rejected = len(filtered) - len(capped)
        pnl_dd = m["pnl"] / m["max_dd"] if m["max_dd"] > 0 else float("inf")
        print(f"  {name:<28}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  "
              f"{m['exp']:+.2f}R  {m['pnl']:+8.2f}  {m['max_dd']:7.2f}  "
              f"{rejected:8d}  {pnl_dd:7.2f}")

    # Detailed breakdown of best variant vs baseline
    print(f"\n  Detailed comparison: No cap vs Max 5 pos + daily -5R")
    baseline = filtered
    best = simulate_portfolio_cap(filtered, max_positions=5, max_daily_loss_rr=-5.0)

    for label, tlist in [("No cap", baseline), ("Max 5 + DLS", best)]:
        l = [t for t in tlist if t.side == "LONG"]
        s = [t for t in tlist if t.side == "SHORT"]
        ml = compute_metrics(l)
        ms = compute_metrics(s)
        mc = compute_metrics(tlist)
        ws = compute_weekly_stats(tlist)
        weeks_pos = sum(1 for w in ws.values() if w["pnl"] > 0)
        ds = compute_daily_stats(tlist)
        worst_day = min(d["pnl"] for d in ds.values()) if ds else 0
        print(f"\n    {label}:")
        print(f"      Long:     N={ml['n']:4d}  PF={pf_str(ml['pf'])}  PnL={ml['pnl']:+8.2f}")
        print(f"      Short:    N={ms['n']:4d}  PF={pf_str(ms['pf'])}  PnL={ms['pnl']:+8.2f}")
        print(f"      Combined: N={mc['n']:4d}  PF={pf_str(mc['pf'])}  PnL={mc['pnl']:+8.2f}  "
              f"MaxDD={mc['max_dd']:.2f}")
        print(f"      Worst day: {worst_day:+.2f}  |  Weeks pos: {weeks_pos}/{len(ws)}")

    # ═════════════════════════════════════════════════════════════
    #  SECTION 8: EQUITY CURVE / DRAWDOWN PROFILE
    # ═════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 8: DRAWDOWN PROFILE")
    print("=" * 120)

    sorted_trades = sorted(filtered, key=lambda t: t.entry_time)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_start = None
    max_dd_end = None
    dd_start = sorted_trades[0].entry_time if sorted_trades else None
    cum_series = []

    for t in sorted_trades:
        cum += t.pnl_points
        cum_series.append((t.entry_time, cum))
        if cum > peak:
            peak = cum
            dd_start = t.entry_time
        if peak - cum > max_dd:
            max_dd = peak - cum
            max_dd_start = dd_start
            max_dd_end = t.entry_time

    print(f"\n  Max drawdown: {max_dd:.2f} points")
    if max_dd_start and max_dd_end:
        print(f"  DD period: {max_dd_start.date()} → {max_dd_end.date()}")
    print(f"  Final cumulative PnL: {cum:.2f}")
    print(f"  PnL / MaxDD ratio: {cum/max_dd:.2f}" if max_dd > 0 else "")

    # Drawdown recovery
    in_dd = False
    dd_periods = []
    dd_st = None
    for ts, cv in cum_series:
        if cv < peak and not in_dd:
            in_dd = True
            dd_st = ts
        elif cv >= peak and in_dd:
            in_dd = False
            if dd_st:
                dd_periods.append((dd_st, ts))

    if dd_periods:
        durations = [(e - s).days for s, e in dd_periods]
        print(f"\n  Drawdown episodes: {len(dd_periods)}")
        print(f"  Avg recovery (calendar days): {statistics.mean(durations):.0f}")
        print(f"  Max recovery (calendar days): {max(durations)}")

    # ═════════════════════════════════════════════════════════════
    #  VERDICT
    # ═════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("PORTFOLIO TRADABILITY VERDICT")
    print("=" * 120)

    m_all = compute_metrics(filtered)
    daily_pnls = [st["pnl"] for st in daily_stats.values()]
    worst_day_pnl = min(daily_pnls) if daily_pnls else 0
    weekly_pnls = [st["pnl"] for st in weekly_stats.values()]
    worst_week_pnl = min(weekly_pnls) if weekly_pnls else 0
    weeks_pos_count = sum(1 for p in weekly_pnls if p > 0)
    days_pos_count = sum(1 for p in daily_pnls if p > 0)
    max_concurrent = max(all_total) if snaps else 0

    checks = [
        ("PF >= 1.5",                    m_all["pf"] >= 1.5),
        ("Max drawdown < 20",            m_all["max_dd"] < 20),
        ("PnL/MaxDD >= 3.0",             (m_all["pnl"] / m_all["max_dd"] >= 3.0) if m_all["max_dd"] > 0 else True),
        ("Worst day > -10",              worst_day_pnl > -10),
        ("Worst week > -15",             worst_week_pnl > -15),
        ("Win days > 45%",               days_pos_count / len(daily_pnls) * 100 > 45 if daily_pnls else False),
        ("Win weeks > 50%",              weeks_pos_count / len(weekly_pnls) * 100 > 50 if weekly_pnls else False),
        ("Max concurrent <= 15",         max_concurrent <= 15),
        ("Stop rate < 40%",              m_all["stop_rate"] < 40),
        ("Short book PF > 1.0",          compute_metrics(shorts)["pf"] > 1.0),
    ]

    all_pass = True
    for name, passed in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {name}")

    print(f"\n  Summary: {m_all['n']} trades, PF={pf_str(m_all['pf'])}, "
          f"PnL={m_all['pnl']:+.2f}, MaxDD={m_all['max_dd']:.2f}, "
          f"Worst day={worst_day_pnl:+.2f}, Max concurrent={max_concurrent}")

    if all_pass:
        print(f"\n  ALL CHECKS PASS. System is portfolio-tradable.")
    else:
        print(f"\n  NOT ALL CHECKS PASS. Review failures before declaring tradable.")


if __name__ == "__main__":
    main()
