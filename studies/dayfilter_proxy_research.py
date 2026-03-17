"""
Day-Filter Proxy Research — Part 1 & Part 2

Part 1: Test theory-driven real-time market/day proxies using VR and VKA as probes.
Part 2: Engine drift triage for best proxies.

Proxy candidates:
  A. SPY pct_from_open > threshold  (current=0.05%, try 0.00%, 0.03%, 0.10%, 0.15%)
  B. SPY above VWAP
  C. SPY above VWAP AND pct_from_open > 0
  D. SPY 3-bar momentum positive (close > close[3])
  E. SPY EMA9 > EMA20
  F. Delayed start: skip first N bars (10:15, 10:30 start)
  G. Combo: SPY above VWAP + EMA9 rising + pct > 0
  H. Combo: SPY above VWAP + pct > 0 + delayed (10:15+)

All use only live-available information.
"""

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..backtest import run_backtest, load_bars_from_csv, Trade
from ..config import OverlayConfig
from ..indicators import EMA, VWAPCalc
from ..market_context import MarketEngine, MarketSnapshot, get_sector_etf, SECTOR_MAP
from ..models import Bar, NaN, SetupId

DATA_DIR = Path(__file__).parent.parent / "data"
_isnan = math.isnan


# ════════════════════════════════════════════════════════════════
#  Shared infrastructure (reused from decomposition)
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


# ════════════════════════════════════════════════════════════════
#  SPY context builder — full bar-by-bar state
# ════════════════════════════════════════════════════════════════

@dataclass
class SpyBarState:
    """Full real-time SPY state at a specific bar."""
    pct_from_open: float = 0.0
    above_vwap: bool = False
    ema9: float = NaN
    ema20: float = NaN
    ema9_above_ema20: bool = False
    ema9_rising: bool = False
    close: float = 0.0
    close_3ago: float = NaN  # for momentum check
    vwap: float = NaN


def build_spy_state_lookup(spy_bars: list) -> Dict[Tuple[date, int], SpyBarState]:
    """Build comprehensive per-bar SPY state for all proxy evaluations."""
    lookup = {}
    ema9 = EMA(9)
    ema20 = EMA(20)
    vwap = VWAPCalc()
    day_open = {}
    prev_date = None
    close_history = deque(maxlen=5)
    prev_e9 = NaN

    for b in spy_bars:
        d = b.timestamp.date()
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute

        if d != prev_date:
            vwap.reset()
            close_history.clear()
            day_open[d] = b.open
            prev_date = d

        e9 = ema9.update(b.close)
        e20 = ema20.update(b.close)
        tp = (b.high + b.low + b.close) / 3.0
        vw = vwap.update(tp, b.volume)

        pct = (b.close - day_open[d]) / day_open[d] * 100 if day_open[d] > 0 else 0.0

        close_3ago = close_history[-3] if len(close_history) >= 3 else NaN
        close_history.append(b.close)

        e9_rising = (e9 > prev_e9) if not _isnan(prev_e9) and not _isnan(e9) else False
        prev_e9 = e9

        ready = ema9.ready and ema20.ready

        lookup[(d, hhmm)] = SpyBarState(
            pct_from_open=pct,
            above_vwap=(b.close > vw if vwap.ready else False),
            ema9=e9 if ready else NaN,
            ema20=e20 if ready else NaN,
            ema9_above_ema20=(e9 > e20 if ready else False),
            ema9_rising=e9_rising,
            close=b.close,
            close_3ago=close_3ago,
            vwap=vw if vwap.ready else NaN,
        )

    return lookup


# ════════════════════════════════════════════════════════════════
#  Market alignment (standalone, same as decomposition)
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
#  Proxy definitions
# ════════════════════════════════════════════════════════════════

ProxyFn = Callable[[Dict[Tuple[date, int], SpyBarState], date, int], bool]


def _proxy_perfect(spy_day_info: dict) -> ProxyFn:
    """Perfect-foresight GREEN (baseline)."""
    def fn(spy_state, d, hhmm):
        return spy_day_info.get(d) == "GREEN"
    return fn


def _proxy_none() -> ProxyFn:
    """No filter at all."""
    def fn(spy_state, d, hhmm):
        return True
    return fn


