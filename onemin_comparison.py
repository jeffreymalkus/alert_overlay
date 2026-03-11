"""
1-min vs 5-min bar comparison — same 31 tickers, 3 configs:
  A. 5-min bars, current params (baseline)
  B. 1-min bars, params scaled ×5 (equivalent real-time behavior)
  C. 1-min bars, unscaled params (tighter windows)

Reports: trade count, WR, PF, exp, PnL, DD, stop rate, longs/shorts.
Also includes regime breakdown and short-side analysis.

Usage:
    python -m alert_overlay.onemin_comparison
"""

import argparse
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import NaN, SetupId, SETUP_DISPLAY_NAME

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent / "data"
ONEMIN_DIR = DATA_DIR / "1min"


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "∞"


def classify_spy_days(spy_bars):
    daily = defaultdict(list)
    for b in spy_bars:
        daily[b.timestamp.date()].append(b)
    day_info = {}
    ranges_10d = []
    for d in sorted(daily.keys()):
        bars = daily[d]
        day_open = bars[0].open
        day_close = bars[-1].close
        day_high = max(b.high for b in bars)
        day_low = min(b.low for b in bars)
        day_range = day_high - day_low
        change_pct = (day_close - day_open) / day_open * 100 if day_open > 0 else 0
        direction = "GREEN" if change_pct > 0.05 else ("RED" if change_pct < -0.05 else "FLAT")
        ranges_10d.append(day_range)
        if len(ranges_10d) > 10: ranges_10d.pop(0)
        avg_range = statistics.mean(ranges_10d) if ranges_10d else day_range
        volatility = "HIGH_VOL" if (len(ranges_10d) >= 5 and day_range > 1.5 * avg_range) else "NORMAL"
        if day_range > 0:
            cp = (day_close - day_low) / day_range
            character = "TREND" if (cp >= 0.75 or cp <= 0.25) else "CHOPPY"
        else:
            character = "CHOPPY"
        day_info[d] = {"direction": direction, "volatility": volatility, "character": character}
    return day_info


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
    avg_hold = statistics.mean(t.bars_held for t in trades)
    cum = pk = dd = 0.0
    for t in trades:
        cum += t.pnl_points
        if cum > pk: pk = cum
        if pk - cum > dd: dd = pk - cum
    td = len(set(str(t.signal.timestamp)[:10] for t in trades))
    weeks = (n_days or td) / 5.0
    return {"n": n, "wr": len(wins)/n*100, "pf": pf, "pnl": pnl,
            "exp": avg_rr, "stop_rate": stopped/n*100, "max_dd": dd,
            "avg_hold": avg_hold, "tpw": n/weeks if weeks > 0 else 0}


def collect_trades(bars_dict, cfg, spy_bars=None, qqq_bars=None, sector_bars_dict=None):
    from .market_context import get_sector_etf
    all_trades = []
    for sym, bars in bars_dict.items():
        sec_bars = None
        if sector_bars_dict and cfg.use_sector_context:
            sector_etf = get_sector_etf(sym)
            if sector_etf and sector_etf in sector_bars_dict:
                sec_bars = sector_bars_dict[sector_etf]
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        for t in result.trades:
            all_trades.append((sym, t))
    return all_trades


# ── config builders ──────────────────────────────────────────────────

def cfg_5min_baseline():
    """5-min locked baseline: VK + SC_VOL, shorts enabled for comparison."""
    c = OverlayConfig()
    c.show_ema_scalp = False
    c.show_failed_bounce = False
    c.show_spencer = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    # Keep shorts OFF to match current locked baseline
    return c


def cfg_5min_with_shorts():
    """5-min with shorts enabled."""
    c = cfg_5min_baseline()
    c.vk_long_only = False
    c.sc_long_only = False
    c.show_failed_bounce = True
    return c


