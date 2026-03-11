"""
Reconciliation Study — 4 Portfolio Definitions, R-Only Metrics.

Portfolios:
  1. LONG BOOK ONLY          — VK + SC longs, Non-RED, Q>=2, <15:30
  2. LONG + FROZEN SHORT     — #1 + BDR big-wick shorts on RED+TREND AM only
  3. LONG + BROAD SHORT      — #1 + BDR big-wick shorts (all regimes, AM)
  4. SHORT BOOK ONLY         — BDR big-wick shorts (all regimes, AM)

All metrics in R. No points anywhere.

Usage:
    python -m alert_overlay.reconciliation_study
"""

from collections import defaultdict
from pathlib import Path
from typing import List, Optional

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import SetupId, SETUP_DISPLAY_NAME
from .market_context import SECTOR_MAP, get_sector_etf
from .breakdown_retest_study import (
    BDRTrade, compute_bar_contexts, scan_for_breakdowns,
    simulate_exit, build_market_context_map,
)

DATA_DIR = Path(__file__).parent / "data"


# ── Unified trade wrapper (R-only) ──

class UTrade:
    __slots__ = ("pnl_rr", "exit_reason", "bars_held", "entry_time",
                 "entry_date", "side", "setup_name", "symbol")

    def __init__(self, pnl_rr, exit_reason, bars_held, entry_time, side, setup_name, symbol):
        self.pnl_rr = pnl_rr
        self.exit_reason = exit_reason
        self.bars_held = bars_held
        self.entry_time = entry_time
        self.entry_date = entry_time.date() if entry_time else None
        self.side = side
        self.setup_name = setup_name
        self.symbol = symbol


def wrap_engine(t: Trade) -> UTrade:
    return UTrade(
        pnl_rr=t.pnl_rr,
        exit_reason=t.exit_reason,
        bars_held=t.bars_held,
        entry_time=t.signal.timestamp,
        side="LONG" if t.signal.direction == 1 else "SHORT",
        setup_name=SETUP_DISPLAY_NAME.get(t.signal.setup_id, str(t.signal.setup_id)),
        symbol=t.signal.symbol if hasattr(t.signal, 'symbol') else "",
    )


def wrap_bdr(t: BDRTrade) -> UTrade:
    return UTrade(
        pnl_rr=t.pnl_rr,
        exit_reason=t.exit_reason,
        bars_held=t.bars_held,
        entry_time=t.entry_time,
        side="SHORT",
        setup_name="BDR SHORT",
        symbol=t.sym,
    )


# ── SPY day classification ──

def classify_spy_days(spy_bars):
    daily = defaultdict(list)
    for b in spy_bars:
        daily[b.timestamp.date()].append(b)
    day_info = {}
    ranges = []
    sorted_dates = sorted(daily.keys())
    for d in sorted_dates:
        bars = daily[d]
        o = bars[0].open
        c = bars[-1].close
        h = max(b.high for b in bars)
        lo = min(b.low for b in bars)
        chg = (c - o) / o * 100 if o > 0 else 0
        if chg > 0.05:
            direction = "GREEN"
        elif chg < -0.05:
            direction = "RED"
        else:
            direction = "FLAT"
        day_range = h - lo
        ranges.append(day_range)
        avg10 = sum(ranges[-10:]) / len(ranges[-10:]) if ranges else day_range
        trend = "TREND" if day_range >= avg10 * 0.7 else "CHOPPY"
        day_info[d] = {"direction": direction, "trend": trend,
                       "regime": f"{direction}+{trend}"}
    return day_info


# ── Metrics (R-only) ──

def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


def compute_metrics(trades: List[UTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0, "pf_r": 0, "exp_r": 0, "total_r": 0,
                "max_dd_r": 0, "stop_rate": 0}
    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in trades)
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    # Max DD in R
    cum = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl_rr
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum
    return {
        "n": n,
        "wr": len(wins) / n * 100,
        "pf_r": pf,
        "exp_r": total_r / n,
        "total_r": total_r,
        "max_dd_r": dd,
        "stop_rate": stopped / n * 100,
    }


