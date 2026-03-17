"""
Candle Anatomy Feature Study — disciplined measurement on existing setups.

Measures trigger-bar and pullback-bar composition for VK and EMA_RECLAIM.
Does NOT create new setups or hard-gate multiple rules.

Phase 1: Annotate every trade's trigger bar anatomy.
Phase 2: Test a small, pre-declared feature set with coarse thresholds.
Phase 3: Report pass/fail subsets for each candidate feature.

Usage:
    python -m alert_overlay.candle_anatomy_study --universe all94
"""

import argparse
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import NaN, SetupId, Bar

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent.parent / "data"
WATCHLIST_FILE = Path(__file__).parent.parent / "watchlist.txt"

# ── Lookback for "recent" median range / volume ──
LOOKBACK_BARS = 10


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
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "exp": 0.0,
                "stop_rate": 0.0, "max_dd": 0.0}
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
    return {"n": n, "wr": len(wins) / n * 100, "pf": pf, "pnl": pnl,
            "exp": sum(t.pnl_rr for t in trades) / n,
            "stop_rate": stopped / n * 100, "max_dd": dd}


# ══════════════════════════════════════════════════════════════
#  CANDLE ANNOTATION
# ══════════════════════════════════════════════════════════════

def annotate_trigger_bar(bar: Bar, prev_bars: List[Bar], direction: int) -> dict:
    """
    Compute trigger-bar anatomy metrics.

    direction: 1 = long, -1 = short
    prev_bars: prior bars in reverse chronological order (prev_bars[0] = bar[-1])
    """
    rng = bar.high - bar.low
    body = abs(bar.close - bar.open)

    if rng > 0:
        body_pct = body / rng
        close_loc = (bar.close - bar.low) / rng  # 0=at low, 1=at high
        open_loc = (bar.open - bar.low) / rng
        upper_wick = bar.high - max(bar.close, bar.open)
        lower_wick = min(bar.close, bar.open) - bar.low
        upper_wick_pct = upper_wick / rng
        lower_wick_pct = lower_wick / rng
    else:
        body_pct = 0.0
        close_loc = 0.5
        open_loc = 0.5
        upper_wick = 0.0
        lower_wick = 0.0
        upper_wick_pct = 0.0
        lower_wick_pct = 0.0

    # Directional close location: how well-placed is the close for the trade direction
    # For longs: close_loc (higher = better). For shorts: 1 - close_loc.
    dir_close_loc = close_loc if direction == 1 else (1.0 - close_loc)

    # Opposing wick: the wick that opposes the trade direction
    # For longs: upper wick is neutral/good, lower wick is opposing (shows selling)
    # Actually: for longs, the *upper wick* shows rejection (couldn't hold highs)
    # Standard: opposing wick = the wick on the trade-direction side that shows rejection
    # For longs: upper wick = rejection (price went up but couldn't hold)
    # For shorts: lower wick = rejection (price went down but bounced)
    if direction == 1:
        opposing_wick_pct = upper_wick_pct   # longs: upper wick = failed to hold highs
    else:
        opposing_wick_pct = lower_wick_pct   # shorts: lower wick = failed to hold lows

    # Bullish/bearish body
    bullish_body = bar.close > bar.open

    # Close in top N% of range
    close_top20 = close_loc >= 0.80 if direction == 1 else close_loc <= 0.20
    close_top30 = close_loc >= 0.70 if direction == 1 else close_loc <= 0.30
    close_top40 = close_loc >= 0.60 if direction == 1 else close_loc <= 0.40

    # Range vs recent bars
    recent_ranges = [b.high - b.low for b in prev_bars[:LOOKBACK_BARS] if (b.high - b.low) > 0]
    med_range = statistics.median(recent_ranges) if recent_ranges else rng
    range_expansion = rng / med_range if med_range > 0 else 1.0

    # Volume vs prior bar and recent average
    prev_vol = prev_bars[0].volume if prev_bars else bar.volume
    vol_vs_prior = bar.volume / prev_vol if prev_vol > 0 else 1.0
    recent_vols = [b.volume for b in prev_bars[:LOOKBACK_BARS] if b.volume > 0]
    avg_vol = statistics.mean(recent_vols) if recent_vols else bar.volume
    vol_vs_avg = bar.volume / avg_vol if avg_vol > 0 else 1.0

    # Closes above prior high / below prior low
    if prev_bars:
        prev = prev_bars[0]
        closes_above_prior_high = bar.close > prev.high
        closes_below_prior_low = bar.close < prev.low
        # Inside / outside / engulfing
        is_inside = bar.high <= prev.high and bar.low >= prev.low
        is_outside = bar.high > prev.high and bar.low < prev.low
        is_engulfing = is_outside and ((direction == 1 and bar.close > bar.open) or
                                        (direction == -1 and bar.close < bar.open))
    else:
        closes_above_prior_high = False
        closes_below_prior_low = False
        is_inside = False
        is_outside = False
        is_engulfing = False

    # Directional: does bar close beyond prior bar's extreme in trade direction?
    if direction == 1:
        closes_beyond_prior = closes_above_prior_high
    else:
        closes_beyond_prior = closes_below_prior_low

    return {
        "range": rng,
        "body": body,
        "body_pct": body_pct,
        "close_loc": close_loc,
        "dir_close_loc": dir_close_loc,
        "open_loc": open_loc,
        "upper_wick_pct": upper_wick_pct,
        "lower_wick_pct": lower_wick_pct,
        "opposing_wick_pct": opposing_wick_pct,
        "bullish_body": bullish_body,
        "close_top20": close_top20,
        "close_top30": close_top30,
        "close_top40": close_top40,
        "range_expansion": range_expansion,
        "vol_vs_prior": vol_vs_prior,
        "vol_vs_avg": vol_vs_avg,
        "closes_beyond_prior": closes_beyond_prior,
        "is_inside": is_inside,
        "is_outside": is_outside,
        "is_engulfing": is_engulfing,
    }


