"""
AUTHORITATIVE Strategy Replay — feeds historical bars through the LIVE pipeline
(StrategyManager + shared gate modules) and computes trade metrics.

This is THE replay harness. It uses the same execution path as the live dashboard:
  - shared/regime_gate.py (bar-level, time-normalized, no look-ahead)
  - shared/in_play_proxy.py (correct data window per timeframe)
  - shared/quality_scoring.py (real-time market context inputs)
  - live strategy classes via StrategyManager

Gate chain (matches dashboard.py exactly):
  Gate 0:   Regime gate (GREEN / RED / FAILED_LONG / FAILED_SHORT)
  Gate 0.5: In-play gate (blanket suppression via evaluate_session)
  Gate 1:   Quality scoring + A-tier gate

NOTE: replay_legacy.py (the old replay.py) uses EnhancedMarketRegime with
      end-of-day look-ahead bias and is NOT authoritative for benchmarking.

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.strategies.replay_live_path
    python -m alert_overlay.strategies.replay_live_path --ungated
"""

import csv
import math
import sys

_isnan = math.isnan
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..backtest import load_bars_from_csv
from ..models import Bar, NaN
from ..market_context import MarketEngine, MarketSnapshot, compute_market_context
from ..layered_regime import PermissionWeights, compute_permission

from .shared.signal_schema import StrategySignal, StrategyTrade, QualityTier
from .shared.config import StrategyConfig
from .shared.in_play_proxy import InPlayProxy, SessionSnapshot, InPlayResult, DayOpenStats
from .shared.in_play_proxy_v2 import InPlayProxyV2, InPlayResultV2
from .shared.market_regime import EnhancedMarketRegime
from .shared.helpers import simulate_strategy_trade, trigger_bar_quality
from .shared.regime_gate import (
    STRATEGY_REGIME_GATE,
    check_regime_gate as _shared_check_regime_gate,
    hostile_threshold,
)

from .live.manager import StrategyManager
from .live.shared_indicators import SharedIndicators
from .live.base import RawSignal
from .live.sc_sniper_live import SCSniperLive
from .live.fl_antichop_live import FLAntiChopLive
from .live.spencer_atier_live import SpencerATierLive
from .live.hitchhiker_live import HitchHikerLive
from .live.ema_fpip_live import EmaFpipLive
from .live.bdr_short_live import BDRShortLive
from .live.ema9_ft_live import EMA9FirstTouchLive
from .live.backside_live import BacksideStructureLive
from .live.orl_fbd_long_live import ORLFBDLongLive
from .live.orh_fbo_short_v2_live import ORHFBOShortV2Live
from .live.pdh_fbo_short_live import PDHFBOShortLive
from .live.fft_newlow_reversal_live import FFTNewlowReversalLive


DATA_DIR = Path(__file__).parent.parent / "data"
DATA_1MIN_DIR = DATA_DIR / "1min"
OUT_DIR = Path(__file__).parent.parent / "outputs"

# ── Regime gate imported from shared module ──────────────────────────
# STRATEGY_REGIME_GATE imported from .shared.regime_gate

# Target RR per strategy (matches config defaults and replay.py)
STRATEGY_TARGET_RR = {
    "SC_SNIPER": 2.0, "FL_ANTICHOP": 1.5, "FL_REBUILD_STRUCT_Q7": 3.0, "FL_REBUILD_R10_Q6": 1.0, "SP_ATIER": 3.0, "SP_V2_BAL": 1.9, "SP_V2_SIMPLE": 1.9, "SP_V2_HQ": 1.9,
    "HH_QUALITY": 1.5, "EMA_FPIP": 2.0,
    "EMA_FPIP_V3_A": 1.5, "EMA_FPIP_V3_B": 2.0, "EMA_FPIP_V3_C": 1.5,
    "BDR_SHORT": 2.0,
    "BDR_V3_A": 1.5, "BDR_V3_B": 1.5, "BDR_V3_C": 2.0, "BDR_V3_D": 1.5,
    "BDR_V4_A": 1.5, "BDR_V4_B": 1.5, "BDR_V4_C": 1.5, "BDR_V4_D": 1.5,
    "EMA9_FT": 2.0,
    "EMA9_V4_A": 1.25, "EMA9_V4_B": 2.0, "EMA9_V4_C": 1.25, "EMA9_V4_D": 1.25,
    "EMA9_V5_A": 2.0, "EMA9_V5_B": 2.0, "EMA9_V5_C": 2.0, "EMA9_V5_D": 2.0,
    "BS_STRUCT": 2.0, "GGG_LONG_V1": 1.5, "ORL_FBD_LONG": 2.0,
    "ORH_FBO_V2_A": 1.5, "ORH_FBO_V2_B": 1.5,
    "PDH_FBO_B": 1.5, "FFT_NEWLOW_REV": 2.0,
}

# Max bars per strategy (5-min timeframe for 5m strats, 1-min for 1m strats)
STRATEGY_MAX_BARS = {
    "SC_SNIPER": 78, "FL_ANTICHOP": 52, "FL_REBUILD_STRUCT_Q7": 52, "FL_REBUILD_R10_Q6": 52, "SP_ATIER": 78, "SP_V2_BAL": 78, "SP_V2_SIMPLE": 78, "SP_V2_HQ": 78,
    "HH_QUALITY": 20, "EMA_FPIP": 24,
    "EMA_FPIP_V3_A": 24, "EMA_FPIP_V3_B": 24, "EMA_FPIP_V3_C": 24,
    "BDR_SHORT": 8,
    "BDR_V3_A": 8, "BDR_V3_B": 8, "BDR_V3_C": 16, "BDR_V3_D": 8,
    "BDR_V4_A": 8, "BDR_V4_B": 8, "BDR_V4_C": 8, "BDR_V4_D": 8,
    "EMA9_FT": 120,
    "EMA9_V4_A": 120, "EMA9_V4_B": 120, "EMA9_V4_C": 120, "EMA9_V4_D": 120,
    "EMA9_V5_A": 120, "EMA9_V5_B": 120, "EMA9_V5_C": 120, "EMA9_V5_D": 120,
    "BS_STRUCT": 30, "GGG_LONG_V1": 60, "ORL_FBD_LONG": 24,
    "ORH_FBO_V2_A": 60, "ORH_FBO_V2_B": 60,
    "PDH_FBO_B": 60, "FFT_NEWLOW_REV": 60,
}

# Transaction cost: 6 bps per side (slippage + commission), 12 bps round-trip.
SLIP_PER_SIDE_BPS = 6.0

TAPE_WEIGHTS = PermissionWeights()
LONG_TAPE_THRESHOLD = 0.40

# Confluence quality gate — per-strategy minimum confluence count.
# Only used for strategies NOT in the quality-scoring path (newer strategies).
MIN_CONFLUENCE_DEFAULT = 3
MIN_CONFLUENCE_BY_STRATEGY = {
    "ORH_FBO_V2_A": 4,   # PF 1.11→1.25, +15.7R
    "ORH_FBO_V2_B": 4,   # PF 1.35→1.42, +5.0R
    "PDH_FBO_B": 5,       # PF 1.01→1.26, +4.5R
}

# ── Quality scoring pipeline (unified — ALL strategies) ──
# All 13 strategies use the full rejection + quality scoring + A-tier pipeline.
# No more Family A/B split. One pipeline, one gate chain.
QUALITY_SCORED_STRATEGIES = {
    "SC_SNIPER", "SP_ATIER", "SP_V2_BAL", "SP_V2_SIMPLE", "SP_V2_HQ", "HH_QUALITY",
    # FL_ANTICHOP demoted 2026-03-17
    "EMA_FPIP", "EMA_FPIP_V3_A", "EMA_FPIP_V3_B", "EMA_FPIP_V3_C",
    "BDR_SHORT", "BDR_V3_A", "BDR_V3_B", "BDR_V3_C", "BDR_V3_D",
    "BDR_V4_A", "BDR_V4_B", "BDR_V4_C", "BDR_V4_D",
    "EMA9_FT", "EMA9_V4_A", "EMA9_V4_B", "EMA9_V4_C", "EMA9_V4_D",
    "EMA9_V5_A", "EMA9_V5_B", "EMA9_V5_C", "EMA9_V5_D",
    "BS_STRUCT", "GGG_LONG_V1", "ORL_FBD_LONG",
    "ORH_FBO_V2_A", "ORH_FBO_V2_B", "PDH_FBO_B", "FFT_NEWLOW_REV",
    "FL_REBUILD_STRUCT_Q7", "FL_REBUILD_R10_Q6",
}

from .shared.quality_scoring import QualityScorer
from .shared.signal_schema import QualityTier
from .shared.helpers import trigger_bar_quality


