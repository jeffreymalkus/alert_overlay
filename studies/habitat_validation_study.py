"""
Habitat Validation Study — STATIC vs IN_PLAY vs BOTH (Full R-First Metrics)

Once you have a real in-play history (even partial), run this study to
determine whether the IN_PLAY habitat adds genuine edge or just volume.

Metrics per group:
  - Trades (N)
  - PF(R) — profit factor in R-multiples
  - Expectancy(R) — average R per trade
  - Total R — cumulative R
  - Max DD(R) — peak-to-trough drawdown in R
  - Train/Test PF(R) — odd/even date split
  - Ex-Best-Day — PF(R) with best day removed
  - Ex-Top-Symbol — PF(R) with top symbol removed

Broken out by:
  - Universe segment (STATIC, IN_PLAY, BOTH, ALL)
  - Setup (per-setup within each universe)
  - Direction (LONG / SHORT per universe)
  - Regime (GREEN / RED per universe)

Usage:
    python -m alert_overlay.habitat_validation_study \\
        --static watchlist.txt --in-play in_play.txt

    # Quick sanity check (same list = all BOTH):
    python -m alert_overlay.habitat_validation_study \\
        --static watchlist.txt --in-play watchlist.txt
"""

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import NaN, SetupId, SETUP_DISPLAY_NAME
from ..market_context import SECTOR_MAP, get_sector_etf

DATA_DIR = Path(__file__).parent.parent / "data"


# ── Universe assignment ──

def load_symbol_list(path: str) -> Set[str]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Symbol list not found: {path}")
    symbols = set()
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            symbols.add(line.upper())
    return symbols


def assign_universe(sym: str, static: Set[str], in_play: Set[str]) -> str:
    if sym in static and sym in in_play:
        return "BOTH"
    elif sym in in_play:
        return "IN_PLAY"
    return "STATIC"


def load_snapshots(snapshot_dir: str) -> Dict[str, Set[str]]:
    """Load all daily in-play snapshots from a directory.

    Returns: {date_str: set_of_symbols} for each YYYY-MM-DD.txt file.
    """
    snap_dir = Path(snapshot_dir)
    if not snap_dir.exists():
        return {}
    snapshots: Dict[str, Set[str]] = {}
    for f in sorted(snap_dir.glob("*.txt")):
        date_str = f.stem  # e.g. "2026-03-10"
        symbols = set()
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                symbols.add(line.upper())
        if symbols:
            snapshots[date_str] = symbols
    return snapshots


def assign_universe_dated(sym: str, trade_date_str: str,
                          static: Set[str],
                          snapshots: Dict[str, Set[str]]) -> str:
    """Date-aware universe assignment using snapshot history.

    If snapshots are available for the trade date, use them.
    Otherwise fall back to STATIC.
    """
    in_play_that_day = snapshots.get(trade_date_str, set())
    in_static = sym in static
    in_play = sym in in_play_that_day
    if in_static and in_play:
        return "BOTH"
    elif in_play:
        return "IN_PLAY"
    return "STATIC"


# ── Extended trade wrapper ──

class HTrade:
    """Trade with universe, regime, and date metadata for habitat analysis."""
    __slots__ = (
        "pnl_rr", "exit_reason", "bars_held", "entry_time", "entry_date",
        "side", "setup", "universe", "symbol", "regime",
    )

    def __init__(self, t: Trade, universe: str, symbol: str,
                 spy_day_color: str = "FLAT"):
        self.pnl_rr = t.pnl_rr
        self.exit_reason = t.exit_reason
        self.bars_held = t.bars_held
        self.entry_time = t.signal.timestamp
        self.entry_date = t.signal.timestamp.date() if t.signal.timestamp else None
        self.side = "LONG" if t.signal.direction == 1 else "SHORT"
        self.setup = t.signal.setup_id
        self.universe = universe
        self.symbol = symbol
        self.regime = spy_day_color


