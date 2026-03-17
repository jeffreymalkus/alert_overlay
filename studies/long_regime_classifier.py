"""
Long Regime Classifier — Can we identify favorable long days by 10:00 AM?

Question: Using only information available at a fixed morning cutoff (10:00 AM),
can we separate days into favorable/neutral/hostile for long continuation scalps?

Design:
  FEATURES (all computed by 10:00 AM — 6 bars of data):
    F1. SPY pct from open at 10:00
    F2. SPY above VWAP at 10:00
    F3. SPY EMA9 > EMA20 at 10:00
    F4. QQQ pct from open at 10:00
    F5. QQQ above VWAP at 10:00
    F6. SPY–QQQ alignment (both above VWAP)
    F7. % of watchlist above VWAP at 10:00
    F8. % of watchlist green (close > open) at 10:00
    F9. % of watchlist breaking OR highs by 10:00
    F10. % of watchlist with RS > SPY at 10:00
    F11. Sector breadth: # of 11 sectors above VWAP at 10:00
    F12. Sector breadth: # of 11 sectors green at 10:00
    F13. Average RVOL of watchlist at bar 6
    F14. OR range of SPY (high-low of first 3 bars) as % of price

  LABEL (afternoon outcome — measured 10:00-15:55):
    Run VK acceptance longs on every symbol from 10:00-11:30 (same as prior study,
    but with NO market gate, NO in-play gate, NO leadership gate). Classify each
    day by the aggregate R of those trades:
      FAVORABLE: daily aggregate R > +0.5R per trade
      NEUTRAL:   daily aggregate R between -0.5R and +0.5R per trade
      HOSTILE:   daily aggregate R < -0.5R per trade

  ANALYSIS:
    1. Feature distributions by label group
    2. Correlation of each feature with daily long R
    3. Simple threshold rules and their classification accuracy
    4. Best 2-3 feature rule and its separation quality
    5. Whether any live rule materially improves deployment

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.long_regime_classifier
"""

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..backtest import load_bars_from_csv
from ..indicators import EMA, VWAPCalc
from ..market_context import MarketEngine, SECTOR_MAP, get_sector_etf
from ..models import Bar, NaN

DATA_DIR = Path(__file__).parent.parent / "data"
_isnan = math.isnan

CUTOFF_HHMM = 1000  # Feature snapshot time
CUTOFF_BAR_IDX = 6  # 9:30 + 6 bars × 5min = 10:00

# For EMA-based features, need more warmup — use bar 12 (10:00 + 6 more = enough for EMA9/20)
# But we compute VWAP and pct_from_open directly (no warmup needed) at bar 6.
# EMA features use a separate pass with cross-day warmup.

SECTOR_ETFS = sorted(set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})


# ════════════════════════════════════════════════════════════════
#  Data loading
# ════════════════════════════════════════════════════════════════

def load_bars(sym: str) -> list:
    p = DATA_DIR / f"{sym}_5min.csv"
    return load_bars_from_csv(str(p)) if p.exists() else []


def get_universe() -> list:
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    return sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])


def bars_by_day(bars: list) -> dict:
    daily = defaultdict(list)
    for b in bars:
        daily[b.timestamp.date()].append(b)
    return dict(daily)


# ════════════════════════════════════════════════════════════════
#  Feature computation (all by 10:00 AM)
# ════════════════════════════════════════════════════════════════

@dataclass
class MorningFeatures:
    date: date = None
    # SPY
    spy_pct: float = 0.0          # F1
    spy_above_vwap: bool = False  # F2
    spy_ema9_gt_ema20: bool = False  # F3
    # QQQ
    qqq_pct: float = 0.0         # F4
    qqq_above_vwap: bool = False  # F5
    # Alignment
    spy_qqq_aligned: bool = False  # F6
    # Watchlist breadth
    pct_above_vwap: float = 0.0   # F7
    pct_green: float = 0.0        # F8
    pct_or_high_break: float = 0.0 # F9
    pct_rs_positive: float = 0.0   # F10
    # Sector breadth
    sectors_above_vwap: int = 0    # F11
    sectors_green: int = 0         # F12
    # Volume / range
    avg_rvol: float = 0.0         # F13
    spy_or_range_pct: float = 0.0  # F14


