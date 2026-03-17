"""
Engine Drift Audit — Find exact code-level mismatches

The proxy research showed 216 shared VR trades where standalone PF=1.10 but
engine PF=0.61 on IDENTICAL entries. That's not a logic/design issue — that's
a code bug or mechanical mismatch.

This script:
1. Runs standalone VR and engine VR side by side
2. Matches trades by (symbol, date, hhmm)
3. Compares entry_price, stop_price, target_price, exit_reason, pnl_rr
4. Categorizes divergence sources
"""

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..backtest import run_backtest, load_bars_from_csv, Trade
from ..config import OverlayConfig
from ..indicators import EMA, VWAPCalc
from ..market_context import MarketEngine, get_sector_etf, SECTOR_MAP
from ..models import Bar, NaN, SetupId

DATA_DIR = Path(__file__).parent.parent / "data"
_isnan = math.isnan


def load_bars(sym: str) -> list:
    p = DATA_DIR / f"{sym}_5min.csv"
    return load_bars_from_csv(str(p)) if p.exists() else []


def get_universe() -> list:
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    return sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])


# ════════════════════════════════════════════════════════════════
#  Standalone VR — returns detailed trade info
# ════════════════════════════════════════════════════════════════

@dataclass
class DetailedTrade:
    symbol: str
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    direction: int
    setup: str
    risk: float = 0.0
    pnl_rr: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0
    exit_price: float = 0.0

    @property
    def entry_date(self) -> date:
        return self.entry_time.date()

    @property
    def hhmm(self) -> int:
        return self.entry_time.hour * 100 + self.entry_time.minute


def simulate_trade_detailed(trade: DetailedTrade, bars: list, bar_idx: int,
                            max_bars: int = 78, target_rr: float = 3.0) -> DetailedTrade:
    risk = trade.risk
    if risk <= 0:
        trade.pnl_rr = 0
        trade.exit_reason = "invalid"
        return trade

    for j in range(bar_idx + 1, min(bar_idx + max_bars + 1, len(bars))):
        b = bars[j]
        trade.bars_held += 1
        if b.timestamp.date() != trade.entry_time.date():
            trade.exit_price = bars[j-1].close
            trade.pnl_rr = (trade.exit_price - trade.entry_price) / risk
            trade.exit_reason = "eod"
            return trade

        # Stop check
        if trade.direction == 1 and b.low <= trade.stop_price:
            trade.exit_price = trade.stop_price
            trade.pnl_rr = (trade.stop_price - trade.entry_price) / risk
            trade.exit_reason = "stop"
            return trade

        # Target check
        if trade.direction == 1 and b.high >= trade.target_price:
            trade.exit_price = trade.target_price
            trade.pnl_rr = target_rr
            trade.exit_reason = "target"
            return trade

    last_bar = bars[min(bar_idx + max_bars, len(bars) - 1)]
    trade.exit_price = last_bar.close
    trade.pnl_rr = (last_bar.close - trade.entry_price) / risk
    trade.exit_reason = "eod"
    return trade


def build_spy_market_snapshots(spy_bars: list) -> dict:
    me = MarketEngine()
    snapshots = {}
    for b in spy_bars:
        snap = me.process_bar(b)
        d = b.timestamp.date()
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute
        snapshots[(d, hhmm)] = snap
    return snapshots


def is_market_aligned_long(spy_ctx: dict, d: date, hhmm: int) -> bool:
    snap = spy_ctx.get((d, hhmm))
    if snap is None or not snap.ready:
        return True
    if not snap.above_vwap and not snap.ema9_above_ema20:
        return False
    return True