# ── Metrics ──

def pf_str(pf: float) -> str:
    return f"{pf:.2f}" if pf < 999 else "inf"


def compute_metrics(trades: List[HTrade]) -> dict:
    """Full R-first metrics suite."""
    n = len(trades)
    if n == 0:
        return {
            "n": 0, "wr": 0, "pf": 0, "exp": 0, "total_r": 0,
            "max_dd_r": 0, "train_pf": 0, "test_pf": 0,
            "ex_best_day_pf": 0, "ex_top_sym_pf": 0,
            "stop_rate": 0, "qstop_rate": 0,
        }

    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in trades)
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")

    # Max drawdown in R
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: (x.entry_date or x.entry_time, x.entry_time)):
        cum += t.pnl_rr
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    # Train/test: odd dates = train, even = test
    train = [t for t in trades if t.entry_date and t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date and t.entry_date.day % 2 == 0]
    train_pf = _pf(train)
    test_pf = _pf(test)

    # Ex-best-day: remove the single best calendar day
    day_r: Dict[str, float] = defaultdict(float)
    for t in trades:
        if t.entry_date:
            day_r[str(t.entry_date)] += t.pnl_rr
    if day_r:
        best_day = max(day_r, key=day_r.get)
        ex_best = [t for t in trades if str(t.entry_date) != best_day]
    else:
        ex_best = trades
    ex_best_day_pf = _pf(ex_best)

    # Ex-top-symbol: remove the single best symbol
    sym_r: Dict[str, float] = defaultdict(float)
    for t in trades:
        sym_r[t.symbol] += t.pnl_rr
    if sym_r:
        top_sym = max(sym_r, key=sym_r.get)
        ex_top = [t for t in trades if t.symbol != top_sym]
    else:
        ex_top = trades
    ex_top_sym_pf = _pf(ex_top)

    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    qstop = sum(1 for t in trades if t.exit_reason == "stop" and t.bars_held <= 3)

    return {
        "n": n,
        "wr": len(wins) / n * 100,
        "pf": pf,
        "exp": total_r / n,
        "total_r": total_r,
        "max_dd_r": max_dd,
        "train_pf": train_pf,
        "test_pf": test_pf,
        "ex_best_day_pf": ex_best_day_pf,
        "ex_top_sym_pf": ex_top_sym_pf,
        "stop_rate": stopped / n * 100,
        "qstop_rate": qstop / n * 100,
    }


def _pf(trades: List[HTrade]) -> float:
    if not trades:
        return 0.0
    gw = sum(t.pnl_rr for t in trades if t.pnl_rr > 0)
    gl = abs(sum(t.pnl_rr for t in trades if t.pnl_rr <= 0))
    return gw / gl if gl > 0 else float("inf")


# ── SPY day-color classification ──

def classify_spy_days(spy_bars) -> Dict[str, str]:
    """Returns {date_str: GREEN|FLAT|RED} per trading day."""
    from collections import defaultdict
    days = defaultdict(list)
    for b in spy_bars:
        ds = str(b.timestamp.date())
        days[ds].append(b)

    colors = {}
    for ds, bars in sorted(days.items()):
        if not bars:
            continue
        day_open = bars[0].open
        day_close = bars[-1].close
        pct = (day_close - day_open) / day_open * 100
        if pct > 0.15:
            colors[ds] = "GREEN"
        elif pct < -0.15:
            colors[ds] = "RED"
        else:
            colors[ds] = "FLAT"
    return colors


# ── Display ──

HEADER = (
    f"  {'Label':36s}  {'N':>4s}  {'WR%':>5s}  {'PF(R)':>6s}  {'Exp(R)':>7s}  "
    f"{'TotalR':>8s}  {'MaxDD':>7s}  {'TrnPF':>6s}  {'TstPF':>6s}  "
    f"{'ExDay':>6s}  {'ExSym':>6s}  {'Stop%':>5s}"
)
DIVIDER = "  " + "-" * 130


