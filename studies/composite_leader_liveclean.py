"""
Composite Leader Variants — LIVE-CLEAN Replay

Rebuilds the best composite variants WITHOUT hindsight GREEN-day logic.
Market support uses ONLY live-available SPY conditions:
  ML2: SPY above VWAP at time of entry
  ML3: SPY above VWAP + EMA9 > EMA20 at time of entry

Key variants to test:
  - VK_leader (ML2/ML3 + L1)
  - EMA9_leader (ML2/ML3 + L1)
  - VK_inplay_leader (ML2/ML3 + P3 + L1)
  - EMA9_inplay_leader (ML2/ML3 + P3 + L1)

Then: standalone metrics, combined with FLR, and capped portfolio.

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.studies.composite_leader_liveclean
"""

import csv
import math
import statistics
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
OUT_DIR = Path(__file__).parent.parent / "outputs"

_isnan = math.isnan

SLIPPAGE_BPS = 4
COMMISSION_PER_SHARE = 0.005


# ════════════════════════════════════════════════════════════════
#  Trade model
# ════════════════════════════════════════════════════════════════

@dataclass
class CTrade:
    symbol: str
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    direction: int  # 1=long
    setup: str
    risk: float = 0.0
    pnl_r: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0

    @property
    def entry_date(self) -> date:
        return self.entry_time.date()


# ════════════════════════════════════════════════════════════════
#  Shared infrastructure
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


# ════════════════════════════════════════════════════════════════
#  Market context (Layer M) — LIVE CLEAN, no GREEN hindsight
# ════════════════════════════════════════════════════════════════

def build_spy_snapshots(spy_bars: list) -> dict:
    me = MarketEngine()
    snapshots = {}
    for b in spy_bars:
        snap = me.process_bar(b)
        d = b.timestamp.date()
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute
        snapshots[(d, hhmm)] = snap
    return snapshots


def check_market_live(spy_ctx: dict, d: date, hhmm: int, m_level: str) -> bool:
    """Live-clean market check. NO GREEN-day hindsight."""
    if m_level == "-":
        return True
    snap = spy_ctx.get((d, hhmm))
    if snap is None or not snap.ready:
        return False  # no data = reject (conservative for live-clean)
    if m_level == "ML2":
        return snap.above_vwap
    if m_level == "ML3":
        return snap.above_vwap and snap.ema9_above_ema20
    return True


# ════════════════════════════════════════════════════════════════
#  In-play proxy (Layer P) — same as original
# ════════════════════════════════════════════════════════════════

def compute_open_stats(bars: list) -> dict:
    daily = defaultdict(list)
    for b in bars:
        daily[b.timestamp.date()].append(b)
    dates_sorted = sorted(daily.keys())
    stats = {}
    vol_baseline_buf = deque(maxlen=20)
    prev_close = None
    for d in dates_sorted:
        day_bars = daily[d]
        o = day_bars[0].open
        gap = abs((o - prev_close) / prev_close * 100) if prev_close and prev_close > 0 else 0.0
        first3_vol = sum(b.volume for b in day_bars[:3]) if len(day_bars) >= 3 else 0
        avg_baseline = statistics.mean(vol_baseline_buf) if vol_baseline_buf else first3_vol
        rvol = first3_vol / avg_baseline if avg_baseline > 0 else 1.0
        vol_baseline_buf.append(first3_vol)
        stats[d] = {"gap_pct": gap, "rvol": rvol}
        prev_close = day_bars[-1].close
    return stats


def check_inplay(open_stats: dict, d: date, p_level: str) -> bool:
    if p_level == "-":
        return True
    s = open_stats.get(d)
    if s is None:
        return False
    if p_level == "P1":
        return s["gap_pct"] >= 1.0
    if p_level == "P2":
        return s["rvol"] >= 2.0
    if p_level == "P3":
        return s["gap_pct"] >= 1.0 and s["rvol"] >= 2.0
    return True