def split_train_test(trades):
    train = [t for t in trades if t.entry_date and t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date and t.entry_date.day % 2 == 0]
    return train, test


def exclude_best_day(trades):
    daily_r = defaultdict(float)
    for t in trades:
        if t.entry_date:
            daily_r[t.entry_date] += t.pnl_rr
    if not daily_r:
        return trades
    best = max(daily_r, key=daily_r.get)
    return [t for t in trades if t.entry_date != best], best, daily_r[best]


def exclude_top_symbol(trades):
    sym_r = defaultdict(float)
    for t in trades:
        sym_r[t.symbol] += t.pnl_rr
    if not sym_r:
        return trades
    top = max(sym_r, key=sym_r.get)
    return [t for t in trades if t.symbol != top], top, sym_r[top]


def print_header(label):
    print(f"\n  {label}")
    print(f"  {'':38s}  {'N':>5s}  {'WR':>6s}  {'PF(R)':>6s}  {'Exp(R)':>8s}  "
          f"{'TotalR':>8s}  {'MaxDD(R)':>8s}  {'Stop%':>6s}")
    print(f"  {'-'*38}  {'-'*5}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*6}")


def print_row(label, m, indent="  "):
    print(f"{indent}{label:38s}  {m['n']:5d}  {m['wr']:5.1f}%  {pf_str(m['pf_r']):>6s}  "
          f"{m['exp_r']:+7.3f}R  {m['total_r']:+7.2f}R  {m['max_dd_r']:7.2f}R  "
          f"{m['stop_rate']:5.1f}%")


# ── Main ──

