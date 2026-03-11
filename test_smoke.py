"""
Smoke test — verify the engine runs without errors on synthetic data.
"""

import sys
import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Add parent to path for direct execution
sys.path.insert(0, str(Path(__file__).parent.parent))

from alert_overlay.config import OverlayConfig
from alert_overlay.models import Bar
from alert_overlay.engine import SignalEngine


def generate_synthetic_day(base_date: datetime, base_price: float = 450.0,
                           volatility: float = 0.003,
                           bar_minutes: int = 5) -> list:
    """Generate a synthetic intraday session of N-min bars."""
    bars = []
    price = base_price
    t = base_date.replace(hour=9, minute=30, second=0, microsecond=0)
    end = base_date.replace(hour=16, minute=0, second=0, microsecond=0)

    while t < end:
        change = price * volatility * random.gauss(0, 1)
        o = price
        h = o + abs(random.gauss(0, 1) * price * volatility)
        l = o - abs(random.gauss(0, 1) * price * volatility)
        c = o + change
        h = max(h, o, c)
        l = min(l, o, c)
        vol = random.randint(50000, 500000)

        bars.append(Bar(
            timestamp=t, open=o, high=h, low=l, close=c, volume=vol
        ))
        price = c
        t += timedelta(minutes=bar_minutes)

    return bars


def main():
    print("=" * 50)
    print("  SMOKE TEST — Signal Engine v4.5")
    print("=" * 50)

    cfg = OverlayConfig()
    engine = SignalEngine(cfg)

    # Warm up daily ATR with fake daily data
    daily_history = []
    p = 445.0
    for i in range(30):
        h = p + random.uniform(2, 8)
        l = p - random.uniform(2, 8)
        c = p + random.uniform(-3, 3)
        daily_history.append({"high": h, "low": l, "close": c})
        p = c

    engine.set_daily_atr_history(daily_history)
    engine.set_prior_day(daily_history[-1]["high"], daily_history[-1]["low"])

    # Generate 3 days of synthetic data
    all_signals = []
    eastern = ZoneInfo("US/Eastern")
    base = datetime(2025, 3, 3, 9, 30, tzinfo=eastern)
    total_bars = 0

    for day_offset in range(3):
        day_dt = base + timedelta(days=day_offset)
        bars = generate_synthetic_day(day_dt)
        total_bars += len(bars)

        for bar in bars:
            signals = engine.process_bar(bar)
            all_signals.extend(signals)

    print(f"  Processed {total_bars} bars across 3 days")
    print(f"  Signals generated: {len(all_signals)}")

    if all_signals:
        print(f"\n  Sample signals:")
        for sig in all_signals[:5]:
            print(f"    {sig.timestamp} | {sig.label} | "
                  f"E:{sig.entry_price:.2f} S:{sig.stop_price:.2f} "
                  f"T:{sig.target_price:.2f} | RR:{sig.rr_ratio:.2f} Q:{sig.quality_score}")
    else:
        print("  (No signals on synthetic random data — this is normal)")
        print("  The engine ran without errors, which validates the logic flow.")

    print(f"\n  Engine state:")
    print(f"    Daily ATR: {engine._daily_atr.value:.4f}")
    print(f"    EMA9:  {engine.ema9.value:.4f}")
    print(f"    EMA20: {engine.ema20.value:.4f}")
    print(f"    VWAP:  {engine.vwap.value:.4f}")
    print(f"    Prev day H/L: {engine.prev_day_high:.2f} / {engine.prev_day_low:.2f}")

    print("\n  SMOKE TEST PASSED")
    print("=" * 50)


if __name__ == "__main__":
    main()
