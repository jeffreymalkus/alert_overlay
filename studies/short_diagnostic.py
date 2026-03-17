"""
Short trade diagnostic — re-evaluate shorts with current quality gates.

Runs the full system with shorts ENABLED on every setup path, then measures:
  - Per-setup short performance (VK shorts, SC shorts, FB shorts, reversal shorts)
  - Short performance by SPY regime (green/red, trend/choppy)
  - Longs vs shorts head-to-head
  - Combined system with shorts included
  - Feature-level analysis on short trades (quality score, RS, market trend)

Usage:
    python -m alert_overlay.short_diagnostic --universe all94
"""

import argparse
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import (
    NaN, SetupId, SetupFamily, SETUP_DISPLAY_NAME, SETUP_FAMILY_MAP,
)

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent.parent / "data"


# ── SPY day classification (same as robustness_eval) ──────────────

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

        ranges_10d.append(day_range)
        if len(ranges_10d) > 10:
            ranges_10d.pop(0)
        avg_range = statistics.mean(ranges_10d) if ranges_10d else day_range
        volatility = "HIGH_VOL" if (len(ranges_10d) >= 5 and day_range > 1.5 * avg_range) else "NORMAL"

        if day_range > 0:
            close_position = (day_close - day_low) / day_range
            character = "TREND" if (close_position >= 0.75 or close_position <= 0.25) else "CHOPPY"
        else:
            character = "CHOPPY"

        day_info[d] = {
            "direction": direction, "volatility": volatility,
            "character": character, "spy_change_pct": change_pct,
        }
    return day_info


# ── metrics ──────────────────────────────────────────────────────────

def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "∞"

def compute_metrics(trades, n_days=None):
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "exp": 0.0,
                "stop_rate": 0.0, "max_dd": 0.0, "avg_hold": 0.0,
                "avg_win_rr": 0.0, "avg_loss_rr": 0.0, "tpw": 0.0}
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
    avg_win_rr = statistics.mean(t.pnl_rr for t in wins) if wins else 0
    avg_loss_rr = statistics.mean(t.pnl_rr for t in losses) if losses else 0
    return {"n": n, "wr": len(wins)/n*100, "pf": pf, "pnl": pnl,
            "exp": avg_rr, "stop_rate": stopped/n*100, "max_dd": dd,
            "avg_hold": avg_hold, "avg_win_rr": avg_win_rr,
            "avg_loss_rr": avg_loss_rr, "tpw": n/weeks if weeks > 0 else 0}


def print_row(label, m, width=30):
    print(f"  {label:<{width}} │ {m['n']:>4} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
          f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['max_dd']:>5.1f} "
          f"{m['stop_rate']:>4.0f}% {m['avg_hold']:>5.1f}b")


# ── collect trades ──────────────────────────────────────────────────

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


# ── config variants ─────────────────────────────────────────────────

def cfg_shorts_enabled_all():
    """Enable ALL short paths — maximum discovery."""
    c = OverlayConfig()
    c.show_ema_scalp = False       # keep EMA off (separate question)
    c.show_failed_bounce = True    # enable FB (the short-specific setup)
    c.show_spencer = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    c.show_reversal_setups = False # keep reversals off for now
    c.show_ema_retest = False
    c.show_ema_mean_rev = False
    c.show_ema_pullback = False
    # UNLOCK SHORTS
    c.vk_long_only = False
    c.sc_long_only = False
    return c


def cfg_vk_shorts_only():
    """VK with shorts enabled, no SC."""
    c = OverlayConfig()
    c.show_second_chance = False
    c.show_ema_scalp = False
    c.show_failed_bounce = False
    c.show_spencer = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    c.vk_long_only = False         # UNLOCK shorts
    return c


def cfg_sc_shorts_only():
    """SC with shorts enabled, no VK."""
    c = OverlayConfig()
    c.show_reversal_setups = False
    c.show_trend_setups = False
    c.show_ema_retest = False
    c.show_ema_mean_rev = False
    c.show_ema_pullback = False
    c.show_second_chance = True
    c.show_spencer = False
    c.show_failed_bounce = False
    c.show_ema_scalp = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    c.sc_long_only = False         # UNLOCK shorts
    return c


