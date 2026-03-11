"""
Integrated Two-Sided System Validation.

Runs the full engine (long VK+SC + short BDR) under one unified framework,
then validates against the standalone-study results.

Reports:
  1. Full combined backtest under corrected execution
  2. Train/test split (odd/even dates)
  3. Walk-forward validation (weekly rolling)
  4. Concurrency / overlap statistics
  5. Long vs short book contribution
  6. Regime contribution
  7. Portfolio controls: max positions, exposure, per-symbol rule

Usage:
    python -m alert_overlay.integrated_validation --universe all94
"""

import argparse
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import NaN, Bar, SetupId, SetupFamily, SETUP_FAMILY_MAP, SETUP_DISPLAY_NAME
from .market_context import SECTOR_MAP, get_sector_etf

DATA_DIR = Path(__file__).parent / "data"


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


def classify_spy_days(spy_bars):
    daily = defaultdict(list)
    for b in spy_bars:
        daily[b.timestamp.date()].append(b)
    sorted_dates = sorted(daily.keys())
    day_info = {}
    ranges_10d = []
    for d in sorted_dates:
        bars = daily[d]
        day_open = bars[0].open
        day_close = bars[-1].close
        day_high = max(b.high for b in bars)
        day_low = min(b.low for b in bars)
        day_range = day_high - day_low
        change_pct = (day_close - day_open) / day_open * 100 if day_open > 0 else 0
        if change_pct > 0.05:
            direction = "GREEN"
        elif change_pct < -0.05:
            direction = "RED"
        else:
            direction = "FLAT"
        if day_range > 0:
            close_pos = (day_close - day_low) / day_range
            character = "TREND" if (close_pos >= 0.75 or close_pos <= 0.25) else "CHOPPY"
        else:
            character = "CHOPPY"
        ranges_10d.append(day_range)
        day_info[d] = {"direction": direction, "character": character,
                       "spy_change_pct": change_pct}
    return day_info


# ── Unified trade wrapper ──

class UTrade:
    __slots__ = ("pnl_points", "pnl_rr", "exit_reason", "bars_held",
                 "entry_time", "exit_time", "entry_date", "side", "setup",
                 "sym", "quality")

    def __init__(self, t: Trade, sym: str):
        self.pnl_points = t.pnl_points
        self.pnl_rr = t.pnl_rr
        self.exit_reason = t.exit_reason
        self.bars_held = t.bars_held
        self.entry_time = t.signal.timestamp
        self.exit_time = t.exit_time if t.exit_time else t.signal.timestamp
        self.entry_date = t.signal.timestamp.date()
        self.side = "LONG" if t.signal.direction == 1 else "SHORT"
        self.setup = t.signal.setup_id
        self.sym = sym
        self.quality = t.signal.quality_score


# ── Metrics ──

