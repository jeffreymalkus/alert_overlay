"""
Long Regime Classifier v2 — Multi-cutoff, hostile-day focus, breadth vs index.

Upgrades from v1:
  - Three cutoff times: 9:45, 10:00, 10:15
  - Hostile-day identification (can we detect "avoid" days?)
  - Breadth vs index separation comparison
  - Threshold sweeps for top features
  - Structured readout per user spec

Labels are constant across cutoffs (VK acceptance 10:00-11:30, no filters).
Only features shift with cutoff time.
"""

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .backtest import load_bars_from_csv
from .indicators import EMA, VWAPCalc
from .market_context import MarketEngine, SECTOR_MAP, get_sector_etf
from .models import Bar, NaN

DATA_DIR = Path(__file__).parent / "data"
_isnan = math.isnan

# Cutoff configs: (label, hhmm, bar_index)
CUTOFFS = [
    ("09:45", 945, 3),
    ("10:00", 1000, 6),
    ("10:15", 1015, 9),
]

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
#  Feature computation — parameterized by cutoff
# ════════════════════════════════════════════════════════════════

@dataclass
class MorningFeatures:
    date: date = None
    cutoff: str = ""
    # SPY
    spy_pct: float = 0.0
    spy_above_vwap: bool = False
    spy_ema9_gt_ema20: bool = False
    # QQQ
    qqq_pct: float = 0.0
    qqq_above_vwap: bool = False
    # Alignment
    spy_qqq_aligned: bool = False
    # Watchlist breadth
    pct_above_vwap: float = 0.0
    pct_green: float = 0.0
    pct_or_high_break: float = 0.0
    pct_rs_positive: float = 0.0
    # Sector breadth
    sectors_above_vwap: int = 0
    sectors_green: int = 0
    # Volume / range
    avg_rvol: float = 0.0
    spy_or_range_pct: float = 0.0


def compute_etf_at_cutoff(bars_day: list, bar_idx: int) -> Optional[dict]:
    """VWAP + pct from open at given bar index."""
    if not bars_day or len(bars_day) < bar_idx:
        return None

    vwap = VWAPCalc()
    for i, b in enumerate(bars_day):
        tp = (b.high + b.low + b.close) / 3.0
        vw = vwap.update(tp, b.volume)
        if i + 1 >= bar_idx:
            break

    cutoff_bar = bars_day[bar_idx - 1]
    day_open = bars_day[0].open
    pct = (cutoff_bar.close - day_open) / day_open * 100 if day_open > 0 else 0
    above_vwap = cutoff_bar.close > vw if vwap.ready else False

    return {
        "pct": pct,
        "vwap": vw,
        "close": cutoff_bar.close,
        "above_vwap": above_vwap,
        "green": cutoff_bar.close > day_open if day_open > 0 else False,
    }


def compute_ema_state(all_bars: list, target_date: date, cutoff_hhmm: int) -> dict:
    """EMA9/EMA20 with cross-day warmup, stopping at cutoff_hhmm on target_date."""
    ema9 = EMA(9)
    ema20 = EMA(20)
    e9 = e20 = NaN

    for b in all_bars:
        d = b.timestamp.date()
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute
        e9 = ema9.update(b.close)
        e20 = ema20.update(b.close)
        if d == target_date and hhmm >= cutoff_hhmm - 5:
            break

    if not ema9.ready or not ema20.ready:
        return {"ema9_gt_ema20": False, "ready": False}
    return {"ema9_gt_ema20": e9 > e20, "ready": True}


def compute_stock_at_cutoff(bars_day: list, bar_idx: int, spy_pct: float) -> Optional[dict]:
    """Per-stock morning stats at given bar index."""
    if not bars_day or len(bars_day) < bar_idx:
        return None

    vwap = VWAPCalc()
    day_open = bars_day[0].open
    or_high = max(b.high for b in bars_day[:min(3, len(bars_day))])

    for i, b in enumerate(bars_day):
        tp = (b.high + b.low + b.close) / 3.0
        vw = vwap.update(tp, b.volume)
        if i + 1 >= bar_idx:
            break

    cutoff_bar = bars_day[bar_idx - 1]
    pct = (cutoff_bar.close - day_open) / day_open * 100 if day_open > 0 else 0
    vol_nbars = sum(b.volume for b in bars_day[:bar_idx])

    return {
        "above_vwap": cutoff_bar.close > vw if vwap.ready else False,
        "green": cutoff_bar.close > day_open,
        "or_high_break": cutoff_bar.close > or_high,
        "pct_from_open": pct,
        "rs_spy": pct - spy_pct,
        "vol_nbars": vol_nbars,
    }


