"""
Day-Filter Decomposition — Isolate failure source

3×2 matrix:
  Rows:    standalone+perfect-foresight | standalone+realtime | engine+realtime
  Columns: VR | VKA

For each cell: N, PF(R), Exp(R), TotalR, MaxDD, Stop%, QStop%, Train/Test PF

Goal: Determine if the collapse is caused by:
  (a) the day-filter substitution (perfect → real-time), or
  (b) separate engine-integration drift
"""

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import List

from .backtest import run_backtest, load_bars_from_csv, Trade
from .config import OverlayConfig
from .indicators import EMA, VWAPCalc
from .market_context import MarketEngine, get_sector_etf, SECTOR_MAP
from .models import Bar, NaN, SetupId

DATA_DIR = Path(__file__).parent / "data"
_isnan = math.isnan


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


def classify_spy_days(spy_bars: list) -> dict:
    """Perfect-foresight: full-day SPY return."""
    daily = defaultdict(list)
    for b in spy_bars:
        daily[b.timestamp.date()].append(b)
    day_info = {}
    for d in sorted(daily.keys()):
        bars = daily[d]
        o, c = bars[0].open, bars[-1].close
        chg = (c - o) / o * 100 if o > 0 else 0
        if chg > 0.05:
            day_info[d] = "GREEN"
        elif chg < -0.05:
            day_info[d] = "RED"
        else:
            day_info[d] = "FLAT"
    return day_info


def build_spy_realtime_lookup(spy_bars: list) -> dict:
    """
    Build (date, hhmm) → pct_from_open for real-time proxy.
    This exactly replicates what the engine sees via spy_snap.pct_from_open.
    """
    lookup = {}
    day_open = {}
    for b in spy_bars:
        d = b.timestamp.date()
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute
        if d not in day_open:
            day_open[d] = b.open
        pct = (b.close - day_open[d]) / day_open[d] * 100 if day_open[d] > 0 else 0.0
        lookup[(d, hhmm)] = pct
    return lookup


def is_realtime_green(spy_rt: dict, d: date, hhmm: int) -> bool:
    """Real-time GREEN: SPY pct_from_open > +0.05% at signal time."""
    pct = spy_rt.get((d, hhmm))
    if pct is None:
        return True  # no data = allow (matches engine behavior)
    return pct > 0.05


# ════════════════════════════════════════════════════════════════
#  Trade + metrics (standalone)
# ════════════════════════════════════════════════════════════════

@dataclass
class ATrade:
    symbol: str
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    direction: int
    setup: str
    risk: float = 0.0
    pnl_rr: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0

    @property
    def entry_date(self) -> date:
        return self.entry_time.date()


def simulate_trade(trade: ATrade, bars: list, bar_idx: int,
                   max_bars: int = 78, target_rr: float = 3.0) -> ATrade:
    risk = trade.risk
    if risk <= 0:
        trade.pnl_rr = 0
        trade.exit_reason = "invalid"
        return trade

    for j in range(bar_idx + 1, min(bar_idx + max_bars + 1, len(bars))):
        b = bars[j]
        trade.bars_held += 1
        if b.timestamp.date() != trade.entry_time.date():
            break  # new day = EOD

        # Stop check
        if trade.direction == 1 and b.low <= trade.stop_price:
            trade.pnl_rr = (trade.stop_price - trade.entry_price) / risk
            trade.exit_reason = "stop"
            return trade

        # Target check
        if trade.direction == 1 and b.high >= trade.target_price:
            trade.pnl_rr = target_rr
            trade.exit_reason = "target"
            return trade

    # EOD flat
    last_bar = bars[min(bar_idx + max_bars, len(bars) - 1)]
    trade.pnl_rr = (last_bar.close - trade.entry_price) / risk
    trade.exit_reason = "eod"
    return trade