# ════════════════════════════════════════════════════════════════
#  Metrics (reuse from replay.py)
# ════════════════════════════════════════════════════════════════

def _pf(trades):
    if not trades:
        return 0.0
    gw = sum(t.pnl_rr for t in trades if t.pnl_rr > 0)
    gl = abs(sum(t.pnl_rr for t in trades if t.pnl_rr <= 0))
    return gw / gl if gl > 0 else float("inf")


def pf_str(v):
    return f"{v:.2f}" if v < 999 else "inf"


def compute_metrics(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0, "pf": 0, "exp": 0, "total_r": 0, "max_dd_r": 0,
                "avg_win": 0, "avg_loss": 0,
                "train_pf": 0, "test_pf": 0, "train_n": 0, "test_n": 0,
                "wf_pf": 0, "wf_n": 0,
                "ex_best_day_pf": 0, "ex_top_sym_pf": 0,
                "stop_rate": 0, "target_rate": 0, "time_rate": 0, "avg_bars_held": 0,
                "days_active": 0, "avg_per_day": 0, "pct_pos_days": 0}

    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in trades)
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")

    avg_win = gw / len(wins) if wins else 0
    avg_loss = gl / len(losses) if losses else 0

    cum = peak = max_dd = 0.0
    for t in sorted(trades, key=lambda x: (x.entry_date, x.signal.timestamp)):
        cum += t.pnl_rr
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    # Train/test (60/40 by date)
    all_dates = sorted(set(t.entry_date for t in trades))
    split = int(len(all_dates) * 0.60)
    train_dates = set(all_dates[:split])
    test_dates = set(all_dates[split:])
    train = [t for t in trades if t.entry_date in train_dates]
    test = [t for t in trades if t.entry_date in test_dates]

    # Walk-forward (67/33 by date)
    wf_split = int(len(all_dates) * 0.67)
    wf_test_dates = set(all_dates[wf_split:])
    wf_test = [t for t in trades if t.entry_date in wf_test_dates]

    # Ex-best-day, ex-top-symbol
    day_r = defaultdict(float)
    for t in trades:
        day_r[t.entry_date] += t.pnl_rr
    best_day = max(day_r, key=day_r.get) if day_r else None
    ex_best = [t for t in trades if t.entry_date != best_day] if best_day else trades

    sym_r = defaultdict(float)
    for t in trades:
        sym_r[t.symbol] += t.pnl_rr
    top_sym = max(sym_r, key=sym_r.get) if sym_r else None
    ex_top = [t for t in trades if t.symbol != top_sym] if top_sym else trades

    # Exit reasons
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    target = sum(1 for t in trades if t.exit_reason == "target")
    timed = sum(1 for t in trades if t.exit_reason in ("eod", "trail", "time"))

    # Activity
    days_active = len(set(t.entry_date for t in trades))
    avg_per_day = n / days_active if days_active else 0
    day_pnl = list(day_r.values())
    pos_days = sum(1 for d in day_pnl if d > 0)
    pct_pos = pos_days / len(day_pnl) * 100 if day_pnl else 0
    avg_bars = sum(t.bars_held for t in trades) / n

    return {
        "n": n, "wr": len(wins) / n * 100, "pf": pf, "exp": total_r / n,
        "total_r": total_r, "max_dd_r": max_dd,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "train_pf": _pf(train), "test_pf": _pf(test),
        "train_n": len(train), "test_n": len(test),
        "wf_pf": _pf(wf_test), "wf_n": len(wf_test),
        "ex_best_day_pf": _pf(ex_best), "ex_top_sym_pf": _pf(ex_top),
        "stop_rate": stopped / n * 100, "target_rate": target / n * 100,
        "time_rate": timed / n * 100, "avg_bars_held": avg_bars,
        "days_active": days_active, "avg_per_day": avg_per_day,
        "pct_pos_days": pct_pos,
    }


# ════════════════════════════════════════════════════════════════
#  Gate logic (matches dashboard.py exactly)
# ════════════════════════════════════════════════════════════════

def check_regime_gate(strategy_name, spy_snap, bar_time_hhmm=None):
    """Delegate to shared regime gate with time-normalized thresholds."""
    return _shared_check_regime_gate(strategy_name, spy_snap, bar_time_hhmm)


def raw_signal_to_strategy_signal(sig: RawSignal, symbol: str, bar_ts: datetime,
                                   in_play_score=0.0, regime_label="") -> StrategySignal:
    """Convert live RawSignal to replay StrategySignal for trade simulation."""
    return StrategySignal(
        strategy_name=sig.strategy_name,
        symbol=symbol,
        timestamp=bar_ts,
        direction=sig.direction,
        entry_price=sig.entry_price,
        stop_price=sig.stop_price,
        target_price=sig.target_price,
        quality_tier=QualityTier.A_TIER,  # live signals are pre-filtered
        quality_score=sig.quality,
        in_play_score=in_play_score,
        market_regime=regime_label,
        metadata=sig.metadata,
    )


# ════════════════════════════════════════════════════════════════
#  BarUpsampler (copied from dashboard.py)
# ════════════════════════════════════════════════════════════════