def compute_features_at_cutoff(d: date, bar_idx: int, cutoff_hhmm: int, cutoff_label: str,
                                spy_daily: dict, qqq_daily: dict,
                                sector_daily: dict, stock_daily: dict,
                                stock_vol_baselines: dict, symbols: list,
                                spy_all_bars: list, qqq_all_bars: list) -> Optional[MorningFeatures]:
    spy_bars = spy_daily.get(d)
    qqq_bars = qqq_daily.get(d)
    if not spy_bars or len(spy_bars) < bar_idx:
        return None
    if not qqq_bars or len(qqq_bars) < bar_idx:
        return None

    f = MorningFeatures(date=d, cutoff=cutoff_label)

    # SPY VWAP
    spy_m = compute_etf_at_cutoff(spy_bars, bar_idx)
    f.spy_pct = spy_m["pct"]
    f.spy_above_vwap = spy_m["above_vwap"]

    # SPY EMA
    spy_ema = compute_ema_state(spy_all_bars, d, cutoff_hhmm)
    f.spy_ema9_gt_ema20 = spy_ema.get("ema9_gt_ema20", False)

    # SPY OR range (always use first 3 bars)
    spy_or_high = max(b.high for b in spy_bars[:min(3, len(spy_bars))])
    spy_or_low = min(b.low for b in spy_bars[:min(3, len(spy_bars))])
    spy_price = spy_bars[0].open
    f.spy_or_range_pct = (spy_or_high - spy_or_low) / spy_price * 100 if spy_price > 0 else 0

    # QQQ VWAP
    qqq_m = compute_etf_at_cutoff(qqq_bars, bar_idx)
    f.qqq_pct = qqq_m["pct"]
    f.qqq_above_vwap = qqq_m["above_vwap"]

    # Alignment
    f.spy_qqq_aligned = f.spy_above_vwap and f.qqq_above_vwap

    # Sector breadth
    sec_above = sec_green = 0
    for etf in SECTOR_ETFS:
        sec_bars = sector_daily.get(etf, {}).get(d)
        if not sec_bars or len(sec_bars) < bar_idx:
            continue
        sm = compute_etf_at_cutoff(sec_bars, bar_idx)
        if sm is None:
            continue
        if sm["above_vwap"]:
            sec_above += 1
        if sm["green"]:
            sec_green += 1
    f.sectors_above_vwap = sec_above
    f.sectors_green = sec_green

    # Watchlist breadth
    n_above_vwap = n_green = n_or_high = n_rs_pos = n_total = 0
    vol_ratios = []

    for sym in symbols:
        sym_bars = stock_daily.get(sym, {}).get(d)
        if not sym_bars or len(sym_bars) < bar_idx:
            continue
        sm = compute_stock_at_cutoff(sym_bars, bar_idx, f.spy_pct)
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

        baseline = stock_vol_baselines.get(sym, {}).get(d, 0)
        if baseline > 0 and sm["vol_nbars"] > 0:
            vol_ratios.append(sm["vol_nbars"] / baseline)

    if n_total > 0:
        f.pct_above_vwap = n_above_vwap / n_total * 100
        f.pct_green = n_green / n_total * 100
        f.pct_or_high_break = n_or_high / n_total * 100
        f.pct_rs_positive = n_rs_pos / n_total * 100

    f.avg_rvol = sum(vol_ratios) / len(vol_ratios) if vol_ratios else 0
    return f


# ════════════════════════════════════════════════════════════════
#  Labels (constant across cutoffs)
# ════════════════════════════════════════════════════════════════

@dataclass
class DayLabel:
    date: date
    n_trades: int = 0
    total_r: float = 0.0
    avg_r: float = 0.0
    label: str = "NEUTRAL"
    trades: list = field(default_factory=list)  # individual R values