def annotate_pullback(prev_bars: List[Bar], direction: int) -> dict:
    """
    Annotate pullback / pre-trigger bar anatomy.
    prev_bars[0] = bar immediately before trigger.
    """
    if not prev_bars:
        return {}

    # Last pullback bar (bar[-1])
    pb = prev_bars[0]
    pb_rng = pb.high - pb.low
    pb_body = abs(pb.close - pb.open)

    if pb_rng > 0:
        pb_body_pct = pb_body / pb_rng
        pb_close_loc = (pb.close - pb.low) / pb_rng
        pb_upper_wick = (pb.high - max(pb.close, pb.open)) / pb_rng
        pb_lower_wick = (min(pb.close, pb.open) - pb.low) / pb_rng
    else:
        pb_body_pct = 0.0
        pb_close_loc = 0.5
        pb_upper_wick = 0.0
        pb_lower_wick = 0.0

    # Pullback bars (2-4 bars before trigger)
    pb_window = prev_bars[:4]
    pb_ranges = [b.high - b.low for b in pb_window]
    pb_vols = [b.volume for b in pb_window]

    # Compare to impulse bars (bars 4-8 before trigger)
    impulse_window = prev_bars[4:8] if len(prev_bars) >= 8 else prev_bars[len(pb_window):]
    impulse_ranges = [b.high - b.low for b in impulse_window] if impulse_window else pb_ranges
    impulse_vols = [b.volume for b in impulse_window] if impulse_window else pb_vols

    avg_pb_range = statistics.mean(pb_ranges) if pb_ranges else 0
    avg_imp_range = statistics.mean(impulse_ranges) if impulse_ranges else avg_pb_range
    pb_range_ratio = avg_pb_range / avg_imp_range if avg_imp_range > 0 else 1.0

    avg_pb_vol = statistics.mean(pb_vols) if pb_vols else 0
    avg_imp_vol = statistics.mean(impulse_vols) if impulse_vols else avg_pb_vol
    pb_vol_ratio = avg_pb_vol / avg_imp_vol if avg_imp_vol > 0 else 1.0

    # Overlapping / compressed: how many of the pullback bars have body overlap?
    overlap_count = 0
    for j in range(1, len(pb_window)):
        b0 = pb_window[j - 1]
        b1 = pb_window[j]
        # Overlap: bodies intersect
        b0_top = max(b0.close, b0.open)
        b0_bot = min(b0.close, b0.open)
        b1_top = max(b1.close, b1.open)
        b1_bot = min(b1.close, b1.open)
        if b0_top >= b1_bot and b1_top >= b0_bot:
            overlap_count += 1
    overlap_ratio = overlap_count / max(len(pb_window) - 1, 1)

    # Pullback direction pressure: are pullback bars moving against trade direction?
    # For longs: bearish pullback bars (close < open) = healthy
    # For shorts: bullish pullback bars (close > open) = healthy
    opposing_body_count = 0
    for b in pb_window:
        if direction == 1 and b.close < b.open:
            opposing_body_count += 1
        elif direction == -1 and b.close > b.open:
            opposing_body_count += 1
    opposing_body_ratio = opposing_body_count / len(pb_window) if pb_window else 0

    # Rejection in pullback: wicks showing rejection of the opposing direction
    # For longs: lower wicks on pullback = buyers defending
    # For shorts: upper wicks on pullback = sellers defending
    rejection_count = 0
    for b in pb_window:
        b_rng = b.high - b.low
        if b_rng > 0:
            if direction == 1:
                low_wick = (min(b.close, b.open) - b.low) / b_rng
                if low_wick >= 0.30:
                    rejection_count += 1
            else:
                up_wick = (b.high - max(b.close, b.open)) / b_rng
                if up_wick >= 0.30:
                    rejection_count += 1
    rejection_ratio = rejection_count / len(pb_window) if pb_window else 0

    return {
        "pb_body_pct": pb_body_pct,
        "pb_close_loc": pb_close_loc,
        "pb_upper_wick_pct": pb_upper_wick,
        "pb_lower_wick_pct": pb_lower_wick,
        "pb_range_ratio": pb_range_ratio,
        "pb_vol_ratio": pb_vol_ratio,
        "pb_overlap_ratio": overlap_ratio,
        "pb_opposing_body_ratio": opposing_body_ratio,
        "pb_rejection_ratio": rejection_ratio,
    }


