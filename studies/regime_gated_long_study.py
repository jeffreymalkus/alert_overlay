"""
Regime-Gated Long Study — Decisive test of 10:15 sector-breadth gate.

Question: Does applying the 10:15 sector VWAP breadth gate to the best
surviving live long entry (VK_acc + L1 + ML1) create a materially better
strategy, or is the classifier merely diagnostic?

Entry scaffold: VK acceptance, hold=2, expansion close, 3R target
  - Market gate: ML1 (SPY above VWAP at signal bar)
  - Leadership: L1 (stock RS > SPY)
  - In-play: none
  - Window: 10:15-11:30 (shifted from 10:00 to align with gate availability)
  - Exit: X2 (3R target, stop at acceptance low / VWAP)

Regime gate (computed once at 10:15, frozen for the day):
  Gate A: >6 sectors above VWAP at 10:15
  Gate B: >7 sectors above VWAP at 10:15
  Gate C: SPY > VWAP AND >6 sectors above VWAP at 10:15

Deployment modes:
  1. Ungated baseline (same entry, 10:15-11:30, no regime gate)
  2. Gate A only
  3. Gate B only
  4. Gate C only

For each mode: full metrics, robustness, concentration.
For each gate: deploy-day vs non-deploy-day contrast.
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

SECTOR_ETFS = sorted(set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})

# Entry window: 10:15-11:30 (aligned with gate availability)
TIME_START = 1015
TIME_END = 1130

# Cost model
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
    risk: float = 0.0
    pnl_rr: float = 0.0
    pnl_rr_raw: float = 0.0  # before costs
    exit_reason: str = ""
    bars_held: int = 0
    rs_spy: float = 0.0
    market_level: str = ""
    regime_gate: str = ""  # which gate this trade was under

    @property
    def entry_date(self) -> date:
        return self.entry_time.date()


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
#  Regime gate: 10:15 sector VWAP breadth (computed ONCE, frozen)
# ════════════════════════════════════════════════════════════════

GATE_BAR_IDX = 9  # bar 9 = 10:15 (9:30 + 9*5min)
GATE_HHMM = 1015

def compute_daily_regime(spy_daily: dict, sector_daily: dict, all_dates: list) -> dict:
    """Compute regime state at 10:15 for each day. Returns dict: date -> regime_info."""
    regime = {}
    for d in all_dates:
        # SPY VWAP at 10:15
        spy_bars = spy_daily.get(d, [])
        spy_above_vwap = False
        if len(spy_bars) >= GATE_BAR_IDX:
            vwap = VWAPCalc()
            for i, b in enumerate(spy_bars):
                tp = (b.high + b.low + b.close) / 3.0
                vw = vwap.update(tp, b.volume)
                if i + 1 >= GATE_BAR_IDX:
                    break
            cutoff_bar = spy_bars[GATE_BAR_IDX - 1]
            spy_above_vwap = cutoff_bar.close > vw if vwap.ready else False

        # Sector VWAP breadth at 10:15
        sectors_above = 0
        sectors_checked = 0
        for etf in SECTOR_ETFS:
            etf_bars = sector_daily.get(etf, {}).get(d, [])
            if len(etf_bars) < GATE_BAR_IDX:
                continue
            sectors_checked += 1
            vwap = VWAPCalc()
            for i, b in enumerate(etf_bars):
                tp = (b.high + b.low + b.close) / 3.0
                vw = vwap.update(tp, b.volume)
                if i + 1 >= GATE_BAR_IDX:
                    break
            cutoff_bar = etf_bars[GATE_BAR_IDX - 1]
            if cutoff_bar.close > vw and vwap.ready:
                sectors_above += 1

        regime[d] = {
            "spy_above_vwap": spy_above_vwap,
            "sectors_above": sectors_above,
            "sectors_checked": sectors_checked,
            "gate_a": sectors_above > 6,
            "gate_b": sectors_above > 7,
            "gate_c": spy_above_vwap and sectors_above > 6,
        }

    return regime


# ════════════════════════════════════════════════════════════════
#  Live market gate (ML1: SPY above VWAP at signal bar)
# ════════════════════════════════════════════════════════════════

def build_spy_snapshots(spy_bars: list) -> dict:
    me = MarketEngine()
    snaps = {}
    for b in spy_bars:
        snap = me.process_bar(b)
        d = b.timestamp.date()
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute
        snaps[(d, hhmm)] = snap
    return snaps


def check_market_live(spy_ctx: dict, d: date, hhmm: int) -> bool:
    """ML1: SPY above VWAP at signal bar."""
    snap = spy_ctx.get((d, hhmm))
    if snap is None or not snap.ready:
        return False
    return snap.above_vwap


# ════════════════════════════════════════════════════════════════
#  Leadership (L1: RS > SPY)
# ════════════════════════════════════════════════════════════════

def build_pct_from_open(bars: list) -> dict:
    daily_open = {}
    result = {}
    for b in bars:
        d = b.timestamp.date()
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute
        if d not in daily_open:
            daily_open[d] = b.open
        o = daily_open[d]
        result[(d, hhmm)] = (b.close - o) / o * 100 if o > 0 else 0.0
    return result


# ════════════════════════════════════════════════════════════════
#  Exit: X2 (3R target, stop at acceptance low / VWAP, EOD close)
# ════════════════════════════════════════════════════════════════

def simulate_trade(trade: CTrade, bars: list, bar_idx: int) -> CTrade:
    risk = trade.risk
    if risk <= 0:
        trade.pnl_rr = 0
        trade.pnl_rr_raw = 0
        trade.exit_reason = "invalid"
        return trade

    target_rr = 3.0
    for i in range(bar_idx + 1, len(bars)):
        b = bars[i]
        trade.bars_held += 1
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute

        if hhmm >= 1555 or i == len(bars) - 1:
            trade.pnl_rr_raw = (b.close - trade.entry_price) / risk
            trade.exit_reason = "eod"
            break
        if b.low <= trade.stop_price:
            trade.pnl_rr_raw = (trade.stop_price - trade.entry_price) / risk
            trade.exit_reason = "stop"
            break
        if b.high >= trade.entry_price + target_rr * risk:
            trade.pnl_rr_raw = target_rr
            trade.exit_reason = "target"
            break

    # Apply costs
    entry = trade.entry_price
    slip = entry * SLIPPAGE_BPS / 10000
    comm = COMMISSION_PER_SHARE
    cost_r = 2 * (slip + comm) / risk
    trade.pnl_rr = trade.pnl_rr_raw - cost_r
    return trade


# ════════════════════════════════════════════════════════════════
#  Entry: VK acceptance (the best surviving live scaffold)
# ════════════════════════════════════════════════════════════════

def run_vk_entry(bars: list, sym: str, spy_ctx: dict,
                 stock_pfo: dict, spy_pfo: dict) -> List[CTrade]:
    """VK acceptance + ML1 + L1. No regime gate applied here — that's done post-hoc."""
    trades = []
    ema9 = EMA(9)
    vwap = VWAPCalc()
    vol_buf = deque(maxlen=20)
    vol_ma = NaN
    atr_buf = deque(maxlen=14)
    intra_atr = NaN

    touched = False
    hold_count = 0
    hold_low = NaN
    triggered_today = None
    prev_date = None

    for i, bar in enumerate(bars):
        e9 = ema9.update(bar.close)
        tp = (bar.high + bar.low + bar.close) / 3.0
        d = bar.timestamp.date()
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute

        if d != prev_date:
            vwap.reset()
            touched = False
            hold_count = 0
            hold_low = NaN
            triggered_today = None
            prev_date = d

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

        # Time window: 10:15-11:30
        if hhmm < TIME_START or hhmm > TIME_END:
            continue
        if triggered_today == d:
            continue

        # ML1: SPY above VWAP at signal bar
        if not check_market_live(spy_ctx, d, hhmm):
            continue

        # L1: stock RS > SPY
        s_pct = stock_pfo.get((d, hhmm), 0.0)
        spy_pct = spy_pfo.get((d, hhmm), 0.0)
        rs_spy = s_pct - spy_pct
        if rs_spy <= 0:
            continue

        # VK acceptance state machine
        kiss_dist = 0.05 * intra_atr
        above_vwap = bar.close > vw
        near_vwap = abs(bar.low - vw) <= kiss_dist or bar.low <= vw <= bar.high

        if near_vwap and above_vwap:
            if not touched:
                touched = True
                hold_count = 1
                hold_low = bar.low
            elif hold_count > 0 and hold_count < 2:
                hold_count += 1
                hold_low = min(hold_low, bar.low)
        elif above_vwap and touched and hold_count > 0 and hold_count < 2:
            hold_count += 1
            hold_low = min(hold_low, bar.low)
        elif not above_vwap:
            touched = False
            hold_count = 0
            hold_low = NaN

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
                    t = CTrade(
                        symbol=sym, entry_time=bar.timestamp,
                        entry_price=bar.close, stop_price=stop,
                        target_price=bar.close + 3.0 * risk,
                        risk=risk, rs_spy=rs_spy, market_level="ML1",
                    )
                    t = simulate_trade(t, bars, i)
                    trades.append(t)
                    triggered_today = d
                    touched = False
                    hold_count = 0
                    hold_low = NaN

    return trades


