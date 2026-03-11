"""
Tradability Model Validation — fixed candidate model, no tuning.

Candidate model:
  - Longs: tradability_long >= 0.0
  - Shorts: tradability_short <= -0.1 (i.e. short_score <= +0.1 inverted)
  - Plus base filters: Q>=2, <15:30

Validation sections:
  1. Combined-system comparison using corrected execution framework
  2. Setup-level attribution (VK longs, SC longs, short setups)
  3. Walk-forward & train/test stability
  4. Trade count sufficiency analysis
  5. Per-trade log for the filtered short set

Usage:
    python -m alert_overlay.tradability_validation --universe all94
"""

import argparse
import math
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import List, Dict, Tuple
from zoneinfo import ZoneInfo

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import NaN, SetupId, SetupFamily, SETUP_DISPLAY_NAME, SETUP_FAMILY_MAP

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent / "data"
WATCHLIST_FILE = Path(__file__).parent / "watchlist.txt"

# ── Candidate thresholds (fixed — no tuning) ──
LONG_THRESHOLD = 0.0
SHORT_THRESHOLD = 0.1  # shorts require tradability_short <= -0.1


def load_watchlist(path=WATCHLIST_FILE):
    symbols = []
    with open(path) as f:
        for line in f:
            sym = line.strip().upper()
            if sym and not sym.startswith("#"):
                symbols.append(sym)
    return symbols


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
        if day_range > 0:
            close_pos = (day_close - day_low) / day_range
            character = "TREND" if (close_pos >= 0.75 or close_pos <= 0.25) else "CHOPPY"
        else:
            character = "CHOPPY"
        ranges_10d.append(day_range)
        day_info[d] = {"direction": direction, "character": character, "spy_change_pct": change_pct}
    return day_info


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
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    cum = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: t.signal.timestamp):
        cum += t.pnl_points
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum
    avg_hold = statistics.mean(t.bars_held for t in trades)
    td = len(set(t.signal.timestamp.date() for t in trades))
    weeks = (n_days or td) / 5.0
    return {"n": n, "wr": len(wins) / n * 100, "pf": pf, "pnl": pnl,
            "exp": sum(t.pnl_rr for t in trades) / n if n > 0 else 0,
            "stop_rate": stopped / n * 100, "max_dd": dd,
            "avg_hold": avg_hold, "tpw": n / weeks if weeks > 0 else 0}


def print_row(label, m, show_hold=True):
    hold_str = f" {m['avg_hold']:>5.1f}" if show_hold else ""
    print(f"  {label:<40s} │ {m['n']:>4} {m['tpw']:>5.1f} {m['wr']:>5.1f}% {pf_str(m['pf']):>6s} "
          f"{m['exp']:>+6.2f}R {m['pnl']:>+8.2f} {m['max_dd']:>6.1f} "
          f"{m['stop_rate']:>5.0f}%{hold_str}")


def print_header(show_hold=True):
    hold_h = " AvgH " if show_hold else ""
    hold_d = "─" * 6 if show_hold else ""
    print(f"\n  {'Variant':<40s} │ {'N':>4} {'T/Wk':>5} {'WR%':>5} {'PF':>6} "
          f"{'Exp':>7} {'PnL':>8} {'MaxDD':>6} {'StpR':>5}{hold_h}")
    print(f"  {'─' * 40}─┼{'─' * 52}{hold_d}")


# ── Filter functions ──

def apply_old_locked(trades, spy_day_info):
    """Old baseline: Non-RED + Q>=2 + <15:30, longs only."""
    out = []
    for t in trades:
        if t.signal.direction != 1:
            continue
        d = t.signal.timestamp.date()
        info = spy_day_info.get(d, {})
        if info.get("direction") == "RED":
            continue
        if t.signal.quality_score < 2:
            continue
        hhmm = t.signal.timestamp.hour * 100 + t.signal.timestamp.minute
        if hhmm >= 1530:
            continue
        out.append(t)
    return out


