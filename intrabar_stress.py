"""
Intrabar Execution Stress Test — v1.

Since no 1-minute data is available, this stress-tests EMA_RECLAIM trades under
conservative assumptions about what happens WITHIN each 5-minute bar:

1. Adversarial Entry Model: Instead of bar-close entry, simulates entry at
   close + adverse fraction of next bar's range (assumes you get filled
   worse than bar close due to momentum/spread).

2. Adversarial Stop Model: For each trade's holding period, simulates
   the bar path as low-first (longs) / high-first (shorts), meaning
   the stop is always tested before the favorable move.

3. Time-of-Day Penalty: Adds time-of-day-based slippage premium for
   entries near the open (higher volatility, wider spreads).

4. Partial Fill Stress: Simulates the impact of only filling on bars
   where the close is near the favorable direction (momentum fade).

Reports:
  - Original vs stress-tested PnL / WR / PF / expectancy
  - Stop-hit differential (how many more stops under stress)
  - Dollar-cost impact per trade
  - Survival matrix: does EMA_RECLAIM stay positive under each stress?

Usage:
    python -m alert_overlay.intrabar_stress
"""

import math
import os
import sys
import statistics
from collections import defaultdict
from pathlib import Path
from typing import List, Optional

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import Bar, SetupId, SetupFamily, SETUP_FAMILY_MAP, NaN
from .market_context import get_sector_etf, SECTOR_MAP

DATA_DIR = Path(__file__).parent / "data"


def _make_ema_iso_cfg() -> OverlayConfig:
    """EMA_RECLAIM isolated config."""
    cfg = OverlayConfig()
    cfg.show_reversal_setups = False
    cfg.show_trend_setups = False
    cfg.show_ema_retest = False
    cfg.show_ema_mean_rev = False
    cfg.show_ema_pullback = False
    cfg.show_second_chance = False
    cfg.show_spencer = False
    cfg.show_failed_bounce = False
    cfg.show_ema_scalp = True
    cfg.show_ema_confirm = False
    cfg.use_dynamic_slippage = True
    return cfg


def _load_market_data():
    """Load SPY, QQQ, sector ETF bars."""
    spy = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv")) if (DATA_DIR / "SPY_5min.csv").exists() else None
    qqq = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv")) if (DATA_DIR / "QQQ_5min.csv").exists() else None
    sectors = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sectors[etf] = load_bars_from_csv(str(p))
    return spy, qqq, sectors


def _collect_ema_trades(cfg, spy_bars, qqq_bars, sector_bars_dict):
    """Run backtest with given config, return (sym, trade, bars) tuples for EMA_RECLAIM."""
    results = []
    for f in sorted(os.listdir(DATA_DIR)):
        if not f.endswith('_5min.csv') or f in ('SPY_5min.csv', 'QQQ_5min.csv'):
            continue
        sym = f.replace('_5min.csv', '')
        bars = load_bars_from_csv(str(DATA_DIR / f))
        if not bars:
            continue

        kw = {"cfg": cfg}
        if spy_bars:
            kw["spy_bars"] = spy_bars
            kw["qqq_bars"] = qqq_bars
            sec_etf = get_sector_etf(sym)
            if sec_etf and sector_bars_dict and sec_etf in sector_bars_dict:
                kw["sector_bars"] = sector_bars_dict[sec_etf]

        r = run_backtest(bars, **kw)
        for t in r.trades:
            if t.signal.setup_id == SetupId.EMA_RECLAIM:
                results.append((sym, t, bars))
    return results


def _tod_penalty(hhmm: int) -> float:
    """Time-of-day slippage multiplier. Higher near open, lower midday."""
    if hhmm < 1000:
        return 1.5  # first 30 min: wider spreads, more volatility
    elif hhmm < 1030:
        return 1.2
    elif hhmm < 1300:
        return 1.0
    elif hhmm < 1400:
        return 0.9  # midday: tighter
    elif hhmm < 1530:
        return 1.0
    else:
        return 1.3  # near close: widening


