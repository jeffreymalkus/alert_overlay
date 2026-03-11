"""
Failed Bounce Structural Redesign Study.

Tests 3 structural variants of FB entry/stop logic against the current baseline.
Does NOT modify engine.py or the locked long baseline.
Instead, post-processes FB signals by re-simulating entry/exit on the raw bars.

Variants:
  CURRENT  — Current FB: enter on confirmation bar close, stop = bounce_high + $0.02
  A        — Later confirmation: skip 1 bar after current signal, enter only if
             next bar closes below current signal bar's low (re-break confirmation)
  B        — Structure stop: keep current entry, widen stop to
             max(bounce_high, highest_since_bounce) + 0.5*ATR
  C        — A + B combined

For each variant:
  trade count, WR, PF, expectancy, PnL, max drawdown, stop rate,
  quick-stop rate (1-3 bars), average hold time

Usage:
    python -m alert_overlay.fb_variant_study --universe all94
"""

import argparse
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from .backtest import load_bars_from_csv, run_backtest, Trade
from .config import OverlayConfig
from .models import NaN, SetupId, Bar

EASTERN = ZoneInfo("US/Eastern")
DATA_DIR = Path(__file__).parent / "data"
WATCHLIST_FILE = Path(__file__).parent / "watchlist.txt"

SESSION_END_HHMM = 1555


def load_watchlist(path=WATCHLIST_FILE):
    symbols = []
    with open(path) as f:
        for line in f:
            sym = line.strip().upper()
            if sym and not sym.startswith("#"):
                symbols.append(sym)
    return symbols


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "∞"


# ══════════════════════════════════════════════════════════════
#  SIMULATED TRADE RESULT
# ══════════════════════════════════════════════════════════════

@dataclass
class SimTrade:
    """Simulated trade for variant comparison."""
    sym: str
    entry_price: float
    entry_time: object
    exit_price: float
    exit_time: object
    exit_reason: str       # stop, time, eod, ema9trail
    stop_price: float
    pnl_points: float
    pnl_rr: float
    risk: float
    bars_held: int
    variant: str
    quality: int
    entry_bar_idx: int


