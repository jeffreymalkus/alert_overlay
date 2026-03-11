"""
Failed Bounce — Bounce Quality Study.

For each FB trade, reconstructs the breakdown→bounce→confirmation sequence
from the raw bars and measures pre-failure bounce characteristics.

Features per FB trade:
  1. bounce_depth_pct     — bounce high recovery as % of selloff range
  2. bounce_bar_count     — number of bars in the bounce phase
  3. bounce_vol_vs_sell   — total bounce volume / total selloff volume
  4. bounce_overlap       — fraction of bounce bars whose range overlaps prior bar (grindiness)
  5. bounce_slope_atr     — (bounce_high - bounce_start) / (bar_count × ATR) — normalized slope
  6. bounce_reclaim_pct   — how close bounce high got to the broken level (0=nowhere, 1=touched)
  7. bounce_reached_vwap  — did bounce bar's high reach within 0.1 ATR of VWAP?
  8. bounce_reached_ema9  — did bounce bar's high reach within 0.1 ATR of EMA9?
  9. rejection_wick_pct   — upper wick of bounce bar as % of bar range
  10. rejection_body_pct  — body of rejection bar as % of bar range
  11. rejection_bearish   — was the rejection bar bearish (close < open)?
  12. rs_market_at_bounce — RS vs SPY at the bounce bar
  13. rs_sector_at_bounce — RS vs sector at the bounce bar
  14. market_trend_bounce — market trend at the bounce bar
  15. selloff_range_atr   — selloff (breakdown) range in ATR units

Usage:
    python -m alert_overlay.fb_bounce_quality_study --universe all94
"""

import argparse
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import NaN, SetupId, Bar

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent / "data"
WATCHLIST_FILE = Path(__file__).parent / "watchlist.txt"


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


def compute_metrics_from_anns(anns):
    n = len(anns)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "exp": 0.0,
                "stop_rate": 0.0, "max_dd": 0.0}
    wins = [a for a in anns if a["pnl"] > 0]
    losses = [a for a in anns if a["pnl"] <= 0]
    pnl = sum(a["pnl"] for a in anns)
    gw = sum(a["pnl"] for a in wins)
    gl = abs(sum(a["pnl"] for a in losses))
    pf = gw / gl if gl > 0 else float("inf")
    stopped = sum(1 for a in anns if a["exit_reason"] == "stop")
    cum = pk = dd = 0.0
    for a in sorted(anns, key=lambda a: a["entry_time"]):
        cum += a["pnl"]
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum
    return {"n": n, "wr": len(wins) / n * 100, "pf": pf, "pnl": pnl,
            "exp": sum(a["pnl_rr"] for a in anns) / n,
            "stop_rate": stopped / n * 100, "max_dd": dd}


def print_metrics(label, m):
    print(f"  {label:40s}  N={m['n']:3d}  WR={m['wr']:5.1f}%  PF={pf_str(m['pf']):>6s}  "
          f"PnL={m['pnl']:+7.2f}  Exp={m['exp']:+.2f}R  Stop={m['stop_rate']:4.1f}%")


# ══════════════════════════════════════════════════════════════
#  BOUNCE RECONSTRUCTION
# ══════════════════════════════════════════════════════════════