def cfg_fb_only():
    """Failed Bounce only (short-specific setup)."""
    c = OverlayConfig()
    c.show_reversal_setups = False
    c.show_trend_setups = False
    c.show_ema_retest = False
    c.show_ema_mean_rev = False
    c.show_ema_pullback = False
    c.show_second_chance = False
    c.show_spencer = False
    c.show_failed_bounce = True
    c.show_ema_scalp = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    return c


def cfg_current_baseline():
    """Current long-only baseline for comparison."""
    c = OverlayConfig()
    c.show_ema_scalp = False
    c.show_failed_bounce = False
    c.show_spencer = False
    c.show_ema_fpip = False
    c.show_sc_v2 = False
    return c


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Short trade diagnostic")
    parser.add_argument("--universe", type=str, default="all94",
                        choices=["orig35", "all94"])
    args = parser.parse_args()

    # Load data
    if args.universe == "all94":
        symbols = sorted(set(
            p.stem.replace("_5min", "")
            for p in DATA_DIR.glob("*_5min.csv")
        ))
    else:
        from .oos_validation import load_watchlist, WATCHLIST_FILE
        symbols = load_watchlist(WATCHLIST_FILE)

    spy_bars = qqq_bars = None
    sector_bars_dict = {}

    spy_path = DATA_DIR / "SPY_5min.csv"
    qqq_path = DATA_DIR / "QQQ_5min.csv"
    if spy_path.exists():
        spy_bars = load_bars_from_csv(str(spy_path))
        print(f"SPY bars loaded: {len(spy_bars)}")
    if qqq_path.exists():
        qqq_bars = load_bars_from_csv(str(qqq_path))
        print(f"QQQ bars loaded: {len(qqq_bars)}")

    from .market_context import SECTOR_MAP
    sector_etfs = set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))
    if sector_bars_dict:
        print(f"Sector ETFs loaded: {len(sector_bars_dict)}")

    all_bars = {}
    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if bars:
            all_bars[sym] = bars

    all_dates = sorted(set(
        b.timestamp.date() for bars in all_bars.values() for b in bars
    ))
    n_days = len(all_dates)

    spy_days = classify_spy_days(spy_bars) if spy_bars else {}

    print(f"\nShort Trade Diagnostic — {len(all_bars)} symbols [{args.universe}]")
    print(f"Period: {min(all_dates)} → {max(all_dates)} ({n_days} trading days)")

    ctx = dict(spy_bars=spy_bars, qqq_bars=qqq_bars, sector_bars_dict=sector_bars_dict)

    def trade_date(t):
        return t.signal.timestamp.date() if hasattr(t.signal.timestamp, "date") else None

    # ══════════════════════════════════════════════════════════════
    #  TEST 1: CURRENT BASELINE vs SHORTS-ENABLED
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print("  TEST 1: CURRENT BASELINE vs ALL SHORTS ENABLED")
    print(f"{'='*110}")

    configs = [
        ("Current baseline (long-only)", cfg_current_baseline()),
        ("Shorts enabled (VK+SC+FB)",    cfg_shorts_enabled_all()),
    ]

    hdr = f"  {'Config':<30} │ {'N':>4} {'WR%':>5} {'PF':>5} {'Exp':>7} {'PnL':>8} {'MaxDD':>5} {'StpR':>4} {'Hold':>5}"
    print(f"\n{hdr}")
    print(f"  {'─'*30}─┼{'─'*55}")

    for label, cfg in configs:
        trades_raw = collect_trades(all_bars, cfg, **ctx)
        trades = [t for _, t in trades_raw]
        m = compute_metrics(trades, n_days)
        print_row(label, m)

    # ══════════════════════════════════════════════════════════════
    #  TEST 2: LONGS vs SHORTS — ALL SETUPS COMBINED
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print("  TEST 2: LONGS vs SHORTS (all setups, shorts enabled)")
    print(f"{'='*110}")

    cfg_all = cfg_shorts_enabled_all()
    all_trades_raw = collect_trades(all_bars, cfg_all, **ctx)
    all_trades = [t for _, t in all_trades_raw]
    longs = [t for t in all_trades if t.signal.direction == 1]
    shorts = [t for t in all_trades if t.signal.direction == -1]

    print(f"\n{hdr}")
    print(f"  {'─'*30}─┼{'─'*55}")
    print_row("ALL trades", compute_metrics(all_trades, n_days))
    print_row("LONG trades", compute_metrics(longs, n_days))
    print_row("SHORT trades", compute_metrics(shorts, n_days))

    # ══════════════════════════════════════════════════════════════
    #  TEST 3: SHORT PERFORMANCE BY SETUP
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print("  TEST 3: SHORT TRADES BY SETUP")
    print(f"{'='*110}")

    # Per-setup isolation
    setup_configs = [
        ("VK shorts",  cfg_vk_shorts_only()),
        ("SC shorts",  cfg_sc_shorts_only()),
        ("FB shorts",  cfg_fb_only()),
    ]

    print(f"\n{hdr}")
    print(f"  {'─'*30}─┼{'─'*55}")

    for label, cfg in setup_configs:
        trades_raw = collect_trades(all_bars, cfg, **ctx)
        trades = [t for _, t in trades_raw]
        short_trades = [t for t in trades if t.signal.direction == -1]
        long_trades = [t for t in trades if t.signal.direction == 1]
        if short_trades:
            print_row(label, compute_metrics(short_trades, n_days))
        else:
            print(f"  {label:<30} │    0 trades")
        if long_trades and label == "VK shorts":
            # Also show longs from VK for comparison
            print_row("  (VK longs for ref)", compute_metrics(long_trades, n_days))

    # ══════════════════════════════════════════════════════════════
    #  TEST 4: SHORTS BY SPY REGIME
    # ══════════════════════════════════════════════════════════════
    if spy_days:
        print(f"\n{'='*110}")
        print("  TEST 4: SHORT TRADES BY SPY REGIME (all setups)")
        print(f"{'='*110}")

        print(f"\n  ── Shorts by SPY Direction ──")
        for regime in ["GREEN", "RED", "FLAT"]:
            regime_dates = {d for d, info in spy_days.items() if info["direction"] == regime}
            regime_shorts = [t for t in shorts if trade_date(t) in regime_dates]
            regime_longs = [t for t in longs if trade_date(t) in regime_dates]
            n_d = len(regime_dates)
            if regime_shorts:
                m = compute_metrics(regime_shorts, n_d)
                print(f"    {regime:<8} shorts ({n_d:>2}d): ", end="")
                print(f"N={m['n']:>3}  WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  "
                      f"Exp={m['exp']:+.3f}R  PnL={m['pnl']:+.2f}  "
                      f"StpR={m['stop_rate']:.0f}%")
            else:
                print(f"    {regime:<8} shorts ({n_d:>2}d): 0 trades")
            if regime_longs:
                m = compute_metrics(regime_longs, n_d)
                print(f"    {regime:<8} longs  ({n_d:>2}d): "
                      f"N={m['n']:>3}  WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  "
                      f"Exp={m['exp']:+.3f}R  PnL={m['pnl']:+.2f}  "
                      f"StpR={m['stop_rate']:.0f}%")

        print(f"\n  ── Shorts by Day Character ──")
        for char_val in ["TREND", "CHOPPY"]:
            regime_dates = {d for d, info in spy_days.items() if info["character"] == char_val}
            regime_shorts = [t for t in shorts if trade_date(t) in regime_dates]
            n_d = len(regime_dates)
            if regime_shorts:
                m = compute_metrics(regime_shorts, n_d)
                print(f"    {char_val:<8} shorts ({n_d:>2}d): "
                      f"N={m['n']:>3}  WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  "
                      f"Exp={m['exp']:+.3f}R  PnL={m['pnl']:+.2f}  "
                      f"StpR={m['stop_rate']:.0f}%")
            else:
                print(f"    {char_val:<8} shorts ({n_d:>2}d): 0 trades")

        print(f"\n  ── Composite: RED day shorts vs GREEN day shorts ──")
        for comp_label, comp_fn in [
            ("RED + TREND shorts",   lambda i: i["direction"] == "RED" and i["character"] == "TREND"),
            ("RED + CHOPPY shorts",  lambda i: i["direction"] == "RED" and i["character"] == "CHOPPY"),
            ("GREEN + TREND shorts", lambda i: i["direction"] == "GREEN" and i["character"] == "TREND"),
            ("GREEN + CHOPPY shorts",lambda i: i["direction"] == "GREEN" and i["character"] == "CHOPPY"),
        ]:
            regime_dates = {d for d, info in spy_days.items() if comp_fn(info)}
            regime_shorts = [t for t in shorts if trade_date(t) in regime_dates]
            n_d = len(regime_dates)
            if regime_shorts:
                m = compute_metrics(regime_shorts, n_d)
                print(f"    {comp_label:<25} ({n_d:>2}d): "
                      f"N={m['n']:>3}  WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  "
                      f"Exp={m['exp']:+.3f}R  PnL={m['pnl']:+.2f}")
            else:
                print(f"    {comp_label:<25} ({n_d:>2}d): 0 trades")

    # ══════════════════════════════════════════════════════════════
    #  TEST 5: SHORT TRADE QUALITY ANALYSIS
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print("  TEST 5: SHORT TRADE QUALITY ANALYSIS")
    print(f"{'='*110}")

    if shorts:
        # Quality score distribution
        q_dist = defaultdict(list)
        for t in shorts:
            q_dist[t.signal.quality_score].append(t)

        print(f"\n  ── By Quality Score ──")
        for q in sorted(q_dist):
            trades = q_dist[q]
            m = compute_metrics(trades)
            print(f"    Q={q}: N={m['n']:>3}  WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  "
                  f"Exp={m['exp']:+.3f}R  PnL={m['pnl']:+.2f}")

        # By setup
        setup_dist = defaultdict(list)
        for t in shorts:
            name = SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id))
            setup_dist[name].append(t)

        print(f"\n  ── By Setup ──")
        for name in sorted(setup_dist):
            trades = setup_dist[name]
            m = compute_metrics(trades)
            print(f"    {name:<18}: N={m['n']:>3}  WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  "
                  f"Exp={m['exp']:+.3f}R  PnL={m['pnl']:+.2f}  StpR={m['stop_rate']:.0f}%")

        # Exit reasons
        exit_dist = defaultdict(int)
        for t in shorts:
            exit_dist[t.exit_reason] += 1
        print(f"\n  ── Exit Reasons (shorts) ──")
        for reason, count in sorted(exit_dist.items(), key=lambda x: -x[1]):
            pct = count / len(shorts) * 100
            # PnL by exit reason
            reason_trades = [t for t in shorts if t.exit_reason == reason]
            reason_pnl = sum(t.pnl_points for t in reason_trades)
            print(f"    {reason:<12}: {count:>3} ({pct:.0f}%)  PnL={reason_pnl:+.2f}")

        # Winners vs losers
        short_wins = [t for t in shorts if t.pnl_points > 0]
        short_losses = [t for t in shorts if t.pnl_points <= 0]
        if short_wins:
            print(f"\n  ── Short Winners ({len(short_wins)}) ──")
            for t in sorted(short_wins, key=lambda t: -t.pnl_rr)[:10]:
                sym = None
                for s, tr in all_trades_raw:
                    if tr is t:
                        sym = s
                        break
                name = SETUP_DISPLAY_NAME.get(t.signal.setup_id, "?")
                d = trade_date(t)
                regime_tag = ""
                if d and d in spy_days:
                    info = spy_days[d]
                    regime_tag = f"[{info['direction'][:1]}{info['character'][:1]}]"
                print(f"    {sym or '?':<6} {str(t.signal.timestamp)[5:16]} "
                      f"{name:<15} Q={t.signal.quality_score} "
                      f"PnL={t.pnl_rr:+.2f}R ({t.pnl_points:+.2f}pts) "
                      f"exit={t.exit_reason} {regime_tag}")

        if short_losses:
            print(f"\n  ── Short Losers ({len(short_losses)}) — worst 10 ──")
            for t in sorted(short_losses, key=lambda t: t.pnl_rr)[:10]:
                sym = None
                for s, tr in all_trades_raw:
                    if tr is t:
                        sym = s
                        break
                name = SETUP_DISPLAY_NAME.get(t.signal.setup_id, "?")
                d = trade_date(t)
                regime_tag = ""
                if d and d in spy_days:
                    info = spy_days[d]
                    regime_tag = f"[{info['direction'][:1]}{info['character'][:1]}]"
                print(f"    {sym or '?':<6} {str(t.signal.timestamp)[5:16]} "
                      f"{name:<15} Q={t.signal.quality_score} "
                      f"PnL={t.pnl_rr:+.2f}R ({t.pnl_points:+.2f}pts) "
                      f"exit={t.exit_reason} {regime_tag}")

    # ══════════════════════════════════════════════════════════════
    #  TEST 6: WHAT-IF — RED-DAY-ONLY SHORTS
    # ══════════════════════════════════════════════════════════════
    if spy_days and shorts:
        print(f"\n{'='*110}")
        print("  TEST 6: WHAT-IF SCENARIOS")
        print(f"{'='*110}")

        red_dates = {d for d, info in spy_days.items() if info["direction"] == "RED"}
        non_green_dates = {d for d, info in spy_days.items() if info["direction"] != "GREEN"}

        scenarios = [
            ("All shorts (unrestricted)",   shorts),
            ("RED-day shorts only",         [t for t in shorts if trade_date(t) in red_dates]),
            ("Non-GREEN shorts only",       [t for t in shorts if trade_date(t) in non_green_dates]),
            ("Quality >= 5 shorts",         [t for t in shorts if t.signal.quality_score >= 5]),
            ("RED + Q>=5 shorts",           [t for t in shorts if trade_date(t) in red_dates and t.signal.quality_score >= 5]),
        ]

        print(f"\n  {'Scenario':<30} │ {'N':>4} {'WR%':>5} {'PF':>5} {'Exp':>7} {'PnL':>8} {'MaxDD':>5} {'StpR':>4}")
        print(f"  {'─'*30}─┼{'─'*48}")

        for label, trades in scenarios:
            if trades:
                m = compute_metrics(trades)
                print(f"  {label:<30} │ {m['n']:>4} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
                      f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['max_dd']:>5.1f} "
                      f"{m['stop_rate']:>4.0f}%")
            else:
                print(f"  {label:<30} │    0 trades")

        # Combined: current longs + best short scenario
        print(f"\n  ── Combined: Current Longs + Filtered Shorts ──")
        baseline_long_raw = collect_trades(all_bars, cfg_current_baseline(), **ctx)
        baseline_longs = [t for _, t in baseline_long_raw]

        combos = [
            ("Longs only (current)",           baseline_longs),
            ("Longs + all shorts",             baseline_longs + shorts),
            ("Longs + RED-day shorts",         baseline_longs + [t for t in shorts if trade_date(t) in red_dates]),
            ("Longs + non-GREEN shorts",       baseline_longs + [t for t in shorts if trade_date(t) in non_green_dates]),
            ("Longs + Q>=5 shorts",            baseline_longs + [t for t in shorts if t.signal.quality_score >= 5]),
        ]

        print(f"\n  {'Combo':<30} │ {'N':>4} {'WR%':>5} {'PF':>5} {'Exp':>7} {'PnL':>8} {'MaxDD':>5}")
        print(f"  {'─'*30}─┼{'─'*42}")

        for label, trades in combos:
            if trades:
                # Sort by timestamp for proper DD calculation
                trades_sorted = sorted(trades, key=lambda t: t.signal.timestamp)
                m = compute_metrics(trades_sorted, n_days)
                print(f"  {label:<30} │ {m['n']:>4} {m['wr']:>5.1f}% {pf_str(m['pf']):>5} "
                      f"{m['exp']:>+6.3f}R {m['pnl']:>+8.2f} {m['max_dd']:>5.1f}")

    # ══════════════════════════════════════════════════════════════
    #  SUMMARY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print("  SHORT DIAGNOSTIC SUMMARY")
    print(f"{'='*110}")
    print(f"  Universe: {args.universe} ({len(all_bars)} symbols)")
    print(f"  Period: {min(all_dates)} → {max(all_dates)} ({n_days} days)")
    print(f"  Total shorts found: {len(shorts)}")
    print(f"  Total longs found: {len(longs)}")
    if shorts:
        m_s = compute_metrics(shorts)
        m_l = compute_metrics(longs)
        print(f"  Shorts: WR={m_s['wr']:.1f}%, PF={pf_str(m_s['pf'])}, Exp={m_s['exp']:+.3f}R")
        print(f"  Longs:  WR={m_l['wr']:.1f}%, PF={pf_str(m_l['pf'])}, Exp={m_l['exp']:+.3f}R")
    print(f"{'='*110}\n")


if __name__ == "__main__":
    main()