# ════════════════════════════════════════════════════════════════
#  Leadership (Layer L) — same as original
# ════════════════════════════════════════════════════════════════

def build_pct_from_open(bars: list) -> dict:
    pfo: Dict[Tuple[date, int], float] = {}
    day_open = None
    current_d = None
    for b in bars:
        d = b.timestamp.date()
        if d != current_d:
            day_open = b.open
            current_d = d
        if day_open and day_open > 0:
            pfo[(d, b.timestamp.hour * 100 + b.timestamp.minute)] = (b.close - day_open) / day_open * 100
    return pfo


def check_leadership(stock_pfo: dict, spy_pfo: dict, sector_pfo: dict,
                      d: date, hhmm: int, l_level: str) -> bool:
    if l_level == "-":
        return True
    stock_rs = stock_pfo.get((d, hhmm), 0)
    spy_rs = spy_pfo.get((d, hhmm), 0)
    rs_vs_spy = stock_rs - spy_rs
    if l_level == "L1":
        return rs_vs_spy > 0
    if l_level == "L4":
        sec_rs = sector_pfo.get((d, hhmm), 0)
        rs_vs_sec = stock_rs - sec_rs
        return rs_vs_spy > 0 and rs_vs_sec > 0
    return True


# ════════════════════════════════════════════════════════════════
#  Entry + exit logic: VK acceptance (E1)
# ════════════════════════════════════════════════════════════════

def entry_vk(bars: list, sym: str, spy_ctx: dict,
             open_stats: dict, stock_pfo: dict, spy_pfo: dict, sector_pfo: dict,
             m_level: str, p_level: str, l_level: str,
             x_type: str = "X2",
             time_start: int = 1000, time_end: int = 1130,
             hold_bars: int = 2, min_body_pct: float = 0.40,
             target_rr: float = 3.0, max_hold: int = 78) -> List[CTrade]:
    trades: List[CTrade] = []
    vwap_calc = VWAPCalc()
    below_vwap = False
    hold_count = 0
    hold_low = float("inf")
    fired_today = None
    current_day = None

    for i, bar in enumerate(bars):
        d = bar.timestamp.date()
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute
        if d != current_day:
            vwap_calc.reset()
            current_day = d
            below_vwap = False
            hold_count = 0
            hold_low = float("inf")
            if d != fired_today:
                fired_today = None
        tp = (bar.high + bar.low + bar.close) / 3
        vwap_calc.update(tp, bar.volume)
        if hhmm < 935:
            continue
        vw = vwap_calc.value
        if _isnan(vw):
            continue
        if d != fired_today:
            if bar.close < vw:
                below_vwap = True
                hold_count = 0
                hold_low = float("inf")
            elif below_vwap and bar.close > vw:
                hold_count += 1
                hold_low = min(hold_low, bar.low)
                if hold_count >= hold_bars:
                    body = abs(bar.close - bar.open)
                    rng = bar.high - bar.low
                    body_ok = (body / rng >= min_body_pct) if rng > 0 else False
                    if body_ok and hhmm >= time_start and hhmm <= time_end:
                        if not check_market_live(spy_ctx, d, hhmm, m_level):
                            continue
                        if not check_inplay(open_stats, d, p_level):
                            continue
                        if not check_leadership(stock_pfo, spy_pfo, sector_pfo, d, hhmm, l_level):
                            continue
                        stop = hold_low
                        risk = bar.close - stop
                        if risk > 0:
                            target = bar.close + target_rr * risk
                            t = CTrade(sym, bar.timestamp, bar.close, stop, target, 1,
                                       f"VK_{m_level}_{p_level}_{l_level}", risk)
                            t = simulate_trade(t, bars, i, max_hold, target_rr)
                            trades.append(t)
                            fired_today = d
                            below_vwap = False
                            hold_count = 0
            elif bar.close < vw:
                below_vwap = True
                hold_count = 0
                hold_low = float("inf")
    return trades