def fmt_row(label: str, m: dict, indent: str = "  ") -> str:
    return (
        f"{indent}{label:36s}  {m['n']:4d}  {m['wr']:5.1f}  {pf_str(m['pf']):>6s}  "
        f"{m['exp']:+7.3f}  {m['total_r']:+8.2f}  {m['max_dd_r']:7.2f}  "
        f"{pf_str(m['train_pf']):>6s}  {pf_str(m['test_pf']):>6s}  "
        f"{pf_str(m['ex_best_day_pf']):>6s}  {pf_str(m['ex_top_sym_pf']):>6s}  "
        f"{m['stop_rate']:5.1f}"
    )


# ── Main ──

def main():
    parser = argparse.ArgumentParser(description="Habitat Validation Study — STATIC vs IN_PLAY")
    parser.add_argument("--static", required=True, help="Path to static watchlist file")
    parser.add_argument("--in-play", default=None,
                        help="Path to in-play symbol list (flat file, all dates same)")
    parser.add_argument("--snapshots", default=None,
                        help="Path to in_play_snapshots/ directory (date-aware mode)")
    args = parser.parse_args()

    if not args.in_play and not args.snapshots:
        parser.error("Provide either --in-play (flat file) or --snapshots (date-aware directory)")

    static_set = load_symbol_list(args.static)

    # Two modes: flat file (all symbols treated same across all dates)
    # or snapshot directory (per-date in-play lists)
    use_snapshots = args.snapshots is not None
    snapshots: Dict[str, Set[str]] = {}
    in_play_set: Set[str] = set()

    if use_snapshots:
        snapshots = load_snapshots(args.snapshots)
        if not snapshots:
            print(f"WARNING: No snapshots found in {args.snapshots}")
            print("  Run the dashboard and add in-play symbols to generate snapshots.")
            print("  Falling back to STATIC-only mode.\n")
        else:
            print(f"Loaded {len(snapshots)} daily snapshots")
            # Union of all in-play symbols across all dates
            for syms in snapshots.values():
                in_play_set |= syms
            print(f"  Unique in-play symbols across all dates: {len(in_play_set)}")
            print(f"  Date range: {min(snapshots.keys())} → {max(snapshots.keys())}\n")
    else:
        in_play_set = load_symbol_list(args.in_play)

    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    all_candidates = (static_set | in_play_set) - excluded
    available = sorted([
        s for s in all_candidates
        if (DATA_DIR / f"{s}_5min.csv").exists()
    ])

    # For flat mode, compute static universe map
    # For snapshot mode, universe is assigned per-trade (below)
    if not use_snapshots:
        universe_map = {sym: assign_universe(sym, static_set, in_play_set) for sym in available}
    else:
        universe_map = {sym: "STATIC" for sym in available}  # placeholder; actual is per-trade

    static_only = [s for s, u in universe_map.items() if u == "STATIC"]
    in_play_only = [s for s, u in universe_map.items() if u == "IN_PLAY"]
    both = [s for s, u in universe_map.items() if u == "BOTH"]

    mode_label = "SNAPSHOT" if use_snapshots else "FLAT"
    print("=" * 140)
    print(f"HABITAT VALIDATION STUDY — STATIC vs IN_PLAY vs BOTH (Full R-First, {mode_label} mode)")
    print("=" * 140)
    print(f"Static list:   {len(static_set)} symbols")
    print(f"In-play pool:  {len(in_play_set)} symbols")
    if not use_snapshots:
        print(f"  STATIC-only: {len(static_only)}, IN_PLAY-only: {len(in_play_only)}, BOTH: {len(both)}")
    print(f"Total with data: {len(available)}")
    print(f"Train/test: odd dates = train, even dates = test\n")

    # ── Load SPY/QQQ/sector bars ──
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    spy_colors = classify_spy_days(spy_bars)

    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    # ── Run backtests ──
    cfg = OverlayConfig()
    # These should match config.py validated model settings
    cfg.show_ema_scalp = False
    cfg.show_failed_bounce = False
    cfg.show_spencer = False
    cfg.show_ema_fpip = False
    cfg.show_sc_v2 = False
    cfg.show_trend_setups = False

    print("Running backtests...")
    all_trades: List[HTrade] = []
    sym_counts = {"STATIC": 0, "IN_PLAY": 0, "BOTH": 0}

    for sym in available:
        bars = load_bars_from_csv(str(DATA_DIR / f"{sym}_5min.csv"))
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)

        for t in result.trades:
            # Determine SPY regime for trade day
            day_str = str(t.signal.timestamp.date()) if t.signal.timestamp else ""
            color = spy_colors.get(day_str, "FLAT")

            # Universe assignment: per-trade in snapshot mode, per-symbol in flat mode
            if use_snapshots:
                uni = assign_universe_dated(sym, day_str, static_set, snapshots)
            else:
                uni = universe_map[sym]

            all_trades.append(HTrade(t, universe=uni, symbol=sym, spy_day_color=color))

    # Recount by universe (per-trade, not per-symbol)
    sym_counts = {"STATIC": 0, "IN_PLAY": 0, "BOTH": 0}
    seen = set()
    for t in all_trades:
        key = (t.symbol, t.universe)
        if key not in seen:
            seen.add(key)
            sym_counts[t.universe] += 1

    print(f"  Trades: {len(all_trades)}")
    print(f"  Symbols: STATIC={sym_counts['STATIC']}, IN_PLAY={sym_counts['IN_PLAY']}, BOTH={sym_counts['BOTH']}\n")

    # ── Group trades ──
    groups: Dict[str, List[HTrade]] = {
        "STATIC": [t for t in all_trades if t.universe == "STATIC"],
        "IN_PLAY": [t for t in all_trades if t.universe == "IN_PLAY"],
        "BOTH": [t for t in all_trades if t.universe == "BOTH"],
        "ALL": all_trades,
    }

    # ════════════════════════════════════════════════════════════
    #  1. OVERALL COMPARISON
    # ════════════════════════════════════════════════════════════
    print("=" * 140)
    print("1. OVERALL COMPARISON BY UNIVERSE")
    print("=" * 140)
    print(HEADER)
    print(DIVIDER)
    for label in ["STATIC", "IN_PLAY", "BOTH", "ALL"]:
        m = compute_metrics(groups[label])
        print(fmt_row(label, m))

    # ════════════════════════════════════════════════════════════
    #  2. PER-SETUP BREAKDOWN WITHIN EACH UNIVERSE
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 140)
    print("2. PER-SETUP BREAKDOWN BY UNIVERSE")
    print("=" * 140)

    for uni_label in ["STATIC", "IN_PLAY", "BOTH", "ALL"]:
        trades = groups[uni_label]
        if not trades:
            continue
        print(f"\n  [{uni_label}]")
        print(HEADER)
        print(DIVIDER)

        setup_groups: Dict[str, List[HTrade]] = defaultdict(list)
        for t in trades:
            name = SETUP_DISPLAY_NAME.get(t.setup, str(t.setup))
            setup_groups[name].append(t)

        for setup_name in sorted(setup_groups.keys()):
            strades = setup_groups[setup_name]
            m = compute_metrics(strades)
            if m["n"] >= 3:
                print(fmt_row(f"  {setup_name}", m))

    # ════════════════════════════════════════════════════════════
    #  3. DIRECTION BREAKDOWN (LONG / SHORT PER UNIVERSE)
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 140)
    print("3. DIRECTION BREAKDOWN BY UNIVERSE")
    print("=" * 140)
    print(HEADER)
    print(DIVIDER)

    for uni_label in ["STATIC", "IN_PLAY", "BOTH", "ALL"]:
        trades = groups[uni_label]
        if not trades:
            continue
        longs = [t for t in trades if t.side == "LONG"]
        shorts = [t for t in trades if t.side == "SHORT"]
        if longs:
            print(fmt_row(f"{uni_label} — LONG", compute_metrics(longs)))
        if shorts:
            print(fmt_row(f"{uni_label} — SHORT", compute_metrics(shorts)))

    # ════════════════════════════════════════════════════════════
    #  4. REGIME BREAKDOWN (GREEN / RED PER UNIVERSE)
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 140)
    print("4. REGIME BREAKDOWN BY UNIVERSE")
    print("=" * 140)
    print(HEADER)
    print(DIVIDER)

    for uni_label in ["STATIC", "IN_PLAY", "BOTH", "ALL"]:
        trades = groups[uni_label]
        if not trades:
            continue
        for regime in ["GREEN", "FLAT", "RED"]:
            rt = [t for t in trades if t.regime == regime]
            if rt:
                print(fmt_row(f"{uni_label} — {regime}", compute_metrics(rt)))

    # ════════════════════════════════════════════════════════════
    #  5. ROBUSTNESS: EX-BEST-DAY & EX-TOP-SYMBOL DETAIL
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 140)
    print("5. ROBUSTNESS DETAIL")
    print("=" * 140)

    for uni_label in ["STATIC", "IN_PLAY", "BOTH", "ALL"]:
        trades = groups[uni_label]
        m = compute_metrics(trades)
        if m["n"] == 0:
            continue

        # Best day
        day_r: Dict[str, float] = defaultdict(float)
        for t in trades:
            if t.entry_date:
                day_r[str(t.entry_date)] += t.pnl_rr
        if day_r:
            best_day = max(day_r, key=day_r.get)
            best_day_r = day_r[best_day]
        else:
            best_day, best_day_r = "N/A", 0

        # Top symbol
        sym_r: Dict[str, float] = defaultdict(float)
        for t in trades:
            sym_r[t.symbol] += t.pnl_rr
        if sym_r:
            top_sym = max(sym_r, key=sym_r.get)
            top_sym_r = sym_r[top_sym]
        else:
            top_sym, top_sym_r = "N/A", 0

        print(f"\n  [{uni_label}]  Total PF={pf_str(m['pf'])}  |  "
              f"Best day: {best_day} ({best_day_r:+.2f}R)  ExDayPF={pf_str(m['ex_best_day_pf'])}  |  "
              f"Top sym: {top_sym} ({top_sym_r:+.2f}R)  ExSymPF={pf_str(m['ex_top_sym_pf'])}")

    # ════════════════════════════════════════════════════════════
    #  6. DELTA ANALYSIS — DOES IN_PLAY ADD EDGE?
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 140)
    print("6. DELTA ANALYSIS — DOES IN_PLAY ADD EDGE?")
    print("=" * 140)

    m_s = compute_metrics(groups["STATIC"])
    m_i = compute_metrics(groups["IN_PLAY"])
    m_b = compute_metrics(groups["BOTH"])
    m_a = compute_metrics(groups["ALL"])

    print(f"""
  STATIC:   N={m_s['n']:4d}  PF={pf_str(m_s['pf'])}  Exp={m_s['exp']:+.3f}R  DD={m_s['max_dd_r']:.1f}R  Trn={pf_str(m_s['train_pf'])} Tst={pf_str(m_s['test_pf'])}
  IN_PLAY:  N={m_i['n']:4d}  PF={pf_str(m_i['pf'])}  Exp={m_i['exp']:+.3f}R  DD={m_i['max_dd_r']:.1f}R  Trn={pf_str(m_i['train_pf'])} Tst={pf_str(m_i['test_pf'])}
  BOTH:     N={m_b['n']:4d}  PF={pf_str(m_b['pf'])}  Exp={m_b['exp']:+.3f}R  DD={m_b['max_dd_r']:.1f}R  Trn={pf_str(m_b['train_pf'])} Tst={pf_str(m_b['test_pf'])}
  ALL:      N={m_a['n']:4d}  PF={pf_str(m_a['pf'])}  Exp={m_a['exp']:+.3f}R  DD={m_a['max_dd_r']:.1f}R  Trn={pf_str(m_a['train_pf'])} Tst={pf_str(m_a['test_pf'])}
""")

    # ── Decision criteria ──
    edge_criteria = []

    # C1: IN_PLAY has positive expectancy
    if m_i["n"] >= 10 and m_i["exp"] > 0 and m_i["pf"] > 1.0:
        edge_criteria.append("C1-PASS: IN_PLAY has positive raw edge")
    elif m_i["n"] >= 10:
        edge_criteria.append(f"C1-FAIL: IN_PLAY exp={m_i['exp']:+.3f}R, PF={pf_str(m_i['pf'])}")
    else:
        edge_criteria.append(f"C1-SKIP: IN_PLAY N={m_i['n']} (too few)")

    # C2: IN_PLAY train/test stable
    if m_i["train_pf"] >= 1.0 and m_i["test_pf"] >= 1.0:
        edge_criteria.append("C2-PASS: IN_PLAY train/test both PF>=1.0")
    else:
        edge_criteria.append(f"C2-FAIL: IN_PLAY train={pf_str(m_i['train_pf'])} test={pf_str(m_i['test_pf'])}")

    # C3: IN_PLAY survives robustness
    if m_i["ex_best_day_pf"] >= 1.0 and m_i["ex_top_sym_pf"] >= 1.0:
        edge_criteria.append("C3-PASS: IN_PLAY robust (ex-day, ex-sym both PF>=1.0)")
    else:
        edge_criteria.append(f"C3-FAIL: ExDay={pf_str(m_i['ex_best_day_pf'])} ExSym={pf_str(m_i['ex_top_sym_pf'])}")

    # C4: Combined (ALL) doesn't degrade vs STATIC alone
    if m_a["pf"] >= m_s["pf"] * 0.90:
        edge_criteria.append("C4-PASS: Adding IN_PLAY doesn't degrade combined PF")
    else:
        edge_criteria.append(f"C4-FAIL: ALL PF={pf_str(m_a['pf'])} < 90% of STATIC PF={pf_str(m_s['pf'])}")

    print("  PROMOTION CRITERIA:")
    for c in edge_criteria:
        prefix = "  ✓" if "PASS" in c else ("  ✗" if "FAIL" in c else "  ○")
        print(f"  {prefix} {c}")

    passes = sum(1 for c in edge_criteria if "PASS" in c)
    total_criteria = sum(1 for c in edge_criteria if "SKIP" not in c)

    # ════════════════════════════════════════════════════════════
    #  7. VERDICT
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 140)
    print("7. VERDICT")
    print("=" * 140)

    if total_criteria == 0:
        print("\n  INCOMPLETE: Not enough IN_PLAY trades to evaluate.")
        print("  Need real in-play symbol history with backtest data.")
    elif passes == total_criteria:
        print(f"\n  PROMOTE: IN_PLAY universe passes all {total_criteria} criteria.")
        print("  Recommendation: Include daily in-play symbols in live trading.")
        print("  Action: Keep in-play panel active during market hours.")
    elif passes >= total_criteria - 1:
        print(f"\n  CONDITIONAL: IN_PLAY passes {passes}/{total_criteria} criteria.")
        print("  Recommendation: Use with additional quality filter (Q>=3).")
        print("  Re-evaluate after 30 more trading days of in-play data.")
    else:
        print(f"\n  REJECT: IN_PLAY passes only {passes}/{total_criteria} criteria.")
        print("  Recommendation: Do NOT include in-play in live trading yet.")
        print("  Focus on static universe optimization instead.")

    print("\n" + "=" * 140)


if __name__ == "__main__":
    main()
