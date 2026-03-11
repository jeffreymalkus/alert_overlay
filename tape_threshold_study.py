"""
Tape Threshold Study — Compare frozen regime gating vs tape permission thresholds.

Does NOT modify the frozen system.  Replays the same trade universe and applies
tape-based permission filters as an alternative to coarse RED/GREEN gating.

Comparisons (all in R):

  LONG SIDE
    1. Current frozen long book (Non-RED, Q>=2, <15:30, VK+SC)
    2. Frozen + long_permission >= 0.30
    3. Frozen + long_permission >= 0.40
    4. Frozen + long_permission >= 0.50

  SHORT SIDE
    1. Current frozen BDR (RED+TREND, wick>=30%, AM <11:00)
    2. BDR AM-only + short_permission >= 0.30
    3. BDR AM-only + short_permission >= 0.40
    4. BDR AM-only + short_permission >= 0.50

  COMBINED PORTFOLIO variants

  CROSS-REGIME ANALYSIS:
    - Longs on old RED days that tape permits
    - Shorts on old GREEN days that tape permits

Usage:
    python -m alert_overlay.tape_threshold_study
"""

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import Bar, Signal, SetupId, NaN, SETUP_DISPLAY_NAME
from .market_context import (
    MarketEngine, MarketContext, MarketSnapshot,
    compute_market_context, get_sector_etf, SECTOR_MAP,
)
from .tape_model import TapeReading, TapeWeights, read_tape, classify_tape_zone
from .breakdown_retest_study import (
    BDRTrade, compute_bar_contexts, scan_for_breakdowns,
    build_market_context_map, TIME_STOP_BARS, STOP_BUFFER_ATR, SESSION_END_HHMM,
)
from .validated_combined_system import (
    simulate_exit_dynamic_slip, classify_spy_days, UTrade,
    wrap_engine_trade, wrap_bdr_trade, compute_metrics as _compute_metrics_raw,
    split_train_test,
)

DATA_DIR = Path(__file__).parent / "data"
RISK_PER_TRADE = 100.0  # $100 per trade for R normalization


# ═══════════════════════════════════════════════════════════════════
#  Extended trade record with tape reading
# ═══════════════════════════════════════════════════════════════════

class TaggedTrade:
    """A UTrade augmented with tape reading and old regime label."""
    __slots__ = ("ut", "tape", "old_regime", "old_direction",
                 "long_perm", "short_perm", "tape_zone")

    def __init__(self, ut: UTrade, tape: TapeReading, old_regime: str):
        self.ut = ut
        self.tape = tape
        self.old_regime = old_regime
        self.old_direction = old_regime.split("+")[0] if "+" in old_regime else old_regime
        self.long_perm = tape.long_permission
        self.short_perm = tape.short_permission
        self.tape_zone = classify_tape_zone(tape.tape_score)


# ═══════════════════════════════════════════════════════════════════
#  Metrics (all in R)
# ═══════════════════════════════════════════════════════════════════

def compute_metrics_r(trades: list) -> dict:
    """Compute metrics using R-multiples."""
    tlist = [t.ut if isinstance(t, TaggedTrade) else t for t in trades]
    n = len(tlist)
    if n == 0:
        return {"n": 0, "pf_r": 0, "exp_r": 0, "total_r": 0,
                "max_dd_r": 0, "stop_rate": 0, "wr": 0}
    wins = [t for t in tlist if t.pnl_rr > 0]
    losses = [t for t in tlist if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in tlist)
    exp_r = total_r / n
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf_r = gw / gl if gl > 0 else float("inf")
    wr = len(wins) / n * 100

    # Max DD in R (chronological)
    sorted_t = sorted(tlist, key=lambda t: t.entry_time or datetime.min)
    cum = pk = dd = 0.0
    for t in sorted_t:
        cum += t.pnl_rr
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum

    stopped = sum(1 for t in tlist if t.exit_reason == "stop")

    return {
        "n": n, "pf_r": round(pf_r, 2), "exp_r": round(exp_r, 3),
        "total_r": round(total_r, 2), "max_dd_r": round(dd, 2),
        "stop_rate": round(stopped / n * 100, 1), "wr": round(wr, 1),
    }


def _pf(v):
    return f"{v:.2f}" if v < 999 else "inf"