def _proxy_pct_threshold(threshold: float) -> ProxyFn:
    """SPY pct_from_open > threshold."""
    def fn(spy_state, d, hhmm):
        s = spy_state.get((d, hhmm))
        if s is None:
            return True
        return s.pct_from_open > threshold
    return fn


def _proxy_above_vwap() -> ProxyFn:
    """SPY close > VWAP."""
    def fn(spy_state, d, hhmm):
        s = spy_state.get((d, hhmm))
        if s is None:
            return True
        return s.above_vwap
    return fn


def _proxy_above_vwap_and_pct(pct_thresh: float = 0.0) -> ProxyFn:
    """SPY above VWAP AND pct_from_open > threshold."""
    def fn(spy_state, d, hhmm):
        s = spy_state.get((d, hhmm))
        if s is None:
            return True
        return s.above_vwap and s.pct_from_open > pct_thresh
    return fn


def _proxy_momentum_3bar() -> ProxyFn:
    """SPY close > close[3] (3-bar momentum positive)."""
    def fn(spy_state, d, hhmm):
        s = spy_state.get((d, hhmm))
        if s is None:
            return True
        if _isnan(s.close_3ago):
            return True
        return s.close > s.close_3ago
    return fn


def _proxy_ema_structure() -> ProxyFn:
    """SPY EMA9 > EMA20."""
    def fn(spy_state, d, hhmm):
        s = spy_state.get((d, hhmm))
        if s is None:
            return True
        return s.ema9_above_ema20
    return fn


def _proxy_combo_strong() -> ProxyFn:
    """SPY above VWAP + EMA9 rising + pct > 0."""
    def fn(spy_state, d, hhmm):
        s = spy_state.get((d, hhmm))
        if s is None:
            return True
        return s.above_vwap and s.ema9_rising and s.pct_from_open > 0
    return fn


def _proxy_combo_vwap_pct_delayed(min_hhmm: int = 1015) -> ProxyFn:
    """SPY above VWAP + pct > 0 + delayed start."""
    def fn(spy_state, d, hhmm):
        if hhmm < min_hhmm:
            return False  # block trades before delay threshold
        s = spy_state.get((d, hhmm))
        if s is None:
            return True
        return s.above_vwap and s.pct_from_open > 0
    return fn


# ════════════════════════════════════════════════════════════════
#  Trade simulation (same as decomposition)
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
            break
        if trade.direction == 1 and b.low <= trade.stop_price:
            trade.pnl_rr = (trade.stop_price - trade.entry_price) / risk
            trade.exit_reason = "stop"
            return trade
        if trade.direction == 1 and b.high >= trade.target_price:
            trade.pnl_rr = target_rr
            trade.exit_reason = "target"
            return trade

    last_bar = bars[min(bar_idx + max_bars, len(bars) - 1)]
    trade.pnl_rr = (last_bar.close - trade.entry_price) / risk
    trade.exit_reason = "eod"
    return trade


def compute_r_metrics(trades: list) -> dict:
    if not trades:
        return {"n": 0, "pf_r": 0, "exp_r": 0, "total_r": 0, "max_dd_r": 0,
                "stop_rate": 0, "quick_stop": 0, "target_rate": 0,
                "train_pf": 0, "test_pf": 0, "train_n": 0, "test_n": 0,
                "train_total_r": 0, "test_total_r": 0}

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

    train = [t for t in trades if t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date.day % 2 == 0]
    tr_wins = sum(t.pnl_rr for t in train if t.pnl_rr > 0)
    tr_losses = abs(sum(t.pnl_rr for t in train if t.pnl_rr < 0))
    te_wins = sum(t.pnl_rr for t in test if t.pnl_rr > 0)
    te_losses = abs(sum(t.pnl_rr for t in test if t.pnl_rr < 0))

    return {
        "n": len(trades),
        "pf_r": pf_r,
        "exp_r": exp_r,
        "total_r": total_r,
        "max_dd_r": max_dd,
        "stop_rate": stops / len(trades) * 100,
        "quick_stop": qstops / len(trades) * 100,
        "target_rate": targets / len(trades) * 100,
        "train_pf": tr_wins / tr_losses if tr_losses > 0 else float('inf'),
        "test_pf": te_wins / te_losses if te_losses > 0 else float('inf'),
        "train_n": len(train),
        "test_n": len(test),
        "train_total_r": sum(t.pnl_rr for t in train),
        "test_total_r": sum(t.pnl_rr for t in test),
    }


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