def compute_variant_metrics(trades: List[SimTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "exp": 0.0,
                "stop_rate": 0.0, "quick_stop_rate": 0.0, "avg_hold": 0.0,
                "max_dd": 0.0, "avg_win": 0.0, "avg_loss": 0.0}
    wins = [t for t in trades if t.pnl_points > 0]
    losses = [t for t in trades if t.pnl_points <= 0]
    pnl = sum(t.pnl_points for t in trades)
    gw = sum(t.pnl_points for t in wins)
    gl = abs(sum(t.pnl_points for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    quick_stopped = sum(1 for t in trades if t.exit_reason == "stop" and t.bars_held <= 3)
    avg_hold = statistics.mean(t.bars_held for t in trades)
    avg_win = gw / len(wins) if wins else 0.0
    avg_loss = gl / len(losses) if losses else 0.0
    # Max drawdown
    cum = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl_points
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum
    return {"n": n, "wr": len(wins) / n * 100, "pf": pf, "pnl": pnl,
            "exp": sum(t.pnl_rr for t in trades) / n,
            "stop_rate": stopped / n * 100,
            "quick_stop_rate": quick_stopped / n * 100,
            "avg_hold": avg_hold, "max_dd": dd,
            "avg_win": avg_win, "avg_loss": avg_loss}


def print_variant_metrics(label, m):
    print(f"  {label:20s}  N={m['n']:3d}  WR={m['wr']:5.1f}%  PF={pf_str(m['pf']):>6s}  "
          f"PnL={m['pnl']:+8.2f}  Exp={m['exp']:+.2f}R  "
          f"Stop={m['stop_rate']:4.1f}%  QStop={m['quick_stop_rate']:4.1f}%  "
          f"AvgHold={m['avg_hold']:4.1f}  MaxDD={m['max_dd']:.2f}")


# ══════════════════════════════════════════════════════════════
#  DYNAMIC SLIPPAGE (simplified — matches backtest.py logic)
# ══════════════════════════════════════════════════════════════

def compute_slippage(cfg, bar_price, i_atr):
    """Simplified slippage for SHORT_STRUCT family."""
    if not cfg.use_dynamic_slippage:
        return cfg.slippage_per_side
    base = bar_price * cfg.slip_bps
    base = max(base, cfg.slip_min)
    # Vol multiplier
    vol_mult = 1.0  # simplified — we don't have bar-level vol tracking here
    # Family mult for SHORT_STRUCT
    family_mult = 1.3  # slip_family_mult_reversal (SHORT_STRUCT uses this)
    return base * vol_mult * family_mult


# ══════════════════════════════════════════════════════════════
#  VARIANT SIMULATION
# ══════════════════════════════════════════════════════════════

def simulate_fb_variants(
    bars: List[Bar],
    fb_trades: List[Trade],
    sym: str,
    cfg: OverlayConfig,
    time_stop_bars: int = 8,
) -> Dict[str, List[SimTrade]]:
    """
    Given a list of FB trades (from the engine) and the raw bars,
    simulate 4 entry/stop variants.

    CURRENT: Uses the trade as-is from the backtest.
    A (Later confirm): Delays entry by 1+ bars — waits for next bar to close
       below the signal bar's low.
    B (Structure stop): Same entry as CURRENT but widens stop by 0.5*ATR.
    C: A entry + B stop.
    """
    results: Dict[str, List[SimTrade]] = {
        "CURRENT": [], "A": [], "B": [], "C": []
    }

    # Pre-compute intra ATR at each bar (rolling 14-bar average range)
    atr_at = [0.0] * len(bars)
    rng_buf = []
    for i, b in enumerate(bars):
        rng_buf.append(b.high - b.low)
        if len(rng_buf) > 14:
            rng_buf.pop(0)
        atr_at[i] = statistics.mean(rng_buf) if rng_buf else 0.01

    comm = cfg.commission_per_share

    for trade in fb_trades:
        sig = trade.signal
        bi = sig.bar_index  # index of the confirmation/entry bar

        if bi < 0 or bi >= len(bars):
            continue

        entry_bar = bars[bi]
        i_atr = atr_at[bi] if atr_at[bi] > 0 else 0.01
        slip = compute_slippage(cfg, entry_bar.close, i_atr)
        entry_cost = slip + comm

        # ── CURRENT variant (reproduce from trade) ──
        results["CURRENT"].append(SimTrade(
            sym=sym,
            entry_price=trade.signal.entry_price,
            entry_time=trade.signal.timestamp,
            exit_price=trade.exit_price,
            exit_time=trade.exit_time,
            exit_reason=trade.exit_reason,
            stop_price=trade.signal.stop_price,
            pnl_points=trade.pnl_points,
            pnl_rr=trade.pnl_rr,
            risk=trade.signal.risk,
            bars_held=trade.bars_held,
            variant="CURRENT",
            quality=trade.signal.quality_score,
            entry_bar_idx=bi,
        ))

        # ── Helper: simulate exit from a given entry bar index ──
        def sim_exit(entry_idx: int, entry_price: float, stop_price: float,
                     risk: float, max_bars: int) -> Tuple[float, object, str, int]:
            """Walk forward from entry_idx, return (exit_price, exit_time, reason, bars_held)."""
            entry_date = bars[entry_idx].timestamp.date()
            for j in range(entry_idx + 1, len(bars)):
                b = bars[j]
                held = j - entry_idx

                # Day boundary → close at prior bar close
                if b.timestamp.date() != entry_date:
                    prev = bars[j - 1]
                    return prev.close, prev.timestamp, "eod", held - 1

                # Session end
                if b.time_hhmm >= SESSION_END_HHMM:
                    return b.close, b.timestamp, "eod", held

                # Stop hit (short: bar.high >= stop)
                if b.high >= stop_price:
                    return stop_price, b.timestamp, "stop", held

                # Time stop
                if held >= max_bars:
                    return b.close, b.timestamp, "time", held

            # End of data
            last = bars[-1]
            return last.close, last.timestamp, "eod", len(bars) - 1 - entry_idx

        # ── VARIANT A: Later confirmation ──
        # Wait for the NEXT bar after the signal bar to close below the signal bar's low.
        # This means the signal bar's low acts as a "re-break pivot".
        # Scan up to 3 bars after the signal bar for the re-break.
        a_entry_idx = None
        a_entry_price = None
        signal_bar_low = entry_bar.low

        for k in range(bi + 1, min(bi + 4, len(bars))):
            candidate = bars[k]
            # Must be same day
            if candidate.timestamp.date() != entry_bar.timestamp.date():
                break
            if candidate.time_hhmm >= SESSION_END_HHMM:
                break
            # Re-break: close below the signal bar's low, and bar is bearish
            if candidate.close < signal_bar_low and candidate.close < candidate.open:
                a_entry_idx = k
                a_entry_price = candidate.close
                break

        if a_entry_idx is not None:
            a_slip = compute_slippage(cfg, a_entry_price, atr_at[a_entry_idx])
            a_filled = a_entry_price - (a_slip + comm)  # short: fill below close
            a_stop = sig.stop_price  # same stop as current
            a_risk = abs(a_filled - a_stop)
            if a_risk <= 0:
                a_risk = 0.01

            ep, et, er, bh = sim_exit(a_entry_idx, a_filled, a_stop, a_risk, time_stop_bars)
            # Compute PnL (short direction)
            exit_slip = compute_slippage(cfg, ep, atr_at[min(a_entry_idx + bh, len(bars) - 1)])
            adj_exit = ep + (exit_slip + comm)  # short: adverse exit is higher
            pnl_pts = (a_filled - adj_exit)  # short: entry - exit
            pnl_rr = pnl_pts / a_risk if a_risk > 0 else 0.0

            results["A"].append(SimTrade(
                sym=sym,
                entry_price=a_filled,
                entry_time=bars[a_entry_idx].timestamp,
                exit_price=ep,
                exit_time=et,
                exit_reason=er,
                stop_price=a_stop,
                pnl_points=pnl_pts,
                pnl_rr=pnl_rr,
                risk=a_risk,
                bars_held=bh,
                variant="A",
                quality=sig.quality_score,
                entry_bar_idx=a_entry_idx,
            ))

        # ── VARIANT B: Structure-based stop ──
        # Same entry as CURRENT, but widen stop.
        # New stop = max(bounce_bar_high, highest_since_bounce) + 0.5 * ATR
        # We reconstruct this from the bars: scan back from bi to find the
        # highest high between breakdown and entry.
        lookback_start = max(0, bi - 10)
        highest_in_bounce = max(b.high for b in bars[lookback_start:bi + 1])
        b_stop = highest_in_bounce + 0.5 * i_atr

        # Make sure stop is at least as wide as current
        b_stop = max(b_stop, sig.stop_price)

        b_filled = sig.entry_price + (entry_cost * sig.direction)  # same fill as current
        # Wait — for short, direction = -1, so filled_entry = entry - cost
        # Actually matching backtest: filled_entry = entry_price + (entry_cost * direction)
        # For short (dir=-1): filled = entry_price - entry_cost → lower fill (adverse)
        b_filled = sig.entry_price + (entry_cost * (-1))  # short direction

        b_risk = abs(b_filled - b_stop)
        if b_risk <= 0:
            b_risk = 0.01

        ep, et, er, bh = sim_exit(bi, b_filled, b_stop, b_risk, time_stop_bars)
        exit_slip = compute_slippage(cfg, ep, atr_at[min(bi + bh, len(bars) - 1)])
        adj_exit = ep + (exit_slip + comm)
        pnl_pts = b_filled - adj_exit
        pnl_rr = pnl_pts / b_risk if b_risk > 0 else 0.0

        results["B"].append(SimTrade(
            sym=sym,
            entry_price=b_filled,
            entry_time=sig.timestamp,
            exit_price=ep,
            exit_time=et,
            exit_reason=er,
            stop_price=b_stop,
            pnl_points=pnl_pts,
            pnl_rr=pnl_rr,
            risk=b_risk,
            bars_held=bh,
            variant="B",
            quality=sig.quality_score,
            entry_bar_idx=bi,
        ))

        # ── VARIANT C: Later confirmation + structure stop ──
        if a_entry_idx is not None:
            c_atr = atr_at[a_entry_idx] if atr_at[a_entry_idx] > 0 else 0.01
            # Recalculate highest including the bars between signal and new entry
            c_lookback_start = max(0, a_entry_idx - 12)
            c_highest = max(b.high for b in bars[c_lookback_start:a_entry_idx + 1])
            c_stop = c_highest + 0.5 * c_atr
            c_stop = max(c_stop, sig.stop_price)

            c_slip = compute_slippage(cfg, a_entry_price, c_atr)
            c_filled = a_entry_price - (c_slip + comm)
            c_risk = abs(c_filled - c_stop)
            if c_risk <= 0:
                c_risk = 0.01

            ep, et, er, bh = sim_exit(a_entry_idx, c_filled, c_stop, c_risk, time_stop_bars)
            exit_slip = compute_slippage(cfg, ep, atr_at[min(a_entry_idx + bh, len(bars) - 1)])
            adj_exit = ep + (exit_slip + comm)
            pnl_pts = c_filled - adj_exit
            pnl_rr = pnl_pts / c_risk if c_risk > 0 else 0.0

            results["C"].append(SimTrade(
                sym=sym,
                entry_price=c_filled,
                entry_time=bars[a_entry_idx].timestamp,
                exit_price=ep,
                exit_time=et,
                exit_reason=er,
                stop_price=c_stop,
                pnl_points=pnl_pts,
                pnl_rr=pnl_rr,
                risk=c_risk,
                bars_held=bh,
                variant="C",
                quality=sig.quality_score,
                entry_bar_idx=a_entry_idx,
            ))

    return results


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="all94")
    args = parser.parse_args()

    from .market_context import SECTOR_MAP, get_sector_etf

    if args.universe == "all94":
        excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
        symbols = sorted([
            p.stem.replace("_5min", "")
            for p in DATA_DIR.glob("*_5min.csv")
            if p.stem.replace("_5min", "") not in excluded
        ])
    elif args.universe == "watchlist":
        symbols = load_watchlist()
    else:
        symbols = [s.strip().upper() for s in args.universe.split(",")]

    print("=" * 100)
    print("FAILED BOUNCE STRUCTURAL REDESIGN STUDY")
    print("=" * 100)
    print(f"Universe: {len(symbols)} symbols\n")

    print("Variants:")
    print("  CURRENT — Current FB: confirm bar breaks below bounce_low → enter, stop = bounce_high + $0.02")
    print("  A       — Later confirmation: wait for next bar to re-break below signal bar low")
    print("  B       — Structure stop: same entry, stop = max(bounce highs) + 0.5×ATR")
    print("  C       — A + B combined\n")

    # Load SPY/QQQ/sector for market context
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    # Config: enable FB with min quality=0 for max sample
    cfg = OverlayConfig(
        show_reversal_setups=False,
        show_trend_setups=True,
        show_ema_retest=False,
        show_ema_mean_rev=False,
        show_ema_pullback=False,
        show_second_chance=True,
        show_spencer=False,
        show_failed_bounce=True,
        show_ema_scalp=False,
        show_ema_fpip=False,
        show_sc_v2=False,
        vk_long_only=False,
        sc_long_only=False,
        sp_long_only=False,
        use_market_context=True,
        use_sector_context=True,
        use_tradability_gate=False,
        fb_min_quality=0,
    )

    all_variants: Dict[str, List[SimTrade]] = {"CURRENT": [], "A": [], "B": [], "C": []}

    for sym in symbols:
        fpath = DATA_DIR / f"{sym}_5min.csv"
        if not fpath.exists():
            continue
        bars = load_bars_from_csv(str(fpath))
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)

        fb_trades = [t for t in result.trades if t.signal.setup_id == SetupId.FAILED_BOUNCE]
        if not fb_trades:
            continue

        variant_trades = simulate_fb_variants(bars, fb_trades, sym, cfg,
                                               time_stop_bars=cfg.fb_time_stop_bars)

        for v in all_variants:
            all_variants[v].extend(variant_trades[v])

    # ══════════════════════════════════════════════════════════
    # SECTION 1: HEAD-TO-HEAD COMPARISON
    # ══════════════════════════════════════════════════════════
    print("=" * 100)
    print("SECTION 1: HEAD-TO-HEAD VARIANT COMPARISON")
    print("=" * 100)

    print(f"\n  {'Variant':20s}  {'N':>4s}  {'WR':>6s}  {'PF':>6s}  {'PnL':>9s}  {'Exp':>6s}  "
          f"{'Stop%':>6s}  {'QStop%':>7s}  {'AvgHld':>7s}  {'MaxDD':>7s}")
    print(f"  {'-'*20}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*9}  {'-'*6}  "
          f"{'-'*6}  {'-'*7}  {'-'*7}  {'-'*7}")

    for v in ["CURRENT", "A", "B", "C"]:
        m = compute_variant_metrics(all_variants[v])
        print_variant_metrics(v, m)

    # ══════════════════════════════════════════════════════════
    # SECTION 2: STOP ANALYSIS PER VARIANT
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("SECTION 2: STOP ANALYSIS PER VARIANT")
    print("=" * 100)

    for v in ["CURRENT", "A", "B", "C"]:
        trades = all_variants[v]
        if not trades:
            print(f"\n  {v}: No trades")
            continue

        stopped = [t for t in trades if t.exit_reason == "stop"]
        non_stop = [t for t in trades if t.exit_reason != "stop"]
        quick_stop = [t for t in stopped if t.bars_held <= 3]

        print(f"\n  {v}:")
        print(f"    Stopped: {len(stopped):3d} ({len(stopped)/len(trades)*100:.1f}%)  "
              f"Quick-stop (≤3 bars): {len(quick_stop):3d} ({len(quick_stop)/len(trades)*100:.1f}%)")

        if stopped:
            stop_pnl = sum(t.pnl_points for t in stopped)
            stop_avg = statistics.mean(t.pnl_points for t in stopped)
            print(f"    Stop PnL: {stop_pnl:+.2f}  Avg stop loss: {stop_avg:+.2f}")
        if quick_stop:
            qs_pnl = sum(t.pnl_points for t in quick_stop)
            qs_avg = statistics.mean(t.pnl_points for t in quick_stop)
            print(f"    Quick-stop PnL: {qs_pnl:+.2f}  Avg quick-stop loss: {qs_avg:+.2f}")
        if non_stop:
            ns_pnl = sum(t.pnl_points for t in non_stop)
            ns_wr = sum(1 for t in non_stop if t.pnl_points > 0) / len(non_stop) * 100
            print(f"    Non-stop PnL: {ns_pnl:+.2f}  WR: {ns_wr:.1f}%  N: {len(non_stop)}")

    # ══════════════════════════════════════════════════════════
    # SECTION 3: BARS HELD DISTRIBUTION
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("SECTION 3: BARS HELD DISTRIBUTION PER VARIANT")
    print("=" * 100)

    for v in ["CURRENT", "A", "B", "C"]:
        trades = all_variants[v]
        if not trades:
            continue

        print(f"\n  {v}:")
        for label, lo, hi in [("1-2 bars", 1, 3), ("3-4 bars", 3, 5),
                               ("5-6 bars", 5, 7), ("7-8 bars", 7, 9), ("9+ bars", 9, 999)]:
            subset = [t for t in trades if lo <= t.bars_held < hi]
            if subset:
                m = compute_variant_metrics(subset)
                print(f"    {label:12s}  N={m['n']:3d}  WR={m['wr']:5.1f}%  PF={pf_str(m['pf']):>6s}  "
                      f"PnL={m['pnl']:+8.2f}  Stop%={m['stop_rate']:4.1f}%")

    # ══════════════════════════════════════════════════════════
    # SECTION 4: STOP DISTANCE COMPARISON
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("SECTION 4: STOP DISTANCE COMPARISON")
    print("=" * 100)

    for v in ["CURRENT", "B", "C"]:
        trades = all_variants[v]
        if not trades:
            continue
        risks = [t.risk for t in trades if t.risk > 0]
        if risks:
            print(f"\n  {v}:")
            print(f"    Avg risk (pts): {statistics.mean(risks):.3f}")
            print(f"    Med risk (pts): {statistics.median(risks):.3f}")
            print(f"    Min risk: {min(risks):.3f}  Max risk: {max(risks):.3f}")

    # ══════════════════════════════════════════════════════════
    # SECTION 5: VARIANT A — ENTRY DELAY ANALYSIS
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("SECTION 5: VARIANT A — HOW MANY CURRENT TRADES GET RE-CONFIRMED?")
    print("=" * 100)

    n_current = len(all_variants["CURRENT"])
    n_a = len(all_variants["A"])
    n_dropped = n_current - n_a
    drop_pct = n_dropped / n_current * 100 if n_current > 0 else 0

    print(f"\n  Current FB trades: {n_current}")
    print(f"  Variant A trades:  {n_a} ({n_a/n_current*100:.1f}% re-confirmed)" if n_current > 0 else "")
    print(f"  Dropped by delay:  {n_dropped} ({drop_pct:.1f}%)")

    # What happens to the dropped trades in CURRENT?
    # These are trades where the next bar did NOT re-break below signal bar low.
    if n_current > 0:
        a_entry_idxs = set(t.entry_bar_idx for t in all_variants["A"])
        current_a_match = [t for t in all_variants["CURRENT"]
                          if t.entry_bar_idx in a_entry_idxs or
                          any(at.sym == t.sym and abs((at.entry_time - t.entry_time).total_seconds()) < 600
                              for at in all_variants["A"] if at.sym == t.sym)]

        # Simpler: just use the ones in CURRENT that have no match in A by sym+date
        a_keys = set()
        for t in all_variants["A"]:
            a_keys.add((t.sym, t.entry_time.date() if hasattr(t.entry_time, 'date') else None))

        dropped = [t for t in all_variants["CURRENT"]
                   if (t.sym, t.entry_time.date() if hasattr(t.entry_time, 'date') else None) not in a_keys]
        kept = [t for t in all_variants["CURRENT"]
                if (t.sym, t.entry_time.date() if hasattr(t.entry_time, 'date') else None) in a_keys]

        if dropped:
            m_drop = compute_variant_metrics(dropped)
            print(f"\n  Dropped trades (no re-break):")
            print_variant_metrics("  Dropped", m_drop)
        if kept:
            m_kept = compute_variant_metrics(kept)
            print(f"  Kept trades (re-break confirmed):")
            print_variant_metrics("  Kept", m_kept)

    # ══════════════════════════════════════════════════════════
    # SECTION 6: DIAGNOSIS — ENTRY vs STOP vs BOTH
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("SECTION 6: DIAGNOSIS — IS FB FAILING FROM ENTRY, STOP, OR BOTH?")
    print("=" * 100)

    m_cur = compute_variant_metrics(all_variants["CURRENT"])
    m_a = compute_variant_metrics(all_variants["A"])
    m_b = compute_variant_metrics(all_variants["B"])
    m_c = compute_variant_metrics(all_variants["C"])

    print(f"\n  Key metrics comparison:")
    print(f"  {'':20s}  {'WR':>6s}  {'PF':>6s}  {'QStop%':>7s}  {'PnL':>9s}  {'N':>4s}")
    print(f"  {'-'*20}  {'-'*6}  {'-'*6}  {'-'*7}  {'-'*9}  {'-'*4}")
    for label, m in [("CURRENT", m_cur), ("A (later entry)", m_a),
                     ("B (wider stop)", m_b), ("C (A+B)", m_c)]:
        print(f"  {label:20s}  {m['wr']:5.1f}%  {pf_str(m['pf']):>6s}  {m['quick_stop_rate']:6.1f}%  "
              f"{m['pnl']:+9.2f}  {m['n']:4d}")

    # Derive verdict
    print(f"\n  DIAGNOSIS:")

    # Entry improvement: compare A vs CURRENT
    a_wr_lift = m_a["wr"] - m_cur["wr"] if m_a["n"] > 0 else 0
    a_pf_lift = (m_a["pf"] - m_cur["pf"]) if m_a["n"] > 0 and m_a["pf"] < 999 and m_cur["pf"] < 999 else 0

    # Stop improvement: compare B vs CURRENT
    b_qstop_drop = m_cur["quick_stop_rate"] - m_b["quick_stop_rate"] if m_b["n"] > 0 else 0
    b_pf_lift = (m_b["pf"] - m_cur["pf"]) if m_b["n"] > 0 and m_b["pf"] < 999 and m_cur["pf"] < 999 else 0

    entry_helps = a_wr_lift > 5 or a_pf_lift > 0.3
    stop_helps = b_qstop_drop > 5 or b_pf_lift > 0.3

    if entry_helps and stop_helps:
        print(f"    BOTH entry timing and stop placement are problems.")
        print(f"    Later entry lifts WR by {a_wr_lift:+.1f}%, PF by {a_pf_lift:+.2f}")
        print(f"    Wider stop reduces quick-stops by {b_qstop_drop:+.1f}%, PF by {b_pf_lift:+.2f}")
    elif entry_helps:
        print(f"    ENTRY TIMING is the primary problem.")
        print(f"    Later entry lifts WR by {a_wr_lift:+.1f}%, PF by {a_pf_lift:+.2f}")
        print(f"    Wider stop has limited incremental value.")
    elif stop_helps:
        print(f"    STOP PLACEMENT is the primary problem.")
        print(f"    Wider stop reduces quick-stops by {b_qstop_drop:+.1f}%, PF by {b_pf_lift:+.2f}")
        print(f"    Later entry has limited incremental value.")
    else:
        print(f"    Neither entry delay nor wider stop meaningfully improves FB.")
        print(f"    The setup may have a deeper structural issue.")

    # Combined verdict
    if m_c["n"] > 0 and m_c["pf"] >= 1.0:
        print(f"\n    COMBINED (C) achieves PF={pf_str(m_c['pf'])}, WR={m_c['wr']:.1f}% on N={m_c['n']}")
        print(f"    FB may be viable with structural changes.")
    elif m_c["n"] > 0:
        print(f"\n    COMBINED (C) still PF={pf_str(m_c['pf'])}, WR={m_c['wr']:.1f}% on N={m_c['n']}")
        print(f"    Structural changes alone are insufficient. Setup needs fundamental rethink.")

    # ══════════════════════════════════════════════════════════
    # SECTION 7: TOP TRADE LOG (top 5 wins, top 5 losses per variant)
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("SECTION 7: NOTABLE TRADES (top 5 wins / losses per variant)")
    print("=" * 100)

    for v in ["CURRENT", "A", "B", "C"]:
        trades = all_variants[v]
        if not trades:
            continue

        sorted_by_pnl = sorted(trades, key=lambda t: t.pnl_points, reverse=True)
        top_wins = sorted_by_pnl[:5]
        top_losses = sorted_by_pnl[-5:]

        print(f"\n  {v} — Top wins:")
        for t in top_wins:
            print(f"    {t.sym:>6s}  {t.entry_time}  PnL={t.pnl_points:+7.2f}  "
                  f"Exit={t.exit_reason:>6s}  Held={t.bars_held}b  Risk={t.risk:.2f}")

        print(f"  {v} — Top losses:")
        for t in top_losses:
            print(f"    {t.sym:>6s}  {t.entry_time}  PnL={t.pnl_points:+7.2f}  "
                  f"Exit={t.exit_reason:>6s}  Held={t.bars_held}b  Risk={t.risk:.2f}")


if __name__ == "__main__":
    main()