def run_vk_day(bars_day: list, sym: str) -> list:
    """VK acceptance longs, 10:00-11:30, no filters. Returns list of pnl_rr."""
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
                    pnl_rr = 0.0
                    for j in range(i + 1, len(bars_day)):
                        b2 = bars_day[j]
                        h2 = b2.timestamp.hour * 100 + b2.timestamp.minute
                        if h2 >= 1555 or j == len(bars_day) - 1:
                            pnl_rr = (b2.close - bar.close) / risk
                            break
                        if b2.low <= stop:
                            pnl_rr = (stop - bar.close) / risk
                            break
                        if b2.high >= bar.close + target_rr * risk:
                            pnl_rr = target_rr
                            break

                    slip = bar.close * 4 / 10000
                    comm = 0.005
                    cost_r = 2 * (slip + comm) / risk
                    pnl_rr -= cost_r
                    results.append(pnl_rr)
                    triggered = True
                    touched = False
                    hold_count = 0

    return results


def compute_day_labels(symbols: list, stock_daily: dict, all_dates: list) -> dict:
    day_trades = defaultdict(list)
    for sym in symbols:
        sym_days = stock_daily.get(sym, {})
        for d in all_dates:
            bars_day = sym_days.get(d)
            if not bars_day:
                continue
            results = run_vk_day(bars_day, sym)
            day_trades[d].extend(results)

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

        labels[d] = DayLabel(date=d, n_trades=n, total_r=total, avg_r=avg, label=label, trades=trades)

    return labels


# ════════════════════════════════════════════════════════════════
#  Analysis helpers
# ════════════════════════════════════════════════════════════════

def pf_from_trades(trades: list) -> float:
    """Profit factor from list of R values."""
    wins = sum(t for t in trades if t > 0)
    losses = abs(sum(t for t in trades if t <= 0))
    return wins / losses if losses > 0 else float('inf')


def rule_stats(paired, rfunc):
    """Compute deploy/sit stats for a rule function."""
    deploy_days = [(f, l) for f, l in paired if rfunc(f)]
    sit_days = [(f, l) for f, l in paired if not rfunc(f)]

    deploy_trades = []
    sit_trades = []
    for _, l in deploy_days:
        deploy_trades.extend(l.trades)
    for _, l in sit_days:
        sit_trades.extend(l.trades)

    d_n = len(deploy_days)
    s_n = len(sit_days)
    d_r = sum(t for t in deploy_trades)
    s_r = sum(t for t in sit_trades)
    d_nt = len(deploy_trades)
    s_nt = len(sit_trades)
    d_exp = d_r / d_nt if d_nt > 0 else 0
    s_exp = s_r / s_nt if s_nt > 0 else 0
    d_pf = pf_from_trades(deploy_trades)
    s_pf = pf_from_trades(sit_trades)

    # Hostile-day capture: how many hostile days does "sit" catch?
    hostile_deploy = sum(1 for f, l in deploy_days if l.label == "HOSTILE")
    hostile_sit = sum(1 for f, l in sit_days if l.label == "HOSTILE")
    hostile_total = hostile_deploy + hostile_sit
    hostile_catch = hostile_sit / hostile_total if hostile_total > 0 else 0

    # Favorable preservation: how many favorable days stay in deploy?
    fav_deploy = sum(1 for f, l in deploy_days if l.label == "FAVORABLE")
    fav_sit = sum(1 for f, l in sit_days if l.label == "FAVORABLE")
    fav_total = fav_deploy + fav_sit
    fav_keep = fav_deploy / fav_total if fav_total > 0 else 0

    return {
        "d_days": d_n, "s_days": s_n,
        "d_trades": d_nt, "s_trades": s_nt,
        "d_r": d_r, "s_r": s_r,
        "d_exp": d_exp, "s_exp": s_exp,
        "d_pf": d_pf, "s_pf": s_pf,
        "hostile_catch": hostile_catch,
        "fav_keep": fav_keep,
        "hostile_in_deploy": hostile_deploy,
        "hostile_in_sit": hostile_sit,
    }


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    vx = sum((x - mx) ** 2 for x in xs) / n
    vy = sum((y - my) ** 2 for y in ys) / n
    return cov / (vx ** 0.5 * vy ** 0.5) if vx > 0 and vy > 0 else 0


