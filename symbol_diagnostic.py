"""
Symbol & Setup Diagnostic — identifies which symbols and setups carry the edge.

Runs full backtest at Q>=1 (no regime filter) to see ALL trades, then
slices by setup type, symbol category, and volatility bucket.

Usage:
    python -m alert_overlay.symbol_diagnostic
"""

import glob
import math
import os
from collections import defaultdict
from pathlib import Path

from .backtest import load_bars_from_csv, run_backtest, BacktestResult, Trade
from .config import OverlayConfig
from .models import SetupId


DATA_DIR = Path(__file__).parent / "data"

# ── Symbol categories ──
LEVERAGED_ETFS = {"SPXL", "SPXS", "TQQQ", "SQQQ", "TECL", "TNA", "SOXL",
                  "FAS", "NUGT", "JNUG", "LABU", "FNGU", "WEBL", "NVDL", "TSLL", "UPRO"}
CRYPTO_PLAYS = {"COIN", "MSTR", "MARA"}
INDEX_ETFS = {"SPY", "QQQ"}
MEGA_CAP = {"AAPL", "MSFT", "GOOG", "AMZN", "META", "NVDA", "NFLX", "TSLA"}
SMALL_SPEC = {"NIO", "RIVN", "SOFI", "PLTR", "BA"}


def categorize(sym: str) -> str:
    if sym in CRYPTO_PLAYS:
        return "Crypto"
    if sym in LEVERAGED_ETFS:
        return "Leveraged ETF"
    if sym in INDEX_ETFS:
        return "Index ETF"
    if sym in MEGA_CAP:
        return "Mega Cap"
    if sym in SMALL_SPEC:
        return "Small/Spec"
    return "Other"


def compute_atr_pct(bars) -> float:
    """Average true range as % of price over the bar set."""
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i-1].close
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    avg_tr = sum(trs) / len(trs) if trs else 0
    avg_price = sum(b.close for b in bars) / len(bars)
    return (avg_tr / avg_price * 100) if avg_price > 0 else 0


def pf(trades):
    gross_w = sum(t.pnl_points for t in trades if t.pnl_points > 0)
    gross_l = abs(sum(t.pnl_points for t in trades if t.pnl_points <= 0))
    return gross_w / gross_l if gross_l > 0 else float('inf')


def wr(trades):
    if not trades:
        return 0.0
    return sum(1 for t in trades if t.pnl_points > 0) / len(trades) * 100


def pnl(trades):
    return sum(t.pnl_points for t in trades)