def compute_r_metrics(trades: list) -> dict:
    if not trades:
        return {"n": 0, "pf_r": 0, "exp_r": 0, "total_r": 0, "max_dd_r": 0,
                "stop_rate": 0, "quick_stop": 0, "target_rate": 0}

    wins_r = sum(t.pnl_rr for t in trades if t.pnl_rr > 0)
    losses_r = abs(sum(t.pnl_rr for t in trades if t.pnl_rr < 0))
    pf_r = wins_r / losses_r if losses_r > 0 else float('inf')
    total_r = sum(t.pnl_rr for t in trades)
    exp_r = total_r / len(trades)

    cum = 0
    peak = 0
    max_dd = 0
    for t in sorted(trades, key=lambda x: x.entry_time):
        cum += t.pnl_rr
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    stops = sum(1 for t in trades if t.exit_reason == "stop")
    qstops = sum(1 for t in trades if t.exit_reason == "stop" and t.bars_held <= 2)
    targets = sum(1 for t in trades if t.exit_reason == "target")

    # Train/test
    train = [t for t in trades if t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date.day % 2 == 0]
    tr_total = sum(t.pnl_rr for t in train)
    tr_wins = sum(t.pnl_rr for t in train if t.pnl_rr > 0)
    tr_losses = abs(sum(t.pnl_rr for t in train if t.pnl_rr < 0))
    te_total = sum(t.pnl_rr for t in test)
    te_wins = sum(t.pnl_rr for t in test if t.pnl_rr > 0)
    te_losses = abs(sum(t.pnl_rr for t in test if t.pnl_rr < 0))
    train_pf = tr_wins / tr_losses if tr_losses > 0 else float('inf')
    test_pf = te_wins / te_losses if te_losses > 0 else float('inf')

    return {
        "n": len(trades),
        "pf_r": pf_r,
        "exp_r": exp_r,
        "total_r": total_r,
        "max_dd_r": max_dd,
        "stop_rate": stops / len(trades) * 100,
        "quick_stop": qstops / len(trades) * 100,
        "target_rate": targets / len(trades) * 100,
        "train_pf": train_pf,
        "test_pf": test_pf,
        "train_n": len(train),
        "test_n": len(test),
    }


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


# ════════════════════════════════════════════════════════════════
#  Market alignment (standalone)
# ════════════════════════════════════════════════════════════════

def build_spy_market_snapshots(spy_bars: list) -> dict:
    me = MarketEngine()
    snapshots = {}
    for b in spy_bars:
        snap = me.process_bar(b)
        d = b.timestamp.date()
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute
        snapshots[(d, hhmm)] = snap
    return snapshots


def is_market_aligned_long(spy_ctx: dict, d: date, hhmm: int) -> bool:
    snap = spy_ctx.get((d, hhmm))
    if snap is None or not snap.ready:
        return True
    if not snap.above_vwap and not snap.ema9_above_ema20:
        return False
    return True


# ════════════════════════════════════════════════════════════════
#  Standalone VR (VWAP Reclaim)
# ════════════════════════════════════════════════════════════════

