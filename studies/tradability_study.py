"""
Tradability Score Study — evaluate the graded directional tradability model.

Studies:
  1. Score distribution: all trades, split by SPY day color
  2. Bucketed performance: group by score bucket → WR/PF/PnL
  3. RED-day drill-down: which trades accepted at various thresholds
  4. Threshold sweep: -1.0 to +1.0 in 0.25 steps → metrics at each
  5. Old vs new: blanket RED rejection vs tradability threshold
  6. Component attribution: which component most predictive
  7. Trade log with scores for pattern inspection

Usage:
    python -m alert_overlay.tradability_study --universe all94
"""

import argparse
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import List, Dict
from zoneinfo import ZoneInfo

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import NaN, SetupId, SETUP_DISPLAY_NAME

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent.parent / "data"
WATCHLIST_FILE = Path(__file__).parent.parent / "watchlist.txt"


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
        avg_range_10d = statistics.mean(ranges_10d[-10:]) if ranges_10d else day_range
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


def compute_metrics(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "exp": 0.0, "stop_rate": 0.0}
    wins = [t for t in trades if t.pnl_points > 0]
    losses = [t for t in trades if t.pnl_points <= 0]
    pnl = sum(t.pnl_points for t in trades)
    gw = sum(t.pnl_points for t in wins)
    gl = abs(sum(t.pnl_points for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    return {"n": n, "wr": len(wins) / n * 100, "pf": pf, "pnl": pnl,
            "exp": sum(t.pnl_rr for t in trades) / n, "stop_rate": stopped / n * 100}


def print_metrics(label, m):
    print(f"  {label:40s}  N={m['n']:3d}  WR={m['wr']:5.1f}%  PF={pf_str(m['pf']):>6s}  PnL={m['pnl']:+7.2f}  Exp={m['exp']:+.2f}R  Stop={m['stop_rate']:4.1f}%")


def score_bucket(score, step=0.25):
    """Assign a score to a bucket like [-1.00,-0.75), [-0.75,-0.50), etc."""
    lo = math.floor(score / step) * step
    hi = lo + step
    return f"[{lo:+.2f},{hi:+.2f})"


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

    print(f"=== TRADABILITY SCORE STUDY ===")
    print(f"Universe: {len(symbols)} symbols\n")

    # Load SPY/QQQ for market context + day classification
    from .market_context import SECTOR_MAP, get_sector_etf

    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    spy_day_info = classify_spy_days(spy_bars) if spy_bars else {}

    # Load sector ETF bars
    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    # Run backtest with ALL signals (no long_only, no day-color gating, shorts enabled)
    cfg_all = OverlayConfig(
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
        use_tradability_gate=False,  # compute but don't gate — we'll filter in post
    )

    all_trades: List[Trade] = []
    for sym in symbols:
        fpath = DATA_DIR / f"{sym}_5min.csv"
        if not fpath.exists():
            continue
        bars = load_bars_from_csv(str(fpath))
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        result = run_backtest(bars, cfg=cfg_all, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        all_trades.extend(result.trades)

    print(f"Total trades (all directions, no filter): {len(all_trades)}\n")

    # Apply Q>=2 and <15:30 baseline filters (but NOT day-color)
    def apply_base_filters(trades):
        filtered = []
        for t in trades:
            if t.signal.quality_score < 2:
                continue
            ts = t.signal.timestamp
            hhmm = ts.hour * 100 + ts.minute
            if hhmm >= 1530:
                continue
            filtered.append(t)
        return filtered

    base_trades = apply_base_filters(all_trades)
    longs = [t for t in base_trades if t.signal.direction == 1]
    shorts = [t for t in base_trades if t.signal.direction == -1]

    print(f"After Q>=2 + <15:30: {len(base_trades)} trades ({len(longs)} long, {len(shorts)} short)\n")

    # ── STUDY 1: Score Distribution ──
    print("=" * 80)
    print("STUDY 1: TRADABILITY SCORE DISTRIBUTION")
    print("=" * 80)

    for label, subset in [("ALL LONGS", longs), ("ALL SHORTS", shorts)]:
        scores = [t.signal.tradability_long if t.signal.direction == 1 else t.signal.tradability_short
                  for t in subset]
        if not scores:
            print(f"\n  {label}: no trades")
            continue
        print(f"\n  {label} (N={len(scores)}):")
        print(f"    Mean={statistics.mean(scores):+.3f}  Med={statistics.median(scores):+.3f}  "
              f"Min={min(scores):+.3f}  Max={max(scores):+.3f}  StdDev={statistics.stdev(scores) if len(scores) > 1 else 0:.3f}")

        # Split by day color
        for color in ["GREEN", "RED", "FLAT"]:
            color_trades = [t for t in subset
                           if spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == color]
            if not color_trades:
                continue
            cs = [t.signal.tradability_long if t.signal.direction == 1 else t.signal.tradability_short
                  for t in color_trades]
            m = compute_metrics(color_trades)
            print(f"    {color:6s} (N={len(cs):3d}): score={statistics.mean(cs):+.3f}  "
                  f"WR={m['wr']:5.1f}%  PF={pf_str(m['pf']):>6s}  PnL={m['pnl']:+7.2f}")

    # ── STUDY 2: Bucketed Performance ──
    print(f"\n{'=' * 80}")
    print("STUDY 2: PERFORMANCE BY TRADABILITY SCORE BUCKET")
    print("=" * 80)

    for label, subset, use_long_score in [("LONGS (by long_score)", longs, True),
                                           ("SHORTS (by short_score)", shorts, False)]:
        print(f"\n  {label}:")
        buckets = defaultdict(list)
        for t in subset:
            sc = t.signal.tradability_long if use_long_score else t.signal.tradability_short
            bk = score_bucket(sc)
            buckets[bk].append(t)

        for bk in sorted(buckets.keys()):
            m = compute_metrics(buckets[bk])
            print(f"    {bk:16s}  N={m['n']:3d}  WR={m['wr']:5.1f}%  PF={pf_str(m['pf']):>6s}  PnL={m['pnl']:+7.2f}")

    # ── STUDY 3: RED-Day Drill-Down ──
    print(f"\n{'=' * 80}")
    print("STUDY 3: RED-DAY DRILL-DOWN — LONGS ACCEPTED AT VARIOUS THRESHOLDS")
    print("=" * 80)

    red_longs = [t for t in longs
                 if spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == "RED"]
    print(f"\n  RED-day longs total: {len(red_longs)}")

    if red_longs:
        for thresh in [-1.0, -0.75, -0.5, -0.3, -0.1, 0.0, 0.1, 0.25, 0.5]:
            accepted = [t for t in red_longs if t.signal.tradability_long >= thresh]
            if accepted:
                m = compute_metrics(accepted)
                print(f"    threshold >= {thresh:+.2f}: ", end="")
                print(f"N={m['n']:3d}  WR={m['wr']:5.1f}%  PF={pf_str(m['pf']):>6s}  PnL={m['pnl']:+7.2f}")
            else:
                print(f"    threshold >= {thresh:+.2f}:  N=  0")

    # ── STUDY 4: Threshold Sweep ──
    print(f"\n{'=' * 80}")
    print("STUDY 4: THRESHOLD SWEEP — FULL SYSTEM (LONGS + SHORTS)")
    print("=" * 80)

    print(f"\n  {'Threshold':>10s}  {'N':>4s}  {'N_L':>4s}  {'N_S':>4s}  {'WR':>6s}  {'PF':>6s}  {'PnL':>8s}")
    print(f"  {'-'*10}  {'-'*4}  {'-'*4}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}")

    for thresh in [-1.0, -0.75, -0.5, -0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0]:
        accepted_l = [t for t in longs if t.signal.tradability_long >= thresh]
        accepted_s = [t for t in shorts if t.signal.tradability_short <= -thresh]
        combined = accepted_l + accepted_s
        if combined:
            m = compute_metrics(combined)
            print(f"  {thresh:+10.2f}  {m['n']:4d}  {len(accepted_l):4d}  {len(accepted_s):4d}  "
                  f"{m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  {m['pnl']:+8.2f}")
        else:
            print(f"  {thresh:+10.2f}     0     0     0      -       -         -")

    # ── STUDY 5: Old vs New ──
    print(f"\n{'=' * 80}")
    print("STUDY 5: OLD (BLANKET RED REJECTION) vs NEW (TRADABILITY THRESHOLD)")
    print("=" * 80)

    # Old system: Non-RED longs only (locked baseline)
    old_longs = [t for t in longs
                 if spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") != "RED"]
    m_old = compute_metrics(old_longs)
    print(f"\n  OLD — Non-RED longs (blanket rejection):")
    print_metrics("Non-RED longs", m_old)

    # New system: tradability threshold longs (try default -0.3)
    for thresh in [-0.3, -0.2, -0.1, 0.0]:
        new_longs = [t for t in longs if t.signal.tradability_long >= thresh]
        m_new = compute_metrics(new_longs)
        red_accepted = [t for t in new_longs
                       if spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == "RED"]
        m_red = compute_metrics(red_accepted)
        print(f"\n  NEW — Tradability >= {thresh:+.2f}:")
        print_metrics(f"All longs (trad >= {thresh:+.2f})", m_new)
        print_metrics(f"  ...of which RED-day longs", m_red)

    # Add shorts comparison
    print(f"\n  SHORTS COMPARISON:")
    red_shorts = [t for t in shorts
                  if spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == "RED"]
    m_red_shorts = compute_metrics(red_shorts)
    print_metrics("RED-day shorts (old filter)", m_red_shorts)

    for thresh in [-0.3, -0.2, -0.1, 0.0]:
        new_shorts = [t for t in shorts if t.signal.tradability_short <= -thresh]
        m_ns = compute_metrics(new_shorts)
        print_metrics(f"Shorts (trad_short <= {-thresh:+.2f})", m_ns)

    # ── STUDY 6: Component Attribution ──
    print(f"\n{'=' * 80}")
    print("STUDY 6: COMPONENT ATTRIBUTION")
    print("=" * 80)

    # For each component, split trades into above/below median → compare WR/PF
    print("\n  LONGS — winning vs losing trades by component value:")
    if len(longs) >= 10:
        win_l = [t for t in longs if t.pnl_points > 0]
        loss_l = [t for t in longs if t.pnl_points <= 0]
        for comp_name, getter in [
            ("mkt_component (tradability_long as proxy)", lambda t: t.signal.tradability_long),
        ]:
            if win_l and loss_l:
                w_mean = statistics.mean(getter(t) for t in win_l)
                l_mean = statistics.mean(getter(t) for t in loss_l)
                print(f"    {comp_name}:")
                print(f"      Winners mean={w_mean:+.3f}  Losers mean={l_mean:+.3f}  Delta={w_mean - l_mean:+.3f}")

        # Split by above/below median tradability score
        med = statistics.median(t.signal.tradability_long for t in longs)
        above = [t for t in longs if t.signal.tradability_long >= med]
        below = [t for t in longs if t.signal.tradability_long < med]
        m_above = compute_metrics(above)
        m_below = compute_metrics(below)
        print(f"\n    Split at median tradability_long ({med:+.3f}):")
        print_metrics(f"Above median", m_above)
        print_metrics(f"Below median", m_below)

    # ── STUDY 7: Combined System ──
    print(f"\n{'=' * 80}")
    print("STUDY 7: COMBINED LONG + SHORT SYSTEM AT VARIOUS THRESHOLDS")
    print("=" * 80)

    print(f"\n  {'Config':>40s}  {'N':>4s}  {'WR':>6s}  {'PF':>6s}  {'PnL':>8s}")
    print(f"  {'-'*40}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}")

    # Old baseline: Non-RED longs only, no shorts
    m = compute_metrics(old_longs)
    print(f"  {'Old: Non-RED longs only':>40s}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  {m['pnl']:+8.2f}")

    # Old + RED shorts
    old_combined = old_longs + red_shorts
    m = compute_metrics(old_combined)
    print(f"  {'Old: Non-RED longs + RED shorts':>40s}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  {m['pnl']:+8.2f}")

    # Tradability gated
    for lt in [-0.3, -0.2, -0.1, 0.0]:
        for st in [-0.3, -0.2, -0.1, 0.0]:
            tl = [t for t in longs if t.signal.tradability_long >= lt]
            ts = [t for t in shorts if t.signal.tradability_short <= -st]
            combined = tl + ts
            if combined:
                m = compute_metrics(combined)
                lbl = f"Trad L>={lt:+.1f} S<={-st:+.1f}"
                print(f"  {lbl:>40s}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  {m['pnl']:+8.2f}")

    # ── STUDY 8: Trade Log (top and bottom scores) ──
    print(f"\n{'=' * 80}")
    print("STUDY 8: TRADE LOG — EXTREME SCORES")
    print("=" * 80)

    # Sort longs by tradability score
    sorted_longs = sorted(longs, key=lambda t: t.signal.tradability_long)
    print(f"\n  WORST 10 LONG SCORES (lowest tradability_long):")
    print(f"  {'Date':>10s}  {'Time':>5s}  {'Symbol':>6s}  {'Setup':>14s}  {'Score':>7s}  {'PnL':>7s}  {'Day':>6s}")
    for t in sorted_longs[:10]:
        d = t.signal.timestamp.date()
        day_color = spy_day_info.get(d, {}).get("direction", "?")
        print(f"  {str(d):>10s}  {t.signal.timestamp.strftime('%H:%M'):>5s}  "
              f"{getattr(t, 'symbol', '?'):>6s}  {t.signal.setup_name:>14s}  "
              f"{t.signal.tradability_long:+7.3f}  {t.pnl_points:+7.2f}  {day_color:>6s}")

    print(f"\n  BEST 10 LONG SCORES (highest tradability_long):")
    for t in sorted_longs[-10:]:
        d = t.signal.timestamp.date()
        day_color = spy_day_info.get(d, {}).get("direction", "?")
        print(f"  {str(d):>10s}  {t.signal.timestamp.strftime('%H:%M'):>5s}  "
              f"{getattr(t, 'symbol', '?'):>6s}  {t.signal.setup_name:>14s}  "
              f"{t.signal.tradability_long:+7.3f}  {t.pnl_points:+7.2f}  {day_color:>6s}")

    if shorts:
        sorted_shorts = sorted(shorts, key=lambda t: t.signal.tradability_short)
        print(f"\n  BEST 10 SHORT SCORES (most negative tradability_short):")
        for t in sorted_shorts[:10]:
            d = t.signal.timestamp.date()
            day_color = spy_day_info.get(d, {}).get("direction", "?")
            print(f"  {str(d):>10s}  {t.signal.timestamp.strftime('%H:%M'):>5s}  "
                  f"{getattr(t, 'symbol', '?'):>6s}  {t.signal.setup_name:>14s}  "
                  f"{t.signal.tradability_short:+7.3f}  {t.pnl_points:+7.2f}  {day_color:>6s}")

    print(f"\n{'=' * 80}")
    print("STUDY COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    main()