def cfg_1min_scaled():
    """1-min bars, all bar-count params ×5 to match 5-min real-time behavior.
    Shorts enabled for full comparison."""
    c = OverlayConfig()
    c.show_ema_scalp = False
    c.show_failed_bounce = True    # enable for short evaluation
    c.show_spencer = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    c.vk_long_only = False
    c.sc_long_only = False

    # Scale bar-count params ×5
    SCALE = 5

    # ATR / vol lookback
    c.atr_len = 14 * SCALE               # 14 → 70
    c.vol_lookback = 20 * SCALE           # 20 → 100

    # SC windows
    c.sc_retest_window = 6 * SCALE        # 6 → 30
    c.sc_confirm_window = 3 * SCALE       # 3 → 15

    # SC_V2 windows (not active but scale for completeness)
    c.sc2_time_stop_bars = 16 * SCALE

    # Spencer
    c.sp_box_max_bars = 8 * SCALE         # 8 → 40

    # Failed Bounce
    c.fb_bounce_window = 6 * SCALE        # 6 → 30
    c.fb_confirm_window = 3 * SCALE       # 3 → 15
    c.fb_time_stop_bars = 8 * SCALE       # 8 → 40

    # EMA scalp
    c.ema_scalp_time_stop_bars = 20 * SCALE  # 20 → 100

    # Breakout (SC, Spencer)
    c.breakout_time_stop_bars = 20 * SCALE   # 20 → 100

    # FPIP (not active but scale)
    c.ema_fpip_time_stop_bars = 12 * SCALE

    return c


def cfg_1min_scaled_long_only():
    """1-min scaled, long-only (matches current baseline behavior)."""
    c = cfg_1min_scaled()
    c.vk_long_only = True
    c.sc_long_only = True
    c.show_failed_bounce = False
    return c


def cfg_1min_unscaled():
    """1-min bars, original params (tighter windows). Shorts enabled."""
    c = OverlayConfig()
    c.show_ema_scalp = False
    c.show_failed_bounce = True
    c.show_spencer = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    c.vk_long_only = False
    c.sc_long_only = False
    return c