def main():
    # Build symbol list
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    symbols = sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])

    print("=" * 120)
    print("RECONCILIATION STUDY — R-ONLY METRICS, 4 PORTFOLIO DEFINITIONS")
    print("=" * 120)
    print(f"Universe: {len(symbols)} symbols")
    print(f"Long book: VK + SC, Non-RED, Q>=2, <15:30")
    print(f"Frozen short: BDR + wick>=30% + RED+TREND + AM only")
    print(f"Broad short: BDR + wick>=30% + AM (all regimes)")
    print(f"Train/test: odd dates = train, even dates = test")

    # ── Load market data ──
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    spy_day_info = classify_spy_days(spy_bars)

    # ── Run engine backtests ──
    cfg = OverlayConfig()
    cfg.show_ema_scalp = False
    cfg.show_failed_bounce = False
    cfg.show_spencer = False
    cfg.show_ema_fpip = False
    cfg.show_sc_v2 = False

    print("\nRunning engine backtests...")
    raw_engine_trades: List[Trade] = []
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
            # Backfill symbol from bars filename
            if not hasattr(t.signal, 'symbol') or not t.signal.symbol:
                t.signal.symbol = sym
            raw_engine_trades.append(t)

    # Apply locked long filters
    def is_red(t):
        return spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == "RED"
    def signal_hhmm(t):
        ts = t.signal.timestamp
        return ts.hour * 100 + ts.minute

    locked_long_raw = [t for t in raw_engine_trades
                       if not is_red(t)
                       and t.signal.quality_score >= 2
                       and signal_hhmm(t) < 1530
                       and t.signal.direction == 1]  # longs only

    long_trades = [wrap_engine(t) for t in locked_long_raw]
    print(f"  Long book trades: {len(long_trades)}")

    # ── Run BDR scanner ──
    print("Running BDR scanner...")
    all_bdr: List[BDRTrade] = []
    for sym in symbols:
        fpath = DATA_DIR / f"{sym}_5min.csv"
        if not fpath.exists():
            continue
        bars = load_bars_from_csv(str(fpath))
        if not bars or len(bars) < 30:
            continue
        ctxs = compute_bar_contexts(bars)
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        mkt_map = build_market_context_map(spy_bars, qqq_bars, bars, sec_bars)
        candidates = scan_for_breakdowns(bars, ctxs, mkt_map, sym)
        for t in candidates:
            simulate_exit(t, bars, ctxs)
        all_bdr.extend(candidates)

    # BDR big-wick + AM filter (common to both frozen and broad)
    bdr_wick_am = [t for t in all_bdr
                   if t.rejection_wick_pct >= 0.30
                   and t.time_bucket == "AM"]

    # Frozen short: RED+TREND only
    frozen_short_raw = [t for t in bdr_wick_am
                        if spy_day_info.get(t.entry_time.date(), {}).get("regime") == "RED+TREND"]
    frozen_short = [wrap_bdr(t) for t in frozen_short_raw]

    # Broad short: all regimes
    broad_short = [wrap_bdr(t) for t in bdr_wick_am]

    print(f"  BDR wick+AM total: {len(bdr_wick_am)}")
    print(f"  Frozen short (RED+TREND): {len(frozen_short)}")
    print(f"  Broad short (all regimes): {len(broad_short)}")

    # ── Build 4 portfolios ──
    portfolios = {
        "1. LONG BOOK ONLY": long_trades,
        "2. LONG + FROZEN SHORT": long_trades + frozen_short,
        "3. LONG + BROAD SHORT": long_trades + broad_short,
        "4. SHORT BOOK ONLY": broad_short,
    }

    # ════════════════════════════════════════════════════════════
    #  HEADLINE TABLE
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("HEADLINE COMPARISON (R-only)")
    print("=" * 120)
    print_header("Full sample")
    for label, trades in portfolios.items():
        print_row(label, compute_metrics(trades))

    # ════════════════════════════════════════════════════════════
    #  TRAIN / TEST
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("TRAIN / TEST SPLIT")
    print("=" * 120)

    print(f"\n  {'Portfolio':38s}  {'Trn N':>5s}  {'Trn PF(R)':>9s}  {'Trn Exp(R)':>10s}  "
          f"{'Tst N':>5s}  {'Tst PF(R)':>9s}  {'Tst Exp(R)':>10s}  {'Stable?':>7s}")
    print(f"  {'-'*38}  {'-'*5}  {'-'*9}  {'-'*10}  {'-'*5}  {'-'*9}  {'-'*10}  {'-'*7}")

    for label, trades in portfolios.items():
        train, test = split_train_test(trades)
        tr = compute_metrics(train)
        te = compute_metrics(test)
        stable = "YES" if (tr["pf_r"] >= 1.0 and te["pf_r"] >= 1.0
                           and abs(te["wr"] - tr["wr"]) < 5.0) else "NO"
        print(f"  {label:38s}  {tr['n']:5d}  {pf_str(tr['pf_r']):>9s}  {tr['exp_r']:+9.3f}R  "
              f"{te['n']:5d}  {pf_str(te['pf_r']):>9s}  {te['exp_r']:+9.3f}R  {stable:>7s}")

    # ════════════════════════════════════════════════════════════
    #  ROBUSTNESS
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("ROBUSTNESS — EXCLUDING BEST DAY AND TOP SYMBOL")
    print("=" * 120)

    print(f"\n  {'Portfolio':38s}  {'Full TotalR':>11s}  {'ExBestDay':>10s}  "
          f"{'Day':>12s}  {'ExTopSym':>10s}  {'Sym':>6s}")
    print(f"  {'-'*38}  {'-'*11}  {'-'*10}  {'-'*12}  {'-'*10}  {'-'*6}")

    for label, trades in portfolios.items():
        m = compute_metrics(trades)
        ex_day, best_day, day_r = exclude_best_day(trades)
        ex_sym, top_sym, sym_r = exclude_top_symbol(trades)
        m_exday = compute_metrics(ex_day)
        m_exsym = compute_metrics(ex_sym)
        print(f"  {label:38s}  {m['total_r']:+10.2f}R  {m_exday['total_r']:+9.2f}R  "
              f"{str(best_day):>12s}  {m_exsym['total_r']:+9.2f}R  {top_sym:>6s}")

    # ════════════════════════════════════════════════════════════
    #  SETUP-LEVEL CONTRIBUTION IN R
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SETUP-LEVEL CONTRIBUTION (R)")
    print("=" * 120)

    # Focus setups
    focus_setups = {"VWAP KISS", "2ND CHANCE", "BDR SHORT",
                    "BOX REV", "MANIP FADE", "VWAP SEP", "9EMA SEP",
                    "EMA PULL", "9EMA RETEST"}

    for port_label, trades in portfolios.items():
        setup_groups: dict = defaultdict(list)
        for t in trades:
            setup_groups[t.setup_name].append(t)

        print(f"\n  {port_label}:")
        print(f"    {'Setup':20s}  {'N':>5s}  {'WR':>6s}  {'PF(R)':>6s}  {'Exp(R)':>8s}  "
              f"{'TotalR':>8s}  {'Stop%':>6s}")
        print(f"    {'-'*20}  {'-'*5}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*6}")

        for setup_name in sorted(setup_groups.keys()):
            strades = setup_groups[setup_name]
            m = compute_metrics(strades)
            marker = " ***" if setup_name in {"VWAP KISS", "2ND CHANCE", "BDR SHORT"} else ""
            print(f"    {setup_name:20s}  {m['n']:5d}  {m['wr']:5.1f}%  {pf_str(m['pf_r']):>6s}  "
                  f"{m['exp_r']:+7.3f}R  {m['total_r']:+7.2f}R  {m['stop_rate']:5.1f}%{marker}")

    # ════════════════════════════════════════════════════════════
    #  KEY QUESTIONS
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("KEY QUESTIONS")
    print("=" * 120)

    # SC drag on long book
    long_by_setup = defaultdict(list)
    for t in long_trades:
        long_by_setup[t.setup_name].append(t)

    vk_setups = ["VWAP KISS"]
    sc_setups = ["2ND CHANCE"]
    # VK family = all setups that are VK-derived (VWAP KISS, etc.)
    # SC family = 2ND CHANCE
    # Everything else = "other long setups"
    vk_r = sum(t.pnl_rr for t in long_trades if t.setup_name == "VWAP KISS")
    sc_r = sum(t.pnl_rr for t in long_trades if t.setup_name == "2ND CHANCE")
    other_long_r = sum(t.pnl_rr for t in long_trades
                       if t.setup_name not in ("VWAP KISS", "2ND CHANCE"))
    total_long_r = sum(t.pnl_rr for t in long_trades)

    n_vk = sum(1 for t in long_trades if t.setup_name == "VWAP KISS")
    n_sc = sum(1 for t in long_trades if t.setup_name == "2ND CHANCE")
    n_other = len(long_trades) - n_vk - n_sc

    print(f"""
  Q1: Is SC the main drag on the long book?
    VK trades:      N={n_vk:3d}  TotalR={vk_r:+7.2f}R  Exp={vk_r/n_vk if n_vk else 0:+.3f}R
    SC trades:      N={n_sc:3d}  TotalR={sc_r:+7.2f}R  Exp={sc_r/n_sc if n_sc else 0:+.3f}R
    Other longs:    N={n_other:3d}  TotalR={other_long_r:+7.2f}R  Exp={other_long_r/n_other if n_other else 0:+.3f}R
    Long book total: N={len(long_trades):3d}  TotalR={total_long_r:+7.2f}R
""")
    if n_sc > 0 and sc_r < 0 and abs(sc_r) > abs(total_long_r) * 0.5:
        print("    ANSWER: YES — SC is a significant drag. Removing it would improve the long book.")
    elif n_sc > 0 and sc_r < 0:
        print("    ANSWER: SC is negative but not the dominant drag.")
    else:
        print("    ANSWER: SC is not a drag (positive or zero R contribution).")

    # VK edge
    if n_vk > 0:
        vk_trades_list = [t for t in long_trades if t.setup_name == "VWAP KISS"]
        vk_m = compute_metrics(vk_trades_list)
        vk_train, vk_test = split_train_test(vk_trades_list)
        vk_trm = compute_metrics(vk_train)
        vk_tem = compute_metrics(vk_test)
        print(f"""  Q2: Does VWAP KISS have a real edge?
    Full:  N={vk_m['n']:3d}  PF(R)={pf_str(vk_m['pf_r'])}  Exp={vk_m['exp_r']:+.3f}R  TotalR={vk_m['total_r']:+.2f}R
    Train: N={vk_trm['n']:3d}  PF(R)={pf_str(vk_trm['pf_r'])}  Exp={vk_trm['exp_r']:+.3f}R
    Test:  N={vk_tem['n']:3d}  PF(R)={pf_str(vk_tem['pf_r'])}  Exp={vk_tem['exp_r']:+.3f}R
""")
        if vk_m["pf_r"] > 1.0 and vk_trm["pf_r"] > 1.0 and vk_tem["pf_r"] > 1.0:
            print("    ANSWER: YES — VK has a real edge, stable across train/test.")
        elif vk_m["pf_r"] > 1.0:
            print("    ANSWER: MAYBE — VK is profitable overall but unstable in splits.")
        else:
            print("    ANSWER: NO — VK does not show a consistent R edge.")

    # Broad short in R
    broad_m = compute_metrics(broad_short)
    broad_train, broad_test = split_train_test(broad_short)
    broad_trm = compute_metrics(broad_train)
    broad_tem = compute_metrics(broad_test)
    print(f"""
  Q3: Is the broad short book truly positive in R?
    Full:  N={broad_m['n']:3d}  PF(R)={pf_str(broad_m['pf_r'])}  Exp={broad_m['exp_r']:+.3f}R  TotalR={broad_m['total_r']:+.2f}R  MaxDD={broad_m['max_dd_r']:.2f}R
    Train: N={broad_trm['n']:3d}  PF(R)={pf_str(broad_trm['pf_r'])}  Exp={broad_trm['exp_r']:+.3f}R  TotalR={broad_trm['total_r']:+.2f}R
    Test:  N={broad_tem['n']:3d}  PF(R)={pf_str(broad_tem['pf_r'])}  Exp={broad_tem['exp_r']:+.3f}R  TotalR={broad_tem['total_r']:+.2f}R
""")
    if broad_m["total_r"] > 0 and broad_m["pf_r"] > 1.0:
        if broad_trm["pf_r"] > 1.0 and broad_tem["pf_r"] > 1.0:
            print("    ANSWER: YES — Broad short book is positive in R and stable.")
        else:
            print("    ANSWER: PARTIAL — Positive in R overall but unstable in splits.")
    else:
        print("    ANSWER: NO — Broad short book is NOT positive in R.")

    # Frozen vs broad short comparison
    frozen_m = compute_metrics(frozen_short)
    print(f"""
  Q3b: Frozen vs Broad short comparison:
    Frozen (RED+TREND): N={frozen_m['n']:3d}  PF(R)={pf_str(frozen_m['pf_r'])}  Exp={frozen_m['exp_r']:+.3f}R  TotalR={frozen_m['total_r']:+.2f}R
    Broad (all regime):  N={broad_m['n']:3d}  PF(R)={pf_str(broad_m['pf_r'])}  Exp={broad_m['exp_r']:+.3f}R  TotalR={broad_m['total_r']:+.2f}R
""")
    if frozen_m["exp_r"] > broad_m["exp_r"] * 1.2:
        print("    Frozen has materially better per-trade edge. Broad adds volume but dilutes.")
    elif broad_m["total_r"] > frozen_m["total_r"]:
        print("    Broad generates more total R despite lower per-trade edge.")
    else:
        print("    Frozen is better on both per-trade and total R.")

    print("\n" + "=" * 120)
    print("STUDY COMPLETE")
    print("=" * 120)


if __name__ == "__main__":
    main()
