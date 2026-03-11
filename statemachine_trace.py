"""
State Machine Trace — Find exact divergence between standalone and engine VR.

Pick a symbol + date where standalone fires but engine doesn't (even with all gates off).
Trace VR state bar-by-bar in both paths.
"""

import math
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from .backtest import run_backtest, load_bars_from_csv
from .config import OverlayConfig
from .indicators import EMA, VWAPCalc
from .market_context import MarketEngine, get_sector_etf, SECTOR_MAP
from .models import Bar, NaN, SetupId
from .engine import SignalEngine
from .market_context import compute_market_context

DATA_DIR = Path(__file__).parent / "data"
_isnan = math.isnan


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


def standalone_vr_entries(bars: list, sym: str) -> list:
    """Return (date, hhmm, bar_idx) for every standalone VR trigger."""
    entries = []
    ema9 = EMA(9)
    vwap = VWAPCalc()
    vol_buf = deque(maxlen=20)
    vol_ma = NaN
    was_below = False
    hold_count = 0
    hold_low = NaN
    triggered_today = None
    prev_date = None

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

        if above_vwap and not was_below:
            pass
        elif not above_vwap:
            was_below = True
            hold_count = 0
            hold_low = NaN
        elif was_below and above_vwap and hold_count == 0:
            hold_count = 1
            hold_low = bar.low
        elif hold_count > 0 and hold_count < 3:
            if above_vwap:
                hold_count += 1
                hold_low = min(hold_low, bar.low) if not _isnan(hold_low) else bar.low
            else:
                was_below = True
                hold_count = 0
                hold_low = NaN

        if hhmm < 1000 or hhmm > 1059:
            continue
        if triggered_today == d:
            continue

        if hold_count >= 3 and above_vwap:
            rng = bar.high - bar.low
            body = abs(bar.close - bar.open)
            is_bull = bar.close > bar.open
            body_pct = body / rng if rng > 0 else 0
            candle_ok = is_bull and body_pct >= 0.40
            vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 0.70 * vol_ma

            if candle_ok and vol_ok:
                entries.append((d, hhmm, i))
                triggered_today = d
                was_below = False
                hold_count = 0
                hold_low = NaN

    return entries


def engine_vr_entries(sym: str, bars: list, spy_bars: list, qqq_bars: list, cfg: OverlayConfig) -> list:
    """Return (date, hhmm) for every engine VR signal."""
    sec_etf = get_sector_etf(sym)
    sec_bars = load_bars(sec_etf) if sec_etf and sec_etf not in {"SPY", "QQQ"} else None
    result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars,
                          qqq_bars=qqq_bars, sector_bars=sec_bars)
    entries = []
    for t in result.trades:
        if t.signal.setup_name == "VWAP RECLAIM":
            ts = t.signal.timestamp
            entries.append((ts.date(), ts.hour * 100 + ts.minute))
    # Also collect signals that didn't become trades (from the engine run)
    # Actually we need raw signals. Let's run the engine directly.
    engine = SignalEngine(cfg)
    spy_engine = MarketEngine()
    qqq_engine = MarketEngine()
    sec_engine = MarketEngine() if sec_bars else None

    def _build_ts_map(market_bars):
        return {mb.timestamp: idx for idx, mb in enumerate(market_bars)}

    spy_ts_map = _build_ts_map(spy_bars) if spy_bars else {}
    qqq_ts_map = _build_ts_map(qqq_bars) if qqq_bars else {}
    sec_ts_map = _build_ts_map(sec_bars) if sec_bars else {}

    spy_idx = qqq_idx = sec_idx = 0
    spy_snap = qqq_snap = sec_snap = None

    signal_entries = []

    for i, bar in enumerate(bars):
        ts = bar.timestamp
        if ts in spy_ts_map and spy_ts_map[ts] >= spy_idx:
            while spy_idx <= spy_ts_map[ts]:
                spy_snap = spy_engine.process_bar(spy_bars[spy_idx])
                spy_idx += 1
        if ts in qqq_ts_map and qqq_ts_map[ts] >= qqq_idx:
            while qqq_idx <= qqq_ts_map[ts]:
                qqq_snap = qqq_engine.process_bar(qqq_bars[qqq_idx])
                qqq_idx += 1
        if sec_bars and ts in sec_ts_map and sec_ts_map[ts] >= sec_idx:
            while sec_idx <= sec_ts_map[ts]:
                sec_snap = sec_engine.process_bar(sec_bars[sec_idx])
                sec_idx += 1

        market_ctx = None
        if cfg.use_market_context and spy_snap and qqq_snap:
            date_int = ts.year * 10000 + ts.month * 100 + ts.day
            if not hasattr(engine, '_rs_date') or engine._rs_date != date_int:
                engine._rs_date = date_int
                engine._day_open_for_rs = bar.open
            stock_pct = (bar.close - engine._day_open_for_rs) / engine._day_open_for_rs * 100.0 if engine._day_open_for_rs > 0 else NaN
            market_ctx = compute_market_context(spy_snap, qqq_snap,
                                                 sector_snapshot=sec_snap,
                                                 stock_pct_from_open=stock_pct)

        signals = engine.process_bar(bar, market_ctx=market_ctx)
        for sig in signals:
            if sig.setup_id == SetupId.VWAP_RECLAIM:
                signal_entries.append((ts.date(), ts.hour * 100 + ts.minute))

    return signal_entries