# ════════════════════════════════════════════════════════════════
#  Analysis per cutoff
# ════════════════════════════════════════════════════════════════

def analyze_cutoff(cutoff_label: str, features: dict, labels: dict):
    """Full analysis for one cutoff time."""
    paired = []
    for d in sorted(features.keys()):
        if d in labels and labels[d].label != "NO_TRADES":
            paired.append((features[d], labels[d]))

    if not paired:
        print(f"  No paired data for {cutoff_label}.")
        return {}

    by_label = defaultdict(list)
    for f, l in paired:
        by_label[l.label].append((f, l))

    # ── Feature means by label ──
    print(f"\n  {'Feature':30s} {'FAV':>8s} {'NEU':>8s} {'HOS':>8s} {'F-H':>7s} {'Corr':>6s}")
    print(f"  {'-'*30} {'-'*8} {'-'*8} {'-'*8} {'-'*7} {'-'*6}")

    all_daily_r = [l.avg_r for _, l in paired]
    feature_attrs = [
        ("spy_pct", "SPY pct"),
        ("spy_above_vwap", "SPY>VWAP"),
        ("spy_ema9_gt_ema20", "SPY EMA9>20"),
        ("qqq_pct", "QQQ pct"),
        ("qqq_above_vwap", "QQQ>VWAP"),
        ("spy_qqq_aligned", "SPY+QQQ aligned"),
        ("pct_above_vwap", "%wl>VWAP"),
        ("pct_green", "%wl green"),
        ("pct_or_high_break", "%OR-hi break"),
        ("pct_rs_positive", "%RS>SPY"),
        ("sectors_above_vwap", "sec>VWAP"),
        ("sectors_green", "sec green"),
        ("avg_rvol", "avg RVOL"),
        ("spy_or_range_pct", "SPY OR rng%"),
    ]

    corr_results = {}
    for fname, flabel in feature_attrs:
        vals_by_lab = {}
        for lab in ["FAVORABLE", "NEUTRAL", "HOSTILE"]:
            items = by_label[lab]
            if items:
                vals_by_lab[lab] = sum(float(getattr(f, fname)) for f, _ in items) / len(items)
            else:
                vals_by_lab[lab] = 0

        fh = vals_by_lab.get("FAVORABLE", 0) - vals_by_lab.get("HOSTILE", 0)
        all_fvals = [float(getattr(f, fname)) for f, _ in paired]
        corr = pearson(all_fvals, all_daily_r)
        corr_results[fname] = corr

        is_bool = fname in ("spy_above_vwap", "spy_ema9_gt_ema20", "qqq_above_vwap", "spy_qqq_aligned")
        def fmt(v):
            if is_bool:
                return f"{v*100:5.0f}%"
            return f"{v:+7.3f}" if abs(v) < 100 else f"{v:7.1f}"

        print(f"  {flabel:30s} {fmt(vals_by_lab.get('FAVORABLE',0)):>8s} "
              f"{fmt(vals_by_lab.get('NEUTRAL',0)):>8s} {fmt(vals_by_lab.get('HOSTILE',0)):>8s} "
              f"{fh:+6.2f} {corr:+5.3f}")

    # ── Threshold rules with hostile identification ──
    print(f"\n  THRESHOLD RULES — Deploy vs Sit")
    print(f"  {'Rule':32s} {'Dpl':>4s} {'Sit':>4s} {'DplPF':>6s} {'SitPF':>6s} "
          f"{'DplExp':>7s} {'SitExp':>7s} {'H-catch':>7s} {'F-keep':>7s} {'H-in-D':>6s}")
    print(f"  {'-'*32} {'-'*4} {'-'*4} {'-'*6} {'-'*6} "
          f"{'-'*7} {'-'*7} {'-'*7} {'-'*7} {'-'*6}")

    # Define rules — index-only, breadth-only, and mixed
    rules = [
        # INDEX-ONLY
        ("  [IDX] SPY>VWAP", lambda f: f.spy_above_vwap),
        ("  [IDX] QQQ>VWAP", lambda f: f.qqq_above_vwap),
        ("  [IDX] SPY+QQQ aligned", lambda f: f.spy_qqq_aligned),
        ("  [IDX] SPY pct>0", lambda f: f.spy_pct > 0),
        ("  [IDX] SPY pct>+0.1%", lambda f: f.spy_pct > 0.1),
        ("  [IDX] SPY EMA9>20", lambda f: f.spy_ema9_gt_ema20),
        # BREADTH-ONLY
        ("  [BRD] >40% wl>VWAP", lambda f: f.pct_above_vwap > 40),
        ("  [BRD] >50% wl>VWAP", lambda f: f.pct_above_vwap > 50),
        ("  [BRD] >60% wl>VWAP", lambda f: f.pct_above_vwap > 60),
        ("  [BRD] >40% wl green", lambda f: f.pct_green > 40),
        ("  [BRD] >50% wl green", lambda f: f.pct_green > 50),
        ("  [BRD] >60% wl green", lambda f: f.pct_green > 60),
        ("  [BRD] >5 sec>VWAP", lambda f: f.sectors_above_vwap > 5),
        ("  [BRD] >6 sec>VWAP", lambda f: f.sectors_above_vwap > 6),
        ("  [BRD] >7 sec>VWAP", lambda f: f.sectors_above_vwap > 7),
        ("  [BRD] >8 sec>VWAP", lambda f: f.sectors_above_vwap > 8),
        ("  [BRD] >5 sec green", lambda f: f.sectors_green > 5),
        ("  [BRD] >6 sec green", lambda f: f.sectors_green > 6),
        ("  [BRD] >7 sec green", lambda f: f.sectors_green > 7),
        ("  [BRD] >20% OR-hi break", lambda f: f.pct_or_high_break > 20),
        ("  [BRD] >50% RS>SPY", lambda f: f.pct_rs_positive > 50),
        # COMPOSITE (breadth + index)
        ("  [MIX] SPY>VW + >50%wl>VW", lambda f: f.spy_above_vwap and f.pct_above_vwap > 50),
        ("  [MIX] SPY>VW + >6sec>VW", lambda f: f.spy_above_vwap and f.sectors_above_vwap > 6),
        ("  [MIX] aligned + >50%green", lambda f: f.spy_qqq_aligned and f.pct_green > 50),
        ("  [MIX] aligned + >6sec>VW", lambda f: f.spy_qqq_aligned and f.sectors_above_vwap > 6),
        ("  [MIX] SPY>VW+>50%VW+>6secVW", lambda f: f.spy_above_vwap and f.pct_above_vwap > 50 and f.sectors_above_vwap > 6),
    ]

    rule_results = []
    for rname, rfunc in rules:
        s = rule_stats(paired, rfunc)
        dpf_s = f"{s['d_pf']:.2f}" if s['d_pf'] < 99 else "inf"
        spf_s = f"{s['s_pf']:.2f}" if s['s_pf'] < 99 else "inf"

        print(f"  {rname:32s} {s['d_days']:4d} {s['s_days']:4d} {dpf_s:>6s} {spf_s:>6s} "
              f"{s['d_exp']:+6.3f} {s['s_exp']:+6.3f} "
              f"{s['hostile_catch']*100:5.1f}% {s['fav_keep']*100:5.1f}% "
              f"{s['hostile_in_deploy']:5d}")
        rule_results.append((rname, s))

    # ── HOSTILE-DAY IDENTIFICATION FOCUS ──
    print(f"\n  HOSTILE-DAY IDENTIFICATION — Which rules best exclude bad days?")
    print(f"  Sorted by hostile_catch rate (higher = more hostile days excluded when rule says 'sit').")
    print(f"  A useful rule catches hostile days WITHOUT losing too many favorable days.")
    print(f"  {'Rule':32s} {'H-catch':>7s} {'F-keep':>7s} {'DplPF':>6s} {'D-days':>6s} {'D-trades':>8s}")
    print(f"  {'-'*32} {'-'*7} {'-'*7} {'-'*6} {'-'*6} {'-'*8}")

    # Sort by hostile catch, descending
    sorted_rules = sorted(rule_results, key=lambda x: x[1]['hostile_catch'], reverse=True)
    for rname, s in sorted_rules[:15]:
        dpf_s = f"{s['d_pf']:.2f}" if s['d_pf'] < 99 else "inf"
        print(f"  {rname:32s} {s['hostile_catch']*100:5.1f}% {s['fav_keep']*100:5.1f}% "
              f"{dpf_s:>6s} {s['d_days']:6d} {s['d_trades']:8d}")

    # ── BREADTH vs INDEX comparison ──
    print(f"\n  BREADTH vs INDEX COMPARISON")
    print(f"  Best index-only rule vs best breadth-only rule by deploy PF:")

    best_idx = max((s for rn, s in rule_results if "[IDX]" in rn), key=lambda s: s['d_pf'], default=None)
    best_brd = max((s for rn, s in rule_results if "[BRD]" in rn), key=lambda s: s['d_pf'], default=None)
    best_mix = max((s for rn, s in rule_results if "[MIX]" in rn), key=lambda s: s['d_pf'], default=None)

    best_idx_name = next((rn for rn, s in rule_results if "[IDX]" in rn and s is best_idx), "")
    best_brd_name = next((rn for rn, s in rule_results if "[BRD]" in rn and s is best_brd), "")
    best_mix_name = next((rn for rn, s in rule_results if "[MIX]" in rn and s is best_mix), "")

    for tag, name, s in [("INDEX", best_idx_name, best_idx),
                         ("BREADTH", best_brd_name, best_brd),
                         ("MIXED", best_mix_name, best_mix)]:
        if s:
            dpf_s = f"{s['d_pf']:.2f}" if s['d_pf'] < 99 else "inf"
            print(f"    {tag:8s}: {name.strip():30s} DplPF={dpf_s:>5s} DplExp={s['d_exp']:+.3f} "
                  f"Days={s['d_days']:2d} H-catch={s['hostile_catch']*100:.0f}%")

    return {"corr": corr_results, "rule_results": rule_results, "paired": paired}