def main():
    csv_files = sorted(glob.glob(str(DATA_DIR / "*_5min.csv")))
    if not csv_files:
        print("No cached CSV files found in data/")
        return

    # Run backtest with NO filters (Q>=1, no regime requirement)
    cfg_open = OverlayConfig()
    cfg_open.min_quality = 1
    cfg_open.require_regime = False

    # Run backtest with production filters (Q>=4, regime required)
    cfg_prod = OverlayConfig()  # uses new defaults: Q>=4, require_regime=True

    all_trades_open = []
    all_trades_prod = []
    symbol_data = {}  # sym -> {bars, atr_pct, trades_open, trades_prod, category}

    print("Running diagnostics on all cached symbols...\n")

    for f in csv_files:
        sym = os.path.basename(f).replace("_5min.csv", "")
        bars = load_bars_from_csv(f)
        if len(bars) < 100:
            continue

        atr_pct = compute_atr_pct(bars)
        avg_price = sum(b.close for b in bars) / len(bars)

        res_open = run_backtest(bars, cfg=cfg_open)
        res_prod = run_backtest(bars, cfg=cfg_prod)

        for t in res_open.trades:
            t._symbol = sym
        for t in res_prod.trades:
            t._symbol = sym

        all_trades_open.extend(res_open.trades)
        all_trades_prod.extend(res_prod.trades)

        symbol_data[sym] = {
            "bars": len(bars),
            "atr_pct": atr_pct,
            "avg_price": avg_price,
            "trades_open": res_open.trades,
            "trades_prod": res_prod.trades,
            "category": categorize(sym),
        }

    # ═══════════════════════════════════════
    # A) SETUP CONTRIBUTION (all trades, no filter)
    # ═══════════════════════════════════════
    print("=" * 90)
    print("  A) SETUP CONTRIBUTION — All trades (Q>=1, no regime filter)")
    print("=" * 90)

    setup_trades = defaultdict(list)
    for t in all_trades_open:
        setup_trades[t.signal.setup_id.name].append(t)

    print(f"\n  {'Setup':<15} {'Trades':>7} {'WR%':>7} {'PF':>7} {'PnL':>10} {'AvgR':>7} {'% of PnL':>9}")
    print("  " + "-" * 63)
    total_pnl_open = pnl(all_trades_open)
    for name in sorted(setup_trades.keys(), key=lambda n: pnl(setup_trades[n]), reverse=True):
        tr = setup_trades[name]
        p = pnl(tr)
        print(f"  {name:<15} {len(tr):>7} {wr(tr):>6.1f}% {pf(tr):>7.2f} {p:>+10.2f} "
              f"{sum(t.pnl_rr for t in tr)/len(tr):>+7.2f} {p/total_pnl_open*100 if total_pnl_open else 0:>8.1f}%")
    print(f"\n  TOTAL: {len(all_trades_open)} trades, {wr(all_trades_open):.1f}% WR, "
          f"PF {pf(all_trades_open):.2f}, {total_pnl_open:+.2f} pts")

    # Same but with Q>=4 + regime
    print(f"\n  --- With Q>=4 + Regime ---")
    setup_trades_prod = defaultdict(list)
    for t in all_trades_prod:
        setup_trades_prod[t.signal.setup_id.name].append(t)

    total_pnl_prod = pnl(all_trades_prod)
    print(f"  {'Setup':<15} {'Trades':>7} {'WR%':>7} {'PF':>7} {'PnL':>10} {'AvgR':>7}")
    print("  " + "-" * 55)
    for name in sorted(setup_trades_prod.keys(), key=lambda n: pnl(setup_trades_prod[n]), reverse=True):
        tr = setup_trades_prod[name]
        p = pnl(tr)
        print(f"  {name:<15} {len(tr):>7} {wr(tr):>6.1f}% {pf(tr):>7.2f} {p:>+10.2f} "
              f"{sum(t.pnl_rr for t in tr)/len(tr):>+7.2f}")
    print(f"\n  TOTAL: {len(all_trades_prod)} trades, {wr(all_trades_prod):.1f}% WR, "
          f"PF {pf(all_trades_prod):.2f}, {total_pnl_prod:+.2f} pts")

    # ═══════════════════════════════════════
    # B) CATEGORY BREAKDOWN
    # ═══════════════════════════════════════
    print("\n" + "=" * 90)
    print("  B) SYMBOL CATEGORY BREAKDOWN")
    print("=" * 90)

    cat_trades_open = defaultdict(list)
    cat_trades_prod = defaultdict(list)
    for t in all_trades_open:
        cat = categorize(t._symbol)
        cat_trades_open[cat].append(t)
    for t in all_trades_prod:
        cat = categorize(t._symbol)
        cat_trades_prod[cat].append(t)

    print(f"\n  All trades (Q>=1):")
    print(f"  {'Category':<18} {'Syms':>5} {'Trades':>7} {'WR%':>7} {'PF':>7} {'PnL':>10}")
    print("  " + "-" * 55)
    for cat in sorted(cat_trades_open.keys(), key=lambda c: pnl(cat_trades_open[c]), reverse=True):
        tr = cat_trades_open[cat]
        syms = len(set(t._symbol for t in tr))
        print(f"  {cat:<18} {syms:>5} {len(tr):>7} {wr(tr):>6.1f}% {pf(tr):>7.2f} {pnl(tr):>+10.2f}")

    print(f"\n  Q>=4 + Regime:")
    print(f"  {'Category':<18} {'Syms':>5} {'Trades':>7} {'WR%':>7} {'PF':>7} {'PnL':>10}")
    print("  " + "-" * 55)
    for cat in sorted(cat_trades_prod.keys(), key=lambda c: pnl(cat_trades_prod[c]), reverse=True):
        tr = cat_trades_prod[cat]
        syms = len(set(t._symbol for t in tr))
        print(f"  {cat:<18} {syms:>5} {len(tr):>7} {wr(tr):>6.1f}% {pf(tr):>7.2f} {pnl(tr):>+10.2f}")

    # ═══════════════════════════════════════
    # C) VOLATILITY CONDITIONING
    # ═══════════════════════════════════════
    print("\n" + "=" * 90)
    print("  C) VOLATILITY CONDITIONING (ATR% of price)")
    print("=" * 90)

    # Sort symbols by ATR%
    syms_by_atr = sorted(symbol_data.items(), key=lambda x: x[1]["atr_pct"])

    # Create volatility buckets
    vol_buckets = {"Low (<0.5%)": [], "Medium (0.5-1.5%)": [], "High (1.5-3%)": [], "Extreme (>3%)": []}

    print(f"\n  {'Symbol':<8} {'Cat':<16} {'AvgPx':>8} {'ATR%':>7} {'Trds(all)':>10} {'PnL(all)':>10} "
          f"{'Trds(Q4)':>10} {'PnL(Q4)':>10}")
    print("  " + "-" * 90)

    for sym, d in syms_by_atr:
        atr = d["atr_pct"]
        if atr < 0.5:
            bucket = "Low (<0.5%)"
        elif atr < 1.5:
            bucket = "Medium (0.5-1.5%)"
        elif atr < 3.0:
            bucket = "High (1.5-3%)"
        else:
            bucket = "Extreme (>3%)"

        vol_buckets[bucket].append(sym)
        t_open = d["trades_open"]
        t_prod = d["trades_prod"]
        print(f"  {sym:<8} {d['category']:<16} {d['avg_price']:>8.1f} {atr:>6.2f}% "
              f"{len(t_open):>10} {pnl(t_open):>+10.2f} {len(t_prod):>10} {pnl(t_prod):>+10.2f}")

    print(f"\n  Volatility Bucket Summary (Q>=4 + Regime):")
    print(f"  {'Bucket':<22} {'Symbols':>8} {'Trades':>8} {'WR%':>7} {'PF':>7} {'PnL':>10}")
    print("  " + "-" * 63)
    for bucket_name in ["Low (<0.5%)", "Medium (0.5-1.5%)", "High (1.5-3%)", "Extreme (>3%)"]:
        syms_in = vol_buckets[bucket_name]
        if not syms_in:
            continue
        bucket_trades = []
        for sym in syms_in:
            bucket_trades.extend(symbol_data[sym]["trades_prod"])
        if bucket_trades:
            print(f"  {bucket_name:<22} {len(syms_in):>8} {len(bucket_trades):>8} "
                  f"{wr(bucket_trades):>6.1f}% {pf(bucket_trades):>7.2f} {pnl(bucket_trades):>+10.2f}")
        else:
            print(f"  {bucket_name:<22} {len(syms_in):>8}        0       -       -          -")

    # ═══════════════════════════════════════
    # D) SYMBOL-LEVEL RANKING (Q>=4 + Regime)
    # ═══════════════════════════════════════
    print("\n" + "=" * 90)
    print("  D) SYMBOL RANKING — Q>=4 + Regime")
    print("=" * 90)

    sym_results = []
    for sym, d in symbol_data.items():
        tr = d["trades_prod"]
        sym_results.append((sym, d["category"], d["atr_pct"], len(tr), wr(tr),
                           pf(tr) if tr else 0, pnl(tr)))

    sym_results.sort(key=lambda x: x[6], reverse=True)

    print(f"\n  {'Symbol':<8} {'Cat':<16} {'ATR%':>7} {'Trades':>7} {'WR%':>7} {'PF':>7} {'PnL':>10}")
    print("  " + "-" * 65)
    for sym, cat, atr, cnt, w, p, pl in sym_results:
        if cnt == 0:
            print(f"  {sym:<8} {cat:<16} {atr:>6.2f}%       0       -       -          -")
        else:
            print(f"  {sym:<8} {cat:<16} {atr:>6.2f}% {cnt:>7} {w:>6.1f}% {p:>7.2f} {pl:>+10.2f}")

    # ═══════════════════════════════════════
    # E) CUT RECOMMENDATIONS
    # ═══════════════════════════════════════
    print("\n" + "=" * 90)
    print("  E) CUT RECOMMENDATIONS")
    print("=" * 90)

    # Criteria for cut: (1) crypto, (2) leveraged with negative PnL, (3) extreme vol with negative PnL
    cuts = []
    keeps = []
    reviews = []

    for sym, d in symbol_data.items():
        cat = d["category"]
        tr_prod = d["trades_prod"]
        p = pnl(tr_prod)
        atr = d["atr_pct"]

        if sym in CRYPTO_PLAYS:
            cuts.append((sym, "Crypto play — doesn't follow equity market conventions"))
        elif cat == "Leveraged ETF" and len(tr_prod) >= 3 and p < 0:
            reviews.append((sym, f"Leveraged ETF, negative PnL ({p:+.2f}), ATR {atr:.2f}%"))
        elif atr > 3.0 and len(tr_prod) >= 3 and p < 0:
            reviews.append((sym, f"Extreme volatility ({atr:.2f}%), negative PnL ({p:+.2f})"))
        elif len(tr_prod) >= 3 and p > 0:
            keeps.append((sym, f"Profitable under Q4+regime ({p:+.2f} pts, {len(tr_prod)} trades)"))
        elif len(tr_prod) < 3:
            reviews.append((sym, f"Insufficient trades ({len(tr_prod)}) for assessment"))

    print("\n  DEFINITE CUTS:")
    for sym, reason in cuts:
        print(f"    {sym:<8} — {reason}")

    print("\n  REVIEW (needs more data):")
    for sym, reason in reviews:
        print(f"    {sym:<8} — {reason}")

    print("\n  KEEP (profitable under Q4+regime):")
    for sym, reason in keeps:
        print(f"    {sym:<8} — {reason}")

    print("\n" + "=" * 90)


if __name__ == "__main__":
    main()