def compute_etf_morning(bars_day: list) -> dict:
    """Compute VWAP, EMA, pct_from_open at bar CUTOFF_BAR_IDX.

    VWAP: computed directly (no warmup needed).
    EMA9/EMA20: from MarketEngine — ready flag may be False if <20 bars.
    above_vwap: always available (VWAP ready after 1 bar).
    ema9_gt_ema20: only if ready (needs 20 bars — won't be ready on day 1).
    """
    if not bars_day or len(bars_day) < CUTOFF_BAR_IDX:
        return None

    # VWAP computed directly (guaranteed ready)
    vwap_calc = VWAPCalc()
    for i, b in enumerate(bars_day):
        tp = (b.high + b.low + b.close) / 3.0
        vw = vwap_calc.update(tp, b.volume)
        if i + 1 >= CUTOFF_BAR_IDX:
            break

    cutoff_bar = bars_day[CUTOFF_BAR_IDX - 1]
    day_open = bars_day[0].open
    pct = (cutoff_bar.close - day_open) / day_open * 100 if day_open > 0 else 0

    above_vwap = cutoff_bar.close > vw if vwap_calc.ready else False

    return {
        "pct": pct,
        "vwap": vw,
        "close": cutoff_bar.close,
        "above_vwap": above_vwap,
        "green": cutoff_bar.close > day_open if day_open > 0 else False,
    }


def compute_etf_ema_state(all_bars: list, target_date: date) -> dict:
    """Compute EMA9/EMA20 state for an ETF at CUTOFF bar on target_date.

    Uses cross-day warmup: feeds ALL bars from start through cutoff of target_date.
    This gives EMAs enough history to be ready by day 2+.
    """
    ema9 = EMA(9)
    ema20 = EMA(20)
    e9 = e20 = NaN

    for b in all_bars:
        d = b.timestamp.date()
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute

        e9 = ema9.update(b.close)
        e20 = ema20.update(b.close)

        if d == target_date and hhmm >= CUTOFF_HHMM - 5:
            # We've reached the cutoff bar on target date
            break

    if not ema9.ready or not ema20.ready:
        return {"ema9_gt_ema20": False, "ema9_rising": False, "ready": False}

    return {
        "ema9_gt_ema20": e9 > e20,
        "ema9_rising": True,  # approximate — would need prev bar
        "ready": True,
    }


def compute_stock_morning(bars_day: list, spy_pct: float) -> dict:
    """Compute per-stock morning stats at cutoff."""
    if not bars_day or len(bars_day) < CUTOFF_BAR_IDX:
        return None

    vwap = VWAPCalc()
    vol_baseline_20 = []  # won't have 20 days in a single day, use raw vol

    day_open = bars_day[0].open
    or_high = max(b.high for b in bars_day[:3])  # first 3 bars = OR
    or_low = min(b.low for b in bars_day[:3])

    for i, b in enumerate(bars_day):
        tp = (b.high + b.low + b.close) / 3.0
        vw = vwap.update(tp, b.volume)
        if i + 1 >= CUTOFF_BAR_IDX:
            break

    cutoff_bar = bars_day[CUTOFF_BAR_IDX - 1]
    pct = (cutoff_bar.close - day_open) / day_open * 100 if day_open > 0 else 0
    vol_6bars = sum(b.volume for b in bars_day[:CUTOFF_BAR_IDX])

    return {
        "above_vwap": cutoff_bar.close > vw if vwap.ready else False,
        "green": cutoff_bar.close > day_open,
        "or_high_break": cutoff_bar.close > or_high,
        "or_low_break": cutoff_bar.close < or_low,
        "pct_from_open": pct,
        "rs_spy": pct - spy_pct,
        "vol_6bars": vol_6bars,
    }