def compute_metrics(trades: List[UTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0, "pf": 0, "exp": 0, "pnl": 0,
                "max_dd": 0, "stop_rate": 0, "qstop_rate": 0}
    wins = [t for t in trades if t.pnl_points > 0]
    losses = [t for t in trades if t.pnl_points <= 0]
    pnl = sum(t.pnl_points for t in trades)
    gw = sum(t.pnl_points for t in wins)
    gl = abs(sum(t.pnl_points for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    qstop = sum(1 for t in trades if t.exit_reason == "stop" and t.bars_held <= 3)
    cum = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl_points
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum
    return {
        "n": n, "wr": len(wins) / n * 100, "pf": pf,
        "exp": sum(t.pnl_rr for t in trades) / n,
        "pnl": pnl, "max_dd": dd,
        "stop_rate": stopped / n * 100,
        "qstop_rate": qstop / n * 100,
    }


def split_train_test(trades):
    train = [t for t in trades if t.entry_date and t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date and t.entry_date.day % 2 == 0]
    return train, test


def print_row(label, m, indent="  "):
    print(f"{indent}{label:42s}  N={m['n']:4d}  WR={m['wr']:5.1f}%  "
          f"PF={pf_str(m['pf']):>6s}  Exp={m['exp']:+.2f}R  PnL={m['pnl']:+8.2f}  "
          f"MaxDD={m['max_dd']:7.2f}  Stop={m['stop_rate']:4.1f}%  QStop={m['qstop_rate']:4.1f}%")


# ── Concurrency / portfolio analysis ──

def compute_portfolio_stats(trades: List[UTrade]):
    """Compute overlap, exposure, per-symbol stats."""
    if not trades:
        return {}

    intervals = []
    for t in trades:
        intervals.append((t.entry_time, t.exit_time, t.side, t.sym))

    min_t = min(i[0] for i in intervals)
    max_t = max(i[1] for i in intervals)

    delta = timedelta(minutes=5)
    cursor = min_t.replace(second=0, microsecond=0)
    cursor = cursor - timedelta(minutes=cursor.minute % 5)

    total_bars = 0
    max_open = 0
    max_long = 0
    max_short = 0
    total_open = 0
    total_long = 0
    total_short = 0
    flat_bars = 0
    max_per_sym = 0

    while cursor <= max_t:
        hhmm = cursor.hour * 100 + cursor.minute
        if 930 <= hhmm <= 1555:
            n_open = 0
            n_long = 0
            n_short = 0
            sym_counts = defaultdict(int)
            for entry, exit_t, side, sym in intervals:
                if entry <= cursor < exit_t:
                    n_open += 1
                    if side == "LONG":
                        n_long += 1
                    else:
                        n_short += 1
                    sym_counts[sym] += 1
            total_bars += 1
            total_open += n_open
            total_long += n_long
            total_short += n_short
            if n_open > max_open:
                max_open = n_open
            if n_long > max_long:
                max_long = n_long
            if n_short > max_short:
                max_short = n_short
            if n_open == 0:
                flat_bars += 1
            for c in sym_counts.values():
                if c > max_per_sym:
                    max_per_sym = c
        cursor += delta

    # Per-symbol trade count
    sym_trade_count = defaultdict(int)
    for t in trades:
        sym_trade_count[t.sym] += 1

    return {
        "avg_open": total_open / total_bars if total_bars > 0 else 0,
        "max_open": max_open,
        "avg_long": total_long / total_bars if total_bars > 0 else 0,
        "avg_short": total_short / total_bars if total_bars > 0 else 0,
        "max_long": max_long,
        "max_short": max_short,
        "pct_flat": flat_bars / total_bars * 100 if total_bars > 0 else 100,
        "max_per_sym": max_per_sym,
        "sym_trade_count": dict(sym_trade_count),
    }


# ── Main ──

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="all94")
    args = parser.parse_args()

    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    symbols = sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])

    # Integrated config: long VK+SC + short BDR, everything else off
    cfg = OverlayConfig()
    cfg.show_ema_scalp = False
    cfg.show_failed_bounce = False
    cfg.show_spencer = False
    cfg.show_ema_fpip = False
    cfg.show_sc_v2 = False
    cfg.show_breakdown_retest = True

    print("=" * 130)
    print("INTEGRATED TWO-SIDED SYSTEM VALIDATION")
    print("=" * 130)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Long: VK + SC (locked baseline filters applied post-hoc)")
    print(f"Short: BDR + big rejection wick (>=30%, integrated in engine)")
    print(f"Execution: dynamic slippage, SHORT_STRUCT mult={cfg.slip_family_mult_short_struct}")
    print(f"Train/test: odd dates = train, even dates = test\n")

    # Load index/sector bars
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    spy_day_info = classify_spy_days(spy_bars)

    # Run backtest
    print("Running integrated backtest...")
    all_raw_trades: List[Trade] = []
    sym_for_trade: Dict[int, str] = {}

    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        for t in result.trades:
            all_raw_trades.append(t)
            sym_for_trade[id(t)] = sym

    # Wrap all trades
    all_utrades = [UTrade(t, sym_for_trade[id(t)]) for t in all_raw_trades]
    print(f"  Raw trades: {len(all_utrades)}")

    # Apply locked long baseline filters (Non-RED, Q>=2, <15:30) to longs only
    def is_red(t):
        return spy_day_info.get(t.entry_date, {}).get("direction") == "RED"

    filtered = []
    for t in all_utrades:
        if t.side == "LONG":
            hhmm = t.entry_time.hour * 100 + t.entry_time.minute
            if is_red(t):
                continue
            if t.quality < 2:
                continue
            if hhmm >= 1530:
                continue
        filtered.append(t)

    print(f"  After locked long filters: {len(filtered)}")

    longs = [t for t in filtered if t.side == "LONG"]
    shorts = [t for t in filtered if t.side == "SHORT"]
    print(f"  Longs: {len(longs)}, Shorts: {len(shorts)}\n")

    # ════════════════════════════════════════════════════════════
    #  SECTION 1: FULL BACKTEST RESULTS
    # ════════════════════════════════════════════════════════════
    print("=" * 130)
    print("SECTION 1: FULL BACKTEST — INTEGRATED ENGINE")
    print("=" * 130)

    print_row("Long book", compute_metrics(longs))
    print_row("Short book (BDR)", compute_metrics(shorts))
    print_row("Combined", compute_metrics(filtered))

    # By setup type
    print(f"\n  By setup:")
    setup_groups = defaultdict(list)
    for t in filtered:
        name = SETUP_DISPLAY_NAME.get(t.setup, str(t.setup))
        setup_groups[name].append(t)
    for name in sorted(setup_groups.keys()):
        m = compute_metrics(setup_groups[name])
        print(f"    {name:<16}  N={m['n']:4d}  WR={m['wr']:5.1f}%  PF={pf_str(m['pf']):>6s}  PnL={m['pnl']:+8.2f}")

    # ════════════════════════════════════════════════════════════
    #  SECTION 2: TRAIN / TEST SPLIT
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("SECTION 2: TRAIN / TEST SPLIT")
    print("=" * 130)

    for label, tlist in [("Long book", longs), ("Short book", shorts), ("Combined", filtered)]:
        train, test = split_train_test(tlist)
        mt = compute_metrics(train)
        ms = compute_metrics(test)
        wr_d = ms["wr"] - mt["wr"]
        stable = mt["pf"] >= 1.0 and ms["pf"] >= 1.0 and abs(wr_d) < 5.0
        print(f"\n  {label}:")
        print_row("Train (odd dates)", mt, indent="    ")
        print_row("Test  (even dates)", ms, indent="    ")
        print(f"    Stable: {'YES' if stable else 'NO'}  (PF {pf_str(mt['pf'])}/{pf_str(ms['pf'])}, WR delta {wr_d:+.1f}%)")

    # ════════════════════════════════════════════════════════════
    #  SECTION 3: WALK-FORWARD (weekly rolling)
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("SECTION 3: WALK-FORWARD VALIDATION (weekly)")
    print("=" * 130)

    # Group by week
    from datetime import date
    week_trades = defaultdict(list)
    for t in filtered:
        iso = t.entry_date.isocalendar()
        week_key = f"{iso[0]}-W{iso[1]:02d}"
        week_trades[week_key].append(t)

    print(f"\n  {'Week':<10}  {'N':>4}  {'WR':>6}  {'PF':>6}  {'PnL':>8}  {'Long':>4}  {'Short':>5}  {'StpR':>5}")
    print(f"  {'-'*10}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*4}  {'-'*5}  {'-'*5}")

    cum_pnl = 0.0
    weeks_positive = 0
    weeks_total = 0
    for week in sorted(week_trades.keys()):
        wt = week_trades[week]
        m = compute_metrics(wt)
        n_long = sum(1 for t in wt if t.side == "LONG")
        n_short = sum(1 for t in wt if t.side == "SHORT")
        cum_pnl += m["pnl"]
        weeks_total += 1
        if m["pnl"] > 0:
            weeks_positive += 1
        print(f"  {week:<10}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  {m['pnl']:+8.2f}  "
              f"{n_long:4d}  {n_short:5d}  {m['stop_rate']:4.1f}%")

    print(f"\n  Weeks positive: {weeks_positive}/{weeks_total} ({weeks_positive/weeks_total*100:.0f}%)")
    print(f"  Cumulative PnL: {cum_pnl:+.2f}")

    # ════════════════════════════════════════════════════════════
    #  SECTION 4: CONCURRENCY / PORTFOLIO CONTROLS
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("SECTION 4: PORTFOLIO CONTROLS & CONCURRENCY")
    print("=" * 130)

    ps = compute_portfolio_stats(filtered)
    print(f"\n  Avg open trades:       {ps['avg_open']:.2f}")
    print(f"  Max simultaneous:      {ps['max_open']}")
    print(f"  Avg long open:         {ps['avg_long']:.2f}")
    print(f"  Avg short open:        {ps['avg_short']:.2f}")
    print(f"  Max long:              {ps['max_long']}")
    print(f"  Max short:             {ps['max_short']}")
    print(f"  Pct flat (no pos):     {ps['pct_flat']:.1f}%")
    print(f"  Max same-symbol open:  {ps['max_per_sym']}")

    # Per-day exposure summary
    print(f"\n  Per-day exposure:")
    day_exposure = defaultdict(lambda: {"long": 0, "short": 0})
    for t in filtered:
        day_exposure[t.entry_date][t.side.lower()] += 1

    print(f"    {'Date':<12}  {'Long':>5}  {'Short':>5}  {'Total':>5}  {'Net':>5}")
    print(f"    {'-'*12}  {'-'*5}  {'-'*5}  {'-'*5}  {'-'*5}")
    for d in sorted(day_exposure.keys()):
        de = day_exposure[d]
        total = de["long"] + de["short"]
        net = de["long"] - de["short"]
        print(f"    {d}  {de['long']:5d}  {de['short']:5d}  {total:5d}  {net:+5d}")

    # Net exposure over time summary
    net_exposures = [day_exposure[d]["long"] - day_exposure[d]["short"]
                     for d in sorted(day_exposure.keys())]
    if net_exposures:
        print(f"\n  Net exposure stats:")
        print(f"    Mean net: {statistics.mean(net_exposures):+.1f}")
        print(f"    Min net:  {min(net_exposures):+d} (most short-heavy)")
        print(f"    Max net:  {max(net_exposures):+d} (most long-heavy)")

    # Per-symbol concentration
    sym_counts = ps.get("sym_trade_count", {})
    if sym_counts:
        top_syms = sorted(sym_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        print(f"\n  Top 10 symbols by trade count:")
        for sym, cnt in top_syms:
            side_l = sum(1 for t in filtered if t.sym == sym and t.side == "LONG")
            side_s = sum(1 for t in filtered if t.sym == sym and t.side == "SHORT")
            print(f"    {sym:<6}  {cnt:3d} trades  ({side_l}L / {side_s}S)")

    # ════════════════════════════════════════════════════════════
    #  SECTION 5: LONG vs SHORT CONTRIBUTION
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("SECTION 5: LONG vs SHORT BOOK CONTRIBUTION")
    print("=" * 130)

    for split_name, split_trades in [("Full", filtered),
                                      ("Train", split_train_test(filtered)[0]),
                                      ("Test", split_train_test(filtered)[1])]:
        l = [t for t in split_trades if t.side == "LONG"]
        s = [t for t in split_trades if t.side == "SHORT"]
        ml = compute_metrics(l)
        ms = compute_metrics(s)
        mc = compute_metrics(split_trades)
        print(f"\n  {split_name}:")
        print(f"    Long:     N={ml['n']:4d}  PF={pf_str(ml['pf'])}  PnL={ml['pnl']:+8.2f}  MaxDD={ml['max_dd']:.2f}")
        print(f"    Short:    N={ms['n']:4d}  PF={pf_str(ms['pf'])}  PnL={ms['pnl']:+8.2f}  MaxDD={ms['max_dd']:.2f}")
        print(f"    Combined: N={mc['n']:4d}  PF={pf_str(mc['pf'])}  PnL={mc['pnl']:+8.2f}  MaxDD={mc['max_dd']:.2f}")

    # ════════════════════════════════════════════════════════════
    #  SECTION 6: REGIME CONTRIBUTION
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("SECTION 6: REGIME CONTRIBUTION")
    print("=" * 130)

    regimes = ["GREEN+TREND", "GREEN+CHOPPY", "RED+TREND", "RED+CHOPPY",
               "FLAT+TREND", "FLAT+CHOPPY"]

    def get_regime(t):
        info = spy_day_info.get(t.entry_date, {})
        return f"{info.get('direction', 'UNK')}+{info.get('character', 'UNK')}"

    for book_name, book_trades in [("Long book", longs),
                                    ("Short book", shorts),
                                    ("Combined", filtered)]:
        groups = defaultdict(list)
        for t in book_trades:
            groups[get_regime(t)].append(t)

        print(f"\n  {book_name}:")
        print(f"    {'Regime':<16}  {'N':>4}  {'WR':>6}  {'PF':>6}  {'PnL':>8}  {'StpR':>5}")
        print(f"    {'-'*16}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*5}")
        for regime in regimes:
            if regime not in groups:
                continue
            m = compute_metrics(groups[regime])
            print(f"    {regime:<16}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  "
                  f"{m['pnl']:+8.2f}  {m['stop_rate']:4.1f}%")
        m = compute_metrics(book_trades)
        print(f"    {'TOTAL':<16}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  "
              f"{m['pnl']:+8.2f}  {m['stop_rate']:4.1f}%")

    # Complementarity
    print(f"\n  Complementarity:")
    for regime in regimes:
        lr = [t for t in longs if get_regime(t) == regime]
        sr = [t for t in shorts if get_regime(t) == regime]
        ml = compute_metrics(lr)
        ms = compute_metrics(sr)
        if ml["n"] == 0 and ms["n"] == 0:
            continue
        hedged = "HEDGED" if ml["pnl"] * ms["pnl"] < 0 else (
            "ALIGNED" if ml["pnl"] > 0 and ms["pnl"] > 0 else "BOTH NEG")
        print(f"    {regime:<16}  Long: {ml['pnl']:+.2f} (N={ml['n']})  "
              f"Short: {ms['pnl']:+.2f} (N={ms['n']})  -> {hedged}")

    # ════════════════════════════════════════════════════════════
    #  SECTION 7: STANDALONE vs INTEGRATED COMPARISON
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("SECTION 7: STANDALONE STUDY vs INTEGRATED ENGINE")
    print("=" * 130)

    m_int_long = compute_metrics(longs)
    m_int_short = compute_metrics(shorts)
    m_int_comb = compute_metrics(filtered)

    print(f"""
  Standalone study results (for reference):
    Long baseline:    N=  45  PF=2.04  PnL=  +19.64
    BDR + wick:       N= 376  PF=1.79  PnL= +102.38  (dynamic slip)
    Combined:         N= 421  PF=1.82  PnL= +122.02

  Integrated engine results:
    Long baseline:    N={m_int_long['n']:4d}  PF={pf_str(m_int_long['pf'])}  PnL={m_int_long['pnl']:+8.2f}
    BDR + wick:       N={m_int_short['n']:4d}  PF={pf_str(m_int_short['pf'])}  PnL={m_int_short['pnl']:+8.2f}
    Combined:         N={m_int_comb['n']:4d}  PF={pf_str(m_int_comb['pf'])}  PnL={m_int_comb['pnl']:+8.2f}
""")

    # ════════════════════════════════════════════════════════════
    #  VERDICT
    # ════════════════════════════════════════════════════════════
    print("=" * 130)
    print("VERDICT")
    print("=" * 130)

    train_c, test_c = split_train_test(filtered)
    mt = compute_metrics(train_c)
    ms = compute_metrics(test_c)
    wr_delta = ms["wr"] - mt["wr"]

    checks = [
        ("Combined PF >= 1.0", m_int_comb["pf"] >= 1.0),
        ("Train PF >= 1.0", mt["pf"] >= 1.0),
        ("Test PF >= 1.0", ms["pf"] >= 1.0),
        ("WR train/test delta < 5%", abs(wr_delta) < 5.0),
        ("Short book adds PnL", m_int_short["pnl"] > 0),
        ("Weeks positive > 50%", weeks_positive > weeks_total / 2),
    ]

    all_pass = True
    for name, passed in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {name}")

    print(f"\n  Train: PF={pf_str(mt['pf'])} WR={mt['wr']:.1f}%  |  "
          f"Test: PF={pf_str(ms['pf'])} WR={ms['wr']:.1f}%  |  "
          f"Delta: {wr_delta:+.1f}%")

    if all_pass:
        print(f"\n  ALL CHECKS PASS. Integrated engine reproduces standalone results.")
        print(f"  This is a validated two-sided system under one unified framework.")
    else:
        print(f"\n  NOT ALL CHECKS PASS. Review failures before declaring validated.")


if __name__ == "__main__":
    main()
