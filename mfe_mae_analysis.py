"""
MFE / MAE Excursion Analysis

For every trade, tracks bar-by-bar:
  MAE = Maximum Adverse Excursion (worst drawdown before exit)
  MFE = Maximum Favorable Excursion (best unrealized gain before exit)

Expressed in R-multiples (units of initial risk).

This answers the core diagnostic question:
  - Do trades move in our favor before stopping out? → stops too tight
  - Do trades barely move favorably at all? → entries lack edge
  - Do trades reach +1R but not +2R? → targets too far

Usage:
    python -m alert_overlay.mfe_mae_analysis
    python -m alert_overlay.mfe_mae_analysis --symbols TSLA,META
    python -m alert_overlay.mfe_mae_analysis --min-quality 4 --require-regime
"""

import argparse
import sys
import statistics
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

from .backtest import load_bars_from_csv
from .config import OverlayConfig
from .engine import SignalEngine
from .models import Signal, SetupId, SetupFamily, SETUP_FAMILY_MAP, NaN

import math

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


def run_with_excursions(bars, cfg, min_quality=1, require_regime=False,
                        session_end=1555):
    """
    Run backtest tracking bar-by-bar MFE/MAE for each trade.
    Returns list of trade dicts with excursion data.
    """
    engine = SignalEngine(cfg)
    slip = cfg.slippage_per_side
    comm = cfg.commission_per_share
    cost_per_side = slip + comm

    trades = []
    open_trade = None

    for i, bar in enumerate(bars):
        signals = engine.process_bar(bar)

        # Filter signals
        filtered = []
        for sig in signals:
            if sig.quality_score < min_quality:
                continue
            if require_regime and not sig.fits_regime:
                continue
            filtered.append(sig)

        # Track excursions on open trade
        if open_trade is not None:
            sig = open_trade["signal"]
            d = sig.direction

            # Favorable and adverse moves from entry (using bar extremes)
            if d == 1:  # long
                favorable = bar.high - open_trade["filled_entry"]
                adverse = open_trade["filled_entry"] - bar.low
            else:  # short
                favorable = open_trade["filled_entry"] - bar.low
                adverse = bar.high - open_trade["filled_entry"]

            favorable = max(favorable, 0)
            adverse = max(adverse, 0)

            if favorable > open_trade["raw_mfe"]:
                open_trade["raw_mfe"] = favorable
            if adverse > open_trade["raw_mae"]:
                open_trade["raw_mae"] = adverse

            # Track R-multiple thresholds reached
            risk = sig.risk
            if risk > 0:
                fav_r = favorable / risk
                # Update peak favorable R reached this bar
                peak_r = open_trade["raw_mfe"] / risk
                # We already updated raw_mfe above, so peak_r is current max

            # Check exits
            exited = False
            is_breakout = SETUP_FAMILY_MAP.get(sig.setup_id) == SetupFamily.BREAKOUT
            bars_held = i - open_trade["idx"]

            # EOD
            if bar.time_hhmm >= session_end:
                open_trade["exit_reason"] = "eod"
                open_trade["exit_price"] = bar.close
                exited = True

            if not exited:
                if d == 1:
                    hit_stop = bar.low <= sig.stop_price
                else:
                    hit_stop = bar.high >= sig.stop_price

                if is_breakout:
                    # Breakout exits: stop > time stop > ema9 trail (no static target)
                    hit_time_stop = bars_held >= cfg.breakout_time_stop_bars
                    ema9_exit = False
                    if bars_held >= 2 and hasattr(bar, '_e9') and not math.isnan(bar._e9):
                        if d == 1 and bar.close < bar._e9:
                            ema9_exit = True
                        elif d == -1 and bar.close > bar._e9:
                            ema9_exit = True

                    if hit_stop:
                        open_trade["exit_reason"] = "stop"
                        open_trade["exit_price"] = sig.stop_price
                        exited = True
                    elif hit_time_stop:
                        open_trade["exit_reason"] = "time"
                        open_trade["exit_price"] = bar.close
                        exited = True
                    elif ema9_exit:
                        open_trade["exit_reason"] = "ema9trail"
                        open_trade["exit_price"] = bar.close
                        exited = True
                else:
                    # Non-breakout exits: stop > target
                    if d == 1:
                        hit_target = bar.high >= sig.target_price
                    else:
                        hit_target = bar.low <= sig.target_price

                    if hit_stop:
                        open_trade["exit_reason"] = "stop"
                        open_trade["exit_price"] = sig.stop_price
                        exited = True
                    elif hit_target:
                        open_trade["exit_reason"] = "target"
                        open_trade["exit_price"] = sig.target_price
                        exited = True

            if exited:
                _finalize_trade(open_trade, cost_per_side)
                trades.append(open_trade)
                open_trade = None

        # Process new signals
        for sig in filtered:
            # Close opposing
            if open_trade is not None:
                old = open_trade["signal"]
                if old.direction != sig.direction:
                    open_trade["exit_reason"] = "opposing"
                    open_trade["exit_price"] = bar.close
                    _finalize_trade(open_trade, cost_per_side)
                    trades.append(open_trade)
                    open_trade = None

            if open_trade is None:
                filled_entry = sig.entry_price + (cost_per_side * sig.direction)
                open_trade = {
                    "signal": sig,
                    "idx": i,
                    "filled_entry": filled_entry,
                    "raw_mfe": 0.0,
                    "raw_mae": 0.0,
                    "exit_reason": "",
                    "exit_price": 0.0,
                }

    # Close remaining
    if open_trade is not None:
        open_trade["exit_reason"] = "eod"
        open_trade["exit_price"] = bars[-1].close
        _finalize_trade(open_trade, cost_per_side)
        trades.append(open_trade)

    return trades