# ════════════════════════════════════════════════════════════════
#  Standalone VR with pluggable proxy
# ════════════════════════════════════════════════════════════════

def standalone_vr_proxy(bars: list, sym: str, spy_state: dict, spy_ctx: dict,
                        proxy_fn: ProxyFn, time_start: int = 1000,
                        time_end: int = 1059) -> List[ATrade]:
    """VR standalone with pluggable day-filter proxy."""
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

    for i, bar in enumerate(bars):
        ema9.update(bar.close)
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

        # State machine (always runs)
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

        # Apply proxy filter
        if not proxy_fn(spy_state, d, hhmm):
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
#  Standalone VKA with pluggable proxy
# ════════════════════════════════════════════════════════════════

def standalone_vka_proxy(bars: list, sym: str, spy_state: dict, spy_ctx: dict,
                         proxy_fn: ProxyFn, time_start: int = 1000,
                         time_end: int = 1059) -> List[ATrade]:
    """VKA standalone with pluggable day-filter proxy."""
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
    kiss_atr_frac = 0.05

    for i, bar in enumerate(bars):
        ema9.update(bar.close)
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

        # Apply proxy filter
        if not proxy_fn(spy_state, d, hhmm):
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
#  Engine runner (from decomposition)
# ════════════════════════════════════════════════════════════════

@dataclass
class EngTrade:
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
#  Part 2: Engine drift triage
# ════════════════════════════════════════════════════════════════