# ════════════════════════════════════════════════════════════════
#  Threshold sweep for top features
# ════════════════════════════════════════════════════════════════

def threshold_sweep(cutoff_label: str, features: dict, labels: dict):
    """Sweep thresholds for key continuous features to find step-function effects."""
    paired = []
    for d in sorted(features.keys()):
        if d in labels and labels[d].label != "NO_TRADES":
            paired.append((features[d], labels[d]))

    if not paired:
        return

    print(f"\n  THRESHOLD SWEEP — Key continuous features")
    print(f"  Looking for step-function effects in deploy PF / hostile catch.")

    sweeps = [
        ("sectors_above_vwap", "sec>VWAP", range(3, 10)),
        ("sectors_green", "sec green", range(3, 10)),
        ("pct_above_vwap", "%wl>VWAP", [30, 35, 40, 45, 50, 55, 60, 65, 70]),
        ("pct_green", "%wl green", [30, 35, 40, 45, 50, 55, 60, 65, 70]),
        ("pct_or_high_break", "%OR-hi", [5, 10, 15, 20, 25, 30]),
    ]

    for fname, flabel, thresholds in sweeps:
        print(f"\n  {flabel}:")
        print(f"    {'Thresh':>7s} {'Dpl':>4s} {'Sit':>4s} {'DplPF':>6s} {'DplExp':>7s} {'H-catch':>7s} {'F-keep':>7s}")
        for thresh in thresholds:
            rfunc = lambda f, t=thresh, fn=fname: getattr(f, fn) > t
            s = rule_stats(paired, rfunc)
            dpf_s = f"{s['d_pf']:.2f}" if s['d_pf'] < 99 else "inf"
            print(f"    >{thresh:5} {s['d_days']:4d} {s['s_days']:4d} {dpf_s:>6s} "
                  f"{s['d_exp']:+6.3f} {s['hostile_catch']*100:5.1f}% {s['fav_keep']*100:5.1f}%")


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    W = 120
    print("=" * W)
    print("LONG REGIME CLASSIFIER v2 — Multi-cutoff, hostile-day focus")
    print("=" * W)

    symbols = get_universe()

    # Load SPY, QQQ
    print("\n  Loading market data...")
    spy_bars = load_bars("SPY")
    qqq_bars = load_bars("QQQ")
    spy_daily = bars_by_day(spy_bars)
    qqq_daily = bars_by_day(qqq_bars)
    all_dates = sorted(spy_daily.keys())

    # Load sectors
    sector_daily = {}
    for etf in SECTOR_ETFS:
        sb = load_bars(etf)
        if sb:
            sector_daily[etf] = bars_by_day(sb)

    # Load stocks
    print(f"  Loading {len(symbols)} symbols...")
    stock_daily = {}
    stock_vol_baselines = {}
    for sym in symbols:
        bars = load_bars(sym)
        if not bars:
            continue
        sd = bars_by_day(bars)
        stock_daily[sym] = sd

        vol_buf = deque(maxlen=20)
        baselines = {}
        for d in sorted(sd.keys()):
            day_bars = sd[d]
            vol_6 = sum(b.volume for b in day_bars[:min(6, len(day_bars))])
            if len(vol_buf) >= 5:
                baselines[d] = sum(vol_buf) / len(vol_buf)
            vol_buf.append(vol_6)
        stock_vol_baselines[sym] = baselines

    print(f"  Data: {all_dates[0]} -> {all_dates[-1]}, {len(all_dates)} trading days")
    print(f"  Universe: {len(stock_daily)} symbols, {len(sector_daily)} sector ETFs")

    # ── SECTION 1: Day label definition ──
    print(f"\n{'='*W}")
    print("SECTION 1 — DAY LABEL DEFINITION")
    print(f"{'='*W}")
    print("  Label method: VK acceptance longs 10:00-11:30, ALL symbols, NO gates")
    print("  FAVORABLE: avg R/trade > +0.5R")
    print("  NEUTRAL:   avg R/trade between -0.5R and +0.5R")
    print("  HOSTILE:   avg R/trade < -0.5R")
    print("  Labels are CONSTANT across all cutoff times.")

    print("\n  Computing labels...")
    labels = compute_day_labels(symbols, stock_daily, all_dates)
    n_fav = sum(1 for l in labels.values() if l.label == "FAVORABLE")
    n_neu = sum(1 for l in labels.values() if l.label == "NEUTRAL")
    n_hos = sum(1 for l in labels.values() if l.label == "HOSTILE")
    total_trades = sum(l.n_trades for l in labels.values())
    total_r = sum(l.total_r for l in labels.values())
    print(f"  {n_fav} FAVORABLE, {n_neu} NEUTRAL, {n_hos} HOSTILE")
    print(f"  {total_trades} trades, {total_r:+.1f}R aggregate")

    for lab in ["FAVORABLE", "NEUTRAL", "HOSTILE"]:
        items = [l for l in labels.values() if l.label == lab]
        if items:
            avg_trades = sum(l.n_trades for l in items) / len(items)
            avg_r = sum(l.avg_r for l in items) / len(items)
            tot_r = sum(l.total_r for l in items)
            print(f"    {lab:12s}: {len(items):3d} days, {avg_trades:.0f} trades/day, "
                  f"avg {avg_r:+.3f}R/trade, total {tot_r:+.1f}R")

    # ── Compute features at each cutoff ──
    all_cutoff_features = {}
    for clabel, chhmm, cbar in CUTOFFS:
        print(f"\n  Computing features at {clabel} (bar {cbar})...")
        feats = {}
        for d in all_dates:
            f = compute_features_at_cutoff(
                d, cbar, chhmm, clabel,
                spy_daily, qqq_daily, sector_daily, stock_daily,
                stock_vol_baselines, symbols,
                spy_bars, qqq_bars
            )
            if f:
                feats[d] = f
        all_cutoff_features[clabel] = feats
        print(f"  {clabel}: {len(feats)}/{len(all_dates)} days")

    # ── SECTION 2: Per-cutoff analysis ──
    cutoff_results = {}
    for clabel, _, _ in CUTOFFS:
        print(f"\n{'='*W}")
        print(f"SECTION 2 — CUTOFF {clabel}")
        print(f"{'='*W}")
        feats = all_cutoff_features[clabel]
        result = analyze_cutoff(clabel, feats, labels)
        cutoff_results[clabel] = result

    # ── SECTION 3: Threshold sweeps (10:00 and 10:15 only) ──
    for clabel in ["10:00", "10:15"]:
        print(f"\n{'='*W}")
        print(f"SECTION 3 — THRESHOLD SWEEP at {clabel}")
        print(f"{'='*W}")
        threshold_sweep(clabel, all_cutoff_features[clabel], labels)

    # ── SECTION 4: Cross-cutoff comparison ──
    print(f"\n{'='*W}")
    print("SECTION 4 — CROSS-CUTOFF COMPARISON")
    print(f"{'='*W}")
    print("  Does separation improve from 9:45 → 10:00 → 10:15?")
    print(f"\n  Key rules compared across cutoffs:")
    print(f"  {'Rule':32s}  {'9:45 DplPF':>10s} {'10:00 DplPF':>11s} {'10:15 DplPF':>11s}")
    print(f"  {'-'*32}  {'-'*10} {'-'*11} {'-'*11}")

    # Pick key rules to compare
    compare_rules = [
        ("SPY>VWAP", lambda f: f.spy_above_vwap),
        ("SPY+QQQ aligned", lambda f: f.spy_qqq_aligned),
        (">50% wl>VWAP", lambda f: f.pct_above_vwap > 50),
        (">6 sec>VWAP", lambda f: f.sectors_above_vwap > 6),
        (">8 sec>VWAP", lambda f: f.sectors_above_vwap > 8),
        (">50% wl green", lambda f: f.pct_green > 50),
        (">6 sec green", lambda f: f.sectors_green > 6),
        ("SPY>VW+>50%VW+>6secVW", lambda f: f.spy_above_vwap and f.pct_above_vwap > 50 and f.sectors_above_vwap > 6),
    ]

    for rname, rfunc in compare_rules:
        pfs = []
        for clabel in ["09:45", "10:00", "10:15"]:
            feats = all_cutoff_features[clabel]
            paired = []
            for d in sorted(feats.keys()):
                if d in labels and labels[d].label != "NO_TRADES":
                    paired.append((feats[d], labels[d]))
            if paired:
                s = rule_stats(paired, rfunc)
                pf = s['d_pf']
                pf_s = f"{pf:.2f}" if pf < 99 else "inf"
                pfs.append(f"{pf_s:>6s} ({s['d_days']:2d}d)")
            else:
                pfs.append("  N/A")
        print(f"  {rname:32s}  {pfs[0]:>10s} {pfs[1]:>11s} {pfs[2]:>11s}")

    # ── SECTION 5: Day-by-day detail (10:00) ──
    print(f"\n{'='*W}")
    print("SECTION 5 — DAY-BY-DAY DETAIL (10:00 cutoff)")
    print(f"{'='*W}")

    feats_1000 = all_cutoff_features["10:00"]
    paired_1000 = [(feats_1000[d], labels[d]) for d in sorted(feats_1000.keys())
                   if d in labels and labels[d].label != "NO_TRADES"]

    print(f"\n  {'Date':12s} {'Label':10s} {'#Tr':>4s} {'TotR':>7s} {'AvgR':>7s} "
          f"{'SPY>V':>5s} {'QQQ>V':>5s} {'%VW':>5s} {'%Grn':>5s} {'%ORhi':>5s} "
          f"{'secVW':>5s} {'secGr':>5s}")
    print(f"  {'-'*12} {'-'*10} {'-'*4} {'-'*7} {'-'*7} "
          f"{'-'*5} {'-'*5} {'-'*5} {'-'*5} {'-'*5} "
          f"{'-'*5} {'-'*5}")

    for f, l in sorted(paired_1000, key=lambda x: (
            {"FAVORABLE": 0, "NEUTRAL": 1, "HOSTILE": 2}.get(x[1].label, 3), x[0].date)):
        print(f"  {str(f.date):12s} {l.label:10s} {l.n_trades:4d} {l.total_r:+6.1f}R {l.avg_r:+6.3f}R "
              f"{'Y' if f.spy_above_vwap else 'N':>5s} "
              f"{'Y' if f.qqq_above_vwap else 'N':>5s} "
              f"{f.pct_above_vwap:4.0f}% {f.pct_green:4.0f}% {f.pct_or_high_break:4.0f}% "
              f"{f.sectors_above_vwap:5d} {f.sectors_green:5d}")

    print(f"\n{'='*W}")
    print("REGIME CLASSIFIER v2 COMPLETE")
    print(f"{'='*W}")


if __name__ == "__main__":
    main()
