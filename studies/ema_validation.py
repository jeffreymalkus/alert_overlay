"""
EMA_RECLAIM validation — path-dependency and realism analysis.

Since 1-minute data is not available, we use 5-minute OHLC to measure:
1. MAE (Maximum Adverse Excursion) — how far price goes against before any profit
2. MFE (Maximum Favorable Excursion) — best possible exit price in the trade
3. Intrabar stop vulnerability — would stop have been hit before profitable move?
4. Entry-to-low analysis — how tight is the edge between entry and first pullback?
5. Comparison: 5-min EMA_RECLAIM results under various slippage assumptions

This answers the key question: "Does EMA_RECLAIM survive realistic execution?"
"""

import os
import sys
from collections import defaultdict
from pathlib import Path

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import SetupId, SetupFamily, SETUP_FAMILY_MAP, NaN
from ..market_context import get_sector_etf, SECTOR_MAP

DATA_DIR = Path(__file__).parent.parent / "data"


def analyze_ema_reclaim_trades(spy_bars=None, qqq_bars=None, sector_bars_dict=None):
    """
    Run EMA_RECLAIM in isolation and compute MAE/MFE for each trade.
    """
    cfg = OverlayConfig()
    cfg.show_ema_confirm = False
    cfg.use_dynamic_slippage = True  # realistic slippage

    # Isolate EMA_RECLAIM: disable everything else
    cfg_iso = OverlayConfig()
    cfg_iso.show_reversal_setups = False
    cfg_iso.show_trend_setups = False
    cfg_iso.show_ema_retest = False
    cfg_iso.show_ema_mean_rev = False
    cfg_iso.show_ema_pullback = False
    cfg_iso.show_second_chance = False
    cfg_iso.show_spencer = False
    cfg_iso.show_failed_bounce = False
    cfg_iso.show_ema_scalp = True
    cfg_iso.show_ema_confirm = False
    cfg_iso.use_dynamic_slippage = True

    all_data = []

    for f in sorted(os.listdir(DATA_DIR)):
        if not f.endswith('_5min.csv') or f in ('SPY_5min.csv', 'QQQ_5min.csv'):
            continue
        sym = f.replace('_5min.csv', '')
        bars = load_bars_from_csv(str(DATA_DIR / f))

        kw = {"cfg": cfg_iso}
        if spy_bars:
            kw["spy_bars"] = spy_bars
            kw["qqq_bars"] = qqq_bars
            sec_etf = get_sector_etf(sym)
            if sec_etf and sector_bars_dict and sec_etf in sector_bars_dict:
                kw["sector_bars"] = sector_bars_dict[sec_etf]

        result = run_backtest(bars, **kw)

        # Now compute MAE/MFE for each trade by walking through bars
        for trade in result.trades:
            if trade.signal.setup_id != SetupId.EMA_RECLAIM:
                continue

            sig = trade.signal
            entry = sig.entry_price
            stop = sig.stop_price
            direction = sig.direction
            risk = abs(entry - stop)

            # Find the trade's bars (from entry bar to exit bar)
            entry_time = sig.timestamp
            exit_time = trade.exit_time

            # Walk bars to compute MAE/MFE
            mae = 0.0  # worst adverse move (positive = against us)
            mfe = 0.0  # best favorable move (positive = in our favor)
            in_trade = False
            bars_to_mae = 0
            bars_count = 0

            for b in bars:
                if b.timestamp == entry_time:
                    in_trade = True
                    continue  # skip entry bar

                if not in_trade:
                    continue

                if exit_time and b.timestamp > exit_time:
                    break

                bars_count += 1

                if direction == 1:  # long
                    adverse = entry - b.low
                    favorable = b.high - entry
                else:  # short
                    adverse = b.high - entry
                    favorable = entry - b.low

                if adverse > mae:
                    mae = adverse
                    bars_to_mae = bars_count
                mfe = max(mfe, favorable)

            # Intrabar stop vulnerability: MAE > distance to stop?
            stop_dist = abs(entry - stop)
            stop_vulnerable = mae >= stop_dist * 0.8  # within 80% of stop

            all_data.append({
                "sym": sym,
                "time": str(entry_time),
                "direction": direction,
                "entry": entry,
                "stop": stop,
                "risk": risk,
                "mae": mae,
                "mfe": mfe,
                "mae_r": mae / risk if risk > 0 else 0,
                "mfe_r": mfe / risk if risk > 0 else 0,
                "pnl": trade.pnl_points,
                "pnl_r": trade.pnl_rr,
                "exit_reason": trade.exit_reason,
                "bars_held": trade.bars_held,
                "bars_to_mae": bars_to_mae,
                "stop_vulnerable": stop_vulnerable,
                "stop_dist_pct": stop_dist / entry * 100 if entry > 0 else 0,
            })

    return all_data