def reconstruct_bounce(trade: Trade, bars: List[Bar], sym: str) -> Optional[dict]:
    """
    Reconstruct the BD→bounce→confirm sequence for a single FB trade.

    The confirmation bar (entry) is at signal.bar_index.
    Walk backward to find:
      1. Bounce bar: the bar with highest high between BD and confirm
      2. Breakdown bar: the bar with lowest close before the bounce bar
      3. Pre-breakdown context: bars before BD for selloff measurement
    """
    sig = trade.signal
    if sig.setup_id != SetupId.FAILED_BOUNCE:
        return None

    bi = sig.bar_index  # confirmation bar index
    if bi < 2 or bi >= len(bars):
        return None

    confirm_bar = bars[bi]

    # Compute intra-ATR at confirmation
    lookback = min(14, bi)
    if lookback >= 5:
        ranges = [bars[bi - j].high - bars[bi - j].low for j in range(1, lookback + 1)]
        i_atr = statistics.mean(ranges)
    else:
        i_atr = confirm_bar.high - confirm_bar.low
    if i_atr <= 0:
        i_atr = 0.01

    # ── Scan backward to find the bounce-phase bars ──
    # FB structure: BD bar → (0-6 bars) → bounce bar → (0-3 bars) → confirm bar
    # The bounce bar has the highest high in the window between BD and confirm.
    # The BD bar has the lowest close before the bounce.

    scan_start = max(0, bi - 12)  # max lookback (bounce_window + confirm_window + margin)
    window = bars[scan_start:bi]  # exclude confirm bar itself

    if len(window) < 2:
        return None

    # Find bounce bar = bar with highest high in window
    bounce_bar = max(window, key=lambda b: b.high)
    bounce_idx = scan_start + window.index(bounce_bar)

    # Find breakdown bar = bar with lowest close BEFORE the bounce bar
    pre_bounce = bars[scan_start:bounce_idx]
    if not pre_bounce:
        # bounce is first bar in window — try extending back
        ext_start = max(0, scan_start - 4)
        pre_bounce = bars[ext_start:bounce_idx]
        if not pre_bounce:
            return None
        scan_start = ext_start

    bd_bar = min(pre_bounce, key=lambda b: b.close)
    bd_idx = scan_start + list(bars[scan_start:bounce_idx]).index(bd_bar)

    # ── Selloff measurement ──
    # Selloff range: from the high before breakdown to the BD bar's low
    pre_bd_bars = bars[max(0, bd_idx - 5):bd_idx]
    if pre_bd_bars:
        selloff_high = max(b.high for b in pre_bd_bars)
    else:
        selloff_high = bd_bar.high
    selloff_low = bd_bar.low
    selloff_range = selloff_high - selloff_low
    selloff_range_atr = selloff_range / i_atr if i_atr > 0 else 0.0

    # Total selloff volume (BD bar + bars leading into BD)
    selloff_bars = bars[max(0, bd_idx - 2):bd_idx + 1]
    selloff_vol = sum(b.volume for b in selloff_bars) if selloff_bars else 1.0

    # ── Bounce measurement ──
    bounce_bars_list = bars[bd_idx + 1:bounce_idx + 1]  # bars from after BD to bounce (inclusive)
    bounce_bar_count = len(bounce_bars_list)

    if bounce_bar_count == 0:
        # BD and bounce are adjacent — use bounce bar alone
        bounce_bars_list = [bounce_bar]
        bounce_bar_count = 1

    # Bounce depth: how much of the selloff was recovered
    bounce_high = bounce_bar.high
    bounce_start = bd_bar.low  # lowest point of selloff
    bounce_recovery = bounce_high - bounce_start
    bounce_depth_pct = bounce_recovery / selloff_range if selloff_range > 0 else 0.0

    # Bounce volume
    bounce_vol = sum(b.volume for b in bounce_bars_list)
    bounce_vol_vs_sell = bounce_vol / selloff_vol if selloff_vol > 0 else 1.0

    # Bounce overlap / grindiness: fraction of bounce bars where low < prior bar's high
    overlap_count = 0
    for k in range(1, len(bounce_bars_list)):
        if bounce_bars_list[k].low < bounce_bars_list[k - 1].high:
            overlap_count += 1
    bounce_overlap = overlap_count / (len(bounce_bars_list) - 1) if len(bounce_bars_list) > 1 else 1.0

    # Bounce slope (normalized by ATR and bar count)
    if bounce_bar_count > 0 and i_atr > 0:
        bounce_slope_atr = (bounce_high - bounce_start) / (bounce_bar_count * i_atr)
    else:
        bounce_slope_atr = 0.0

    # Bounce reclaim: how close bounce got to the broken level
    # level ≈ VWAP or structural level. We approximate from signal metadata.
    # The entry requires close < VWAP and close < EMA9, so VWAP is above entry.
    # Use the VWAP at the bounce bar as proxy for the level.
    vwap_at_bounce = bounce_bar._vwap if hasattr(bounce_bar, '_vwap') and bounce_bar._vwap > 0 else 0.0
    ema9_at_bounce = bounce_bar._e9 if hasattr(bounce_bar, '_e9') and bounce_bar._e9 > 0 else 0.0

    # Reclaim pct: how close bounce high got to VWAP (1.0 = touched, >1.0 = exceeded)
    if vwap_at_bounce > 0 and selloff_range > 0:
        dist_to_level = vwap_at_bounce - bounce_start
        bounce_reclaim_pct = bounce_recovery / dist_to_level if dist_to_level > 0 else 0.0
    else:
        bounce_reclaim_pct = bounce_depth_pct  # fallback to depth

    # Reached VWAP / EMA9?
    bounce_reached_vwap = vwap_at_bounce > 0 and bounce_high >= vwap_at_bounce - 0.1 * i_atr
    bounce_reached_ema9 = ema9_at_bounce > 0 and bounce_high >= ema9_at_bounce - 0.1 * i_atr

    # ── Rejection bar anatomy (the bounce bar itself) ──
    b_rng = bounce_bar.high - bounce_bar.low
    if b_rng > 0:
        upper_wick = bounce_bar.high - max(bounce_bar.open, bounce_bar.close)
        rejection_wick_pct = upper_wick / b_rng
        body = abs(bounce_bar.close - bounce_bar.open)
        rejection_body_pct = body / b_rng
    else:
        rejection_wick_pct = 0.0
        rejection_body_pct = 0.0
    rejection_bearish = bounce_bar.close < bounce_bar.open

    # ── Signal-level context ──
    market_trend = sig.market_trend
    rs_market = sig.rs_market if not math.isnan(sig.rs_market) else 0.0
    rs_sector = sig.rs_sector if not math.isnan(sig.rs_sector) else 0.0

    # Time bucket
    hhmm = sig.timestamp.hour * 100 + sig.timestamp.minute
    if hhmm < 1100:
        time_bucket = "AM"
    elif hhmm < 1330:
        time_bucket = "MID"
    else:
        time_bucket = "LATE"

    return {
        "sym": sym,
        "date": sig.timestamp.strftime("%Y-%m-%d"),
        "time": sig.timestamp.strftime("%H:%M"),
        "entry_time": sig.timestamp,
        "pnl": trade.pnl_points,
        "pnl_rr": trade.pnl_rr,
        "exit_reason": trade.exit_reason,
        "bars_held": trade.bars_held,
        "winner": trade.pnl_points > 0,
        "quality": sig.quality_score,
        # Bounce features
        "bounce_depth_pct": bounce_depth_pct,
        "bounce_bar_count": bounce_bar_count,
        "bounce_vol_vs_sell": bounce_vol_vs_sell,
        "bounce_overlap": bounce_overlap,
        "bounce_slope_atr": bounce_slope_atr,
        "bounce_reclaim_pct": bounce_reclaim_pct,
        "bounce_reached_vwap": bounce_reached_vwap,
        "bounce_reached_ema9": bounce_reached_ema9,
        # Rejection bar
        "rejection_wick_pct": rejection_wick_pct,
        "rejection_body_pct": rejection_body_pct,
        "rejection_bearish": rejection_bearish,
        # Selloff
        "selloff_range_atr": selloff_range_atr,
        # Context
        "market_trend": market_trend,
        "rs_market": rs_market,
        "rs_sector": rs_sector,
        "time_bucket": time_bucket,
    }