# ══════════════════════════════════════════════════════════════
#  FEATURE TESTING
# ══════════════════════════════════════════════════════════════

def test_feature(trades, annotations, feature_name, threshold, direction=">="):
    """
    Split trades by a single feature threshold.
    Returns (pass_trades, fail_trades, pass_rate).
    """
    pass_trades = []
    fail_trades = []
    for t, ann in zip(trades, annotations):
        val = ann.get(feature_name, 0)
        if direction == ">=":
            passes = val >= threshold
        elif direction == "<=":
            passes = val <= threshold
        elif direction == "==":
            passes = bool(val) == bool(threshold)
        else:
            passes = False

        if passes:
            pass_trades.append(t)
        else:
            fail_trades.append(t)

    pass_rate = len(pass_trades) / len(trades) * 100 if trades else 0
    return pass_trades, fail_trades, pass_rate


def print_feature_row(name, thresh_str, pass_t, fail_t, n_total):
    mp = compute_metrics(pass_t)
    mf = compute_metrics(fail_t)
    pass_rate = len(pass_t) / n_total * 100 if n_total > 0 else 0

    print(f"  {name:30s} {thresh_str:>12s} │ {pass_rate:5.0f}%  "
          f"{mp['n']:>3d} {mp['wr']:>5.1f}% {pf_str(mp['pf']):>6s} {mp['exp']:>+5.2f}R {mp['pnl']:>+7.2f} │ "
          f"{mf['n']:>3d} {mf['wr']:>5.1f}% {pf_str(mf['pf']):>6s} {mf['exp']:>+5.2f}R {mf['pnl']:>+7.2f}")


