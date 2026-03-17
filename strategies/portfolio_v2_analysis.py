"""
Portfolio v2 Analysis — merged metrics for ORH v2 integration.

Computes:
1. Existing portfolio (9 strategies, 5-min date range)
2. Portfolio + ORH Mode A only
3. Portfolio + ORH Mode A + Mode B
4. Overlap/correlation: ORH Mode A vs existing strategies (same day/symbol)
5. Trades/day and drawdown analysis
"""

import csv
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import List, Dict, Tuple

OUTPUT_DIR = Path("alert_overlay/outputs")


def load_trades(csv_path: str) -> List[dict]:
    """Load trades from CSV."""
    trades = []
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            pnl = float(row.get("pnl_rr", 0))
            d = row.get("date", "")
            trades.append({
                "date": d,
                "symbol": row.get("symbol", ""),
                "strategy": row.get("setup", row.get("strategy", "")),
                "side": row.get("side", ""),
                "pnl": pnl,
                "exit": row.get("exit_reason", ""),
                "bars": int(row.get("bars_held", 0)),
                "regime": row.get("regime", ""),
            })
    return trades


def compute_metrics(trades: List[dict], label: str) -> dict:
    """Compute standard metrics for a trade set."""
    if not trades:
        return {"label": label, "N": 0}

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    flat = [p for p in pnls if p == 0]

    gross_win = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0.001
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    total_r = sum(pnls)
    exp = total_r / len(pnls) if pnls else 0
    wr = len(wins) / len(pnls) * 100 if pnls else 0

    # Drawdown
    cum = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    # Trades per day
    dates = sorted(set(t["date"] for t in trades))
    trades_per_day = len(trades) / len(dates) if dates else 0

    # Exit breakdown
    stops = sum(1 for t in trades if t["exit"] == "stop")
    targets = sum(1 for t in trades if t["exit"] == "target")
    time_exits = sum(1 for t in trades if t["exit"] in ("time", "eod"))

    return {
        "label": label,
        "N": len(trades),
        "PF": round(pf, 2),
        "Exp": round(exp, 3),
        "TotalR": round(total_r, 1),
        "WR": round(wr, 1),
        "MaxDD": round(max_dd, 1),
        "Days": len(dates),
        "Trades/Day": round(trades_per_day, 2),
        "Stop%": round(stops / len(trades) * 100, 1),
        "Tgt%": round(targets / len(trades) * 100, 1),
        "Time%": round(time_exits / len(trades) * 100, 1),
        "AvgBars": round(sum(t["bars"] for t in trades) / len(trades), 1),
    }


def compute_monthly(trades: List[dict]) -> Dict[str, dict]:
    """Monthly breakdown."""
    months = defaultdict(list)
    for t in trades:
        ym = t["date"][:7]
        months[ym].append(t["pnl"])

    result = {}
    cum = 0
    for ym in sorted(months.keys()):
        pnls = months[ym]
        r = sum(pnls)
        cum += r
        result[ym] = {"N": len(pnls), "R": round(r, 1), "cum": round(cum, 1)}
    return result


def overlap_analysis(orh_a_trades: List[dict], portfolio_trades: List[dict]):
    """Analyze how ORH Mode A trades overlap with existing portfolio."""
    # Build date → symbols with trades in portfolio
    port_day_syms = defaultdict(set)
    port_day_strats = defaultdict(set)
    for t in portfolio_trades:
        port_day_syms[t["date"]].add(t["symbol"])
        port_day_strats[t["date"]].add(t["strategy"])

    # Check each ORH Mode A trade
    same_day_same_sym = 0
    same_day_diff_sym = 0
    unique_day = 0
    same_day_regime = defaultdict(int)

    for t in orh_a_trades:
        d = t["date"]
        if d in port_day_syms:
            if t["symbol"] in port_day_syms[d]:
                same_day_same_sym += 1
            else:
                same_day_diff_sym += 1
        else:
            unique_day += 1
        same_day_regime[t.get("regime", "?")] += 1

    # Portfolio activity on ORH Mode A dates
    orh_dates = set(t["date"] for t in orh_a_trades)
    port_on_orh_dates = [t for t in portfolio_trades if t["date"] in orh_dates]
    port_pnl_on_orh_dates = sum(t["pnl"] for t in port_on_orh_dates)

    return {
        "orh_a_count": len(orh_a_trades),
        "same_day_same_sym": same_day_same_sym,
        "same_day_diff_sym": same_day_diff_sym,
        "unique_day": unique_day,
        "regime_breakdown": dict(same_day_regime),
        "portfolio_trades_on_orh_dates": len(port_on_orh_dates),
        "portfolio_pnl_on_orh_dates": round(port_pnl_on_orh_dates, 1),
        "orh_dates_count": len(orh_dates),
    }