# ══════════════════════════════════════════════════════════════
#  FEATURE TESTS
# ══════════════════════════════════════════════════════════════

FEATURE_TESTS = [
    # (name, field, op, threshold, description)
    ("shallow_bounce", "bounce_depth_pct", "<=", 1.0, "Bounce recovers ≤100% of selloff"),
    ("deep_bounce", "bounce_depth_pct", ">", 2.0, "Bounce recovers >200% of selloff"),
    ("quick_bounce", "bounce_bar_count", "<=", 2, "Bounce lasts ≤2 bars"),
    ("slow_bounce", "bounce_bar_count", ">=", 4, "Bounce lasts ≥4 bars"),
    ("weak_vol_bounce", "bounce_vol_vs_sell", "<=", 0.8, "Bounce vol ≤80% of selloff vol"),
    ("strong_vol_bounce", "bounce_vol_vs_sell", ">", 1.5, "Bounce vol >150% of selloff vol"),
    ("grindy_bounce", "bounce_overlap", ">=", 0.8, "≥80% of bounce bars overlap (grindy)"),
    ("impulsive_bounce", "bounce_overlap", "<=", 0.3, "≤30% overlap (impulsive)"),
    ("flat_slope", "bounce_slope_atr", "<=", 0.5, "Bounce slope ≤0.5 ATR/bar"),
    ("steep_slope", "bounce_slope_atr", ">", 1.5, "Bounce slope >1.5 ATR/bar"),
    ("low_reclaim", "bounce_reclaim_pct", "<=", 0.5, "Bounce reached ≤50% toward level"),
    ("high_reclaim", "bounce_reclaim_pct", ">", 0.9, "Bounce reached >90% toward level"),
    ("reached_vwap", "bounce_reached_vwap", "==", True, "Bounce touched VWAP"),
    ("missed_vwap", "bounce_reached_vwap", "==", False, "Bounce did NOT touch VWAP"),
    ("reached_ema9", "bounce_reached_ema9", "==", True, "Bounce touched EMA9"),
    ("big_wick", "rejection_wick_pct", ">=", 0.40, "Rejection bar upper wick ≥40%"),
    ("small_wick", "rejection_wick_pct", "<=", 0.15, "Rejection bar upper wick ≤15%"),
    ("rejection_bearish", "rejection_bearish", "==", True, "Rejection bar is bearish"),
    ("small_body", "rejection_body_pct", "<=", 0.30, "Rejection bar body ≤30% (doji-ish)"),
    ("big_selloff", "selloff_range_atr", ">=", 2.0, "Selloff ≥2 ATR"),
    ("small_selloff", "selloff_range_atr", "<=", 1.0, "Selloff ≤1 ATR"),
    ("mkt_not_bull", "market_trend", "<=", 0, "Market trend ≤ 0"),
    ("rs_weak", "rs_market", "<=", -0.003, "RS vs SPY ≤ -0.3%"),
    ("am_entry", "time_bucket", "==", "AM", "Entry before 11:00"),
    ("late_entry", "time_bucket", "==", "LATE", "Entry after 13:30"),
]