# ════════════════════════════════════════════════════════════════
#  Metrics engine
# ════════════════════════════════════════════════════════════════

def compute_metrics(trades: List[CTrade], all_dates: list = None, eligible_dates: set = None) -> dict:
    """Full metric suite."""
    n = len(trades)
    if n == 0:
        return _empty_metrics()

    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    total_r = sum(t.pnl_rr for t in trades)

    # Raw (no costs)
    gw_raw = sum(t.pnl_rr_raw for t in wins if t.pnl_rr_raw > 0)
    gl_raw = abs(sum(t.pnl_rr_raw for t in [t2 for t2 in trades if t2.pnl_rr_raw <= 0]))
    pf_raw = gw_raw / gl_raw if gl_raw > 0 else float("inf")
    total_r_raw = sum(t.pnl_rr_raw for t in trades)

    wr = len(wins) / n * 100
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    quick_stop = sum(1 for t in trades if t.exit_reason == "stop" and t.bars_held <= 3)
    targets = sum(1 for t in trades if t.exit_reason == "target")
    avg_win = gw / len(wins) if wins else 0
    avg_loss = -gl / len(losses) if losses else 0

    all_rr = sorted([t.pnl_rr for t in trades])
    median_r = all_rr[n // 2]

    # Max DD
    cum = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl_rr
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum

    # Daily aggregates
    by_day = defaultdict(list)
    for t in trades:
        by_day[t.entry_date].append(t)
    days_active = len(by_day)
    daily_rs = {d: sum(t.pnl_rr for t in ts) for d, ts in by_day.items()}
    daily_r_list = sorted(daily_rs.values())
    median_daily_r = daily_r_list[len(daily_r_list) // 2] if daily_r_list else 0
    pct_pos_days = sum(1 for r in daily_rs.values() if r > 0) / days_active * 100 if days_active else 0
    avg_trades_day = n / days_active if days_active else 0

    # Zero-trade days among eligible
    if eligible_dates:
        zero_trade_days = len(eligible_dates - set(by_day.keys()))
        pct_zero_trade = zero_trade_days / len(eligible_dates) * 100 if eligible_dates else 0
    else:
        zero_trade_days = 0
        pct_zero_trade = 0

    # Train/test (odd/even dates)
    train = [t for t in trades if t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date.day % 2 == 0]
    train_pf = _pf(train)
    test_pf = _pf(test)

    # Ex-best-day
    if daily_rs:
        best_day = max(daily_rs, key=daily_rs.get)
        ex_best = [t for t in trades if t.entry_date != best_day]
        ex_best_pf = _pf(ex_best)
    else:
        ex_best_pf = 0

    # Ex-top-symbol
    by_sym = defaultdict(float)
    for t in trades:
        by_sym[t.symbol] += t.pnl_rr
    if by_sym:
        top_sym = max(by_sym, key=by_sym.get)
        ex_top_sym = [t for t in trades if t.symbol != top_sym]
        ex_top_sym_pf = _pf(ex_top_sym)
    else:
        ex_top_sym_pf = 0

    # Top-3-day contribution
    sorted_days = sorted(daily_rs.items(), key=lambda x: x[1], reverse=True)
    top3_day_r = sum(r for _, r in sorted_days[:3])
    top3_day_pct = top3_day_r / total_r * 100 if total_r != 0 else 0

    # Top-3-symbol contribution
    sorted_syms = sorted(by_sym.items(), key=lambda x: x[1], reverse=True)
    top3_sym_r = sum(r for _, r in sorted_syms[:3])
    top3_sym_pct = top3_sym_r / total_r * 100 if total_r != 0 else 0

    return {
        "n": n, "days_active": days_active, "avg_trades_day": avg_trades_day,
        "pf": pf, "exp": total_r / n, "total_r": total_r, "max_dd": dd,
        "pf_raw": pf_raw, "total_r_raw": total_r_raw,
        "wr": wr, "stop_rate": stopped / n * 100, "quick_stop_rate": quick_stop / n * 100,
        "target_rate": targets / n * 100,
        "avg_win": avg_win, "avg_loss": avg_loss, "median_r": median_r,
        "train_pf": train_pf, "test_pf": test_pf,
        "ex_best_day_pf": ex_best_pf, "ex_top_sym_pf": ex_top_sym_pf,
        "top3_day_pct": top3_day_pct, "top3_sym_pct": top3_sym_pct,
        "median_daily_r": median_daily_r, "pct_pos_days": pct_pos_days,
        "pct_zero_trade": pct_zero_trade,
        "top3_days": sorted_days[:3], "top3_syms": sorted_syms[:3],
    }


def _pf(trades):
    if not trades:
        return 0
    gw = sum(t.pnl_rr for t in trades if t.pnl_rr > 0)
    gl = abs(sum(t.pnl_rr for t in trades if t.pnl_rr <= 0))
    return gw / gl if gl > 0 else float("inf")


def _empty_metrics():
    return {k: 0 for k in [
        "n", "days_active", "avg_trades_day", "pf", "exp", "total_r", "max_dd",
        "pf_raw", "total_r_raw", "wr", "stop_rate", "quick_stop_rate", "target_rate",
        "avg_win", "avg_loss", "median_r", "train_pf", "test_pf",
        "ex_best_day_pf", "ex_top_sym_pf", "top3_day_pct", "top3_sym_pct",
        "median_daily_r", "pct_pos_days", "pct_zero_trade",
    ]}


# ════════════════════════════════════════════════════════════════
#  Reporting
# ════════════════════════════════════════════════════════════════

def print_metrics(label: str, m: dict, indent: int = 2):
    pad = " " * indent
    pf_s = f"{m['pf']:.2f}" if m['pf'] < 99 else "inf"
    pf_raw_s = f"{m['pf_raw']:.2f}" if m['pf_raw'] < 99 else "inf"
    print(f"{pad}{label}")
    print(f"{pad}  Trades: {m['n']:>5d}   Days active: {m['days_active']:>3d}   "
          f"Avg/day: {m['avg_trades_day']:.1f}")
    print(f"{pad}  PF(R): {pf_s:>6s}   PF(raw): {pf_raw_s:>6s}   "
          f"Exp(R): {m['exp']:+.3f}   Total R: {m['total_r']:+.1f}   "
          f"Raw R: {m['total_r_raw']:+.1f}")
    print(f"{pad}  MaxDD: {m['max_dd']:.1f}R   WR: {m['wr']:.1f}%   "
          f"Stop: {m['stop_rate']:.1f}%   QStop: {m['quick_stop_rate']:.1f}%   "
          f"Target: {m['target_rate']:.1f}%")
    print(f"{pad}  AvgWin: {m['avg_win']:+.3f}R   AvgLoss: {m['avg_loss']:+.3f}R   "
          f"MedianR: {m['median_r']:+.3f}")
    print(f"{pad}  Train PF: {m['train_pf']:.2f}   Test PF: {m['test_pf']:.2f}   "
          f"Ex-best-day PF: {m['ex_best_day_pf']:.2f}   Ex-top-sym PF: {m['ex_top_sym_pf']:.2f}")
    print(f"{pad}  Top3 day %: {m['top3_day_pct']:+.1f}%   Top3 sym %: {m['top3_sym_pct']:+.1f}%")
    print(f"{pad}  Median daily R: {m['median_daily_r']:+.3f}   "
          f"%pos days: {m['pct_pos_days']:.1f}%   "
          f"%zero-trade days: {m['pct_zero_trade']:.1f}%")


def print_comparison(gate_name: str, deploy_m: dict, nondeploy_m: dict, baseline_m: dict):
    print(f"\n  {gate_name} — Deploy vs Non-deploy contrast:")
    print(f"    {'Metric':20s} {'Deploy':>10s} {'Non-deploy':>12s} {'Delta':>10s} {'Baseline':>10s}")
    print(f"    {'-'*20} {'-'*10} {'-'*12} {'-'*10} {'-'*10}")

    rows = [
        ("Trades", f"{deploy_m['n']}", f"{nondeploy_m['n']}", "", f"{baseline_m['n']}"),
        ("Days", f"{deploy_m['days_active']}", f"{nondeploy_m['days_active']}", "", f"{baseline_m['days_active']}"),
        ("PF(R)", f"{deploy_m['pf']:.2f}", f"{nondeploy_m['pf']:.2f}",
         f"{deploy_m['pf'] - nondeploy_m['pf']:+.2f}", f"{baseline_m['pf']:.2f}"),
        ("Exp(R)", f"{deploy_m['exp']:+.3f}", f"{nondeploy_m['exp']:+.3f}",
         f"{deploy_m['exp'] - nondeploy_m['exp']:+.3f}", f"{baseline_m['exp']:+.3f}"),
        ("Total R", f"{deploy_m['total_r']:+.1f}", f"{nondeploy_m['total_r']:+.1f}",
         "", f"{baseline_m['total_r']:+.1f}"),
        ("WR%", f"{deploy_m['wr']:.1f}%", f"{nondeploy_m['wr']:.1f}%",
         f"{deploy_m['wr'] - nondeploy_m['wr']:+.1f}", f"{baseline_m['wr']:.1f}%"),
        ("Stop%", f"{deploy_m['stop_rate']:.1f}%", f"{nondeploy_m['stop_rate']:.1f}%",
         f"{deploy_m['stop_rate'] - nondeploy_m['stop_rate']:+.1f}", f"{baseline_m['stop_rate']:.1f}%"),
        ("MaxDD", f"{deploy_m['max_dd']:.1f}R", f"{nondeploy_m['max_dd']:.1f}R",
         "", f"{baseline_m['max_dd']:.1f}R"),
        ("%Pos days", f"{deploy_m['pct_pos_days']:.1f}%", f"{nondeploy_m['pct_pos_days']:.1f}%",
         f"{deploy_m['pct_pos_days'] - nondeploy_m['pct_pos_days']:+.1f}", f"{baseline_m['pct_pos_days']:.1f}%"),
    ]
    for label, d, nd, delta, bl in rows:
        print(f"    {label:20s} {d:>10s} {nd:>12s} {delta:>10s} {bl:>10s}")


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    W = 120
    print("=" * W)
    print("REGIME-GATED LONG STUDY — Decisive test of 10:15 sector-breadth gate")
    print("=" * W)

    # ── Section 1: Study definition ──
    print(f"\n{'='*W}")
    print("SECTION 1 — STUDY DEFINITION")
    print(f"{'='*W}")
    print("  Entry: VK acceptance (hold=2, expansion close, vol>70% MA)")
    print("  Market gate: ML1 (SPY above VWAP at signal bar)")
    print("  Leadership: L1 (stock RS > SPY at signal bar)")
    print("  In-play: none")
    print("  Exit: X2 (3R target, stop at acceptance low / VWAP, EOD close)")
    print(f"  Entry window: {TIME_START//100}:{TIME_START%100:02d}-{TIME_END//100}:{TIME_END%100:02d}")
    print("  Cost: 4bps slippage/side + $0.005/share commission")
    print()
    print("  Regime gate (computed ONCE at 10:15, frozen for the day):")
    print("    Gate A: >6 sectors above VWAP at 10:15")
    print("    Gate B: >7 sectors above VWAP at 10:15")
    print("    Gate C: SPY>VWAP AND >6 sectors above VWAP at 10:15")
    print()
    print("  LIVE-AVAILABLE: all regime features use only data through bar 9 (10:15).")
    print("  No hindsight. No GREEN-day. No future information.")

    # ── Load data ──
    symbols = get_universe()
    print(f"\n  Loading data...")
    spy_bars = load_bars("SPY")
    spy_daily = bars_by_day(spy_bars)
    all_dates = sorted(spy_daily.keys())

    # Sectors
    sector_daily = {}
    for etf in SECTOR_ETFS:
        sb = load_bars(etf)
        if sb:
            sector_daily[etf] = bars_by_day(sb)

    # Regime state
    print("  Computing 10:15 regime states...")
    regime = compute_daily_regime(spy_daily, sector_daily, all_dates)

    # Print regime day counts
    ga_days = sum(1 for d in all_dates if regime.get(d, {}).get("gate_a", False))
    gb_days = sum(1 for d in all_dates if regime.get(d, {}).get("gate_b", False))
    gc_days = sum(1 for d in all_dates if regime.get(d, {}).get("gate_c", False))
    print(f"  {len(all_dates)} trading days: Gate A={ga_days}d, Gate B={gb_days}d, Gate C={gc_days}d")

    # Print regime detail per day
    print(f"\n  Day-by-day regime state:")
    print(f"  {'Date':12s} {'Sec>VW':>6s} {'SPY>VW':>6s} {'A':>3s} {'B':>3s} {'C':>3s}")
    for d in all_dates:
        r = regime.get(d, {})
        print(f"  {str(d):12s} {r.get('sectors_above',0):6d} "
              f"{'Y' if r.get('spy_above_vwap') else 'N':>6s} "
              f"{'Y' if r.get('gate_a') else '-':>3s} "
              f"{'Y' if r.get('gate_b') else '-':>3s} "
              f"{'Y' if r.get('gate_c') else '-':>3s}")

    # ── Load symbols and run entry ──
    print(f"\n  Loading {len(symbols)} symbols and running VK acceptance entry...")
    spy_ctx = build_spy_snapshots(spy_bars)
    spy_pfo = build_pct_from_open(spy_bars)

    all_trades = []
    for sym in symbols:
        bars = load_bars(sym)
        if not bars:
            continue
        stock_pfo = build_pct_from_open(bars)
        trades = run_vk_entry(bars, sym, spy_ctx, stock_pfo, spy_pfo)
        all_trades.extend(trades)

    all_trades.sort(key=lambda t: t.entry_time)
    print(f"  Total trades (ungated): {len(all_trades)}")

    # ── Section 2: Ledger ──
    print(f"\n{'='*W}")
    print("SECTION 2 — LEDGER")
    print(f"{'='*W}")

    # Ungated baseline
    baseline_dates = set(all_dates)
    baseline_m = compute_metrics(all_trades, all_dates, baseline_dates)
    print_metrics("UNGATED BASELINE", baseline_m)

    # Gates
    gates = [
        ("GATE A (>6 sec>VWAP @10:15)", "gate_a"),
        ("GATE B (>7 sec>VWAP @10:15)", "gate_b"),
        ("GATE C (SPY>VW + >6 sec>VWAP @10:15)", "gate_c"),
    ]

    gate_results = {}
    for gname, gkey in gates:
        deploy_dates = set(d for d in all_dates if regime.get(d, {}).get(gkey, False))
        deploy_trades = [t for t in all_trades if t.entry_date in deploy_dates]
        nondeploy_trades = [t for t in all_trades if t.entry_date not in deploy_dates]

        deploy_m = compute_metrics(deploy_trades, all_dates, deploy_dates)
        nondeploy_m = compute_metrics(nondeploy_trades, all_dates, set(all_dates) - deploy_dates)

        print(f"\n  {gname}")
        print_metrics(f"  Deploy days ({len(deploy_dates)}d)", deploy_m, indent=4)
        print_metrics(f"  Non-deploy days ({len(all_dates) - len(deploy_dates)}d)", nondeploy_m, indent=4)

        gate_results[gkey] = {"deploy": deploy_m, "nondeploy": nondeploy_m, "deploy_dates": deploy_dates}

    # ── Section 3: Gate-on vs gate-off comparison ──
    print(f"\n{'='*W}")
    print("SECTION 3 — GATE-ON vs GATE-OFF COMPARISON")
    print(f"{'='*W}")

    for gname, gkey in gates:
        gr = gate_results[gkey]
        print_comparison(gname, gr["deploy"], gr["nondeploy"], baseline_m)

    # ── Section 4: Robustness / concentration detail ──
    print(f"\n{'='*W}")
    print("SECTION 4 — ROBUSTNESS & CONCENTRATION")
    print(f"{'='*W}")

    for gname, gkey in gates:
        gr = gate_results[gkey]
        dm = gr["deploy"]
        print(f"\n  {gname} — Deploy days:")
        print(f"    Train PF: {dm['train_pf']:.2f}   Test PF: {dm['test_pf']:.2f}")
        print(f"    Ex-best-day PF: {dm['ex_best_day_pf']:.2f}   Ex-top-sym PF: {dm['ex_top_sym_pf']:.2f}")
        if dm.get("top3_days"):
            print(f"    Top-3 days: ", end="")
            for d, r in dm["top3_days"]:
                print(f"{d} ({r:+.1f}R)  ", end="")
            print(f"  [{dm['top3_day_pct']:+.1f}% of total]")
        if dm.get("top3_syms"):
            print(f"    Top-3 syms: ", end="")
            for s, r in dm["top3_syms"]:
                print(f"{s} ({r:+.1f}R)  ", end="")
            print(f"  [{dm['top3_sym_pct']:+.1f}% of total]")
        print(f"    Median daily R: {dm['median_daily_r']:+.3f}   "
              f"%pos days: {dm['pct_pos_days']:.1f}%   "
              f"%zero-trade eligible: {dm['pct_zero_trade']:.1f}%")

    # ── Section 5: Summary table ──
    print(f"\n{'='*W}")
    print("SECTION 5 — SUMMARY COMPARISON TABLE")
    print(f"{'='*W}")
    print(f"\n  {'Mode':42s} {'N':>5s} {'Days':>5s} {'PF':>6s} {'PF(raw)':>7s} "
          f"{'Exp':>7s} {'TotR':>7s} {'MaxDD':>6s} {'WR%':>5s} {'Stop%':>5s} "
          f"{'TrnPF':>6s} {'TstPF':>6s} {'xBdPF':>6s} {'xTsPF':>6s} {'%Pos':>5s}")
    print(f"  {'-'*42} {'-'*5} {'-'*5} {'-'*6} {'-'*7} "
          f"{'-'*7} {'-'*7} {'-'*6} {'-'*5} {'-'*5} "
          f"{'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*5}")

    def row(label, m):
        pf_s = f"{m['pf']:.2f}" if m['pf'] < 99 else "inf"
        pf_raw_s = f"{m['pf_raw']:.2f}" if m['pf_raw'] < 99 else "inf"
        print(f"  {label:42s} {m['n']:5d} {m['days_active']:5d} {pf_s:>6s} {pf_raw_s:>7s} "
              f"{m['exp']:+6.3f} {m['total_r']:+6.1f} {m['max_dd']:5.1f} {m['wr']:4.1f}% {m['stop_rate']:4.1f}% "
              f"{m['train_pf']:5.2f} {m['test_pf']:5.2f} {m['ex_best_day_pf']:5.2f} {m['ex_top_sym_pf']:5.2f} {m['pct_pos_days']:4.1f}%")

    row("Ungated baseline", baseline_m)
    for gname, gkey in gates:
        row(f"{gname} (deploy)", gate_results[gkey]["deploy"])
        row(f"{gname} (non-deploy)", gate_results[gkey]["nondeploy"])

    print(f"\n{'='*W}")
    print("REGIME-GATED LONG STUDY COMPLETE")
    print(f"{'='*W}")


if __name__ == "__main__":
    main()