def _finalize_trade(t, cost_per_side):
    """Compute final PnL and R-multiple excursions."""
    sig = t["signal"]
    d = sig.direction
    risk = sig.risk

    adjusted_exit = t["exit_price"] - (cost_per_side * d)
    t["pnl_points"] = (adjusted_exit - t["filled_entry"]) * d
    t["pnl_rr"] = t["pnl_points"] / risk if risk > 0 else 0

    # Convert raw excursions to R-multiples
    t["mfe_r"] = t["raw_mfe"] / risk if risk > 0 else 0
    t["mae_r"] = t["raw_mae"] / risk if risk > 0 else 0


def print_section(title):
    print(f"\n{'─' * 70}")
    print(f"  {title}")
    print(f"{'─' * 70}")


def main():
    parser = argparse.ArgumentParser(description="MFE/MAE excursion analysis")
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

    print(f"MFE/MAE Analysis — {len(symbols)} symbols")
    print(f"Filters: min_quality={args.min_quality}, require_regime={args.require_regime}")

    # Gather all trades with excursion data
    all_trades = []
    for sym in symbols:
        csv_path = DATA_DIR / f"{sym}_5min.csv"
        if not csv_path.exists():
            continue
        bars = load_bars_from_csv(str(csv_path))
        if not bars:
            continue
        trades = run_with_excursions(
            bars, cfg,
            min_quality=args.min_quality,
            require_regime=args.require_regime,
            session_end=args.session_end,
        )
        for t in trades:
            t["symbol"] = sym
        all_trades.extend(trades)

    total = len(all_trades)
    winners = [t for t in all_trades if t["pnl_points"] > 0]
    losers = [t for t in all_trades if t["pnl_points"] <= 0]
    stops = [t for t in all_trades if t["exit_reason"] == "stop"]
    targets = [t for t in all_trades if t["exit_reason"] == "target"]
    eods = [t for t in all_trades if t["exit_reason"] == "eod"]
    opposing = [t for t in all_trades if t["exit_reason"] == "opposing"]
    time_exits = [t for t in all_trades if t["exit_reason"] == "time"]
    ema9_exits = [t for t in all_trades if t["exit_reason"] == "ema9trail"]

    print(f"\nTotal trades: {total} | Winners: {len(winners)} | Losers: {len(losers)}")
    print(f"Stops: {len(stops)} | Targets: {len(targets)} | EMA9Trail: {len(ema9_exits)} | "
          f"Time: {len(time_exits)} | EOD: {len(eods)} | Opposing: {len(opposing)}")

    # ══════════════════════════════════════════════════════════════
    # 1. CORE DIAGNOSTIC — MFE DISTRIBUTION (ALL TRADES)
    # ══════════════════════════════════════════════════════════════
    print_section("1. MFE DISTRIBUTION — HOW FAR DO TRADES MOVE IN YOUR FAVOR?")

    thresholds = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
    mfe_values = [t["mfe_r"] for t in all_trades]

    print(f"\n  ALL TRADES ({total}):")
    print(f"  {'Reached':>10} {'Count':>7} {'% of All':>9}")
    print(f"  {'-' * 28}")
    for thresh in thresholds:
        count = sum(1 for m in mfe_values if m >= thresh)
        pct = count / total * 100 if total > 0 else 0
        bar = "█" * int(pct / 2)
        print(f"  {'+' + str(thresh) + 'R':>10} {count:>7} {pct:>8.1f}%  {bar}")

    # Median and mean MFE
    if mfe_values:
        print(f"\n  Mean MFE:   {statistics.mean(mfe_values):>+.3f}R")
        print(f"  Median MFE: {statistics.median(mfe_values):>+.3f}R")

    # ══════════════════════════════════════════════════════════════
    # 2. MFE OF STOPPED-OUT TRADES (THE KEY QUESTION)
    # ══════════════════════════════════════════════════════════════
    print_section("2. MFE OF STOPPED-OUT TRADES — DID THEY MOVE FAVORABLY FIRST?")

    if stops:
        stop_mfes = [t["mfe_r"] for t in stops]
        print(f"\n  STOPPED-OUT TRADES ({len(stops)}):")
        print(f"  {'Reached':>10} {'Count':>7} {'% of Stops':>11}")
        print(f"  {'-' * 30}")
        for thresh in thresholds:
            count = sum(1 for m in stop_mfes if m >= thresh)
            pct = count / len(stops) * 100
            bar = "█" * int(pct / 2)
            print(f"  {'+' + str(thresh) + 'R':>10} {count:>7} {pct:>10.1f}%  {bar}")

        print(f"\n  Mean MFE of stopped trades:   {statistics.mean(stop_mfes):>+.3f}R")
        print(f"  Median MFE of stopped trades: {statistics.median(stop_mfes):>+.3f}R")

        # How many stopped trades had meaningful favorable movement?
        reached_half_r = sum(1 for m in stop_mfes if m >= 0.5)
        reached_1r = sum(1 for m in stop_mfes if m >= 1.0)
        print(f"\n  Stopped trades that reached +0.5R before stopping: "
              f"{reached_half_r} ({reached_half_r/len(stops)*100:.1f}%)")
        print(f"  Stopped trades that reached +1.0R before stopping:  "
              f"{reached_1r} ({reached_1r/len(stops)*100:.1f}%)")

        if reached_1r > 0:
            print(f"\n  → {reached_1r} trades moved a full R in your favor then reversed to stop.")
            print(f"    This is {reached_1r/len(stops)*100:.1f}% of all stops — these could have been winners.")
        if reached_half_r > reached_1r:
            marginal = reached_half_r - reached_1r
            print(f"  → {marginal} additional trades reached +0.5R but not +1.0R before stopping.")

    # ══════════════════════════════════════════════════════════════
    # 3. MAE DISTRIBUTION (ALL TRADES)
    # ══════════════════════════════════════════════════════════════
    print_section("3. MAE DISTRIBUTION — HOW FAR DO TRADES MOVE AGAINST YOU?")

    mae_values = [t["mae_r"] for t in all_trades]
    print(f"\n  ALL TRADES ({total}):")
    print(f"  {'Drawdown':>10} {'Count':>7} {'% of All':>9}")
    print(f"  {'-' * 28}")
    for thresh in thresholds:
        count = sum(1 for m in mae_values if m >= thresh)
        pct = count / total * 100 if total > 0 else 0
        bar = "█" * int(pct / 2)
        print(f"  {'-' + str(thresh) + 'R':>10} {count:>7} {pct:>8.1f}%  {bar}")

    if mae_values:
        print(f"\n  Mean MAE:   {statistics.mean(mae_values):.3f}R")
        print(f"  Median MAE: {statistics.median(mae_values):.3f}R")

    # ══════════════════════════════════════════════════════════════
    # 4. MAE OF WINNING TRADES
    # ══════════════════════════════════════════════════════════════
    print_section("4. MAE OF WINNERS — HOW MUCH HEAT DO WINNERS TAKE?")

    if winners:
        win_maes = [t["mae_r"] for t in winners]
        print(f"\n  WINNING TRADES ({len(winners)}):")
        print(f"  {'Drawdown':>10} {'Count':>7} {'% of Wins':>10}")
        print(f"  {'-' * 29}")
        for thresh in thresholds:
            count = sum(1 for m in win_maes if m >= thresh)
            pct = count / len(winners) * 100
            print(f"  {'-' + str(thresh) + 'R':>10} {count:>7} {pct:>9.1f}%")

        print(f"\n  Mean MAE of winners:   {statistics.mean(win_maes):.3f}R")
        print(f"  Median MAE of winners: {statistics.median(win_maes):.3f}R")

        # What stop width would capture most winners?
        for pct_target in [90, 95, 99]:
            sorted_maes = sorted(win_maes)
            idx = int(len(sorted_maes) * pct_target / 100) - 1
            if idx >= 0 and idx < len(sorted_maes):
                print(f"  Stop at {sorted_maes[idx]:.2f}R would keep {pct_target}% of winners")

    # ══════════════════════════════════════════════════════════════
    # 5. MFE OF WINNING TRADES (UNREALIZED POTENTIAL)
    # ══════════════════════════════════════════════════════════════
    print_section("5. MFE OF WINNERS — HOW MUCH DO WINNERS LEAVE ON THE TABLE?")

    if winners:
        win_mfes = [t["mfe_r"] for t in winners]
        win_realized = [t["pnl_rr"] for t in winners]

        print(f"\n  Mean MFE of winners:      {statistics.mean(win_mfes):>+.3f}R")
        print(f"  Mean realized R of wins:  {statistics.mean(win_realized):>+.3f}R")
        print(f"  Avg left on table:        {statistics.mean(win_mfes) - statistics.mean(win_realized):>+.3f}R")

    # ══════════════════════════════════════════════════════════════
    # 6. MFE vs EXIT — BY EXIT REASON
    # ══════════════════════════════════════════════════════════════
    print_section("6. EXCURSION PROFILES BY EXIT REASON")

    for reason, group in [("stop", stops), ("target", targets),
                          ("ema9trail", ema9_exits), ("time", time_exits),
                          ("eod", eods), ("opposing", opposing)]:
        if not group:
            continue
        mfes = [t["mfe_r"] for t in group]
        maes = [t["mae_r"] for t in group]
        pnls = [t["pnl_rr"] for t in group]

        print(f"\n  {reason.upper()} exits ({len(group)}):")
        print(f"    Mean MFE: {statistics.mean(mfes):>+.3f}R | "
              f"Mean MAE: {statistics.mean(maes):>.3f}R | "
              f"Mean PnL: {statistics.mean(pnls):>+.3f}R")

        # For stops specifically: show the MFE→MAE journey
        if reason == "stop":
            # Trades that went positive first
            went_positive = [t for t in group if t["mfe_r"] > 0.25]
            print(f"    Reached +0.25R before stopping: {len(went_positive)} ({len(went_positive)/len(group)*100:.1f}%)")
            if went_positive:
                print(f"    Their avg MFE: {statistics.mean(t['mfe_r'] for t in went_positive):+.3f}R")

    # ══════════════════════════════════════════════════════════════
    # 7. BY SETUP TYPE
    # ══════════════════════════════════════════════════════════════
    print_section("7. EXCURSION BY SETUP TYPE")

    setup_groups = defaultdict(list)
    for t in all_trades:
        setup_groups[t["signal"].setup_name].append(t)

    print(f"\n  {'Setup':<16} {'Trades':>6} {'MeanMFE':>8} {'MeanMAE':>8} {'MFE>1R%':>8} {'MedMFE':>8}")
    print(f"  {'-' * 56}")
    for name in sorted(setup_groups.keys()):
        group = setup_groups[name]
        mfes = [t["mfe_r"] for t in group]
        maes = [t["mae_r"] for t in group]
        pct_1r = sum(1 for m in mfes if m >= 1.0) / len(mfes) * 100
        print(f"  {name:<16} {len(group):>6} {statistics.mean(mfes):>+8.3f} "
              f"{statistics.mean(maes):>8.3f} {pct_1r:>7.1f}% {statistics.median(mfes):>+8.3f}")

    # ══════════════════════════════════════════════════════════════
    # 8. BY QUALITY SCORE
    # ══════════════════════════════════════════════════════════════
    print_section("8. EXCURSION BY QUALITY SCORE")

    q_groups = defaultdict(list)
    for t in all_trades:
        q_groups[t["signal"].quality_score].append(t)

    print(f"\n  {'Quality':>8} {'Trades':>6} {'MeanMFE':>8} {'MeanMAE':>8} {'MFE>1R%':>8} {'MeanPnL(R)':>11}")
    print(f"  {'-' * 55}")
    for q in sorted(q_groups.keys()):
        group = q_groups[q]
        mfes = [t["mfe_r"] for t in group]
        maes = [t["mae_r"] for t in group]
        pnls = [t["pnl_rr"] for t in group]
        pct_1r = sum(1 for m in mfes if m >= 1.0) / len(mfes) * 100
        print(f"  Q={q:<5} {len(group):>6} {statistics.mean(mfes):>+8.3f} "
              f"{statistics.mean(maes):>8.3f} {pct_1r:>7.1f}% {statistics.mean(pnls):>+11.3f}")

    # ══════════════════════════════════════════════════════════════
    # 9. REGIME = TRUE vs FALSE
    # ══════════════════════════════════════════════════════════════
    print_section("9. EXCURSION BY REGIME ALIGNMENT")

    for regime_val in [True, False]:
        group = [t for t in all_trades if t["signal"].fits_regime == regime_val]
        if not group:
            continue
        mfes = [t["mfe_r"] for t in group]
        maes = [t["mae_r"] for t in group]
        pnls = [t["pnl_rr"] for t in group]
        stops_in = [t for t in group if t["exit_reason"] == "stop"]
        stop_mfes = [t["mfe_r"] for t in stops_in] if stops_in else [0]

        print(f"\n  Regime={regime_val} ({len(group)} trades):")
        print(f"    Mean MFE: {statistics.mean(mfes):>+.3f}R | Mean MAE: {statistics.mean(maes):>.3f}R")
        print(f"    Mean PnL: {statistics.mean(pnls):>+.3f}R")
        print(f"    % reaching +1R: {sum(1 for m in mfes if m >= 1.0)/len(mfes)*100:.1f}%")
        if stops_in:
            print(f"    Stopped trades MFE: {statistics.mean(stop_mfes):>+.3f}R "
                  f"({sum(1 for m in stop_mfes if m >= 0.5)/len(stop_mfes)*100:.1f}% reached +0.5R first)")

    # ══════════════════════════════════════════════════════════════
    # 10. THE VERDICT
    # ══════════════════════════════════════════════════════════════
    print_section("10. DIAGNOSTIC SUMMARY")

    all_mfes = [t["mfe_r"] for t in all_trades]
    stop_mfes = [t["mfe_r"] for t in stops]
    pct_reach_half = sum(1 for m in all_mfes if m >= 0.5) / total * 100
    pct_reach_1 = sum(1 for m in all_mfes if m >= 1.0) / total * 100
    pct_reach_2 = sum(1 for m in all_mfes if m >= 2.0) / total * 100

    stop_pct_reach_half = sum(1 for m in stop_mfes if m >= 0.5) / len(stops) * 100 if stops else 0
    stop_pct_reach_1 = sum(1 for m in stop_mfes if m >= 1.0) / len(stops) * 100 if stops else 0

    print(f"\n  All trades reaching +0.5R:  {pct_reach_half:.1f}%")
    print(f"  All trades reaching +1.0R:  {pct_reach_1:.1f}%")
    print(f"  All trades reaching +2.0R:  {pct_reach_2:.1f}%")

    print(f"\n  Stopped trades that first reached +0.5R: {stop_pct_reach_half:.1f}%")
    print(f"  Stopped trades that first reached +1.0R:  {stop_pct_reach_1:.1f}%")

    # Classify the problem
    print(f"\n  DIAGNOSIS:")
    if stop_pct_reach_1 > 20:
        print(f"  → STOPS TOO TIGHT: {stop_pct_reach_1:.0f}% of stopped trades reached +1R first.")
        print(f"    These trades had the right idea but got shaken out.")
    elif stop_pct_reach_half > 30:
        print(f"  → STOPS POSSIBLY TIGHT: {stop_pct_reach_half:.0f}% of stopped trades reached +0.5R.")
        print(f"    Some favorable movement exists, but not enough to confirm stop width as the problem.")
    elif pct_reach_half < 40:
        print(f"  → ENTRIES MAY LACK EDGE: Only {pct_reach_half:.0f}% of all trades reach +0.5R.")
        print(f"    Price barely moves in the trade direction. Signal quality is the issue.")
    else:
        print(f"  → TARGETS POSSIBLY TOO FAR: Trades move favorably but don't reach target.")
        print(f"    Consider closer targets or a trailing exit.")

    if pct_reach_1 > 40 and pct_reach_2 < 15:
        print(f"  → TARGET GAP: {pct_reach_1:.0f}% reach +1R but only {pct_reach_2:.0f}% reach +2R.")
        print(f"    Target at +1R to +1.5R would capture more winners.")

    print(f"\n{'=' * 70}")


if __name__ == "__main__":
    main()