def standalone_vr(bars: list, sym: str, spy_day_info: dict, spy_rt: dict,
                  spy_ctx: dict, green_mode: str = "perfect") -> List[ATrade]:
    """
    VR standalone matching engine params:
      hold=3, target=3.0R, time 10:00-10:59, body>=40%, vol>=70% MA,
      market_align, green_only

    green_mode: "perfect" = end-of-day classification
                "realtime" = SPY pct_from_open > 0.05% at signal time
                "none" = no day filter
    """
    trades = []
    ema9 = EMA(9)
    vwap = VWAPCalc()
    vol_buf = deque(maxlen=20)
    vol_ma = NaN

    was_below = False
    hold_count = 0
    hold_low = NaN
    triggered_today = None
    prev_date = None

    hold_bars = 3
    target_rr = 3.0
    time_start = 1000
    time_end = 1059

    for i, bar in enumerate(bars):
        e9 = ema9.update(bar.close)
        tp = (bar.high + bar.low + bar.close) / 3.0

        d = bar.timestamp.date()
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute

        if d != prev_date:
            vwap.reset()
            was_below = False
            hold_count = 0
            hold_low = NaN
            triggered_today = None
            prev_date = d

        vw = vwap.update(tp, bar.volume)

        if len(vol_buf) == 20:
            vol_ma = sum(vol_buf) / 20
        vol_buf.append(bar.volume)

        if not vwap.ready:
            continue

        above_vwap = bar.close > vw

        # State machine (always runs, even outside window)
        if above_vwap and not was_below:
            pass
        elif not above_vwap:
            was_below = True
            hold_count = 0
            hold_low = NaN
        elif was_below and above_vwap and hold_count == 0:
            hold_count = 1
            hold_low = bar.low
        elif hold_count > 0 and hold_count < hold_bars:
            if above_vwap:
                hold_count += 1
                hold_low = min(hold_low, bar.low) if not _isnan(hold_low) else bar.low
            else:
                was_below = True
                hold_count = 0
                hold_low = NaN

        # Day filter
        if green_mode == "perfect":
            if spy_day_info.get(d) != "GREEN":
                continue
        elif green_mode == "realtime":
            if not is_realtime_green(spy_rt, d, hhmm):
                continue

        if hhmm < time_start or hhmm > time_end:
            continue
        if triggered_today == d:
            continue

        # Trigger check
        if hold_count >= hold_bars and above_vwap:
            rng = bar.high - bar.low
            body = abs(bar.close - bar.open)
            is_bull = bar.close > bar.open
            body_pct = body / rng if rng > 0 else 0

            candle_ok = is_bull and body_pct >= 0.40
            vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 0.70 * vol_ma
            market_ok = is_market_aligned_long(spy_ctx, d, hhmm)

            if candle_ok and vol_ok and market_ok:
                stop = (hold_low if not _isnan(hold_low) else bar.low) - 0.02
                risk = bar.close - stop
                if risk > 0:
                    target = bar.close + target_rr * risk
                    t = ATrade(symbol=sym, entry_time=bar.timestamp,
                               entry_price=bar.close, stop_price=stop,
                               target_price=target, direction=1,
                               setup="VR", risk=risk)
                    t = simulate_trade(t, bars, i, target_rr=target_rr)
                    trades.append(t)
                    triggered_today = d
                    was_below = False
                    hold_count = 0
                    hold_low = NaN

    return trades


# ════════════════════════════════════════════════════════════════
#  Standalone VKA (VWAP Kiss Accept)
# ════════════════════════════════════════════════════════════════