def print_feature_header():
    print(f"  {'Feature':30s} {'Threshold':>12s} │ {'Pass%':>5s}  "
          f"{'N_P':>3s} {'WR_P':>5s} {'PF_P':>6s} {'Exp_P':>6s} {'PnL_P':>7s} │ "
          f"{'N_F':>3s} {'WR_F':>5s} {'PF_F':>6s} {'Exp_F':>6s} {'PnL_F':>7s}")
    print(f"  {'─' * 30} {'─' * 12}─┼{'─' * 43}─┼{'─' * 43}")


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

    print(f"{'=' * 110}")
    print(f"  CANDLE ANATOMY FEATURE STUDY — MEASUREMENT ONLY")
    print(f"{'=' * 110}")
    print(f"  Universe: {len(symbols)} symbols")
    print(f"  Focus: VWAP_KISS, EMA_RECLAIM")
    print(f"  Guardrail: one feature at a time, coarse thresholds, no parameter sweeps\n")

    # ── Load market data ──
    from .market_context import SECTOR_MAP, get_sector_etf

    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    # ── Config: active setups (VK + SC baseline) + EMA_RECLAIM for study ──
    cfg = OverlayConfig(
        show_reversal_setups=False,
        show_trend_setups=True,
        show_ema_retest=False,
        show_ema_mean_rev=False,
        show_ema_pullback=False,
        show_second_chance=True,
        show_spencer=False,
        show_failed_bounce=False,
        show_ema_scalp=True,       # EMA_RECLAIM enabled for study
        show_ema_fpip=False,
        show_sc_v2=False,
        vk_long_only=True,
        sc_long_only=True,
        use_market_context=True,
        use_sector_context=True,
    )

    # ── Run backtest, storing bars per symbol for lookback ──
    all_trades: List[Trade] = []
    bars_by_symbol: Dict[str, List[Bar]] = {}
    symbol_for_trade: Dict[int, str] = {}

    for sym in symbols:
        fpath = DATA_DIR / f"{sym}_5min.csv"
        if not fpath.exists():
            continue
        bars = load_bars_from_csv(str(fpath))
        if not bars:
            continue
        bars_by_symbol[sym] = bars
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        for t in result.trades:
            symbol_for_trade[id(t)] = sym
        all_trades.extend(result.trades)

    print(f"  Total trades: {len(all_trades)}\n")

    # ── Annotate every trade ──
    annotations: Dict[int, dict] = {}  # trade id → annotation dict
    for t in all_trades:
        sym = symbol_for_trade.get(id(t), "")
        bars = bars_by_symbol.get(sym, [])
        bi = t.signal.bar_index

        if not bars or bi < 0 or bi >= len(bars):
            annotations[id(t)] = {}
            continue

        trigger_bar = bars[bi]
        prev_bars = [bars[bi - j] for j in range(1, min(bi, LOOKBACK_BARS + 1) + 1)]

        trig_ann = annotate_trigger_bar(trigger_bar, prev_bars, t.signal.direction)
        pb_ann = annotate_pullback(prev_bars, t.signal.direction)

        combined = {**trig_ann, **pb_ann}
        annotations[id(t)] = combined

    # ── Split by setup ──
    vk_trades = [(t, annotations.get(id(t), {})) for t in all_trades
                 if t.signal.setup_id == SetupId.VWAP_KISS]
    ema_trades = [(t, annotations.get(id(t), {})) for t in all_trades
                  if t.signal.setup_id == SetupId.EMA_RECLAIM]
    sc_trades = [(t, annotations.get(id(t), {})) for t in all_trades
                 if t.signal.setup_id == SetupId.SECOND_CHANCE]

    # ══════════════════════════════════════════════════════════════
    #  PHASE 1: RAW ANATOMY STATS
    # ══════════════════════════════════════════════════════════════

    for setup_label, setup_data in [("VWAP_KISS", vk_trades),
                                     ("EMA_RECLAIM", ema_trades),
                                     ("SECOND_CHANCE (optional)", sc_trades)]:
        trades = [t for t, _ in setup_data]
        anns = [a for _, a in setup_data]
        n = len(trades)

        if n == 0:
            print(f"\n  {setup_label}: 0 trades — skipped\n")
            continue

        m = compute_metrics(trades)
        print(f"\n{'=' * 110}")
        print(f"  {setup_label} — PHASE 1: RAW ANATOMY STATS")
        print(f"{'=' * 110}")
        print(f"  Baseline: N={m['n']}  WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  "
              f"Exp={m['exp']:+.2f}R  PnL={m['pnl']:+.2f}  MaxDD={m['max_dd']:.2f}\n")

        # Winners vs losers anatomy
        win_anns = [a for t, a in setup_data if t.pnl_points > 0]
        loss_anns = [a for t, a in setup_data if t.pnl_points <= 0]

        print(f"  {'Metric':35s} │ {'All':>6s}  {'Winners':>7s}  {'Losers':>7s}  {'Delta':>7s}")
        print(f"  {'─' * 35}─┼{'─' * 35}")

        for metric in ["body_pct", "dir_close_loc", "opposing_wick_pct",
                        "range_expansion", "vol_vs_avg", "close_loc"]:
            all_vals = [a.get(metric, 0) for a in anns if metric in a]
            win_vals = [a.get(metric, 0) for a in win_anns if metric in a]
            loss_vals = [a.get(metric, 0) for a in loss_anns if metric in a]

            if not all_vals:
                continue

            all_mean = statistics.mean(all_vals)
            win_mean = statistics.mean(win_vals) if win_vals else 0
            loss_mean = statistics.mean(loss_vals) if loss_vals else 0
            delta = win_mean - loss_mean

            print(f"  {metric:35s} │ {all_mean:>6.3f}  {win_mean:>7.3f}  {loss_mean:>7.3f}  {delta:>+7.3f}")

        # Pullback metrics
        pb_metrics = ["pb_body_pct", "pb_range_ratio", "pb_vol_ratio",
                       "pb_overlap_ratio", "pb_rejection_ratio"]
        has_pb = any(m in anns[0] for m in pb_metrics) if anns else False
        if has_pb:
            print(f"\n  {'Pullback Metric':35s} │ {'All':>6s}  {'Winners':>7s}  {'Losers':>7s}  {'Delta':>7s}")
            print(f"  {'─' * 35}─┼{'─' * 35}")
            for metric in pb_metrics:
                all_vals = [a.get(metric, 0) for a in anns if metric in a]
                win_vals = [a.get(metric, 0) for a in win_anns if metric in a]
                loss_vals = [a.get(metric, 0) for a in loss_anns if metric in a]
                if not all_vals:
                    continue
                all_mean = statistics.mean(all_vals)
                win_mean = statistics.mean(win_vals) if win_vals else 0
                loss_mean = statistics.mean(loss_vals) if loss_vals else 0
                delta = win_mean - loss_mean
                print(f"  {metric:35s} │ {all_mean:>6.3f}  {win_mean:>7.3f}  {loss_mean:>7.3f}  {delta:>+7.3f}")

        # Boolean features: pass rates for winners vs losers
        print(f"\n  {'Boolean Feature':35s} │ {'All%':>5s}  {'Win%':>5s}  {'Loss%':>5s}  {'Lift':>6s}")
        print(f"  {'─' * 35}─┼{'─' * 26}")
        for feat in ["closes_beyond_prior", "is_outside", "close_top30", "close_top40"]:
            all_rate = sum(1 for a in anns if a.get(feat)) / n * 100 if n else 0
            win_rate = sum(1 for a in win_anns if a.get(feat)) / len(win_anns) * 100 if win_anns else 0
            loss_rate = sum(1 for a in loss_anns if a.get(feat)) / len(loss_anns) * 100 if loss_anns else 0
            lift = win_rate - loss_rate
            print(f"  {feat:35s} │ {all_rate:>5.0f}%  {win_rate:>5.0f}%  {loss_rate:>5.0f}%  {lift:>+5.0f}pp")

    # ══════════════════════════════════════════════════════════════
    #  PHASE 2 + 3: FEATURE TESTING — VK
    # ══════════════════════════════════════════════════════════════

    for setup_label, setup_data, feature_defs in [
        ("VWAP_KISS", vk_trades, [
            # Feature 1: trigger bar close location %
            ("dir_close_loc", ">=", [0.60, 0.70]),
            # Feature 2: trigger bar body % of range
            ("body_pct", ">=", [0.40, 0.50]),
            # Feature 3: opposing wick size
            ("opposing_wick_pct", "<=", [0.20, 0.25]),
            # Feature 4: range expansion vs recent bars
            ("range_expansion", ">=", [1.2, 1.5]),
            # Feature 5: closes beyond prior high / is outside bar
            ("closes_beyond_prior", "==", [True]),
        ]),
        ("EMA_RECLAIM", ema_trades, [
            # Feature 1: reclaim bar close location %
            ("dir_close_loc", ">=", [0.60, 0.70]),
            # Feature 2: reclaim bar body % of range
            ("body_pct", ">=", [0.40, 0.50]),
            # Feature 3: opposing wick size on reclaim bar
            ("opposing_wick_pct", "<=", [0.20, 0.25]),
            # Feature 4: pullback-bar weakness (range contraction = healthy pullback)
            ("pb_range_ratio", "<=", [0.80, 1.0]),
            # Feature 5: reclaim bar range expansion vs recent bars
            ("range_expansion", ">=", [1.2, 1.5]),
        ]),
        ("SECOND_CHANCE (optional)", sc_trades, [
            ("dir_close_loc", ">=", [0.60, 0.70]),
            ("body_pct", ">=", [0.40, 0.50]),
            ("opposing_wick_pct", "<=", [0.20, 0.25]),
            ("range_expansion", ">=", [1.2, 1.5]),
            ("closes_beyond_prior", "==", [True]),
        ]),
    ]:
        trades = [t for t, _ in setup_data]
        anns = [a for _, a in setup_data]
        n = len(trades)

        if n == 0:
            continue

        m_base = compute_metrics(trades)

        print(f"\n{'=' * 110}")
        print(f"  {setup_label} — PHASE 2+3: FEATURE PASS/FAIL TESTING")
        print(f"{'=' * 110}")
        print(f"  Baseline: N={m_base['n']}  WR={m_base['wr']:.1f}%  PF={pf_str(m_base['pf'])}  "
              f"Exp={m_base['exp']:+.2f}R  PnL={m_base['pnl']:+.2f}\n")

        print_feature_header()

        best_feature = None
        best_lift = -999.0  # track best by WR lift with reasonable pass count

        for feat_name, direction, thresholds in feature_defs:
            for thresh in thresholds:
                if direction == "==":
                    thresh_str = f"= {thresh}"
                else:
                    thresh_str = f"{direction} {thresh}"

                pass_t, fail_t, pass_rate = test_feature(trades, anns, feat_name, thresh, direction)
                print_feature_row(feat_name, thresh_str, pass_t, fail_t, n)

                # Track best: want meaningful pass count (>= 40% pass rate)
                # and WR lift
                if pass_t and fail_t and len(pass_t) >= 0.3 * n:
                    mp = compute_metrics(pass_t)
                    mf = compute_metrics(fail_t)
                    lift = mp['wr'] - mf['wr']
                    # Also consider PF separation
                    pf_pass = mp['pf'] if mp['pf'] < 999 else 10
                    pf_fail = mf['pf'] if mf['pf'] < 999 else 10
                    # Score: WR lift + PF advantage
                    score = lift + (pf_pass - pf_fail) * 2
                    if score > best_lift and mp['pf'] > mf['pf']:
                        best_lift = score
                        best_feature = (feat_name, thresh_str, len(pass_t), mp['wr'], pf_str(mp['pf']),
                                       len(fail_t), mf['wr'], pf_str(mf['pf']))

        # ── Recommendation ──
        print(f"\n  RECOMMENDATION for {setup_label}:")
        if best_feature:
            fn, ts, np, wrp, pfp, nf, wrf, pff = best_feature
            print(f"    Best candidate: {fn} {ts}")
            print(f"      Pass: N={np}  WR={wrp:.1f}%  PF={pfp}")
            print(f"      Fail: N={nf}  WR={wrf:.1f}%  PF={pff}")
            if np < 10:
                print(f"    ⚠ WARNING: Only {np} passing trades — too sparse to trust")
            elif np < 20:
                print(f"    ⚠ CAUTION: {np} passing trades — borderline sample")
            else:
                print(f"    ✓ Sample adequate ({np} trades)")
        else:
            print(f"    No feature showed clear separation with adequate pass count.")

    # ══════════════════════════════════════════════════════════════
    #  TRADE-LEVEL LOG (worst anatomy trades)
    # ══════════════════════════════════════════════════════════════
    for setup_label, setup_data in [("VWAP_KISS", vk_trades), ("EMA_RECLAIM", ema_trades)]:
        trades = [t for t, _ in setup_data]
        anns_list = [a for _, a in setup_data]
        n = len(trades)
        if n == 0:
            continue

        print(f"\n{'=' * 110}")
        print(f"  {setup_label} — TRADE LOG (all trades with anatomy)")
        print(f"{'=' * 110}")
        print(f"  {'Date':>10s} {'Time':>5s} {'Sym':>6s} {'PnL':>7s} {'Exit':>5s} "
              f"{'BdyPct':>6s} {'ClLoc':>5s} {'OppWk':>5s} {'RngX':>5s} {'VolA':>5s} "
              f"{'CbPr':>4s} {'Out':>3s}")
        print(f"  {'─' * 10} {'─' * 5} {'─' * 6} {'─' * 7} {'─' * 5} "
              f"{'─' * 6} {'─' * 5} {'─' * 5} {'─' * 5} {'─' * 5} "
              f"{'─' * 4} {'─' * 3}")

        for t, ann in sorted(zip(trades, anns_list), key=lambda x: x[0].signal.timestamp):
            sym = symbol_for_trade.get(id(t), "?")
            d = t.signal.timestamp.date()
            print(f"  {str(d):>10s} {t.signal.timestamp.strftime('%H:%M'):>5s} {sym:>6s} "
                  f"{t.pnl_points:>+7.2f} {t.exit_reason:>5s} "
                  f"{ann.get('body_pct', 0):>6.2f} {ann.get('dir_close_loc', 0):>5.2f} "
                  f"{ann.get('opposing_wick_pct', 0):>5.2f} {ann.get('range_expansion', 0):>5.2f} "
                  f"{ann.get('vol_vs_avg', 0):>5.2f} "
                  f"{'Y' if ann.get('closes_beyond_prior') else 'N':>4s} "
                  f"{'Y' if ann.get('is_outside') else 'N':>3s}")

    print(f"\n{'=' * 110}")
    print(f"  CANDLE ANATOMY STUDY COMPLETE — MEASUREMENT ONLY")
    print(f"{'=' * 110}")


if __name__ == "__main__":
    main()