# ════════════════════════════════════════════════════════════════
#  Entry + exit logic: EMA9 acceptance (E2)
# ════════════════════════════════════════════════════════════════

def entry_ema9(bars: list, sym: str, spy_ctx: dict,
               open_stats: dict, stock_pfo: dict, spy_pfo: dict, sector_pfo: dict,
               m_level: str, p_level: str, l_level: str,
               x_type: str = "X2",
               time_start: int = 1000, time_end: int = 1130,
               hold_bars: int = 2, min_body_pct: float = 0.40,
               target_rr: float = 3.0, max_hold: int = 78) -> List[CTrade]:
    trades: List[CTrade] = []
    ema = EMA(9)
    was_below = False
    hold_count = 0
    hold_low = float("inf")
    fired_today = None

    for i, bar in enumerate(bars):
        d = bar.timestamp.date()
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute
        if hhmm < 935:
            ema.update(bar.close)
            was_below = False
            hold_count = 0
            fired_today = None
            continue
        ema.update(bar.close)
        e9 = ema.value
        if _isnan(e9):
            continue
        if d != fired_today:
            if bar.close <= e9:
                was_below = True
                hold_count = 0
                hold_low = float("inf")
            elif was_below and bar.close > e9:
                hold_count += 1
                hold_low = min(hold_low, bar.low)
                if hold_count >= hold_bars:
                    body = abs(bar.close - bar.open)
                    rng = bar.high - bar.low
                    body_ok = (body / rng >= min_body_pct) if rng > 0 else False
                    if body_ok and hhmm >= time_start and hhmm <= time_end:
                        if not check_market_live(spy_ctx, d, hhmm, m_level):
                            # Track state even when market blocked
                            if bar.close <= e9:
                                was_below = True
                                hold_count = 0
                            continue
                        if not check_inplay(open_stats, d, p_level):
                            continue
                        if not check_leadership(stock_pfo, spy_pfo, sector_pfo, d, hhmm, l_level):
                            continue
                        stop = hold_low
                        risk = bar.close - stop
                        if risk > 0:
                            target = bar.close + target_rr * risk
                            t = CTrade(sym, bar.timestamp, bar.close, stop, target, 1,
                                       f"EMA9_{m_level}_{p_level}_{l_level}", risk)
                            t = simulate_trade(t, bars, i, max_hold, target_rr)
                            trades.append(t)
                            fired_today = d
                            was_below = False
                            hold_count = 0
            elif bar.close <= e9:
                was_below = True
                hold_count = 0
                hold_low = float("inf")
    return trades


# ════════════════════════════════════════════════════════════════
#  Trade simulation (stop/target/EOD)
# ════════════════════════════════════════════════════════════════

def simulate_trade(trade: CTrade, bars: list, bar_idx: int,
                   max_bars: int = 78, target_rr: float = 3.0) -> CTrade:
    risk = trade.risk
    if risk <= 0:
        trade.pnl_r = 0
        trade.exit_reason = "invalid"
        return trade

    slip = trade.entry_price * SLIPPAGE_BPS / 10000
    entry_cost = slip + COMMISSION_PER_SHARE

    for i in range(bar_idx + 1, min(bar_idx + max_bars + 1, len(bars))):
        b = bars[i]
        trade.bars_held += 1
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute

        if hhmm >= 1555 or (i == len(bars) - 1):
            pnl = (b.close - trade.entry_price) * trade.direction
            pnl -= (entry_cost + slip + COMMISSION_PER_SHARE)
            trade.pnl_r = pnl / risk
            trade.exit_reason = "eod"
            return trade

        if trade.direction == 1:
            if b.low <= trade.stop_price:
                pnl = (trade.stop_price - trade.entry_price)
                pnl -= (entry_cost + slip + COMMISSION_PER_SHARE)
                trade.pnl_r = pnl / risk
                trade.exit_reason = "stop"
                return trade
            if b.high >= trade.target_price:
                pnl = (trade.target_price - trade.entry_price)
                pnl -= (entry_cost + slip + COMMISSION_PER_SHARE)
                trade.pnl_r = pnl / risk
                trade.exit_reason = "target"
                return trade

    pnl = (bars[min(bar_idx + max_bars, len(bars) - 1)].close - trade.entry_price)
    pnl -= (entry_cost + slip + COMMISSION_PER_SHARE)
    trade.pnl_r = pnl / risk
    trade.exit_reason = "time"
    return trade


