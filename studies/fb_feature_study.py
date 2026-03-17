"""
Failed Bounce Short-Side Feature Study — disciplined measurement.

Applies the same methodology as candle_anatomy_study.py:
  Phase 1: Annotate every FB trade's key features.
  Phase 2: Test a small, pre-declared feature set with coarse thresholds.
  Phase 3: Report pass/fail subsets for each candidate feature.
  Phase 4: Stop logic analysis — is stop placement a bigger problem than entry?
  Phase 5: Summary recommendation.

Features measured per FB trade:
  1. market_state       — SPY trend at entry (-1/0/+1)
  2. sector_state       — sector trend at entry (-1/0/+1)
  3. rs_weakness        — RS vs SPY (negative = weak vs market)
  4. bounce_depth_pct   — bounce high vs breakdown bar range (shallow vs deep)
  5. bounce_vol_ratio   — bounce bar volume / breakdown bar volume
  6. rejection_wick_pct — upper wick on bounce bar as % of bounce bar range
  7. dist_from_vwap     — trigger bar close distance from VWAP (in ATR units)
  8. dist_from_ema9     — trigger bar close distance from EMA9 (in ATR units)
  9. time_bucket        — time of day bucket (AM / MID / LATE)
  10. trigger_close_loc  — where trigger bar closes within its range (0=low, 1=high)
  11. follow_through_1b  — 1-bar follow-through (next bar close vs entry, in ATR)
  12. quality_score      — engine quality score
  13. level_tag          — VWAP / ORL / SWING
  14. spy_day_color      — RED / GREEN / FLAT

Usage:
    python -m alert_overlay.fb_feature_study --universe all94
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
    day_info = {}
    for d in sorted(daily.keys()):
        bars = daily[d]
        day_open = bars[0].open
        day_close = bars[-1].close
        change_pct = (day_close - day_open) / day_open * 100 if day_open > 0 else 0
        if change_pct > 0.05:
            direction = "GREEN"
        elif change_pct < -0.05:
            direction = "RED"
        else:
            direction = "FLAT"
        day_info[d] = direction
    return day_info


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "∞"


def compute_metrics(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "exp": 0.0,
                "stop_rate": 0.0, "avg_loss": 0.0, "avg_win": 0.0, "max_dd": 0.0}
    wins = [t for t in trades if t.pnl_points > 0]
    losses = [t for t in trades if t.pnl_points <= 0]
    pnl = sum(t.pnl_points for t in trades)
    gw = sum(t.pnl_points for t in wins)
    gl = abs(sum(t.pnl_points for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    avg_win = gw / len(wins) if wins else 0.0
    avg_loss = gl / len(losses) if losses else 0.0
    cum = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: t.signal.timestamp):
        cum += t.pnl_points
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum
    return {"n": n, "wr": len(wins) / n * 100, "pf": pf, "pnl": pnl,
            "exp": sum(t.pnl_rr for t in trades) / n,
            "stop_rate": stopped / n * 100, "avg_win": avg_win,
            "avg_loss": avg_loss, "max_dd": dd}


def print_metrics(label, m):
    print(f"  {label:40s}  N={m['n']:3d}  WR={m['wr']:5.1f}%  "
          f"PF={pf_str(m['pf']):>6s}  PnL={m['pnl']:+7.2f}  "
          f"Exp={m['exp']:+.2f}R  Stop={m['stop_rate']:4.1f}%")


# ══════════════════════════════════════════════════════════════
#  FEATURE ANNOTATION
# ══════════════════════════════════════════════════════════════

def annotate_fb_trade(trade: Trade, bars: List[Bar], sym: str) -> Optional[dict]:
    """
    Annotate a Failed Bounce trade with short-side features.
    Uses signal.bar_index to find the trigger bar and look back for
    breakdown/bounce bars using DayState info embedded in the signal.
    """
    sig = trade.signal
    if sig.setup_id != SetupId.FAILED_BOUNCE:
        return None

    bi = sig.bar_index
    if bi < 0 or bi >= len(bars):
        return None

    trigger = bars[bi]
    trig_rng = trigger.high - trigger.low

    # ── Signal-level features (already on signal) ──
    market_trend = sig.market_trend       # -1, 0, +1
    rs_market = sig.rs_market if not math.isnan(sig.rs_market) else 0.0
    rs_sector = sig.rs_sector if not math.isnan(sig.rs_sector) else 0.0
    quality = sig.quality_score

    # ── Trigger bar anatomy ──
    if trig_rng > 0:
        trigger_close_loc = (trigger.close - trigger.low) / trig_rng  # 0=low, 1=high
        # For shorts, opposing wick = lower wick (rejection of lows = bad for shorts)
        lower_wick = min(trigger.close, trigger.open) - trigger.low
        opposing_wick_pct = lower_wick / trig_rng
    else:
        trigger_close_loc = 0.5
        opposing_wick_pct = 0.0

    # ── Compute intra-ATR from recent bars ──
    lookback = min(14, bi)
    if lookback >= 5:
        ranges = [bars[bi - j].high - bars[bi - j].low for j in range(1, lookback + 1)]
        i_atr = statistics.mean(ranges)
    else:
        i_atr = trig_rng if trig_rng > 0 else 0.01

    if i_atr <= 0:
        i_atr = 0.01

    # ── Distance from VWAP and EMA9 ──
    vwap_val = trigger._vwap if hasattr(trigger, '_vwap') and trigger._vwap > 0 else 0.0
    ema9_val = trigger._e9 if hasattr(trigger, '_e9') and trigger._e9 > 0 else 0.0

    dist_vwap_atr = (trigger.close - vwap_val) / i_atr if vwap_val > 0 else 0.0
    dist_ema9_atr = (trigger.close - ema9_val) / i_atr if ema9_val > 0 else 0.0

    # ── Time bucket ──
    hhmm = sig.timestamp.hour * 100 + sig.timestamp.minute
    if hhmm < 1100:
        time_bucket = "AM"
    elif hhmm < 1330:
        time_bucket = "MID"
    else:
        time_bucket = "LATE"

    # ── Bounce / Breakdown lookback ──
    # Walk backward from trigger bar to find bounce bar and breakdown bar.
    # FB structure: breakdown (bd) → bounce (bounced) → confirmation (trigger).
    # Bounce bar: closest bar before trigger where high approaches fb_level from below.
    # Breakdown bar: the bar that first broke below the level.
    # We'll approximate by scanning backward up to 12 bars.

    bounce_bar = None
    breakdown_bar = None
    bounce_depth_pct = 0.0
    bounce_vol_ratio = 1.0
    rejection_wick_pct = 0.0

    # Heuristic: bounce bar is 1-4 bars before trigger (highest bar in window)
    scan_start = max(0, bi - 8)
    scan_end = bi  # exclusive — don't include trigger itself

    if scan_end > scan_start:
        window_bars = bars[scan_start:scan_end]
        # Bounce bar = bar with highest high in the window (the "bounce attempt")
        bounce_bar = max(window_bars, key=lambda b: b.high)
        bounce_idx = scan_start + window_bars.index(bounce_bar)

        # Breakdown bar = bar with lowest close before bounce (deepest bar)
        pre_bounce = bars[scan_start:bounce_idx] if bounce_idx > scan_start else []
        if pre_bounce:
            breakdown_bar = min(pre_bounce, key=lambda b: b.close)
        elif bounce_idx > 0:
            # bounce is first bar in window; breakdown might be 1 bar earlier
            bd_start = max(0, scan_start - 3)
            bd_window = bars[bd_start:scan_start]
            if bd_window:
                breakdown_bar = min(bd_window, key=lambda b: b.close)

    # ── Bounce features ──
    if bounce_bar is not None:
        b_rng = bounce_bar.high - bounce_bar.low
        if b_rng > 0:
            # Rejection wick = upper wick on bounce bar (trapped buyers)
            upper_wick = bounce_bar.high - max(bounce_bar.open, bounce_bar.close)
            rejection_wick_pct = upper_wick / b_rng
        else:
            rejection_wick_pct = 0.0

    if breakdown_bar is not None and bounce_bar is not None:
        bd_range = breakdown_bar.high - breakdown_bar.low
        if bd_range > 0:
            # Bounce depth: how much of the breakdown bar's range was recovered
            bounce_depth_pct = (bounce_bar.high - breakdown_bar.low) / bd_range
        # Volume comparison
        if breakdown_bar.volume > 0:
            bounce_vol_ratio = bounce_bar.volume / breakdown_bar.volume

    # ── 1-bar follow-through ──
    follow_through_1b = 0.0
    if bi + 1 < len(bars):
        next_bar = bars[bi + 1]
        # For shorts, follow-through = (entry - next_close) / ATR
        # Positive = price went down (good for short)
        follow_through_1b = (trigger.close - next_bar.close) / i_atr

    # ── Risk/stop analysis ──
    stop_dist_atr = abs(sig.stop_price - sig.entry_price) / i_atr if i_atr > 0 else 0.0

    # ── Level tag from confluence tags ──
    level_tag = "UNK"
    for tag in sig.confluence_tags:
        if "ORL" in str(tag).upper():
            level_tag = "ORL"
            break
        elif "VWAP" in str(tag).upper():
            level_tag = "VWAP"
            break
        elif "SWING" in str(tag).upper():
            level_tag = "SWING"
            break

    return {
        "sym": sym,
        "date": sig.timestamp.strftime("%Y-%m-%d"),
        "time": sig.timestamp.strftime("%H:%M"),
        "hhmm": hhmm,
        "pnl": trade.pnl_points,
        "pnl_rr": trade.pnl_rr,
        "exit_reason": trade.exit_reason,
        "bars_held": trade.bars_held,
        "winner": trade.pnl_points > 0,
        # Features
        "market_trend": market_trend,
        "rs_market": rs_market,
        "rs_sector": rs_sector,
        "quality": quality,
        "trigger_close_loc": trigger_close_loc,
        "opposing_wick_pct": opposing_wick_pct,
        "bounce_depth_pct": bounce_depth_pct,
        "bounce_vol_ratio": bounce_vol_ratio,
        "rejection_wick_pct": rejection_wick_pct,
        "dist_vwap_atr": dist_vwap_atr,
        "dist_ema9_atr": dist_ema9_atr,
        "time_bucket": time_bucket,
        "follow_through_1b": follow_through_1b,
        "stop_dist_atr": stop_dist_atr,
        "level_tag": level_tag,
        "tradability_short": sig.tradability_short,
    }


# ══════════════════════════════════════════════════════════════
#  FEATURE TESTS (coarse thresholds, pre-declared)
# ══════════════════════════════════════════════════════════════

FEATURE_TESTS = [
    # (name, field, operator, threshold, description)
    ("market_bearish", "market_trend", "<=", 0, "Market trend <= 0 (not bullish)"),
    ("market_strongly_bear", "market_trend", "==", -1, "Market trend = -1 (bearish)"),
    ("rs_weak_vs_mkt", "rs_market", "<=", -0.003, "RS vs SPY <= -0.3%"),
    ("rs_weak_vs_sec", "rs_sector", "<=", -0.003, "RS vs sector <= -0.3%"),
    ("bounce_shallow", "bounce_depth_pct", "<=", 1.5, "Bounce depth <= 150% of BD range"),
    ("bounce_weak_vol", "bounce_vol_ratio", "<=", 0.8, "Bounce vol <= 80% of BD vol"),
    ("wick_rejection", "rejection_wick_pct", ">=", 0.25, "Bounce bar upper wick >= 25%"),
    ("below_vwap", "dist_vwap_atr", "<=", -0.3, "Trigger close >= 0.3 ATR below VWAP"),
    ("below_ema9", "dist_ema9_atr", "<=", -0.3, "Trigger close >= 0.3 ATR below EMA9"),
    ("am_session", "time_bucket", "==", "AM", "Entry before 11:00 AM"),
    ("trigger_close_low", "trigger_close_loc", "<=", 0.30, "Trigger bar closes in lower 30%"),
    ("quality_high", "quality", ">=", 4, "Quality score >= 4"),
    ("stop_tight", "stop_dist_atr", "<=", 1.5, "Stop distance <= 1.5 ATR"),
    ("tradability_short_neg", "tradability_short", "<=", 0.0, "Tradability short score <= 0"),
]


def test_feature(annotations: List[dict], feature_name: str, field: str,
                 operator: str, threshold) -> Tuple[List[dict], List[dict]]:
    """Split annotations into pass/fail based on feature test."""
    pass_trades = []
    fail_trades = []
    for a in annotations:
        val = a.get(field)
        if val is None:
            fail_trades.append(a)
            continue
        if operator == "<=":
            passed = val <= threshold
        elif operator == ">=":
            passed = val >= threshold
        elif operator == "==":
            passed = val == threshold
        elif operator == "<":
            passed = val < threshold
        elif operator == ">":
            passed = val > threshold
        else:
            passed = False
        if passed:
            pass_trades.append(a)
        else:
            fail_trades.append(a)
    return pass_trades, fail_trades


def metrics_from_annotations(anns: List[dict]) -> dict:
    """Compute metrics from annotation dicts (not Trade objects)."""
    n = len(anns)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "exp": 0.0,
                "stop_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0}
    wins = [a for a in anns if a["pnl"] > 0]
    losses = [a for a in anns if a["pnl"] <= 0]
    pnl = sum(a["pnl"] for a in anns)
    gw = sum(a["pnl"] for a in wins)
    gl = abs(sum(a["pnl"] for a in losses))
    pf = gw / gl if gl > 0 else float("inf")
    stopped = sum(1 for a in anns if a["exit_reason"] == "stop")
    avg_win = gw / len(wins) if wins else 0.0
    avg_loss = gl / len(losses) if losses else 0.0
    return {"n": n, "wr": len(wins) / n * 100, "pf": pf, "pnl": pnl,
            "exp": sum(a["pnl_rr"] for a in anns) / n,
            "stop_rate": stopped / n * 100, "avg_win": avg_win,
            "avg_loss": avg_loss}


def print_ann_metrics(label, m):
    print(f"  {label:40s}  N={m['n']:3d}  WR={m['wr']:5.1f}%  "
          f"PF={pf_str(m['pf']):>6s}  PnL={m['pnl']:+7.2f}  "
          f"Exp={m['exp']:+.2f}R  Stop={m['stop_rate']:4.1f}%")


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="all94")
    args = parser.parse_args()

    from .market_context import SECTOR_MAP, get_sector_etf

    # Determine universe
    if args.universe == "all94":
        # Use ALL data files, excluding index ETFs and sector ETFs
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

    print("=" * 80)
    print("FAILED BOUNCE SHORT-SIDE FEATURE STUDY")
    print("=" * 80)
    print(f"Universe: {len(symbols)} symbols\n")

    # Load SPY/QQQ for market context
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    spy_day_info = classify_spy_days(spy_bars) if spy_bars else {}

    # Load sector ETF bars
    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    # Config: enable FB, enable all shorts, keep market context
    cfg = OverlayConfig(
        show_reversal_setups=False,
        show_trend_setups=True,
        show_ema_retest=False,
        show_ema_mean_rev=False,
        show_ema_pullback=False,
        show_second_chance=True,
        show_spencer=False,
        show_failed_bounce=True,       # ENABLE FB
        show_ema_scalp=False,
        show_ema_fpip=False,
        show_sc_v2=False,
        vk_long_only=False,
        sc_long_only=False,
        sp_long_only=False,
        use_market_context=True,
        use_sector_context=True,
        use_tradability_gate=False,
        fb_min_quality=0,              # accept ALL FB trades for measurement
    )

    # Run backtest and collect FB trades + bars per symbol
    all_annotations: List[dict] = []
    all_fb_trades: List[Trade] = []
    symbol_for_trade: Dict[int, str] = {}
    bars_for_sym: Dict[str, List[Bar]] = {}

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

        fb_trades = [t for t in result.trades if t.signal.setup_id == SetupId.FAILED_BOUNCE]
        bars_for_sym[sym] = bars

        for t in fb_trades:
            symbol_for_trade[id(t)] = sym
            all_fb_trades.append(t)
            ann = annotate_fb_trade(t, bars, sym)
            if ann is not None:
                # Add SPY day color
                trade_date = t.signal.timestamp.date()
                ann["spy_day_color"] = spy_day_info.get(trade_date, "UNK")
                all_annotations.append(ann)

    print(f"Total FB trades found: {len(all_fb_trades)}")
    print(f"Annotated: {len(all_annotations)}\n")

    if not all_annotations:
        print("NO FAILED BOUNCE TRADES FOUND. Cannot proceed.")
        return

    # ══════════════════════════════════════════════════════════
    # PHASE 1: RAW ANATOMY STATS
    # ══════════════════════════════════════════════════════════
    print("=" * 80)
    print("PHASE 1: RAW FB TRADE ANATOMY")
    print("=" * 80)

    winners = [a for a in all_annotations if a["winner"]]
    losers = [a for a in all_annotations if not a["winner"]]

    print(f"\n  Total: {len(all_annotations)}  |  Winners: {len(winners)}  |  Losers: {len(losers)}")

    m_all = metrics_from_annotations(all_annotations)
    m_win = metrics_from_annotations(winners)
    m_lose = metrics_from_annotations(losers)

    print_ann_metrics("All FB trades", m_all)
    print()

    # Feature stats: winners vs losers
    numeric_features = [
        ("market_trend", "Market trend"),
        ("rs_market", "RS vs SPY"),
        ("rs_sector", "RS vs sector"),
        ("bounce_depth_pct", "Bounce depth %"),
        ("bounce_vol_ratio", "Bounce vol ratio"),
        ("rejection_wick_pct", "Rejection wick %"),
        ("dist_vwap_atr", "Dist from VWAP (ATR)"),
        ("dist_ema9_atr", "Dist from EMA9 (ATR)"),
        ("trigger_close_loc", "Trigger close loc"),
        ("stop_dist_atr", "Stop dist (ATR)"),
        ("follow_through_1b", "1-bar follow-thru"),
        ("quality", "Quality score"),
        ("tradability_short", "Tradability short"),
    ]

    print(f"\n  {'Feature':30s}  {'Winners':>10s}  {'Losers':>10s}  {'Delta':>10s}")
    print(f"  {'-'*30}  {'-'*10}  {'-'*10}  {'-'*10}")

    for field, label in numeric_features:
        w_vals = [a[field] for a in winners if a[field] is not None and not (isinstance(a[field], float) and math.isnan(a[field]))]
        l_vals = [a[field] for a in losers if a[field] is not None and not (isinstance(a[field], float) and math.isnan(a[field]))]
        w_mean = statistics.mean(w_vals) if w_vals else 0.0
        l_mean = statistics.mean(l_vals) if l_vals else 0.0
        delta = w_mean - l_mean
        print(f"  {label:30s}  {w_mean:10.3f}  {l_mean:10.3f}  {delta:+10.3f}")

    # Categorical features
    print(f"\n  Time bucket distribution:")
    for bucket in ["AM", "MID", "LATE"]:
        n_w = sum(1 for a in winners if a["time_bucket"] == bucket)
        n_l = sum(1 for a in losers if a["time_bucket"] == bucket)
        total = n_w + n_l
        wr = n_w / total * 100 if total > 0 else 0.0
        pnl = sum(a["pnl"] for a in all_annotations if a["time_bucket"] == bucket)
        print(f"    {bucket:6s}  N={total:3d}  W={n_w:2d}  L={n_l:2d}  WR={wr:5.1f}%  PnL={pnl:+.2f}")

    print(f"\n  Level tag distribution:")
    for tag in ["VWAP", "ORL", "SWING", "UNK"]:
        n_w = sum(1 for a in winners if a["level_tag"] == tag)
        n_l = sum(1 for a in losers if a["level_tag"] == tag)
        total = n_w + n_l
        wr = n_w / total * 100 if total > 0 else 0.0
        pnl = sum(a["pnl"] for a in all_annotations if a["level_tag"] == tag)
        if total > 0:
            print(f"    {tag:6s}  N={total:3d}  W={n_w:2d}  L={n_l:2d}  WR={wr:5.1f}%  PnL={pnl:+.2f}")

    print(f"\n  SPY day color distribution:")
    for color in ["GREEN", "RED", "FLAT"]:
        n_w = sum(1 for a in winners if a["spy_day_color"] == color)
        n_l = sum(1 for a in losers if a["spy_day_color"] == color)
        total = n_w + n_l
        wr = n_w / total * 100 if total > 0 else 0.0
        pnl = sum(a["pnl"] for a in all_annotations if a["spy_day_color"] == color)
        if total > 0:
            print(f"    {color:6s}  N={total:3d}  W={n_w:2d}  L={n_l:2d}  WR={wr:5.1f}%  PnL={pnl:+.2f}")

    # ══════════════════════════════════════════════════════════
    # PHASE 2: FEATURE PASS/FAIL TESTS
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("PHASE 2: FEATURE PASS/FAIL TESTS (coarse thresholds)")
    print("=" * 80)
    print(f"\n  {'Feature':30s}  {'Pass':>6s}  {'P_WR':>6s}  {'P_PF':>6s}  {'P_PnL':>8s}  "
          f"{'Fail':>6s}  {'F_WR':>6s}  {'F_PF':>6s}  {'F_PnL':>8s}  {'Delta_WR':>9s}")
    print(f"  {'-'*30}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*8}  "
          f"{'-'*6}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*9}")

    feature_results = []
    for name, field, op, threshold, desc in FEATURE_TESTS:
        pass_anns, fail_anns = test_feature(all_annotations, name, field, op, threshold)
        mp = metrics_from_annotations(pass_anns)
        mf = metrics_from_annotations(fail_anns)
        delta_wr = mp["wr"] - mf["wr"] if mf["n"] > 0 else 0.0
        delta_pf = (mp["pf"] - mf["pf"]) if mf["n"] > 0 and mp["pf"] < 999 and mf["pf"] < 999 else 0.0

        print(f"  {name:30s}  {mp['n']:6d}  {mp['wr']:5.1f}%  {pf_str(mp['pf']):>6s}  {mp['pnl']:+8.2f}  "
              f"{mf['n']:6d}  {mf['wr']:5.1f}%  {pf_str(mf['pf']):>6s}  {mf['pnl']:+8.2f}  {delta_wr:+8.1f}%")

        feature_results.append({
            "name": name, "desc": desc, "field": field, "op": op, "threshold": threshold,
            "pass_n": mp["n"], "pass_wr": mp["wr"], "pass_pf": mp["pf"], "pass_pnl": mp["pnl"],
            "fail_n": mf["n"], "fail_wr": mf["wr"], "fail_pf": mf["pf"], "fail_pnl": mf["pnl"],
            "delta_wr": delta_wr, "delta_pf": delta_pf,
        })

    # ══════════════════════════════════════════════════════════
    # PHASE 3: STOP LOGIC ANALYSIS
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("PHASE 3: STOP LOGIC ANALYSIS — IS STOP PLACEMENT THE BIGGER PROBLEM?")
    print("=" * 80)

    stopped_trades = [a for a in all_annotations if a["exit_reason"] == "stop"]
    time_exits = [a for a in all_annotations if a["exit_reason"] in ("eod", "time", "opposing")]
    target_exits = [a for a in all_annotations if a["exit_reason"] == "target"]
    other_exits = [a for a in all_annotations if a["exit_reason"] not in ("stop", "eod", "time", "opposing", "target")]

    print(f"\n  Exit reason breakdown:")
    print(f"    Stopped:   {len(stopped_trades):3d}  ({len(stopped_trades)/len(all_annotations)*100:.1f}%)")
    print(f"    EOD/Time:  {len(time_exits):3d}  ({len(time_exits)/len(all_annotations)*100:.1f}%)")
    print(f"    Target:    {len(target_exits):3d}  ({len(target_exits)/len(all_annotations)*100:.1f}%)")
    if other_exits:
        print(f"    Other:     {len(other_exits):3d}  ({len(other_exits)/len(all_annotations)*100:.1f}%)")

    # Average loss on stops vs average loss on time exits
    stop_losses = [a["pnl"] for a in stopped_trades if a["pnl"] <= 0]
    time_losses = [a["pnl"] for a in time_exits if a["pnl"] <= 0]
    stop_wins = [a["pnl"] for a in stopped_trades if a["pnl"] > 0]
    time_wins = [a["pnl"] for a in time_exits if a["pnl"] > 0]

    print(f"\n  Stop analysis:")
    if stop_losses:
        print(f"    Avg loss on stops:  {statistics.mean(stop_losses):+.2f} pts  (N={len(stop_losses)})")
    if time_losses:
        print(f"    Avg loss on time:   {statistics.mean(time_losses):+.2f} pts  (N={len(time_losses)})")
    if stop_wins:
        print(f"    Avg WIN on stops:   {statistics.mean(stop_wins):+.2f} pts  (N={len(stop_wins)})")
    if time_wins:
        print(f"    Avg WIN on time:    {statistics.mean(time_wins):+.2f} pts  (N={len(time_wins)})")

    # Stop distance analysis: tight vs wide stops
    print(f"\n  Stop distance distribution:")
    for label, lo, hi in [("Tight (<1.0 ATR)", 0, 1.0), ("Medium (1-2 ATR)", 1.0, 2.0), ("Wide (>2 ATR)", 2.0, 99)]:
        subset = [a for a in all_annotations if lo <= a["stop_dist_atr"] < hi]
        if subset:
            m_sub = metrics_from_annotations(subset)
            print_ann_metrics(label, m_sub)

    # Bars held analysis
    print(f"\n  Bars held distribution:")
    for label, lo, hi in [("Quick (1-3 bars)", 1, 4), ("Medium (4-8 bars)", 4, 9), ("Slow (9+ bars)", 9, 999)]:
        subset = [a for a in all_annotations if lo <= a["bars_held"] < hi]
        if subset:
            m_sub = metrics_from_annotations(subset)
            print_ann_metrics(label, m_sub)

    # ══════════════════════════════════════════════════════════
    # PHASE 4: TOP DISCRIMINATORS
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("PHASE 4: TOP DISCRIMINATORS — RANKED BY WR SEPARATION")
    print("=" * 80)

    # Rank by absolute delta_wr, but only features with at least 3 trades in each subset
    valid_results = [r for r in feature_results if r["pass_n"] >= 3 and r["fail_n"] >= 3]
    valid_results.sort(key=lambda r: abs(r["delta_wr"]), reverse=True)

    print(f"\n  {'Rank':>4s}  {'Feature':30s}  {'ΔWR':>8s}  {'Pass WR':>8s}  {'Fail WR':>8s}  "
          f"{'P_N':>4s}  {'F_N':>4s}  {'Description'}")
    print(f"  {'-'*4}  {'-'*30}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*4}  {'-'*4}  {'-'*40}")

    for rank, r in enumerate(valid_results[:10], 1):
        print(f"  {rank:4d}  {r['name']:30s}  {r['delta_wr']:+7.1f}%  {r['pass_wr']:7.1f}%  "
              f"{r['fail_wr']:7.1f}%  {r['pass_n']:4d}  {r['fail_n']:4d}  {r['desc']}")

    # ══════════════════════════════════════════════════════════
    # PHASE 5: TRADE LOG
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("PHASE 5: FULL TRADE LOG")
    print("=" * 80)

    print(f"\n  {'Sym':>5s}  {'Date':>10s}  {'Time':>5s}  {'PnL':>7s}  {'Exit':>8s}  {'Q':>2s}  "
          f"{'MktTr':>5s}  {'RS_M':>7s}  {'BncDp':>6s}  {'BncVR':>6s}  {'RejWk':>6s}  "
          f"{'DstVW':>6s}  {'StpAt':>6s}  {'FT1b':>6s}  {'DayC':>5s}")
    print(f"  {'-'*5}  {'-'*10}  {'-'*5}  {'-'*7}  {'-'*8}  {'-'*2}  "
          f"{'-'*5}  {'-'*7}  {'-'*6}  {'-'*6}  {'-'*6}  "
          f"{'-'*6}  {'-'*6}  {'-'*6}  {'-'*5}")

    sorted_anns = sorted(all_annotations, key=lambda a: (a["date"], a["time"]))
    for a in sorted_anns:
        w_marker = "W" if a["winner"] else "L"
        print(f"  {a['sym']:>5s}  {a['date']:>10s}  {a['time']:>5s}  {a['pnl']:+7.2f}  "
              f"{a['exit_reason']:>8s}  {a['quality']:2d}  "
              f"{a['market_trend']:+5d}  {a['rs_market']:+7.4f}  "
              f"{a['bounce_depth_pct']:6.2f}  {a['bounce_vol_ratio']:6.2f}  "
              f"{a['rejection_wick_pct']:6.2f}  {a['dist_vwap_atr']:+6.2f}  "
              f"{a['stop_dist_atr']:6.2f}  {a['follow_through_1b']:+6.2f}  "
              f"{a['spy_day_color']:>5s}  {w_marker}")

    # ══════════════════════════════════════════════════════════
    # PHASE 6: SUMMARY & RECOMMENDATION
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("PHASE 6: SUMMARY")
    print("=" * 80)

    print(f"\n  Total FB trades: {len(all_annotations)}")
    m = metrics_from_annotations(all_annotations)
    print(f"  Overall: WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  PnL={m['pnl']:+.2f}  Stop%={m['stop_rate']:.1f}%")

    if valid_results:
        top = valid_results[0]
        print(f"\n  Top discriminator: {top['name']}")
        print(f"    Pass: N={top['pass_n']}  WR={top['pass_wr']:.1f}%  PF={pf_str(top['pass_pf'])}  PnL={top['pass_pnl']:+.2f}")
        print(f"    Fail: N={top['fail_n']}  WR={top['fail_wr']:.1f}%  PF={pf_str(top['fail_pf'])}  PnL={top['fail_pnl']:+.2f}")
        print(f"    WR separation: {top['delta_wr']:+.1f}%")

    if len(valid_results) >= 2:
        r2 = valid_results[1]
        print(f"\n  2nd discriminator: {r2['name']}")
        print(f"    Pass: N={r2['pass_n']}  WR={r2['pass_wr']:.1f}%  PF={pf_str(r2['pass_pf'])}  PnL={r2['pass_pnl']:+.2f}")
        print(f"    Fail: N={r2['fail_n']}  WR={r2['fail_wr']:.1f}%  PF={pf_str(r2['fail_pf'])}  PnL={r2['fail_pnl']:+.2f}")
        print(f"    WR separation: {r2['delta_wr']:+.1f}%")

    # Stop vs entry verdict
    print(f"\n  STOP VS ENTRY ANALYSIS:")
    if stopped_trades:
        stop_pnl = sum(a["pnl"] for a in stopped_trades)
        non_stop_pnl = sum(a["pnl"] for a in all_annotations if a["exit_reason"] != "stop")
        print(f"    Stop exit PnL: {stop_pnl:+.2f}  ({len(stopped_trades)} trades)")
        print(f"    Non-stop PnL:  {non_stop_pnl:+.2f}  ({len(all_annotations) - len(stopped_trades)} trades)")
        if abs(stop_pnl) > abs(non_stop_pnl) * 1.5 and stop_pnl < 0:
            print(f"    VERDICT: Stop logic is likely the bigger problem")
        else:
            print(f"    VERDICT: Entry selection is likely the bigger problem")

    # FB viability
    print(f"\n  FB VIABILITY:")
    if m["n"] < 15:
        print(f"    ⚠ SAMPLE TOO SMALL (N={m['n']}). Need 15+ trades for meaningful feature study.")
        print(f"    Recommendation: Keep FB disabled until more data accumulates.")
    elif m["pf"] >= 1.0 and m["wr"] >= 40:
        print(f"    FB shows baseline viability (PF={pf_str(m['pf'])}, WR={m['wr']:.1f}%).")
        print(f"    Potential core of short-side book with feature gating.")
    else:
        print(f"    FB is currently marginal (PF={pf_str(m['pf'])}, WR={m['wr']:.1f}%).")
        if valid_results and valid_results[0]["delta_wr"] > 15:
            print(f"    But top discriminator ({valid_results[0]['name']}) shows {valid_results[0]['delta_wr']:+.1f}% WR separation.")
            print(f"    Feature gating could rescue the setup.")
        else:
            print(f"    No single feature provides strong enough separation.")
            print(f"    Recommendation: Keep FB disabled; revisit with more data or structural changes.")


if __name__ == "__main__":
    main()