def main():
    # ── Load trades ──
    combined_csv = OUTPUT_DIR / "replay_strategy_combined.csv"
    orh_v2_csv = OUTPUT_DIR / "replay_orh_fbo_v2.csv"

    if not combined_csv.exists():
        print(f"ERROR: {combined_csv} not found. Run main replay first.")
        return
    if not orh_v2_csv.exists():
        print(f"ERROR: {orh_v2_csv} not found. Run v2 replay first.")
        return

    portfolio_trades = load_trades(str(combined_csv))
    orh_v2_trades = load_trades(str(orh_v2_csv))

    # Split ORH v2 into Mode A and Mode B
    orh_a = [t for t in orh_v2_trades if "V2_A" in t["strategy"]]
    orh_b = [t for t in orh_v2_trades if "V2_B" in t["strategy"]]

    # ── Date range alignment ──
    # Portfolio uses 5-min data (wider range). ORH v2 uses 1-min (Sep 2025+).
    # For fair comparison, restrict portfolio to 1-min date range.
    orh_dates = sorted(set(t["date"] for t in orh_v2_trades))
    if orh_dates:
        min_date = orh_dates[0]
        max_date = orh_dates[-1]
        print(f"  ORH v2 date range: {min_date} → {max_date}")
        portfolio_aligned = [t for t in portfolio_trades if min_date <= t["date"] <= max_date]
        print(f"  Portfolio aligned: {len(portfolio_aligned)} trades (from {len(portfolio_trades)} total)")
    else:
        portfolio_aligned = portfolio_trades

    # ── Compute metrics ──
    print(f"\n{'=' * 100}")
    print("PORTFOLIO COMPARISON")
    print(f"{'=' * 100}\n")

    m_port = compute_metrics(portfolio_aligned, "Portfolio (9 strats, aligned dates)")
    m_port_a = compute_metrics(portfolio_aligned + orh_a, "Portfolio + ORH Mode A")
    m_port_ab = compute_metrics(portfolio_aligned + orh_a + orh_b, "Portfolio + ORH Mode A + B")
    m_orh_a = compute_metrics(orh_a, "ORH Mode A (standalone)")
    m_orh_b = compute_metrics(orh_b, "ORH Mode B (standalone)")

    header = f"  {'Label':<42} {'N':>4}  {'PF':>5}  {'Exp':>7}  {'TotalR':>7}  {'WR':>5}  {'MaxDD':>6}  {'Days':>4}  {'T/Day':>5}  {'Stop%':>5}  {'Tgt%':>5}  {'Time%':>5}"
    sep = f"  {'-' * 42} {'-' * 4}  {'-' * 5}  {'-' * 7}  {'-' * 7}  {'-' * 5}  {'-' * 6}  {'-' * 4}  {'-' * 5}  {'-' * 5}  {'-' * 5}  {'-' * 5}"

    print(header)
    print(sep)
    for m in [m_port, m_port_a, m_port_ab, m_orh_a, m_orh_b]:
        if m["N"] == 0:
            continue
        print(f"  {m['label']:<42} {m['N']:>4}  {m['PF']:>5.2f}  {m['Exp']:>+7.3f}  {m['TotalR']:>+7.1f}  {m['WR']:>5.1f}  {m['MaxDD']:>6.1f}  {m['Days']:>4}  {m['Trades/Day']:>5.2f}  {m['Stop%']:>5.1f}  {m['Tgt%']:>5.1f}  {m['Time%']:>5.1f}")

    # ── Monthly comparison ──
    print(f"\n{'=' * 100}")
    print("MONTHLY COMPARISON")
    print(f"{'=' * 100}\n")

    monthly_port = compute_monthly(portfolio_aligned)
    monthly_port_a = compute_monthly(portfolio_aligned + orh_a)
    monthly_port_ab = compute_monthly(portfolio_aligned + orh_a + orh_b)

    all_months = sorted(set(list(monthly_port.keys()) + list(monthly_port_a.keys()) + list(monthly_port_ab.keys())))

    print(f"  {'Month':<10}  {'Port N':>6}  {'Port R':>7}  {'Port cum':>8}  │  {'+A N':>5}  {'+A R':>7}  {'+A cum':>8}  │  {'+AB N':>5}  {'+AB R':>7}  {'+AB cum':>8}")
    print(f"  {'-' * 10}  {'-' * 6}  {'-' * 7}  {'-' * 8}  │  {'-' * 5}  {'-' * 7}  {'-' * 8}  │  {'-' * 5}  {'-' * 7}  {'-' * 8}")

    for ym in all_months:
        p = monthly_port.get(ym, {"N": 0, "R": 0, "cum": 0})
        pa = monthly_port_a.get(ym, {"N": 0, "R": 0, "cum": 0})
        pab = monthly_port_ab.get(ym, {"N": 0, "R": 0, "cum": 0})
        print(f"  {ym:<10}  {p['N']:>6}  {p['R']:>+7.1f}  {p['cum']:>+8.1f}  │  {pa['N']:>5}  {pa['R']:>+7.1f}  {pa['cum']:>+8.1f}  │  {pab['N']:>5}  {pab['R']:>+7.1f}  {pab['cum']:>+8.1f}")

    # ── Overlap analysis ──
    print(f"\n{'=' * 100}")
    print("OVERLAP ANALYSIS — ORH Mode A vs Existing Portfolio")
    print(f"{'=' * 100}\n")

    ov = overlap_analysis(orh_a, portfolio_aligned)
    print(f"  ORH Mode A trades: {ov['orh_a_count']}")
    print(f"  Same day + same symbol as portfolio trade: {ov['same_day_same_sym']}")
    print(f"  Same day + different symbol: {ov['same_day_diff_sym']}")
    print(f"  ORH Mode A on day with NO portfolio trades: {ov['unique_day']}")
    print(f"  Regime breakdown: {ov['regime_breakdown']}")
    print(f"\n  ORH Mode A active on {ov['orh_dates_count']} unique days")
    print(f"  Portfolio had {ov['portfolio_trades_on_orh_dates']} trades on those same days → {ov['portfolio_pnl_on_orh_dates']:+.1f}R")

    # ── Per-strategy breakdown on ORH Mode A dates ──
    orh_a_dates = set(t["date"] for t in orh_a)
    strat_on_dates = defaultdict(list)
    for t in portfolio_aligned:
        if t["date"] in orh_a_dates:
            strat_on_dates[t["strategy"]].append(t["pnl"])

    if strat_on_dates:
        print(f"\n  Portfolio strategy performance on ORH Mode A RED days:")
        for strat in sorted(strat_on_dates.keys()):
            pnls = strat_on_dates[strat]
            r = sum(pnls)
            print(f"    {strat:<20} N={len(pnls):>2}  R={r:>+5.1f}")

    # ── Regime distribution ──
    print(f"\n{'=' * 100}")
    print("REGIME DISTRIBUTION — ORH Mode A vs Portfolio")
    print(f"{'=' * 100}\n")

    port_regime = defaultdict(list)
    for t in portfolio_aligned:
        # Infer regime from date correlation
        port_regime["all"].append(t["pnl"])

    # Portfolio by direction
    port_long = [t for t in portfolio_aligned if t["side"] == "LONG"]
    port_short = [t for t in portfolio_aligned if t["side"] == "SHORT"]
    print(f"  Portfolio longs:  N={len(port_long)}, R={sum(t['pnl'] for t in port_long):+.1f}")
    print(f"  Portfolio shorts: N={len(port_short)}, R={sum(t['pnl'] for t in port_short):+.1f}")
    print(f"  ORH Mode A:      N={len(orh_a)}, R={sum(t['pnl'] for t in orh_a):+.1f} (100% SHORT, 100% RED)")
    print(f"  ORH Mode B:      N={len(orh_b)}, R={sum(t['pnl'] for t in orh_b):+.1f} (100% SHORT, ~94% RED)")

    # ── Correlation: daily PnL ──
    print(f"\n{'=' * 100}")
    print("DAILY PNL CORRELATION — ORH Mode A vs Portfolio")
    print(f"{'=' * 100}\n")

    port_daily = defaultdict(float)
    for t in portfolio_aligned:
        port_daily[t["date"]] += t["pnl"]
    orh_a_daily = defaultdict(float)
    for t in orh_a:
        orh_a_daily[t["date"]] += t["pnl"]

    common_dates = sorted(set(port_daily.keys()) & set(orh_a_daily.keys()))
    if common_dates:
        # Show daily PnL side by side
        print(f"  {'Date':<12} {'Port R':>7}  {'ORH-A R':>7}  {'Combined':>8}")
        print(f"  {'-' * 12} {'-' * 7}  {'-' * 7}  {'-' * 8}")
        for d in common_dates:
            pr = port_daily[d]
            ar = orh_a_daily[d]
            print(f"  {d:<12} {pr:>+7.1f}  {ar:>+7.1f}  {pr + ar:>+8.1f}")

        # Correlation direction
        same_sign = sum(1 for d in common_dates if (port_daily[d] > 0) == (orh_a_daily[d] > 0) and port_daily[d] != 0 and orh_a_daily[d] != 0)
        opp_sign = sum(1 for d in common_dates if (port_daily[d] > 0) != (orh_a_daily[d] > 0) and port_daily[d] != 0 and orh_a_daily[d] != 0)
        print(f"\n  Same-direction days: {same_sign}")
        print(f"  Opposite-direction days: {opp_sign}")
        print(f"  ORH Mode A diversification ratio: {opp_sign / (same_sign + opp_sign):.0%}" if (same_sign + opp_sign) > 0 else "  No overlap")
    else:
        print("  No common trading dates (ORH Mode A fires on days with no portfolio trades)")

    # Days where ORH Mode A fires but portfolio doesn't
    orh_only_dates = set(orh_a_daily.keys()) - set(port_daily.keys())
    if orh_only_dates:
        print(f"\n  ORH Mode A fires on {len(orh_only_dates)} dates with NO portfolio trades:")
        for d in sorted(orh_only_dates):
            print(f"    {d}  ORH-A R={orh_a_daily[d]:+.1f}")


if __name__ == "__main__":
    main()