def compute_morning_features(d: date, spy_daily: dict, qqq_daily: dict,
                             sector_daily: dict, stock_daily: dict,
                             stock_vol_baselines: dict,
                             symbols: list,
                             spy_all_bars: list = None,
                             qqq_all_bars: list = None) -> Optional[MorningFeatures]:
    """Compute all 14 morning features for one day."""
    spy_bars = spy_daily.get(d)
    qqq_bars = qqq_daily.get(d)
    if not spy_bars or len(spy_bars) < CUTOFF_BAR_IDX:
        return None
    if not qqq_bars or len(qqq_bars) < CUTOFF_BAR_IDX:
        return None

    f = MorningFeatures(date=d)

    # SPY — VWAP-based (always available)
    spy_m = compute_etf_morning(spy_bars)
    f.spy_pct = spy_m["pct"]
    f.spy_above_vwap = spy_m["above_vwap"]

    # SPY — EMA-based (cross-day warmup)
    if spy_all_bars:
        spy_ema = compute_etf_ema_state(spy_all_bars, d)
        f.spy_ema9_gt_ema20 = spy_ema.get("ema9_gt_ema20", False)
    else:
        f.spy_ema9_gt_ema20 = False

    # SPY OR range
    spy_or_high = max(b.high for b in spy_bars[:3])
    spy_or_low = min(b.low for b in spy_bars[:3])
    spy_price = spy_bars[0].open
    f.spy_or_range_pct = (spy_or_high - spy_or_low) / spy_price * 100 if spy_price > 0 else 0

    # QQQ — VWAP-based
    qqq_m = compute_etf_morning(qqq_bars)
    f.qqq_pct = qqq_m["pct"]
    f.qqq_above_vwap = qqq_m["above_vwap"]

    # Alignment
    f.spy_qqq_aligned = f.spy_above_vwap and f.qqq_above_vwap

    # Sector breadth — VWAP-based (always available)
    sec_above = 0
    sec_green = 0
    sec_total = 0
    for etf in SECTOR_ETFS:
        sec_bars = sector_daily.get(etf, {}).get(d)
        if not sec_bars or len(sec_bars) < CUTOFF_BAR_IDX:
            continue
        sec_total += 1
        sm = compute_etf_morning(sec_bars)
        if sm is None:
            continue
        if sm["above_vwap"]:
            sec_above += 1
        if sm["green"]:
            sec_green += 1
    f.sectors_above_vwap = sec_above
    f.sectors_green = sec_green

    # Watchlist breadth
    n_above_vwap = 0
    n_green = 0
    n_or_high = 0
    n_rs_pos = 0
    n_total = 0
    vol_ratios = []

    for sym in symbols:
        sym_bars = stock_daily.get(sym, {}).get(d)
        if not sym_bars or len(sym_bars) < CUTOFF_BAR_IDX:
            continue

        sm = compute_stock_morning(sym_bars, f.spy_pct)
        if sm is None:
            continue

        n_total += 1
        if sm["above_vwap"]:
            n_above_vwap += 1
        if sm["green"]:
            n_green += 1
        if sm["or_high_break"]:
            n_or_high += 1
        if sm["rs_spy"] > 0:
            n_rs_pos += 1

        # RVOL: this day's 6-bar vol vs 20-day avg
        baseline = stock_vol_baselines.get(sym, {}).get(d, 0)
        if baseline > 0 and sm["vol_6bars"] > 0:
            vol_ratios.append(sm["vol_6bars"] / baseline)

    if n_total > 0:
        f.pct_above_vwap = n_above_vwap / n_total * 100
        f.pct_green = n_green / n_total * 100
        f.pct_or_high_break = n_or_high / n_total * 100
        f.pct_rs_positive = n_rs_pos / n_total * 100

    f.avg_rvol = sum(vol_ratios) / len(vol_ratios) if vol_ratios else 0

    return f


# ════════════════════════════════════════════════════════════════
#  Label computation: afternoon long performance (10:00-11:30)
#  Runs VK acceptance with NO filters — pure signal quality
# ════════════════════════════════════════════════════════════════

@dataclass
class DayLabel:
    date: date
    n_trades: int = 0
    total_r: float = 0.0
    avg_r: float = 0.0
    label: str = "NEUTRAL"  # FAVORABLE / NEUTRAL / HOSTILE


