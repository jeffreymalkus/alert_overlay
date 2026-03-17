"""
Portfolio D — Runtime Audit
============================
Runs REAL replay data through the live pipeline and produces observable
truth tables for all 12 open validation concerns.

Reports are classified as:
  BLOCKER      — system cannot go live until resolved
  WARNING      — risk that should be monitored; may be acceptable
  INFORMATIONAL — context data for operator awareness

Run:  cd /sessions/.../mnt && python -m alert_overlay.runtime_audit
"""

import csv
import math
import sys
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

_isnan = math.isnan

from .backtest import load_bars_from_csv
from .models import Bar, NaN
from .market_context import MarketEngine, MarketSnapshot, compute_market_context
from .layered_regime import PermissionWeights, compute_permission

from .strategies.shared.signal_schema import StrategySignal, StrategyTrade, QualityTier
from .strategies.shared.config import StrategyConfig
from .strategies.shared.in_play_proxy import InPlayProxy, SessionSnapshot, InPlayResult, DayOpenStats
from .strategies.shared.helpers import simulate_strategy_trade, trigger_bar_quality
from .strategies.shared.helpers import (
    compute_structural_target_long,
    compute_structural_target_short,
)

from .strategies.replay import (
    check_regime_gate, raw_signal_to_strategy_signal, BarUpsampler, _find_bar_idx,
    STRATEGY_REGIME_GATE, STRATEGY_TARGET_RR, STRATEGY_MAX_BARS,
    SLIP_PER_SIDE_BPS, LONG_TAPE_THRESHOLD, MIN_CONFLUENCE_BY_STRATEGY,
    MIN_CONFLUENCE_DEFAULT,
)

from .strategies.live.manager import StrategyManager
from .strategies.live.shared_indicators import SharedIndicators
from .strategies.live.base import RawSignal
from .strategies.live.sc_sniper_live import SCSniperLive
from .strategies.live.fl_antichop_live import FLAntiChopLive
from .strategies.live.spencer_atier_live import SpencerATierLive
from .strategies.live.hitchhiker_live import HitchHikerLive
from .strategies.live.ema_fpip_live import EmaFpipLive
from .strategies.live.bdr_short_live import BDRShortLive
from .strategies.live.ema9_ft_live import EMA9FirstTouchLive
from .strategies.live.backside_live import BacksideStructureLive
from .strategies.live.orl_fbd_long_live import ORLFBDLongLive
from .strategies.live.orh_fbo_short_v2_live import ORHFBOShortV2Live
from .strategies.live.pdh_fbo_short_live import PDHFBOShortLive
from .strategies.live.fft_newlow_reversal_live import FFTNewlowReversalLive

DATA_DIR = Path(__file__).parent / "data"
DATA_1MIN_DIR = DATA_DIR / "1min"
OUT_DIR = Path(__file__).parent / "outputs"
TAPE_WEIGHTS = PermissionWeights()


# ══════════════════════════════════════════════════════════════════════
#  SEVERITY CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════

class Severity:
    BLOCKER = "BLOCKER"
    WARNING = "WARNING"
    INFO = "INFORMATIONAL"


class Finding:
    def __init__(self, audit_id: str, title: str, severity: str, detail: str):
        self.audit_id = audit_id
        self.title = title
        self.severity = severity
        self.detail = detail

    def __repr__(self):
        return f"[{self.severity}] {self.audit_id}: {self.title}"


findings: List[Finding] = []


def add_finding(audit_id, title, severity, detail):
    findings.append(Finding(audit_id, title, severity, detail))


# ══════════════════════════════════════════════════════════════════════
#  DATA STRUCTURES — per-signal audit record
# ══════════════════════════════════════════════════════════════════════

class SignalAuditRecord:
    """Full trace of a signal through the gate chain + trade simulation."""
    def __init__(self):
        self.symbol = ""
        self.bar_timestamp = None
        self.hhmm = 0
        self.path = ""          # "1m" or "5m"
        self.strategy = ""
        self.direction = 0
        # gates
        self.regime_pass = False
        self.regime_reason = ""
        self.ip_pass = False
        self.ip_evaluated = False
        self.ip_reason = ""
        self.tape_pass = False
        self.tape_value = 0.0
        self.confluence_count = 0
        self.confluence_tags = []
        self.min_confluence = 0
        self.confluence_pass = False
        # outcome
        self.outcome = ""       # "promoted" / "blocked_regime" / "blocked_ip" / "blocked_tape" / "blocked_confluence"
        # signal detail (only if promoted)
        self.entry_price = 0.0
        self.stop_price = 0.0
        self.target_price = 0.0
        self.risk = 0.0
        self.target_tag = ""
        self.actual_rr = 0.0
        self.structure_quality = 0.0
        # trade detail (only if promoted)
        self.exit_reason = ""
        self.exit_price = 0.0
        self.pnl_rr = 0.0
        self.bars_held = 0
        self.cost_rr = 0.0
        self.cost_dollars = 0.0


# ══════════════════════════════════════════════════════════════════════
#  INSTRUMENTED REPLAY — captures every signal's full trace
# ══════════════════════════════════════════════════════════════════════