def apply_tradability_filter(trades, long_thresh, short_thresh):
    """Tradability-based: Q>=2 + <15:30 + score gate."""
    out = []
    for t in trades:
        if t.signal.quality_score < 2:
            continue
        hhmm = t.signal.timestamp.hour * 100 + t.signal.timestamp.minute
        if hhmm >= 1530:
            continue
        if t.signal.direction == 1:
            if t.signal.tradability_long < long_thresh:
                continue
        else:
            if t.signal.tradability_short > -short_thresh:
                continue
        out.append(t)
    return out


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="all94")
    args = parser.parse_args()

    if args.universe == "all94":
        symbols = load_watchlist()
    else:
        symbols = [s.strip().upper() for s in args.universe.split(",")]

    print(f"{'=' * 100}")
    print(f"  TRADABILITY MODEL VALIDATION — FIXED CANDIDATE")
    print(f"  Long threshold: tradability_long >= {LONG_THRESHOLD:+.2f}")
    print(f"  Short threshold: tradability_short <= {-SHORT_THRESHOLD:+.2f}")
    print(f"{'=' * 100}")

    # ── Load market data ──
    from .market_context import SECTOR_MAP, get_sector_etf

    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    spy_day_info = classify_spy_days(spy_bars)

    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    all_dates = sorted(spy_day_info.keys())
    n_days = len(all_dates)
    red_days = sum(1 for info in spy_day_info.values() if info["direction"] == "RED")
    green_days = sum(1 for info in spy_day_info.values() if info["direction"] == "GREEN")
    flat_days = n_days - red_days - green_days

    print(f"\n  Period: {min(all_dates)} → {max(all_dates)} ({n_days} days)")
    print(f"  Universe: {len(symbols)} symbols")
    print(f"  SPY days: {green_days} GREEN, {red_days} RED, {flat_days} FLAT\n")

    # ── Run backtest: all setups, both directions, no tradability gating ──
    cfg = OverlayConfig(
        show_reversal_setups=False,
        show_trend_setups=True,
        show_ema_retest=False,
        show_ema_mean_rev=False,
        show_ema_pullback=False,
        show_second_chance=True,
        show_spencer=True,
        show_failed_bounce=True,
        show_ema_scalp=True,
        show_ema_fpip=True,
        show_sc_v2=True,
        vk_long_only=False,
        sc_long_only=False,
        sp_long_only=False,
        use_market_context=True,
        use_sector_context=True,
        use_tradability_gate=False,
    )

    all_trades: List[Trade] = []
    symbol_for_trade: Dict[int, str] = {}  # trade id → symbol

    for sym in symbols:
        fpath = DATA_DIR / f"{sym}_5min.csv"
        if not fpath.exists():
            continue
        bars = load_bars_from_csv(str(fpath))
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        for t in result.trades:
            symbol_for_trade[id(t)] = sym
        all_trades.extend(result.trades)

    print(f"  Raw trades (all setups, both directions): {len(all_trades)}")

    # ── Build filter variants ──
    old_locked = apply_old_locked(all_trades, spy_day_info)
    trad_longs = apply_tradability_filter(
        [t for t in all_trades if t.signal.direction == 1], LONG_THRESHOLD, 999)
    trad_shorts = apply_tradability_filter(
        [t for t in all_trades if t.signal.direction == -1], -999, SHORT_THRESHOLD)
    trad_combined = trad_longs + trad_shorts

    # Old + RED shorts for reference
    old_red_shorts = [t for t in all_trades if t.signal.direction == -1
                      and spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == "RED"
                      and t.signal.quality_score >= 2
                      and t.signal.timestamp.hour * 100 + t.signal.timestamp.minute < 1530]

    # ══════════════════════════════════════════════════════════════
    #  SECTION 1: COMBINED-SYSTEM COMPARISON
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  SECTION 1: COMBINED-SYSTEM COMPARISON")
    print(f"{'=' * 100}")

    print_header()
    print_row("A: Old locked (Non-RED longs only)", compute_metrics(old_locked, n_days))
    print_row("B: Old + RED shorts", compute_metrics(old_locked + old_red_shorts, n_days))
    print_row("C: Trad longs (>= 0.0)", compute_metrics(trad_longs, n_days))
    print_row("D: Trad shorts (<= -0.1)", compute_metrics(trad_shorts, n_days))
    print_row("E: Trad combined (C + D)", compute_metrics(trad_combined, n_days))

    # Compare what tradability adds/removes vs old
    old_set = set(id(t) for t in old_locked)
    trad_long_set = set(id(t) for t in trad_longs)
    rescued = [t for t in trad_longs if id(t) not in old_set]
    dropped = [t for t in old_locked if id(t) not in trad_long_set]

    print(f"\n  LONGS DELTA vs old locked baseline:")
    print(f"    Trades in old locked but NOT in trad longs (dropped): {len(dropped)}")
    if dropped:
        m = compute_metrics(dropped)
        print(f"      → WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  PnL={m['pnl']:+.2f}")
        for t in sorted(dropped, key=lambda t: t.signal.timestamp):
            d = t.signal.timestamp.date()
            dc = spy_day_info.get(d, {}).get("direction", "?")
            sym = symbol_for_trade.get(id(t), "?")
            print(f"        {d} {t.signal.timestamp.strftime('%H:%M')} {sym:>6s} "
                  f"{t.signal.setup_name:>14s}  trad_L={t.signal.tradability_long:+.3f}  "
                  f"PnL={t.pnl_points:+.2f}  {dc}")

    print(f"\n    Trades in trad longs but NOT in old locked (rescued): {len(rescued)}")
    if rescued:
        m = compute_metrics(rescued)
        print(f"      → WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  PnL={m['pnl']:+.2f}")
        for t in sorted(rescued, key=lambda t: t.signal.timestamp):
            d = t.signal.timestamp.date()
            dc = spy_day_info.get(d, {}).get("direction", "?")
            sym = symbol_for_trade.get(id(t), "?")
            print(f"        {d} {t.signal.timestamp.strftime('%H:%M')} {sym:>6s} "
                  f"{t.signal.setup_name:>14s}  trad_L={t.signal.tradability_long:+.3f}  "
                  f"PnL={t.pnl_points:+.2f}  {dc}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 2: SETUP-LEVEL ATTRIBUTION
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  SECTION 2: SETUP-LEVEL ATTRIBUTION")
    print(f"{'=' * 100}")

    # Longs by setup
    print(f"\n  LONGS passing tradability gate (>= {LONG_THRESHOLD:+.2f}):")
    print_header(show_hold=False)

    setup_groups_long = defaultdict(list)
    for t in trad_longs:
        setup_groups_long[t.signal.setup_name].append(t)

    for setup_name in sorted(setup_groups_long.keys()):
        trades = setup_groups_long[setup_name]
        print_row(f"  {setup_name}", compute_metrics(trades, n_days), show_hold=False)
    print_row("  ALL LONGS", compute_metrics(trad_longs, n_days), show_hold=False)

    # Shorts by setup
    print(f"\n  SHORTS passing tradability gate (<= {-SHORT_THRESHOLD:+.2f}):")
    print_header(show_hold=False)

    setup_groups_short = defaultdict(list)
    for t in trad_shorts:
        setup_groups_short[t.signal.setup_name].append(t)

    for setup_name in sorted(setup_groups_short.keys()):
        trades = setup_groups_short[setup_name]
        print_row(f"  {setup_name}", compute_metrics(trades, n_days), show_hold=False)
    print_row("  ALL SHORTS", compute_metrics(trad_shorts, n_days), show_hold=False)

    # Day-color breakdown for each direction
    print(f"\n  LONG attribution by SPY day color:")
    for color in ["GREEN", "RED", "FLAT"]:
        subset = [t for t in trad_longs
                  if spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == color]
        if subset:
            m = compute_metrics(subset)
            print(f"    {color:6s}  N={m['n']:3d}  WR={m['wr']:5.1f}%  PF={pf_str(m['pf']):>6s}  PnL={m['pnl']:+7.2f}")

    print(f"\n  SHORT attribution by SPY day color:")
    for color in ["GREEN", "RED", "FLAT"]:
        subset = [t for t in trad_shorts
                  if spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == color]
        if subset:
            m = compute_metrics(subset)
            print(f"    {color:6s}  N={m['n']:3d}  WR={m['wr']:5.1f}%  PF={pf_str(m['pf']):>6s}  PnL={m['pnl']:+7.2f}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 3: WALK-FORWARD & TRAIN/TEST STABILITY
    # ══════════════════════════════════════════════════════════════
    split_date = date(2026, 2, 21)

    print(f"\n{'=' * 100}")
    print(f"  SECTION 3: WALK-FORWARD STABILITY (train < {split_date}, test ≥ {split_date})")
    print(f"{'=' * 100}")

    train_dates = [d for d in all_dates if d < split_date]
    test_dates = [d for d in all_dates if d >= split_date]

    for label, trades in [("TRAD LONGS", trad_longs),
                           ("TRAD SHORTS", trad_shorts),
                           ("TRAD COMBINED", trad_combined),
                           ("OLD LOCKED (reference)", old_locked)]:
        train = [t for t in trades if t.signal.timestamp.date() < split_date]
        test = [t for t in trades if t.signal.timestamp.date() >= split_date]

        mt = compute_metrics(train, len(train_dates))
        ms = compute_metrics(test, len(test_dates))

        print(f"\n  {label}:")
        print(f"    {'Period':<12} │ {'N':>4} {'T/Wk':>5} {'WR%':>5} {'PF':>6} "
              f"{'Exp':>7} {'PnL':>8} {'MaxDD':>6} {'StpR':>5}")
        print(f"    {'─' * 12}─┼{'─' * 52}")
        for plabel, pm in [("Train", mt), ("Test", ms)]:
            if pm['n'] == 0:
                print(f"    {plabel:<12} │    0   — no trades")
            else:
                print(f"    {plabel:<12} │ {pm['n']:>4} {pm['tpw']:>5.1f} {pm['wr']:>5.1f}% {pf_str(pm['pf']):>6s} "
                      f"{pm['exp']:>+6.2f}R {pm['pnl']:>+8.2f} {pm['max_dd']:>6.1f} "
                      f"{pm['stop_rate']:>5.0f}%")

        # PF stability check
        if mt['n'] >= 3 and ms['n'] >= 3:
            pf_diff = abs((mt['pf'] if mt['pf'] < 999 else 10) - (ms['pf'] if ms['pf'] < 999 else 10))
            wr_diff = abs(mt['wr'] - ms['wr'])
            stable = pf_diff < 5.0 and wr_diff < 25.0
            flag = "✓ STABLE" if stable else "⚠ UNSTABLE"
            print(f"    {flag}  PF diff={pf_diff:.2f}  WR diff={wr_diff:.1f}pp")

    # Rolling window stability (2-week rolling)
    print(f"\n  ROLLING 2-WEEK WINDOWS (trad combined):")
    print(f"    {'Window':<25} │ {'N':>4} {'WR%':>5} {'PF':>6} {'PnL':>8}")
    print(f"    {'─' * 25}─┼{'─' * 28}")

    window_size = 10  # ~2 trading weeks
    for i in range(0, len(all_dates) - window_size + 1, 5):
        w_start = all_dates[i]
        w_end = all_dates[min(i + window_size - 1, len(all_dates) - 1)]
        w_trades = [t for t in trad_combined
                    if w_start <= t.signal.timestamp.date() <= w_end]
        if not w_trades:
            print(f"    {str(w_start)} → {str(w_end)} │    0   — no trades")
            continue
        wm = compute_metrics(w_trades)
        print(f"    {str(w_start)} → {str(w_end)} │ {wm['n']:>4} {wm['wr']:>5.1f}% "
              f"{pf_str(wm['pf']):>6s} {wm['pnl']:>+8.2f}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 4: TRADE COUNT SUFFICIENCY ANALYSIS
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  SECTION 4: TRADE COUNT SUFFICIENCY ANALYSIS")
    print(f"{'=' * 100}")

    for label, trades in [("Trad longs", trad_longs),
                           ("Trad shorts", trad_shorts),
                           ("Trad combined", trad_combined)]:
        n = len(trades)
        if n == 0:
            print(f"\n  {label}: 0 trades — insufficient")
            continue

        m = compute_metrics(trades, n_days)
        wins = sum(1 for t in trades if t.pnl_points > 0)
        wr = wins / n

        # Trades per week
        tpw = m['tpw']

        # Days with trades
        trade_days = len(set(t.signal.timestamp.date() for t in trades))

        # Consecutive losers
        sorted_trades = sorted(trades, key=lambda t: t.signal.timestamp)
        max_consec_loss = 0
        cur_consec = 0
        for t in sorted_trades:
            if t.pnl_points <= 0:
                cur_consec += 1
                max_consec_loss = max(max_consec_loss, cur_consec)
            else:
                cur_consec = 0

        # Win rate confidence interval (Wilson score interval)
        import math as m_mod
        z = 1.96  # 95% CI
        denom = 1 + z * z / n
        center = (wr + z * z / (2 * n)) / denom
        spread = z * m_mod.sqrt((wr * (1 - wr) + z * z / (4 * n)) / n) / denom
        ci_low = max(0, center - spread)
        ci_high = min(1, center + spread)

        # Profit factor bootstrap (simple: resample 1000x)
        import random
        random.seed(42)
        pf_samples = []
        pnl_list = [t.pnl_points for t in trades]
        for _ in range(1000):
            sample = random.choices(pnl_list, k=n)
            gw = sum(x for x in sample if x > 0)
            gl = abs(sum(x for x in sample if x <= 0))
            pf_samples.append(gw / gl if gl > 0 else 10.0)
        pf_samples.sort()
        pf_5 = pf_samples[49]    # 5th percentile
        pf_50 = pf_samples[499]  # median
        pf_95 = pf_samples[949]

        # Is PF > 1.0 at 95% confidence?
        pf_robust = pf_5 > 1.0

        # Expected trades per month (21 trading days)
        trades_per_month = tpw * 4.33

        print(f"\n  {label} (N={n}):")
        print(f"    Trade frequency:      {tpw:.1f} trades/week  ({trades_per_month:.0f}/month)")
        print(f"    Active trade days:    {trade_days} / {n_days} ({trade_days/n_days*100:.0f}%)")
        print(f"    Win rate:             {wr*100:.1f}%  95% CI: [{ci_low*100:.1f}%, {ci_high*100:.1f}%]")
        print(f"    Max consec losers:    {max_consec_loss}")
        print(f"    PF bootstrap:         5th={pf_5:.2f}  50th={pf_50:.2f}  95th={pf_95:.2f}")
        print(f"    PF > 1.0 at 95% CI:  {'YES ✓' if pf_robust else 'NO ⚠'}")
        print(f"    Max drawdown:         {m['max_dd']:.2f} pts")

        # Minimum sample assessment
        min_n = 30  # standard minimum for statistical significance
        if n < min_n:
            print(f"    ⚠ SAMPLE SIZE WARNING: {n} < {min_n} minimum recommended trades")
            print(f"      Need ~{min_n - n} more trades for reliable inference")
            print(f"      At {tpw:.1f} trades/week, need ~{(min_n - n) / max(tpw, 0.1):.0f} more weeks of data")
        else:
            print(f"    ✓ Sample size adequate ({n} >= {min_n})")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 5: FILTERED SHORT TRADE LOG
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  SECTION 5: FILTERED SHORT TRADE LOG")
    print(f"{'=' * 100}")

    sorted_shorts = sorted(trad_shorts, key=lambda t: t.signal.timestamp)
    print(f"\n  {'Date':>10s}  {'Time':>5s}  {'Symbol':>6s}  {'Setup':>14s}  {'Q':>2s}  "
          f"{'TradS':>7s}  {'RS_M':>6s}  {'RS_S':>6s}  {'Entry':>7s}  {'PnL':>7s}  "
          f"{'Exit':>6s}  {'Bars':>4s}  {'Day':>6s}")
    print(f"  {'─' * 10}  {'─' * 5}  {'─' * 6}  {'─' * 14}  {'─' * 2}  "
          f"{'─' * 7}  {'─' * 6}  {'─' * 6}  {'─' * 7}  {'─' * 7}  "
          f"{'─' * 6}  {'─' * 4}  {'─' * 6}")

    for t in sorted_shorts:
        d = t.signal.timestamp.date()
        dc = spy_day_info.get(d, {}).get("direction", "?")
        sym = symbol_for_trade.get(id(t), "?")
        rs_m = t.signal.rs_market
        rs_s = t.signal.rs_sector
        rs_m_str = f"{rs_m:+.2f}" if not math.isnan(rs_m) else "  NaN"
        rs_s_str = f"{rs_s:+.2f}" if not math.isnan(rs_s) else "  NaN"
        print(f"  {str(d):>10s}  {t.signal.timestamp.strftime('%H:%M'):>5s}  {sym:>6s}  "
              f"{t.signal.setup_name:>14s}  {t.signal.quality_score:>2d}  "
              f"{t.signal.tradability_short:+7.3f}  {rs_m_str:>6s}  {rs_s_str:>6s}  "
              f"{t.signal.entry_price:>7.2f}  {t.pnl_points:+7.2f}  "
              f"{t.exit_reason:>6s}  {t.bars_held:>4d}  {dc:>6s}")

    # ══════════════════════════════════════════════════════════════
    #  SUMMARY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  VALIDATION SUMMARY")
    print(f"{'=' * 100}")

    m_old = compute_metrics(old_locked, n_days)
    m_trad = compute_metrics(trad_combined, n_days)

    print(f"\n  Old locked baseline:     N={m_old['n']:3d}  PF={pf_str(m_old['pf']):>6s}  "
          f"PnL={m_old['pnl']:+8.2f}  WR={m_old['wr']:.1f}%  MaxDD={m_old['max_dd']:.1f}")
    print(f"  Tradability combined:    N={m_trad['n']:3d}  PF={pf_str(m_trad['pf']):>6s}  "
          f"PnL={m_trad['pnl']:+8.2f}  WR={m_trad['wr']:.1f}%  MaxDD={m_trad['max_dd']:.1f}")

    delta_pnl = m_trad['pnl'] - m_old['pnl']
    delta_n = m_trad['n'] - m_old['n']
    print(f"\n  Delta: {delta_n:+d} trades, {delta_pnl:+.2f} pts PnL")

    print(f"\n{'=' * 100}")
    print("  VALIDATION COMPLETE")
    print(f"{'=' * 100}")


if __name__ == "__main__":
    main()