def cfg_1min_unscaled_long_only():
    """1-min unscaled, long-only."""
    c = cfg_1min_unscaled()
    c.vk_long_only = True
    c.sc_long_only = True
    c.show_failed_bounce = False
    return c


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="1-min vs 5-min comparison")
    args = parser.parse_args()

    # Discover 1-min symbols
    onemin_syms = sorted(set(
        p.stem.replace("_1min", "")
        for p in ONEMIN_DIR.glob("*_1min.csv")
    ))
    print(f"1-min symbols: {len(onemin_syms)}")
    print(f"  {', '.join(onemin_syms)}")

    # Load 5-min bars for the same symbols
    bars_5min = {}
    for sym in onemin_syms:
        p = DATA_DIR / f"{sym}_5min.csv"
        if p.exists():
            bars_5min[sym] = load_bars_from_csv(str(p))

    # Load 1-min bars
    bars_1min = {}
    for sym in onemin_syms:
        p = ONEMIN_DIR / f"{sym}_1min.csv"
        if p.exists():
            bars_1min[sym] = load_bars_from_csv(str(p))

    # Market context: use matching frequency
    spy_5min = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv")) if (DATA_DIR / "SPY_5min.csv").exists() else None
    qqq_5min = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv")) if (DATA_DIR / "QQQ_5min.csv").exists() else None
    spy_1min = load_bars_from_csv(str(ONEMIN_DIR / "SPY_1min.csv")) if (ONEMIN_DIR / "SPY_1min.csv").exists() else None
    qqq_1min = load_bars_from_csv(str(ONEMIN_DIR / "QQQ_1min.csv")) if (ONEMIN_DIR / "QQQ_1min.csv").exists() else None

    # Sector ETFs — use 5-min for both (1-min sector may not exist)
    sector_bars_dict = {}
    from .market_context import SECTOR_MAP
    sector_etfs = set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    # Also load 1-min sector ETFs if available
    sector_bars_1min = {}
    for etf in sector_etfs:
        p = ONEMIN_DIR / f"{etf}_1min.csv"
        if p.exists():
            sector_bars_1min[etf] = load_bars_from_csv(str(p))

    # Use 5-min sectors for 1-min runs too (sector data mostly for context, not timing critical)
    # Unless 1-min sector data exists
    sector_for_1min = sector_bars_1min if sector_bars_1min else sector_bars_dict

    # SPY regime classification (use 5-min for consistency)
    spy_days = classify_spy_days(spy_5min) if spy_5min else {}

    # Only use symbols present in BOTH 5-min and 1-min
    common_syms = sorted(set(bars_5min.keys()) & set(bars_1min.keys()) - {"SPY", "QQQ"})
    bars_5min_common = {s: bars_5min[s] for s in common_syms if s in bars_5min}
    bars_1min_common = {s: bars_1min[s] for s in common_syms if s in bars_1min}

    print(f"Common symbols (excl SPY/QQQ): {len(common_syms)}")

    all_dates = sorted(set(
        b.timestamp.date() for bars in bars_5min_common.values() for b in bars
    ))
    n_days = len(all_dates)
    print(f"Period: {min(all_dates)} → {max(all_dates)} ({n_days} trading days)")

    for sym in common_syms[:3]:
        print(f"  {sym}: 5min={len(bars_5min_common[sym])} bars, 1min={len(bars_1min_common[sym])} bars")

    def trade_date(t):
        return t.signal.timestamp.date() if hasattr(t.signal.timestamp, "date") else None

    # ══════════════════════════════════════════════════════════════
    #  SECTION 1: LONG-ONLY HEAD-TO-HEAD (apples-to-apples)
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print("  SECTION 1: LONG-ONLY — 5-min vs 1-min (same 31 tickers)")
    print(f"{'='*110}")

    scenarios_longonly = [
        ("A. 5-min baseline",        bars_5min_common, cfg_5min_baseline(), spy_5min, qqq_5min, sector_bars_dict),
        ("B. 1-min scaled (×5)",     bars_1min_common, cfg_1min_scaled_long_only(), spy_1min, qqq_1min, sector_for_1min),
        ("C. 1-min unscaled",        bars_1min_common, cfg_1min_unscaled_long_only(), spy_1min, qqq_1min, sector_for_1min),
    ]

    hdr = (f"  {'Config':<25} │ {'N':>4} {'WR%':>5} {'PF':>5} {'Exp':>7} "
           f"{'PnL':>8} {'MaxDD':>5} {'StpR':>4} {'Hold':>6} {'T/wk':>4}")
    print(f"\n{hdr}")
    print(f"  {'─'*25}─┼{'─'*60}")

    longonly_results = {}
    for label, bars, cfg, spy, qqq, sec in scenarios_longonly:
        trades_raw = collect_trades(bars, cfg, spy_bars=spy, qqq_bars=qqq, sector_bars_dict=sec)
        trades = [t for _, t in trades_raw]
        m = compute_metrics(trades, n_days)
        longonly_results[label] = (trades_raw, trades, m)
        print(f"  {label:<25} │ {m['n']:>4} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
              f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['max_dd']:>5.1f} "
              f"{m['stop_rate']:>4.0f}% {m['avg_hold']:>6.1f} {m['tpw']:>4.1f}")

    # Setup breakdown for each
    print(f"\n  ── Setup Breakdown ──")
    for label, (trades_raw, trades, m) in longonly_results.items():
        by_setup = defaultdict(list)
        for sym, t in trades_raw:
            name = SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id))
            by_setup[name].append(t)
        parts = []
        for name in sorted(by_setup):
            s = compute_metrics(by_setup[name])
            parts.append(f"{name}: N={s['n']}, WR={s['wr']:.0f}%, PF={pf_str(s['pf'])}, Exp={s['exp']:+.3f}R")
        print(f"    {label}: {' │ '.join(parts)}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 2: WITH SHORTS — 5-min vs 1-min
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print("  SECTION 2: SHORTS ENABLED — 5-min vs 1-min")
    print(f"{'='*110}")

    scenarios_shorts = [
        ("A. 5-min + shorts",        bars_5min_common, cfg_5min_with_shorts(), spy_5min, qqq_5min, sector_bars_dict),
        ("B. 1-min scaled + shorts", bars_1min_common, cfg_1min_scaled(), spy_1min, qqq_1min, sector_for_1min),
        ("C. 1-min unscaled + shorts",bars_1min_common, cfg_1min_unscaled(), spy_1min, qqq_1min, sector_for_1min),
    ]

    print(f"\n  ── ALL trades ──")
    print(f"{hdr}")
    print(f"  {'─'*25}─┼{'─'*60}")

    all_results = {}
    for label, bars, cfg, spy, qqq, sec in scenarios_shorts:
        trades_raw = collect_trades(bars, cfg, spy_bars=spy, qqq_bars=qqq, sector_bars_dict=sec)
        trades = [t for _, t in trades_raw]
        all_results[label] = (trades_raw, trades)
        m = compute_metrics(trades, n_days)
        print(f"  {label:<25} │ {m['n']:>4} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
              f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['max_dd']:>5.1f} "
              f"{m['stop_rate']:>4.0f}% {m['avg_hold']:>6.1f} {m['tpw']:>4.1f}")

    # Longs vs shorts split
    print(f"\n  ── LONG trades ──")
    print(f"{hdr}")
    print(f"  {'─'*25}─┼{'─'*60}")
    for label, (trades_raw, trades) in all_results.items():
        lt = [t for t in trades if t.signal.direction == 1]
        m = compute_metrics(lt, n_days)
        print(f"  {label:<25} │ {m['n']:>4} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
              f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['max_dd']:>5.1f} "
              f"{m['stop_rate']:>4.0f}% {m['avg_hold']:>6.1f} {m['tpw']:>4.1f}")

    print(f"\n  ── SHORT trades ──")
    print(f"{hdr}")
    print(f"  {'─'*25}─┼{'─'*60}")
    for label, (trades_raw, trades) in all_results.items():
        st = [t for t in trades if t.signal.direction == -1]
        m = compute_metrics(st, n_days)
        if m['n'] > 0:
            print(f"  {label:<25} │ {m['n']:>4} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
                  f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['max_dd']:>5.1f} "
                  f"{m['stop_rate']:>4.0f}% {m['avg_hold']:>6.1f} {m['tpw']:>4.1f}")
        else:
            print(f"  {label:<25} │    0 trades")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 3: SHORTS BY REGIME — 1-min scaled
    # ══════════════════════════════════════════════════════════════
    if spy_days:
        print(f"\n{'='*110}")
        print("  SECTION 3: SHORT TRADES BY SPY REGIME — 1-min scaled")
        print(f"{'='*110}")

        # Use 1-min scaled as the primary comparison
        _, trades_1m_scaled = all_results.get("B. 1-min scaled + shorts", ([], []))
        shorts_1m = [t for t in trades_1m_scaled if t.signal.direction == -1]
        longs_1m = [t for t in trades_1m_scaled if t.signal.direction == 1]

        _, trades_5m = all_results.get("A. 5-min + shorts", ([], []))
        shorts_5m = [t for t in trades_5m if t.signal.direction == -1]

        for regime_type, regime_values in [
            ("Direction", ["GREEN", "RED", "FLAT"]),
            ("Character", ["TREND", "CHOPPY"]),
        ]:
            print(f"\n  ── Shorts by SPY {regime_type} ──")
            key = "direction" if regime_type == "Direction" else "character"
            for val in regime_values:
                rdates = {d for d, info in spy_days.items() if info[key] == val}

                s1m = [t for t in shorts_1m if trade_date(t) in rdates]
                s5m = [t for t in shorts_5m if trade_date(t) in rdates]
                l1m = [t for t in longs_1m if trade_date(t) in rdates]
                nd = len(rdates)

                m1 = compute_metrics(s1m, nd)
                m5 = compute_metrics(s5m, nd)
                ml = compute_metrics(l1m, nd)

                if m1['n'] > 0 or m5['n'] > 0:
                    print(f"    {val:<8} 1m-shorts: N={m1['n']:>3}  WR={m1['wr']:.1f}%  PF={pf_str(m1['pf'])}  Exp={m1['exp']:+.3f}R  PnL={m1['pnl']:+.2f}")
                    print(f"    {'':<8} 5m-shorts: N={m5['n']:>3}  WR={m5['wr']:.1f}%  PF={pf_str(m5['pf'])}  Exp={m5['exp']:+.3f}R  PnL={m5['pnl']:+.2f}")
                    print(f"    {'':<8} 1m-longs:  N={ml['n']:>3}  WR={ml['wr']:.1f}%  PF={pf_str(ml['pf'])}  Exp={ml['exp']:+.3f}R  PnL={ml['pnl']:+.2f}")

        # Quality filter: RED + Q>=5 on 1-min
        print(f"\n  ── What-if: RED + Q>=5 shorts (1-min scaled) ──")
        red_dates = {d for d, info in spy_days.items() if info["direction"] == "RED"}
        filtered_1m = [t for t in shorts_1m if trade_date(t) in red_dates and t.signal.quality_score >= 5]
        if filtered_1m:
            m = compute_metrics(filtered_1m)
            print(f"    RED + Q>=5: N={m['n']:>3}  WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  "
                  f"Exp={m['exp']:+.3f}R  PnL={m['pnl']:+.2f}  StpR={m['stop_rate']:.0f}%")
        else:
            print(f"    RED + Q>=5: 0 trades")

        # Combined: current 1-min longs + RED Q>=5 shorts
        print(f"\n  ── Combined: 1-min longs + RED Q>=5 shorts ──")
        _, lo_trades_1m = all_results.get("B. 1-min scaled + shorts", ([], []))
        longs_only_1m = [t for t in lo_trades_1m if t.signal.direction == 1]
        combo = sorted(longs_only_1m + filtered_1m, key=lambda t: t.signal.timestamp)
        if combo:
            m = compute_metrics(combo, n_days)
            print(f"    Combined: N={m['n']:>3}  WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  "
                  f"Exp={m['exp']:+.3f}R  PnL={m['pnl']:+.2f}  MaxDD={m['max_dd']:.1f}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 4: PER-SETUP BREAKDOWN (1-min scaled, shorts enabled)
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print("  SECTION 4: PER-SETUP BREAKDOWN — 1-min scaled, shorts enabled")
    print(f"{'='*110}")

    trades_raw_1m, trades_1m = all_results.get("B. 1-min scaled + shorts", ([], []))
    by_setup = defaultdict(lambda: {"long": [], "short": []})
    for sym, t in trades_raw_1m:
        name = SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id))
        side = "long" if t.signal.direction == 1 else "short"
        by_setup[name][side].append(t)

    print(f"\n  {'Setup':<18} {'Side':<6} │ {'N':>4} {'WR%':>5} {'PF':>5} "
          f"{'Exp':>7} {'PnL':>8} {'StpR':>4} {'Hold':>5}")
    print(f"  {'─'*18}─{'─'*6}─┼{'─'*48}")

    for name in sorted(by_setup):
        for side in ["long", "short"]:
            trades = by_setup[name][side]
            if trades:
                m = compute_metrics(trades)
                print(f"  {name:<18} {side:<6} │ {m['n']:>4} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
                      f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['stop_rate']:>4.0f}% "
                      f"{m['avg_hold']:>5.1f}")

    # ══════════════════════════════════════════════════════════════
    #  SUMMARY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print("  1-MIN vs 5-MIN COMPARISON SUMMARY")
    print(f"{'='*110}")
    print(f"  Symbols: {len(common_syms)} (common between 1-min and 5-min)")
    print(f"  Period: {min(all_dates)} → {max(all_dates)} ({n_days} days)")
    print(f"  5-min bars/sym: ~{len(list(bars_5min_common.values())[0]) if bars_5min_common else 0}")
    print(f"  1-min bars/sym: ~{len(list(bars_1min_common.values())[0]) if bars_1min_common else 0}")
    print(f"  Scaling: ×5 for ATR(70), vol_lookback(100), windows, time_stops")
    print(f"{'='*110}\n")


if __name__ == "__main__":
    main()