def run_slippage_sensitivity(spy_bars=None, qqq_bars=None, sector_bars_dict=None):
    """
    Run EMA_RECLAIM under multiple slippage assumptions to measure degradation.
    """
    slippage_levels = [
        ("Flat $0.02", 0.02, False),
        ("Flat $0.05", 0.05, False),
        ("Flat $0.10", 0.10, False),
        ("Dynamic (default)", 0.0, True),
        ("Dynamic 2x BPS", 0.0, True),  # will override slip_bps
    ]

    results_table = []

    for label, flat_slip, use_dyn in slippage_levels:
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
        cfg.use_dynamic_slippage = use_dyn
        if not use_dyn:
            cfg.slippage_per_side = flat_slip
        if label == "Dynamic 2x BPS":
            cfg.slip_bps = 0.0008  # double default

        all_trades = []
        for f in sorted(os.listdir(DATA_DIR)):
            if not f.endswith('_5min.csv') or f in ('SPY_5min.csv', 'QQQ_5min.csv'):
                continue
            sym = f.replace('_5min.csv', '')
            bars = load_bars_from_csv(str(DATA_DIR / f))

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
                    all_trades.append(t)

        n = len(all_trades)
        wins = sum(1 for t in all_trades if t.pnl_points > 0)
        pnl = sum(t.pnl_points for t in all_trades)
        stops = sum(1 for t in all_trades if t.exit_reason == "stop")
        gw = sum(t.pnl_points for t in all_trades if t.pnl_points > 0)
        gl = abs(sum(t.pnl_points for t in all_trades if t.pnl_points <= 0))
        pf = gw / gl if gl > 0 else float("inf")
        exp = pnl / n if n > 0 else 0
        wr = wins / n * 100 if n > 0 else 0

        results_table.append({
            "label": label, "n": n, "wr": wr, "pnl": pnl, "exp": exp,
            "pf": pf, "stops": stops, "stop_pct": stops / n * 100 if n > 0 else 0,
        })

    return results_table