def print_row(label, m, indent="  "):
    print(f"{indent}{label:50s}  N={m['n']:>4}  WR={m['wr']:>5.1f}%  "
          f"PF(R)={_pf(m['pf_r']):>6}  Exp={m['exp_r']:>+7.3f}R  "
          f"TotalR={m['total_r']:>+8.2f}  MaxDD={m['max_dd_r']:>6.2f}R  "
          f"Stop={m['stop_rate']:>5.1f}%")


# ═══════════════════════════════════════════════════════════════════
#  Robustness helpers
# ═══════════════════════════════════════════════════════════════════

def robustness_check(trades: list, label: str):
    """Exclude best day and top symbol, report degraded metrics."""
    tlist = [t.ut if isinstance(t, TaggedTrade) else t for t in trades]
    if len(tlist) < 5:
        return

    # Find best day
    by_day = defaultdict(list)
    for t in tlist:
        if t.entry_date:
            by_day[t.entry_date].append(t)
    day_r = {d: sum(t.pnl_rr for t in ts) for d, ts in by_day.items()}
    best_day = max(day_r, key=day_r.get)
    ex_day = [t for t in tlist if t.entry_date != best_day]

    # Find top symbol
    by_sym = defaultdict(list)
    for t in tlist:
        by_sym[t.sym].append(t)
    sym_r = {s: sum(t.pnl_rr for t in ts) for s, ts in by_sym.items()}
    best_sym = max(sym_r, key=sym_r.get)
    ex_sym = [t for t in tlist if t.sym != best_sym]

    m_full = compute_metrics_r(tlist)
    m_ex_day = compute_metrics_r(ex_day)
    m_ex_sym = compute_metrics_r(ex_sym)

    print(f"\n    Robustness — {label}:")
    print(f"      Full:                 N={m_full['n']:>4}  PF(R)={_pf(m_full['pf_r']):>6}  "
          f"Exp={m_full['exp_r']:>+7.3f}R  TotalR={m_full['total_r']:>+8.2f}")
    print(f"      Excl best day ({best_day}): N={m_ex_day['n']:>4}  PF(R)={_pf(m_ex_day['pf_r']):>6}  "
          f"Exp={m_ex_day['exp_r']:>+7.3f}R  TotalR={m_ex_day['total_r']:>+8.2f}")
    print(f"      Excl top sym ({best_sym:>5}):  N={m_ex_sym['n']:>4}  PF(R)={_pf(m_ex_sym['pf_r']):>6}  "
          f"Exp={m_ex_sym['exp_r']:>+7.3f}R  TotalR={m_ex_sym['total_r']:>+8.2f}")


def train_test_report(trades: list, label: str):
    """Train/test split (odd/even day)."""
    tlist = [t.ut if isinstance(t, TaggedTrade) else t for t in trades]
    train, test = split_train_test(tlist)
    m_train = compute_metrics_r(train)
    m_test = compute_metrics_r(test)
    print(f"\n    Train/Test — {label}:")
    print(f"      Train (odd):   N={m_train['n']:>4}  PF(R)={_pf(m_train['pf_r']):>6}  "
          f"Exp={m_train['exp_r']:>+7.3f}R  TotalR={m_train['total_r']:>+8.2f}")
    print(f"      Test  (even):  N={m_test['n']:>4}  PF(R)={_pf(m_test['pf_r']):>6}  "
          f"Exp={m_test['exp_r']:>+7.3f}R  TotalR={m_test['total_r']:>+8.2f}")


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════