def standalone_vr_detailed(bars: list, sym: str, spy_ctx: dict) -> List[DetailedTrade]:
    """VR standalone — NO day filter, returns all possible trades with full details."""
    trades = []
    ema9 = EMA(9)
    vwap = VWAPCalc()
    vol_buf = deque(maxlen=20)
    vol_ma = NaN

    was_below = False
    hold_count = 0
    hold_low = NaN
    triggered_today = None
    prev_date = None

    hold_bars = 3
    target_rr = 3.0
    time_start = 1000
    time_end = 1059

    for i, bar in enumerate(bars):
        ema9.update(bar.close)
        tp = (bar.high + bar.low + bar.close) / 3.0
        d = bar.timestamp.date()
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute

        if d != prev_date:
            vwap.reset()
            was_below = False
            hold_count = 0
            hold_low = NaN
            triggered_today = None
            prev_date = d

        vw = vwap.update(tp, bar.volume)
        if len(vol_buf) == 20:
            vol_ma = sum(vol_buf) / 20
        vol_buf.append(bar.volume)

        if not vwap.ready:
            continue

        above_vwap = bar.close > vw

        # State machine
        if above_vwap and not was_below:
            pass
        elif not above_vwap:
            was_below = True
            hold_count = 0
            hold_low = NaN
        elif was_below and above_vwap and hold_count == 0:
            hold_count = 1
            hold_low = bar.low
        elif hold_count > 0 and hold_count < hold_bars:
            if above_vwap:
                hold_count += 1
                hold_low = min(hold_low, bar.low) if not _isnan(hold_low) else bar.low
            else:
                was_below = True
                hold_count = 0
                hold_low = NaN

        if hhmm < time_start or hhmm > time_end:
            continue
        if triggered_today == d:
            continue

        if hold_count >= hold_bars and above_vwap:
            rng = bar.high - bar.low
            body = abs(bar.close - bar.open)
            is_bull = bar.close > bar.open
            body_pct = body / rng if rng > 0 else 0
            candle_ok = is_bull and body_pct >= 0.40
            vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 0.70 * vol_ma
            market_ok = is_market_aligned_long(spy_ctx, d, hhmm)

            if candle_ok and vol_ok and market_ok:
                stop = (hold_low if not _isnan(hold_low) else bar.low) - 0.02
                risk = bar.close - stop
                if risk > 0:
                    target = bar.close + target_rr * risk
                    t = DetailedTrade(symbol=sym, entry_time=bar.timestamp,
                                      entry_price=bar.close, stop_price=stop,
                                      target_price=target, direction=1,
                                      setup="VR", risk=risk)
                    t = simulate_trade_detailed(t, bars, i, target_rr=target_rr)
                    trades.append(t)
                    triggered_today = d
                    was_below = False
                    hold_count = 0
                    hold_low = NaN

    return trades


# ════════════════════════════════════════════════════════════════
#  Engine runner — extract detailed trade info
# ════════════════════════════════════════════════════════════════

@dataclass
class EngineDetailedTrade:
    symbol: str
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    direction: int
    setup: str
    risk: float = 0.0
    pnl_rr: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0
    exit_price: float = 0.0
    quality: int = 0
    rr_ratio: float = 0.0

    @property
    def entry_date(self) -> date:
        return self.entry_time.date()

    @property
    def hhmm(self) -> int:
        return self.entry_time.hour * 100 + self.entry_time.minute


def run_engine_detailed(cfg_fn, symbols, spy_bars, qqq_bars) -> List[EngineDetailedTrade]:
    """Run engine, extract ALL trades with full details for VR setup."""
    cfg = cfg_fn()
    all_trades = []
    for sym in symbols:
        bars = load_bars(sym)
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = load_bars(sec_etf) if sec_etf and sec_etf not in {"SPY", "QQQ"} else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars,
                              qqq_bars=qqq_bars, sector_bars=sec_bars)
        for t in result.trades:
            if t.signal.setup_name == "VWAP RECLAIM":
                all_trades.append(EngineDetailedTrade(
                    symbol=sym,
                    entry_time=t.signal.timestamp,
                    entry_price=t.signal.entry_price,
                    stop_price=t.signal.stop_price,
                    target_price=t.signal.target_price,
                    direction=t.signal.direction,
                    setup="VR",
                    risk=t.signal.risk,
                    pnl_rr=t.pnl_rr,
                    exit_reason=t.exit_reason,
                    bars_held=t.bars_held,
                    exit_price=t.exit_price if hasattr(t, 'exit_price') else 0.0,
                    quality=t.signal.quality_score,
                    rr_ratio=t.signal.rr_ratio,
                ))
    return all_trades


# ════════════════════════════════════════════════════════════════
#  Main: Side-by-side comparison
# ════════════════════════════════════════════════════════════════