def test_feature(anns, field, op, threshold):
    pass_list, fail_list = [], []
    for a in anns:
        val = a.get(field)
        if val is None:
            fail_list.append(a)
            continue
        if op == "<=":
            passed = val <= threshold
        elif op == ">=":
            passed = val >= threshold
        elif op == ">":
            passed = val > threshold
        elif op == "<":
            passed = val < threshold
        elif op == "==":
            passed = val == threshold
        else:
            passed = False
        (pass_list if passed else fail_list).append(a)
    return pass_list, fail_list


# ══════════════════════════════════════════════════════════════
#  ARCHETYPE DETECTION
# ══════════════════════════════════════════════════════════════

def score_weak_bounce(a: dict) -> int:
    """
    Score how 'weak' a bounce looks. Higher = weaker = better short candidate.
    Each criterion that suggests the bounce is feeble adds +1.
    """
    score = 0
    if a["bounce_depth_pct"] <= 1.0:          # shallow
        score += 1
    if a["bounce_bar_count"] <= 2:             # quick
        score += 1
    if a["bounce_vol_vs_sell"] <= 0.8:         # low volume
        score += 1
    if a["bounce_slope_atr"] <= 0.5:           # flat
        score += 1
    if a["rejection_wick_pct"] >= 0.30:        # wick rejection
        score += 1
    if a["rejection_bearish"]:                 # bearish rejection bar
        score += 1
    if not a["bounce_reached_vwap"]:           # didn't reclaim VWAP
        score += 1
    if a["bounce_reclaim_pct"] <= 0.6:         # low reclaim
        score += 1
    return score


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

    print("=" * 90)
    print("FAILED BOUNCE — BOUNCE QUALITY STUDY")
    print("=" * 90)
    print(f"Universe: {len(symbols)} symbols\n")

    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    cfg = OverlayConfig(
        show_reversal_setups=False,
        show_trend_setups=True,
        show_ema_retest=False,
        show_ema_mean_rev=False,
        show_ema_pullback=False,
        show_second_chance=True,
        show_spencer=False,
        show_failed_bounce=True,
        show_ema_scalp=False,
        show_ema_fpip=False,
        show_sc_v2=False,
        vk_long_only=False,
        sc_long_only=False,
        sp_long_only=False,
        use_market_context=True,
        use_sector_context=True,
        use_tradability_gate=False,
        fb_min_quality=0,
    )

    all_anns = []

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
        for t in fb_trades:
            ann = reconstruct_bounce(t, bars, sym)
            if ann is not None:
                all_anns.append(ann)

    print(f"Total FB trades annotated: {len(all_anns)}\n")
    if not all_anns:
        print("NO FB TRADES FOUND.")
        return

    winners = [a for a in all_anns if a["winner"]]
    losers = [a for a in all_anns if not a["winner"]]

    # ══════════════════════════════════════════════════════════
    # PHASE 1: RAW BOUNCE ANATOMY — WINNERS vs LOSERS
    # ══════════════════════════════════════════════════════════
    print("=" * 90)
    print("PHASE 1: BOUNCE ANATOMY — WINNERS vs LOSERS")
    print("=" * 90)

    m_all = compute_metrics_from_anns(all_anns)
    print_metrics("All FB trades", m_all)
    print()

    features = [
        ("bounce_depth_pct", "Bounce depth (% of selloff)"),
        ("bounce_bar_count", "Bounce bar count"),
        ("bounce_vol_vs_sell", "Bounce vol / selloff vol"),
        ("bounce_overlap", "Bounce overlap (grindiness)"),
        ("bounce_slope_atr", "Bounce slope (ATR/bar)"),
        ("bounce_reclaim_pct", "Bounce reclaim %"),
        ("rejection_wick_pct", "Rejection wick %"),
        ("rejection_body_pct", "Rejection body %"),
        ("selloff_range_atr", "Selloff range (ATR)"),
        ("market_trend", "Market trend"),
        ("rs_market", "RS vs SPY"),
        ("rs_sector", "RS vs sector"),
    ]

    print(f"  {'Feature':35s}  {'Winners':>10s}  {'Losers':>10s}  {'Delta':>10s}  {'Direction':>10s}")
    print(f"  {'-'*35}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*10}")

    for field, label in features:
        w_vals = [a[field] for a in winners if isinstance(a[field], (int, float)) and not math.isnan(a[field])]
        l_vals = [a[field] for a in losers if isinstance(a[field], (int, float)) and not math.isnan(a[field])]
        w_mean = statistics.mean(w_vals) if w_vals else 0.0
        l_mean = statistics.mean(l_vals) if l_vals else 0.0
        delta = w_mean - l_mean
        # Direction: which way helps shorts? (lower depth = weaker bounce = better for short)
        if field in ("bounce_depth_pct", "bounce_bar_count", "bounce_vol_vs_sell",
                     "bounce_slope_atr", "bounce_reclaim_pct", "rejection_body_pct"):
            direction = "W<L=good" if delta < 0 else "W>L=bad"
        elif field in ("rejection_wick_pct", "selloff_range_atr"):
            direction = "W>L=good" if delta > 0 else "W<L=bad"
        else:
            direction = ""
        print(f"  {label:35s}  {w_mean:10.3f}  {l_mean:10.3f}  {delta:+10.3f}  {direction:>10s}")

    # Boolean features
    print(f"\n  {'Boolean feature':35s}  {'W_True':>7s}  {'W_False':>8s}  {'L_True':>7s}  {'L_False':>8s}")
    print(f"  {'-'*35}  {'-'*7}  {'-'*8}  {'-'*7}  {'-'*8}")

    for field, label in [("bounce_reached_vwap", "Reached VWAP"),
                         ("bounce_reached_ema9", "Reached EMA9"),
                         ("rejection_bearish", "Rejection bearish")]:
        wt = sum(1 for a in winners if a[field])
        wf = sum(1 for a in winners if not a[field])
        lt = sum(1 for a in losers if a[field])
        lf = sum(1 for a in losers if not a[field])
        print(f"  {label:35s}  {wt:7d}  {wf:8d}  {lt:7d}  {lf:8d}")

    # ══════════════════════════════════════════════════════════
    # PHASE 2: FEATURE PASS/FAIL TESTS
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 90)
    print("PHASE 2: FEATURE PASS/FAIL TESTS")
    print("=" * 90)

    print(f"\n  {'Feature':25s}  {'Pass':>5s}  {'P_WR':>6s}  {'P_PF':>6s}  {'P_PnL':>8s}  "
          f"{'Fail':>5s}  {'F_WR':>6s}  {'F_PF':>6s}  {'F_PnL':>8s}  {'ΔWR':>7s}")
    print(f"  {'-'*25}  {'-'*5}  {'-'*6}  {'-'*6}  {'-'*8}  "
          f"{'-'*5}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*7}")

    feature_results = []
    for name, field, op, threshold, desc in FEATURE_TESTS:
        p, f = test_feature(all_anns, field, op, threshold)
        mp = compute_metrics_from_anns(p)
        mf = compute_metrics_from_anns(f)
        dwr = mp["wr"] - mf["wr"] if mf["n"] > 0 else 0.0

        print(f"  {name:25s}  {mp['n']:5d}  {mp['wr']:5.1f}%  {pf_str(mp['pf']):>6s}  {mp['pnl']:+8.2f}  "
              f"{mf['n']:5d}  {mf['wr']:5.1f}%  {pf_str(mf['pf']):>6s}  {mf['pnl']:+8.2f}  {dwr:+6.1f}%")

        feature_results.append({
            "name": name, "desc": desc,
            "pass_n": mp["n"], "pass_wr": mp["wr"], "pass_pf": mp["pf"], "pass_pnl": mp["pnl"],
            "fail_n": mf["n"], "fail_wr": mf["wr"], "fail_pf": mf["pf"], "fail_pnl": mf["pnl"],
            "delta_wr": dwr,
        })

    # ══════════════════════════════════════════════════════════
    # PHASE 3: TOP DISCRIMINATORS
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 90)
    print("PHASE 3: TOP DISCRIMINATORS (min 5 trades per subset)")
    print("=" * 90)

    valid = [r for r in feature_results if r["pass_n"] >= 5 and r["fail_n"] >= 5]
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
    # PHASE 4: WEAK-BOUNCE ARCHETYPE
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 90)
    print("PHASE 4: WEAK-BOUNCE ARCHETYPE SCORING")
    print("=" * 90)

    # Score every trade
    for a in all_anns:
        a["weak_score"] = score_weak_bounce(a)

    # Distribution
    score_dist = defaultdict(list)
    for a in all_anns:
        score_dist[a["weak_score"]].append(a)

    print(f"\n  {'Score':>5s}  {'N':>4s}  {'WR':>6s}  {'PF':>6s}  {'PnL':>8s}  {'Stop%':>6s}")
    print(f"  {'-'*5}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*6}")

    for score in sorted(score_dist.keys()):
        anns = score_dist[score]
        m = compute_metrics_from_anns(anns)
        print(f"  {score:5d}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  "
              f"{m['pnl']:+8.2f}  {m['stop_rate']:5.1f}%")

    # Try thresholds: "weak bounce" = score >= X
    print(f"\n  Weak-bounce thresholds:")
    print(f"  {'Threshold':>10s}  {'N':>4s}  {'WR':>6s}  {'PF':>6s}  {'PnL':>8s}  {'Stop%':>6s}  {'MaxDD':>7s}")
    print(f"  {'-'*10}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*6}  {'-'*7}")

    for thresh in range(3, 8):
        subset = [a for a in all_anns if a["weak_score"] >= thresh]
        if len(subset) >= 3:
            m = compute_metrics_from_anns(subset)
            print(f"  {'>=' + str(thresh):>10s}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  "
                  f"{m['pnl']:+8.2f}  {m['stop_rate']:5.1f}%  {m['max_dd']:7.2f}")

    # ══════════════════════════════════════════════════════════
    # PHASE 5: COMPOSITE FILTER — TOP 2 FEATURES COMBINED
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 90)
    print("PHASE 5: COMPOSITE FILTERS (top feature combinations)")
    print("=" * 90)

    # Test the top 3 individual features combined pairwise
    if len(valid) >= 3:
        top3 = valid[:3]
        from itertools import combinations
        for (r1, r2) in combinations(top3, 2):
            t1 = next((ft for ft in FEATURE_TESTS if ft[0] == r1["name"]), None)
            t2 = next((ft for ft in FEATURE_TESTS if ft[0] == r2["name"]), None)
            if t1 and t2:
                both_set = []
                rest = []
                for a in all_anns:
                    p1_pass = _eval(a, t1[1], t1[2], t1[3])
                    p2_pass = _eval(a, t2[1], t2[2], t2[3])
                    if p1_pass and p2_pass:
                        both_set.append(a)
                    else:
                        rest.append(a)
                mb = compute_metrics_from_anns(both_set)
                mr = compute_metrics_from_anns(rest)
                print(f"\n  {r1['name']} + {r2['name']}:")
                print_metrics(f"  Pass both", mb)
                print_metrics(f"  Fail either", mr)

    # ══════════════════════════════════════════════════════════
    # PHASE 6: VERDICT
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 90)
    print("PHASE 6: VERDICT")
    print("=" * 90)

    m = compute_metrics_from_anns(all_anns)
    print(f"\n  Overall FB: N={m['n']}  WR={m['wr']:.1f}%  PF={pf_str(m['pf'])}  PnL={m['pnl']:+.2f}")

    # Best archetype
    best_thresh = None
    best_pf = 0
    for thresh in range(3, 8):
        subset = [a for a in all_anns if a["weak_score"] >= thresh]
        if len(subset) >= 10:
            m_sub = compute_metrics_from_anns(subset)
            if m_sub["pf"] > best_pf:
                best_pf = m_sub["pf"]
                best_thresh = thresh

    if best_thresh is not None:
        subset = [a for a in all_anns if a["weak_score"] >= best_thresh]
        m_sub = compute_metrics_from_anns(subset)
        print(f"\n  Best weak-bounce archetype: score >= {best_thresh}")
        print(f"    N={m_sub['n']}  WR={m_sub['wr']:.1f}%  PF={pf_str(m_sub['pf'])}  PnL={m_sub['pnl']:+.2f}  "
              f"Stop={m_sub['stop_rate']:.1f}%  MaxDD={m_sub['max_dd']:.2f}")

    if valid:
        top = valid[0]
        print(f"\n  Top single discriminator: {top['name']} ({top['desc']})")
        print(f"    Pass: N={top['pass_n']}  WR={top['pass_wr']:.1f}%  PF={pf_str(top['pass_pf'])}")
        print(f"    Fail: N={top['fail_n']}  WR={top['fail_wr']:.1f}%  PF={pf_str(top['fail_pf'])}")
        print(f"    ΔWR = {top['delta_wr']:+.1f}%")

    # Final recommendation
    has_viable_subset = False
    if best_thresh is not None:
        subset = [a for a in all_anns if a["weak_score"] >= best_thresh]
        m_sub = compute_metrics_from_anns(subset)
        if m_sub["pf"] >= 1.0 and m_sub["n"] >= 10:
            has_viable_subset = True

    any_feature_viable = any(r["pass_pf"] >= 1.0 and r["pass_n"] >= 10 for r in feature_results)

    print(f"\n  RECOMMENDATION:")
    if has_viable_subset:
        print(f"    A weak-bounce archetype (score >= {best_thresh}) shows PF >= 1.0 on N >= 10.")
        print(f"    FB may be viable if restricted to this narrow archetype.")
        print(f"    Next step: implement as a quality gate and validate on train/test split.")
    elif any_feature_viable:
        viable = [r for r in feature_results if r["pass_pf"] >= 1.0 and r["pass_n"] >= 10]
        print(f"    {len(viable)} individual feature(s) produce PF >= 1.0 on N >= 10:")
        for r in viable[:3]:
            print(f"      {r['name']}: Pass N={r['pass_n']} PF={pf_str(r['pass_pf'])} WR={r['pass_wr']:.1f}%")
        print(f"    Worth testing as a single filter gate.")
    else:
        print(f"    No bounce-quality feature or archetype produces PF >= 1.0 on N >= 10.")
        print(f"    FB should be RETIRED. The setup does not have a viable subset.")
        print(f"    Short-side research should explore alternative structures.")


def _eval(ann, field, op, threshold):
    """Evaluate a single feature test on an annotation."""
    val = ann.get(field)
    if val is None:
        return False
    if op == "<=":
        return val <= threshold
    elif op == ">=":
        return val >= threshold
    elif op == ">":
        return val > threshold
    elif op == "<":
        return val < threshold
    elif op == "==":
        return val == threshold
    return False


if __name__ == "__main__":
    main()