def main():
    print("=" * 120)
    print("  TAPE THRESHOLD STUDY — Frozen Regime Gating vs Tape Permission Thresholds")
    print("=" * 120)

    # ── Load market data ──
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    # Build SPY/QQQ snapshot maps
    spy_engine = MarketEngine()
    qqq_engine = MarketEngine()
    spy_snaps: Dict[datetime, MarketSnapshot] = {}
    qqq_snaps: Dict[datetime, MarketSnapshot] = {}
    for b in spy_bars:
        spy_snaps[b.timestamp] = spy_engine.process_bar(b)
    for b in qqq_bars:
        qqq_snaps[b.timestamp] = qqq_engine.process_bar(b)

    # Load sector data
    sector_bars_dict: Dict[str, List[Bar]] = {}
    sector_snap_dict: Dict[str, Dict[datetime, MarketSnapshot]] = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            bars = load_bars_from_csv(str(p))
            sector_bars_dict[etf] = bars
            eng = MarketEngine()
            snaps = {}
            for b in bars:
                snaps[b.timestamp] = eng.process_bar(b)
            sector_snap_dict[etf] = snaps

    # Get symbols
    wl = Path(__file__).parent / "watchlist.txt"
    symbols = []
    with open(wl) as f:
        for line in f:
            s = line.strip().upper()
            if s and not s.startswith("#") and s not in ("SPY", "QQQ"):
                symbols.append(s)

    spy_day_info = classify_spy_days(spy_bars)
    weights = TapeWeights()  # Fixed default weights — no optimization

    print(f"\n  Universe: {len(symbols)} symbols")
    print(f"  Tape weights (FIXED): {weights.as_dict()}")

    # ── Helper: find nearest snapshot ──
    def find_snap(snap_dict, ts):
        if ts in snap_dict:
            return snap_dict[ts]
        for d in range(1, 11):
            c = ts - timedelta(minutes=d)
            if c in snap_dict:
                return snap_dict[c]
        return None

    # ── Helper: compute tape reading for a trade ──
    def get_tape(sym: str, entry_ts: datetime, entry_price: float) -> Optional[TapeReading]:
        spy_s = find_snap(spy_snaps, entry_ts)
        qqq_s = find_snap(qqq_snaps, entry_ts)
        if not spy_s or not qqq_s:
            return None

        sec_etf = get_sector_etf(sym)
        sec_s = None
        if sec_etf and sec_etf in sector_snap_dict:
            sec_s = find_snap(sector_snap_dict[sec_etf], entry_ts)

        # Compute stock RS
        stock_pct = NaN
        if not math.isnan(spy_s.pct_from_open):
            # Find day open for stock
            target_date = entry_ts.date()
            stock_open = _day_opens.get((sym, target_date))
            if stock_open and stock_open > 0:
                stock_pct = (entry_price - stock_open) / stock_open * 100.0

        ctx = compute_market_context(spy_s, qqq_s, sector_snapshot=sec_s,
                                     stock_pct_from_open=stock_pct)
        return read_tape(ctx, weights)

    # ── Pre-compute day opens for all symbols ──
    _day_opens: Dict[Tuple[str, object], float] = {}
    print("\n  Loading symbol data and computing day opens...")
    all_sym_bars: Dict[str, List[Bar]] = {}
    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue
        all_sym_bars[sym] = bars
        current_date = None
        for b in bars:
            d = b.timestamp.date()
            if d != current_date:
                current_date = d
                _day_opens[(sym, d)] = b.open

    # ════════════════════════════════════════════════════════════
    #  REPLAY LONG BOOK
    # ════════════════════════════════════════════════════════════
    print("\n  Running long book backtest...")

    long_cfg = OverlayConfig()
    long_cfg.show_ema_scalp = False
    long_cfg.show_failed_bounce = False
    long_cfg.show_spencer = False
    long_cfg.show_ema_fpip = False
    long_cfg.show_sc_v2 = False

    raw_longs: List[Tuple[Trade, str]] = []  # (trade, symbol)
    for sym in symbols:
        if sym not in all_sym_bars:
            continue
        bars = all_sym_bars[sym]
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        result = run_backtest(bars, cfg=long_cfg, spy_bars=spy_bars,
                              qqq_bars=qqq_bars, sector_bars=sec_bars)
        for t in result.trades:
            raw_longs.append((t, sym))

    # Apply frozen long filters: Non-RED, Q>=2, <15:30
    def is_red_trade(t):
        return spy_day_info.get(t.signal.timestamp.date(), {}).get("direction") == "RED"

    def hhmm(t):
        ts = t.signal.timestamp
        return ts.hour * 100 + ts.minute

    frozen_longs_raw = [(t, sym) for t, sym in raw_longs
                        if not is_red_trade(t)
                        and t.signal.quality_score >= 2
                        and hhmm(t) < 1530]

    # ALL longs with Q>=2, <15:30 (no regime filter — for tape-only variants)
    all_qual_longs_raw = [(t, sym) for t, sym in raw_longs
                          if t.signal.quality_score >= 2
                          and hhmm(t) < 1530]

    # Tag each long with tape reading
    def tag_long(t: Trade, sym: str) -> Optional[TaggedTrade]:
        ut = wrap_engine_trade(t, sym)
        tape = get_tape(sym, t.signal.timestamp, t.signal.entry_price)
        if tape is None:
            return None
        regime = spy_day_info.get(t.signal.timestamp.date(), {})
        old_label = regime.get("direction", "UNK") + "+" + regime.get("character", "UNK")
        return TaggedTrade(ut, tape, old_label)

    print("  Tagging longs with tape readings...")
    frozen_longs: List[TaggedTrade] = []
    for t, sym in frozen_longs_raw:
        tt = tag_long(t, sym)
        if tt:
            frozen_longs.append(tt)

    all_qual_longs: List[TaggedTrade] = []
    for t, sym in all_qual_longs_raw:
        tt = tag_long(t, sym)
        if tt:
            all_qual_longs.append(tt)

    # ════════════════════════════════════════════════════════════
    #  REPLAY SHORT BOOK (BDR)
    # ════════════════════════════════════════════════════════════
    print("  Running BDR scanner...")

    cfg = OverlayConfig()
    rt_dates = set(d for d, info in spy_day_info.items()
                   if info["direction"] == "RED" and info["character"] == "TREND")

    all_bdr_trades: List[BDRTrade] = []
    for sym in symbols:
        if sym not in all_sym_bars:
            continue
        bars = all_sym_bars[sym]
        if len(bars) < 30:
            continue
        ctxs = compute_bar_contexts(bars)
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        mkt_map = build_market_context_map(spy_bars, qqq_bars, bars, sec_bars)
        candidates = scan_for_breakdowns(bars, ctxs, mkt_map, sym)
        for t in candidates:
            simulate_exit_dynamic_slip(t, bars, ctxs, cfg)
        all_bdr_trades.extend(candidates)

    # Frozen short: RED+TREND, wick>=30%, AM <11:00
    frozen_shorts_bdr = [t for t in all_bdr_trades
                         if t.rejection_wick_pct >= 0.30
                         and t.entry_time.date() in rt_dates
                         and t.entry_time.hour * 100 + t.entry_time.minute < 1100]

    # ALL BDR with wick>=30% + AM <11:00 (no regime filter — for tape variants)
    all_bdr_am = [t for t in all_bdr_trades
                  if t.rejection_wick_pct >= 0.30
                  and t.entry_time.hour * 100 + t.entry_time.minute < 1100]

    # Tag shorts with tape readings
    def tag_bdr(t: BDRTrade) -> Optional[TaggedTrade]:
        ut = wrap_bdr_trade(t)
        tape = get_tape(t.sym, t.entry_time, t.entry_price)
        if tape is None:
            return None
        regime = spy_day_info.get(t.entry_time.date(), {})
        old_label = regime.get("direction", "UNK") + "+" + regime.get("character", "UNK")
        return TaggedTrade(ut, tape, old_label)

    print("  Tagging shorts with tape readings...")
    frozen_shorts: List[TaggedTrade] = []
    for t in frozen_shorts_bdr:
        tt = tag_bdr(t)
        if tt:
            frozen_shorts.append(tt)

    all_bdr_am_tagged: List[TaggedTrade] = []
    for t in all_bdr_am:
        tt = tag_bdr(t)
        if tt:
            all_bdr_am_tagged.append(tt)

    print(f"\n  Frozen longs: {len(frozen_longs)} trades")
    print(f"  All qual longs (no regime gate): {len(all_qual_longs)} trades")
    print(f"  Frozen shorts: {len(frozen_shorts)} trades")
    print(f"  All BDR AM (no regime gate): {len(all_bdr_am_tagged)} trades")
    print(f"  RED+TREND days: {len(rt_dates)}")

    # ════════════════════════════════════════════════════════════
    #  SECTION 1: LONG SIDE COMPARISON
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("  SECTION 1: LONG SIDE — Frozen Regime Gate vs Tape Permission")
    print("=" * 120)

    # Variant 1: Frozen (Non-RED gate)
    print_row("1. Frozen long book (Non-RED, Q>=2, <15:30)", compute_metrics_r(frozen_longs))

    # Tape variants: replace regime gate with tape permission
    # Use ALL qualified longs (including RED days), filtered by tape
    for thresh in [0.30, 0.40, 0.50]:
        tape_longs = [t for t in all_qual_longs if t.long_perm >= thresh]
        print_row(f"{2 + [0.30, 0.40, 0.50].index(thresh)}. Tape long_perm >= {thresh:.2f} (replaces Non-RED)",
                  compute_metrics_r(tape_longs))

    # Train/test and robustness for each
    train_test_report(frozen_longs, "Frozen long")
    robustness_check(frozen_longs, "Frozen long")

    for thresh in [0.30, 0.40, 0.50]:
        tape_longs = [t for t in all_qual_longs if t.long_perm >= thresh]
        train_test_report(tape_longs, f"Tape long >= {thresh}")
        robustness_check(tape_longs, f"Tape long >= {thresh}")

    # ════════════════════════════════════════════════════════════
    #  SECTION 2: SHORT SIDE COMPARISON
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("  SECTION 2: SHORT SIDE — Frozen Regime Gate vs Tape Permission")
    print("=" * 120)

    # Variant 1: Frozen (RED+TREND)
    print_row("1. Frozen short book (RED+TREND, wick>=30%, AM)", compute_metrics_r(frozen_shorts))

    # Tape variants: replace RED+TREND with tape permission
    for thresh in [0.30, 0.40, 0.50]:
        tape_shorts = [t for t in all_bdr_am_tagged if t.short_perm >= thresh]
        print_row(f"{2 + [0.30, 0.40, 0.50].index(thresh)}. Tape short_perm >= {thresh:.2f} (replaces RED+TREND)",
                  compute_metrics_r(tape_shorts))

    train_test_report(frozen_shorts, "Frozen short")
    robustness_check(frozen_shorts, "Frozen short")

    for thresh in [0.30, 0.40, 0.50]:
        tape_shorts = [t for t in all_bdr_am_tagged if t.short_perm >= thresh]
        train_test_report(tape_shorts, f"Tape short >= {thresh}")
        robustness_check(tape_shorts, f"Tape short >= {thresh}")

    # ════════════════════════════════════════════════════════════
    #  SECTION 3: COMBINED PORTFOLIO COMPARISON
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("  SECTION 3: COMBINED PORTFOLIO — Frozen vs Tape Variants")
    print("=" * 120)

    # Frozen combined
    frozen_combined = frozen_longs + frozen_shorts
    print_row("1. Frozen Portfolio C (Non-RED longs + RED+TREND shorts)",
              compute_metrics_r(frozen_combined))

    # Best tape threshold combinations
    # Use 0.30 as the "relaxed" threshold for both sides
    for lt, st in [(0.30, 0.30), (0.40, 0.30), (0.40, 0.40), (0.50, 0.40)]:
        tape_l = [t for t in all_qual_longs if t.long_perm >= lt]
        tape_s = [t for t in all_bdr_am_tagged if t.short_perm >= st]
        tape_comb = tape_l + tape_s
        label = f"Tape L>={lt:.2f} + S>={st:.2f}"
        print_row(label, compute_metrics_r(tape_comb))

    # Detailed reports for frozen vs best tape variant
    train_test_report(frozen_combined, "Frozen Portfolio C")
    robustness_check(frozen_combined, "Frozen Portfolio C")

    for lt, st in [(0.30, 0.30), (0.40, 0.40)]:
        tape_l = [t for t in all_qual_longs if t.long_perm >= lt]
        tape_s = [t for t in all_bdr_am_tagged if t.short_perm >= st]
        tape_comb = tape_l + tape_s
        label = f"Tape L>={lt} + S>={st}"
        train_test_report(tape_comb, label)
        robustness_check(tape_comb, label)

    # ════════════════════════════════════════════════════════════
    #  SECTION 4: CROSS-REGIME ANALYSIS
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("  SECTION 4: CROSS-REGIME — Trades the Old Model Blocks but Tape Permits")
    print("=" * 120)

    # Longs on RED days (blocked by frozen model)
    red_longs = [t for t in all_qual_longs if t.old_direction == "RED"]
    print(f"\n  LONGS ON OLD RED DAYS (frozen model blocks these):")
    print(f"  Total longs on RED days: {len(red_longs)}")

    if red_longs:
        print_row("  All RED longs (blocked by frozen)", compute_metrics_r(red_longs))
        for thresh in [0.0, 0.10, 0.20, 0.30]:
            allowed = [t for t in red_longs if t.long_perm >= thresh]
            if allowed:
                print_row(f"  RED + tape long_perm >= {thresh:.2f}", compute_metrics_r(allowed))

        # Detailed view of each RED long trade
        print(f"\n  Individual RED long trades:")
        print(f"  {'Symbol':<8} {'Time':<22} {'Setup':<15} {'R':>7} {'Tape':>7} "
              f"{'LPerm':>7} {'Zone':<14} {'Old Regime':<16}")
        print("  " + "-" * 100)
        for t in sorted(red_longs, key=lambda x: x.ut.entry_time or datetime.min):
            ts_str = t.ut.entry_time.strftime("%Y-%m-%d %H:%M") if t.ut.entry_time else "?"
            print(f"  {t.ut.sym:<8} {ts_str:<22} {t.ut.source:<15} {t.ut.pnl_rr:>+7.2f} "
                  f"{t.tape.tape_score:>+7.3f} {t.long_perm:>+7.3f} "
                  f"{t.tape_zone:<14} {t.old_regime:<16}")

    # Shorts on GREEN days (blocked by frozen model)
    green_shorts = [t for t in all_bdr_am_tagged if t.old_direction == "GREEN"]
    print(f"\n  SHORTS ON OLD GREEN DAYS (frozen model blocks these):")
    print(f"  Total shorts on GREEN days: {len(green_shorts)}")

    if green_shorts:
        print_row("  All GREEN shorts (blocked by frozen)", compute_metrics_r(green_shorts))
        for thresh in [0.0, 0.10, 0.20, 0.30]:
            allowed = [t for t in green_shorts if t.short_perm >= thresh]
            if allowed:
                print_row(f"  GREEN + tape short_perm >= {thresh:.2f}", compute_metrics_r(allowed))

        print(f"\n  Individual GREEN short trades:")
        print(f"  {'Symbol':<8} {'Time':<22} {'Setup':<15} {'R':>7} {'Tape':>7} "
              f"{'SPerm':>7} {'Zone':<14} {'Old Regime':<16}")
        print("  " + "-" * 100)
        for t in sorted(green_shorts, key=lambda x: x.ut.entry_time or datetime.min):
            ts_str = t.ut.entry_time.strftime("%Y-%m-%d %H:%M") if t.ut.entry_time else "?"
            print(f"  {t.ut.sym:<8} {ts_str:<22} {t.ut.source:<15} {t.ut.pnl_rr:>+7.2f} "
                  f"{t.tape.tape_score:>+7.3f} {t.short_perm:>+7.3f} "
                  f"{t.tape_zone:<14} {t.old_regime:<16}")

    # Shorts on FLAT days
    flat_shorts = [t for t in all_bdr_am_tagged if t.old_direction == "FLAT"]
    if flat_shorts:
        print(f"\n  SHORTS ON FLAT DAYS:")
        print(f"  Total shorts on FLAT days: {len(flat_shorts)}")
        print_row("  All FLAT shorts", compute_metrics_r(flat_shorts))
        for thresh in [0.0, 0.30]:
            allowed = [t for t in flat_shorts if t.short_perm >= thresh]
            if allowed:
                print_row(f"  FLAT + tape short_perm >= {thresh:.2f}", compute_metrics_r(allowed))

    # ════════════════════════════════════════════════════════════
    #  SUMMARY
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("  SUMMARY")
    print("=" * 120)

    # Count newly allowed trades
    red_allowed_030 = [t for t in red_longs if t.long_perm >= 0.30]
    green_allowed_030 = [t for t in green_shorts if t.short_perm >= 0.30]

    m_frozen = compute_metrics_r(frozen_combined)
    m_tape_030 = compute_metrics_r(
        [t for t in all_qual_longs if t.long_perm >= 0.30] +
        [t for t in all_bdr_am_tagged if t.short_perm >= 0.30]
    )
    m_tape_040 = compute_metrics_r(
        [t for t in all_qual_longs if t.long_perm >= 0.40] +
        [t for t in all_bdr_am_tagged if t.short_perm >= 0.40]
    )

    print(f"""
  Frozen Portfolio C:   N={m_frozen['n']:>4}  PF(R)={_pf(m_frozen['pf_r'])}  Exp={m_frozen['exp_r']:>+.3f}R  TotalR={m_frozen['total_r']:>+.2f}
  Tape L>=0.30 S>=0.30: N={m_tape_030['n']:>4}  PF(R)={_pf(m_tape_030['pf_r'])}  Exp={m_tape_030['exp_r']:>+.3f}R  TotalR={m_tape_030['total_r']:>+.2f}
  Tape L>=0.40 S>=0.40: N={m_tape_040['n']:>4}  PF(R)={_pf(m_tape_040['pf_r'])}  Exp={m_tape_040['exp_r']:>+.3f}R  TotalR={m_tape_040['total_r']:>+.2f}

  Cross-regime trades newly allowed by tape (at >= 0.30):
    Longs on RED days:    {len(red_allowed_030)} trades  (R total = {sum(t.ut.pnl_rr for t in red_allowed_030):+.2f})
    Shorts on GREEN days: {len(green_allowed_030)} trades  (R total = {sum(t.ut.pnl_rr for t in green_allowed_030):+.2f})
""")

    print("=" * 120)
    print("  STUDY COMPLETE")
    print("=" * 120)


if __name__ == "__main__":
    main()
