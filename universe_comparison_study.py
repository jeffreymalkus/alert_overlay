"""
Universe Comparison Study — STATIC vs IN_PLAY vs BOTH.

Compares alert and trade performance across universe segments:
  1. STATIC-only symbols (in static watchlist, not in-play)
  2. IN_PLAY-only symbols (in in-play list, not in static)
  3. BOTH symbols (in both lists — overlap)
  4. ALL combined

Metrics per group: N alerts, N trades, WR%, PF(R), Exp(R), Total R,
Stop rate, per-setup breakdown.

Train/test: odd dates = train, even dates = test.

Usage:
    python -m alert_overlay.universe_comparison_study \
        --static watchlist.txt --in-play in_play.txt

    # Use same file for both to test (all symbols tagged BOTH):
    python -m alert_overlay.universe_comparison_study \
        --static watchlist.txt --in-play watchlist.txt
"""

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import NaN, SetupId, SETUP_DISPLAY_NAME
from .market_context import SECTOR_MAP, get_sector_etf

DATA_DIR = Path(__file__).parent / "data"


# ── Universe assignment ──

def load_symbol_list(path: str) -> Set[str]:
    """Load symbols from a text file (one per line, # comments)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Symbol list not found: {path}")
    symbols = set()
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            symbols.add(line.upper())
    return symbols


def assign_universe(symbol: str, static_set: Set[str], in_play_set: Set[str]) -> str:
    in_static = symbol in static_set
    in_play = symbol in in_play_set
    if in_static and in_play:
        return "BOTH"
    elif in_play:
        return "IN_PLAY"
    else:
        return "STATIC"


# ── Trade wrapper ──

class UTrade:
    """Unified trade with universe tag."""
    __slots__ = ("pnl_points", "pnl_rr", "exit_reason", "bars_held",
                 "entry_time", "entry_date", "side", "setup", "universe", "symbol")

    def __init__(self, t: Trade, universe: str, symbol: str):
        self.pnl_points = t.pnl_points
        self.pnl_rr = t.pnl_rr
        self.exit_reason = t.exit_reason
        self.bars_held = t.bars_held
        self.entry_time = t.signal.timestamp
        self.entry_date = t.signal.timestamp.date()
        self.side = "LONG" if t.signal.direction == 1 else "SHORT"
        self.setup = t.signal.setup_id
        self.universe = universe
        self.symbol = symbol


# ── Metrics ──

def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


def compute_metrics(trades: List[UTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0, "pf": 0, "exp": 0, "total_r": 0,
                "stop_rate": 0, "qstop_rate": 0}
    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in trades)
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    qstop = sum(1 for t in trades if t.exit_reason == "stop" and t.bars_held <= 3)
    return {
        "n": n,
        "wr": len(wins) / n * 100,
        "pf": pf,
        "exp": total_r / n,
        "total_r": total_r,
        "stop_rate": stopped / n * 100,
        "qstop_rate": qstop / n * 100,
    }


def split_train_test(trades: List[UTrade]):
    train = [t for t in trades if t.entry_date and t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date and t.entry_date.day % 2 == 0]
    return train, test


def print_row(label, m, indent="  "):
    print(f"{indent}{label:38s}  N={m['n']:4d}  WR={m['wr']:5.1f}%  "
          f"PF={pf_str(m['pf']):>6s}  Exp={m['exp']:+.3f}R  TotalR={m['total_r']:+7.2f}  "
          f"Stop={m['stop_rate']:4.1f}%  QStop={m['qstop_rate']:4.1f}%")


# ── Main ──

def main():
    parser = argparse.ArgumentParser(description="Universe Comparison: STATIC vs IN_PLAY")
    parser.add_argument("--static", required=True, help="Path to static watchlist file")
    parser.add_argument("--in-play", required=True, help="Path to in-play symbol list file")
    args = parser.parse_args()

    static_set = load_symbol_list(args.static)
    in_play_set = load_symbol_list(args.in_play)

    # Exclude index/sector ETFs from tradable universe
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})

    # All symbols that have data AND appear in at least one list
    all_candidates = (static_set | in_play_set) - excluded
    available = sorted([
        s for s in all_candidates
        if (DATA_DIR / f"{s}_5min.csv").exists()
    ])

    # Assign universe per symbol
    universe_map: Dict[str, str] = {}
    for sym in available:
        universe_map[sym] = assign_universe(sym, static_set, in_play_set)

    static_only = sorted([s for s, u in universe_map.items() if u == "STATIC"])
    in_play_only = sorted([s for s, u in universe_map.items() if u == "IN_PLAY"])
    both = sorted([s for s, u in universe_map.items() if u == "BOTH"])

    print("=" * 120)
    print("UNIVERSE COMPARISON STUDY — STATIC vs IN_PLAY")
    print("=" * 120)
    print(f"Static list:   {len(static_set)} symbols ({len(static_only)} unique, {len(both)} overlap)")
    print(f"In-play list:  {len(in_play_set)} symbols ({len(in_play_only)} unique, {len(both)} overlap)")
    print(f"Total symbols with data: {len(available)}")
    print(f"Train/test: odd dates = train, even dates = test\n")

    # ── Load index/sector bars ──
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    # ── Run backtest per symbol, tag with universe ──
    cfg = OverlayConfig()
    cfg.show_ema_scalp = False
    cfg.show_failed_bounce = False
    cfg.show_spencer = False
    cfg.show_ema_fpip = False
    cfg.show_sc_v2 = False

    print("Running backtests...")
    all_trades: List[UTrade] = []
    sym_counts = {"STATIC": 0, "IN_PLAY": 0, "BOTH": 0}

    for sym in available:
        p = DATA_DIR / f"{sym}_5min.csv"
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue

        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)

        uni = universe_map[sym]
        sym_counts[uni] += 1
        for t in result.trades:
            all_trades.append(UTrade(t, universe=uni, symbol=sym))

    print(f"  Total trades: {len(all_trades)}")
    print(f"  Symbols run: STATIC={sym_counts['STATIC']}, "
          f"IN_PLAY={sym_counts['IN_PLAY']}, BOTH={sym_counts['BOTH']}\n")

    # ── Group trades ──
    groups: Dict[str, List[UTrade]] = {
        "STATIC": [t for t in all_trades if t.universe == "STATIC"],
        "IN_PLAY": [t for t in all_trades if t.universe == "IN_PLAY"],
        "BOTH": [t for t in all_trades if t.universe == "BOTH"],
        "ALL": all_trades,
    }

    # ════════════════════════════════════════════════════════════
    #  OVERALL COMPARISON
    # ════════════════════════════════════════════════════════════
    print("=" * 120)
    print("OVERALL COMPARISON")
    print("=" * 120)
    for label in ["STATIC", "IN_PLAY", "BOTH", "ALL"]:
        print_row(label, compute_metrics(groups[label]))

    # ════════════════════════════════════════════════════════════
    #  TRAIN / TEST STABILITY
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("TRAIN / TEST STABILITY")
    print("=" * 120)

    print(f"\n  {'Group':38s}  {'Trn PF':>6s}  {'Tst PF':>6s}  {'Delta':>6s}  "
          f"{'Trn WR':>6s}  {'Tst WR':>6s}  {'Delta':>6s}  {'Stable?':>7s}")
    print(f"  {'-'*38}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*7}")

    for label in ["STATIC", "IN_PLAY", "BOTH", "ALL"]:
        trades = groups[label]
        if not trades:
            continue
        train, test = split_train_test(trades)
        tr = compute_metrics(train)
        te = compute_metrics(test)
        pf_d = te["pf"] - tr["pf"] if tr["pf"] < 900 and te["pf"] < 900 else float("nan")
        wr_d = te["wr"] - tr["wr"]
        stable = "YES" if (tr["pf"] >= 1.0 and te["pf"] >= 1.0 and abs(wr_d) < 5.0) else "NO"
        pf_d_s = f"{pf_d:+.2f}" if abs(pf_d) < 100 else "N/A"
        print(f"  {label:38s}  {pf_str(tr['pf']):>6s}  {pf_str(te['pf']):>6s}  {pf_d_s:>6s}  "
              f"{tr['wr']:5.1f}%  {te['wr']:5.1f}%  {wr_d:+5.1f}%  {stable:>7s}")

    # ════════════════════════════════════════════════════════════
    #  PER-SETUP BREAKDOWN WITHIN EACH UNIVERSE
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("PER-SETUP BREAKDOWN BY UNIVERSE")
    print("=" * 120)

    for label in ["STATIC", "IN_PLAY", "BOTH", "ALL"]:
        trades = groups[label]
        if not trades:
            continue
        print(f"\n  {label}:")

        setup_groups: Dict[str, List[UTrade]] = defaultdict(list)
        for t in trades:
            name = SETUP_DISPLAY_NAME.get(t.setup, str(t.setup))
            setup_groups[name].append(t)

        for setup_name in sorted(setup_groups.keys()):
            strades = setup_groups[setup_name]
            m = compute_metrics(strades)
            if m["n"] >= 3:
                print_row(setup_name, m, indent="    ")

    # ════════════════════════════════════════════════════════════
    #  DELTA ANALYSIS: DOES IN_PLAY ADD EDGE?
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("DELTA ANALYSIS — DOES IN_PLAY ADD EDGE OR JUST VOLUME?")
    print("=" * 120)

    m_static = compute_metrics(groups["STATIC"])
    m_inplay = compute_metrics(groups["IN_PLAY"])
    m_both = compute_metrics(groups["BOTH"])
    m_all = compute_metrics(groups["ALL"])

    print(f"""
  Universe breakdown:
    STATIC-only:  {m_static['n']:4d} trades  WR={m_static['wr']:5.1f}%  PF={pf_str(m_static['pf'])}  Exp={m_static['exp']:+.3f}R  TotalR={m_static['total_r']:+7.2f}
    IN_PLAY-only: {m_inplay['n']:4d} trades  WR={m_inplay['wr']:5.1f}%  PF={pf_str(m_inplay['pf'])}  Exp={m_inplay['exp']:+.3f}R  TotalR={m_inplay['total_r']:+7.2f}
    BOTH overlap: {m_both['n']:4d} trades  WR={m_both['wr']:5.1f}%  PF={pf_str(m_both['pf'])}  Exp={m_both['exp']:+.3f}R  TotalR={m_both['total_r']:+7.2f}
    ALL combined: {m_all['n']:4d} trades  WR={m_all['wr']:5.1f}%  PF={pf_str(m_all['pf'])}  Exp={m_all['exp']:+.3f}R  TotalR={m_all['total_r']:+7.2f}