def run_vk_day(bars_day: list, sym: str) -> list:
    """Run VK acceptance on one symbol for one day, return list of (pnl_rr, exit_reason)."""
    if len(bars_day) < 20:
        return []

    ema9 = EMA(9)
    vwap = VWAPCalc()
    vol_buf = deque(maxlen=20)
    vol_ma = NaN
    atr_buf = deque(maxlen=14)
    intra_atr = NaN

    touched = False
    hold_count = 0
    hold_low = NaN
    micro_high = NaN
    triggered = False

    results = []

    for i, bar in enumerate(bars_day):
        e9 = ema9.update(bar.close)
        tp = (bar.high + bar.low + bar.close) / 3.0
        vw = vwap.update(tp, bar.volume)

        if len(vol_buf) == 20:
            vol_ma = sum(vol_buf) / 20
        vol_buf.append(bar.volume)

        tr = bar.high - bar.low
        atr_buf.append(tr)
        if len(atr_buf) >= 5:
            intra_atr = sum(atr_buf) / len(atr_buf)

        if not vwap.ready or _isnan(intra_atr):
            continue

        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute
        if hhmm < 1000 or hhmm > 1130:
            continue
        if triggered:
            continue

        kiss_dist = 0.05 * intra_atr
        above_vwap = bar.close > vw
        near_vwap = abs(bar.low - vw) <= kiss_dist or bar.low <= vw <= bar.high

        if near_vwap and above_vwap:
            if not touched:
                touched = True
                hold_count = 1
                hold_low = bar.low
                micro_high = bar.high
            elif hold_count > 0 and hold_count < 2:
                hold_count += 1
                hold_low = min(hold_low, bar.low)
                micro_high = max(micro_high, bar.high)
        elif above_vwap and touched and hold_count > 0 and hold_count < 2:
            hold_count += 1
            hold_low = min(hold_low, bar.low)
            micro_high = max(micro_high, bar.high)
        elif not above_vwap:
            touched = False
            hold_count = 0
            hold_low = NaN
            micro_high = NaN

        if hold_count >= 2 and above_vwap and touched:
            rng = bar.high - bar.low
            body = abs(bar.close - bar.open)
            is_bull = bar.close > bar.open
            body_pct = body / rng if rng > 0 else 0
            trigger_ok = is_bull and body_pct >= 0.40
            vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 0.70 * vol_ma

            if trigger_ok and vol_ok:
                stop = (hold_low if not _isnan(hold_low) else bar.low) - 0.02
                stop = min(stop, vw - 0.02)
                risk = bar.close - stop
                if risk > 0:
                    target_rr = 3.0
                    # Simulate trade forward
                    pnl_rr = 0.0
                    exit_reason = "eod"
                    for j in range(i + 1, len(bars_day)):
                        b2 = bars_day[j]
                        h2 = b2.timestamp.hour * 100 + b2.timestamp.minute
                        if h2 >= 1555 or j == len(bars_day) - 1:
                            pnl_rr = (b2.close - bar.close) / risk
                            exit_reason = "eod"
                            break
                        if b2.low <= stop:
                            pnl_rr = (stop - bar.close) / risk
                            exit_reason = "stop"
                            break
                        target_price = bar.close + target_rr * risk
                        if b2.high >= target_price:
                            pnl_rr = target_rr
                            exit_reason = "target"
                            break

                    # Apply costs
                    slip = bar.close * 4 / 10000
                    comm = 0.005
                    cost_r = 2 * (slip + comm) / risk
                    pnl_rr -= cost_r

                    results.append((pnl_rr, exit_reason))
                    triggered = True
                    touched = False
                    hold_count = 0

    return results


def compute_day_labels(symbols: list, stock_daily: dict, all_dates: list) -> dict:
    """Run VK longs across all symbols, aggregate R per day, label."""
    day_trades = defaultdict(list)  # date -> list of pnl_rr

    for sym in symbols:
        sym_days = stock_daily.get(sym, {})
        for d in all_dates:
            bars_day = sym_days.get(d)
            if not bars_day:
                continue
            results = run_vk_day(bars_day, sym)
            for pnl_rr, _ in results:
                day_trades[d].append(pnl_rr)

    labels = {}
    for d in all_dates:
        trades = day_trades.get(d, [])
        n = len(trades)
        total = sum(trades)
        avg = total / n if n > 0 else 0

        if n == 0:
            label = "NO_TRADES"
        elif avg > 0.5:
            label = "FAVORABLE"
        elif avg < -0.5:
            label = "HOSTILE"
        else:
            label = "NEUTRAL"

        labels[d] = DayLabel(date=d, n_trades=n, total_r=total, avg_r=avg, label=label)

    return labels


# ════════════════════════════════════════════════════════════════
#  Analysis
# ════════════════════════════════════════════════════════════════