def main():
    symbols = get_universe()
    spy_bars = load_bars("SPY")
    qqq_bars = load_bars("QQQ")
    spy_ctx = build_spy_market_snapshots(spy_bars)

    from .portfolio_configs import candidate_v2_vrgreen_bdr

    print("=" * 140)
    print("ENGINE DRIFT AUDIT — Find exact code-level mismatches for VR")
    print("=" * 140)

    # ── Run standalone (no day filter) ──
    print("\n  Running standalone VR (no day filter)...")
    sa_trades = []
    for sym in symbols:
        bars = load_bars(sym)
        if bars:
            sa_trades.extend(standalone_vr_detailed(bars, sym, spy_ctx))
    print(f"  Standalone: {len(sa_trades)} trades")

    # ── Run engine ──
    print("  Running engine VR...")
    eng_trades = run_engine_detailed(candidate_v2_vrgreen_bdr, symbols, spy_bars, qqq_bars)
    print(f"  Engine: {len(eng_trades)} trades")

    # ── Match trades ──
    sa_by_key = {}
    for t in sa_trades:
        key = (t.symbol, t.entry_date, t.hhmm)
        sa_by_key[key] = t

    eng_by_key = {}
    for t in eng_trades:
        key = (t.symbol, t.entry_date, t.hhmm)
        eng_by_key[key] = t

    shared_keys = set(sa_by_key.keys()) & set(eng_by_key.keys())
    sa_only_keys = set(sa_by_key.keys()) - set(eng_by_key.keys())
    eng_only_keys = set(eng_by_key.keys()) - set(sa_by_key.keys())

    print(f"\n  Shared entries: {len(shared_keys)}")
    print(f"  Standalone-only (engine suppressed): {len(sa_only_keys)}")
    print(f"  Engine-only (engine extras): {len(eng_only_keys)}")

    # ════════════════════════════════════════════════════════════════
    #  Section 1: Shared trade comparison — entry/stop/target deltas
    # ════════════════════════════════════════════════════════════════
    print(f"\n{'=' * 140}")
    print("SECTION 1: SHARED TRADES — Entry/Stop/Target comparison")
    print(f"{'=' * 140}")

    entry_match = 0
    entry_diff = 0
    stop_match = 0
    stop_diff = 0
    target_match = 0
    target_diff = 0
    exit_match = 0
    exit_diff = 0
    pnl_match = 0
    pnl_diff = 0

    entry_diffs = []
    stop_diffs = []
    target_diffs = []
    pnl_diffs = []
    exit_mismatches = []

    for key in sorted(shared_keys):
        sa = sa_by_key[key]
        eng = eng_by_key[key]

        e_diff = abs(sa.entry_price - eng.entry_price)
        s_diff = abs(sa.stop_price - eng.stop_price)
        t_diff = abs(sa.target_price - eng.target_price)
        p_diff = abs(sa.pnl_rr - eng.pnl_rr)

        if e_diff < 0.01:
            entry_match += 1
        else:
            entry_diff += 1
            entry_diffs.append((key, sa.entry_price, eng.entry_price, e_diff))

        if s_diff < 0.01:
            stop_match += 1
        else:
            stop_diff += 1
            stop_diffs.append((key, sa.stop_price, eng.stop_price, s_diff))

        if t_diff < 0.05:
            target_match += 1
        else:
            target_diff += 1
            target_diffs.append((key, sa.target_price, eng.target_price, t_diff))

        if sa.exit_reason == eng.exit_reason:
            exit_match += 1
        else:
            exit_diff += 1
            exit_mismatches.append((key, sa.exit_reason, eng.exit_reason,
                                    sa.pnl_rr, eng.pnl_rr))

        if p_diff < 0.05:
            pnl_match += 1
        else:
            pnl_diff += 1
            pnl_diffs.append((key, sa.pnl_rr, eng.pnl_rr, p_diff,
                              sa.exit_reason, eng.exit_reason,
                              sa.stop_price, eng.stop_price,
                              sa.target_price, eng.target_price))

    n = len(shared_keys) if shared_keys else 1
    print(f"\n  Entry price:  {entry_match:4d} match ({entry_match/n*100:.1f}%)  "
          f"{entry_diff:4d} differ ({entry_diff/n*100:.1f}%)")
    print(f"  Stop price:   {stop_match:4d} match ({stop_match/n*100:.1f}%)  "
          f"{stop_diff:4d} differ ({stop_diff/n*100:.1f}%)")
    print(f"  Target price: {target_match:4d} match ({target_match/n*100:.1f}%)  "
          f"{target_diff:4d} differ ({target_diff/n*100:.1f}%)")
    print(f"  Exit reason:  {exit_match:4d} match ({exit_match/n*100:.1f}%)  "
          f"{exit_diff:4d} differ ({exit_diff/n*100:.1f}%)")
    print(f"  PnL (R):      {pnl_match:4d} match ({pnl_match/n*100:.1f}%)  "
          f"{pnl_diff:4d} differ ({pnl_diff/n*100:.1f}%)")

    # Show some divergent examples
    if entry_diffs:
        print(f"\n  ENTRY PRICE DIFFS (first 10):")
        for key, sa_p, eng_p, diff in sorted(entry_diffs, key=lambda x: -x[3])[:10]:
            print(f"    {key[0]:6s} {key[1]} {key[2]:04d}  SA={sa_p:.2f}  ENG={eng_p:.2f}  Δ={diff:.2f}")

    if stop_diffs:
        print(f"\n  STOP PRICE DIFFS (first 10):")
        for key, sa_p, eng_p, diff in sorted(stop_diffs, key=lambda x: -x[3])[:10]:
            print(f"    {key[0]:6s} {key[1]} {key[2]:04d}  SA={sa_p:.2f}  ENG={eng_p:.2f}  Δ={diff:.2f}")

    if target_diffs:
        print(f"\n  TARGET PRICE DIFFS (first 10):")
        for key, sa_p, eng_p, diff in sorted(target_diffs, key=lambda x: -x[3])[:10]:
            print(f"    {key[0]:6s} {key[1]} {key[2]:04d}  SA={sa_p:.2f}  ENG={eng_p:.2f}  Δ={diff:.2f}")

    # ════════════════════════════════════════════════════════════════
    #  Section 2: Exit reason cross-tab
    # ════════════════════════════════════════════════════════════════
    print(f"\n{'=' * 140}")
    print("SECTION 2: EXIT REASON CROSS-TAB (standalone × engine)")
    print(f"{'=' * 140}")

    cross = defaultdict(int)
    cross_pnl = defaultdict(float)
    for key in shared_keys:
        sa = sa_by_key[key]
        eng = eng_by_key[key]
        cross[(sa.exit_reason, eng.exit_reason)] += 1
        cross_pnl[(sa.exit_reason, eng.exit_reason)] += (eng.pnl_rr - sa.pnl_rr)

    reasons = sorted(set(r for pair in cross.keys() for r in pair))
    print(f"\n  {'':12s}", end="")
    for r in reasons:
        print(f" ENG_{r:6s}", end="")
    print()
    for sa_r in reasons:
        print(f"  SA_{sa_r:8s}", end="")
        for eng_r in reasons:
            count = cross.get((sa_r, eng_r), 0)
            if count > 0:
                delta_pnl = cross_pnl.get((sa_r, eng_r), 0)
                print(f" {count:4d}({delta_pnl:+.1f}R)", end="")
            else:
                print(f"     {'':8s}", end="")
        print()

    # ════════════════════════════════════════════════════════════════
    #  Section 3: PnL divergence analysis
    # ════════════════════════════════════════════════════════════════
    print(f"\n{'=' * 140}")
    print("SECTION 3: PnL DIVERGENCE — Where is the R going?")
    print(f"{'=' * 140}")

    sa_total_r = sum(sa_by_key[k].pnl_rr for k in shared_keys)
    eng_total_r = sum(eng_by_key[k].pnl_rr for k in shared_keys)
    delta_r = eng_total_r - sa_total_r

    print(f"\n  Shared trades: N={len(shared_keys)}")
    print(f"  Standalone TotalR: {sa_total_r:+.2f}R")
    print(f"  Engine TotalR:     {eng_total_r:+.2f}R")
    print(f"  Delta (eng - sa):  {delta_r:+.2f}R")

    # Break down delta by exit reason mismatch category
    print(f"\n  Delta by exit reason transition:")
    for (sa_r, eng_r), count in sorted(cross.items(), key=lambda x: cross_pnl[x[0]]):
        if count == 0:
            continue
        delta = cross_pnl[(sa_r, eng_r)]
        avg_delta = delta / count if count > 0 else 0
        print(f"    SA:{sa_r:6s} → ENG:{eng_r:6s}  N={count:4d}  ΔR={delta:+8.2f}R  avg={avg_delta:+.3f}R/trade")

    # ════════════════════════════════════════════════════════════════
    #  Section 4: Large PnL divergence examples
    # ════════════════════════════════════════════════════════════════
    print(f"\n{'=' * 140}")
    print("SECTION 4: LARGEST PnL DIVERGENCES (top 20)")
    print(f"{'=' * 140}")

    if pnl_diffs:
        sorted_diffs = sorted(pnl_diffs, key=lambda x: -abs(x[3]))[:20]
        print(f"\n  {'Symbol':8s} {'Date':12s} {'HHMM':>4s}  {'SA_PnL':>7s} {'ENG_PnL':>7s} {'Delta':>7s}  "
              f"{'SA_Exit':>7s} {'ENG_Exit':>7s}  {'SA_Stop':>8s} {'ENG_Stop':>8s}  "
              f"{'SA_Tgt':>8s} {'ENG_Tgt':>8s}")
        print(f"  {'-'*8} {'-'*12} {'-'*4}  {'-'*7} {'-'*7} {'-'*7}  "
              f"{'-'*7} {'-'*7}  {'-'*8} {'-'*8}  {'-'*8} {'-'*8}")
        for key, sa_pnl, eng_pnl, diff, sa_exit, eng_exit, sa_stop, eng_stop, sa_tgt, eng_tgt in sorted_diffs:
            print(f"  {key[0]:8s} {str(key[1]):12s} {key[2]:4d}  "
                  f"{sa_pnl:+6.2f}R {eng_pnl:+6.2f}R {eng_pnl-sa_pnl:+6.2f}R  "
                  f"{sa_exit:>7s} {eng_exit:>7s}  "
                  f"{sa_stop:8.2f} {eng_stop:8.2f}  "
                  f"{sa_tgt:8.2f} {eng_tgt:8.2f}")

    # ════════════════════════════════════════════════════════════════
    #  Section 5: Suppressed trade characterization
    # ════════════════════════════════════════════════════════════════
    print(f"\n{'=' * 140}")
    print("SECTION 5: SUPPRESSED TRADES — Why did engine skip them?")
    print(f"{'=' * 140}")

    sa_only = [sa_by_key[k] for k in sa_only_keys]
    if sa_only:
        # PnL stats
        sup_total_r = sum(t.pnl_rr for t in sa_only)
        sup_wins = sum(t.pnl_rr for t in sa_only if t.pnl_rr > 0)
        sup_losses = abs(sum(t.pnl_rr for t in sa_only if t.pnl_rr < 0))
        sup_pf = sup_wins / sup_losses if sup_losses > 0 else float('inf')
        print(f"\n  Suppressed: N={len(sa_only)}  PF={sup_pf:.2f}  TotalR={sup_total_r:+.2f}R")

        # Check if these have a time pattern
        time_dist = defaultdict(list)
        for t in sa_only:
            time_dist[t.hhmm].append(t.pnl_rr)
        print(f"\n  By entry time:")
        for hhmm in sorted(time_dist.keys()):
            trades_at = time_dist[hhmm]
            total = sum(trades_at)
            print(f"    {hhmm:04d}  N={len(trades_at):4d}  TotalR={total:+.2f}R")

        # Check if these come from specific symbols disproportionately
        sym_dist = defaultdict(list)
        for t in sa_only:
            sym_dist[t.symbol].append(t.pnl_rr)
        top_syms = sorted(sym_dist.items(), key=lambda x: -len(x[1]))[:10]
        print(f"\n  Top 10 symbols with most suppressed trades:")
        for sym, pnls in top_syms:
            total = sum(pnls)
            wins = sum(p for p in pnls if p > 0)
            losses = abs(sum(p for p in pnls if p < 0))
            pf = wins / losses if losses > 0 else float('inf')
            print(f"    {sym:8s}  N={len(pnls):3d}  PF={pf:.2f}  TotalR={total:+.2f}R")

    # ════════════════════════════════════════════════════════════════
    #  Section 6: Engine extras — what's generating false triggers?
    # ════════════════════════════════════════════════════════════════
    print(f"\n{'=' * 140}")
    print("SECTION 6: ENGINE EXTRAS — Trades engine generates that standalone doesn't")
    print(f"{'=' * 140}")

    eng_only = [eng_by_key[k] for k in eng_only_keys]
    if eng_only:
        ext_total_r = sum(t.pnl_rr for t in eng_only)
        ext_wins = sum(t.pnl_rr for t in eng_only if t.pnl_rr > 0)
        ext_losses = abs(sum(t.pnl_rr for t in eng_only if t.pnl_rr < 0))
        ext_pf = ext_wins / ext_losses if ext_losses > 0 else float('inf')
        print(f"\n  Extras: N={len(eng_only)}  PF={ext_pf:.2f}  TotalR={ext_total_r:+.2f}R")

        # Time distribution
        time_dist = defaultdict(list)
        for t in eng_only:
            time_dist[t.hhmm].append(t.pnl_rr)
        print(f"\n  By entry time:")
        for hhmm in sorted(time_dist.keys()):
            trades_at = time_dist[hhmm]
            total = sum(trades_at)
            print(f"    {hhmm:04d}  N={len(trades_at):4d}  TotalR={total:+.2f}R")

        # Show some examples
        print(f"\n  Sample extras (first 10):")
        for t in sorted(eng_only, key=lambda x: x.entry_time)[:10]:
            print(f"    {t.symbol:8s} {t.entry_date} {t.hhmm:04d}  "
                  f"entry={t.entry_price:.2f}  stop={t.stop_price:.2f}  "
                  f"tgt={t.target_price:.2f}  pnl={t.pnl_rr:+.2f}R  "
                  f"exit={t.exit_reason}  Q={t.quality}")

    # ════════════════════════════════════════════════════════════════
    #  Section 7: Summary verdict
    # ════════════════════════════════════════════════════════════════
    print(f"\n{'=' * 140}")
    print("SUMMARY VERDICT")
    print(f"{'=' * 140}")

    print(f"\n  Total divergence on shared trades: {delta_r:+.2f}R")
    if entry_diff > 0:
        print(f"  ⚠ ENTRY PRICE MISMATCH: {entry_diff} trades have different entries")
    if stop_diff > 0:
        print(f"  ⚠ STOP PRICE MISMATCH: {stop_diff} trades have different stops")
    if target_diff > 0:
        print(f"  ⚠ TARGET PRICE MISMATCH: {target_diff} trades have different targets")
    if exit_diff > 0:
        print(f"  ⚠ EXIT REASON MISMATCH: {exit_diff} trades exit differently")

    if entry_diff == 0 and stop_diff == 0 and target_diff == 0:
        print(f"\n  → Entries, stops, targets MATCH perfectly.")
        print(f"  → Divergence is entirely in EXIT HANDLING.")
        if exit_diff > 0:
            print(f"  → {exit_diff} trades have different exit reasons despite same entry/stop/target.")
            print(f"  → This points to a backtest exit-simulation code mismatch.")
    elif entry_diff > 0 or stop_diff > 0:
        print(f"\n  → Entry or stop prices differ — this points to state machine divergence.")
        print(f"  → The engine's VR state machine is computing different hold_low / entry conditions.")

    if len(sa_only) > len(shared_keys):
        print(f"\n  ⚠ ENGINE SUPPRESSES {len(sa_only)} of {len(sa_trades)} standalone trades ({len(sa_only)/len(sa_trades)*100:.0f}%)")
        if sup_pf > 1.0:
            print(f"  → Suppressed trades are PROFITABLE (PF={sup_pf:.2f}) — engine gates are hurting")
        else:
            print(f"  → Suppressed trades are unprofitable — engine gates are helping")

    print(f"\n{'=' * 140}")


if __name__ == "__main__":
    main()