def standalone_vka(bars: list, sym: str, spy_day_info: dict, spy_rt: dict,
                   spy_ctx: dict, green_mode: str = "perfect") -> List[ATrade]:
    """
    VKA standalone matching engine params:
      hold=2, target=2.0R, time 10:00-10:59, body>=40%, vol>=70% MA,
      kiss_atr_frac=0.05, market_align, green_only

    green_mode: "perfect" | "realtime" | "none"
    """
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
    hold_high = NaN
    triggered_today = None
    prev_date = None

    hold_bars = 2
    target_rr = 2.0
    time_start = 1000
    time_end = 1059
    kiss_atr_frac = 0.05

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
            hold_high = NaN
            triggered_today = None
            prev_date = d

        vw = vwap.update(tp, bar.volume)

        tr = bar.high - bar.low
        atr_buf.append(tr)
        if len(atr_buf) >= 5:
            intra_atr = sum(atr_buf) / len(atr_buf)

        if len(vol_buf) == 20:
            vol_ma = sum(vol_buf) / 20
        vol_buf.append(bar.volume)

        if not vwap.ready or _isnan(intra_atr):
            continue

        above_vwap = bar.close >= vw
        kiss_dist = kiss_atr_frac * intra_atr
        near_vwap = abs(bar.close - vw) <= kiss_dist or (bar.low <= vw <= bar.high)

        # State machine (always runs)
        if not touched:
            if near_vwap or (bar.low <= vw <= bar.high):
                touched = True
                hold_count = 0
                hold_low = bar.low
                hold_high = bar.high
        elif touched and hold_count < hold_bars:
            if above_vwap or near_vwap:
                hold_count += 1
                hold_low = min(hold_low, bar.low) if not _isnan(hold_low) else bar.low
                hold_high = max(hold_high, bar.high) if not _isnan(hold_high) else bar.high
            else:
                touched = False
                hold_count = 0
                hold_low = NaN
                hold_high = NaN
                if near_vwap or (bar.low <= vw <= bar.high):
                    touched = True
                    hold_count = 0
                    hold_low = bar.low
                    hold_high = bar.high

        # Day filter
        if green_mode == "perfect":
            if spy_day_info.get(d) != "GREEN":
                continue
        elif green_mode == "realtime":
            if not is_realtime_green(spy_rt, d, hhmm):
                continue

        if hhmm < time_start or hhmm > time_end:
            continue
        if triggered_today == d:
            continue

        # Trigger check
        if touched and hold_count >= hold_bars and above_vwap:
            rng = bar.high - bar.low
            body = abs(bar.close - bar.open)
            is_bull = bar.close > bar.open
            body_pct = body / rng if rng > 0 else 0

            candle_ok = is_bull and body_pct >= 0.40
            vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 0.70 * vol_ma
            market_ok = is_market_aligned_long(spy_ctx, d, hhmm)

            if candle_ok and vol_ok and market_ok:
                stop = (hold_low if not _isnan(hold_low) else bar.low) - 0.02
                risk = bar.close - stop
                if risk > 0:
                    target = bar.close + target_rr * risk
                    t = ATrade(symbol=sym, entry_time=bar.timestamp,
                               entry_price=bar.close, stop_price=stop,
                               target_price=target, direction=1,
                               setup="VKA", risk=risk)
                    t = simulate_trade(t, bars, i, target_rr=target_rr)
                    trades.append(t)
                    triggered_today = d
                    touched = False
                    hold_count = 0
                    hold_low = NaN
                    hold_high = NaN

    return trades


# ════════════════════════════════════════════════════════════════
#  Engine runner
# ════════════════════════════════════════════════════════════════

@dataclass
class EngTrade:
    """Wrapper for engine Trade with standalone-compatible interface."""
    symbol: str
    trade: Trade

    @property
    def pnl_rr(self): return self.trade.pnl_rr
    @property
    def entry_date(self): return self.trade.signal.timestamp.date()
    @property
    def entry_time(self): return self.trade.signal.timestamp
    @property
    def exit_reason(self): return self.trade.exit_reason
    @property
    def bars_held(self): return self.trade.bars_held