def analyze_features_vs_labels(features: dict, labels: dict):
    """Core analysis: which morning features separate long days?"""

    # Pair features with labels
    paired = []
    for d in sorted(features.keys()):
        if d in labels and labels[d].label != "NO_TRADES":
            paired.append((features[d], labels[d]))

    if not paired:
        print("  No paired data.")
        return

    # ── Section A: Label distribution ──
    print(f"\n{'='*120}")
    print("SECTION A — DAY LABEL DISTRIBUTION")
    print(f"{'='*120}")

    by_label = defaultdict(list)
    for f, l in paired:
        by_label[l.label].append((f, l))

    for lab in ["FAVORABLE", "NEUTRAL", "HOSTILE"]:
        items = by_label[lab]
        if items:
            avg_r = sum(l.avg_r for _, l in items) / len(items)
            avg_n = sum(l.n_trades for _, l in items) / len(items)
            total_r = sum(l.total_r for _, l in items)
            print(f"  {lab:12s}: {len(items):3d} days, avg trades/day={avg_n:.1f}, "
                  f"avg R/trade={avg_r:+.3f}, total R={total_r:+.1f}")

    # ── Section B: Feature means by label ──
    print(f"\n{'='*120}")
    print("SECTION B — FEATURE MEANS BY LABEL GROUP")
    print(f"{'='*120}")

    feature_names = [
        ("spy_pct", "F1: SPY pct from open"),
        ("spy_above_vwap", "F2: SPY above VWAP"),
        ("spy_ema9_gt_ema20", "F3: SPY EMA9>EMA20"),
        ("qqq_pct", "F4: QQQ pct from open"),
        ("qqq_above_vwap", "F5: QQQ above VWAP"),
        ("spy_qqq_aligned", "F6: SPY+QQQ aligned"),
        ("pct_above_vwap", "F7: % wl above VWAP"),
        ("pct_green", "F8: % wl green"),
        ("pct_or_high_break", "F9: % wl OR-high break"),
        ("pct_rs_positive", "F10: % wl RS>SPY"),
        ("sectors_above_vwap", "F11: sectors above VWAP"),
        ("sectors_green", "F12: sectors green"),
        ("avg_rvol", "F13: avg RVOL"),
        ("spy_or_range_pct", "F14: SPY OR range %"),
    ]

    print(f"\n  {'Feature':30s} {'FAVORABLE':>12s} {'NEUTRAL':>12s} {'HOSTILE':>12s} {'F-H delta':>10s} {'Corr w/R':>9s}")
    print(f"  {'-'*30} {'-'*12} {'-'*12} {'-'*12} {'-'*10} {'-'*9}")

    # Also compute correlation with daily avg R
    all_daily_r = [l.avg_r for _, l in paired]
    mean_r = sum(all_daily_r) / len(all_daily_r)

    for fname, flabel in feature_names:
        means = {}
        for lab in ["FAVORABLE", "NEUTRAL", "HOSTILE"]:
            items = by_label[lab]
            if items:
                vals = [float(getattr(f, fname)) for f, _ in items]
                means[lab] = sum(vals) / len(vals)
            else:
                means[lab] = 0

        fh_delta = means.get("FAVORABLE", 0) - means.get("HOSTILE", 0)

        # Pearson correlation with daily avg R
        all_fvals = [float(getattr(f, fname)) for f, _ in paired]
        mean_f = sum(all_fvals) / len(all_fvals)
        cov = sum((fv - mean_f) * (rv - mean_r) for fv, rv in zip(all_fvals, all_daily_r)) / len(paired)
        var_f = sum((fv - mean_f) ** 2 for fv in all_fvals) / len(paired)
        var_r = sum((rv - mean_r) ** 2 for rv in all_daily_r) / len(paired)
        corr = cov / (var_f ** 0.5 * var_r ** 0.5) if var_f > 0 and var_r > 0 else 0

        # Format
        def fmt(v, is_pct=False, is_bool=False, is_int=False):
            if is_bool:
                return f"{v*100:5.0f}%"
            if is_pct:
                return f"{v:5.1f}%"
            if is_int:
                return f"{v:5.1f}"
            return f"{v:+6.3f}"

        is_bool = fname in ("spy_above_vwap", "spy_ema9_gt_ema20", "qqq_above_vwap", "spy_qqq_aligned")
        is_pct = fname in ("pct_above_vwap", "pct_green", "pct_or_high_break", "pct_rs_positive")
        is_int = fname in ("sectors_above_vwap", "sectors_green")

        fav_s = fmt(means.get("FAVORABLE", 0), is_pct, is_bool, is_int)
        neu_s = fmt(means.get("NEUTRAL", 0), is_pct, is_bool, is_int)
        hos_s = fmt(means.get("HOSTILE", 0), is_pct, is_bool, is_int)

        print(f"  {flabel:30s} {fav_s:>12s} {neu_s:>12s} {hos_s:>12s} {fh_delta:+9.3f} {corr:+8.3f}")

    # ── Section C: Simple threshold rules ──
    print(f"\n{'='*120}")
    print("SECTION C — SIMPLE THRESHOLD RULES")
    print(f"{'='*120}")
    print("  Test: if feature > threshold, deploy longs. Measure PF and Exp of 'deploy' vs 'sit'.")

    rules = [
        ("SPY pct > 0", lambda f: f.spy_pct > 0),
        ("SPY pct > +0.1%", lambda f: f.spy_pct > 0.1),
        ("SPY pct > +0.2%", lambda f: f.spy_pct > 0.2),
        ("SPY above VWAP", lambda f: f.spy_above_vwap),
        ("SPY+QQQ aligned", lambda f: f.spy_qqq_aligned),
        ("SPY EMA9>EMA20", lambda f: f.spy_ema9_gt_ema20),
        (">50% wl above VWAP", lambda f: f.pct_above_vwap > 50),
        (">60% wl above VWAP", lambda f: f.pct_above_vwap > 60),
        (">50% wl green", lambda f: f.pct_green > 50),
        (">60% wl green", lambda f: f.pct_green > 60),
        (">6 sectors above VWAP", lambda f: f.sectors_above_vwap > 6),
        (">8 sectors above VWAP", lambda f: f.sectors_above_vwap > 8),
        (">6 sectors green", lambda f: f.sectors_green > 6),
        (">20% OR-high breaks", lambda f: f.pct_or_high_break > 20),
        (">30% OR-high breaks", lambda f: f.pct_or_high_break > 30),
        (">50% RS positive", lambda f: f.pct_rs_positive > 50),
    ]

    print(f"\n  {'Rule':30s} {'Deploy':>7s} {'Sit':>5s} {'DeplR':>8s} {'SitR':>8s} "
          f"{'DeplExp':>8s} {'SitExp':>8s} {'DeplPF':>7s} {'SitPF':>7s}")
    print(f"  {'-'*30} {'-'*7} {'-'*5} {'-'*8} {'-'*8} "
          f"{'-'*8} {'-'*8} {'-'*7} {'-'*7}")

    for rname, rfunc in rules:
        deploy_trades = []
        sit_trades = []
        deploy_days = 0
        sit_days = 0

        for f, l in paired:
            if rfunc(f):
                deploy_trades.extend([l.avg_r] * l.n_trades if l.n_trades > 0 else [])
                deploy_days += 1
            else:
                sit_trades.extend([l.avg_r] * l.n_trades if l.n_trades > 0 else [])
                sit_days += 1

        # Recompute from actual day aggregates for accuracy
        deploy_r = sum(l.total_r for f2, l in paired if rfunc(f2))
        sit_r = sum(l.total_r for f2, l in paired if not rfunc(f2))
        deploy_n = sum(l.n_trades for f2, l in paired if rfunc(f2))
        sit_n = sum(l.n_trades for f2, l in paired if not rfunc(f2))
        deploy_exp = deploy_r / deploy_n if deploy_n > 0 else 0
        sit_exp = sit_r / sit_n if sit_n > 0 else 0

        # PF from actual trade-level data (approximate from daily aggregates)
        deploy_win_r = sum(l.total_r for f2, l in paired if rfunc(f2) and l.total_r > 0)
        deploy_loss_r = abs(sum(l.total_r for f2, l in paired if rfunc(f2) and l.total_r <= 0))
        sit_win_r = sum(l.total_r for f2, l in paired if not rfunc(f2) and l.total_r > 0)
        sit_loss_r = abs(sum(l.total_r for f2, l in paired if not rfunc(f2) and l.total_r <= 0))

        dpf = deploy_win_r / deploy_loss_r if deploy_loss_r > 0 else float('inf')
        spf = sit_win_r / sit_loss_r if sit_loss_r > 0 else float('inf')

        dpf_s = f"{dpf:.2f}" if dpf < 99 else "inf"
        spf_s = f"{spf:.2f}" if spf < 99 else "inf"

        print(f"  {rname:30s} {deploy_days:4d}d  {sit_days:4d}d {deploy_r:+7.1f}R {sit_r:+7.1f}R "
              f"{deploy_exp:+7.3f}R {sit_exp:+7.3f}R {dpf_s:>7s} {spf_s:>7s}")

    # ── Section D: Best composite rules ──
    print(f"\n{'='*120}")
    print("SECTION D — COMPOSITE RULES (2-3 features)")
    print(f"{'='*120}")

    composites = [
        ("SPY>VWAP + >50% wl green",
         lambda f: f.spy_above_vwap and f.pct_green > 50),
        ("SPY>VWAP + >6 sectors green",
         lambda f: f.spy_above_vwap and f.sectors_green > 6),
        ("SPY+QQQ aligned + >50% wl green",
         lambda f: f.spy_qqq_aligned and f.pct_green > 50),
        ("SPY+QQQ aligned + >6 sec green",
         lambda f: f.spy_qqq_aligned and f.sectors_green > 6),
        ("SPY pct>0.1 + >50% green + >6 sec",
         lambda f: f.spy_pct > 0.1 and f.pct_green > 50 and f.sectors_green > 6),
        ("SPY>VWAP + >50% VWAP + >6 sec VWAP",
         lambda f: f.spy_above_vwap and f.pct_above_vwap > 50 and f.sectors_above_vwap > 6),
        (">60% wl green + >8 sec green",
         lambda f: f.pct_green > 60 and f.sectors_green > 8),
        ("SPY>VWAP + >20% OR breaks + >50% RS+",
         lambda f: f.spy_above_vwap and f.pct_or_high_break > 20 and f.pct_rs_positive > 50),
        ("Broad: SPY>VWAP + >50% green + >50% RS+",
         lambda f: f.spy_above_vwap and f.pct_green > 50 and f.pct_rs_positive > 50),
    ]

    print(f"\n  {'Rule':42s} {'Depl':>5s} {'Sit':>5s} {'DeplR':>8s} {'SitR':>8s} "
          f"{'DeplExp':>8s} {'SitExp':>8s} {'DeplPF':>7s}")
    print(f"  {'-'*42} {'-'*5} {'-'*5} {'-'*8} {'-'*8} "
          f"{'-'*8} {'-'*8} {'-'*7}")

    for rname, rfunc in composites:
        deploy_r = sum(l.total_r for f2, l in paired if rfunc(f2))
        sit_r = sum(l.total_r for f2, l in paired if not rfunc(f2))
        deploy_n = sum(l.n_trades for f2, l in paired if rfunc(f2))
        sit_n = sum(l.n_trades for f2, l in paired if not rfunc(f2))
        deploy_days = sum(1 for f2, _ in paired if rfunc(f2))
        sit_days = sum(1 for f2, _ in paired if not rfunc(f2))
        deploy_exp = deploy_r / deploy_n if deploy_n > 0 else 0
        sit_exp = sit_r / sit_n if sit_n > 0 else 0

        deploy_win = sum(l.total_r for f2, l in paired if rfunc(f2) and l.total_r > 0)
        deploy_loss = abs(sum(l.total_r for f2, l in paired if rfunc(f2) and l.total_r <= 0))
        dpf = deploy_win / deploy_loss if deploy_loss > 0 else float('inf')
        dpf_s = f"{dpf:.2f}" if dpf < 99 else "inf"

        print(f"  {rname:42s} {deploy_days:4d}d {sit_days:4d}d {deploy_r:+7.1f}R {sit_r:+7.1f}R "
              f"{deploy_exp:+7.3f}R {sit_exp:+7.3f}R {dpf_s:>7s}")

    # ── Section E: Day-by-day detail of best rule ──
    print(f"\n{'='*120}")
    print("SECTION E — DAY-BY-DAY DETAIL")
    print(f"{'='*120}")
    print("  Showing every day sorted by label, with key features and long R.")

    print(f"\n  {'Date':12s} {'Label':10s} {'#Tr':>4s} {'TotR':>7s} {'AvgR':>7s} "
          f"{'SPYpct':>7s} {'SPY>VW':>6s} {'QQQ>VW':>6s} "
          f"{'%Green':>6s} {'%VWAP':>6s} {'%ORhi':>6s} {'SecGr':>5s}")
    print(f"  {'-'*12} {'-'*10} {'-'*4} {'-'*7} {'-'*7} "
          f"{'-'*7} {'-'*6} {'-'*6} "
          f"{'-'*6} {'-'*6} {'-'*6} {'-'*5}")

    for f, l in sorted(paired, key=lambda x: ({"FAVORABLE": 0, "NEUTRAL": 1, "HOSTILE": 2}.get(x[1].label, 3), x[0].date)):
        spy_vw = "Y" if f.spy_above_vwap else "N"
        qqq_vw = "Y" if f.qqq_above_vwap else "N"
        print(f"  {str(f.date):12s} {l.label:10s} {l.n_trades:4d} {l.total_r:+6.1f}R {l.avg_r:+6.3f}R "
              f"{f.spy_pct:+6.2f}% {spy_vw:>6s} {qqq_vw:>6s} "
              f"{f.pct_green:5.1f}% {f.pct_above_vwap:5.1f}% {f.pct_or_high_break:5.1f}% {f.sectors_green:5d}")


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    print("=" * 120)
    print("LONG REGIME CLASSIFIER — Can we identify favorable long days by 10:00 AM?")
    print("=" * 120)

    symbols = get_universe()

    # Load SPY, QQQ
    print("\n  Loading market data...")
    spy_bars = load_bars("SPY")
    qqq_bars = load_bars("QQQ")
    spy_daily = bars_by_day(spy_bars)
    qqq_daily = bars_by_day(qqq_bars)
    all_dates = sorted(spy_daily.keys())

    # Load sector ETFs
    sector_daily = {}
    for etf in SECTOR_ETFS:
        sb = load_bars(etf)
        if sb:
            sector_daily[etf] = bars_by_day(sb)

    # Load all symbols
    print(f"  Loading {len(symbols)} symbols...")
    stock_daily = {}
    stock_vol_baselines = {}

    for sym in symbols:
        bars = load_bars(sym)
        if not bars:
            continue
        sd = bars_by_day(bars)
        stock_daily[sym] = sd

        # Build 20-day rolling vol baseline for first 6 bars
        vol_buf = deque(maxlen=20)
        baselines = {}
        for d in sorted(sd.keys()):
            day_bars = sd[d]
            vol_6 = sum(b.volume for b in day_bars[:min(CUTOFF_BAR_IDX, len(day_bars))])
            if len(vol_buf) >= 5:
                baselines[d] = sum(vol_buf) / len(vol_buf)
            vol_buf.append(vol_6)
        stock_vol_baselines[sym] = baselines

    print(f"  Data: {all_dates[0]} -> {all_dates[-1]}, {len(all_dates)} trading days")
    print(f"  Universe: {len(stock_daily)} symbols, {len(sector_daily)} sector ETFs")

    # ── Compute morning features ──
    print("\n  Computing morning features...")
    features = {}
    for d in all_dates:
        f = compute_morning_features(d, spy_daily, qqq_daily, sector_daily,
                                     stock_daily, stock_vol_baselines, symbols,
                                     spy_all_bars=spy_bars, qqq_all_bars=qqq_bars)
        if f:
            features[d] = f
    print(f"  Features computed for {len(features)}/{len(all_dates)} days")

    # ── Compute afternoon labels ──
    print("  Computing afternoon long labels (VK acceptance, no filters)...")
    labels = compute_day_labels(symbols, stock_daily, all_dates)
    n_fav = sum(1 for l in labels.values() if l.label == "FAVORABLE")
    n_neu = sum(1 for l in labels.values() if l.label == "NEUTRAL")
    n_hos = sum(1 for l in labels.values() if l.label == "HOSTILE")
    n_no = sum(1 for l in labels.values() if l.label == "NO_TRADES")
    total_trades = sum(l.n_trades for l in labels.values())
    total_r = sum(l.total_r for l in labels.values())
    print(f"  Labels: {n_fav} FAVORABLE, {n_neu} NEUTRAL, {n_hos} HOSTILE, {n_no} no-trades")
    print(f"  Total: {total_trades} trades, {total_r:+.1f}R aggregate")

    # ── Run analysis ──
    analyze_features_vs_labels(features, labels)

    print(f"\n{'='*120}")
    print("LONG REGIME CLASSIFIER COMPLETE")
    print(f"{'='*120}")


if __name__ == "__main__":
    main()