# ════════════════════════════════════════════════════════════════
#  Metrics
# ════════════════════════════════════════════════════════════════

def compute_metrics(trades: List[CTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {k: 0 for k in [
            "n", "wr", "pf", "exp", "total_r", "max_dd",
            "train_pf", "test_pf", "ex_best_day_pf", "ex_top_sym_pf",
            "stop_rate", "target_rate", "time_rate",
            "days_active", "pct_pos_days",
        ]}
    wins = [t for t in trades if t.pnl_r > 0]
    losses = [t for t in trades if t.pnl_r <= 0]
    total_r = sum(t.pnl_r for t in trades)
    gw = sum(t.pnl_r for t in wins)
    gl = abs(sum(t.pnl_r for t in losses))
    pf = gw / gl if gl > 0 else float("inf")

    cum = peak = max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.entry_time):
        cum += t.pnl_r
        if cum > peak: peak = cum
        dd = peak - cum
        if dd > max_dd: max_dd = dd

    train = [t for t in trades if t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date.day % 2 == 0]
    train_pf = gw_gl_pf(train)
    test_pf = gw_gl_pf(test)

    day_r = defaultdict(float)
    for t in trades:
        day_r[t.entry_date] += t.pnl_r
    if day_r:
        best_day = max(day_r, key=day_r.get)
        ex_best = [t for t in trades if t.entry_date != best_day]
    else:
        ex_best = trades
    ex_best_day_pf = gw_gl_pf(ex_best)

    sym_r = defaultdict(float)
    for t in trades:
        sym_r[t.symbol] += t.pnl_r
    if sym_r:
        top_sym = max(sym_r, key=sym_r.get)
        ex_top = [t for t in trades if t.symbol != top_sym]
    else:
        ex_top = trades
    ex_top_sym_pf = gw_gl_pf(ex_top)

    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    target_hit = sum(1 for t in trades if t.exit_reason == "target")
    timed = sum(1 for t in trades if t.exit_reason in ("time", "eod"))
    days_active = len(set(t.entry_date for t in trades))
    pos_days = sum(1 for d in day_r.values() if d > 0)
    pct_pos = pos_days / len(day_r) * 100 if day_r else 0

    return {
        "n": n, "wr": len(wins) / n * 100, "pf": pf, "exp": total_r / n,
        "total_r": total_r, "max_dd": max_dd,
        "train_pf": train_pf, "test_pf": test_pf,
        "ex_best_day_pf": ex_best_day_pf, "ex_top_sym_pf": ex_top_sym_pf,
        "stop_rate": stopped / n * 100, "target_rate": target_hit / n * 100,
        "time_rate": timed / n * 100,
        "days_active": days_active, "pct_pos_days": pct_pos,
    }


def gw_gl_pf(trades):
    if not trades: return 0.0
    gw = sum(t.pnl_r for t in trades if t.pnl_r > 0)
    gl = abs(sum(t.pnl_r for t in trades if t.pnl_r <= 0))
    return gw / gl if gl > 0 else float("inf")


def pf_str(v):
    return f"{v:.2f}" if v < 999 else "inf"


# ════════════════════════════════════════════════════════════════
#  Variant definitions — LIVE CLEAN ONLY
# ════════════════════════════════════════════════════════════════

LIVE_VARIANTS = [
    # VK family — ML2 (SPY above VWAP, no GREEN)
    {"name": "VK_ML2",                    "E": "VK", "M": "ML2", "P": "-",  "L": "-"},
    {"name": "VK_ML2_leader",             "E": "VK", "M": "ML2", "P": "-",  "L": "L1"},
    {"name": "VK_ML2_inplay_leader",      "E": "VK", "M": "ML2", "P": "P3", "L": "L1"},
    # VK family — ML3 (SPY above VWAP + EMA structure)
    {"name": "VK_ML3",                    "E": "VK", "M": "ML3", "P": "-",  "L": "-"},
    {"name": "VK_ML3_leader",             "E": "VK", "M": "ML3", "P": "-",  "L": "L1"},
    {"name": "VK_ML3_inplay_leader",      "E": "VK", "M": "ML3", "P": "P3", "L": "L1"},

    # EMA9 family — ML2
    {"name": "EMA9_ML2",                  "E": "EMA9", "M": "ML2", "P": "-",  "L": "-"},
    {"name": "EMA9_ML2_leader",           "E": "EMA9", "M": "ML2", "P": "-",  "L": "L1"},
    {"name": "EMA9_ML2_inplay_leader",    "E": "EMA9", "M": "ML2", "P": "P3", "L": "L1"},
    # EMA9 family — ML3
    {"name": "EMA9_ML3",                  "E": "EMA9", "M": "ML3", "P": "-",  "L": "-"},
    {"name": "EMA9_ML3_leader",           "E": "EMA9", "M": "ML3", "P": "-",  "L": "L1"},
    {"name": "EMA9_ML3_inplay_leader",    "E": "EMA9", "M": "ML3", "P": "P3", "L": "L1"},

    # No market filter (pure leader)
    {"name": "VK_leader_noM",             "E": "VK",   "M": "-",   "P": "-",  "L": "L1"},
    {"name": "EMA9_leader_noM",           "E": "EMA9", "M": "-",   "P": "-",  "L": "L1"},
]


def run_variant(variant: dict, symbols: list, spy_ctx: dict,
                all_open_stats: dict, all_stock_pfo: dict,
                spy_pfo: dict, sector_pfos: dict,
                all_bars: dict) -> List[CTrade]:
    entry_type = variant["E"]
    m_level = variant["M"]
    p_level = variant["P"]
    l_level = variant["L"]

    entry_func = entry_vk if entry_type == "VK" else entry_ema9
    all_trades = []

    for sym in symbols:
        bars = all_bars.get(sym, [])
        if not bars:
            continue
        open_stats = all_open_stats.get(sym, {})
        stock_pfo = all_stock_pfo.get(sym, {})
        sec_etf = get_sector_etf(sym)
        sector_pfo = sector_pfos.get(sec_etf, {})

        trades = entry_func(
            bars, sym, spy_ctx,
            open_stats, stock_pfo, spy_pfo, sector_pfo,
            m_level=m_level, p_level=p_level, l_level=l_level,
        )
        all_trades.extend(trades)

    return all_trades


# ════════════════════════════════════════════════════════════════
#  CSV export
# ════════════════════════════════════════════════════════════════

def export_csv(trades: List[CTrade], path: Path, label: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["section", "date", "entry_time", "symbol", "setup",
                     "entry_price", "stop_price", "target_price",
                     "pnl_r", "exit_reason", "bars_held"])
        for t in sorted(trades, key=lambda x: x.entry_time):
            w.writerow([
                label,
                str(t.entry_date),
                t.entry_time.strftime("%Y-%m-%d %H:%M"),
                t.symbol,
                t.setup,
                f"{t.entry_price:.2f}",
                f"{t.stop_price:.2f}",
                f"{t.target_price:.2f}",
                f"{t.pnl_r:+.4f}",
                t.exit_reason,
                t.bars_held,
            ])
    print(f"  → Exported {len(trades)} trades to {path.name}")


# ════════════════════════════════════════════════════════════════
#  Capped portfolio
# ════════════════════════════════════════════════════════════════

def capped_portfolio(trades: List[CTrade], max_open: int = 3) -> List[CTrade]:
    """Approximate: entry bar = timestamp, exit bar = entry + bars_held * 5min."""
    from datetime import timedelta
    trades_sorted = sorted(trades, key=lambda t: t.entry_time)
    accepted = []
    open_positions = []  # (exit_time_approx, symbol)
    for t in trades_sorted:
        exit_approx = t.entry_time + timedelta(minutes=t.bars_held * 5)
        open_positions = [
            (et, sym) for (et, sym) in open_positions
            if et > t.entry_time
        ]
        open_syms = {sym for (_, sym) in open_positions}
        if t.symbol in open_syms:
            continue
        if len(open_positions) >= max_open:
            continue
        accepted.append(t)
        open_positions.append((exit_approx, t.symbol))
    return accepted


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    print("=" * 150)
    print("COMPOSITE LEADER VARIANTS — LIVE-CLEAN REPLAY")
    print("Market support: ML2 (SPY > VWAP) / ML3 (SPY > VWAP + EMA9 > EMA20) — NO GREEN hindsight")
    print("=" * 150)

    # Load data
    print("\n  Loading data...")
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    symbols = get_universe()
    all_bars = {}
    for sym in symbols:
        bars = load_bars(sym)
        if bars:
            all_bars[sym] = bars
    symbols = sorted(all_bars.keys())
    print(f"  Universe: {len(symbols)} symbols with data")

    spy_ctx = build_spy_snapshots(spy_bars)
    spy_pfo = build_pct_from_open(spy_bars)

    sector_pfos = {}
    for etf in set(SECTOR_MAP.values()):
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sec_bars = load_bars_from_csv(str(p))
            sector_pfos[etf] = build_pct_from_open(sec_bars)

    all_open_stats = {}
    all_stock_pfo = {}
    for sym in symbols:
        bars = all_bars[sym]
        all_open_stats[sym] = compute_open_stats(bars)
        all_stock_pfo[sym] = build_pct_from_open(bars)

    spy_dates = sorted(set(b.timestamp.date() for b in spy_bars))
    print(f"  Date range: {spy_dates[0]} → {spy_dates[-1]} ({len(spy_dates)} days)")
    print(f"  Cost model: {SLIPPAGE_BPS} bps/side + ${COMMISSION_PER_SHARE}/share")

    # ── Run all live-clean variants ──
    print(f"\n{'='*150}")
    print("SECTION 1 — LIVE-CLEAN VARIANT MATRIX")
    print("=" * 150)

    hdr = (f"  {'Variant':<35s} {'N':>5s} {'PF':>5s} {'Exp(R)':>7s} {'TotalR':>8s} "
           f"{'MaxDD':>6s} {'WR%':>5s} {'Stop%':>5s} {'Tgt%':>5s} "
           f"{'TrnPF':>6s} {'TstPF':>6s} {'ExDay':>6s} {'ExSym':>6s} "
           f"{'%Pos':>5s} {'Verdict':>12s}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    results = {}
    for v in LIVE_VARIANTS:
        name = v["name"]
        trades = run_variant(v, symbols, spy_ctx, all_open_stats, all_stock_pfo,
                             spy_pfo, sector_pfos, all_bars)
        m = compute_metrics(trades)
        results[name] = {"trades": trades, "metrics": m, "variant": v}

        # Verdict
        if m["n"] < 20:
            verdict = "INSUFF_N"
        else:
            c1 = m["pf"] > 1.0
            c2 = m["exp"] > 0
            c3 = m["train_pf"] > 0.80
            c4 = m["test_pf"] > 0.80
            stable = c3 and c4
            passed = sum([c1, c2, c3, c4])
            if passed == 4:
                verdict = "PROMOTE" if m["pf"] > 1.20 else "SURVIVE"
            elif passed >= 3:
                verdict = "CONTINUE"
            else:
                verdict = "RETIRE"

        print(f"  {name:<35s} {m['n']:5d} {pf_str(m['pf']):>5s} {m['exp']:+7.3f} "
              f"{m['total_r']:+8.1f} {m['max_dd']:6.1f} {m['wr']:5.1f} {m['stop_rate']:5.1f} "
              f"{m['target_rate']:5.1f} {pf_str(m['train_pf']):>6s} {pf_str(m['test_pf']):>6s} "
              f"{pf_str(m['ex_best_day_pf']):>6s} {pf_str(m['ex_top_sym_pf']):>6s} "
              f"{m['pct_pos_days']:5.1f} {verdict:>12s}")

    # ── Best variant deep-dive ──
    surviving = {k: v for k, v in results.items()
                 if v["metrics"]["n"] >= 20 and v["metrics"]["pf"] > 1.0 and v["metrics"]["exp"] > 0}

    if surviving:
        best_name = max(surviving, key=lambda k: surviving[k]["metrics"]["pf"])
        best = surviving[best_name]
        best_trades = best["trades"]
        best_m = best["metrics"]

        print(f"\n{'='*150}")
        print(f"SECTION 2 — BEST LIVE-CLEAN VARIANT DEEP DIVE: {best_name}")
        print("=" * 150)

        print(f"\n  Trade count:       {best_m['n']}")
        print(f"  Profit factor:     {pf_str(best_m['pf'])}")
        print(f"  Expectancy (R):    {best_m['exp']:+.3f}")
        print(f"  Total R:           {best_m['total_r']:+.1f}")
        print(f"  Max drawdown (R):  {best_m['max_dd']:.1f}")
        print(f"  Win rate:          {best_m['wr']:.1f}%")
        print(f"  Train PF:          {pf_str(best_m['train_pf'])}")
        print(f"  Test PF:           {pf_str(best_m['test_pf'])}")
        print(f"  Ex-best-day PF:    {pf_str(best_m['ex_best_day_pf'])}")
        print(f"  Ex-top-sym PF:     {pf_str(best_m['ex_top_sym_pf'])}")
        print(f"  Stop rate:         {best_m['stop_rate']:.1f}%")
        print(f"  Target rate:       {best_m['target_rate']:.1f}%")

        c1 = best_m["pf"] > 1.0
        c2 = best_m["exp"] > 0
        c3 = best_m["train_pf"] > 0.80
        c4 = best_m["test_pf"] > 0.80
        n_ok = best_m["n"] >= 10
        print(f"\n  PROMOTION CRITERIA:")
        print(f"    N ≥ 10:           {'PASS' if n_ok else 'FAIL'} ({best_m['n']})")
        print(f"    PF > 1.0:         {'PASS' if c1 else 'FAIL'} ({pf_str(best_m['pf'])})")
        print(f"    Exp > 0:          {'PASS' if c2 else 'FAIL'} ({best_m['exp']:+.3f})")
        print(f"    Train PF > 0.80:  {'PASS' if c3 else 'FAIL'} ({pf_str(best_m['train_pf'])})")
        print(f"    Test PF > 0.80:   {'PASS' if c4 else 'FAIL'} ({pf_str(best_m['test_pf'])})")
        all_pass = c1 and c2 and c3 and c4 and n_ok
        print(f"    → {'SURVIVES' if all_pass else 'DOES NOT SURVIVE'}")

        # Monthly breakdown
        monthly_r = defaultdict(float)
        monthly_n = defaultdict(int)
        for t in best_trades:
            key = t.entry_date.strftime("%Y-%m")
            monthly_r[key] += t.pnl_r
            monthly_n[key] += 1
        cum = 0.0
        print(f"\n  Monthly breakdown:")
        print(f"    {'Month':>8s}  {'N':>5s}  {'R':>8s}  {'CumR':>8s}")
        print(f"    {'-'*35}")
        for mo in sorted(monthly_r.keys()):
            cum += monthly_r[mo]
            print(f"    {mo:>8s}  {monthly_n[mo]:5d}  {monthly_r[mo]:+8.1f}  {cum:+8.1f}")

        # Top/bottom symbols
        sym_r = defaultdict(float)
        sym_n = defaultdict(int)
        for t in best_trades:
            sym_r[t.symbol] += t.pnl_r
            sym_n[t.symbol] += 1
        sorted_syms = sorted(sym_r.items(), key=lambda x: x[1], reverse=True)
        print(f"\n  Top 10 symbols:")
        for sym, tr in sorted_syms[:10]:
            print(f"    {sym:<6s}  N={sym_n[sym]:3d}  TotalR={tr:+6.1f}")
        print(f"  Bottom 10 symbols:")
        for sym, tr in sorted_syms[-10:]:
            print(f"    {sym:<6s}  N={sym_n[sym]:3d}  TotalR={tr:+6.1f}")

        export_csv(best_trades, OUT_DIR / f"replay_composite_{best_name}.csv", best_name)

        # ── Section 3: Capped ──
        print(f"\n{'='*150}")
        print(f"SECTION 3 — CAPPED PORTFOLIO (max 3 concurrent) — {best_name}")
        print("=" * 150)

        capped = capped_portfolio(best_trades, max_open=3)
        cm = compute_metrics(capped)
        print(f"\n  Uncapped:  N={best_m['n']:5d}  PF={pf_str(best_m['pf'])}  Exp={best_m['exp']:+.3f}  TotalR={best_m['total_r']:+.1f}")
        print(f"  Capped(3): N={cm['n']:5d}  PF={pf_str(cm['pf'])}  Exp={cm['exp']:+.3f}  TotalR={cm['total_r']:+.1f}")
        print(f"             Train={pf_str(cm['train_pf'])}  Test={pf_str(cm['test_pf'])}  ExDay={pf_str(cm['ex_best_day_pf'])}  ExSym={pf_str(cm['ex_top_sym_pf'])}")
        export_csv(capped, OUT_DIR / f"replay_composite_{best_name}_capped.csv", f"{best_name}_capped")

    else:
        print(f"\n  No live-clean variants survived (PF>1.0 + Exp>0 with N>=20)")

    # ── Summary ──
    print(f"\n{'='*150}")
    print("FINAL SUMMARY — LIVE-CLEAN COMPOSITE LEADER VARIANTS")
    print("=" * 150)

    promoted = [(k, v) for k, v in results.items()
                if v["metrics"]["n"] >= 20 and v["metrics"]["pf"] > 1.0
                and v["metrics"]["exp"] > 0 and v["metrics"]["train_pf"] > 0.80
                and v["metrics"]["test_pf"] > 0.80]

    if promoted:
        print(f"\n  SURVIVORS (all 4 criteria pass, N >= 20):")
        for name, data in sorted(promoted, key=lambda x: -x[1]["metrics"]["pf"]):
            m = data["metrics"]
            print(f"    {name:<35s}  N={m['n']:5d}  PF={pf_str(m['pf'])}  Exp={m['exp']:+.3f}  "
                  f"Trn={pf_str(m['train_pf'])} Tst={pf_str(m['test_pf'])}  "
                  f"ExDay={pf_str(m['ex_best_day_pf'])} ExSym={pf_str(m['ex_top_sym_pf'])}")
    else:
        print(f"\n  No variants passed all 4 promotion criteria with N >= 20.")

    # Export all trade logs for survivors
    for name, data in results.items():
        m = data["metrics"]
        if m["n"] >= 20 and m["pf"] > 1.0 and m["exp"] > 0:
            export_csv(data["trades"], OUT_DIR / f"replay_composite_{name}.csv", name)

    print(f"\n{'='*150}")
    print("DONE.")
    print("=" * 150)


if __name__ == "__main__":
    main()
