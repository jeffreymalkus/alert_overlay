"""
No-target isolation test.

Strips out the target entirely. Trades exit by:
  1. Fixed stop (at original stop level)
  2. Time exit (N bars after entry)
  3. EOD (session end)

If this is profitable → entries have edge, exit system needs redesign.
If this is weak → entries themselves need work.

Also tests multiple hold periods to find the natural duration of the edge.

Usage:
    python -m alert_overlay.time_exit_test
    python -m alert_overlay.time_exit_test --min-quality 4 --require-regime
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

from .backtest import load_bars_from_csv, Trade
from .config import OverlayConfig
from .engine import SignalEngine
from .models import Signal, NaN

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent / "data"
WATCHLIST_FILE = Path(__file__).parent / "watchlist.txt"


def load_watchlist():
    symbols = []
    with open(WATCHLIST_FILE) as f:
        for line in f:
            sym = line.strip().upper()
            if sym and not sym.startswith("#"):
                symbols.append(sym)
    return symbols


def run_time_exit(bars, cfg, max_bars=12, min_quality=1, require_regime=False,
                  session_end=1555, use_stop=True):
    """
    Run backtest with time-based exit only (no target).
    max_bars: exit after this many bars if stop hasn't hit.
    use_stop: if False, removes the stop too (pure time exit).
    """
    engine = SignalEngine(cfg)
    slip = cfg.slippage_per_side
    comm = cfg.commission_per_share
    cost = slip + comm

    trades = []
    open_trade = None

    for i, bar in enumerate(bars):
        signals = engine.process_bar(bar)

        filtered = [s for s in signals
                    if s.quality_score >= min_quality
                    and (not require_regime or s.fits_regime)]

        # Check open trade
        if open_trade is not None:
            sig = open_trade["signal"]
            bars_held = i - open_trade["idx"]
            exited = False

            # EOD
            if bar.time_hhmm >= session_end:
                _add_trade(trades, sig, open_trade, bar.close, "eod", bars_held, cost)
                open_trade = None
                exited = True

            # Stop (if enabled)
            if not exited and use_stop:
                if sig.direction == 1 and bar.low <= sig.stop_price:
                    _add_trade(trades, sig, open_trade, sig.stop_price, "stop", bars_held, cost)
                    open_trade = None
                    exited = True
                elif sig.direction == -1 and bar.high >= sig.stop_price:
                    _add_trade(trades, sig, open_trade, sig.stop_price, "stop", bars_held, cost)
                    open_trade = None
                    exited = True

            # Time exit
            if not exited and bars_held >= max_bars:
                _add_trade(trades, sig, open_trade, bar.close, "time", bars_held, cost)
                open_trade = None
                exited = True

        # New signals
        for sig in filtered:
            if open_trade is not None:
                old = open_trade["signal"]
                if old.direction != sig.direction:
                    bh = i - open_trade["idx"]
                    _add_trade(trades, old, open_trade, bar.close, "opposing", bh, cost)
                    open_trade = None

            if open_trade is None:
                filled = sig.entry_price + (cost * sig.direction)
                open_trade = {"signal": sig, "idx": i, "filled_entry": filled}

    if open_trade is not None:
        sig = open_trade["signal"]
        bh = len(bars) - 1 - open_trade["idx"]
        _add_trade(trades, sig, open_trade, bars[-1].close, "eod", bh, cost)

    return trades


def _add_trade(trades, sig, ot, exit_price, reason, bars_held, cost):
    adj_exit = exit_price - (cost * sig.direction)
    pnl = (adj_exit - ot["filled_entry"]) * sig.direction
    pnl_rr = pnl / sig.risk if sig.risk > 0 else 0
    trades.append({
        "pnl": pnl,
        "pnl_rr": pnl_rr,
        "reason": reason,
        "bars_held": bars_held,
        "risk": sig.risk,
        "quality": sig.quality_score,
        "setup": sig.setup_name,
        "direction": sig.direction,
        "symbol": "",
    })


def summarize(trades, label=""):
    n = len(trades)
    if n == 0:
        return n, 0, 0, 0, 0
    wins = sum(1 for t in trades if t["pnl"] > 0)
    wr = wins / n * 100
    pnl = sum(t["pnl"] for t in trades)
    avg_rr = sum(t["pnl_rr"] for t in trades) / n
    gw = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    pf = gw / gl if gl > 0 else float("inf")
    return n, wr, pf, pnl, avg_rr


def main():
    parser = argparse.ArgumentParser(description="Time-exit isolation test")
    parser.add_argument("--symbols", type=str, default=None)
    parser.add_argument("--min-quality", type=int, default=1)
    parser.add_argument("--require-regime", action="store_true")
    parser.add_argument("--session-end", type=int, default=1555)
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        symbols = load_watchlist()

    cfg = OverlayConfig()

    # Load data
    all_bars = {}
    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if p.exists():
            bars = load_bars_from_csv(str(p))
            if bars:
                all_bars[sym] = bars

    print(f"Time-Exit Isolation Test — {len(all_bars)} symbols")
    print(f"Filters: min_quality={args.min_quality}, require_regime={args.require_regime}")

    # ══════════════════════════════════════════════════════════════
    # TEST 1: VARYING HOLD PERIODS (with stop)
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 90}")
    print("  TEST 1: TIME EXIT + STOP (no target)")
    print(f"{'=' * 90}")
    print(f"  {'Hold (bars)':<12} {'Hold (min)':<11} {'Trades':>6} {'WR%':>7} {'PF':>6} "
          f"{'PnL(pts)':>10} {'AvgR':>7} {'Stop%':>6} {'Time%':>6} {'EOD%':>5}")
    print(f"  {'-' * 84}")

    for max_bars in [2, 4, 6, 8, 10, 12, 16, 20, 30, 50, 78]:
        all_trades = []
        for sym, bars in all_bars.items():
            trades = run_time_exit(bars, cfg, max_bars=max_bars,
                                   min_quality=args.min_quality,
                                   require_regime=args.require_regime,
                                   session_end=args.session_end,
                                   use_stop=True)
            for t in trades:
                t["symbol"] = sym
            all_trades.extend(trades)

        n, wr, pf, pnl, avg_rr = summarize(all_trades)
        if n == 0:
            continue

        reasons = defaultdict(int)
        for t in all_trades:
            reasons[t["reason"]] += 1

        pf_str = f"{pf:.2f}" if pf < 999 else "∞"
        stop_pct = reasons.get("stop", 0) / n * 100
        time_pct = reasons.get("time", 0) / n * 100
        eod_pct = reasons.get("eod", 0) / n * 100
        hold_min = max_bars * cfg.bar_interval_minutes

        print(f"  {max_bars:<12} {hold_min:<11} {n:>6} {wr:>6.1f}% {pf_str:>6} "
              f"{pnl:>+10.2f} {avg_rr:>+7.3f} {stop_pct:>5.1f}% {time_pct:>5.1f}% {eod_pct:>4.1f}%")

    # ══════════════════════════════════════════════════════════════
    # TEST 2: PURE TIME EXIT (no stop, no target)
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 90}")
    print("  TEST 2: PURE TIME EXIT (no stop, no target)")
    print(f"{'=' * 90}")
    print(f"  {'Hold (bars)':<12} {'Hold (min)':<11} {'Trades':>6} {'WR%':>7} {'PF':>6} "
          f"{'PnL(pts)':>10} {'AvgR':>7}")
    print(f"  {'-' * 62}")

    for max_bars in [2, 4, 6, 8, 10, 12, 16, 20, 30, 50, 78]:
        all_trades = []
        for sym, bars in all_bars.items():
            trades = run_time_exit(bars, cfg, max_bars=max_bars,
                                   min_quality=args.min_quality,
                                   require_regime=args.require_regime,
                                   session_end=args.session_end,
                                   use_stop=False)
            for t in trades:
                t["symbol"] = sym
            all_trades.extend(trades)

        n, wr, pf, pnl, avg_rr = summarize(all_trades)
        if n == 0:
            continue

        pf_str = f"{pf:.2f}" if pf < 999 else "∞"
        hold_min = max_bars * cfg.bar_interval_minutes

        print(f"  {max_bars:<12} {hold_min:<11} {n:>6} {wr:>6.1f}% {pf_str:>6} "
              f"{pnl:>+10.2f} {avg_rr:>+7.3f}")

    # ══════════════════════════════════════════════════════════════
    # TEST 3: COMPARE BASELINE vs BEST TIME EXIT
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 90}")
    print("  TEST 3: ORIGINAL SYSTEM vs TIME-EXIT VARIANTS")
    print(f"{'=' * 90}")

    # Original system (with targets)
    from .backtest import run_backtest
    orig_trades = []
    for sym, bars in all_bars.items():
        result = run_backtest(bars, cfg=cfg, session_end_hhmm=args.session_end)
        for t in result.trades:
            if t.signal.quality_score >= args.min_quality:
                if args.require_regime and not t.signal.fits_regime:
                    continue
                orig_trades.append({"pnl": t.pnl_points, "pnl_rr": t.pnl_rr})

    orig_n = len(orig_trades)
    orig_wins = sum(1 for t in orig_trades if t["pnl"] > 0)
    orig_wr = orig_wins / orig_n * 100 if orig_n > 0 else 0
    orig_pnl = sum(t["pnl"] for t in orig_trades)
    orig_gw = sum(t["pnl"] for t in orig_trades if t["pnl"] > 0)
    orig_gl = abs(sum(t["pnl"] for t in orig_trades if t["pnl"] <= 0))
    orig_pf = orig_gw / orig_gl if orig_gl > 0 else float("inf")

    print(f"\n  {'System':<35} {'Trades':>6} {'WR%':>7} {'PF':>6} {'PnL(pts)':>10}")
    print(f"  {'-' * 66}")
    pf_str = f"{orig_pf:.2f}" if orig_pf < 999 else "∞"
    print(f"  {'Original (stop + target)':<35} {orig_n:>6} {orig_wr:>6.1f}% {pf_str:>6} {orig_pnl:>+10.2f}")

    # Best time exits for comparison
    for label, max_bars, use_stop in [
        ("Time 12 bars + stop", 12, True),
        ("Time 20 bars + stop", 20, True),
        ("Time 30 bars + stop", 30, True),
        ("Time 12 bars, no stop", 12, False),
        ("Time 20 bars, no stop", 20, False),
    ]:
        all_trades = []
        for sym, bars in all_bars.items():
            trades = run_time_exit(bars, cfg, max_bars=max_bars,
                                   min_quality=args.min_quality,
                                   require_regime=args.require_regime,
                                   session_end=args.session_end,
                                   use_stop=use_stop)
            all_trades.extend(trades)

        n, wr, pf, pnl, _ = summarize(all_trades)
        pf_str = f"{pf:.2f}" if pf < 999 else "∞"
        print(f"  {label:<35} {n:>6} {wr:>6.1f}% {pf_str:>6} {pnl:>+10.2f}")

    # ══════════════════════════════════════════════════════════════
    # TEST 4: BY SETUP TYPE (time exit)
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 90}")
    print("  TEST 4: SETUP TYPE PERFORMANCE (20-bar time exit + stop)")
    print(f"{'=' * 90}")

    all_trades = []
    for sym, bars in all_bars.items():
        trades = run_time_exit(bars, cfg, max_bars=20,
                               min_quality=args.min_quality,
                               require_regime=args.require_regime,
                               session_end=args.session_end,
                               use_stop=True)
        for t in trades:
            t["symbol"] = sym
        all_trades.extend(trades)

    setup_groups = defaultdict(list)
    for t in all_trades:
        setup_groups[t["setup"]].append(t)

    print(f"\n  {'Setup':<16} {'Trades':>6} {'WR%':>7} {'PF':>6} {'PnL(pts)':>10} {'AvgR':>7}")
    print(f"  {'-' * 54}")
    for name in sorted(setup_groups.keys()):
        group = setup_groups[name]
        n, wr, pf, pnl, avg_rr = summarize(group)
        pf_str = f"{pf:.2f}" if pf < 999 else "∞"
        print(f"  {name:<16} {n:>6} {wr:>6.1f}% {pf_str:>6} {pnl:>+10.2f} {avg_rr:>+7.3f}")

    print(f"\n{'=' * 90}")


if __name__ == "__main__":
    main()