def trace_vr_state(sym: str, bars: list, target_date: date, spy_bars: list, qqq_bars: list, cfg: OverlayConfig):
    """Trace VR state bar-by-bar on a specific date, comparing standalone vs engine."""
    print(f"\n  ═══ TRACE: {sym} on {target_date} ═══")

    # Filter bars to target date
    day_bars = [b for b in bars if b.timestamp.date() == target_date]
    if not day_bars:
        print(f"    No bars for {target_date}")
        return

    # ── Standalone trace ──
    print(f"\n  STANDALONE VR STATE:")
    ema9 = EMA(9)
    vwap = VWAPCalc()
    vol_buf = deque(maxlen=20)
    vol_ma = NaN
    was_below = False
    hold_count = 0
    hold_low = NaN

    # Warm up indicators on all bars before target_date
    prev_bars = [b for b in bars if b.timestamp.date() < target_date]
    prev_date = None
    for b in prev_bars:
        ema9.update(b.close)
        if b.timestamp.date() != prev_date:
            vwap.reset()
            prev_date = b.timestamp.date()
        tp = (b.high + b.low + b.close) / 3.0
        vwap.update(tp, b.volume)
        vol_buf.append(b.volume)

    # Reset day state
    vwap.reset()
    was_below = False
    hold_count = 0
    hold_low = NaN

    for b in day_bars:
        ema9.update(b.close)
        tp = (b.high + b.low + b.close) / 3.0
        vw = vwap.update(tp, b.volume)
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute

        if len(vol_buf) == 20:
            vol_ma = sum(vol_buf) / 20
        vol_buf.append(b.volume)

        if not vwap.ready:
            continue

        above = b.close > vw

        old_below = was_below
        old_hold = hold_count

        if above and not was_below:
            pass
        elif not above:
            was_below = True
            hold_count = 0
            hold_low = NaN
        elif was_below and above and hold_count == 0:
            hold_count = 1
            hold_low = b.low
        elif hold_count > 0 and hold_count < 3:
            if above:
                hold_count += 1
                hold_low = min(hold_low, b.low) if not _isnan(hold_low) else b.low
            else:
                was_below = True
                hold_count = 0
                hold_low = NaN

        # Only print 09:30-11:00 range
        if 930 <= hhmm <= 1100:
            triggered = ""
            if hold_count >= 3 and above and 1000 <= hhmm <= 1059:
                rng = b.high - b.low
                body = abs(b.close - b.open)
                is_bull = b.close > b.open
                body_pct = body / rng if rng > 0 else 0
                candle_ok = is_bull and body_pct >= 0.40
                vol_ok = not _isnan(vol_ma) and vol_ma > 0 and b.volume >= 0.70 * vol_ma
                if candle_ok and vol_ok:
                    triggered = " ★★★ TRIGGER"
                else:
                    triggered = f" (candle={candle_ok}, vol={vol_ok})"

            print(f"    {hhmm:04d}  C={b.close:8.2f}  VWAP={vw:8.2f}  "
                  f"abv={above}  was_blw={old_below}→{was_below}  "
                  f"hold={old_hold}→{hold_count}  "
                  f"hold_low={'NaN' if _isnan(hold_low) else f'{hold_low:.2f}':>8s}{triggered}")

    # ── Engine trace ──
    print(f"\n  ENGINE VR STATE:")
    engine = SignalEngine(cfg)
    spy_engine = MarketEngine()
    qqq_engine = MarketEngine()
    sec_etf = get_sector_etf(sym)
    sec_bars_data = load_bars(sec_etf) if sec_etf and sec_etf not in {"SPY", "QQQ"} else None
    sec_engine = MarketEngine() if sec_bars_data else None

    def _build_ts_map(market_bars):
        return {mb.timestamp: idx for idx, mb in enumerate(market_bars)}

    spy_ts_map = _build_ts_map(spy_bars) if spy_bars else {}
    qqq_ts_map = _build_ts_map(qqq_bars) if qqq_bars else {}
    sec_ts_map = _build_ts_map(sec_bars_data) if sec_bars_data else {}

    spy_idx = qqq_idx = sec_idx = 0
    spy_snap = qqq_snap = sec_snap = None

    for b in bars:
        ts = b.timestamp
        if ts in spy_ts_map and spy_ts_map[ts] >= spy_idx:
            while spy_idx <= spy_ts_map[ts]:
                spy_snap = spy_engine.process_bar(spy_bars[spy_idx])
                spy_idx += 1
        if ts in qqq_ts_map and qqq_ts_map[ts] >= qqq_idx:
            while qqq_idx <= qqq_ts_map[ts]:
                qqq_snap = qqq_engine.process_bar(qqq_bars[qqq_idx])
                qqq_idx += 1
        if sec_bars_data and ts in sec_ts_map and sec_ts_map[ts] >= sec_idx:
            while sec_idx <= sec_ts_map[ts]:
                sec_snap = sec_engine.process_bar(sec_bars_data[sec_idx])
                sec_idx += 1

        market_ctx = None
        if cfg.use_market_context and spy_snap and qqq_snap:
            date_int = ts.year * 10000 + ts.month * 100 + ts.day
            if not hasattr(engine, '_rs_date') or engine._rs_date != date_int:
                engine._rs_date = date_int
                engine._day_open_for_rs = b.open
            stock_pct = (b.close - engine._day_open_for_rs) / engine._day_open_for_rs * 100.0 if engine._day_open_for_rs > 0 else NaN
            market_ctx = compute_market_context(spy_snap, qqq_snap,
                                                 sector_snapshot=sec_snap,
                                                 stock_pct_from_open=stock_pct)

        signals = engine.process_bar(b, market_ctx=market_ctx)

        d = ts.date()
        hhmm = ts.hour * 100 + ts.minute

        if d == target_date and 930 <= hhmm <= 1100:
            ds = engine.day
            vw = engine.vwap.value if engine.vwap.ready else NaN
            above = b.close > vw if not _isnan(vw) else False
            triggered = ""
            for sig in signals:
                if sig.setup_id == SetupId.VWAP_RECLAIM:
                    triggered = " ★★★ VR SIGNAL"

            print(f"    {hhmm:04d}  C={b.close:8.2f}  VWAP={vw:8.2f}  "
                  f"abv={above}  was_blw={ds.vr_was_below}  "
                  f"hold={ds.vr_hold_count}  "
                  f"hold_low={'NaN' if _isnan(ds.vr_hold_low) else f'{ds.vr_hold_low:.2f}':>8s}  "
                  f"trig={ds.vr_triggered}{triggered}")