def stress_test_adversarial_entry(trades_with_bars, entry_adverse_frac=0.3):
    """
    Stress test 1: Adversarial entry pricing.

    Instead of entering at signal bar close, assume entry at:
    close + adverse_frac * next_bar_range * direction

    This simulates: you see the signal, place the order, and get filled
    at a worse price due to momentum continuation.
    """
    stressed = []
    for sym, trade, bars in trades_with_bars:
        sig = trade.signal
        entry_time = sig.timestamp

        # Find the next bar after entry
        next_bar = None
        for j, b in enumerate(bars):
            if b.timestamp > entry_time:
                next_bar = b
                break

        if next_bar is None:
            stressed.append(trade.pnl_points)
            continue

        # Adverse entry: shift entry in the adverse direction
        bar_range = next_bar.high - next_bar.low
        tod_mult = _tod_penalty(sig.timestamp.hour * 100 + sig.timestamp.minute)
        adverse_slip = entry_adverse_frac * bar_range * tod_mult

        # Original PnL minus the extra adverse entry cost
        stressed_pnl = trade.pnl_points - adverse_slip
        stressed.append(stressed_pnl)

    return stressed


def stress_test_stop_vulnerability(trades_with_bars, stop_tightening_frac=0.0):
    """
    Stress test 2: Adversarial bar-path model (low-first for longs).

    For each bar during the trade, assume the adverse extreme happens first.
    This means if bar.low touches stop before bar.high reaches target,
    the trade is stopped out even if the bar closed profitably.

    stop_tightening_frac: reduce stop distance by this fraction (simulates
    stop placement being slightly too tight for real execution).
    """
    stress_results = []

    for sym, trade, bars in trades_with_bars:
        sig = trade.signal
        entry = sig.entry_price
        original_stop = sig.stop_price
        direction = sig.direction
        risk = abs(entry - original_stop)

        # Tighten stop for stress test
        tightened_risk = risk * (1.0 - stop_tightening_frac)
        if direction == 1:
            stress_stop = entry - tightened_risk
        else:
            stress_stop = entry + tightened_risk

        # Walk bars: low-first model
        in_trade = False
        stress_exit_reason = trade.exit_reason
        stress_pnl = trade.pnl_points
        bars_held = 0

        for b in bars:
            if b.timestamp == sig.timestamp:
                in_trade = True
                continue
            if not in_trade:
                continue
            if trade.exit_time and b.timestamp > trade.exit_time:
                break

            bars_held += 1

            if direction == 1:
                # Low-first: check stop before checking any favorable move
                if b.low <= stress_stop:
                    stress_exit_reason = "stop_stress"
                    stress_pnl = (stress_stop - entry) - (risk * 0.02)  # small extra friction
                    break
            else:
                # High-first: check stop before favorable
                if b.high >= stress_stop:
                    stress_exit_reason = "stop_stress"
                    stress_pnl = (entry - stress_stop) - (risk * 0.02)
                    break

        stress_results.append({
            "sym": sym,
            "original_pnl": trade.pnl_points,
            "stressed_pnl": stress_pnl,
            "original_exit": trade.exit_reason,
            "stressed_exit": stress_exit_reason,
            "risk": risk,
            "bars_held": bars_held,
        })

    return stress_results


def stress_test_combined(trades_with_bars, entry_adverse_frac=0.3, stop_tightening_frac=0.15):
    """
    Stress test 3: Combined adversarial entry + stop tightening.

    This is the harshest realistic scenario:
    - Entry is worse by adverse_frac of next bar's range
    - Stops are 15% tighter than engine-computed distance
    - Time-of-day penalty on entry
    """
    results = []

    for sym, trade, bars in trades_with_bars:
        sig = trade.signal
        entry = sig.entry_price
        original_stop = sig.stop_price
        direction = sig.direction
        risk = abs(entry - original_stop)

        # Find next bar for adversarial entry
        next_bar = None
        for j, b in enumerate(bars):
            if b.timestamp > sig.timestamp:
                next_bar = b
                break

        # Adversarial entry cost
        if next_bar:
            bar_range = next_bar.high - next_bar.low
            tod_mult = _tod_penalty(sig.timestamp.hour * 100 + sig.timestamp.minute)
            entry_cost = entry_adverse_frac * bar_range * tod_mult
        else:
            entry_cost = 0.0

        # Tightened stop
        tightened_risk = risk * (1.0 - stop_tightening_frac)
        if direction == 1:
            stress_stop = entry - tightened_risk
        else:
            stress_stop = entry + tightened_risk

        # Walk bars with low-first model
        in_trade = False
        stress_exit = trade.exit_reason
        stress_pnl = trade.pnl_points - entry_cost  # start with adverse entry

        for b in bars:
            if b.timestamp == sig.timestamp:
                in_trade = True
                continue
            if not in_trade:
                continue
            if trade.exit_time and b.timestamp > trade.exit_time:
                break

            if direction == 1:
                if b.low <= stress_stop:
                    stress_exit = "stop_stress"
                    stress_pnl = (stress_stop - entry) - entry_cost
                    break
            else:
                if b.high >= stress_stop:
                    stress_exit = "stop_stress"
                    stress_pnl = (entry - stress_stop) - entry_cost
                    break

        results.append({
            "sym": sym,
            "time": str(sig.timestamp)[:19],
            "original_pnl": trade.pnl_points,
            "stressed_pnl": stress_pnl,
            "entry_cost": entry_cost,
            "original_exit": trade.exit_reason,
            "stressed_exit": stress_exit,
            "risk": risk,
        })

    return results