""")

    # Key questions
    adds_volume = m_inplay["n"] > 0
    adds_edge = m_inplay["n"] > 0 and m_inplay["exp"] > 0 and m_inplay["pf"] > 1.0

    if adds_edge:
        exp_lift = m_inplay["exp"] - m_static["exp"]
        print(f"  IN_PLAY adds EDGE: Exp delta = {exp_lift:+.3f}R vs STATIC")
        if m_inplay["exp"] > m_static["exp"]:
            print("  IN_PLAY has HIGHER expectancy than STATIC — strong signal.")
        else:
            print("  IN_PLAY has LOWER expectancy than STATIC — adds volume but dilutes edge.")
    elif adds_volume:
        print(f"  IN_PLAY adds VOLUME ({m_inplay['n']} trades) but NOT edge (Exp={m_inplay['exp']:+.3f}R, PF={pf_str(m_inplay['pf'])})")
        print("  Consider: in-play symbols may need tighter filters to be profitable.")
    else:
        print("  No IN_PLAY trades found. Run with an in-play list that has data available.")

    # BOTH overlap check
    if m_both["n"] > 0:
        if m_both["exp"] > m_static["exp"] and m_both["exp"] > m_inplay["exp"]:
            print(f"\n  BOTH (overlap) has HIGHEST expectancy ({m_both['exp']:+.3f}R) — "
                  "overlap symbols are the strongest performers.")
        elif m_both["n"] >= 10:
            print(f"\n  BOTH (overlap) Exp={m_both['exp']:+.3f}R — no clear edge from overlap alone.")

    # ════════════════════════════════════════════════════════════
    #  VERDICT
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("VERDICT")
    print("=" * 120)

    train_all, test_all = split_train_test(all_trades)
    mt = compute_metrics(train_all)
    ms = compute_metrics(test_all)
    both_stable = mt["pf"] >= 1.0 and ms["pf"] >= 1.0

    print(f"""
  ALL trades: N={m_all['n']}  Train PF={pf_str(mt['pf'])}  Test PF={pf_str(ms['pf'])}  Stable={'YES' if both_stable else 'NO'}
""")

    if adds_edge and both_stable:
        print("  PASS: In-play universe adds edge AND combined system is stable.")
        print("  Recommendation: Include daily in-play symbols in live monitoring.")
    elif adds_volume and both_stable:
        print("  PARTIAL: In-play adds volume but not edge. Combined system is stable.")
        print("  Recommendation: Monitor in-play but apply quality filters (Q>=2).")
    elif adds_volume:
        print("  FAIL: In-play adds volume but combined system is NOT stable.")
        print("  Recommendation: Do not include in-play in live trading yet.")
    else:
        print("  INCOMPLETE: No in-play trades to evaluate.")
        print("  Provide an in-play list with symbols that have backtest data.")


if __name__ == "__main__":
    main()