def main():
    symbols = get_universe()
    spy_bars = load_bars("SPY")
    qqq_bars = load_bars("QQQ")

    # Build "all gates off" config
    from .portfolio_configs import candidate_v2_vrgreen_bdr
    cfg = candidate_v2_vrgreen_bdr()
    cfg.vr_day_filter = "none"
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0

    print("=" * 140)
    print("STATE MACHINE TRACE — Find divergence between standalone and engine VR")
    print("=" * 140)

    # Find symbols/dates where standalone fires but engine (all gates off) doesn't
    mismatches = []

    for sym in symbols[:20]:  # Sample first 20 for speed
        bars = load_bars(sym)
        if not bars:
            continue

        sa_entries = standalone_vr_entries(bars, sym)
        eng_entries = engine_vr_entries(sym, bars, spy_bars, qqq_bars, cfg)
        eng_set = set(eng_entries)

        for d, hhmm, idx in sa_entries:
            if (d, hhmm) not in eng_set:
                mismatches.append((sym, d, hhmm))

    print(f"\n  Found {len(mismatches)} standalone-only entries in first 20 symbols")

    if mismatches:
        # Trace first 3 mismatches
        for sym, d, hhmm in mismatches[:3]:
            bars = load_bars(sym)
            trace_vr_state(sym, bars, d, spy_bars, qqq_bars, cfg)

    print(f"\n{'=' * 140}")


if __name__ == "__main__":
    main()