def run_engine(setup_name: str, cfg_fn, symbols, spy_bars, qqq_bars) -> list:
    """Run engine backtest, filter to specific setup."""
    cfg = cfg_fn()
    all_trades = []
    for sym in symbols:
        bars = load_bars(sym)
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = load_bars(sec_etf) if sec_etf and sec_etf not in {"SPY", "QQQ"} else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars,
                              qqq_bars=qqq_bars, sector_bars=sec_bars)
        for t in result.trades:
            if t.signal.setup_name == setup_name:
                all_trades.append(EngTrade(symbol=sym, trade=t))
    return all_trades


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    symbols = get_universe()
    spy_bars = load_bars("SPY")
    qqq_bars = load_bars("QQQ")
    spy_day_info = classify_spy_days(spy_bars)
    spy_rt = build_spy_realtime_lookup(spy_bars)
    spy_ctx = build_spy_market_snapshots(spy_bars)

    print("=" * 130)
    print("DAY-FILTER DECOMPOSITION — Isolate failure source")
    print("=" * 130)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Data: {spy_bars[0].timestamp.date()} → {spy_bars[-1].timestamp.date()}")

    # Count GREEN days by each filter
    all_dates = sorted(set(b.timestamp.date() for b in spy_bars))
    perfect_green = sum(1 for d in all_dates if spy_day_info.get(d) == "GREEN")
    # For real-time, count dates where at least one bar in 10:00-10:59 shows GREEN
    rt_green_dates = set()
    for (d, hhmm), pct in spy_rt.items():
        if 1000 <= hhmm <= 1059 and pct > 0.05:
            rt_green_dates.add(d)
    print(f"\nDay counts: total={len(all_dates)}, "
          f"perfect-foresight GREEN={perfect_green}, "
          f"real-time GREEN (any bar in 10:00-10:59)={len(rt_green_dates)}")
    # Days that are perfect-GREEN but never look GREEN in real-time
    perfect_only = sum(1 for d in all_dates
                       if spy_day_info.get(d) == "GREEN" and d not in rt_green_dates)
    # Days that look GREEN in real-time but aren't perfect-GREEN
    rt_only = sum(1 for d in rt_green_dates if spy_day_info.get(d) != "GREEN")
    print(f"  Perfect-GREEN but never RT-GREEN in window: {perfect_only}")
    print(f"  RT-GREEN in window but NOT perfect-GREEN:   {rt_only}")

    # ── Run 3×2 matrix ──
    from .portfolio_configs import candidate_v2_vrgreen_bdr, vka_only_bdr

    results = {}

    # Row 1: Standalone + perfect-foresight
    print("\n  Running: standalone VR + perfect-foresight...")
    vr_perfect = []
    for sym in symbols:
        bars = load_bars(sym)
        if bars:
            vr_perfect.extend(standalone_vr(bars, sym, spy_day_info, spy_rt, spy_ctx, "perfect"))
    results["VR_standalone_perfect"] = vr_perfect

    print("  Running: standalone VKA + perfect-foresight...")
    vka_perfect = []
    for sym in symbols:
        bars = load_bars(sym)
        if bars:
            vka_perfect.extend(standalone_vka(bars, sym, spy_day_info, spy_rt, spy_ctx, "perfect"))
    results["VKA_standalone_perfect"] = vka_perfect

    # Row 2: Standalone + real-time proxy
    print("  Running: standalone VR + real-time proxy...")
    vr_rt = []
    for sym in symbols:
        bars = load_bars(sym)
        if bars:
            vr_rt.extend(standalone_vr(bars, sym, spy_day_info, spy_rt, spy_ctx, "realtime"))
    results["VR_standalone_realtime"] = vr_rt

    print("  Running: standalone VKA + real-time proxy...")
    vka_rt = []
    for sym in symbols:
        bars = load_bars(sym)
        if bars:
            vka_rt.extend(standalone_vka(bars, sym, spy_day_info, spy_rt, spy_ctx, "realtime"))
    results["VKA_standalone_realtime"] = vka_rt

    # Row 3: Engine + real-time
    print("  Running: engine VR + real-time...")
    vr_engine = run_engine("VWAP RECLAIM", candidate_v2_vrgreen_bdr, symbols, spy_bars, qqq_bars)
    results["VR_engine_realtime"] = vr_engine

    print("  Running: engine VKA + real-time...")
    vka_engine = run_engine("VK ACCEPT", vka_only_bdr, symbols, spy_bars, qqq_bars)
    results["VKA_engine_realtime"] = vka_engine

    # ── Results table ──
    print(f"\n{'=' * 130}")
    print("DECOMPOSITION MATRIX")
    print(f"{'=' * 130}")

    header = (f"  {'Cell':36s} {'N':>5s} {'PF(R)':>6s} {'Exp(R)':>8s} {'TotalR':>8s} "
              f"{'MaxDD':>7s} {'Stop%':>6s} {'QStop%':>6s} {'Tgt%':>5s} {'TrnPF':>6s} {'TstPF':>6s}")
    divider = (f"  {'-'*36} {'-'*5} {'-'*6} {'-'*8} {'-'*8} "
               f"{'-'*7} {'-'*6} {'-'*6} {'-'*5} {'-'*6} {'-'*6}")

    for setup_label, prefix in [("VR (VWAP Reclaim, hold=3, tgt=3.0R)", "VR"),
                                  ("VKA (VWAP Kiss Accept, hold=2, tgt=2.0R)", "VKA")]:
        print(f"\n  ── {setup_label} ──")
        print(header)
        print(divider)

        for row_label, key in [
            ("1. Standalone + Perfect GREEN", f"{prefix}_standalone_perfect"),
            ("2. Standalone + Real-time SPY", f"{prefix}_standalone_realtime"),
            ("3. Engine + Real-time SPY",     f"{prefix}_engine_realtime"),
        ]:
            trades = results[key]
            m = compute_r_metrics(trades)
            print(f"  {row_label:36s} {m['n']:5d} {pf_str(m['pf_r']):>6s} {m['exp_r']:+7.3f}R "
                  f"{m['total_r']:+7.2f}R {m['max_dd_r']:6.2f}R {m['stop_rate']:5.1f}% "
                  f"{m['quick_stop']:5.1f}% {m['target_rate']:4.1f}% "
                  f"{pf_str(m.get('train_pf',0)):>6s} {pf_str(m.get('test_pf',0)):>6s}")

    # ── Delta analysis ──
    print(f"\n{'=' * 130}")
    print("DELTA ANALYSIS — Where does the collapse happen?")
    print(f"{'=' * 130}")

    for prefix, label in [("VR", "VR"), ("VKA", "VKA")]:
        m1 = compute_r_metrics(results[f"{prefix}_standalone_perfect"])
        m2 = compute_r_metrics(results[f"{prefix}_standalone_realtime"])
        m3 = compute_r_metrics(results[f"{prefix}_engine_realtime"])

        delta_12_n = m2["n"] - m1["n"]
        delta_12_r = m2["total_r"] - m1["total_r"]
        delta_23_n = m3["n"] - m2["n"]
        delta_23_r = m3["total_r"] - m2["total_r"]

        print(f"\n  {label}:")
        print(f"    Step 1→2 (perfect → real-time, same standalone):")
        print(f"      ΔN = {delta_12_n:+d}   ΔR = {delta_12_r:+.2f}R   "
              f"ΔPF = {m2['pf_r'] - m1['pf_r']:+.2f}")
        print(f"    Step 2→3 (standalone → engine, same real-time filter):")
        print(f"      ΔN = {delta_23_n:+d}   ΔR = {delta_23_r:+.2f}R   "
              f"ΔPF = {m3['pf_r'] - m2['pf_r']:+.2f}")

        total_collapse = m3["total_r"] - m1["total_r"]
        if total_collapse != 0:
            pct_from_filter = delta_12_r / total_collapse * 100
            pct_from_engine = delta_23_r / total_collapse * 100
        else:
            pct_from_filter = 0
            pct_from_engine = 0

        print(f"    Total collapse: {total_collapse:+.2f}R")
        print(f"      From day-filter substitution: {delta_12_r:+.2f}R ({pct_from_filter:.0f}%)")
        print(f"      From engine integration drift: {delta_23_r:+.2f}R ({pct_from_engine:.0f}%)")

    # ── Characterize the extra trades ──
    print(f"\n{'=' * 130}")
    print("EXTRA TRADES ANALYSIS — What does the real-time filter let through?")
    print(f"{'=' * 130}")

    for prefix, label in [("VR", "VR"), ("VKA", "VKA")]:
        perfect_trades = results[f"{prefix}_standalone_perfect"]
        rt_trades = results[f"{prefix}_standalone_realtime"]

        # Identify trades unique to real-time (not in perfect)
        perfect_keys = set((t.symbol, t.entry_time) for t in perfect_trades)
        extra_trades = [t for t in rt_trades if (t.symbol, t.entry_time) not in perfect_keys]
        shared_trades = [t for t in rt_trades if (t.symbol, t.entry_time) in perfect_keys]

        extra_m = compute_r_metrics(extra_trades)
        shared_m = compute_r_metrics(shared_trades)

        print(f"\n  {label}:")
        print(f"    Shared trades (in both perfect & real-time): N={shared_m['n']}  "
              f"PF={pf_str(shared_m['pf_r'])}  Exp={shared_m['exp_r']:+.3f}R  "
              f"TotalR={shared_m['total_r']:+.2f}R")
        print(f"    Extra trades (real-time only, NOT in perfect): N={extra_m['n']}  "
              f"PF={pf_str(extra_m['pf_r'])}  Exp={extra_m['exp_r']:+.3f}R  "
              f"TotalR={extra_m['total_r']:+.2f}R")

        if extra_trades:
            # What days are these on?
            extra_days = defaultdict(float)
            for t in extra_trades:
                extra_days[spy_day_info.get(t.entry_date, "UNK")] += t.pnl_rr
            print(f"    Extra trades by actual day type:")
            for dtype in ["GREEN", "RED", "FLAT", "UNK"]:
                day_trades = [t for t in extra_trades if spy_day_info.get(t.entry_date, "UNK") == dtype]
                if day_trades:
                    dm = compute_r_metrics(day_trades)
                    print(f"      {dtype:5s}: N={dm['n']:4d}  PF={pf_str(dm['pf_r'])}  "
                          f"TotalR={dm['total_r']:+.2f}R")

        # Also check: trades in perfect but NOT in real-time (missed)
        rt_keys = set((t.symbol, t.entry_time) for t in rt_trades)
        missed_trades = [t for t in perfect_trades if (t.symbol, t.entry_time) not in rt_keys]
        missed_m = compute_r_metrics(missed_trades)
        print(f"    Missed trades (in perfect but NOT real-time): N={missed_m['n']}  "
              f"PF={pf_str(missed_m['pf_r'])}  TotalR={missed_m['total_r']:+.2f}R")

    # ── Verdict ──
    print(f"\n{'=' * 130}")
    print("VERDICT")
    print(f"{'=' * 130}")

    vr_m1 = compute_r_metrics(results["VR_standalone_perfect"])
    vr_m2 = compute_r_metrics(results["VR_standalone_realtime"])
    vr_m3 = compute_r_metrics(results["VR_engine_realtime"])
    vka_m1 = compute_r_metrics(results["VKA_standalone_perfect"])
    vka_m2 = compute_r_metrics(results["VKA_standalone_realtime"])
    vka_m3 = compute_r_metrics(results["VKA_engine_realtime"])

    # Determine primary failure source
    for prefix, label, m1, m2, m3 in [
        ("VR", "VR", vr_m1, vr_m2, vr_m3),
        ("VKA", "VKA", vka_m1, vka_m2, vka_m3),
    ]:
        total_collapse = m1["total_r"] - m3["total_r"]
        filter_collapse = m1["total_r"] - m2["total_r"]
        engine_drift = m2["total_r"] - m3["total_r"]

        print(f"\n  {label}:")
        if total_collapse <= 0:
            print(f"    No collapse detected (engine result >= perfect-foresight)")
        elif abs(filter_collapse) > abs(engine_drift) * 2:
            print(f"    PRIMARY CAUSE: Day-filter substitution ({filter_collapse:+.2f}R)")
            print(f"    Secondary: Engine drift ({engine_drift:+.2f}R)")
        elif abs(engine_drift) > abs(filter_collapse) * 2:
            print(f"    PRIMARY CAUSE: Engine integration drift ({engine_drift:+.2f}R)")
            print(f"    Secondary: Day-filter ({filter_collapse:+.2f}R)")
        else:
            print(f"    BOTH factors contribute: filter={filter_collapse:+.2f}R  "
                  f"engine={engine_drift:+.2f}R")

    print(f"\n{'=' * 130}")


if __name__ == "__main__":
    main()