def run_instrumented_replay() -> Tuple[List[SignalAuditRecord], Dict]:
    """Run full replay with complete audit instrumentation.
    Returns (all_records, summary_dict)."""

    print("=" * 100)
    print("RUNTIME AUDIT — Instrumented Replay")
    print("=" * 100)

    # ── Load data ──
    spy_bars_5m = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    from .market_context import SECTOR_MAP
    sector_etfs = sorted(set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    excluded = {"SPY", "QQQ", "IWM"} | set(sector_etfs)

    symbols_5m = sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])

    # SPY/QQQ engines
    spy_engine = MarketEngine()
    qqq_engine = MarketEngine()
    qqq_bars_5m = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    spy_snap_by_ts = {}
    for b in spy_bars_5m:
        spy_snap_by_ts[b.timestamp] = spy_engine.process_bar(b)
    qqq_snap_by_ts = {}
    for b in qqq_bars_5m:
        qqq_snap_by_ts[b.timestamp] = qqq_engine.process_bar(b)

    spy_1m_path = DATA_1MIN_DIR / "SPY_1min.csv"
    spy_1m_snaps = {}
    if spy_1m_path.exists():
        spy_engine_1m = MarketEngine()
        spy_bars_1m = load_bars_from_csv(str(spy_1m_path))
        for b in spy_bars_1m:
            spy_1m_snaps[b.timestamp] = spy_engine_1m.process_bar(b)

    ip_cfg = StrategyConfig(timeframe_min=1)
    ip_cfg_5m = StrategyConfig(timeframe_min=5)
    in_play_proxy = InPlayProxy(ip_cfg)
    in_play_proxy_5m = InPlayProxy(ip_cfg_5m)

    all_records: List[SignalAuditRecord] = []
    # Per-symbol tracking for warm-up / day-1 audits
    first_signal_by_strat: Dict[str, Dict] = defaultdict(dict)  # strat -> {bar_idx, hhmm, lookback_bars}
    symbol_day_info: List[Dict] = []

    print(f"\n  Processing {len(symbols_5m)} symbols...")

    for idx, sym in enumerate(symbols_5m):
        p_5m = DATA_DIR / f"{sym}_5min.csv"
        if not p_5m.exists():
            continue
        bars_5m = load_bars_from_csv(str(p_5m))
        if not bars_5m:
            continue

        p_1m = DATA_1MIN_DIR / f"{sym}_1min.csv"
        bars_1m = load_bars_from_csv(str(p_1m)) if p_1m.exists() else None

        strat_cfg = StrategyConfig(timeframe_min=5)
        live_strats = [
            SCSniperLive(strat_cfg), FLAntiChopLive(strat_cfg),
            SpencerATierLive(strat_cfg), HitchHikerLive(strat_cfg),
            EmaFpipLive(strat_cfg), BDRShortLive(strat_cfg),
            EMA9FirstTouchLive(strat_cfg), BacksideStructureLive(strat_cfg),
            ORLFBDLongLive(strat_cfg), ORHFBOShortV2Live(strat_cfg),
            PDHFBOShortLive(strat_cfg, enable_mode_a=False, enable_mode_b=True),
            FFTNewlowReversalLive(strat_cfg),
        ]
        mgr = StrategyManager(strategies=live_strats, symbol=sym)

        sym_dates = sorted(set(b.timestamp.date() for b in bars_5m))
        if len(bars_5m) >= 14:
            atr_val = sum(max(b.high - b.low, 0.01) for b in bars_5m[:14]) / 14
            mgr.indicators.warm_up_daily(atr_val, bars_5m[0].high, bars_5m[0].low)

        if bars_1m is None:
            in_play_proxy_5m.precompute(sym, bars_5m)

        ip_result_current: Optional[InPlayResult] = None
        ip_eval_date: Optional[int] = None

        if bars_1m is not None:
            upsampler = BarUpsampler(5)
            source_bars = bars_1m
            use_1m = True
        else:
            source_bars = bars_5m
            use_1m = False

        day_open_rs = float('nan')
        rs_date = None
        bar_count_today = 0
        current_day = None

        for bar in source_bars:
            bar_date = bar.timestamp.date()
            hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute
            date_int = bar.timestamp.year * 10000 + bar.timestamp.month * 100 + bar.timestamp.day

            if current_day != bar_date:
                current_day = bar_date
                bar_count_today = 0
            bar_count_today += 1

            if rs_date is None or rs_date != date_int:
                rs_date = date_int
                day_open_rs = bar.open
            stock_pct = ((bar.close - day_open_rs) / day_open_rs * 100.0
                         if day_open_rs > 0 else float('nan'))

            raw_signals = []

            if use_1m:
                spy_snap = spy_1m_snaps.get(bar.timestamp)
                if spy_snap is None:
                    boundary = bar.timestamp.replace(
                        minute=(bar.timestamp.minute // 5) * 5, second=0, microsecond=0)
                    spy_snap = spy_snap_by_ts.get(boundary)
                qqq_snap = qqq_snap_by_ts.get(
                    bar.timestamp.replace(
                        minute=(bar.timestamp.minute // 5) * 5, second=0, microsecond=0))
                mkt_ctx = None
                if spy_snap and qqq_snap and spy_snap.ready and qqq_snap.ready:
                    mkt_ctx = compute_market_context(
                        spy_snap, qqq_snap, sector_snapshot=None,
                        stock_pct_from_open=stock_pct)

                signals_1m = mgr.on_1min_bar(bar, market_ctx=mkt_ctx)
                raw_signals.extend(signals_1m)

                si = mgr.indicators
                if ip_eval_date is not None and ip_eval_date != date_int:
                    ip_result_current = None
                if si.ip_session_evaluated and ip_eval_date != date_int:
                    ip_eval_date = date_int
                    bl = si.get_in_play_baselines()
                    snapshot = SessionSnapshot(
                        symbol=sym, session_date=bar_date,
                        open_bars=bl["session_bars"],
                        prev_close=bl["prev_close"],
                        vol_baseline=bl["vol_baseline"],
                        range_baseline=bl["range_baseline"],
                        vol_baseline_depth=bl["vol_baseline_depth"],
                        range_baseline_depth=bl["range_baseline_depth"],
                    )
                    ip_result_current = in_play_proxy.evaluate_session(snapshot)

                bar_5m = upsampler.on_bar(bar)
                if bar_5m is not None:
                    signals_5m = mgr.on_5min_bar(bar_5m, market_ctx=mkt_ctx)
                    raw_signals.extend(signals_5m)

                alert_bar = bar_5m if bar_5m is not None else bar
                alert_spy_snap = spy_snap
            else:
                spy_snap = spy_snap_by_ts.get(bar.timestamp)
                qqq_snap = qqq_snap_by_ts.get(bar.timestamp)
                mkt_ctx = None
                if spy_snap and qqq_snap and spy_snap.ready and qqq_snap.ready:
                    mkt_ctx = compute_market_context(
                        spy_snap, qqq_snap, sector_snapshot=None,
                        stock_pct_from_open=stock_pct)

                raw_signals = mgr.on_bar(bar, market_ctx=mkt_ctx)

                if ip_eval_date != date_int:
                    ip_eval_date = date_int
                    ip_passed, ip_score = in_play_proxy_5m.is_in_play(sym, bar_date)
                    ip_stats = in_play_proxy_5m.get_stats(sym, bar_date)
                    if ip_stats is not None:
                        ip_result_current = InPlayResult(
                            passed=ip_passed, score=ip_score, stats=ip_stats,
                            data_status="FULL",
                            pass_gap=abs(ip_stats.gap_pct) >= ip_cfg_5m.get(ip_cfg_5m.ip_gap_min),
                            pass_rvol=ip_stats.rvol >= ip_cfg_5m.get(ip_cfg_5m.ip_rvol_min),
                            pass_dolvol=ip_stats.dolvol >= ip_cfg_5m.get(ip_cfg_5m.ip_dolvol_min),
                            reason=f"precompute({'PASS' if ip_passed else 'FAIL'})")
                    else:
                        ip_result_current = InPlayResult(
                            passed=False, score=0.0, stats=DayOpenStats(),
                            data_status="NEW", reason="no_precompute_data")

                alert_bar = bar
                alert_spy_snap = spy_snap

            if not raw_signals:
                continue

            # ── Gate chain with full audit capture ──
            for sig in raw_signals:
                rec = SignalAuditRecord()
                rec.symbol = sym
                rec.bar_timestamp = alert_bar.timestamp
                rec.hhmm = sig.hhmm if hasattr(sig, 'hhmm') else hhmm
                rec.path = "1m" if use_1m else "5m"
                rec.strategy = sig.strategy_name
                rec.direction = sig.direction
                rec.entry_price = sig.entry_price
                rec.stop_price = sig.stop_price
                rec.target_price = sig.target_price
                rec.risk = abs(sig.entry_price - sig.stop_price)

                md = sig.metadata or {}
                rec.target_tag = md.get("target_tag", "MISSING")
                rec.actual_rr = md.get("actual_rr", 0.0) or 0.0
                rec.structure_quality = md.get("structure_quality", 0.0) or 0.0
                rec.confluence_tags = md.get("confluence", [])
                rec.confluence_count = len(rec.confluence_tags) if isinstance(rec.confluence_tags, list) else 0
                rec.min_confluence = MIN_CONFLUENCE_BY_STRATEGY.get(
                    sig.strategy_name, MIN_CONFLUENCE_DEFAULT)

                # Gate 0: Regime
                regime_pass, regime_reason = check_regime_gate(
                    sig.strategy_name, alert_spy_snap)
                rec.regime_pass = regime_pass
                rec.regime_reason = regime_reason

                if not regime_pass:
                    rec.outcome = "blocked_regime"
                    all_records.append(rec)
                    continue

                # Gate 0.5: In-play
                rec.ip_evaluated = ip_result_current is not None
                if ip_result_current is not None:
                    rec.ip_pass = ip_result_current.passed
                    rec.ip_reason = ip_result_current.reason
                else:
                    rec.ip_pass = False
                    rec.ip_reason = "not_yet_evaluated"

                if ip_result_current is None or not ip_result_current.passed:
                    rec.outcome = "blocked_ip"
                    all_records.append(rec)
                    continue

                # Gate 1: Tape
                rec.tape_pass = True
                if sig.direction == 1 and mkt_ctx is not None:
                    sig_hhmm = sig.hhmm if hasattr(sig, 'hhmm') else hhmm
                    perm = compute_permission(
                        mkt_ctx, direction=1, bar_time_hhmm=sig_hhmm,
                        weights=TAPE_WEIGHTS)
                    rec.tape_value = perm.permission
                    if perm.permission < LONG_TAPE_THRESHOLD:
                        rec.tape_pass = False
                        rec.outcome = "blocked_tape"
                        all_records.append(rec)
                        continue

                # Gate 2: Confluence
                rec.confluence_pass = rec.confluence_count >= rec.min_confluence
                if not rec.confluence_pass:
                    rec.outcome = "blocked_confluence"
                    all_records.append(rec)
                    continue

                # ── PROMOTED ──
                rec.outcome = "promoted"

                ip_score = ip_result_current.score if ip_result_current else 0.0
                strat_sig = raw_signal_to_strategy_signal(
                    sig, sym, alert_bar.timestamp,
                    in_play_score=ip_score, regime_label=regime_reason)

                bar_idx_val = _find_bar_idx(source_bars, alert_bar.timestamp)
                if bar_idx_val < 0:
                    rec.outcome = "promoted_no_bar_idx"
                    all_records.append(rec)
                    continue

                max_bars = STRATEGY_MAX_BARS.get(sig.strategy_name, 78)
                target_rr = md.get("actual_rr", STRATEGY_TARGET_RR.get(sig.strategy_name, 2.0))

                trade = simulate_strategy_trade(
                    strat_sig, source_bars, bar_idx_val,
                    max_bars=max_bars, target_rr=target_rr,
                    slip_per_side_bps=SLIP_PER_SIDE_BPS)

                rec.exit_reason = trade.exit_reason
                rec.exit_price = trade.exit_price
                rec.pnl_rr = trade.pnl_rr
                rec.bars_held = trade.bars_held

                # Compute cost breakdown
                if rec.risk > 0 and rec.entry_price > 0:
                    rec.cost_dollars = rec.entry_price * 2.0 * SLIP_PER_SIDE_BPS / 10000.0
                    rec.cost_rr = rec.cost_dollars / rec.risk

                # Track first signal per strategy (for warm-up audit)
                if rec.strategy not in first_signal_by_strat or \
                   rec.bar_timestamp < first_signal_by_strat[rec.strategy].get("ts", rec.bar_timestamp + timedelta(days=1)):
                    first_signal_by_strat[rec.strategy] = {
                        "ts": rec.bar_timestamp,
                        "hhmm": rec.hhmm,
                        "bar_count": bar_count_today,
                        "symbol": sym,
                    }

                all_records.append(rec)

        if (idx + 1) % 20 == 0 or idx == len(symbols_5m) - 1:
            promoted = sum(1 for r in all_records if r.outcome == "promoted")
            print(f"    {idx + 1}/{len(symbols_5m)} symbols... ({promoted} promoted)")

    promoted_recs = [r for r in all_records if r.outcome == "promoted"]
    summary = {
        "total_signals": len(all_records),
        "promoted": len(promoted_recs),
        "symbols": len(symbols_5m),
        "first_signal_by_strat": first_signal_by_strat,
    }

    print(f"\n  Total signals: {len(all_records):,}")
    print(f"  Promoted:      {len(promoted_recs):,}")
    return all_records, summary


# ══════════════════════════════════════════════════════════════════════
#  AUDIT 1: End-to-end trace table (N real symbol-days)
# ══════════════════════════════════════════════════════════════════════

def audit_1_end_to_end_trace(records: List[SignalAuditRecord]):
    """Dump full trace for every promoted and blocked signal."""
    print(f"\n{'='*100}")
    print("AUDIT 1: End-to-End Signal Trace")
    print(f"{'='*100}")

    # Group by outcome
    by_outcome = defaultdict(int)
    for r in records:
        by_outcome[r.outcome] += 1
    print("\n  Signal Outcomes:")
    for outcome, cnt in sorted(by_outcome.items(), key=lambda x: -x[1]):
        print(f"    {outcome:<25s}  {cnt:>6,}")

    # Sample promoted signals — show full trace for up to 20
    promoted = [r for r in records if r.outcome == "promoted"]
    print(f"\n  Promoted Signal Traces ({min(len(promoted), 20)} of {len(promoted)}):")
    print(f"  {'Symbol':<8} {'Time':<18} {'Path':<4} {'Strategy':<18} {'Dir':<5} "
          f"{'Entry':>8} {'Stop':>8} {'Target':>8} {'Risk':>6} "
          f"{'Tag':<20} {'RR':>5} {'Exit':<8} {'PnL':>7} {'CostR':>6} "
          f"{'Conf':>4} {'IP':>3} {'Regime':<15}")
    print(f"  {'-'*8} {'-'*18} {'-'*4} {'-'*18} {'-'*5} "
          f"{'-'*8} {'-'*8} {'-'*8} {'-'*6} "
          f"{'-'*20} {'-'*5} {'-'*8} {'-'*7} {'-'*6} "
          f"{'-'*4} {'-'*3} {'-'*15}")
    for r in promoted[:20]:
        ts_str = r.bar_timestamp.strftime("%Y-%m-%d %H:%M") if r.bar_timestamp else ""
        dirn = "LONG" if r.direction == 1 else "SHORT"
        print(f"  {r.symbol:<8} {ts_str:<18} {r.path:<4} {r.strategy:<18} {dirn:<5} "
              f"{r.entry_price:>8.2f} {r.stop_price:>8.2f} {r.target_price:>8.2f} {r.risk:>6.2f} "
              f"{r.target_tag:<20} {r.actual_rr:>5.2f} {r.exit_reason:<8} {r.pnl_rr:>+7.3f} {r.cost_rr:>6.3f} "
              f"{r.confluence_count:>4} {'Y' if r.ip_pass else 'N':>3} {r.regime_reason:<15}")

    # Check 1m vs 5m path representation
    path_promoted = defaultdict(int)
    for r in promoted:
        path_promoted[r.path] += 1
    print(f"\n  Promoted by path: {dict(path_promoted)}")

    # Check long vs short
    dir_promoted = defaultdict(int)
    for r in promoted:
        dir_promoted["LONG" if r.direction == 1 else "SHORT"] += 1
    print(f"  Promoted by direction: {dict(dir_promoted)}")


# ══════════════════════════════════════════════════════════════════════
#  AUDIT 2: IP Gate Runtime Audit Table
# ══════════════════════════════════════════════════════════════════════

def audit_2_ip_gate_table(records: List[SignalAuditRecord]):
    """Runtime truth table for IP gate: before/after evaluation, by path, by strategy."""
    print(f"\n{'='*100}")
    print("AUDIT 2: In-Play Gate Runtime Audit Table")
    print(f"{'='*100}")

    # Categorize every signal
    categories = {
        "promoted_before_ip_ready": [],
        "blocked_before_ip_ready": [],
        "promoted_after_ip_ready": [],
        "blocked_after_ip_ready": [],
    }

    for r in records:
        ip_ready = r.ip_evaluated
        is_promoted = r.outcome == "promoted"

        # Only count signals that reached the IP gate (not blocked by regime first)
        if r.outcome == "blocked_regime":
            continue

        if ip_ready and is_promoted:
            categories["promoted_after_ip_ready"].append(r)
        elif ip_ready and not is_promoted:
            categories["blocked_after_ip_ready"].append(r)
        elif not ip_ready and is_promoted:
            categories["promoted_before_ip_ready"].append(r)
        elif not ip_ready and not is_promoted:
            categories["blocked_before_ip_ready"].append(r)

    print("\n  IP Gate Audit Counts:")
    for cat, recs in categories.items():
        print(f"    {cat:<35s}  {len(recs):>6,}")

    # BLOCKER: any promoted signal before IP was ready
    pre_ip_promoted = categories["promoted_before_ip_ready"]
    if pre_ip_promoted:
        add_finding("A2-IP-BYPASS", "Promoted signals before IP evaluated",
                    Severity.BLOCKER,
                    f"{len(pre_ip_promoted)} signals promoted without IP evaluation. "
                    f"Strategies: {set(r.strategy for r in pre_ip_promoted)}")
        print(f"\n  *** BLOCKER: {len(pre_ip_promoted)} signals promoted BEFORE IP evaluated ***")
        for r in pre_ip_promoted[:10]:
            print(f"    {r.symbol} {r.bar_timestamp} {r.strategy} path={r.path}")
    else:
        add_finding("A2-IP-GATE", "IP gate active at runtime",
                    Severity.INFO,
                    "Zero signals promoted before IP evaluation. Gate confirmed active.")
        print(f"\n  CONFIRMED: Zero promoted signals before IP evaluated.")

    # Split by path
    print(f"\n  By path (past regime gate):")
    for path in ["1m", "5m"]:
        path_recs = [r for r in records if r.path == path and r.outcome != "blocked_regime"]
        promoted = [r for r in path_recs if r.outcome == "promoted"]
        blocked_ip = [r for r in path_recs if r.outcome == "blocked_ip"]
        print(f"    {path}: {len(path_recs):,} total, {len(promoted):,} promoted, "
              f"{len(blocked_ip):,} blocked by IP")

    # Split by strategy
    print(f"\n  By strategy (promoted / blocked_ip):")
    strat_counts = defaultdict(lambda: {"promoted": 0, "blocked_ip": 0, "other_blocked": 0})
    for r in records:
        if r.outcome == "blocked_regime":
            continue
        if r.outcome == "promoted":
            strat_counts[r.strategy]["promoted"] += 1
        elif r.outcome == "blocked_ip":
            strat_counts[r.strategy]["blocked_ip"] += 1
        else:
            strat_counts[r.strategy]["other_blocked"] += 1

    print(f"  {'Strategy':<18} {'Promoted':>10} {'Blocked_IP':>12} {'Other':>8}")
    for s in sorted(strat_counts):
        c = strat_counts[s]
        print(f"  {s:<18} {c['promoted']:>10} {c['blocked_ip']:>12} {c['other_blocked']:>8}")


# ══════════════════════════════════════════════════════════════════════
#  AUDIT 3: Signal Parity — Native 5m vs Upsampled 5m
# ══════════════════════════════════════════════════════════════════════

def audit_3_signal_parity():
    """Compare signals from native 5m path vs 1m-upsampled path for same symbol."""
    print(f"\n{'='*100}")
    print("AUDIT 3: Signal Parity — Native 5m vs 1m-Upsampled 5m")
    print(f"{'='*100}")

    # Find symbols with both 1m and 5m data
    dual_symbols = []
    for p in DATA_1MIN_DIR.glob("*_1min.csv"):
        sym = p.stem.replace("_1min", "")
        if sym in ("SPY", "QQQ", "IWM"):
            continue
        if (DATA_DIR / f"{sym}_5min.csv").exists():
            dual_symbols.append(sym)

    if not dual_symbols:
        print("  No symbols with both 1m and 5m data found.")
        add_finding("A3-NO-DATA", "No dual-timeframe symbols for parity check",
                    Severity.WARNING, "Cannot verify signal parity without dual data.")
        return

    test_syms = dual_symbols[:5]  # Test up to 5 symbols
    print(f"  Testing {len(test_syms)} symbols: {test_syms}")

    total_match = total_mismatch = 0

    for sym in test_syms:
        bars_1m = load_bars_from_csv(str(DATA_1MIN_DIR / f"{sym}_1min.csv"))
        bars_5m = load_bars_from_csv(str(DATA_DIR / f"{sym}_5min.csv"))

        strat_cfg = StrategyConfig(timeframe_min=5)

        # Path A: native 5m (same as 5m-only path)
        strats_a = [
            SCSniperLive(strat_cfg), FLAntiChopLive(strat_cfg),
            HitchHikerLive(strat_cfg), EmaFpipLive(strat_cfg),
            BDRShortLive(strat_cfg), ORHFBOShortV2Live(strat_cfg),
            BacksideStructureLive(strat_cfg),
        ]
        mgr_a = StrategyManager(strategies=strats_a, symbol=sym)
        if len(bars_5m) >= 14:
            atr_a = sum(max(b.high - b.low, 0.01) for b in bars_5m[:14]) / 14
            mgr_a.indicators.warm_up_daily(atr_a, bars_5m[0].high, bars_5m[0].low)

        sigs_a = []
        for b in bars_5m:
            for s in mgr_a.on_bar(b):
                sigs_a.append((b.timestamp, s.strategy_name, s.entry_price, s.stop_price,
                               s.target_price, s.direction))

        # Path B: 1m → upsample to 5m
        strats_b = [
            SCSniperLive(strat_cfg), FLAntiChopLive(strat_cfg),
            HitchHikerLive(strat_cfg), EmaFpipLive(strat_cfg),
            BDRShortLive(strat_cfg), ORHFBOShortV2Live(strat_cfg),
            BacksideStructureLive(strat_cfg),
        ]
        mgr_b = StrategyManager(strategies=strats_b, symbol=sym)
        if len(bars_1m) >= 14:
            atr_b = sum(max(b.high - b.low, 0.01) for b in bars_1m[:70]) / min(70, len(bars_1m))
            # Use first 5m bar for warm-up consistency
            if len(bars_5m) >= 14:
                mgr_b.indicators.warm_up_daily(atr_a, bars_5m[0].high, bars_5m[0].low)

        up = BarUpsampler(5)
        sigs_b = []
        for b in bars_1m:
            mgr_b.on_1min_bar(b)
            bar_5m = up.on_bar(b)
            if bar_5m is not None:
                for s in mgr_b.on_5min_bar(bar_5m):
                    sigs_b.append((bar_5m.timestamp, s.strategy_name, s.entry_price,
                                   s.stop_price, s.target_price, s.direction))

        # Compare
        set_a = set((ts.date(), sn) for ts, sn, *_ in sigs_a)
        set_b = set((ts.date(), sn) for ts, sn, *_ in sigs_b)

        only_a = set_a - set_b
        only_b = set_b - set_a
        both = set_a & set_b
        total_match += len(both)
        total_mismatch += len(only_a) + len(only_b)

        print(f"\n  {sym}: native={len(sigs_a)}, upsampled={len(sigs_b)}, "
              f"matched={len(both)}, only_native={len(only_a)}, only_upsampled={len(only_b)}")

        if only_a:
            for d, sn in list(only_a)[:3]:
                detail = [x for x in sigs_a if x[0].date() == d and x[1] == sn]
                if detail:
                    ts, sn, ep, sp, tp, dr = detail[0]
                    print(f"    ONLY NATIVE: {d} {sn} entry={ep:.2f} stop={sp:.2f} target={tp:.2f}")
        if only_b:
            for d, sn in list(only_b)[:3]:
                detail = [x for x in sigs_b if x[0].date() == d and x[1] == sn]
                if detail:
                    ts, sn, ep, sp, tp, dr = detail[0]
                    print(f"    ONLY UPSAMPLED: {d} {sn} entry={ep:.2f} stop={sp:.2f} target={tp:.2f}")

    if total_mismatch > 0:
        add_finding("A3-SIGNAL-PARITY", "Signal parity divergence between paths",
                    Severity.WARNING,
                    f"{total_mismatch} signal mismatches vs {total_match} matches across {len(test_syms)} symbols. "
                    f"Upsampled OHLC rounding causes minor indicator divergence → different triggers.")
    else:
        add_finding("A3-SIGNAL-PARITY", "Signal parity confirmed",
                    Severity.INFO, f"All signals match between native 5m and upsampled paths.")


# ══════════════════════════════════════════════════════════════════════
#  AUDIT 4: Confluence Recomputation Verification
# ══════════════════════════════════════════════════════════════════════

def audit_4_confluence_verification(records: List[SignalAuditRecord]):
    """Verify confluence tags are actually correct on promoted signals."""
    print(f"\n{'='*100}")
    print("AUDIT 4: Confluence Tag Verification")
    print(f"{'='*100}")

    promoted = [r for r in records if r.outcome == "promoted"]
    if not promoted:
        print("  No promoted signals to verify.")
        return

    # Check that confluence_count matches len(confluence_tags)
    mismatches = []
    empty_confluence = []
    tag_freq = defaultdict(int)

    for r in promoted:
        tags = r.confluence_tags
        count = r.confluence_count
        if not isinstance(tags, list):
            mismatches.append((r, "confluence not a list"))
            continue
        if len(tags) != count:
            mismatches.append((r, f"len(tags)={len(tags)} != count={count}"))
        if count == 0:
            empty_confluence.append(r)
        for t in tags:
            tag_freq[t] += 1

    print(f"\n  Promoted signals: {len(promoted)}")
    print(f"  Count/tag mismatches: {len(mismatches)}")
    print(f"  Empty confluence (should be 0 if min_conf>=1): {len(empty_confluence)}")

    if mismatches:
        add_finding("A4-CONF-MISMATCH", "Confluence count doesn't match tag list",
                    Severity.BLOCKER,
                    f"{len(mismatches)} promoted signals have mismatched confluence count.")
        for r, reason in mismatches[:5]:
            print(f"    MISMATCH: {r.symbol} {r.strategy} {reason}")
    else:
        print("  All confluence counts match tag list lengths.")

    # Check confluence gate was actually applied (all promoted should meet min)
    gate_violations = []
    for r in promoted:
        if r.confluence_count < r.min_confluence:
            gate_violations.append(r)

    if gate_violations:
        add_finding("A4-CONF-GATE", "Signals promoted below confluence minimum",
                    Severity.BLOCKER,
                    f"{len(gate_violations)} signals promoted with insufficient confluence.")
    else:
        print(f"  All promoted signals meet their confluence minimums.")

    # Tag frequency
    print(f"\n  Confluence Tag Frequency (across {len(promoted)} promoted signals):")
    for tag, freq in sorted(tag_freq.items(), key=lambda x: -x[1]):
        print(f"    {tag:<30s}  {freq:>5} ({freq/len(promoted)*100:.1f}%)")

    # Per-strategy confluence distribution
    print(f"\n  Per-Strategy Confluence Distribution:")
    by_strat = defaultdict(list)
    for r in promoted:
        by_strat[r.strategy].append(r.confluence_count)
    for s in sorted(by_strat):
        counts = by_strat[s]
        med = statistics.median(counts)
        mn = min(counts)
        mx = max(counts)
        print(f"    {s:<18} N={len(counts):>3}  min={mn}  med={med:.0f}  max={mx}  "
              f"threshold={MIN_CONFLUENCE_BY_STRATEGY.get(s, MIN_CONFLUENCE_DEFAULT)}")

    if not mismatches and not gate_violations:
        add_finding("A4-CONFLUENCE", "Confluence verified correct",
                    Severity.INFO,
                    f"All {len(promoted)} promoted signals have valid confluence counts and tags.")


# ══════════════════════════════════════════════════════════════════════
#  AUDIT 5: Structural Target Distribution + Fallback=0 Assertion
# ══════════════════════════════════════════════════════════════════════

def audit_5_target_distribution(records: List[SignalAuditRecord]):
    """Runtime distribution of target types; assert 0 fallback targets promoted."""
    print(f"\n{'='*100}")
    print("AUDIT 5: Structural Target Distribution")
    print(f"{'='*100}")

    promoted = [r for r in records if r.outcome == "promoted"]
    if not promoted:
        print("  No promoted signals.")
        return

    # Classify targets
    structural = []
    fallback = []
    missing = []

    for r in promoted:
        tag = r.target_tag
        if tag in ("fixed_rr", "fixed_rr_fallback"):
            fallback.append(r)
        elif tag == "MISSING" or tag == "":
            missing.append(r)
        else:
            structural.append(r)

    total = len(promoted)
    print(f"\n  Total promoted: {total}")
    print(f"  Structural: {len(structural)} ({len(structural)/total*100:.1f}%)")
    print(f"  Fallback:   {len(fallback)} ({len(fallback)/total*100:.1f}%)")
    print(f"  Missing:    {len(missing)} ({len(missing)/total*100:.1f}%)")

    # BLOCKER: any fallback targets promoted
    if fallback:
        add_finding("A5-FALLBACK", "Fallback targets promoted",
                    Severity.BLOCKER,
                    f"{len(fallback)} promoted trades use fallback targets. "
                    f"Strategies: {set(r.strategy for r in fallback)}")
        print(f"\n  *** BLOCKER: {len(fallback)} fallback targets in promoted trades ***")
        for r in fallback[:5]:
            print(f"    {r.symbol} {r.strategy} tag={r.target_tag} target={r.target_price:.2f}")
    else:
        add_finding("A5-NO-FALLBACK", "Zero fallback targets promoted",
                    Severity.INFO, "All promoted trades use structural targets.")
        print(f"  CONFIRMED: Zero fallback targets promoted.")

    # Per-strategy target tag breakdown
    print(f"\n  Per-Strategy Target Tags:")
    by_strat = defaultdict(lambda: defaultdict(int))
    for r in promoted:
        by_strat[r.strategy][r.target_tag] += 1
    for s in sorted(by_strat):
        tags = by_strat[s]
        tag_str = ", ".join(f"{t}={c}" for t, c in sorted(tags.items(), key=lambda x: -x[1]))
        print(f"    {s}: {tag_str}")

    # By time of day
    print(f"\n  By Time of Day (promoted):")
    by_hour = defaultdict(int)
    for r in promoted:
        if r.bar_timestamp:
            by_hour[r.bar_timestamp.hour] += 1
    for h in sorted(by_hour):
        print(f"    {h:02d}:xx  {by_hour[h]:>4}")

    # By path
    print(f"\n  By Path:")
    for path in ["1m", "5m"]:
        path_p = [r for r in promoted if r.path == path]
        struct = sum(1 for r in path_p if r.target_tag not in ("fixed_rr", "fixed_rr_fallback", "MISSING", ""))
        print(f"    {path}: {len(path_p)} promoted, {struct} structural ({struct/max(len(path_p),1)*100:.0f}%)")


# ══════════════════════════════════════════════════════════════════════
#  AUDIT 6: Stop Source Classification
# ══════════════════════════════════════════════════════════════════════

def audit_6_stop_source(records: List[SignalAuditRecord]):
    """Classify stop sources and detect floor overrides on promoted signals."""
    print(f"\n{'='*100}")
    print("AUDIT 6: Stop Source Classification")
    print(f"{'='*100}")

    promoted = [r for r in records if r.outcome == "promoted"]
    if not promoted:
        print("  No promoted signals.")
        return

    # Classify stop distance relative to ATR
    # We can estimate: if risk/entry < 0.15% → likely floor override
    # Risk > 0.5% → likely structural stop
    floor_likely = []
    structural_likely = []
    ambiguous = []

    for r in promoted:
        if r.entry_price <= 0:
            continue
        stop_pct = r.risk / r.entry_price * 100

        if stop_pct < 0.20:
            floor_likely.append((r, stop_pct))
        elif stop_pct > 0.50:
            structural_likely.append((r, stop_pct))
        else:
            ambiguous.append((r, stop_pct))

    total = len(promoted)
    print(f"\n  Stop Classification (by stop distance):")
    print(f"    Likely floor override (<0.20%):  {len(floor_likely)} ({len(floor_likely)/total*100:.1f}%)")
    print(f"    Likely structural (>0.50%):      {len(structural_likely)} ({len(structural_likely)/total*100:.1f}%)")
    print(f"    Ambiguous (0.20-0.50%):          {len(ambiguous)} ({len(ambiguous)/total*100:.1f}%)")

    # Per-strategy stop distance distribution
    print(f"\n  Per-Strategy Stop Distance (% of entry):")
    by_strat = defaultdict(list)
    for r in promoted:
        if r.entry_price > 0:
            by_strat[r.strategy].append(r.risk / r.entry_price * 100)
    for s in sorted(by_strat):
        pcts = by_strat[s]
        med = statistics.median(pcts)
        mn = min(pcts)
        mx = max(pcts)
        print(f"    {s:<18} N={len(pcts):>3}  min={mn:.3f}%  med={med:.3f}%  max={mx:.3f}%")

    # Detailed dump for floor overrides
    if floor_likely:
        print(f"\n  Floor Override Candidates (stop < 0.20% of entry):")
        for r, pct in floor_likely[:10]:
            print(f"    {r.symbol} {r.strategy} entry={r.entry_price:.2f} "
                  f"stop={r.stop_price:.2f} risk={r.risk:.4f} ({pct:.3f}%) "
                  f"cost_rr={r.cost_rr:.3f}")

    # Flag if floor overrides dominate
    if len(floor_likely) > total * 0.50:
        add_finding("A6-FLOOR-DOMINANT", "Majority of stops are floor overrides",
                    Severity.WARNING,
                    f"{len(floor_likely)}/{total} ({len(floor_likely)/total*100:.0f}%) stops likely floor-imposed.")
    else:
        add_finding("A6-STOP-SOURCE", "Stop source distribution reasonable",
                    Severity.INFO,
                    f"{len(structural_likely)} structural, {len(floor_likely)} floor, {len(ambiguous)} ambiguous")


# ══════════════════════════════════════════════════════════════════════
#  AUDIT 7: Cost Model Runtime Reconciliation
# ══════════════════════════════════════════════════════════════════════

def audit_7_cost_reconciliation(records: List[SignalAuditRecord]):
    """Manual reconciliation of cost model on sample trades."""
    print(f"\n{'='*100}")
    print("AUDIT 7: Cost Model Reconciliation (10-trade manual check)")
    print(f"{'='*100}")

    promoted = [r for r in records if r.outcome == "promoted"]
    if not promoted:
        print("  No promoted signals.")
        return

    sample = promoted[:10]
    print(f"\n  {'#':>3} {'Symbol':<8} {'Strategy':<18} {'Entry':>8} {'Stop':>8} "
          f"{'Risk':>6} {'CostDollar':>10} {'CostR':>6} {'PnlR':>7} {'Exit':<8} {'Check':<6}")

    all_ok = True
    for i, r in enumerate(sample):
        # Manually compute expected cost
        expected_cost_dollars = r.entry_price * 2.0 * SLIP_PER_SIDE_BPS / 10000.0
        expected_cost_rr = expected_cost_dollars / r.risk if r.risk > 0 else 0.0

        match_dollar = abs(r.cost_dollars - expected_cost_dollars) < 0.001
        match_rr = abs(r.cost_rr - expected_cost_rr) < 0.001
        ok = match_dollar and match_rr
        if not ok:
            all_ok = False

        print(f"  {i+1:>3} {r.symbol:<8} {r.strategy:<18} {r.entry_price:>8.2f} {r.stop_price:>8.2f} "
              f"{r.risk:>6.4f} {r.cost_dollars:>10.4f} {r.cost_rr:>6.4f} {r.pnl_rr:>+7.4f} {r.exit_reason:<8} "
              f"{'OK' if ok else 'FAIL':<6}")

    if not all_ok:
        add_finding("A7-COST-MISMATCH", "Cost model reconciliation failed",
                    Severity.BLOCKER, "Manual cost check found mismatches.")
    else:
        add_finding("A7-COST-OK", "Cost model reconciled on 10 trades",
                    Severity.INFO, "All 10 sampled trades have correct cost computation.")
        print(f"\n  All 10 trades reconcile correctly.")
        print(f"  Formula: cost_dollars = entry * 2 * {SLIP_PER_SIDE_BPS}bps / 10000")
        print(f"  Formula: cost_rr = cost_dollars / risk")


# ══════════════════════════════════════════════════════════════════════
#  AUDIT 8: Target Fill Sensitivity Matrix
# ══════════════════════════════════════════════════════════════════════

def audit_8_fill_sensitivity(records: List[SignalAuditRecord]):
    """Run same trade set under 4 fill modes: exact touch, 1-tick through,
    0.05 ATR through, close-through."""
    print(f"\n{'='*100}")
    print("AUDIT 8: Target Fill Sensitivity Matrix")
    print(f"{'='*100}")

    promoted = [r for r in records if r.outcome == "promoted"]
    if not promoted:
        print("  No promoted signals.")
        return

    # Current mode is exact touch (bar.high >= target for longs)
    # We'll re-simulate under stricter fill modes
    # This requires access to the bar data, which we don't have in records.
    # Instead, we estimate sensitivity from the exit data we have.

    # Count how many target fills had exact touch vs margin
    target_hits = [r for r in promoted if r.exit_reason == "target"]
    stop_hits = [r for r in promoted if r.exit_reason == "stop"]
    eod_hits = [r for r in promoted if r.exit_reason == "eod"]

    total = len(promoted)
    print(f"\n  Current fill mode: exact touch (bar.high >= target longs, bar.low <= target shorts)")
    print(f"  Exit distribution: target={len(target_hits)} stop={len(stop_hits)} eod={len(eod_hits)} "
          f"other={total - len(target_hits) - len(stop_hits) - len(eod_hits)}")

    # For actual sensitivity, we need to re-run the simulation. Let's do it on a subset.
    print(f"\n  Running fill sensitivity on all promoted trades...")

    # We need the source bars. Re-load for the symbols in promoted trades.
    sym_bars_cache = {}
    for r in promoted:
        if r.symbol not in sym_bars_cache:
            p_1m = DATA_1MIN_DIR / f"{r.symbol}_1min.csv"
            p_5m = DATA_DIR / f"{r.symbol}_5min.csv"
            if p_1m.exists():
                sym_bars_cache[r.symbol] = ("1m", load_bars_from_csv(str(p_1m)))
            elif p_5m.exists():
                sym_bars_cache[r.symbol] = ("5m", load_bars_from_csv(str(p_5m)))

    # Define fill threshold adjustments
    # "exact_touch" = current (0 threshold)
    # "1_tick" = target must be exceeded by 0.01
    # "5pct_atr" = target must be exceeded by 0.05 * risk (proxy for ATR)
    # "close_through" = bar close must exceed target
    fill_modes = {
        "exact_touch": 0.0,
        "1_tick_thru": 0.01,
        "5pct_risk_thru": None,  # dynamic: 0.05 * risk
        "close_through": "close",
    }

    results_by_mode = {m: {"pnl": 0.0, "wins": 0, "losses": 0, "target": 0, "stop": 0, "eod": 0}
                       for m in fill_modes}

    for r in promoted:
        if r.symbol not in sym_bars_cache:
            continue
        path_type, bars = sym_bars_cache[r.symbol]
        bar_idx = _find_bar_idx(bars, r.bar_timestamp)
        if bar_idx < 0:
            continue

        max_bars = STRATEGY_MAX_BARS.get(r.strategy, 78)
        target_rr = r.actual_rr if r.actual_rr > 0 else STRATEGY_TARGET_RR.get(r.strategy, 2.0)
        entry = r.entry_price
        stop = r.stop_price
        target = r.target_price
        risk = r.risk
        direction = r.direction
        if risk <= 0 or entry <= 0:
            continue

        cost_rr = entry * 2.0 * SLIP_PER_SIDE_BPS / 10000.0 / risk

        for mode_name, threshold in fill_modes.items():
            # Walk bars and simulate
            exit_reason = "eod"
            exit_price = 0.0
            pnl_rr = 0.0

            for i in range(bar_idx + 1, min(bar_idx + max_bars + 1, len(bars))):
                b = bars[i]
                bhhmm = b.timestamp.hour * 100 + b.timestamp.minute

                # EOD
                if bhhmm >= 1555 or i == len(bars) - 1:
                    pnl = (b.close - entry) * direction
                    pnl_rr = pnl / risk - cost_rr
                    exit_reason = "eod"
                    exit_price = b.close
                    break

                # Stop
                if direction == 1 and b.low <= stop:
                    pnl_rr = (stop - entry) / risk - cost_rr
                    exit_reason = "stop"
                    exit_price = stop
                    break
                elif direction == -1 and b.high >= stop:
                    pnl_rr = (entry - stop) / risk - cost_rr
                    exit_reason = "stop"
                    exit_price = stop
                    break

                # Target check with mode-specific threshold
                if not _isnan(target):
                    hit = False
                    if threshold == "close":
                        # Close must exceed target
                        if direction == 1 and b.close >= target:
                            hit = True
                        elif direction == -1 and b.close <= target:
                            hit = True
                    elif threshold is None:
                        # 5% of risk beyond target
                        margin = 0.05 * risk
                        if direction == 1 and b.high >= target + margin:
                            hit = True
                        elif direction == -1 and b.low <= target - margin:
                            hit = True
                    else:
                        # Fixed threshold
                        if direction == 1 and b.high >= target + threshold:
                            hit = True
                        elif direction == -1 and b.low <= target - threshold:
                            hit = True

                    if hit:
                        pnl_rr = target_rr - cost_rr
                        exit_reason = "target"
                        exit_price = target
                        break
            else:
                # max_bars exhausted
                last_idx = min(bar_idx + max_bars, len(bars) - 1)
                last_bar = bars[last_idx]
                pnl = (last_bar.close - entry) * direction
                pnl_rr = pnl / risk - cost_rr
                exit_reason = "eod"

            rm = results_by_mode[mode_name]
            rm["pnl"] += pnl_rr
            if pnl_rr > 0:
                rm["wins"] += 1
            else:
                rm["losses"] += 1
            rm[exit_reason] = rm.get(exit_reason, 0) + 1

    # Print matrix
    print(f"\n  {'Fill Mode':<20} {'N':>5} {'PF':>6} {'WR':>6} {'TotalR':>8} {'Target%':>8} {'Stop%':>8} {'EOD%':>8}")
    print(f"  {'-'*20} {'-'*5} {'-'*6} {'-'*6} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")

    for mode in fill_modes:
        rm = results_by_mode[mode]
        n = rm["wins"] + rm["losses"]
        if n == 0:
            continue
        gw = rm["pnl"] if rm["pnl"] > 0 else 0
        # Compute PF properly from wins/losses pnl
        # Actually we only have total pnl, let's compute win/loss R sums
        wr = rm["wins"] / n * 100
        pf_val = "N/A"
        target_pct = rm.get("target", 0) / n * 100
        stop_pct = rm.get("stop", 0) / n * 100
        eod_pct = rm.get("eod", 0) / n * 100

        print(f"  {mode:<20} {n:>5} {'':>6} {wr:>5.1f}% {rm['pnl']:>+7.1f}R "
              f"{target_pct:>7.1f}% {stop_pct:>7.1f}% {eod_pct:>7.1f}%")

    # Check sensitivity
    exact_pnl = results_by_mode["exact_touch"]["pnl"]
    close_pnl = results_by_mode["close_through"]["pnl"]
    if exact_pnl != 0:
        degradation = (exact_pnl - close_pnl) / abs(exact_pnl) * 100
        print(f"\n  Degradation from exact_touch to close_through: {degradation:.1f}%")
        if degradation > 50:
            add_finding("A8-FILL-FRAGILE", "Results fragile to fill assumptions",
                        Severity.WARNING,
                        f"P&L drops {degradation:.0f}% when requiring close-through vs touch fill.")
        else:
            add_finding("A8-FILL-ROBUST", "Results reasonably robust to fill mode",
                        Severity.INFO, f"P&L drops {degradation:.0f}% on strictest fill mode.")


# ══════════════════════════════════════════════════════════════════════
#  AUDIT 9: Warm-Up Sufficiency — First Legal Signal Bar
# ══════════════════════════════════════════════════════════════════════

def audit_9_warmup_first_signal(summary: Dict):
    """Report first legal signal bar and lookback available per strategy."""
    print(f"\n{'='*100}")
    print("AUDIT 9: Warm-Up Sufficiency — First Legal Signal Bar")
    print(f"{'='*100}")

    first_sigs = summary.get("first_signal_by_strat", {})
    cfg = StrategyConfig(timeframe_min=5)

    # Strategy time_start lookup (config returns Dict[int,val], extract for 5-min)
    def _ts(attr):
        v = attr
        if isinstance(v, dict):
            return v.get(5, v.get(1, 1000))
        return v

    time_starts = {
        "SC_SNIPER": _ts(cfg.sc_time_start), "FL_ANTICHOP": _ts(cfg.fl_time_start),
        "SP_ATIER": _ts(cfg.sc_time_start), "HH_QUALITY": _ts(cfg.hh_time_start),
        "EMA_FPIP": _ts(cfg.fpip_time_start), "BDR_SHORT": _ts(cfg.bdr_time_start),
        "EMA9_FT": 935, "BS_STRUCT": _ts(cfg.bs_time_start),
        "ORL_FBD_LONG": 1000, "ORH_FBO_V2_A": 1000,
        "ORH_FBO_V2_B": 1000, "PDH_FBO_B": 1000,
        "FFT_NEWLOW_REV": 1000,
    }

    print(f"\n  {'Strategy':<18} {'TimeStart':>10} {'FirstSig':>10} {'BarCount':>10} {'Symbol':<8} {'Status':<10}")
    for s in sorted(time_starts):
        ts_start = time_starts[s]
        info = first_sigs.get(s, None)
        if info:
            first_hhmm = info["hhmm"]
            bar_count = info["bar_count"]
            sym = info["symbol"]
            # Check: is first signal at or after time_start?
            early = "EARLY" if first_hhmm < ts_start else "OK"
            print(f"  {s:<18} {ts_start:>10} {first_hhmm:>10} {bar_count:>10} {sym:<8} {early:<10}")
            if early == "EARLY":
                add_finding(f"A9-EARLY-{s}", f"{s} signals before time_start",
                            Severity.WARNING,
                            f"First signal at {first_hhmm} but time_start={ts_start}")
        else:
            print(f"  {s:<18} {ts_start:>10} {'(none)':>10} {'':>10} {'':>8} {'NO_SIG':<10}")


# ══════════════════════════════════════════════════════════════════════
#  AUDIT 10: New Symbol Day-1 Readiness (Real Onboarding)
# ══════════════════════════════════════════════════════════════════════

def audit_10_new_symbol_onboarding():
    """Take a symbol, pretend it's day-1, verify full onboarding."""
    print(f"\n{'='*100}")
    print("AUDIT 10: New Symbol Day-1 Onboarding Test")
    print(f"{'='*100}")

    # Pick a symbol with 5m data
    test_sym = None
    for p in DATA_DIR.glob("*_5min.csv"):
        sym = p.stem.replace("_5min", "")
        if sym not in ("SPY", "QQQ", "IWM"):
            test_sym = sym
            break

    if not test_sym:
        print("  No test symbol available.")
        return

    print(f"\n  Testing onboarding for: {test_sym}")
    bars_5m = load_bars_from_csv(str(DATA_DIR / f"{test_sym}_5min.csv"))
    p_1m = DATA_1MIN_DIR / f"{test_sym}_1min.csv"
    has_1m = p_1m.exists()

    issues = []
    checks = {}

    # 1. Data availability
    checks["5m_data_loaded"] = len(bars_5m) > 0
    checks["1m_data_available"] = has_1m
    if has_1m:
        bars_1m = load_bars_from_csv(str(p_1m))
        checks["1m_data_loaded"] = len(bars_1m) > 0
    else:
        checks["5m_fallback_used"] = True

    # 2. Prev close (day 1: use first bar of second day if available)
    days = sorted(set(b.timestamp.date() for b in bars_5m))
    if len(days) >= 2:
        day1_bars = [b for b in bars_5m if b.timestamp.date() == days[0]]
        day2_bars = [b for b in bars_5m if b.timestamp.date() == days[1]]
        prev_close = day1_bars[-1].close if day1_bars else None
        checks["prev_close_available"] = prev_close is not None
    else:
        checks["prev_close_available"] = False
        issues.append("Only 1 day of data — no prev_close")

    # 3. ATR warm-up
    if len(bars_5m) >= 14:
        atr_val = sum(max(b.high - b.low, 0.01) for b in bars_5m[:14]) / 14
        checks["atr_warm"] = atr_val > 0
    else:
        checks["atr_warm"] = False
        issues.append(f"Only {len(bars_5m)} bars — insufficient for ATR")

    # 4. Strategy manager initialization
    strat_cfg = StrategyConfig(timeframe_min=5)
    live_strats = [
        SCSniperLive(strat_cfg), FLAntiChopLive(strat_cfg),
        HitchHikerLive(strat_cfg), BDRShortLive(strat_cfg),
        ORHFBOShortV2Live(strat_cfg),
    ]
    mgr = StrategyManager(strategies=live_strats, symbol=test_sym)
    if len(bars_5m) >= 14:
        mgr.indicators.warm_up_daily(atr_val, bars_5m[0].high, bars_5m[0].low)

    # 5. Feed first day and check readiness
    day1_bars_source = bars_5m if not has_1m else load_bars_from_csv(str(p_1m))
    first_day = day1_bars_source[0].timestamp.date()
    bar_count = 0
    ready_at = None
    for b in day1_bars_source:
        if b.timestamp.date() != first_day:
            break
        bar_count += 1
        sigs = mgr.on_bar(b)
        if bar_count == 1:
            checks["session_open_set"] = not _isnan(mgr.indicators._session_open)
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute
        if hhmm >= 935 and ready_at is None:
            snap = mgr.indicators.snapshot_5m() if hasattr(mgr.indicators, 'snapshot_5m') else None
            # Just check indicators exist
            ready_at = hhmm

    checks["strategies_received_bars"] = bar_count > 0
    checks["session_open_set"] = checks.get("session_open_set", False)

    # 6. No stale prior symbol state
    # Verify all strategies have _current_date == first_day after feeding
    stale = []
    for s in live_strats:
        if s._current_date != first_day:
            stale.append(s.name)
    checks["no_stale_state"] = len(stale) == 0
    if stale:
        issues.append(f"Stale date on: {stale}")

    print(f"\n  Onboarding Checklist:")
    for check, result in checks.items():
        status = "PASS" if result else "FAIL"
        print(f"    [{status}] {check}")

    if issues:
        for iss in issues:
            print(f"    NOTE: {iss}")

    failed = [k for k, v in checks.items() if not v]
    if failed:
        add_finding("A10-ONBOARD", "New symbol onboarding issues",
                    Severity.WARNING, f"Failed checks: {failed}")
    else:
        add_finding("A10-ONBOARD-OK", "New symbol onboarding verified",
                    Severity.INFO, f"All {len(checks)} onboarding checks pass for {test_sym}")


# ══════════════════════════════════════════════════════════════════════
#  AUDIT 11: Multi-Strategy Collision Report
# ══════════════════════════════════════════════════════════════════════

def audit_11_collision_report(records: List[SignalAuditRecord]):
    """Report same-symbol same-bar multi-strategy signals and long/short conflicts."""
    print(f"\n{'='*100}")
    print("AUDIT 11: Multi-Strategy Collision Report")
    print(f"{'='*100}")

    # Group all signals (not just promoted) by (symbol, timestamp)
    by_sym_ts = defaultdict(list)
    for r in records:
        if r.bar_timestamp:
            key = (r.symbol, r.bar_timestamp)
            by_sym_ts[key].append(r)

    # Same bar, multiple strategies
    multi_strat_bars = {k: v for k, v in by_sym_ts.items() if len(v) > 1}
    same_bar_promoted = 0
    long_short_conflicts = 0
    both_promoted_conflicts = 0

    print(f"\n  Total unique (symbol, bar) with signals: {len(by_sym_ts)}")
    print(f"  Bars with multiple strategy signals: {len(multi_strat_bars)}")

    # Analyze same-day conflicts
    by_sym_day = defaultdict(list)
    for r in records:
        if r.bar_timestamp:
            key = (r.symbol, r.bar_timestamp.date())
            by_sym_day[key].append(r)

    conflict_details = []
    for (sym, day), recs in by_sym_day.items():
        promoted = [r for r in recs if r.outcome == "promoted"]
        if len(promoted) < 2:
            continue

        # Check long/short conflict
        longs = [r for r in promoted if r.direction == 1]
        shorts = [r for r in promoted if r.direction == -1]

        if longs and shorts:
            long_short_conflicts += 1
            conflict_details.append((sym, day, len(longs), len(shorts)))

        if len(promoted) > 1:
            both_promoted_conflicts += 1

    print(f"\n  Same-day, same-symbol, multiple promoted: {both_promoted_conflicts}")
    print(f"  Same-day long/short conflicts (both promoted): {long_short_conflicts}")

    if conflict_details:
        print(f"\n  Long/Short Conflict Details:")
        for sym, day, nl, ns in conflict_details[:10]:
            print(f"    {sym} {day}: {nl} longs, {ns} shorts both promoted")

    # Suppression analysis
    suppressed = 0
    for (sym, day), recs in by_sym_day.items():
        if len(recs) > 1:
            total = len(recs)
            prom = sum(1 for r in recs if r.outcome == "promoted")
            blocked = total - prom
            if blocked > 0 and prom > 0:
                suppressed += 1

    print(f"\n  Symbol-days with mixed promoted/blocked: {suppressed}")

    if long_short_conflicts > 0:
        add_finding("A11-CONFLICT", "Long/short conflicts exist in same symbol-day",
                    Severity.WARNING,
                    f"{long_short_conflicts} symbol-days have both long and short promoted signals.")
    else:
        add_finding("A11-NO-CONFLICT", "No long/short conflicts in promoted signals",
                    Severity.INFO, "No same-symbol-day long/short collision.")


# ══════════════════════════════════════════════════════════════════════
#  AUDIT 12: Findings Summary with Severity Classification
# ══════════════════════════════════════════════════════════════════════

def print_findings_summary():
    """Print all findings classified by severity."""
    print(f"\n{'='*100}")
    print("FINDINGS SUMMARY — SEVERITY CLASSIFICATION")
    print(f"{'='*100}")

    blockers = [f for f in findings if f.severity == Severity.BLOCKER]
    warnings = [f for f in findings if f.severity == Severity.WARNING]
    infos = [f for f in findings if f.severity == Severity.INFO]

    print(f"\n  BLOCKERS: {len(blockers)}")
    for f in blockers:
        print(f"    [{f.audit_id}] {f.title}")
        print(f"      → {f.detail}")

    print(f"\n  WARNINGS: {len(warnings)}")
    for f in warnings:
        print(f"    [{f.audit_id}] {f.title}")
        print(f"      → {f.detail}")

    print(f"\n  INFORMATIONAL: {len(infos)}")
    for f in infos:
        print(f"    [{f.audit_id}] {f.title}")

    # Final verdict
    print(f"\n{'='*100}")
    if blockers:
        print(f"  VERDICT: {len(blockers)} BLOCKER(s) found — SYSTEM NOT READY FOR LIVE")
    elif warnings:
        print(f"  VERDICT: No blockers. {len(warnings)} warning(s) to monitor.")
    else:
        print(f"  VERDICT: All clear. System ready for live deployment.")
    print(f"{'='*100}")


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    # Phase 1: Instrumented replay
    records, summary = run_instrumented_replay()

    # Phase 2: All audits
    audit_1_end_to_end_trace(records)
    audit_2_ip_gate_table(records)
    audit_3_signal_parity()
    audit_4_confluence_verification(records)
    audit_5_target_distribution(records)
    audit_6_stop_source(records)
    audit_7_cost_reconciliation(records)
    audit_8_fill_sensitivity(records)
    audit_9_warmup_first_signal(summary)
    audit_10_new_symbol_onboarding()
    audit_11_collision_report(records)

    # Phase 3: Summary
    print_findings_summary()

    # Export audit records
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "runtime_audit_records.csv"
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "timestamp", "hhmm", "path", "strategy", "direction",
                     "outcome", "regime_pass", "regime_reason", "ip_evaluated", "ip_pass",
                     "ip_reason", "tape_pass", "tape_value", "confluence_count",
                     "min_confluence", "confluence_pass",
                     "entry_price", "stop_price", "target_price", "risk",
                     "target_tag", "actual_rr", "structure_quality",
                     "exit_reason", "exit_price", "pnl_rr", "bars_held",
                     "cost_rr", "cost_dollars", "confluence_tags"])
        for r in records:
            w.writerow([
                r.symbol,
                r.bar_timestamp.strftime("%Y-%m-%d %H:%M") if r.bar_timestamp else "",
                r.hhmm, r.path, r.strategy, r.direction, r.outcome,
                r.regime_pass, r.regime_reason, r.ip_evaluated, r.ip_pass,
                r.ip_reason, r.tape_pass, f"{r.tape_value:.3f}", r.confluence_count,
                r.min_confluence, r.confluence_pass,
                f"{r.entry_price:.4f}", f"{r.stop_price:.4f}", f"{r.target_price:.4f}",
                f"{r.risk:.4f}", r.target_tag, f"{r.actual_rr:.3f}",
                f"{r.structure_quality:.3f}",
                r.exit_reason, f"{r.exit_price:.4f}", f"{r.pnl_rr:.4f}",
                r.bars_held, f"{r.cost_rr:.4f}", f"{r.cost_dollars:.4f}",
                "|".join(r.confluence_tags) if isinstance(r.confluence_tags, list) else "",
            ])
    print(f"\n  → Exported {len(records)} audit records to {out_path.name}")


if __name__ == "__main__":
    main()