def triage_engine_drift(setup_name: str, cfg_fn, symbols, spy_bars, qqq_bars,
                        standalone_trades: list) -> dict:
    """
    Compare standalone trades vs engine trades to characterize drift.
    Returns dict with categorized suppressed/extra trades.
    """
    engine_trades = run_engine(setup_name, cfg_fn, symbols, spy_bars, qqq_bars)

    sa_keys = {(t.symbol, t.entry_date, t.entry_time.hour * 100 + t.entry_time.minute)
               for t in standalone_trades}
    eng_keys = {(t.symbol, t.entry_date, t.entry_time.hour * 100 + t.entry_time.minute)
                for t in engine_trades}

    suppressed_keys = sa_keys - eng_keys
    extra_keys = eng_keys - sa_keys
    shared_keys = sa_keys & eng_keys

    suppressed = [t for t in standalone_trades
                  if (t.symbol, t.entry_date, t.entry_time.hour * 100 + t.entry_time.minute)
                  in suppressed_keys]
    shared_sa = [t for t in standalone_trades
                 if (t.symbol, t.entry_date, t.entry_time.hour * 100 + t.entry_time.minute)
                 in shared_keys]
    shared_eng = [t for t in engine_trades
                  if (t.symbol, t.entry_date, t.entry_time.hour * 100 + t.entry_time.minute)
                  in shared_keys]
    extra = [t for t in engine_trades
             if (t.symbol, t.entry_date, t.entry_time.hour * 100 + t.entry_time.minute)
             in extra_keys]

    return {
        "engine_total": engine_trades,
        "suppressed": suppressed,
        "extra": extra,
        "shared_sa": shared_sa,
        "shared_eng": shared_eng,
    }


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    symbols = get_universe()
    spy_bars = load_bars("SPY")
    qqq_bars = load_bars("QQQ")
    spy_day_info = classify_spy_days(spy_bars)
    spy_state = build_spy_state_lookup(spy_bars)
    spy_ctx = build_spy_market_snapshots(spy_bars)

    print("=" * 140)
    print("DAY-FILTER PROXY RESEARCH — Part 1: Real-time proxy candidates")
    print("=" * 140)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Data: {spy_bars[0].timestamp.date()} → {spy_bars[-1].timestamp.date()}")

    # ── Define proxy matrix ──
    proxies = [
        ("0_PERFECT",         "Perfect-foresight GREEN (baseline)",   _proxy_perfect(spy_day_info)),
        ("1_CURRENT_RT",      "SPY pct > 0.05% (current proxy)",     _proxy_pct_threshold(0.05)),
        ("2_NO_FILTER",       "No filter (all days)",                 _proxy_none()),
        # Threshold variants
        ("A1_PCT_000",        "SPY pct > 0.00%",                     _proxy_pct_threshold(0.00)),
        ("A2_PCT_003",        "SPY pct > 0.03%",                     _proxy_pct_threshold(0.03)),
        ("A3_PCT_010",        "SPY pct > 0.10%",                     _proxy_pct_threshold(0.10)),
        ("A4_PCT_015",        "SPY pct > 0.15%",                     _proxy_pct_threshold(0.15)),
        # Structural
        ("B_ABOVE_VWAP",      "SPY above VWAP",                      _proxy_above_vwap()),
        ("C_VWAP_AND_PCT",    "SPY above VWAP + pct > 0",            _proxy_above_vwap_and_pct(0.0)),
        ("D_MOMENTUM_3BAR",   "SPY 3-bar momentum positive",         _proxy_momentum_3bar()),
        ("E_EMA_STRUCTURE",   "SPY EMA9 > EMA20",                    _proxy_ema_structure()),
        # Combos
        ("F_COMBO_STRONG",    "SPY abvVWAP + EMA9 rising + pct>0",   _proxy_combo_strong()),
        ("G_COMBO_DELAYED15", "SPY abvVWAP + pct>0 + delay 10:15",   _proxy_combo_vwap_pct_delayed(1015)),
        ("H_COMBO_DELAYED30", "SPY abvVWAP + pct>0 + delay 10:30",   _proxy_combo_vwap_pct_delayed(1030)),
    ]

    # ── Run Part 1 matrix ──
    all_results = {}  # (proxy_id, setup) → trades

    for setup_name, standalone_fn, target_rr_label in [
        ("VR",  standalone_vr_proxy,  "3.0R"),
        ("VKA", standalone_vka_proxy, "2.0R"),
    ]:
        print(f"\n{'─' * 100}")
        print(f"  Setup: {setup_name} (target {target_rr_label})")
        print(f"{'─' * 100}")

        for proxy_id, proxy_label, proxy_fn in proxies:
            trades = []
            for sym in symbols:
                bars = load_bars(sym)
                if bars:
                    trades.extend(standalone_fn(bars, sym, spy_state, spy_ctx, proxy_fn))
            all_results[(proxy_id, setup_name)] = trades

        # Print results table
        header = (f"  {'Proxy':36s} {'N':>5s} {'PF(R)':>6s} {'Exp(R)':>8s} "
                  f"{'TotalR':>9s} {'MaxDD':>7s} {'Tgt%':>5s} "
                  f"{'TrnPF':>6s} {'TstPF':>6s} {'TrnR':>8s} {'TstR':>8s}")
        print(f"\n{header}")
        print(f"  {'-'*36} {'-'*5} {'-'*6} {'-'*8} {'-'*9} {'-'*7} {'-'*5} {'-'*6} {'-'*6} {'-'*8} {'-'*8}")

        # Reference metrics for comparison
        perfect_m = compute_r_metrics(all_results[("0_PERFECT", setup_name)])

        for proxy_id, proxy_label, _ in proxies:
            trades = all_results[(proxy_id, setup_name)]
            m = compute_r_metrics(trades)
            # Mark if this proxy recovers more R than current RT
            marker = ""
            current_m = compute_r_metrics(all_results[("1_CURRENT_RT", setup_name)])
            if proxy_id not in ("0_PERFECT", "1_CURRENT_RT", "2_NO_FILTER"):
                if m["total_r"] > current_m["total_r"] * 1.1:
                    marker = " ★"
                elif m["total_r"] > current_m["total_r"]:
                    marker = " +"

            print(f"  {proxy_label:36s} {m['n']:5d} {pf_str(m['pf_r']):>6s} "
                  f"{m['exp_r']:+7.3f}R {m['total_r']:+8.2f}R {m['max_dd_r']:6.2f}R "
                  f"{m['target_rate']:4.1f}% {pf_str(m['train_pf']):>6s} "
                  f"{pf_str(m['test_pf']):>6s} {m.get('train_total_r', 0):+7.2f}R "
                  f"{m.get('test_total_r', 0):+7.2f}R{marker}")

    # ── Recovery analysis ──
    print(f"\n{'=' * 140}")
    print("RECOVERY ANALYSIS — How much edge does each proxy recover vs perfect-GREEN?")
    print(f"{'=' * 140}")

    for setup_name in ["VR", "VKA"]:
        perfect_m = compute_r_metrics(all_results[("0_PERFECT", setup_name)])
        current_m = compute_r_metrics(all_results[("1_CURRENT_RT", setup_name)])
        gap = perfect_m["total_r"] - current_m["total_r"]

        print(f"\n  {setup_name}: Perfect={perfect_m['total_r']:+.2f}R  "
              f"Current RT={current_m['total_r']:+.2f}R  Gap={gap:.2f}R")

        ranked = []
        for proxy_id, proxy_label, _ in proxies:
            if proxy_id in ("0_PERFECT", "1_CURRENT_RT", "2_NO_FILTER"):
                continue
            m = compute_r_metrics(all_results[(proxy_id, setup_name)])
            recovery = m["total_r"] - current_m["total_r"]
            pct_gap = (recovery / gap * 100) if gap > 0 else 0
            ranked.append((proxy_id, proxy_label, m, recovery, pct_gap))

        ranked.sort(key=lambda x: -x[3])

        print(f"  {'Proxy':36s} {'TotalR':>9s} {'Recovery':>9s} {'%Gap':>6s} {'PF(R)':>6s} {'Stable':>7s}")
        print(f"  {'-'*36} {'-'*9} {'-'*9} {'-'*6} {'-'*6} {'-'*7}")

        for proxy_id, proxy_label, m, recovery, pct_gap in ranked:
            # "Stable" = both train and test PF > 1.0
            stable = "YES" if m["train_pf"] > 1.0 and m["test_pf"] > 1.0 else "no"
            print(f"  {proxy_label:36s} {m['total_r']:+8.2f}R {recovery:+8.2f}R "
                  f"{pct_gap:5.1f}% {pf_str(m['pf_r']):>6s} {stable:>7s}")

    # ── Poison trade analysis for top proxies ──
    print(f"\n{'=' * 140}")
    print("POISON TRADE ANALYSIS — Do the best proxies reduce RED-day trades?")
    print(f"{'=' * 140}")

    for setup_name in ["VR", "VKA"]:
        print(f"\n  {setup_name}:")

        # Top 3 proxies by total_r
        current_m = compute_r_metrics(all_results[("1_CURRENT_RT", setup_name)])
        candidates = []
        for proxy_id, proxy_label, _ in proxies:
            if proxy_id in ("0_PERFECT", "1_CURRENT_RT", "2_NO_FILTER"):
                continue
            m = compute_r_metrics(all_results[(proxy_id, setup_name)])
            candidates.append((proxy_id, proxy_label, m))
        candidates.sort(key=lambda x: -x[2]["total_r"])

        for proxy_id, proxy_label, _ in [("1_CURRENT_RT", "Current RT (pct>0.05%)", None)] + candidates[:3]:
            trades = all_results[(proxy_id, setup_name)]
            red_trades = [t for t in trades if spy_day_info.get(t.entry_date) == "RED"]
            flat_trades = [t for t in trades if spy_day_info.get(t.entry_date) == "FLAT"]
            green_trades = [t for t in trades if spy_day_info.get(t.entry_date) == "GREEN"]

            red_m = compute_r_metrics(red_trades)
            flat_m = compute_r_metrics(flat_trades)
            green_m = compute_r_metrics(green_trades)

            print(f"    {proxy_label}:")
            print(f"      GREEN days: N={green_m['n']:4d}  PF={pf_str(green_m['pf_r'])}  TotalR={green_m['total_r']:+.2f}R")
            print(f"      RED days:   N={red_m['n']:4d}  PF={pf_str(red_m['pf_r'])}  TotalR={red_m['total_r']:+.2f}R")
            print(f"      FLAT days:  N={flat_m['n']:4d}  PF={pf_str(flat_m['pf_r'])}  TotalR={flat_m['total_r']:+.2f}R")

    # ════════════════════════════════════════════════════════════════
    #  Part 2: Engine drift triage on best 2 proxies
    # ════════════════════════════════════════════════════════════════

    print(f"\n{'=' * 140}")
    print("PART 2: ENGINE DRIFT TRIAGE")
    print("=" * 140)

    from .portfolio_configs import candidate_v2_vrgreen_bdr, vka_only_bdr

    # Find best proxy for each setup
    for setup_name, engine_setup_name, cfg_fn in [
        ("VR",  "VWAP RECLAIM", candidate_v2_vrgreen_bdr),
        ("VKA", "VK ACCEPT",    vka_only_bdr),
    ]:
        # Pick the best proxy (highest total_r excluding baselines)
        best_proxy_id = None
        best_total_r = -9999
        for proxy_id, proxy_label, _ in proxies:
            if proxy_id in ("0_PERFECT", "1_CURRENT_RT", "2_NO_FILTER"):
                continue
            m = compute_r_metrics(all_results[(proxy_id, setup_name)])
            if m["total_r"] > best_total_r:
                best_total_r = m["total_r"]
                best_proxy_id = proxy_id

        if best_proxy_id is None:
            continue

        best_proxy_label = [p[1] for p in proxies if p[0] == best_proxy_id][0]
        best_trades = all_results[(best_proxy_id, setup_name)]

        print(f"\n{'─' * 100}")
        print(f"  {setup_name} — Best proxy: {best_proxy_label}")
        print(f"  Standalone with best proxy: {compute_r_metrics(best_trades)['n']} trades, "
              f"PF={pf_str(compute_r_metrics(best_trades)['pf_r'])}, "
              f"TotalR={compute_r_metrics(best_trades)['total_r']:+.2f}R")
        print(f"{'─' * 100}")

        # Run engine drift triage
        drift = triage_engine_drift(engine_setup_name, cfg_fn, symbols, spy_bars, qqq_bars,
                                     best_trades)

        eng_m = compute_r_metrics(drift["engine_total"])
        sup_m = compute_r_metrics(drift["suppressed"])
        ext_m = compute_r_metrics(drift["extra"])
        sh_sa_m = compute_r_metrics(drift["shared_sa"])
        sh_eng_m = compute_r_metrics(drift["shared_eng"])

        print(f"\n  Engine total:       N={eng_m['n']:4d}  PF={pf_str(eng_m['pf_r'])}  TotalR={eng_m['total_r']:+.2f}R")
        print(f"  Shared (standalone): N={sh_sa_m['n']:4d}  PF={pf_str(sh_sa_m['pf_r'])}  TotalR={sh_sa_m['total_r']:+.2f}R")
        print(f"  Shared (engine):     N={sh_eng_m['n']:4d}  PF={pf_str(sh_eng_m['pf_r'])}  TotalR={sh_eng_m['total_r']:+.2f}R")
        print(f"  Suppressed by engine: N={sup_m['n']:4d}  PF={pf_str(sup_m['pf_r'])}  TotalR={sup_m['total_r']:+.2f}R")
        print(f"  Extra from engine:    N={ext_m['n']:4d}  PF={pf_str(ext_m['pf_r'])}  TotalR={ext_m['total_r']:+.2f}R")

        if drift["suppressed"]:
            print(f"\n  SUPPRESSED TRADES — Are they good or bad?")
            print(f"    If PF > 1.0, engine is hurting by suppressing them")
            print(f"    If PF < 1.0, engine is helping by suppressing them")
            print(f"    → Suppressed PF = {pf_str(sup_m['pf_r'])}  →  "
                  f"{'Engine is HURTING (suppressing good trades)' if sup_m['pf_r'] > 1.0 else 'Engine is HELPING (suppressing bad trades)'}")

            # Time distribution of suppressed trades
            time_buckets = defaultdict(list)
            for t in drift["suppressed"]:
                hh = t.entry_time.hour
                time_buckets[hh].append(t)

            print(f"\n  Suppressed by hour:")
            for hh in sorted(time_buckets.keys()):
                bucket = time_buckets[hh]
                bm = compute_r_metrics(bucket)
                print(f"    {hh:02d}:xx  N={bm['n']:3d}  PF={pf_str(bm['pf_r'])}  TotalR={bm['total_r']:+.2f}R")

    # ════════════════════════════════════════════════════════════════
    #  Final verdict
    # ════════════════════════════════════════════════════════════════

    print(f"\n{'=' * 140}")
    print("FINAL VERDICT & RECOMMENDATIONS")
    print("=" * 140)

    # Rank proxies across both setups combined
    combined_rank = []
    for proxy_id, proxy_label, _ in proxies:
        if proxy_id in ("0_PERFECT", "1_CURRENT_RT", "2_NO_FILTER"):
            continue
        vr_m = compute_r_metrics(all_results[(proxy_id, "VR")])
        vka_m = compute_r_metrics(all_results[(proxy_id, "VKA")])
        combined_r = vr_m["total_r"] + vka_m["total_r"]
        avg_pf = (vr_m["pf_r"] + vka_m["pf_r"]) / 2
        # Stability: both train+test positive for both setups
        stable_count = sum([
            vr_m["train_pf"] > 1.0, vr_m["test_pf"] > 1.0,
            vka_m["train_pf"] > 1.0, vka_m["test_pf"] > 1.0,
        ])
        combined_rank.append((proxy_id, proxy_label, combined_r, avg_pf, stable_count,
                              vr_m, vka_m))

    combined_rank.sort(key=lambda x: -x[2])

    print(f"\n  RANKED PROXIES (combined VR + VKA total R):")
    print(f"  {'Rank':>4s}  {'Proxy':36s} {'CombR':>9s} {'AvgPF':>6s} {'Stable':>7s}  {'VR_R':>8s} {'VKA_R':>8s}")
    print(f"  {'-'*4}  {'-'*36} {'-'*9} {'-'*6} {'-'*7}  {'-'*8} {'-'*8}")

    for rank, (proxy_id, proxy_label, combined_r, avg_pf, stable, vr_m, vka_m) in enumerate(combined_rank, 1):
        print(f"  {rank:4d}  {proxy_label:36s} {combined_r:+8.2f}R {avg_pf:5.2f} {stable:5d}/4  "
              f"{vr_m['total_r']:+7.2f}R {vka_m['total_r']:+7.2f}R")

    # Baselines for comparison
    print(f"\n  BASELINES:")
    for proxy_id, proxy_label in [("0_PERFECT", "Perfect-foresight GREEN"),
                                    ("1_CURRENT_RT", "Current RT (pct>0.05%)"),
                                    ("2_NO_FILTER", "No filter")]:
        vr_m = compute_r_metrics(all_results[(proxy_id, "VR")])
        vka_m = compute_r_metrics(all_results[(proxy_id, "VKA")])
        combined_r = vr_m["total_r"] + vka_m["total_r"]
        print(f"        {proxy_label:36s} {combined_r:+8.2f}R  "
              f"VR={vr_m['total_r']:+7.2f}R  VKA={vka_m['total_r']:+7.2f}R")

    # Recovery summary
    perf_vr = compute_r_metrics(all_results[("0_PERFECT", "VR")])
    perf_vka = compute_r_metrics(all_results[("0_PERFECT", "VKA")])
    curr_vr = compute_r_metrics(all_results[("1_CURRENT_RT", "VR")])
    curr_vka = compute_r_metrics(all_results[("1_CURRENT_RT", "VKA")])
    gap_total = (perf_vr["total_r"] + perf_vka["total_r"]) - (curr_vr["total_r"] + curr_vka["total_r"])

    if combined_rank:
        best = combined_rank[0]
        best_recovery = best[2] - (curr_vr["total_r"] + curr_vka["total_r"])
        best_pct = best_recovery / gap_total * 100 if gap_total > 0 else 0
        print(f"\n  BEST PROXY: {best[1]}")
        print(f"    Recovers {best_recovery:+.2f}R of {gap_total:.2f}R gap ({best_pct:.0f}%)")
        print(f"    Combined PF: {best[3]:.2f}")
        print(f"    Stability: {best[4]}/4 train+test splits positive")

    print(f"\n{'=' * 140}")


if __name__ == "__main__":
    main()