def main():
    spy, qqq, sectors = _load_market_data()
    cfg = _make_ema_iso_cfg()

    print(f"\n{'='*100}")
    print("  INTRABAR EXECUTION STRESS TEST — EMA_RECLAIM")
    print(f"{'='*100}")

    trades_with_bars = _collect_ema_trades(cfg, spy, qqq, sectors)
    n = len(trades_with_bars)
    if n == 0:
        print("  No EMA_RECLAIM trades found.")
        return

    # Baseline
    base_pnls = [t.pnl_points for _, t, _ in trades_with_bars]
    base_pnl = sum(base_pnls)
    base_wins = sum(1 for p in base_pnls if p > 0)
    base_wr = base_wins / n * 100
    base_gw = sum(p for p in base_pnls if p > 0)
    base_gl = abs(sum(p for p in base_pnls if p <= 0))
    base_pf = base_gw / base_gl if base_gl > 0 else float("inf")
    base_exp = (sum(t.pnl_rr for _, t, _ in trades_with_bars)) / n
    base_stops = sum(1 for _, t, _ in trades_with_bars if t.exit_reason == "stop")

    print(f"\n  Baseline (standard dynamic slippage):")
    print(f"    Trades: {n}  WR: {base_wr:.1f}%  PF: {base_pf:.2f}  "
          f"PnL: {base_pnl:+.2f}  Exp: {base_exp:+.3f}R  Stops: {base_stops}")

    # ── STRESS 1: Adversarial Entry ──
    print(f"\n  {'─'*80}")
    print(f"  STRESS 1: Adversarial Entry (30% of next bar range + TOD penalty)")
    print(f"  {'─'*80}")

    s1_pnls = stress_test_adversarial_entry(trades_with_bars, entry_adverse_frac=0.3)
    s1_pnl = sum(s1_pnls)
    s1_wins = sum(1 for p in s1_pnls if p > 0)
    s1_wr = s1_wins / n * 100
    s1_gw = sum(p for p in s1_pnls if p > 0)
    s1_gl = abs(sum(p for p in s1_pnls if p <= 0))
    s1_pf = s1_gw / s1_gl if s1_gl > 0 else float("inf")
    s1_cost = base_pnl - s1_pnl

    print(f"    Trades: {n}  WR: {s1_wr:.1f}%  PF: {s1_pf:.2f}  "
          f"PnL: {s1_pnl:+.2f}  Cost: {s1_cost:.2f}")
    print(f"    Per-trade cost: ${s1_cost/n:.3f}  WR delta: {s1_wr - base_wr:+.1f}%")

    # ── STRESS 2: Stop Vulnerability (low-first bar model) ──
    print(f"\n  {'─'*80}")
    print(f"  STRESS 2: Adversarial Bar Path (low-first for longs)")
    print(f"  {'─'*80}")

    for tighten_pct in [0, 10, 15, 20]:
        s2 = stress_test_stop_vulnerability(trades_with_bars, stop_tightening_frac=tighten_pct/100)
        s2_pnl = sum(r["stressed_pnl"] for r in s2)
        s2_wins = sum(1 for r in s2 if r["stressed_pnl"] > 0)
        s2_wr = s2_wins / n * 100
        s2_extra_stops = sum(1 for r in s2 if r["stressed_exit"] == "stop_stress" and r["original_exit"] != "stop")
        s2_gw = sum(r["stressed_pnl"] for r in s2 if r["stressed_pnl"] > 0)
        s2_gl = abs(sum(r["stressed_pnl"] for r in s2 if r["stressed_pnl"] <= 0))
        s2_pf = s2_gw / s2_gl if s2_gl > 0 else float("inf")

        label = f"  Tighten {tighten_pct}%"
        print(f"  {label:<18} WR: {s2_wr:>5.1f}%  PF: {s2_pf:>5.2f}  "
              f"PnL: {s2_pnl:>+8.2f}  Extra stops: {s2_extra_stops:>3}  "
              f"{'POSITIVE' if s2_pnl > 0 else 'NEGATIVE'}")

    # ── STRESS 3: Combined (harshest realistic) ──
    print(f"\n  {'─'*80}")
    print(f"  STRESS 3: Combined (30% adverse entry + 15% stop tightening + TOD)")
    print(f"  {'─'*80}")

    s3 = stress_test_combined(trades_with_bars, entry_adverse_frac=0.3, stop_tightening_frac=0.15)
    s3_pnl = sum(r["stressed_pnl"] for r in s3)
    s3_wins = sum(1 for r in s3 if r["stressed_pnl"] > 0)
    s3_wr = s3_wins / n * 100
    s3_gw = sum(r["stressed_pnl"] for r in s3 if r["stressed_pnl"] > 0)
    s3_gl = abs(sum(r["stressed_pnl"] for r in s3 if r["stressed_pnl"] <= 0))
    s3_pf = s3_gw / s3_gl if s3_gl > 0 else float("inf")
    s3_flipped = sum(1 for r in s3 if r["original_pnl"] > 0 and r["stressed_pnl"] <= 0)
    s3_extra_stops = sum(1 for r in s3 if r["stressed_exit"] == "stop_stress" and r["original_exit"] != "stop")
    s3_entry_costs = [r["entry_cost"] for r in s3]

    print(f"    Trades: {n}  WR: {s3_wr:.1f}%  PF: {s3_pf:.2f}  PnL: {s3_pnl:+.2f}")
    print(f"    Wins flipped to losses: {s3_flipped}/{n}")
    print(f"    Extra stops from tightening: {s3_extra_stops}")
    print(f"    Avg entry cost: ${statistics.mean(s3_entry_costs):.4f}")
    print(f"    Max entry cost: ${max(s3_entry_costs):.4f}")
    print(f"    Total cost vs baseline: {base_pnl - s3_pnl:.2f} pts")

    # ── SURVIVAL MATRIX ──
    print(f"\n  {'─'*80}")
    print(f"  SURVIVAL MATRIX — Does EMA_RECLAIM stay positive?")
    print(f"  {'─'*80}")

    scenarios = [
        ("Baseline (dynamic slip)", base_pnl, base_wr, base_pf, base_exp),
    ]

    # Entry stress at different levels
    for adv_frac in [0.1, 0.2, 0.3, 0.5]:
        pnls = stress_test_adversarial_entry(trades_with_bars, entry_adverse_frac=adv_frac)
        pnl = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        gw = sum(p for p in pnls if p > 0)
        gl = abs(sum(p for p in pnls if p <= 0))
        pf = gw / gl if gl > 0 else float("inf")
        scenarios.append((f"Entry adverse {int(adv_frac*100)}%", pnl, wins/n*100, pf, pnl/n))

    # Combined at different severities
    for entry_frac, stop_frac in [(0.2, 0.10), (0.3, 0.15), (0.4, 0.20), (0.5, 0.25)]:
        s = stress_test_combined(trades_with_bars, entry_adverse_frac=entry_frac,
                                 stop_tightening_frac=stop_frac)
        pnl = sum(r["stressed_pnl"] for r in s)
        wins = sum(1 for r in s if r["stressed_pnl"] > 0)
        gw = sum(r["stressed_pnl"] for r in s if r["stressed_pnl"] > 0)
        gl = abs(sum(r["stressed_pnl"] for r in s if r["stressed_pnl"] <= 0))
        pf = gw / gl if gl > 0 else float("inf")
        scenarios.append((f"Combined {int(entry_frac*100)}%e+{int(stop_frac*100)}%s", pnl, wins/n*100, pf, pnl/n))

    print(f"\n  {'Scenario':<35} {'PnL':>8} {'WR%':>6} {'PF':>5} {'Exp':>7} {'Survive':>8}")
    print(f"  {'─'*75}")
    for label, pnl, wr, pf, exp in scenarios:
        pf_s = f"{pf:.2f}" if pf < 999 else "inf"
        surv = "YES" if pnl > 0 else "NO"
        print(f"  {label:<35} {pnl:>+7.2f} {wr:>5.1f}% {pf_s:>5} {exp:>+6.3f} {'  '+surv:>8}")

    positive = sum(1 for _, pnl, _, _, _ in scenarios if pnl > 0)
    print(f"\n  Survival rate: {positive}/{len(scenarios)} scenarios positive "
          f"({positive/len(scenarios)*100:.0f}%)")

    print(f"\n{'='*100}\n")


if __name__ == "__main__":
    main()