class BarUpsampler:
    """Aggregates completed 1-min bars into 5-min bars."""
    def __init__(self, target_minutes=5):
        self.interval = target_minutes
        self._buf = []
        self._boundary = None

    def _bar_boundary(self, ts):
        minute = (ts.minute // self.interval) * self.interval
        return ts.replace(minute=minute, second=0, microsecond=0)

    def on_bar(self, bar):
        boundary = self._bar_boundary(bar.timestamp)
        if self._boundary is None:
            self._boundary = boundary
            self._buf = [bar]
            return None
        if boundary == self._boundary:
            self._buf.append(bar)
            return None
        # New bucket → emit completed bar from buffer
        completed = self._emit()
        self._boundary = boundary
        self._buf = [bar]
        return completed

    def _emit(self):
        if not self._buf:
            return None
        return Bar(
            timestamp=self._buf[0].timestamp,
            open=self._buf[0].open,
            high=max(b.high for b in self._buf),
            low=min(b.low for b in self._buf),
            close=self._buf[-1].close,
            volume=sum(b.volume for b in self._buf),
        )

    def flush(self):
        return self._emit()


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    ungated = "--ungated" in sys.argv
    mode_label = "UNGATED (no gates)" if ungated else "GATED (regime + in-play + tape)"

    print("=" * 100)
    print(f"LIVE-PATH REPLAY — Portfolio D [{mode_label}]")
    print("=" * 100)

    # ── Load data ──
    print("\n  Loading data...")
    spy_bars_5m = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))

    # Build 1-min bar index from 5-min data: for trade simulation on 5-min strats
    # For 1-min strats, try to load actual 1-min data

    # Identify tradeable symbols (exclude indices and sector ETFs)
    from ..market_context import SECTOR_MAP, get_sector_etf
    sector_etfs = sorted(set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    excluded = {"SPY", "QQQ", "IWM"} | set(sector_etfs)

    symbols_5m = sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])

    spy_dates = sorted(set(b.timestamp.date() for b in spy_bars_5m))
    print(f"  Universe: {len(symbols_5m)} symbols, "
          f"{spy_dates[0]} → {spy_dates[-1]} ({len(spy_dates)} days)")

    # ── Initialize SPY market engine for regime gate + tape permission ──
    print("  Building SPY/QQQ market context...")
    spy_engine = MarketEngine()
    qqq_engine = MarketEngine()
    qqq_bars_5m = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    # Pre-index SPY/QQQ snapshots by timestamp for fast lookup
    spy_snap_by_ts = {}
    for b in spy_bars_5m:
        snap = spy_engine.process_bar(b)
        spy_snap_by_ts[b.timestamp] = snap

    qqq_snap_by_ts = {}
    for b in qqq_bars_5m:
        snap = qqq_engine.process_bar(b)
        qqq_snap_by_ts[b.timestamp] = snap

    # Also build SPY snapshots at 1-min granularity if data exists
    spy_1m_path = DATA_1MIN_DIR / "SPY_1min.csv"
    spy_1m_snaps = {}
    if spy_1m_path.exists():
        spy_engine_1m = MarketEngine()
        spy_bars_1m = load_bars_from_csv(str(spy_1m_path))
        for b in spy_bars_1m:
            snap = spy_engine_1m.process_bar(b)
            spy_1m_snaps[b.timestamp] = snap

    # ── Initialize in-play proxies ──
    ip_cfg = StrategyConfig(timeframe_min=1)
    ip_cfg_5m = StrategyConfig(timeframe_min=5)
    in_play_proxy = InPlayProxy(ip_cfg)       # V1: for 1-min path (first_n=15)
    in_play_proxy_5m = InPlayProxy(ip_cfg_5m) # V1: for 5-min-only path (first_n=3)

    # V2 in-play gate (percentile-ranked, two-stage, rolling)
    _USE_IP_V2 = ip_cfg.ip_v2_enabled
    in_play_v2 = InPlayProxyV2(ip_cfg) if _USE_IP_V2 else None

    # ── Quality scorer (matches original replay.py scan_day pipeline) ──
    quality_cfg = StrategyConfig()
    quality_scorer = QualityScorer(quality_cfg)

    # ── Funnel counters ──
    funnel = {
        "raw_signals": 0,
        "blocked_regime": 0,
        "blocked_inplay": 0,
        "blocked_inplay_score": 0,
        "blocked_tape": 0,
        "blocked_confluence": 0,
        "blocked_quality": 0,
        "ip_pending": 0,
        "promoted": 0,
    }
    per_strategy_funnel = defaultdict(lambda: {
        "raw": 0, "blocked_regime": 0, "blocked_inplay": 0,
        "blocked_tape": 0, "blocked_confluence": 0,
        "blocked_quality": 0, "promoted": 0,
    })

    # ── Process each symbol ──
    all_trades: List[StrategyTrade] = []
    trades_by_strat: Dict[str, List[StrategyTrade]] = defaultdict(list)
    ip_eval_log = []  # (symbol, date, passed, score, data_status, reason)
    target_tag_audit: Dict[str, list] = defaultdict(list)  # strategy -> list of (tag, rr, risk, entry)

    # ── V2 in-play pre-pass: compute cross-sectional features ──
    if _USE_IP_V2:
        print(f"\n  V2 in-play pre-pass: loading bars for {len(symbols_5m)} symbols...")
        # Load SPY bars
        _spy_path = DATA_DIR / "SPY_5min.csv"
        _spy_all_bars = load_bars_from_csv(str(_spy_path)) if _spy_path.exists() else []
        _spy_by_date = defaultdict(list)
        for b in _spy_all_bars:
            _spy_by_date[b.timestamp.date()].append(b)

        # Load all symbol bars and group by date
        _all_bars_by_date = defaultdict(dict)  # {date: {symbol: [bars]}}
        _prior_closes = {}  # {symbol: float} running
        for sym in symbols_5m:
            p = DATA_DIR / f"{sym}_5min.csv"
            if not p.exists():
                continue
            sym_bars = load_bars_from_csv(str(p))
            prev_close = NaN
            by_date = defaultdict(list)
            for b in sym_bars:
                by_date[b.timestamp.date()].append(b)
            for d in sorted(by_date.keys()):
                _all_bars_by_date[d][sym] = by_date[d]
                if not _isnan(prev_close):
                    if d not in _prior_closes:
                        _prior_closes_day = {}
                    else:
                        _prior_closes_day = _prior_closes.get(d, {})
                    _prior_closes_day[sym] = prev_close
                    _prior_closes[d] = _prior_closes_day
                if by_date[d]:
                    prev_close = by_date[d][-1].close

        # Precompute V2 for each trading day
        for d in sorted(_all_bars_by_date.keys()):
            pc = _prior_closes.get(d, {})
            spy_bars_d = _spy_by_date.get(d, [])
            in_play_v2.precompute_day(
                _all_bars_by_date[d], spy_bars_d, d, prior_closes=pc
            )
        print(f"  V2 pre-pass complete: {len(_all_bars_by_date)} trading days processed.")
        del _all_bars_by_date, _spy_by_date, _spy_all_bars  # free memory

    print(f"\n  Processing {len(symbols_5m)} symbols through live pipeline...")

    for idx, sym in enumerate(symbols_5m):
        p_5m = DATA_DIR / f"{sym}_5min.csv"
        if not p_5m.exists():
            continue
        bars_5m = load_bars_from_csv(str(p_5m))
        if not bars_5m:
            continue

        # Load 1-min data if available (for 1-min strategies + in-play)
        p_1m = DATA_1MIN_DIR / f"{sym}_1min.csv"
        bars_1m = load_bars_from_csv(str(p_1m)) if p_1m.exists() else None

        # ── Initialize StrategyManager for this symbol ──
        strat_cfg = StrategyConfig(timeframe_min=5)

        # FPIP V3 variant configs (isolated copies so they don't pollute other strategies)
        from copy import deepcopy

        def _make_fpip_v3_a(base):
            c = deepcopy(base)
            c.fpip_max_pullback_depth = 0.70
            c.fpip_max_pullback_vol_ratio = 0.95
            c.fpip_trigger_require_close_above_prev_high = True
            c.fpip_entry_mode = "prev_high_break"
            c.fpip_entry_buffer = 0.01
            c.fpip_target_mode = "fixed_rr"
            c.fpip_target_rr = 1.5
            return c

        def _make_fpip_v3_b(base):
            c = _make_fpip_v3_a(base)
            c.fpip_target_rr = 2.0
            return c

        def _make_fpip_v3_c(base):
            c = _make_fpip_v3_a(base)
            c.fpip_max_pullback_vol_ratio = 1.00
            c.fpip_target_rr = 1.5
            return c

        # FL rebuild variant configs
        def _make_fl_hybrid_struct_q7(base):
            """High-quality structural: 10:30-11:00, current_hybrid stop, structural target, quality>=7."""
            c = deepcopy(base)
            c.fl_time_start = {1: 1030, 5: 1030}
            c.fl_time_end = {1: 1100, 5: 1100}
            c.fl_stop_mode = "current_hybrid"
            c.fl_target_mode = "structural"
            c.fl_min_quality_score = 7.0
            return c

        def _make_fl_source_r10_q6(base):
            """Balanced fixed-R: 10:30-11:00, source_faithful stop, fixed 1.0R, quality>=6."""
            c = deepcopy(base)
            c.fl_time_start = {1: 1030, 5: 1030}
            c.fl_time_end = {1: 1100, 5: 1100}
            c.fl_stop_mode = "source_faithful"
            c.fl_target_mode = "fixed_rr"
            c.fl_target_rr = 1.0
            c.fl_min_quality_score = 6.0
            return c

        # SP V2 variant configs
        def _make_sp_v2_bal(base):
            """Balanced: time<=14:05, RR<=1.9, struct_q>=0.6."""
            c = deepcopy(base)
            c.sp_v2_enabled = True
            c.sp_v2_max_selected_rr = 1.9
            c.sp_v2_min_structure_quality = 0.6
            c.sp_v2_min_bar_return_pct = 0.0
            return c

        def _make_sp_v2_simple(base):
            """Simple: time<=14:05, RR<=1.9, no struct_q floor."""
            c = deepcopy(base)
            c.sp_v2_enabled = True
            c.sp_v2_max_selected_rr = 1.9
            c.sp_v2_min_structure_quality = 0.0
            c.sp_v2_min_bar_return_pct = 0.0
            return c

        def _make_sp_v2_hq(base):
            """HQ: balanced + bar_return_pct>=0.25."""
            c = _make_sp_v2_bal(base)
            c.sp_v2_min_bar_return_pct = 0.25
            return c

        # BDR V3 variant configs
        def _make_bdr_v3_a(base):
            c = deepcopy(base)
            c.bdr_v3_enabled = True
            c.bdr_use_orl_level = True
            c.bdr_use_swing_low_level = True
            c.bdr_use_vwap_level = False
            c.bdr_v3_time_start = 1025
            c.bdr_v3_time_end = 1040
            c.bdr_setup_mode = "weak_retest_break"
            c.bdr_max_reclaim_above_level_atr = 0.10
            c.bdr_retest_close_max_pos = 0.55
            c.bdr_retest_body_max_pct = 0.55
            c.bdr_retest_min_upper_wick_pct = 0.10
            c.bdr_require_retest_below_vwap = True
            c.bdr_require_retest_below_ema9 = True
            c.bdr_require_trigger_below_vwap = True
            c.bdr_require_trigger_below_ema9 = True
            c.bdr_require_retest_vol_not_stronger_than_breakdown = True
            c.bdr_entry_mode = "retest_low_break"
            c.bdr_entry_buffer = 0.01
            c.bdr_trigger_bars_after_retest = 2
            c.bdr_stop_mode = "retest_high"
            c.bdr_v3_stop_buffer = 0.01
            c.bdr_target_mode_v3 = "fixed_rr"
            c.bdr_target_rr_v3 = 1.5
            c.bdr_skip_generic_trigger_body_filter = True
            c.bdr_skip_generic_trigger_vol_filter = True
            c.bdr_require_red_trend = False
            return c

        def _make_bdr_v3_b(base):
            c = _make_bdr_v3_a(base)
            c.bdr_v3_time_end = 1035
            return c

        def _make_bdr_v3_c(base):
            c = _make_bdr_v3_a(base)
            c.bdr_target_rr_v3 = 2.0
            return c

        def _make_bdr_v3_d(base):
            c = _make_bdr_v3_a(base)
            c.bdr_setup_mode = "failed_reclaim_break"
            c.bdr_retest_close_max_pos = 0.50
            c.bdr_retest_body_max_pct = 0.45
            c.bdr_retest_min_upper_wick_pct = 0.15
            return c

        # BDR V4 variant configs (rebuilt: internal quality gate, bypasses external A-tier)
        def _make_bdr_v4_a(base):
            """Balanced: 10:25-10:35, ip>=0.80, tq>=0.60, no extra quality floor."""
            c = _make_bdr_v3_a(base)  # inherits V3 base (ORL+swing, no VWAP, no regime, etc.)
            c.bdr_v4_enabled = True
            c.bdr_v4_min_ip_score = 0.80
            c.bdr_v4_min_trigger_quality = 0.60
            c.bdr_v4_min_quality_score = 0.0
            c.bdr_v4_time_start = 1025
            c.bdr_v4_time_end = 1035
            return c

        def _make_bdr_v4_b(base):
            """Broader: 10:20-10:40."""
            c = _make_bdr_v4_a(base)
            c.bdr_v4_time_start = 1020
            c.bdr_v4_time_end = 1040
            return c

        def _make_bdr_v4_c(base):
            """Stricter: 10:25-10:35, quality_score>=3.0."""
            c = _make_bdr_v4_a(base)
            c.bdr_v4_min_quality_score = 3.0
            return c

        def _make_bdr_v4_d(base):
            """Strictest D-descendant: 10:25-10:35, quality>=3.0, failed_reclaim anatomy."""
            c = _make_bdr_v4_c(base)
            c.bdr_setup_mode = "failed_reclaim_break"
            c.bdr_retest_close_max_pos = 0.50
            c.bdr_retest_body_max_pct = 0.45
            c.bdr_retest_min_upper_wick_pct = 0.15
            return c

        # EMA9 V4 variant configs
        def _make_ema9_v4_a(base):
            c = deepcopy(base)
            c.ema9_v4_enabled = True
            c.ema9_v4_time_start = 1000
            c.ema9_v4_time_end = 1045
            c.ema9_require_relative_impulse_vs_spy = True
            c.ema9_min_relative_impulse_vs_spy = 2.0  # percent-points (stock outperforms SPY by 2%+)
            c.ema9_stop_mode_v4 = "two_bar_low"
            c.ema9_stop_buffer_v4 = 0.01
            c.ema9_target_mode_v4 = "fixed_rr"
            c.ema9_target_rr_v4 = 1.25
            return c

        def _make_ema9_v4_b(base):
            c = _make_ema9_v4_a(base)
            c.ema9_target_rr_v4 = 2.0
            return c

        def _make_ema9_v4_c(base):
            c = _make_ema9_v4_a(base)
            c.ema9_min_relative_impulse_vs_spy = 4.0  # percent-points (stricter: stock outperforms SPY by 4%+)
            return c

        def _make_ema9_v4_d(base):
            c = _make_ema9_v4_a(base)
            c.ema9_require_5m_context = True
            c.ema9_5m_require_above_vwap = True
            c.ema9_5m_require_ema9_gt_ema20 = True
            c.ema9_5m_touch_count_max = 4
            return c

        # EMA9 V5 variant configs (rebuilt: wider stop floor, tighter window, structural target)
        def _make_ema9_v5_a(base):
            c = deepcopy(base)
            c.ema9_v5_enabled = True
            c.ema9_v5_time_start = 1000
            c.ema9_v5_time_end = 1059
            c.ema9_v5_min_stop_dollar = 0.35
            c.ema9_v5_min_ip_score = 0.80
            c.ema9_v5_min_quality_score = 3.0
            c.ema9_v5_price_min = 0.0
            c.ema9_v5_price_max = 99999.0
            c.ema9_v5_struct_min_rr = 0.0
            c.ema9_v5_struct_max_rr = 5.0
            return c

        def _make_ema9_v5_b(base):
            c = _make_ema9_v5_a(base)
            c.ema9_v5_min_stop_dollar = 0.40
            return c

        def _make_ema9_v5_c(base):
            c = _make_ema9_v5_a(base)
            c.ema9_v5_price_min = 25.0
            c.ema9_v5_price_max = 250.0
            return c

        def _make_ema9_v5_d(base):
            c = _make_ema9_v5_b(base)
            c.ema9_v5_price_min = 25.0
            c.ema9_v5_price_max = 250.0
            return c

        # ── PRODUCTION SLEEVE (single source of truth) ──
        from .production_sleeve import build_production_strategies
        live_strats = build_production_strategies(strat_cfg)

        # Disabled strategies preserved in comments — see REVISIT_NOTES.md
        mgr = StrategyManager(strategies=live_strats, symbol=sym, config=strat_cfg)

        # Daily ATR warmup
        sym_dates = sorted(set(b.timestamp.date() for b in bars_5m))
        if len(bars_5m) >= 14:
            atr_val = sum(
                max(b.high - b.low, 0.01) for b in bars_5m[:14]
            ) / 14
            mgr.indicators.warm_up_daily(atr_val, bars_5m[0].high, bars_5m[0].low)

        # ── Seed 5-min indicators from first trading day ──
        # Feeds first day's 5-min bars through EMA9/20, ATR, vol MA so that
        # 5-min-native strategies are indicator-ready on day 2 at bar 1.
        # In live/dashboard, this would use prior-day historical bars loaded
        # from disk so indicators are ready at 9:35 on the CURRENT day.
        if len(sym_dates) >= 2:
            first_date = sym_dates[0]
            seed_bars = [b for b in bars_5m if b.timestamp.date() == first_date]
            mgr.indicators.seed_5min(seed_bars)

        # ── In-play: precompute for 5-min-only symbols ──
        if bars_1m is None:
            in_play_proxy_5m.precompute(sym, bars_5m)

        # ── In-play state for this symbol ──
        ip_result_current: Optional[InPlayResult] = None
        ip_eval_date: Optional[int] = None

        # Determine bar source and processing mode
        if bars_1m is not None:
            # Full dual-timeframe: feed 1-min bars, upsample to 5-min
            upsampler = BarUpsampler(5)
            source_bars = bars_1m
            use_1m = True
        else:
            # 5-min only: feed through legacy update()
            source_bars = bars_5m
            use_1m = False

        # Precompute EMA9 values for source_bars (used for EMA9_FT trailing exit)
        _ema9_vals = []
        _ema9_k = 2.0 / (9 + 1)
        _ema9_cur = NaN
        _ema9_day = None
        _ema9_day_prices = []
        for _b in source_bars:
            _bd = _b.timestamp.date()
            if _ema9_day is None or _bd != _ema9_day:
                _ema9_day = _bd
                _ema9_cur = NaN
                _ema9_day_prices = []
            _ema9_day_prices.append(_b.close)
            if _isnan(_ema9_cur):
                if len(_ema9_day_prices) >= 9:
                    _ema9_cur = sum(_ema9_day_prices[-9:]) / 9
            else:
                _ema9_cur = _b.close * _ema9_k + _ema9_cur * (1 - _ema9_k)
            _ema9_vals.append(_ema9_cur)

        # Track stock pct from open for RS/tape context
        day_open_rs = float('nan')
        rs_date = None

        for bar in source_bars:
            bar_date = bar.timestamp.date()
            hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute
            date_int = bar.timestamp.year * 10000 + bar.timestamp.month * 100 + bar.timestamp.day

            # Track day open for stock pct
            if rs_date is None or rs_date != date_int:
                rs_date = date_int
                day_open_rs = bar.open
            stock_pct = ((bar.close - day_open_rs) / day_open_rs * 100.0
                         if day_open_rs > 0 else float('nan'))

            raw_signals = []

            if use_1m:
                # Build market context from 1-min SPY snapshot
                spy_snap = spy_1m_snaps.get(bar.timestamp)
                # Fall back to nearest 5-min SPY snapshot
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

                # Feed 1-min bar
                signals_1m = mgr.on_1min_bar(bar, market_ctx=mkt_ctx)
                raw_signals.extend(signals_1m)

                # In-play evaluation (after first N bars)
                si = mgr.indicators
                if ip_eval_date is not None and ip_eval_date != date_int:
                    ip_result_current = None  # reset on day change
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
                    # Surface real in-play score to SharedIndicators → strategies read via snap
                    mgr.indicators.in_play_score = ip_result_current.score if ip_result_current.passed else 0.0
                    ip_eval_log.append((
                        sym, bar_date, ip_result_current.passed,
                        ip_result_current.score, ip_result_current.data_status,
                        ip_result_current.reason))

                # Upsample to 5-min
                bar_5m = upsampler.on_bar(bar)
                if bar_5m is not None:
                    signals_5m = mgr.on_5min_bar(bar_5m, market_ctx=mkt_ctx)
                    raw_signals.extend(signals_5m)

                alert_bar = bar_5m if bar_5m is not None else bar
                alert_spy_snap = spy_snap
            else:
                # 5-min only path
                spy_snap = spy_snap_by_ts.get(bar.timestamp)
                qqq_snap = qqq_snap_by_ts.get(bar.timestamp)
                mkt_ctx = None
                if spy_snap and qqq_snap and spy_snap.ready and qqq_snap.ready:
                    mkt_ctx = compute_market_context(
                        spy_snap, qqq_snap, sector_snapshot=None,
                        stock_pct_from_open=stock_pct)

                raw_signals = mgr.on_bar(bar, market_ctx=mkt_ctx)

                # In-play evaluation (5-min path: use precomputed stats)
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
                    # Surface real in-play score to SharedIndicators → strategies read via snap
                    mgr.indicators.in_play_score = ip_result_current.score if ip_result_current.passed else 0.0
                    # Log 5m-path IP evaluation (was missing — only 1m path logged)
                    ip_eval_log.append((
                        sym, bar_date, ip_result_current.passed,
                        ip_result_current.score, ip_result_current.data_status,
                        ip_result_current.reason))

                alert_bar = bar
                alert_spy_snap = spy_snap

            if not raw_signals:
                continue

            # ── Gate chain ──
            for sig in raw_signals:
                funnel["raw_signals"] += 1
                per_strategy_funnel[sig.strategy_name]["raw"] += 1
                sig_hhmm = sig.hhmm if hasattr(sig, 'hhmm') else hhmm

                if not ungated:
                    # Gate 0: Regime (time-normalized thresholds)
                    regime_pass, regime_reason = check_regime_gate(
                        sig.strategy_name, alert_spy_snap, bar_time_hhmm=sig_hhmm)
                    if not regime_pass:
                        funnel["blocked_regime"] += 1
                        per_strategy_funnel[sig.strategy_name]["blocked_regime"] += 1
                        continue

                    # Gate 0.5: In-play
                    if _USE_IP_V2:
                        # V2 gate: percentile-ranked, two-stage
                        _ip_v2_result = in_play_v2.get_result(sym, bar_date, hhmm=sig_hhmm)

                        # Strategies with internal IP gates that fire before V2 is ready
                        _IP_GATE_BYPASS_STRATEGIES = {"GGG_LONG_V1"}  # fires 9:30-9:45, before V2 at 10:00

                        if sig.strategy_name in _IP_GATE_BYPASS_STRATEGIES:
                            # Strategy uses its own internal IP gate — skip external V2 check
                            ip_score = _ip_v2_result.active_score if not math.isnan(_ip_v2_result.active_score) else 0.0
                            _ip_passed = True
                        else:
                            # Per-strategy threshold (falls back to global default)
                            _strat_thresh = ip_cfg.ip_v2_threshold_by_strategy.get(
                                sig.strategy_name, ip_cfg.ip_v2_threshold_confirmed)
                            _ip_score_raw = _ip_v2_result.active_score
                            _ip_passed = (not math.isnan(_ip_score_raw) and
                                          _ip_score_raw >= _strat_thresh and
                                          _ip_v2_result.active_score_kind != "NONE")

                        if not _ip_passed:
                            funnel["blocked_inplay"] += 1
                            per_strategy_funnel[sig.strategy_name]["blocked_inplay"] += 1
                            if _ip_v2_result.active_score_kind == "NONE":
                                funnel["ip_pending_blocked"] = funnel.get("ip_pending_blocked", 0) + 1
                            continue

                        # Block provisional promotion unless explicitly allowed.
                        # Provisional is informational by default. Confirmed is the promotable stage.
                        # Per-strategy override: ip_v2_allow_provisional_by_strategy
                        if _ip_v2_result.active_score_kind == "PROVISIONAL":
                            _allow_prov = (ip_cfg.ip_v2_allow_provisional_promotion or
                                           ip_cfg.ip_v2_allow_provisional_by_strategy.get(
                                               sig.strategy_name, False))
                            if not _allow_prov:
                                funnel["blocked_inplay"] += 1
                                per_strategy_funnel[sig.strategy_name]["blocked_inplay"] += 1
                                funnel["ip_provisional_blocked"] = funnel.get("ip_provisional_blocked", 0) + 1
                                continue

                        ip_score = _ip_v2_result.active_score
                    else:
                        # V1 gate: hard gate + per-strategy score minimum
                        _IP_SCORE_MIN_DEFAULT = 3.0
                        _IP_SCORE_MIN_BY_STRATEGY = {
                            "SP_ATIER": 3.5,
                            "ORH_FBO_V2_B": 3.5,
                        }
                        _IP_SCORE_MIN = _IP_SCORE_MIN_BY_STRATEGY.get(
                            sig.strategy_name, _IP_SCORE_MIN_DEFAULT)
                        if ip_result_current is None or not ip_result_current.passed:
                            funnel["blocked_inplay"] += 1
                            per_strategy_funnel[sig.strategy_name]["blocked_inplay"] += 1
                            if ip_result_current is None:
                                funnel["ip_pending_blocked"] = funnel.get("ip_pending_blocked", 0) + 1
                            continue
                        if ip_result_current.score < _IP_SCORE_MIN:
                            funnel["blocked_inplay_score"] += 1
                            per_strategy_funnel[sig.strategy_name]["blocked_inplay_score"] = \
                                per_strategy_funnel[sig.strategy_name].get("blocked_inplay_score", 0) + 1
                            continue
                        ip_score = ip_result_current.score

                    # ── Quality tier gate (read from strategy's internal pipeline) ──
                    # Matches dashboard.py exactly: strategies compute quality_tier
                    # internally via injected QualityScorer (rejection filters →
                    # quality scoring → tier assignment). We read the result from
                    # metadata rather than recomputing externally, ensuring parity.
                    _qt = sig.metadata.get("quality_tier", "B") if sig.metadata else "B"
                    if _qt != QualityTier.A_TIER.value:  # "A"
                        funnel["blocked_quality"] += 1
                        per_strategy_funnel[sig.strategy_name]["blocked_quality"] += 1
                        continue

                    # ── EMA9_FT cluster management (matches dashboard.py) ──
                    if sig.strategy_name == "EMA9_FT":
                        _EMA9FT_SPY_MIN_PCT = 0.50  # SPY must be >= +0.50% from open
                        _EMA9FT_DAILY_CAP = 3        # max alerts per day
                        # Gate: SPY % from open
                        if alert_spy_snap is not None and alert_spy_snap.ready:
                            _spy_pct = alert_spy_snap.pct_from_open
                            if not (_spy_pct is not None and not _isnan(_spy_pct)
                                    and _spy_pct >= _EMA9FT_SPY_MIN_PCT):
                                funnel["blocked_quality"] += 1
                                per_strategy_funnel[sig.strategy_name]["blocked_quality"] += 1
                                continue
                        else:
                            funnel["blocked_quality"] += 1
                            per_strategy_funnel[sig.strategy_name]["blocked_quality"] += 1
                            continue
                        # Gate: daily cap (first-in-time)
                        _e9ft_day = alert_bar.timestamp.date()
                        _e9ft_key = f"EMA9_FT_{_e9ft_day}"
                        _e9ft_count = funnel.get(_e9ft_key, 0)
                        if _e9ft_count >= _EMA9FT_DAILY_CAP:
                            funnel["blocked_quality"] += 1
                            per_strategy_funnel[sig.strategy_name]["blocked_quality"] += 1
                            continue
                        funnel[_e9ft_key] = _e9ft_count + 1

                # ── Survived all gates → simulate trade ──
                funnel["promoted"] += 1
                per_strategy_funnel[sig.strategy_name]["promoted"] += 1

                ip_score = ip_result_current.score if ip_result_current else 0.0
                regime_label = regime_reason if not ungated else ""

                # Compute quality inputs available in live pipeline
                snap = mgr.last_snap
                trig_q = 0.0
                rs_mkt = 0.0
                if snap is not None:
                    # trigger_quality: bar body/range/volume score (0-1)
                    _atr = snap.atr_5m if not _isnan(snap.atr_5m) else snap.daily_atr
                    _vma = snap.vol_ma_5m if not _isnan(snap.vol_ma_5m) else snap.vol_ma_1m
                    trig_q = trigger_bar_quality(alert_bar, _atr, _vma)
                    # rs_market: stock pct from open minus SPY pct from open
                    if (not _isnan(stock_pct) and alert_spy_snap is not None
                            and not _isnan(alert_spy_snap.pct_from_open)):
                        rs_mkt = stock_pct - alert_spy_snap.pct_from_open

                # Inject into metadata for CSV export
                if sig.metadata is None:
                    sig.metadata = {}
                sig.metadata["trigger_quality"] = trig_q
                sig.metadata["rs_market"] = rs_mkt

                # Bar-shape metadata for post-hoc analysis
                _bar_range = alert_bar.high - alert_bar.low
                if _bar_range > 0:
                    _close_loc = (alert_bar.close - alert_bar.low) / _bar_range
                    _body = abs(alert_bar.close - alert_bar.open) / _bar_range
                    # Counter-wick: wick opposite to direction
                    if alert_bar.close >= alert_bar.open:  # bullish bar
                        _counter_wick = (alert_bar.open - alert_bar.low) / _bar_range
                    else:  # bearish bar
                        _counter_wick = (alert_bar.high - alert_bar.open) / _bar_range
                else:
                    _close_loc, _body, _counter_wick = 0.5, 0.0, 0.0

                _bar_ret = 0.0
                if alert_bar.open > 0:
                    _bar_ret = (alert_bar.close - alert_bar.open) / alert_bar.open * 100

                _rel_impulse = 0.0
                if alert_spy_snap is not None and not _isnan(alert_spy_snap.pct_from_open):
                    _rel_impulse = _bar_ret - (alert_spy_snap.pct_from_open / 100 if abs(alert_spy_snap.pct_from_open) > 1 else alert_spy_snap.pct_from_open)

                sig.metadata["close_location"] = _close_loc
                sig.metadata["body_fraction"] = _body
                sig.metadata["counter_wick_fraction"] = _counter_wick
                sig.metadata["bar_return_pct"] = _bar_ret
                sig.metadata["replay_relative_impulse_vs_spy"] = _rel_impulse  # audit-only, don't overwrite strategy value

                strat_sig = raw_signal_to_strategy_signal(
                    sig, sym, alert_bar.timestamp,
                    in_play_score=ip_score,
                    regime_label=regime_label,
                )

                # Find bar index in source bars for trade simulation
                bar_idx = _find_bar_idx(source_bars, alert_bar.timestamp)
                if bar_idx < 0:
                    continue

                max_bars = STRATEGY_MAX_BARS.get(sig.strategy_name, 78)

                # All signals reaching here should now have real structural targets
                # (strategies skip when compute_structural_target returns skipped=True).
                _tt = sig.metadata.get("target_tag", "MISSING")
                target_rr = sig.metadata.get(
                    "actual_rr", STRATEGY_TARGET_RR.get(sig.strategy_name, 2.0))

                # Safety: if somehow a non-structural signal slips through, flag it
                _is_structural = _tt not in ("fixed_rr", "fixed_rr_fallback",
                                              "no_structural_target", "MISSING")

                # Audit tracking
                _ar = sig.metadata.get("actual_rr", None)
                target_tag_audit[sig.strategy_name].append(
                    (_tt, _ar, strat_sig.risk, strat_sig.entry_price,
                     strat_sig.target_price, _is_structural))

                # Wire EMA9_FT trailing exit: source material specifies
                # trailing stop on candle close below 9 EMA
                _trail = sig.strategy_name == "EMA9_FT"
                trade = simulate_strategy_trade(
                    strat_sig, source_bars, bar_idx,
                    max_bars=max_bars, target_rr=target_rr,
                    trail_ema9=_trail,
                    ema9_values=_ema9_vals if _trail else None,
                    slip_per_side_bps=SLIP_PER_SIDE_BPS,
                )
                # Tag the trade for split reporting
                trade.target_type = "structural" if _is_structural else "no_target"
                all_trades.append(trade)
                trades_by_strat[sig.strategy_name].append(trade)

        # 1-min symbols: stats cached by evaluate_session() during loop
        # 5-min-only symbols: stats cached by precompute() before loop

        if (idx + 1) % 20 == 0 or idx == len(symbols_5m) - 1:
            print(f"    {idx + 1}/{len(symbols_5m)} symbols done... "
                  f"({funnel['promoted']} trades so far)")

    # ════════════════════════════════════════════════════════════════
    #  Results
    # ════════════════════════════════════════════════════════════════

    print(f"\n{'=' * 100}")
    print("GATE FUNNEL")
    print(f"{'=' * 100}")
    print(f"  Raw signals:      {funnel['raw_signals']:,}")
    print(f"  Blocked regime:   {funnel['blocked_regime']:,}")
    print(f"  Blocked in-play:  {funnel['blocked_inplay']:,}")
    print(f"  Blocked IP score: {funnel['blocked_inplay_score']:,} (IP < 4.5)")
    print(f"  Blocked tape:     {funnel['blocked_tape']:,} (newer strats only)")
    print(f"  Blocked conflu:   {funnel['blocked_confluence']:,} (newer strats only)")
    print(f"  Blocked quality:  {funnel['blocked_quality']:,} (original 9 strats, A-tier gate)")
    ip_pb = funnel.get('ip_pending_blocked', 0)
    print(f"  IP pending→block: {ip_pb:,} (pre-9:45 signals blocked, IP not yet evaluated)")
    print(f"  PROMOTED:         {funnel['promoted']:,}")
    if funnel['raw_signals'] > 0:
        print(f"  Promotion rate:   {funnel['promoted'] / funnel['raw_signals'] * 100:.1f}%")

    print(f"\n  Per-strategy funnel:")
    print(f"  {'Strategy':<18s} {'Raw':>6s} {'Regime':>8s} {'InPlay':>8s} "
          f"{'Tape':>8s} {'Conflu':>8s} {'Quality':>8s} {'Promoted':>10s}")
    print(f"  {'-'*18} {'-'*6} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*10}")
    for strat in sorted(per_strategy_funnel):
        f = per_strategy_funnel[strat]
        print(f"  {strat:<18s} {f['raw']:>6d} {f['blocked_regime']:>8d} "
              f"{f['blocked_inplay']:>8d} {f['blocked_tape']:>8d} "
              f"{f['blocked_confluence']:>8d} {f['blocked_quality']:>8d} "
              f"{f['promoted']:>10d}")

    # ── In-play summary ──
    # 1-min path evaluations
    if ip_eval_log:
        ip_pass = sum(1 for _, _, p, _, _, _ in ip_eval_log if p)
        ip_total = len(ip_eval_log)
        print(f"\n  In-play (1-min path): {ip_pass}/{ip_total} passed "
              f"({ip_pass / ip_total * 100:.1f}%)")
        status_dist = defaultdict(int)
        for _, _, _, _, ds, _ in ip_eval_log:
            status_dist[ds] += 1
        print(f"  Data status: {dict(status_dist)}")
    # 5-min-only path evaluations
    ip5_stats = in_play_proxy_5m.summary_stats()
    if ip5_stats["total_symbol_days"] > 0:
        print(f"  In-play (5-min path): {ip5_stats['passed_symbol_days']}/{ip5_stats['total_symbol_days']} "
              f"passed ({ip5_stats['pass_rate_pct']:.1f}%)")

    # ── Trade metrics per strategy ──
    print(f"\n{'=' * 100}")
    print("TRADE METRICS — PER STRATEGY")
    print(f"{'=' * 100}")

    strat_metrics = {}
    for strat in sorted(trades_by_strat):
        trades = trades_by_strat[strat]
        m = compute_metrics(trades)
        strat_metrics[strat] = m
        print(f"\n  ── {strat} ──")
        print(f"  N={m['n']}  PF={pf_str(m['pf'])}  Exp={m['exp']:+.3f}R  TotalR={m['total_r']:+.1f}  "
              f"WR={m['wr']:.1f}%  MaxDD={m['max_dd_r']:.1f}R")
        print(f"  AvgWin={m['avg_win']:+.2f}R  AvgLoss={m['avg_loss']:.2f}R")
        print(f"  Train: PF={pf_str(m['train_pf'])} N={m['train_n']}  |  "
              f"Test: PF={pf_str(m['test_pf'])} N={m['test_n']}  |  "
              f"WalkFwd: PF={pf_str(m['wf_pf'])} N={m['wf_n']}")
        print(f"  ExDay={pf_str(m['ex_best_day_pf'])}  ExSym={pf_str(m['ex_top_sym_pf'])}")
        print(f"  Stop={m['stop_rate']:.1f}%  Tgt={m['target_rate']:.1f}%  Time={m['time_rate']:.1f}%  "
              f"AvgBars={m['avg_bars_held']:.1f}")
        print(f"  Days={m['days_active']}  Avg/day={m['avg_per_day']:.2f}  %Pos={m['pct_pos_days']:.1f}%")

        # Promotion criteria
        c1 = m["pf"] > 1.0
        c2 = m["exp"] > 0
        c3 = m["train_pf"] > 0.80
        c4 = m["test_pf"] > 0.80
        c5 = m["wf_pf"] > 0.80
        n_ok = m["n"] >= 10
        all_pass = c1 and c2 and c3 and c4 and c5 and n_ok
        print(f"  Criteria: N≥10={'P' if n_ok else 'F'}  PF>1.0={'P' if c1 else 'F'}  "
              f"Exp>0={'P' if c2 else 'F'}  TrnPF>0.80={'P' if c3 else 'F'}  "
              f"TstPF>0.80={'P' if c4 else 'F'}  WfPF>0.80={'P' if c5 else 'F'}  "
              f"→ {'PROMOTED' if all_pass else 'FAIL'}")

        # Monthly
        monthly_r = defaultdict(float)
        monthly_n = defaultdict(int)
        for t in trades:
            key = t.entry_date.strftime("%Y-%m")
            monthly_r[key] += t.pnl_rr
            monthly_n[key] += 1
        cum_mo = 0.0
        print(f"  Monthly:")
        for mo in sorted(monthly_r):
            cum_mo += monthly_r[mo]
            print(f"    {mo}  N={monthly_n[mo]:3d}  R={monthly_r[mo]:+7.1f}  cum={cum_mo:+.1f}")

    # ── Combined ──
    long_strats = {"SC_SNIPER", "SP_ATIER", "SP_V2_BAL", "SP_V2_SIMPLE", "SP_V2_HQ", "HH_QUALITY",
                   "EMA_FPIP", "EMA_FPIP_V3_A", "EMA_FPIP_V3_B", "EMA_FPIP_V3_C",
                   "EMA9_FT", "EMA9_V4_A", "EMA9_V4_B", "EMA9_V4_C", "EMA9_V4_D",
                   "EMA9_V5_A", "EMA9_V5_B", "EMA9_V5_C", "EMA9_V5_D",
                   "BS_STRUCT", "GGG_LONG_V1", "ORL_FBD_LONG",
                   "FFT_NEWLOW_REV", "FL_REBUILD_STRUCT_Q7", "FL_REBUILD_R10_Q6"}
    # FL_ANTICHOP demoted 2026-03-17
    short_strats = {"BDR_SHORT", "BDR_V3_A", "BDR_V3_B", "BDR_V3_C", "BDR_V3_D",
                    "BDR_V4_A", "BDR_V4_B", "BDR_V4_C", "BDR_V4_D",
                    "ORH_FBO_V2_A", "ORH_FBO_V2_B", "PDH_FBO_B"}

    long_trades = [t for t in all_trades
                   if t.signal.strategy_name in long_strats]
    short_trades = [t for t in all_trades
                    if t.signal.strategy_name in short_strats]

    print(f"\n{'=' * 100}")
    print("COMBINED METRICS")
    print(f"{'=' * 100}")

    lm = compute_metrics(long_trades)
    sm = compute_metrics(short_trades)
    am = compute_metrics(all_trades)

    print(f"\n  LONG BOOK:   N={lm['n']}  PF={pf_str(lm['pf'])}  "
          f"Exp={lm['exp']:+.3f}R  TotalR={lm['total_r']:+.1f}  MaxDD={lm['max_dd_r']:.1f}R")
    print(f"  SHORT BOOK:  N={sm['n']}  PF={pf_str(sm['pf'])}  "
          f"Exp={sm['exp']:+.3f}R  TotalR={sm['total_r']:+.1f}  MaxDD={sm['max_dd_r']:.1f}R")
    print(f"  ALL:         N={am['n']}  PF={pf_str(am['pf'])}  "
          f"Exp={am['exp']:+.3f}R  TotalR={am['total_r']:+.1f}  MaxDD={am['max_dd_r']:.1f}R")

    if am['n'] >= 10:
        print(f"  Train: PF={pf_str(am['train_pf'])} N={am['train_n']}  |  "
              f"Test: PF={pf_str(am['test_pf'])} N={am['test_n']}  |  "
              f"WalkFwd: PF={pf_str(am['wf_pf'])} N={am['wf_n']}")
        print(f"  ExDay={pf_str(am['ex_best_day_pf'])}  ExSym={pf_str(am['ex_top_sym_pf'])}")

    # ── Top symbols ──
    print(f"\n{'=' * 100}")
    print("TOP SYMBOLS")
    print(f"{'=' * 100}")
    for strat in sorted(trades_by_strat):
        trades = trades_by_strat[strat]
        sym_r = defaultdict(lambda: [0.0, 0])
        for t in trades:
            sym_r[t.symbol][0] += t.pnl_rr
            sym_r[t.symbol][1] += 1
        ranked = sorted(sym_r.items(), key=lambda x: -x[1][0])[:10]
        print(f"\n  {strat} Top 10:")
        for s, (r, cnt) in ranked:
            print(f"        {s:>6}  N={cnt:3d}  R={r:+.1f}")

    # ── Final summary ──
    print(f"\n{'=' * 100}")
    print("FINAL SUMMARY")
    print(f"{'=' * 100}")
    for strat in sorted(trades_by_strat):
        m = strat_metrics[strat]
        print(f"  {strat:<18s}: N={m['n']}, PF={pf_str(m['pf'])}, "
              f"Exp={m['exp']:+.3f}R, TotalR={m['total_r']:+.1f}")
    print(f"  {'COMB LONG':<18s}: N={lm['n']}, PF={pf_str(lm['pf'])}, "
          f"Exp={lm['exp']:+.3f}R, TotalR={lm['total_r']:+.1f}")
    print(f"  {'COMB SHORT':<18s}: N={sm['n']}, PF={pf_str(sm['pf'])}, "
          f"Exp={sm['exp']:+.3f}R, TotalR={sm['total_r']:+.1f}")
    print(f"  {'COMB ALL':<18s}: N={am['n']}, PF={pf_str(am['pf'])}, "
          f"Exp={am['exp']:+.3f}R, TotalR={am['total_r']:+.1f}")

    # ── Split by target type: structural vs no-target ──
    struct_trades = [t for t in all_trades if getattr(t, 'target_type', '') == 'structural']
    notgt_trades = [t for t in all_trades if getattr(t, 'target_type', '') == 'no_target']

    print(f"\n{'=' * 100}")
    print("SPLIT BY TARGET TYPE — Structural Exit vs No-Target (stop/EOD only)")
    print(f"{'=' * 100}")

    if struct_trades:
        sm2 = compute_metrics(struct_trades)
        print(f"\n  STRUCTURAL TARGET:  N={sm2['n']}  PF={pf_str(sm2['pf'])}  "
              f"Exp={sm2['exp']:+.3f}R  TotalR={sm2['total_r']:+.1f}  "
              f"WR={sm2['wr']:.1f}%  MaxDD={sm2['max_dd_r']:.1f}R")
    if notgt_trades:
        nm2 = compute_metrics(notgt_trades)
        print(f"  NO-TARGET (stop/EOD): N={nm2['n']}  PF={pf_str(nm2['pf'])}  "
              f"Exp={nm2['exp']:+.3f}R  TotalR={nm2['total_r']:+.1f}  "
              f"WR={nm2['wr']:.1f}%  MaxDD={nm2['max_dd_r']:.1f}R")

    print(f"\n  Per-strategy split:")
    print(f"  {'Strategy':<18s} {'N_str':>6s} {'PF_str':>7s} {'Exp_str':>9s} {'TotR_str':>9s} "
          f"{'WR_str':>7s} │ {'N_noT':>6s} {'PF_noT':>7s} {'Exp_noT':>9s} {'TotR_noT':>9s} "
          f"{'WR_noT':>7s}")
    print(f"  {'-'*18} {'-'*6} {'-'*7} {'-'*9} {'-'*9} {'-'*7} ┼ "
          f"{'-'*6} {'-'*7} {'-'*9} {'-'*9} {'-'*7}")
    for strat in sorted(trades_by_strat):
        st = [t for t in trades_by_strat[strat] if getattr(t, 'target_type', '') == 'structural']
        nt = [t for t in trades_by_strat[strat] if getattr(t, 'target_type', '') == 'no_target']
        ms = compute_metrics(st) if st else None
        mn = compute_metrics(nt) if nt else None
        s_str = (f"{ms['n']:>6d} {pf_str(ms['pf']):>7s} {ms['exp']:>+8.3f}R "
                 f"{ms['total_r']:>+8.1f} {ms['wr']:>6.1f}%") if ms else f"{'—':>6} {'—':>7} {'—':>9} {'—':>9} {'—':>7}"
        n_str = (f"{mn['n']:>6d} {pf_str(mn['pf']):>7s} {mn['exp']:>+8.3f}R "
                 f"{mn['total_r']:>+8.1f} {mn['wr']:>6.1f}%") if mn else f"{'—':>6} {'—':>7} {'—':>9} {'—':>9} {'—':>7}"
        print(f"  {strat:<18s} {s_str} │ {n_str}")

    # ── Monthly breakdown ──
    if all_trades:
        print(f"\n  Monthly breakdown:")
        monthly_r = defaultdict(float)
        monthly_n = defaultdict(int)
        for t in all_trades:
            key = t.entry_date.strftime("%Y-%m")
            monthly_r[key] += t.pnl_rr
            monthly_n[key] += 1
        cum = 0.0
        for mo in sorted(monthly_r):
            cum += monthly_r[mo]
            print(f"    {mo}  N={monthly_n[mo]:3d}  R={monthly_r[mo]:+7.1f}  cum={cum:+.1f}")

    # ── Export CSV ──
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "_ungated" if ungated else "_gated"
    out_path = OUT_DIR / f"replay_live_path{suffix}.csv"
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "entry_time", "symbol", "side", "strategy",
                     "pnl_rr", "exit_reason", "bars_held", "in_play_score",
                     "regime", "structure_quality", "confluence_count",
                     "trigger_quality", "rs_market",
                     "quality_score", "quality_score_enhanced",
                     "close_location", "body_fraction", "counter_wick_fraction",
                     "bar_return_pct", "relative_impulse_vs_spy",
                     "entry_price", "stop_price", "target_price",
                     "risk", "actual_rr", "target_type",
                     "stop_ref_type", "stop_ref_price", "raw_stop",
                     "buffer_type", "buffer_value",
                     "min_stop_rule_applied", "min_stop_distance", "final_stop"])
        for t in sorted(all_trades, key=lambda x: x.signal.timestamp):
            md = t.signal.metadata or {}
            struct_q = md.get("structure_quality", 0.0)
            confluence = md.get("confluence", [])
            conf_count = len(confluence) if isinstance(confluence, list) else 0
            trig_q = md.get("trigger_quality", 0.0)
            rs_mkt = md.get("rs_market", 0.0)

            # ── Partial quality score (original, without trigger/rs) ──
            ip = t.signal.in_play_score
            stock_pts = 0.0
            # Rescaled for 0-7 score (v3: gap removed, max=7)
            if ip >= 5.0:   stock_pts = 1.5
            elif ip >= 3.5: stock_pts = 1.0
            elif ip >= 2.0: stock_pts = 0.5

            regime_label = t.signal.market_regime or ""
            if "GREEN" in regime_label:    mkt_pts = 1.0
            elif "FAILED" in regime_label: mkt_pts = 0.7
            else:                          mkt_pts = 0.5

            setup_pts = struct_q * 2.0 + min(conf_count * 0.5, 1.0)
            q_score = round(min(stock_pts + mkt_pts + setup_pts, 10.0), 1)

            # ── Enhanced quality score (with trigger_quality + rs_market) ──
            # Stock: +rs_market (0-1pt)
            rs_pts = 0.0
            if rs_mkt > 0.003:    rs_pts = 1.0
            elif rs_mkt > 0:      rs_pts = 0.5
            enh_stock = min(stock_pts + rs_pts, 3.0)

            # Setup: +trigger_quality (0-2pts)
            enh_setup = struct_q * 2.0 + trig_q * 2.0 + min(conf_count * 0.5, 1.0)
            enh_setup = min(enh_setup, 5.0)

            q_enh = round(min(enh_stock + mkt_pts + enh_setup, 10.0), 1)

            actual_rr = md.get("actual_rr", 0.0)
            w.writerow([
                str(t.entry_date),
                t.signal.timestamp.strftime("%Y-%m-%d %H:%M"),
                t.symbol,
                "LONG" if t.signal.direction == 1 else "SHORT",
                t.signal.strategy_name,
                f"{t.pnl_rr:+.4f}",
                t.exit_reason,
                t.bars_held,
                f"{t.signal.in_play_score:.1f}",
                t.signal.market_regime,
                f"{struct_q:.2f}",
                conf_count,
                f"{trig_q:.3f}",
                f"{rs_mkt:.4f}",
                f"{q_score:.1f}",
                f"{q_enh:.1f}",
                f"{md.get('close_location', 0.0):.3f}",
                f"{md.get('body_fraction', 0.0):.3f}",
                f"{md.get('counter_wick_fraction', 0.0):.3f}",
                f"{md.get('bar_return_pct', 0.0):.4f}",
                f"{md.get('relative_impulse_vs_spy', 0.0):.4f}",
                f"{t.signal.entry_price:.4f}",
                f"{t.signal.stop_price:.4f}",
                f"{t.signal.target_price:.4f}",
                f"{t.signal.risk:.4f}",
                f"{actual_rr:.3f}",
                getattr(t, 'target_type', ''),
                md.get("stop_ref_type", ""),
                f"{md.get('stop_ref_price', 0.0):.4f}" if md.get("stop_ref_price") else "",
                f"{md.get('raw_stop', 0.0):.4f}" if md.get("raw_stop") else "",
                md.get("buffer_type", ""),
                f"{md.get('buffer_value', 0.0)}" if md.get("buffer_value") is not None else "",
                md.get("min_stop_rule_applied", ""),
                f"{md.get('min_stop_distance', 0.0):.4f}" if md.get("min_stop_distance") else "",
                f"{md.get('final_stop', 0.0):.4f}" if md.get("final_stop") else "",
            ])
    print(f"\n  → Exported {len(all_trades)} trades to {out_path.name}")

    # ── In-play eligibility export ──
    if ip_eval_log:
        ip_path = OUT_DIR / f"live_path_in_play_log{suffix}.csv"
        with open(ip_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "date", "passed", "score", "data_status", "reason"])
            for row in ip_eval_log:
                w.writerow(row)
        print(f"  → Exported {len(ip_eval_log)} in-play evaluations to {ip_path.name}")

    # ════════════════════════════════════════════════════════════════
    #  TARGET R:R AUDIT — Structural vs Fixed Fallback
    # ════════════════════════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("TARGET R:R AUDIT — Structural vs Fixed Fallback")
    print(f"{'=' * 100}")
    print(f"\n{'Strategy':<20} {'N':>5} {'Structural':>11} {'FixedFB':>8} {'Missing':>8} "
          f"{'FB%':>6} {'MedRR':>7} {'MinRR':>7} {'MaxRR':>7} {'#UniqueRR':>10} {'MedRisk%':>9}")
    print("-" * 110)

    import statistics as _stats
    _total_struct = _total_fb = _total_n = 0
    for sn in sorted(target_tag_audit.keys()):
        entries = target_tag_audit[sn]
        n = len(entries)
        structural = sum(1 for tag, *_ in entries
                         if tag not in ("fixed_rr", "fixed_rr_fallback", "MISSING"))
        fallback = sum(1 for tag, *_ in entries
                       if tag in ("fixed_rr", "fixed_rr_fallback"))
        missing = sum(1 for tag, *_ in entries if tag == "MISSING")
        _total_struct += structural
        _total_fb += fallback
        _total_n += n

        rrs = [ar for _, ar, *_ in entries if ar is not None and ar > 0]
        unique_rrs = len(set(round(r, 2) for r in rrs))
        med_rr = _stats.median(rrs) if rrs else 0
        min_rr = min(rrs) if rrs else 0
        max_rr = max(rrs) if rrs else 0

        risk_pcts = [risk / entry * 100 for _, _, risk, entry, _, _ in entries
                     if entry > 0 and risk > 0]
        med_risk = _stats.median(risk_pcts) if risk_pcts else 0
        fb_pct = fallback / n * 100 if n > 0 else 0

        print(f"{sn:<20} {n:>5} {structural:>11} {fallback:>8} {missing:>8} "
              f"{fb_pct:>5.1f}% {med_rr:>7.2f} {min_rr:>7.2f} {max_rr:>7.2f} "
              f"{unique_rrs:>10} {med_risk:>8.2f}%")

    print("-" * 110)
    fb_pct_all = _total_fb / _total_n * 100 if _total_n > 0 else 0
    print(f"{'TOTAL':<20} {_total_n:>5} {_total_struct:>11} {_total_fb:>8} "
          f"{'':>8} {fb_pct_all:>5.1f}%")

    # Per-strategy target tag breakdown
    print(f"\nTARGET TAG DETAIL:")
    for sn in sorted(target_tag_audit.keys()):
        entries = target_tag_audit[sn]
        tag_counts = defaultdict(int)
        for tag, *_ in entries:
            tag_counts[tag] += 1
        tags_str = ", ".join(f"{t}={c}" for t, c in sorted(tag_counts.items(), key=lambda x: -x[1]))
        print(f"  {sn}: {tags_str}")

    print(f"\n{'=' * 100}")
    print("DONE.")
    print(f"{'=' * 100}")


def _find_bar_idx(bars, timestamp):
    for i, b in enumerate(bars):
        if b.timestamp == timestamp:
            return i
    return -1


if __name__ == "__main__":
    main()