def main():
    # Load market data
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv")) if (DATA_DIR / "SPY_5min.csv").exists() else None
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv")) if (DATA_DIR / "QQQ_5min.csv").exists() else None

    sector_bars_dict = {}
    sector_etfs = set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    # ── Section 1: MAE/MFE Analysis ──
    print(f"\n{'='*90}")
    print("  EMA_RECLAIM VALIDATION — MAE/MFE Path Dependency Analysis")
    print(f"{'='*90}")

    data = analyze_ema_reclaim_trades(spy_bars, qqq_bars, sector_bars_dict)

    if not data:
        print("\n  No EMA_RECLAIM trades found.")
        return

    n = len(data)
    wins = [d for d in data if d["pnl"] > 0]
    losses = [d for d in data if d["pnl"] <= 0]

    # MAE statistics
    mae_vals = [d["mae_r"] for d in data]
    mfe_vals = [d["mfe_r"] for d in data]

    import statistics
    print(f"\n  Trades: {n} ({len(wins)} wins, {len(losses)} losses)")
    print(f"\n  MAE (Maximum Adverse Excursion) — in R multiples:")
    print(f"    Mean:   {statistics.mean(mae_vals):.2f}R")
    print(f"    Median: {statistics.median(mae_vals):.2f}R")
    print(f"    P75:    {sorted(mae_vals)[int(n*0.75)]:.2f}R")
    print(f"    P90:    {sorted(mae_vals)[int(n*0.9)]:.2f}R")
    print(f"    Max:    {max(mae_vals):.2f}R")

    print(f"\n  MFE (Maximum Favorable Excursion) — in R multiples:")
    print(f"    Mean:   {statistics.mean(mfe_vals):.2f}R")
    print(f"    Median: {statistics.median(mfe_vals):.2f}R")
    print(f"    P75:    {sorted(mfe_vals)[int(n*0.75)]:.2f}R")
    print(f"    Max:    {max(mfe_vals):.2f}R")

    # Stop vulnerability
    vulnerable = [d for d in data if d["stop_vulnerable"]]
    print(f"\n  Stop vulnerability (MAE >= 80% of stop distance):")
    print(f"    {len(vulnerable)}/{n} trades ({len(vulnerable)/n*100:.0f}%) came within 80% of stop")

    # For wins: how close did they get to stop before winning?
    win_mae = [d["mae_r"] for d in wins]
    if win_mae:
        print(f"\n  Winning trades MAE:")
        print(f"    Mean:   {statistics.mean(win_mae):.2f}R (avg drawdown before profit)")
        print(f"    Median: {statistics.median(win_mae):.2f}R")
        win_vuln = [d for d in wins if d["stop_vulnerable"]]
        print(f"    Stop-vulnerable wins: {len(win_vuln)}/{len(wins)} ({len(win_vuln)/len(wins)*100:.0f}%)")

    # Bars to MAE
    btm = [d["bars_to_mae"] for d in data if d["bars_to_mae"] > 0]
    if btm:
        print(f"\n  Bars to MAE:")
        print(f"    Mean:   {statistics.mean(btm):.1f} bars")
        print(f"    Median: {statistics.median(btm):.1f} bars")

    # Stop distance as % of price
    sdp = [d["stop_dist_pct"] for d in data]
    print(f"\n  Stop distance as % of price:")
    print(f"    Mean:   {statistics.mean(sdp):.3f}%")
    print(f"    Median: {statistics.median(sdp):.3f}%")

    # Per-trade detail for worst cases
    print(f"\n  Worst MAE trades (top 5):")
    print(f"  {'Sym':<6} {'Time':<20} {'Dir':>4} {'MAE_R':>6} {'MFE_R':>6} {'PnL':>8} {'Exit':>8}")
    for d in sorted(data, key=lambda x: -x["mae_r"])[:5]:
        dir_s = "LONG" if d["direction"] == 1 else "SHRT"
        print(f"  {d['sym']:<6} {d['time'][:19]:<20} {dir_s:>4} {d['mae_r']:>5.2f}R {d['mfe_r']:>5.2f}R {d['pnl']:>+7.2f} {d['exit_reason']:>8}")

    # ── Section 2: Slippage Sensitivity ──
    print(f"\n{'='*90}")
    print("  EMA_RECLAIM — Slippage Sensitivity Analysis")
    print(f"{'='*90}")

    slip_results = run_slippage_sensitivity(spy_bars, qqq_bars, sector_bars_dict)

    print(f"\n  {'Slippage Model':<22} {'N':>5} {'WR%':>6} {'Exp':>7} {'PF':>6} {'PnL':>9} {'Stop%':>6}")
    print(f"  {'-'*65}")
    for r in slip_results:
        pf_s = f"{r['pf']:.2f}" if r['pf'] < 999 else "inf"
        print(f"  {r['label']:<22} {r['n']:>5} {r['wr']:>5.1f}% {r['exp']:>+6.3f} {pf_s:>6} {r['pnl']:>+8.2f} {r['stop_pct']:>5.0f}%")

    # Degradation analysis
    if len(slip_results) >= 2:
        base = slip_results[0]  # flat $0.02
        print(f"\n  Degradation from flat $0.02 baseline:")
        for r in slip_results[1:]:
            pnl_delta = r["pnl"] - base["pnl"]
            wr_delta = r["wr"] - base["wr"]
            print(f"    {r['label']:<22} PnL: {pnl_delta:>+7.2f}  WR: {wr_delta:>+5.1f}%")


if __name__ == "__main__":
    main()
