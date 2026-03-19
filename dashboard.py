"""
Portfolio D — Live Dashboard Server (Variant 2)

Runs all symbols in a single process using one IB connection.
Serves a web dashboard on http://localhost:8877 with Server-Sent Events
for real-time alert display (zero polling, instant updates).

Portfolio D = Long book (VK+SC, Q>=2, <15:30, tape permission >= 0.40)
            + Short book (BDR, RED+TREND, AM-only)

Usage:
    python -m alert_overlay.dashboard --symbols AAPL TSLA TSLL SPY QQQ
"""

import argparse
import asyncio
import json
import logging
import math
import os
import sys
import threading
import time
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from typing import Optional, List
from zoneinfo import ZoneInfo
from queue import Queue

try:
    from ib_insync import IB, Stock, util
except ImportError:
    print("ERROR: ib_insync not installed. Run: pip3 install ib_insync")
    sys.exit(1)

from .config import OverlayConfig
from .models import Bar, Signal, NaN
from .market_context import MarketEngine, MarketContext, MarketSnapshot, compute_market_context
from .layered_regime import PermissionWeights, compute_permission

# ── Primary live strategy engine ──
from .strategies.shared.config import StrategyConfig as _StrategyConfig
from .strategies.live.manager import StrategyManager as _StrategyManager
from .strategies.live.base import RawSignal as _RawSignal
from .strategies.live.sc_sniper_live import SCSniperLive as _SCSniperLive
from .strategies.live.fl_antichop_live import FLAntiChopLive as _FLAntiChopLive
from .strategies.live.spencer_atier_live import SpencerATierLive as _SpencerATierLive
from .strategies.live.hitchhiker_live import HitchHikerLive as _HitchHikerLive
from .strategies.live.ema_fpip_live import EmaFpipLive as _EmaFpipLive
from .strategies.live.bdr_short_live import BDRShortLive as _BDRShortLive
from .strategies.live.ema9_ft_live import EMA9FirstTouchLive as _EMA9FirstTouchLive
from .strategies.live.backside_live import BacksideStructureLive as _BacksideStructureLive
from .strategies.live.orl_fbd_long_live import ORLFBDLongLive as _ORLFBDLongLive
from .strategies.live.orh_fbo_short_v2_live import ORHFBOShortV2Live as _ORHFBOShortV2Live
from .strategies.live.pdh_fbo_short_live import PDHFBOShortLive as _PDHFBOShortLive
from .strategies.live.fft_newlow_reversal_live import FFTNewlowReversalLive as _FFTNewlowReversalLive

# ── In-play proxy ──
# V2 is the active base gate. V1 kept only for emergency rollback.
from .strategies.shared.in_play_proxy import InPlayProxy as _InPlayProxy
from .strategies.shared.in_play_proxy import SessionSnapshot as _SessionSnapshot
from .strategies.shared.in_play_proxy import InPlayResult as _InPlayResult
from .strategies.shared.in_play_proxy_v2 import InPlayProxyV2 as _InPlayProxyV2
from .strategies.shared.in_play_proxy_v2 import InPlayResultV2 as _InPlayResultV2

_IP_CFG = _StrategyConfig(timeframe_min=1)
_in_play_proxy = _InPlayProxy(_IP_CFG)  # V1 — kept for rollback only
_in_play_v2 = _InPlayProxyV2(_IP_CFG)   # V2 — active base gate
_USE_IP_V2_LIVE = _IP_CFG.ip_v2_enabled  # True = V2 is active

# ── In-play eligibility log (structured CSV) ──
import csv as _csv
_ip_log_dir = Path(__file__).parent / "session_logs"
_ip_log_dir.mkdir(exist_ok=True)
_ip_log_file = _ip_log_dir / f"in_play_eligibility_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
_ip_log_handle = open(_ip_log_file, "w", newline="", buffering=1)
_ip_csv_writer = _csv.writer(_ip_log_handle)
_ip_csv_writer.writerow([
    "timestamp", "symbol", "date", "hhmm",
    "gap_pct", "rvol", "dolvol", "range_exp", "score",
    "pass_gap", "pass_rvol", "pass_dolvol", "passed",
    "data_status", "vol_depth", "range_depth",
    "regime_spy_pct",
])

# ── Legacy engine — emergency rollback only ──
# Set env var ALERT_LEGACY_ENGINE=1 to fall back to old scan_day() engine
_USE_LEGACY_ENGINE = os.environ.get("ALERT_LEGACY_ENGINE", "").strip() in ("1", "true", "yes")
if _USE_LEGACY_ENGINE:
    from .engine import SignalEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dashboard")

# ── Session log capture: writes to disk continuously + in-memory for report ──
_session_log_lines: list[str] = []
_session_log_dir = Path(__file__).parent / "session_logs"
_session_log_dir.mkdir(exist_ok=True)
_session_log_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
_session_log_file = _session_log_dir / f"session_{_session_log_tag}.log"
_session_file_handle = open(_session_log_file, "a", buffering=1)  # line-buffered


class _SessionLogHandler(logging.Handler):
    """Captures log lines in memory AND flushes to disk on every write."""
    def emit(self, record):
        try:
            msg = self.format(record)
            _session_log_lines.append(msg)
            _session_file_handle.write(msg + "\n")
        except Exception:
            pass


_slh = _SessionLogHandler()
_slh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                     datefmt="%H:%M:%S"))
logging.getLogger().addHandler(_slh)

# ── atexit: generate report even on unclean exit ──
import atexit

def _atexit_report():
    try:
        _session_file_handle.close()
    except Exception:
        pass

atexit.register(_atexit_report)

EASTERN = ZoneInfo("US/Eastern")
DASHBOARD_PORT = 8877

# ── Shared state for the web server ──
_alert_history: List[dict] = []
_status: dict = {"symbols": {}, "connected": False, "started_at": None}
_sse_clients: List[Queue] = []
_runners: List = []        # SymbolRunner instances (mutable at runtime)
_ib: Optional[IB] = None   # Shared IBKR connection
_cfg: Optional[OverlayConfig] = None
_lock = threading.Lock()
_main_loop: Optional[asyncio.AbstractEventLoop] = None  # set in main()

# ── Order manager (initialized in main()) ──
from .order_manager import OrderManager
_order_manager: Optional[OrderManager] = None

# ── Market context engines (SPY + QQQ) for Portfolio D regime gating ──
# Tape permission weights for Variant 2 long-side filter
_TAPE_WEIGHTS = PermissionWeights()
_LONG_TAPE_THRESHOLD = 0.40
# Confluence quality gate — per-strategy minimum confluence count.
# Default=3 for most strategies. Data-driven overrides for shorts.
_MIN_CONFLUENCE_DEFAULT = 3
_MIN_CONFLUENCE_BY_STRATEGY = {
    "ORH_FBO_V2_A": 4,   # PF 1.11→1.25, +15.7R
    "ORH_FBO_V2_B": 4,   # PF 1.35→1.42, +5.0R
    "PDH_FBO_B": 5,       # PF 1.01→1.26, +4.5R
}

# ── Quality-scored strategies (ALL — unified pipeline) ──
# All 13 strategies use internal rejection filters + quality scoring + A-tier gate.
# No more Family A/B split. One pipeline, one gate chain.
_QUALITY_SCORED = {
    "SC_SNIPER", "SP_ATIER", "HH_QUALITY",
    # FL_ANTICHOP demoted 2026-03-17
    "EMA_FPIP", "BDR_SHORT", "EMA9_FT", "BS_STRUCT", "ORL_FBD_LONG",
    "ORH_FBO_V2_A", "ORH_FBO_V2_B", "PDH_FBO_B", "FFT_NEWLOW_REV",
}
_spy_engine: Optional[MarketEngine] = None
_qqq_engine: Optional[MarketEngine] = None
_spy_snap = None
_qqq_snap = None
_spy_runner = None  # SymbolRunner for SPY bar subscription
_qqq_runner = None  # SymbolRunner for QQQ bar subscription

# ── Regime gate: strategy name → gate type ──
# Matches replay's per-strategy regime gates from strategy_registry.py.
# ── Regime gate: imported from shared module (single source of truth) ──
# Time-normalized hostile thresholds: 0.16% pre-9:45, 0.32% 9:45-10:29, 0.63% 10:30+
from alert_overlay.strategies.shared.regime_gate import (
    STRATEGY_REGIME_GATE as _STRATEGY_REGIME_GATE,
    check_regime_gate as _shared_check_regime_gate,
)


def _check_regime_gate(strategy_name: str, bar_time_hhmm: int = None) -> tuple:
    """Thin wrapper around shared regime gate — passes live _spy_snap."""
    global _spy_snap
    return _shared_check_regime_gate(strategy_name, _spy_snap, bar_time_hhmm)

    return True, "unknown_gate"


# ── EMA9_FT cluster management ──
# Cap daily EMA9_FT signals (first-in-time basis) and require SPY confirmation
_EMA9FT_DAILY_CAP = 3            # max EMA9_FT alerts per day (0 = unlimited)
_EMA9FT_SPY_MIN_PCT = 0.50       # SPY must be >= +0.50% from open at signal time
_ema9ft_daily_count = 0           # counter, reset on date change
_ema9ft_last_date = None          # date of last reset

# ── Deferred IBKR connection (called from /api/connect or main) ──

_deferred_args = None  # Stored by main() for deferred connect

def _run_ibkr_connection(host="127.0.0.1", port=7497, client_id=10):
    """Run full IBKR connection + warmup sequence.

    Called either from main() (normal startup) or from a background thread
    when the user clicks Connect in deferred mode.
    ib_insync requires its own asyncio event loop on the calling thread.
    """
    global _ib, _cfg, _runners, _spy_engine, _qqq_engine, _spy_snap, _qqq_snap
    global _order_manager, _spy_runner, _qqq_runner, _main_loop

    # Create a new event loop for this thread (ib_insync needs one)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    cfg = _cfg

    # Build symbol list from current state (static watchlist + in-play)
    symbols = _load_watchlist()
    if not symbols:
        symbols = ["AAPL", "TSLA", "TSLL", "SPY", "QQQ"]
    for required in ["SPY", "QQQ"]:
        if required not in symbols:
            symbols.append(required)
    # Add in-play symbols not already in static list
    static_set = set(symbols)
    for ip_sym in _in_play_symbols:
        if ip_sym not in static_set and ip_sym not in ("SPY", "QQQ"):
            symbols.append(ip_sym)

    ib = IB()
    _ib = ib
    log.info(f"Connecting to IBKR at {host}:{port}...")
    try:
        ib.connect(host, port, clientId=client_id)
    except Exception as e:
        log.error(f"IBKR connection failed: {e}")
        with _lock:
            _status["connected"] = False
            _status["mode"] = "OFFLINE"
        _broadcast_sse("status", _status)
        return
    log.info("Connected.")

    with _lock:
        _status["connected"] = True
        _status["mode"] = "LIVE"
    _broadcast_sse("status", _status)

    _order_manager = OrderManager(ib, _broadcast_sse)
    log.info(f"Order manager ready (port {port}{'— PAPER' if port == 7497 else ''})")

    # SPY/QQQ market context
    log.info("Setting up market context (SPY + QQQ)...")
    spy_tracker = MarketBarTracker("SPY", ib, cfg)
    qqq_tracker = MarketBarTracker("QQQ", ib, cfg)
    try:
        spy_tracker.setup()
        _spy_snap = spy_tracker.snapshot
        _spy_engine = spy_tracker.market_engine
    except Exception as e:
        log.error(f"[SPY] Market tracker failed: {e}")
    try:
        qqq_tracker.setup()
        _qqq_snap = qqq_tracker.snapshot
        _qqq_engine = qqq_tracker.market_engine
    except Exception as e:
        log.error(f"[QQQ] Market tracker failed: {e}")

    # Regime hooks
    _orig_spy_on_tick = spy_tracker._on_tick
    def _spy_tick_with_regime(ticker):
        global _spy_snap
        _orig_spy_on_tick(ticker)
        _spy_snap = spy_tracker.snapshot
        with _lock:
            _status["regime"] = _get_regime_label()
            regime = _status["regime"]
            if regime.startswith("RED+TREND"):
                _status["active_books"] = "SHORT only (BDR)"
                _status["long_active"] = False
                _status["short_active"] = True
            elif regime.startswith("RED"):
                _status["active_books"] = "NONE (RED+CHOPPY)"
                _status["long_active"] = False
                _status["short_active"] = False
            else:
                _status["active_books"] = "LONG only (VK+SC)"
                _status["long_active"] = True
                _status["short_active"] = False
        _broadcast_sse("status", _status)
    spy_tracker._on_tick = _spy_tick_with_regime

    _orig_qqq_on_tick = qqq_tracker._on_tick
    def _qqq_tick_with_snap(ticker):
        global _qqq_snap
        _orig_qqq_on_tick(ticker)
        _qqq_snap = qqq_tracker.snapshot
    qqq_tracker._on_tick = _qqq_tick_with_snap

    try:
        spy_tracker.subscribe()
        log.info("[SPY] Market context subscribed")
    except Exception as e:
        log.error(f"[SPY] Subscribe failed: {e}")
    try:
        qqq_tracker.subscribe()
        log.info("[QQQ] Market context subscribed")
    except Exception as e:
        log.error(f"[QQQ] Subscribe failed: {e}")
    _spy_runner = spy_tracker
    _qqq_runner = qqq_tracker

    log.info(f"Regime: {_get_regime_label()}")
    ib.sleep(2.0)

    # Symbol runner setup with pacing
    SETUP_PACE_CACHED = 0.3
    SETUP_PACE_FULL = 1.5
    SUB_PACE = 0.2

    cached_set = set()
    for sym in symbols:
        csv_path = _CACHE_DIR_1MIN / f"{sym}_1min.csv"
        if csv_path.exists():
            cached_set.add(sym)
    n_cached = len(cached_set)
    n_new = len(symbols) - n_cached
    est_time = n_cached * SETUP_PACE_CACHED + n_new * SETUP_PACE_FULL
    log.info(f"Starting setup: {n_cached} cached + {n_new} new symbols "
             f"(est. {est_time:.0f}s)")

    for i, symbol in enumerate(symbols):
        try:
            universe = _get_universe(symbol)
            runner = SymbolRunner(symbol, ib, cfg, universe=universe)
            runner.setup()
            _runners.append(runner)
            with _lock:
                # Preserve existing universe tag from deferred mode if present
                existing = _status.get("symbols", {}).get(symbol)
                if existing and existing.get("universe"):
                    universe = existing["universe"]
                _status["symbols"][symbol] = {
                    "bars": getattr(runner, '_bar_count', 0),
                    "alerts": 0, "last_price": 0, "universe": universe,
                }
            if (i + 1) % 10 == 0:
                _broadcast_sse("status", _status)
        except Exception as e:
            log.error(f"[{symbol}] Failed to set up: {e}")
        pace = SETUP_PACE_CACHED if symbol in cached_set else SETUP_PACE_FULL
        ib.sleep(pace)

    log.info(f"All {len(_runners)} runners set up. Subscribing...")

    for runner in _runners:
        try:
            runner.subscribe()
        except Exception as e:
            log.error(f"[{runner.symbol}] Failed to subscribe: {e}")
        ib.sleep(SUB_PACE)

    _main_loop = asyncio.get_event_loop()
    log.info("Main event loop captured.")
    log.info(f"LIVE: monitoring {len(_runners)} symbols. "
             f"Dashboard: http://localhost:{DASHBOARD_PORT}")
    _broadcast_sse("status", _status)

    try:
        ib.run()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        if ib.isConnected():
            ib.disconnect()
        log.info("Disconnected.")
        _print_session_report()


# ── Schedule IB work on the main event loop ──

def _schedule_on_main(coro_func):
    """Run a coroutine on the main asyncio loop from any thread.

    ib_insync requires all IB API calls to happen on the thread that owns the
    event loop (the main thread running ib.run()).  HTTP handler threads must
    use this helper to schedule setup/subscribe work instead of spawning bare
    threads.
    """
    if _main_loop is None:
        raise RuntimeError("Main event loop not initialised yet")
    asyncio.run_coroutine_threadsafe(coro_func(), _main_loop)


# ── Watchlist persistence ──
WATCHLIST_FILE = Path(__file__).parent / "watchlist.txt"

def _load_watchlist() -> List[str]:
    """Load symbols from watchlist.txt, one per line."""
    if WATCHLIST_FILE.exists():
        lines = WATCHLIST_FILE.read_text().strip().split("\n")
        return [s.strip().upper() for s in lines if s.strip() and not s.strip().startswith("#")]
    return []

def _save_watchlist():
    """Persist ONLY static universe symbols to watchlist.txt.

    Excludes in-play-only symbols so they don't get promoted to static
    on restart.  Symbols tagged as 'BOTH' are included (they're in both).
    """
    with _lock:
        symbols = sorted(
            sym for sym, info in _status.get("symbols", {}).items()
            if isinstance(info, dict) and info.get("universe") != "IN_PLAY"
        )
    WATCHLIST_FILE.write_text("\n".join(symbols) + "\n")

# ── In-Play universe persistence ──
IN_PLAY_FILE = Path(__file__).parent / "in_play.txt"
IN_PLAY_HISTORY_FILE = Path(__file__).parent / "in_play_history.csv"
IN_PLAY_SNAPSHOT_DIR = Path(__file__).parent / "in_play_snapshots"
_in_play_symbols: List[str] = []

def _load_in_play() -> List[str]:
    """Load daily in-play symbols from in_play.txt."""
    if IN_PLAY_FILE.exists():
        lines = IN_PLAY_FILE.read_text().strip().split("\n")
        return [s.strip().upper() for s in lines if s.strip() and not s.strip().startswith("#")]
    return []

def _save_in_play():
    """Persist in-play list to in_play.txt."""
    with _lock:
        symbols = sorted(_in_play_symbols)
    IN_PLAY_FILE.write_text("\n".join(symbols) + "\n")

def _refresh_ipl_in_status():
    """No-op — in-play log is read from disk on demand via /api/iplog endpoint."""
    pass


def _log_in_play_event(symbol: str, action: str):
    """Append to in_play_history.csv — the permanent audit trail.

    Format: date,time,symbol,action
    Actions: ADD, REMOVE, CLEAR
    """
    now = datetime.now(EASTERN)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    header_needed = not IN_PLAY_HISTORY_FILE.exists() or IN_PLAY_HISTORY_FILE.stat().st_size == 0
    with open(IN_PLAY_HISTORY_FILE, "a") as f:
        if header_needed:
            f.write("date,time,symbol,action\n")
        f.write(f"{date_str},{time_str},{symbol},{action}\n")
    _refresh_ipl_in_status()

def _save_in_play_snapshot():
    """Save today's in-play list as a dated snapshot file.

    File: in_play_snapshots/YYYY-MM-DD.txt
    One symbol per line. Overwrites if called multiple times per day.
    Used by habitat_validation_study.py to reconstruct historical in-play sets.
    """
    IN_PLAY_SNAPSHOT_DIR.mkdir(exist_ok=True)
    now = datetime.now(EASTERN)
    date_str = now.strftime("%Y-%m-%d")
    snapshot_file = IN_PLAY_SNAPSHOT_DIR / f"{date_str}.txt"
    with _lock:
        symbols = sorted(_in_play_symbols)
    snapshot_file.write_text("\n".join(symbols) + "\n" if symbols else "")
    log.info(f"In-play snapshot saved: {snapshot_file.name} ({len(symbols)} symbols)")

def _get_universe(symbol: str) -> str:
    """Determine universe source for a symbol."""
    with _lock:
        in_static = symbol in _status.get("symbols", {})
        in_play = symbol in _in_play_symbols
    if in_static and in_play:
        return "BOTH"
    elif in_play:
        return "IN_PLAY"
    return "STATIC"

_IBKR_BAR_SIZE_MAP = {
    1: "1 min", 2: "2 mins", 3: "3 mins", 5: "5 mins",
    10: "10 mins", 15: "15 mins", 20: "20 mins", 30: "30 mins", 60: "1 hour",
}

# ── Safe price extraction from IBKR Ticker objects ──

def _safe_price(ticker) -> Optional[float]:
    """Extract a valid numeric price from an IBKR Ticker, or None.

    Tries ticker.last → ticker.marketPrice() → ticker.bid/ask midpoint.
    Guards against NaN, None, non-numeric (e.g. bound methods), and ≤ 0.
    Note: ticker.close is a METHOD on ib_insync Ticker — never use getattr
    for 'close' directly as it returns a bound method, not a float.
    """
    for attr in ('last',):
        val = getattr(ticker, attr, None)
        if isinstance(val, (int, float)) and val == val and val > 0:  # numeric, not NaN, positive
            return float(val)
    # marketPrice() is a method that returns a computed float
    try:
        val = ticker.marketPrice()
        if isinstance(val, (int, float)) and val == val and val > 0:
            return float(val)
    except Exception:
        pass
    # Last resort: bid/ask midpoint
    bid = getattr(ticker, 'bid', None)
    ask = getattr(ticker, 'ask', None)
    if (isinstance(bid, (int, float)) and bid == bid and bid > 0 and
            isinstance(ask, (int, float)) and ask == ask and ask > 0):
        return float((bid + ask) / 2.0)
    return None


# ── Bar Upsampler: builds higher-timeframe bars from completed lower-TF bars ──

class BarUpsampler:
    """Aggregates completed 1-min Bar objects into 5-min (or any N-min) bars.

    Unlike BarAggregator (which works from raw ticks), this takes completed
    Bar objects and groups them by time boundary.

    Usage:
        upsampler = BarUpsampler(target_minutes=5)
        for each completed_1min_bar:
            bar_5min = upsampler.on_bar(completed_1min_bar)
            if bar_5min is not None:
                # A complete 5-min bar was emitted
    """

    def __init__(self, target_minutes: int = 5):
        self.interval = target_minutes
        self._buf: List[Bar] = []
        self._boundary: Optional[datetime] = None

    def _bar_boundary(self, ts: datetime) -> datetime:
        """Round down to the nearest target-minute boundary."""
        minute = (ts.minute // self.interval) * self.interval
        return ts.replace(minute=minute, second=0, microsecond=0)

    def on_bar(self, bar: Bar) -> Optional[Bar]:
        """Process a completed lower-timeframe bar.

        Returns a completed higher-TF bar when a boundary is crossed, else None.
        """
        boundary = self._bar_boundary(bar.timestamp)

        # First bar ever
        if self._boundary is None:
            self._boundary = boundary
            self._buf = [bar]
            return None

        # Same bucket — accumulate
        if boundary == self._boundary:
            self._buf.append(bar)
            return None

        # New boundary — emit the completed bar from buffer, then start fresh
        completed = self._emit()
        self._boundary = boundary
        self._buf = [bar]
        return completed

    def _emit(self) -> Optional[Bar]:
        """Build a single higher-TF bar from accumulated buffer."""
        if not self._buf:
            return None
        return Bar(
            timestamp=self._boundary,
            open=self._buf[0].open,
            high=max(b.high for b in self._buf),
            low=min(b.low for b in self._buf),
            close=self._buf[-1].close,
            volume=sum(b.volume for b in self._buf),
        )

    def flush(self) -> Optional[Bar]:
        """Force-emit the current forming bar (e.g., at EOD)."""
        if not self._buf:
            return None
        completed = self._emit()
        self._buf = []
        self._boundary = None
        return completed


# ── Bar Aggregator: builds OHLCV bars from reqMktData streaming ticks ──

class BarAggregator:
    """Accumulates streaming tick data into fixed-interval OHLCV bars.

    Used with reqMktData to replace keepUpToDate subscriptions.
    reqMktData supports 100+ concurrent streams (vs keepUpToDate's ~50 limit).
    """

    def __init__(self, bar_interval_minutes: int = 5):
        self.interval = bar_interval_minutes
        self._bar_open: Optional[float] = None
        self._bar_high: float = 0.0
        self._bar_low: float = float('inf')
        self._bar_close: float = 0.0
        self._bar_vol_start: float = 0.0   # cumulative vol at bar start
        self._bar_start: Optional[datetime] = None
        self._last_cum_vol: float = 0.0     # track cumulative volume
        self._tick_count: int = 0

    def _bar_boundary(self, ts: datetime) -> datetime:
        """Round down to the nearest bar boundary."""
        minute = (ts.minute // self.interval) * self.interval
        return ts.replace(minute=minute, second=0, microsecond=0)

    def on_tick(self, price: float, cum_volume: float, ts: datetime) -> Optional[Bar]:
        """Process a tick. Returns a completed Bar if a boundary was crossed, else None.

        Args:
            price: last trade price
            cum_volume: cumulative day volume from IBKR Ticker
            ts: current timestamp (Eastern)

        Returns:
            Completed Bar if the bar boundary was crossed, else None.
            The forming bar is always updated regardless.
        """
        if price <= 0 or price != price:  # guard against NaN (NaN != NaN)
            return None

        boundary = self._bar_boundary(ts)
        completed_bar = None

        # First tick ever — initialize
        if self._bar_start is None:
            self._bar_start = boundary
            self._bar_open = price
            self._bar_high = price
            self._bar_low = price
            self._bar_close = price
            self._bar_vol_start = cum_volume
            self._last_cum_vol = cum_volume
            self._tick_count = 1
            return None

        # Check if we crossed into a new bar
        if boundary > self._bar_start:
            # Close out the previous bar
            bar_volume = self._last_cum_vol - self._bar_vol_start
            completed_bar = Bar(
                timestamp=self._bar_start,
                open=self._bar_open,
                high=self._bar_high,
                low=self._bar_low,
                close=self._bar_close,
                volume=max(0, bar_volume),
            )
            # Start new bar
            self._bar_start = boundary
            self._bar_open = price
            self._bar_high = price
            self._bar_low = price
            self._bar_close = price
            self._bar_vol_start = cum_volume
            self._tick_count = 1
        else:
            # Same bar — update OHLC
            self._bar_high = max(self._bar_high, price)
            self._bar_low = min(self._bar_low, price)
            self._bar_close = price
            self._tick_count += 1

        self._last_cum_vol = cum_volume
        return completed_bar

    @property
    def forming_bar(self) -> Optional[dict]:
        """Return the current forming (incomplete) bar as a dict."""
        if self._bar_start is None:
            return None
        return {
            "timestamp": self._bar_start,
            "open": self._bar_open,
            "high": self._bar_high,
            "low": self._bar_low,
            "close": self._bar_close,
            "volume": max(0, self._last_cum_vol - self._bar_vol_start),
            "ticks": self._tick_count,
        }


def _broadcast_sse(event_type: str, data: dict):
    """Send an SSE event to all connected dashboard clients."""
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with _lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


_alert_counter = 0

def _classify_book(signal: Signal) -> str:
    """Classify a signal into Portfolio C book: LONG or SHORT."""
    if signal.direction == 1:
        return "LONG"
    # Short signals in Portfolio C are BDR_SHORT only
    return "SHORT"

def _get_regime_label() -> str:
    """Get current SPY regime label from market context."""
    global _spy_snap
    import math
    if _spy_snap is None:
        return "UNKNOWN"
    # Regime only needs pct_from_open + day_high/low — not EMA readiness
    if math.isnan(_spy_snap.pct_from_open):
        return "UNKNOWN"
    pct = _spy_snap.pct_from_open
    if pct > 0.05:
        direction = "GREEN"
    elif pct < -0.05:
        direction = "RED"
    else:
        direction = "FLAT"
    # Character: TREND vs CHOPPY
    dh = _spy_snap.day_high if not math.isnan(_spy_snap.day_high) else 0
    dl = _spy_snap.day_low if not math.isnan(_spy_snap.day_low) else 0
    day_range = dh - dl
    if day_range > 0:
        close_pos = (_spy_snap.close - dl) / day_range
        if direction == "RED":
            character = "TREND" if close_pos <= 0.25 else "CHOPPY"
        elif direction == "GREEN":
            character = "TREND" if close_pos >= 0.75 else "CHOPPY"
        else:
            character = "CHOPPY"
    else:
        character = "UNKNOWN"
    return f"{direction}+{character}"


def _signal_to_dict(signal: Signal, symbol: str) -> dict:
    """Convert a Signal to a JSON-serializable dict."""
    global _alert_counter
    _alert_counter += 1
    ts_str = signal.timestamp.strftime("%H%M%S") if signal.timestamp else "000000"
    book = _classify_book(signal)
    regime = _get_regime_label()
    return {
        "id": f"real-{ts_str}-{_alert_counter}",
        "test": False,
        "symbol": symbol,
        "timestamp": signal.timestamp.strftime("%H:%M:%S") if signal.timestamp else "",
        "direction": "LONG" if signal.direction == 1 else "SHORT",
        "setup": signal.setup_name,
        "family": signal.family.name,
        "entry": round(signal.entry_price, 2),
        "stop": round(signal.stop_price, 2),
        "target": round(signal.target_price, 2),
        "risk": round(signal.risk, 4),
        "reward": round(signal.reward, 4),
        "rr": round(signal.rr_ratio, 2),
        "quality": signal.quality_score,
        "confluence": signal.confluence_tags,
        "sweeps": signal.sweep_tags,
        "book": book,
        "regime": regime,
        "universe": signal.universe,
    }


# ── Strategy name → display metadata mapping ──
_STRATEGY_SETUP_MAP = {
    "SC_SNIPER":       ("SECOND_CHANCE", "BREAKOUT"),
    "FL_ANTICHOP":     ("FASHIONABLY_LATE", "MOMENTUM"),
    "SP_ATIER":        ("SPENCER", "BREAKOUT"),
    "HH_QUALITY":      ("HITCHHIKER", "CONSOL_BREAK"),
    "EMA_FPIP":        ("EMA_FPIP", "TREND"),
    "BDR_SHORT":       ("BDR_SHORT", "SHORT_STRUCT"),
    "EMA9_FT":         ("EMA9_FIRST_TOUCH", "TREND"),
    "BS_STRUCT":       ("BACKSIDE_STRUCT", "CONSOL_BREAK"),
    "ORL_FBD_LONG":    ("ORL_FAILED_BD", "FAILED_MOVE"),
    "ORH_FBO_V2_A":    ("ORH_FBO_RETEST", "FAILED_BO_SHORT"),
    "ORH_FBO_V2_B":    ("ORH_FBO_CONT", "FAILED_BO_SHORT"),
    "PDH_FBO_B":       ("PDH_FBO_CONT", "FAILED_BO_SHORT"),
    "FFT_NEWLOW_REV":  ("FFT_NEWLOW", "FAILED_MOVE"),
}


def _raw_signal_to_dict(sig: '_RawSignal', symbol: str, bar_ts, universe: str = "STATIC") -> dict:
    """Convert a RawSignal from StrategyManager to a JSON-serializable alert dict.

    This is the primary alert pipeline for the new incremental engine.
    Produces the same dict shape as _signal_to_dict() so the dashboard
    HTML, SSE stream, order panel, and CSV export all work unchanged.
    """
    global _alert_counter
    _alert_counter += 1

    ts_str = bar_ts.strftime("%H%M%S") if bar_ts else "000000"
    ts_display = bar_ts.strftime("%H:%M:%S") if bar_ts else ""

    setup_name, family_name = _STRATEGY_SETUP_MAP.get(
        sig.strategy_name, (sig.strategy_name, "UNKNOWN"))

    entry = sig.entry_price
    stop = sig.stop_price
    target = sig.target_price

    risk = abs(entry - stop)
    reward = abs(target - entry)
    rr = round(reward / risk, 2) if risk > 0 else 0.0

    book = "LONG" if sig.direction == 1 else "SHORT"
    regime = _get_regime_label()

    return {
        "id": f"real-{ts_str}-{_alert_counter}",
        "test": False,
        "symbol": symbol,
        "timestamp": ts_display,
        "direction": book,
        "setup": setup_name,
        "family": family_name,
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "risk": round(risk, 4),
        "reward": round(reward, 4),
        "rr": rr,
        "quality": sig.quality,
        "confluence": list(sig.metadata.get("confluence", [])),
        "sweeps": list(sig.metadata.get("sweeps", [])),
        "book": book,
        "regime": regime,
        "universe": universe,
    }


class MarketBarTracker:
    """Subscribes to SPY/QQQ via reqMktData and updates shared MarketEngine snapshots.

    Uses reqMktData (streaming Level 1 quotes) instead of keepUpToDate to avoid
    IBKR's concurrent historical-data subscription limit (~50 streams).

    Dual-timeframe architecture (matches SymbolRunner):
      - Builds 1-min bars from tick data using BarAggregator(1)
      - Upsamples 1-min bars to 5-min using BarUpsampler(5)
      - MarketEngine processes 5-min bars for regime/context calculations
      - Live price updates happen on every tick (regime stays responsive)
    """

    BASE_BAR_MINUTES = 1     # always pull 1-min from IBKR
    ENGINE_BAR_MINUTES = 5   # market engine expects 5-min bars

    def __init__(self, symbol: str, ib: IB, cfg: OverlayConfig):
        self.symbol = symbol
        self.ib = ib
        self.cfg = cfg
        self.market_engine = MarketEngine()
        self.contract = Stock(symbol, "SMART", "USD")
        self.bar_size_str = _IBKR_BAR_SIZE_MAP[self.ENGINE_BAR_MINUTES]
        # Tick → 1-min bars (base timeframe)
        self._aggregator = BarAggregator(self.BASE_BAR_MINUTES)
        # 1-min → 5-min upsampler (for market engine consumption)
        self._upsampler = BarUpsampler(self.ENGINE_BAR_MINUTES)
        self._ticker = None       # ib_insync Ticker from reqMktData
        self._subscription = None  # kept for compat with watchdog
        self.snapshot = None
        self._bars_completed = 0

    def setup(self):
        """Qualify contract and warm up with historical bars."""
        self.ib.qualifyContracts(self.contract)
        log.info(f"[MKT:{self.symbol}] Contract qualified")

        # Intraday catch-up: pull 1-min bars and aggregate to 5-min via upsampler
        intraday_bars = self.ib.reqHistoricalData(
            self.contract, endDateTime="", durationStr="1 D",
            barSizeSetting=_IBKR_BAR_SIZE_MAP[self.BASE_BAR_MINUTES],
            whatToShow="TRADES",
            useRTH=True, formatDate=1,
        )
        bars_1min_count = 0
        if intraday_bars:
            for ib_bar in intraday_bars:
                bar_1min = self._convert_bar(ib_bar)
                bars_1min_count += 1

                bar_5min = self._upsampler.on_bar(bar_1min)
                if bar_5min is not None:
                    self.snapshot = self.market_engine.process_bar(bar_5min)
                    self._bars_completed += 1

            # Flush any remaining partial 5-min bar at end of catch-up
            final_5min = self._upsampler.flush()
            if final_5min is not None:
                self.snapshot = self.market_engine.process_bar(final_5min)
                self._bars_completed += 1

            log.info(f"[MKT:{self.symbol}] Caught up: {bars_1min_count} 1-min bars → "
                     f"{self._bars_completed} 5-min bars")

    def subscribe(self):
        """Start reqMktData streaming subscription."""
        self._ticker = self.ib.reqMktData(self.contract, genericTickList="", snapshot=False, regulatorySnapshot=False)
        self._ticker.updateEvent += self._on_tick
        self._subscription = self._ticker  # for watchdog compat
        log.info(f"[MKT:{self.symbol}] reqMktData subscribed for market context")

    def unsubscribe(self):
        """Cancel market data subscription."""
        if self._ticker is not None:
            try:
                self.ib.cancelMktData(self.contract)
            except Exception as e:
                log.warning(f"[MKT:{self.symbol}] Error cancelling mkt data: {e}")
            if self._ticker and hasattr(self._ticker, 'updateEvent'):
                self._ticker.updateEvent -= self._on_tick
            self._ticker = None
            self._subscription = None
        log.info(f"[MKT:{self.symbol}] Unsubscribed.")

    def _on_tick(self, ticker):
        """Called on every tick from reqMktData.

        Dual-timeframe flow:
          1. Tick → BarAggregator(1min) → completed 1-min bar
          2. 1-min bar → BarUpsampler(5min) → completed 5-min bar
          3. MarketEngine processes 5-min bars for regime calculations
          4. Live price always updates snapshot (regime stays responsive between bars)
        """
        price = _safe_price(ticker)
        if price is None:
            return

        # Record timestamp for stale-data watchdog
        _last_bar_timestamps[self.symbol] = time.monotonic()

        ts = datetime.now(EASTERN)
        cum_vol = getattr(ticker, 'volume', 0) or 0

        # Always update snapshot with live price (regime stays responsive)
        if self.snapshot is not None:
            self.snapshot.close = price
            if hasattr(self.snapshot, 'day_high') and price > self.snapshot.day_high:
                self.snapshot.day_high = price
            if hasattr(self.snapshot, 'day_low') and price < self.snapshot.day_low:
                self.snapshot.day_low = price
            if hasattr(self.snapshot, 'day_open') and self.snapshot.day_open > 0:
                self.snapshot.pct_from_open = (
                    (price - self.snapshot.day_open) / self.snapshot.day_open * 100.0
                )

        # Step 1: Tick → 1-min bar
        completed_1min = self._aggregator.on_tick(price, cum_vol, ts)
        if completed_1min is None:
            return  # still forming 1-min bar

        # Step 2: 1-min → 5-min upsampler
        completed_5min = self._upsampler.on_bar(completed_1min)
        if completed_5min is None:
            return  # 5-min bar not yet complete

        # Step 3: Process completed 5-min bar through market engine
        self.snapshot = self.market_engine.process_bar(completed_5min)
        self._bars_completed += 1
        log.info(f"[MKT:{self.symbol}] Bar #{self._bars_completed} closed "
                 f"@ {completed_5min.timestamp.strftime('%H:%M')} C={completed_5min.close:.2f}")

    def _convert_bar(self, ib_bar) -> Bar:
        if hasattr(ib_bar, 'date'):
            if isinstance(ib_bar.date, str):
                try:
                    ts = datetime.strptime(ib_bar.date, "%Y%m%d  %H:%M:%S")
                except ValueError:
                    ts = datetime.strptime(ib_bar.date, "%Y%m%d")
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=EASTERN)
            else:
                if ib_bar.date.tzinfo is not None:
                    ts = ib_bar.date.astimezone(EASTERN)
                else:
                    ts = ib_bar.date.replace(tzinfo=EASTERN)
        else:
            ts = datetime.now(EASTERN)
        return Bar(
            timestamp=ts, open=ib_bar.open, high=ib_bar.high,
            low=ib_bar.low, close=ib_bar.close,
            volume=ib_bar.volume if hasattr(ib_bar, 'volume') else 0,
        )


# ── Local bar cache: load 1-min bars from disk to skip IBKR warmup ──
_CACHE_DIR_1MIN = Path(__file__).parent / "data" / "1min"
_CACHE_DIR_5MIN = Path(__file__).parent / "data"

def _load_cached_1min_bars(symbol: str, max_days: int = 25) -> List[Bar]:
    """Load recent 1-min bars from local CSV cache.

    Returns up to `max_days` trading days of 1-min bars (most recent),
    or empty list if no cache exists.  25 days > 20 trading days ensures
    the 20-session in-play baseline buffer is fully filled.
    """
    csv_path = _CACHE_DIR_1MIN / f"{symbol}_1min.csv"
    if not csv_path.exists():
        return []

    import csv as _csv_mod
    bars: List[Bar] = []
    try:
        with open(csv_path, "r") as f:
            reader = _csv_mod.DictReader(f)
            for row in reader:
                try:
                    ts = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S")
                    ts = ts.replace(tzinfo=EASTERN)
                    bars.append(Bar(
                        timestamp=ts,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(float(row["volume"])),
                    ))
                except (ValueError, KeyError):
                    continue
    except Exception as e:
        log.warning(f"[{symbol}] Failed to read cache {csv_path}: {e}")
        return []

    if not bars:
        return []

    # ── RTH filter: keep only regular-trading-hours bars (9:30–15:59 ET) ──
    # Non-RTH bars (premarket, after-hours) contaminate day_open, prev_close,
    # pct_from_open, RS, regime, gap, and in-play calculations.
    pre_filter = len(bars)
    bars = [b for b in bars if 930 <= (b.timestamp.hour * 100 + b.timestamp.minute) <= 1559]
    if len(bars) < pre_filter:
        log.info(f"[{symbol}] RTH filter: dropped {pre_filter - len(bars)} non-RTH bars from cache")

    if not bars:
        return []

    # Keep only the last max_days trading days
    last_date = bars[-1].timestamp.date()
    unique_dates = sorted(set(b.timestamp.date() for b in bars))
    if len(unique_dates) > max_days:
        cutoff_date = unique_dates[-max_days]
        bars = [b for b in bars if b.timestamp.date() >= cutoff_date]

    return bars


def _append_bars_to_cache(symbol: str, bars: List[Bar]):
    """Append new 1-min bars to the local CSV cache (deduped by timestamp)."""
    csv_path = _CACHE_DIR_1MIN / f"{symbol}_1min.csv"
    _CACHE_DIR_1MIN.mkdir(parents=True, exist_ok=True)

    # Load existing timestamps for dedup
    existing_ts = set()
    if csv_path.exists():
        try:
            with open(csv_path, "r") as f:
                import csv as _csv_mod
                reader = _csv_mod.DictReader(f)
                for row in reader:
                    existing_ts.add(row["datetime"])
        except Exception:
            pass

    new_bars = []
    for b in bars:
        ts_str = b.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        if ts_str not in existing_ts:
            new_bars.append(b)

    if not new_bars:
        return

    write_header = not csv_path.exists() or csv_path.stat().st_size == 0
    try:
        with open(csv_path, "a", newline="") as f:
            import csv as _csv_mod
            writer = _csv_mod.writer(f)
            if write_header:
                writer.writerow(["datetime", "open", "high", "low", "close", "volume"])
            for b in new_bars:
                writer.writerow([
                    b.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{b.open:.4f}", f"{b.high:.4f}", f"{b.low:.4f}", f"{b.close:.4f}",
                    b.volume,
                ])
        log.info(f"[{symbol}] Cached {len(new_bars)} new 1-min bars to disk")
    except Exception as e:
        log.warning(f"[{symbol}] Failed to write cache: {e}")


class SymbolRunner:
    """Manages one symbol's streaming subscription and signal engine.

    Uses reqMktData (Level 1 quotes) instead of keepUpToDate to avoid
    IBKR's concurrent historical-data subscription limit (~50 streams).

    Dual-timeframe architecture:
      - Builds 1-min bars from tick data using BarAggregator(1)
      - Upsamples 1-min bars to 5-min using BarUpsampler(5)
      - Engine processes 5-min bars for existing strategies (SC, FL, SP, HH, etc.)
      - 1-min bars available for future 1-min strategies (EMA9_FT, BS_STRUCT)

    The base bar interval is always 1-min for maximum resolution.
    The engine's bar_interval_minutes stays at 5 so all existing indicator
    calculations (ATR, EMA, VWAP on 5-min) remain unchanged.
    """

    BASE_BAR_MINUTES = 1     # always pull 1-min from IBKR
    ENGINE_BAR_MINUTES = 5   # engine expects 5-min bars

    def __init__(self, symbol: str, ib: IB, cfg: OverlayConfig, universe: str = "STATIC"):
        self.symbol = symbol
        self.universe = universe
        self.ib = ib
        self.cfg = cfg
        self.contract = Stock(symbol, "SMART", "USD")
        self.bar_size_str = _IBKR_BAR_SIZE_MAP[self.ENGINE_BAR_MINUTES]
        # Tick → 1-min bars (base timeframe)
        self._aggregator = BarAggregator(self.BASE_BAR_MINUTES)
        # 1-min → 5-min upsampler (for engine consumption)
        self._upsampler = BarUpsampler(self.ENGINE_BAR_MINUTES)
        self._ticker = None       # ib_insync Ticker from reqMktData
        self._bars_received = 0
        self._tick_count = 0      # total ticks received (for diagnostics)
        self._subscription = None  # kept for compat with watchdog
        self._removed = False  # kill switch: stops callbacks from firing after removal
        self._day_open_for_rs: float = float('nan')
        self._rs_date: Optional[int] = None

        # ── In-play state (per symbol-day) ──
        self._ip_result: Optional[_InPlayResult] = None   # current day's evaluation
        self._ip_eval_date: Optional[int] = None          # YYYYMMDD of last eval

        # ── Primary live strategy engine (incremental step() architecture) ──
        # Strategies disabled by live-path replay:
        #   HH_QUALITY:      PF=0.61, -20.8R (N=159)
        #   SC_SNIPER:       PF=0.47, -13.9R (N=37)
        #   ORL_FBD_LONG:    PF=0.00, -3.0R  (N=15)
        #   FFT_NEWLOW_REV:  PF=0.84, -2.3R  (N=25) — train/test overfit (1.05/0.55)
        # Set ALERT_ENABLE_ALL_STRATS=1 to re-enable them for testing.
        _enable_all = os.environ.get("ALERT_ENABLE_ALL_STRATS", "").strip() in ("1", "true", "yes")
        strat_cfg = _StrategyConfig(timeframe_min=5)
        live_strats = [
            # _SCSniperLive(strat_cfg),          # DISABLED: PF=0.47 live-path
            # _FLAntiChopLive(strat_cfg),  # DEMOTED 2026-03-17: PF=0.53, wrong stock type
            _SpencerATierLive(strat_cfg),
            _HitchHikerLive(strat_cfg),          # PROMOTED: PF=2.09, N=85, TotalR=+25.9 (replay dual-tf)
            _EmaFpipLive(strat_cfg),
            _BDRShortLive(strat_cfg),
            _EMA9FirstTouchLive(strat_cfg),
            _BacksideStructureLive(strat_cfg),
            # _ORLFBDLongLive(strat_cfg),        # DISABLED: PF=0.00 live-path
            _ORHFBOShortV2Live(strat_cfg),
            _PDHFBOShortLive(strat_cfg, enable_mode_a=False, enable_mode_b=True),
            # _FFTNewlowReversalLive(strat_cfg), # DISABLED: PF=0.84, train/test overfit
        ]
        if _enable_all:
            live_strats.extend([
                _SCSniperLive(strat_cfg),
                _ORLFBDLongLive(strat_cfg),
                _FFTNewlowReversalLive(strat_cfg),
            ])
            log.info(f"[{symbol}] ALL strategies enabled (ALERT_ENABLE_ALL_STRATS=1)")
        self._strategy_mgr = _StrategyManager(strategies=live_strats, symbol=symbol, config=strat_cfg)

        # ── Legacy engine (emergency rollback only, off by default) ──
        self._legacy_engine = None
        if _USE_LEGACY_ENGINE:
            self._legacy_engine = SignalEngine(cfg, universe_source=universe)
            log.warning(f"[{symbol}] LEGACY ENGINE ACTIVE (ALERT_LEGACY_ENGINE=1)")

    def setup(self):
        """Qualify contract and warm up strategy engine."""
        self.ib.qualifyContracts(self.contract)
        log.info(f"[{self.symbol}] Contract qualified: {self.contract}")

        # Daily bars for ATR warm-up
        daily_bars = self.ib.reqHistoricalData(
            self.contract, endDateTime="", durationStr="30 D",
            barSizeSetting="1 day", whatToShow="TRADES",
            useRTH=True, formatDate=1,
        )
        if daily_bars:
            last_day = daily_bars[-1]
            log.info(f"[{self.symbol}] ATR warmed: {len(daily_bars)} days, "
                     f"PD H:{last_day.high:.2f} L:{last_day.low:.2f}")

            # Primary engine: warm up daily ATR + prior close for gap calc
            if len(daily_bars) >= 2:
                atr_val = sum(
                    db.high - db.low for db in daily_bars[-14:]
                ) / min(len(daily_bars), 14)
                # last_day may be today's partial bar during market hours,
                # so prior close = daily_bars[-2].close (yesterday's close)
                prior_close = daily_bars[-2].close
                self._strategy_mgr.indicators.warm_up_daily(
                    atr_val, last_day.high, last_day.low,
                    prior_close=prior_close)
                log.info(f"[{self.symbol}] Prior close from daily bars: "
                         f"{prior_close:.2f}")

            # Legacy engine warm-up (only if rollback flag is set)
            if self._legacy_engine is not None:
                daily_history = [
                    {"high": b.high, "low": b.low, "close": b.close}
                    for b in daily_bars
                ]
                self._legacy_engine.set_daily_atr_history(daily_history)
                self._legacy_engine.set_prior_day(last_day.high, last_day.low)

        # ── Intraday catch-up: cache-first, then IBKR delta ──
        # Try local CSV cache first (instant), only fetch delta from IBKR.
        # For new symbols with no cache, fall back to full 20D IBKR pull.
        cached_bars = _load_cached_1min_bars(self.symbol, max_days=25)
        ibkr_delta_bars: List[Bar] = []

        if cached_bars:
            # Determine how stale the cache is
            last_cached_ts = cached_bars[-1].timestamp
            now_et = datetime.now(EASTERN)
            staleness_hours = (now_et - last_cached_ts).total_seconds() / 3600

            if staleness_hours > 1.0:
                # Fetch only the delta from IBKR (since last cached bar)
                # Use "2 D" to cover weekend gaps safely
                delta_duration = "2 D" if staleness_hours < 72 else "5 D"
                try:
                    ibkr_delta = self.ib.reqHistoricalData(
                        self.contract, endDateTime="", durationStr=delta_duration,
                        barSizeSetting=_IBKR_BAR_SIZE_MAP[self.BASE_BAR_MINUTES],
                        whatToShow="TRADES",
                        useRTH=True, formatDate=1,
                    )
                    if ibkr_delta:
                        for ib_bar in ibkr_delta:
                            bar = self._convert_bar(ib_bar)
                            if bar.timestamp > last_cached_ts:
                                ibkr_delta_bars.append(bar)
                        log.info(f"[{self.symbol}] Cache hit: {len(cached_bars)} cached + "
                                 f"{len(ibkr_delta_bars)} delta bars "
                                 f"(stale {staleness_hours:.1f}h, fetched {delta_duration})")
                except Exception as e:
                    log.warning(f"[{self.symbol}] Delta fetch failed ({e}), using cache only")
            else:
                log.info(f"[{self.symbol}] Cache hit: {len(cached_bars)} bars, fresh "
                         f"({staleness_hours:.1f}h old)")

            all_1min_bars = cached_bars + ibkr_delta_bars
        else:
            # No cache — full 20D pull from IBKR (slow path, only for new symbols)
            log.info(f"[{self.symbol}] No local cache — full 20D IBKR pull (new symbol)")
            ibkr_full = self.ib.reqHistoricalData(
                self.contract, endDateTime="", durationStr="20 D",
                barSizeSetting=_IBKR_BAR_SIZE_MAP[self.BASE_BAR_MINUTES],
                whatToShow="TRADES",
                useRTH=True, formatDate=1,
            )
            all_1min_bars = []
            if ibkr_full:
                all_1min_bars = [self._convert_bar(ib_bar) for ib_bar in ibkr_full]

        # ── Seed 5-min indicators from prior-day bars (fast warmup) ──
        # The catch-up loop below also warms indicators by feeding all bars,
        # but seeding first guarantees 5-min EMAs/ATR/vol are ready even if
        # the catch-up is interrupted or the cache is sparse.
        if all_1min_bars:
            today = all_1min_bars[-1].timestamp.date()
            prior_day_bars = [b for b in all_1min_bars if b.timestamp.date() < today]
            if prior_day_bars:
                # Upsample prior-day 1-min bars to 5-min for seeding
                from .strategies.replay import BarUpsampler as _SeedUpsampler
                _seed_up = _SeedUpsampler(5)
                seed_5m = []
                for b in prior_day_bars:
                    b5 = _seed_up.on_bar(b)
                    if b5 is not None:
                        seed_5m.append(b5)
                if seed_5m:
                    self._strategy_mgr.indicators.seed_5min(seed_5m)
                    log.info(f"[{self.symbol}] Seeded 5-min indicators: "
                             f"{len(seed_5m)} bars from prior days")

        # Feed all bars through indicators + strategies (same as before)
        bars_1min_count = 0
        if all_1min_bars:
            for bar_1min in all_1min_bars:
                bars_1min_count += 1

                # Feed 1-min bar to 1-min strategies (EMA9_FT etc.)
                market_ctx_1m = self._build_market_ctx(bar_1min)
                self._strategy_mgr.on_1min_bar(bar_1min, market_ctx=market_ctx_1m)

                # Upsample to 5-min and feed to 5-min strategies
                bar_5min = self._upsampler.on_bar(bar_1min)
                if bar_5min is not None:
                    market_ctx = self._build_market_ctx(bar_5min)
                    self._strategy_mgr.on_5min_bar(bar_5min, market_ctx=market_ctx)
                    if self._legacy_engine is not None:
                        self._legacy_engine.process_bar(bar_5min, market_ctx=market_ctx)
                    self._bars_received += 1

            # Flush any remaining partial 5-min bar at end of catch-up
            final_5min = self._upsampler.flush()
            if final_5min is not None:
                market_ctx = self._build_market_ctx(final_5min)
                self._strategy_mgr.on_5min_bar(final_5min, market_ctx=market_ctx)
                if self._legacy_engine is not None:
                    self._legacy_engine.process_bar(final_5min, market_ctx=market_ctx)
                self._bars_received += 1

            si = self._strategy_mgr.indicators
            ip_bl = si.get_in_play_baselines()

            # ── Safety net: if _ip_prev_close is still NaN after warmup,
            # find yesterday's close from the warmup bars themselves. ──
            if math.isnan(ip_bl['prev_close']) and len(all_1min_bars) >= 2:
                today = all_1min_bars[-1].timestamp.date()
                # Walk backwards to find the last bar from a PRIOR day
                for b in reversed(all_1min_bars):
                    if b.timestamp.date() < today:
                        si._ip_prev_close = b.close
                        si._prev_day_close = b.close
                        log.warning(f"[{self.symbol}] Safety net: set prev_close={b.close:.2f} "
                                    f"from {b.timestamp} (warmup bar fallback)")
                        ip_bl = si.get_in_play_baselines()  # re-read
                        break

            cache_status = "CACHED" if cached_bars else "IBKR"
            _pc = ip_bl['prev_close']
            _pc_str = f"Y({_pc:.2f})" if not math.isnan(_pc) else "N"
            log.info(f"[{self.symbol}] Warmed ({cache_status}): {bars_1min_count} 1-min → "
                     f"{self._bars_received} 5-min bars | "
                     f"E9_5m={'Y' if si.ema9_5m.ready else 'N'} "
                     f"E20_5m={'Y' if si.ema20_5m.ready else 'N'} "
                     f"E9_1m={'Y' if si.ema9_1m.ready else 'N'} "
                     f"E20_1m={'Y' if si.ema20_1m.ready else 'N'} | "
                     f"IP vol_depth={ip_bl['vol_baseline_depth']}/20 "
                     f"range_depth={ip_bl['range_baseline_depth']}/20 "
                     f"prev_close={_pc_str}")

            # Save delta bars to cache for next startup
            if ibkr_delta_bars:
                _append_bars_to_cache(self.symbol, ibkr_delta_bars)
            elif not cached_bars and all_1min_bars:
                # New symbol — save the full IBKR pull to cache
                _append_bars_to_cache(self.symbol, all_1min_bars)

        with _lock:
            _status["symbols"][self.symbol] = {
                "bars": self._bars_received,
                "alerts": 0,
                "last_price": all_1min_bars[-1].close if all_1min_bars else 0,
                "universe": self.universe,
            }
        _broadcast_sse("status", _status)

    def subscribe(self):
        """Start reqMktData streaming subscription (replaces keepUpToDate)."""
        self._ticker = self.ib.reqMktData(self.contract, genericTickList="", snapshot=False, regulatorySnapshot=False)
        self._ticker.updateEvent += self._on_tick
        self._subscription = self._ticker  # for watchdog compat
        log.info(f"[{self.symbol}] reqMktData subscribed. Waiting for ticks...")

    async def setup_async(self):
        """Async version of setup() — safe to call from the running event loop."""
        await self.ib.qualifyContractsAsync(self.contract)
        log.info(f"[{self.symbol}] Contract qualified: {self.contract}")

        daily_bars = await self.ib.reqHistoricalDataAsync(
            self.contract, endDateTime="", durationStr="30 D",
            barSizeSetting="1 day", whatToShow="TRADES",
            useRTH=True, formatDate=1,
        )
        if daily_bars:
            last_day = daily_bars[-1]
            log.info(f"[{self.symbol}] ATR warmed: {len(daily_bars)} days, "
                     f"PD H:{last_day.high:.2f} L:{last_day.low:.2f}")

            # Primary engine: warm up daily ATR + prior close for gap calc
            if len(daily_bars) >= 2:
                atr_val = sum(
                    db.high - db.low for db in daily_bars[-14:]
                ) / min(len(daily_bars), 14)
                # last_day may be today's partial bar during market hours,
                # so prior close = daily_bars[-2].close (yesterday's close)
                prior_close = daily_bars[-2].close
                self._strategy_mgr.indicators.warm_up_daily(
                    atr_val, last_day.high, last_day.low,
                    prior_close=prior_close)
                log.info(f"[{self.symbol}] Prior close from daily bars: "
                         f"{prior_close:.2f}")

            # Legacy engine warm-up (only if rollback flag is set)
            if self._legacy_engine is not None:
                daily_history = [
                    {"high": b.high, "low": b.low, "close": b.close}
                    for b in daily_bars
                ]
                self._legacy_engine.set_daily_atr_history(daily_history)
                self._legacy_engine.set_prior_day(last_day.high, last_day.low)

        # ── Intraday catch-up: cache-first, then IBKR delta (async) ──
        cached_bars = _load_cached_1min_bars(self.symbol, max_days=25)
        ibkr_delta_bars: List[Bar] = []

        if cached_bars:
            last_cached_ts = cached_bars[-1].timestamp
            now_et = datetime.now(EASTERN)
            staleness_hours = (now_et - last_cached_ts).total_seconds() / 3600

            if staleness_hours > 1.0:
                delta_duration = "2 D" if staleness_hours < 72 else "5 D"
                try:
                    ibkr_delta = await self.ib.reqHistoricalDataAsync(
                        self.contract, endDateTime="", durationStr=delta_duration,
                        barSizeSetting=_IBKR_BAR_SIZE_MAP[self.BASE_BAR_MINUTES],
                        whatToShow="TRADES",
                        useRTH=True, formatDate=1,
                    )
                    if ibkr_delta:
                        for ib_bar in ibkr_delta:
                            bar = self._convert_bar(ib_bar)
                            if bar.timestamp > last_cached_ts:
                                ibkr_delta_bars.append(bar)
                        log.info(f"[{self.symbol}] Cache hit: {len(cached_bars)} cached + "
                                 f"{len(ibkr_delta_bars)} delta bars "
                                 f"(stale {staleness_hours:.1f}h, fetched {delta_duration})")
                except Exception as e:
                    log.warning(f"[{self.symbol}] Delta fetch failed ({e}), using cache only")
            else:
                log.info(f"[{self.symbol}] Cache hit: {len(cached_bars)} bars, fresh "
                         f"({staleness_hours:.1f}h old)")

            all_1min_bars = cached_bars + ibkr_delta_bars
        else:
            log.info(f"[{self.symbol}] No local cache — full 20D IBKR pull (new symbol)")
            ibkr_full = await self.ib.reqHistoricalDataAsync(
                self.contract, endDateTime="", durationStr="20 D",
                barSizeSetting=_IBKR_BAR_SIZE_MAP[self.BASE_BAR_MINUTES],
                whatToShow="TRADES",
                useRTH=True, formatDate=1,
            )
            all_1min_bars = []
            if ibkr_full:
                all_1min_bars = [self._convert_bar(ib_bar) for ib_bar in ibkr_full]

        # ── Seed 5-min indicators from prior-day bars (async path) ──
        if all_1min_bars:
            today_async = all_1min_bars[-1].timestamp.date()
            prior_day_bars_async = [b for b in all_1min_bars if b.timestamp.date() < today_async]
            if prior_day_bars_async:
                from .strategies.replay import BarUpsampler as _SeedUpsampler
                _seed_up = _SeedUpsampler(5)
                seed_5m = []
                for b in prior_day_bars_async:
                    b5 = _seed_up.on_bar(b)
                    if b5 is not None:
                        seed_5m.append(b5)
                if seed_5m:
                    self._strategy_mgr.indicators.seed_5min(seed_5m)
                    log.info(f"[{self.symbol}] Seeded 5-min indicators: "
                             f"{len(seed_5m)} bars from prior days")

        bars_1min_count = 0
        if all_1min_bars:
            for bar_1min in all_1min_bars:
                bars_1min_count += 1
                market_ctx_1m = self._build_market_ctx(bar_1min)
                self._strategy_mgr.on_1min_bar(bar_1min, market_ctx=market_ctx_1m)
                bar_5min = self._upsampler.on_bar(bar_1min)
                if bar_5min is not None:
                    market_ctx = self._build_market_ctx(bar_5min)
                    self._strategy_mgr.on_5min_bar(bar_5min, market_ctx=market_ctx)
                    if self._legacy_engine is not None:
                        self._legacy_engine.process_bar(bar_5min, market_ctx=market_ctx)
                    self._bars_received += 1

            final_5min = self._upsampler.flush()
            if final_5min is not None:
                market_ctx = self._build_market_ctx(final_5min)
                self._strategy_mgr.on_5min_bar(final_5min, market_ctx=market_ctx)
                if self._legacy_engine is not None:
                    self._legacy_engine.process_bar(final_5min, market_ctx=market_ctx)
                self._bars_received += 1

            si = self._strategy_mgr.indicators
            ip_bl = si.get_in_play_baselines()

            # ── Safety net: if _ip_prev_close is still NaN after warmup,
            # find yesterday's close from the warmup bars themselves. ──
            if math.isnan(ip_bl['prev_close']) and len(all_1min_bars) >= 2:
                today = all_1min_bars[-1].timestamp.date()
                for b in reversed(all_1min_bars):
                    if b.timestamp.date() < today:
                        si._ip_prev_close = b.close
                        si._prev_day_close = b.close
                        log.warning(f"[{self.symbol}] Safety net: set prev_close={b.close:.2f} "
                                    f"from {b.timestamp} (warmup bar fallback)")
                        ip_bl = si.get_in_play_baselines()
                        break

            cache_status = "CACHED" if cached_bars else "IBKR"
            _pc = ip_bl['prev_close']
            _pc_str = f"Y({_pc:.2f})" if not math.isnan(_pc) else "N"
            log.info(f"[{self.symbol}] Warmed ({cache_status}): {bars_1min_count} 1-min → "
                     f"{self._bars_received} 5-min bars | "
                     f"E9_5m={'Y' if si.ema9_5m.ready else 'N'} "
                     f"E20_5m={'Y' if si.ema20_5m.ready else 'N'} "
                     f"E9_1m={'Y' if si.ema9_1m.ready else 'N'} "
                     f"E20_1m={'Y' if si.ema20_1m.ready else 'N'} | "
                     f"IP vol_depth={ip_bl['vol_baseline_depth']}/20 "
                     f"range_depth={ip_bl['range_baseline_depth']}/20 "
                     f"prev_close={_pc_str}")

            if ibkr_delta_bars:
                _append_bars_to_cache(self.symbol, ibkr_delta_bars)
            elif not cached_bars and all_1min_bars:
                _append_bars_to_cache(self.symbol, all_1min_bars)

        with _lock:
            _status["symbols"][self.symbol] = {
                "bars": self._bars_received,
                "alerts": 0,
                "last_price": all_1min_bars[-1].close if all_1min_bars else 0,
                "universe": self.universe,
            }
        _broadcast_sse("status", _status)

    async def subscribe_async(self):
        """Async version of subscribe() — reqMktData (no keepUpToDate needed)."""
        self._ticker = self.ib.reqMktData(self.contract, genericTickList="", snapshot=False, regulatorySnapshot=False)
        self._ticker.updateEvent += self._on_tick
        self._subscription = self._ticker  # for watchdog compat
        log.info(f"[{self.symbol}] reqMktData subscribed (async). Waiting for ticks...")

    def unsubscribe(self):
        """Cancel market data subscription and clean up."""
        self._removed = True
        if self._ticker is not None:
            try:
                self.ib.cancelMktData(self.contract)
            except Exception as e:
                log.warning(f"[{self.symbol}] Error cancelling mkt data: {e}")
            if self._ticker and hasattr(self._ticker, 'updateEvent'):
                self._ticker.updateEvent -= self._on_tick
            self._ticker = None
            self._subscription = None
        with _lock:
            _status["symbols"].pop(self.symbol, None)
        log.info(f"[{self.symbol}] Unsubscribed and removed.")

    async def unsubscribe_async(self):
        """Async-safe unsubscribe — cancel reqMktData."""
        self._removed = True
        if self._ticker is not None:
            try:
                self.ib.cancelMktData(self.contract)
            except Exception as e:
                log.warning(f"[{self.symbol}] Error cancelling mkt data: {e}")
            if self._ticker and hasattr(self._ticker, 'updateEvent'):
                self._ticker.updateEvent -= self._on_tick
            self._ticker = None
            self._subscription = None
        with _lock:
            _status["symbols"].pop(self.symbol, None)
        log.info(f"[{self.symbol}] Unsubscribed and removed (async).")

    def _maybe_evaluate_in_play(self, bar: Bar):
        """Evaluate in-play status using V2 percentile-ranked base gate.

        V2 computes: gap_abs%, abs_move_from_open%, abs_RS_vs_SPY%
        then ranks cross-sectionally across the live universe.

        Two stages:
          Provisional (9:40): informational only, not promotable by default
          Confirmed (10:00+): active promotable gate, re-evaluated each bar

        Failed names are never zeroed — strategies see the raw score.
        """
        si = self._strategy_mgr.indicators
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute

        # Session reset on day change
        date_int = bar.timestamp.year * 10000 + bar.timestamp.month * 100 + bar.timestamp.day
        if self._ip_eval_date is not None and self._ip_eval_date != date_int:
            self._ip_result = None
            self._ip_eval_date = None

        # Need session open and prior close
        baselines = si.get_in_play_baselines()
        session_open = NaN
        prior_close = baselines.get("prev_close", NaN)
        session_bars = baselines.get("session_bars", [])
        if session_bars:
            session_open = session_bars[0].open

        if math.isnan(session_open) or session_open <= 0:
            return  # can't evaluate without open price

        # Get SPY data
        spy_open = NaN
        spy_close = NaN
        if _spy_snap is not None:
            spy_open = getattr(_spy_snap, 'day_open', NaN)
            spy_close = getattr(_spy_snap, 'last_close', NaN)
            if math.isnan(spy_close):
                spy_close = getattr(_spy_snap, 'close', NaN)

        # Build universe features for cross-sectional ranking
        # Collect features from all active runners
        universe_features = {}
        for runner in _runners:
            r_si = runner._strategy_mgr.indicators if hasattr(runner, '_strategy_mgr') else None
            if r_si is None:
                continue
            r_baselines = r_si.get_in_play_baselines()
            r_bars = r_baselines.get("session_bars", [])
            r_pc = r_baselines.get("prev_close", NaN)
            if not r_bars:
                continue
            r_open = r_bars[0].open
            if runner.symbol == self.symbol:
                r_close = bar.close
            else:
                with _lock:
                    r_close = _status.get("symbols", {}).get(runner.symbol, {}).get("last_price", NaN)
                if r_close == 0:
                    r_close = NaN
            if math.isnan(r_open) or r_open <= 0 or math.isnan(r_close) or r_close <= 0:
                continue
            feat = _InPlayProxyV2._compute_features_direct(
                r_open, r_pc, r_close, spy_open, spy_close
            )
            if feat is not None:
                universe_features[runner.symbol] = feat

        # Evaluate this symbol using V2
        result = _in_play_v2.update_live(
            symbol=self.symbol,
            session_date=bar.timestamp.date(),
            sym_open=session_open,
            prior_close=prior_close,
            current_close=bar.close,
            current_hhmm=hhmm,
            spy_open=spy_open,
            spy_current_close=spy_close,
            universe_features=universe_features,
        )
        self._ip_result = result
        self._ip_eval_date = date_int

        # Surface V2 score to strategies — NEVER zero failed names
        si.in_play_score = result.active_score if not math.isnan(result.active_score) else 0.0
        si.in_play_passed = result.active_passed

        # Log on stage transitions or first eval
        if result.active_score_kind != "NONE":
            log.info(
                f"[{self.symbol}] IN-PLAY V2: {result.reason_flags} | "
                f"kind={result.active_score_kind} score={result.active_score:.3f} "
                f"passed={result.active_passed} | "
                f"gap={result.gap_abs_pct:.2f}% move={result.abs_move_from_open_pct:.2f}% "
                f"rs={result.abs_rs_vs_spy_pct:.2f}%"
            )

        # Surface to dashboard status
        with _lock:
            sym_status = _status.get("symbols", {}).get(self.symbol)
            if sym_status is not None:
                sym_status["ip_passed"] = result.active_passed
                sym_status["ip_score"] = round(result.active_score, 3) if not math.isnan(result.active_score) else None
                sym_status["ip_reason"] = result.reason_flags
                sym_status["ip_kind"] = result.active_score_kind
        _broadcast_sse("status", _status)

        # Structured CSV log (Step 6)
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute
        spy_pct = ""
        if _spy_snap is not None and not math.isnan(_spy_snap.pct_from_open):
            spy_pct = f"{_spy_snap.pct_from_open:+.2f}"
        try:
            _ip_csv_writer.writerow([
                bar.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                self.symbol,
                bar.timestamp.date().isoformat(),
                hhmm,
                f"{st.gap_pct:.4f}",
                f"{st.rvol:.3f}",
                f"{st.dolvol:.0f}",
                f"{st.range_expansion:.3f}",
                f"{result.score:.1f}",
                result.pass_gap,
                result.pass_rvol,
                result.pass_dolvol,
                result.passed,
                result.data_status,
                baselines["vol_baseline_depth"],
                baselines["range_baseline_depth"],
                spy_pct,
            ])
        except Exception:
            pass  # never crash on logging

    def _build_market_ctx(self, bar: Bar) -> Optional[MarketContext]:
        """Build MarketContext from shared SPY/QQQ snapshots."""
        global _spy_snap, _qqq_snap
        if _spy_snap is None or _qqq_snap is None:
            return None
        if not _spy_snap.ready or not _qqq_snap.ready:
            return None

        import math
        # Track day open for RS calculation
        date_int = bar.timestamp.year * 10000 + bar.timestamp.month * 100 + bar.timestamp.day
        if self._rs_date is None or self._rs_date != date_int:
            self._rs_date = date_int
            self._day_open_for_rs = bar.open
        stock_pct = (bar.close - self._day_open_for_rs) / self._day_open_for_rs * 100.0 \
            if self._day_open_for_rs > 0 else float('nan')

        return compute_market_context(
            _spy_snap, _qqq_snap,
            sector_snapshot=None,  # no sector ETF in live yet
            stock_pct_from_open=stock_pct)

    def _on_tick(self, ticker):
        """Called on every tick from reqMktData.

        Flow:
          1. Tick → BarAggregator(1min) → completed 1-min bar
          2. 1-min bar → BarUpsampler(5min) → completed 5-min bar
          3. StrategyManager processes 5-min bar → RawSignals
          4. Tape permission gate → alert pipeline → SSE broadcast
        """
        if self._removed:
            return

        price = _safe_price(ticker)
        if price is None:
            return

        # Record that this runner is receiving data (for stale-data watchdog)
        _last_bar_timestamps[self.symbol] = time.monotonic()
        self._tick_count += 1

        # Always update the live price
        with _lock:
            sym_status = _status.get("symbols", {}).get(self.symbol)
            if sym_status is not None:
                sym_status["last_price"] = round(price, 2)

        ts = datetime.now(EASTERN)
        cum_vol = getattr(ticker, 'volume', 0) or 0

        # Step 1: Tick → 1-min bar
        completed_1min = self._aggregator.on_tick(price, cum_vol, ts)
        if completed_1min is None:
            return  # still forming 1-min bar

        # Step 2a: Feed 1-min bar to 1-min strategies (EMA9_FT etc.)
        market_ctx_1m = self._build_market_ctx(completed_1min)
        signals_1m = self._strategy_mgr.on_1min_bar(completed_1min, market_ctx=market_ctx_1m)

        # Step 2a+: In-play evaluation (once per session, after first 15 bars)
        self._maybe_evaluate_in_play(completed_1min)

        # Step 2b: 1-min bar → 5-min upsampler
        completed_5min = self._upsampler.on_bar(completed_1min)

        raw_signals = list(signals_1m)  # collect 1-min signals

        if completed_5min is not None:
            # Step 3: Process completed 5-min bar through 5-min strategies
            self._bars_received += 1
            market_ctx = self._build_market_ctx(completed_5min)

            # ── Primary engine: StrategyManager 5-min path ──
            signals_5m = self._strategy_mgr.on_5min_bar(completed_5min, market_ctx=market_ctx)
            raw_signals.extend(signals_5m)

            # ── Legacy engine (only if rollback flag is set) ──
            if self._legacy_engine is not None:
                legacy_signals = self._legacy_engine.process_bar(
                    completed_5min, market_ctx=market_ctx)
                if legacy_signals:
                    for ls in legacy_signals:
                        log.info(f"[{self.symbol}] LEGACY {ls.setup_name}: "
                                 f"{'LONG' if ls.direction == 1 else 'SHORT'} "
                                 f"@ {ls.entry_price:.2f}")

            # Diagnostic: log every 10th 5-min bar or any bar that produces signals
            if self._bars_received % 10 == 0 or raw_signals:
                log.info(f"[{self.symbol}] bar #{self._bars_received} "
                         f"@ {completed_5min.timestamp.strftime('%H:%M') if completed_5min.timestamp else '?'} "
                         f"C={completed_5min.close:.2f} | signals={len(raw_signals)}")
        elif signals_1m:
            # Log 1-min signals even when no 5-min bar completed
            log.info(f"[{self.symbol}] 1m signal "
                     f"@ {completed_1min.timestamp.strftime('%H:%M') if completed_1min.timestamp else '?'} "
                     f"C={completed_1min.close:.2f} | signals={len(signals_1m)}")

        with _lock:
            _status["symbols"][self.symbol]["bars"] = self._bars_received

        # Step 4: Alert pipeline — convert RawSignals to dashboard alerts
        # Use the most recent bar's timestamp (5-min if available, else 1-min)
        alert_bar = completed_5min if completed_5min is not None else completed_1min
        alert_ctx = market_ctx if completed_5min is not None else market_ctx_1m
        # Extract bar time (hhmm) for time-normalized regime thresholds
        _bar_hhmm = None
        if alert_bar is not None:
            _bar_ts = getattr(alert_bar, 'timestamp', None) or getattr(alert_bar, 'date', None)
            if _bar_ts is not None:
                try:
                    _et = _bar_ts.astimezone(ZoneInfo("America/New_York"))
                    _bar_hhmm = _et.hour * 100 + _et.minute
                except Exception:
                    pass  # fallback: _bar_hhmm stays None → shared module uses default 0.15%

        for sig in raw_signals:
            # ── Gate 0: Regime gate (time-normalized, shared module) ──
            regime_pass, regime_reason = _check_regime_gate(
                sig.strategy_name, bar_time_hhmm=_bar_hhmm)
            if not regime_pass:
                setup_name = _STRATEGY_SETUP_MAP.get(
                    sig.strategy_name, (sig.strategy_name,))[0]
                log.info(f"[{self.symbol}] BLOCKED {setup_name} by regime: {regime_reason}")
                continue

            # ── Gate 0.5: In-play V2 base gate ──
            # V2 percentile-ranked gate. Confirmed-only promotion by default.
            # Provisional is informational — blocked unless per-strategy override.
            if self._ip_result is None or not hasattr(self._ip_result, 'active_passed'):
                setup_name = _STRATEGY_SETUP_MAP.get(
                    sig.strategy_name, (sig.strategy_name,))[0]
                log.info(f"[{self.symbol}] BLOCKED {setup_name} by in-play: not yet evaluated")
                continue

            if not self._ip_result.active_passed:
                setup_name = _STRATEGY_SETUP_MAP.get(
                    sig.strategy_name, (sig.strategy_name,))[0]
                log.info(f"[{self.symbol}] BLOCKED {setup_name} by in-play V2: "
                         f"score={self._ip_result.active_score:.3f} "
                         f"kind={self._ip_result.active_score_kind} "
                         f"flags={self._ip_result.reason_flags}")
                continue

            # Block provisional promotion unless per-strategy override
            if self._ip_result.active_score_kind == "PROVISIONAL":
                _allow_prov = (_IP_CFG.ip_v2_allow_provisional_promotion or
                               _IP_CFG.ip_v2_allow_provisional_by_strategy.get(
                                   sig.strategy_name, False))
                if not _allow_prov:
                    setup_name = _STRATEGY_SETUP_MAP.get(
                        sig.strategy_name, (sig.strategy_name,))[0]
                    log.info(f"[{self.symbol}] BLOCKED {setup_name} by in-play V2: "
                             f"provisional not promotable (score={self._ip_result.active_score:.3f})")
                    continue

            # ── Gate: Quality tier (unified — ALL strategies) ──
            # All strategies now use the internal quality pipeline
            # (rejection filters → quality scoring → tier assignment).
            # Only A-tier signals are promoted to alerts.
            _qt = sig.metadata.get("quality_tier", "B") if sig.metadata else "B"
            if _qt != "A":  # QualityTier.A_TIER.value
                setup_name = _STRATEGY_SETUP_MAP.get(
                    sig.strategy_name, (sig.strategy_name,))[0]
                log.info(f"[{self.symbol}] BLOCKED {setup_name} by quality: "
                         f"tier={_qt} (need A_TIER)")
                continue

            # ── EMA9_FT cluster management: daily cap + SPY % gate ──
            if sig.strategy_name == "EMA9_FT":
                global _ema9ft_daily_count, _ema9ft_last_date
                today = alert_bar.timestamp.date() if alert_bar.timestamp else None

                with _lock:
                    # Reset counter on new day
                    if today and today != _ema9ft_last_date:
                        _ema9ft_daily_count = 0
                        _ema9ft_last_date = today

                    # Gate 1: SPY must be >= threshold % from open
                    if _EMA9FT_SPY_MIN_PCT > 0 and _spy_snap is not None:
                        spy_pct = _spy_snap.pct_from_open
                        if not (spy_pct is not None and not math.isnan(spy_pct)
                                and spy_pct >= _EMA9FT_SPY_MIN_PCT):
                            log.info(f"[{self.symbol}] BLOCKED EMA9_FT: "
                                     f"SPY pct={spy_pct:.2f}% < {_EMA9FT_SPY_MIN_PCT:.2f}%")
                            continue

                    # Gate 2: Daily cap (first-in-time basis)
                    if _EMA9FT_DAILY_CAP > 0 and _ema9ft_daily_count >= _EMA9FT_DAILY_CAP:
                        log.info(f"[{self.symbol}] BLOCKED EMA9_FT: "
                                 f"daily cap reached ({_ema9ft_daily_count}/{_EMA9FT_DAILY_CAP})")
                        continue

                    _ema9ft_daily_count += 1
                    count_now = _ema9ft_daily_count

                spy_pct_str = (f" (SPY {_spy_snap.pct_from_open:+.2f}%)"
                               if _spy_snap and not math.isnan(_spy_snap.pct_from_open)
                               else "")
                log.info(f"[{self.symbol}] EMA9_FT signal #{count_now}/{_EMA9FT_DAILY_CAP}{spy_pct_str}")

            alert_data = _raw_signal_to_dict(
                sig, self.symbol, alert_bar.timestamp, universe=self.universe)
            # Add regime gate result to alert data
            alert_data["regime_gate"] = regime_reason
            # Add in-play gate result to alert data
            if self._ip_result is not None:
                alert_data["in_play_score"] = round(self._ip_result.score, 1)
                alert_data["in_play_status"] = self._ip_result.data_status
            else:
                alert_data["in_play_score"] = None
                alert_data["in_play_status"] = "PENDING"
            # Add tape permission to alert data for display
            if sig.direction == 1 and alert_ctx is not None:
                alert_data["tape_perm"] = round(perm_reading.permission, 3)

            with _lock:
                _alert_history.insert(0, alert_data)
                _status["symbols"][self.symbol]["alerts"] = \
                    _status["symbols"][self.symbol].get("alerts", 0) + 1
                # Keep last 100 alerts
                if len(_alert_history) > 100:
                    _alert_history.pop()

            log.info(f"[{self.symbol}] ALERT: {alert_data['direction']} "
                     f"{alert_data['setup']} @ {alert_data['entry']}"
                     f"{' perm=' + str(alert_data.get('tape_perm', '')) if alert_data.get('tape_perm') else ''}")
            _broadcast_sse("alert", alert_data)

        _broadcast_sse("status", _status)

    def _convert_bar(self, ib_bar) -> Bar:
        if hasattr(ib_bar, 'date'):
            if isinstance(ib_bar.date, str):
                try:
                    ts = datetime.strptime(ib_bar.date, "%Y%m%d  %H:%M:%S")
                except ValueError:
                    ts = datetime.strptime(ib_bar.date, "%Y%m%d")
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=EASTERN)
            else:
                if ib_bar.date.tzinfo is not None:
                    ts = ib_bar.date.astimezone(EASTERN)
                else:
                    ts = ib_bar.date.replace(tzinfo=EASTERN)
        else:
            ts = datetime.now(EASTERN)

        return Bar(
            timestamp=ts, open=ib_bar.open, high=ib_bar.high,
            low=ib_bar.low, close=ib_bar.close,
            volume=ib_bar.volume if hasattr(ib_bar, 'volume') else 0,
        )


# ── Web Server ──

DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"


class DashboardHandler(SimpleHTTPRequestHandler):
    """Serves the dashboard HTML and SSE stream."""

    def log_message(self, format, *args):
        pass  # Suppress HTTP logs

    def do_GET(self):
        # Strip query params for routing
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            self._serve_file(DASHBOARD_HTML, "text/html")
        elif path == "/api/alerts":
            self._serve_json(_alert_history)
        elif path == "/api/status":
            self._serve_json(_status)
        elif path == "/api/stream":
            self._serve_sse()
        elif path == "/api/test-alert":
            self._send_test_alert()
        elif path == "/api/clear-test-alerts":
            self._clear_test_alerts()
        elif path.startswith("/api/delete-alert/"):
            self._delete_alert(path.split("/")[-1])
        elif path == "/api/export-csv":
            self._export_csv()
        elif path == "/api/orders":
            self._serve_json(
                _order_manager.get_open_orders() if _order_manager else [])
        elif path == "/api/in-play":
            with _lock:
                self._serve_json({"ok": True, "symbols": sorted(_in_play_symbols)})
        elif path == "/api/iplog":
            self._serve_in_play_history()
        elif path == "/api/export-alerts":
            self._export_alert_history()
        elif path == "/api/diag":
            self._serve_diagnostics()
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]
        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode() if content_length else ""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        if path == "/api/add-symbol":
            self._add_symbol(data)
        elif path == "/api/remove-symbol":
            self._remove_symbol(data)
        elif path == "/api/order":
            self._place_order(data)
        elif path == "/api/cancel-order":
            self._cancel_order(data)
        elif path == "/api/add-in-play":
            self._add_in_play(data)
        elif path == "/api/remove-in-play":
            self._remove_in_play(data)
        elif path == "/api/bulk-add-in-play":
            self._bulk_add_in_play(data)
        elif path == "/api/clear-in-play":
            self._clear_in_play(data)
        elif path == "/api/connect":
            self._connect_ibkr(data)
        else:
            self.send_error(404)

    def _add_symbol(self, data):
        """Add a new symbol to the live watchlist."""
        symbol = (data.get("symbol") or "").strip().upper()
        if not symbol:
            self._send_json_response(400, {"ok": False, "error": "No symbol provided"})
            return

        # Check for duplicate
        with _lock:
            if symbol in _status.get("symbols", {}):
                self._send_json_response(409, {"ok": False, "error": f"{symbol} already tracked"})
                return

        # Add a placeholder so UI updates immediately
        universe = _get_universe(symbol)
        with _lock:
            _status["symbols"][symbol] = {"bars": 0, "alerts": 0, "last_price": 0,
                                          "universe": universe}
        _save_watchlist()
        _broadcast_sse("status", _status)

        # If IBKR is connected, subscribe the symbol live
        if _main_loop is not None:
            with _lock:
                _status["symbols"][symbol]["warming_up"] = True
            _broadcast_sse("status", _status)

            async def _setup_and_subscribe():
                global _ib, _cfg, _runners
                try:
                    runner = SymbolRunner(symbol, _ib, _cfg, universe=universe)
                    await runner.setup_async()
                    await runner.subscribe_async()
                    with _lock:
                        _runners.append(runner)
                        _status["symbols"][symbol].pop("warming_up", None)
                    _save_watchlist()
                    _broadcast_sse("status", _status)
                    log.info(f"[{symbol}] Added to watchlist (universe={universe}).")
                except Exception as e:
                    log.error(f"[{symbol}] Failed to add: {e}")
                    with _lock:
                        _status["symbols"].pop(symbol, None)
                    _broadcast_sse("status", _status)

            try:
                _schedule_on_main(lambda: _setup_and_subscribe())
            except RuntimeError:
                pass  # will subscribe on next connect
            self._send_json_response(200, {"ok": True, "symbol": symbol, "message": f"Adding {symbol}..."})
        else:
            # Offline/deferred mode — symbol saved to watchlist, will subscribe on connect
            log.info(f"[{symbol}] Added to static watchlist (offline, will subscribe on connect).")
            self._send_json_response(200, {"ok": True, "symbol": symbol, "message": f"{symbol} added (will connect later)"})

    def _remove_symbol(self, data):
        """Remove a symbol from the live watchlist."""
        symbol = (data.get("symbol") or "").strip().upper()
        if not symbol:
            self._send_json_response(400, {"ok": False, "error": "No symbol provided"})
            return

        # Find and remove the runner
        removed = False
        with _lock:
            for runner in _runners:
                if runner.symbol == symbol:
                    _runners.remove(runner)
                    removed = True
                    break

        if removed:
            # Immediately set kill switch so no more alerts fire
            runner._removed = True
            # Unsubscribe on the main event loop (IB requires it)
            async def _unsub():
                try:
                    await runner.unsubscribe_async()
                except Exception as e:
                    log.warning(f"[{symbol}] Error during unsubscribe: {e}")
                _save_watchlist()
                _broadcast_sse("status", _status)

            try:
                _schedule_on_main(lambda: _unsub())
            except RuntimeError as e:
                log.warning(f"[{symbol}] Removed from list but unsubscribe deferred: {e}")
            self._send_json_response(200, {"ok": True, "symbol": symbol})
        else:
            # Not a runner but might be in status (failed warmup)
            with _lock:
                _status["symbols"].pop(symbol, None)
            _broadcast_sse("status", _status)
            _save_watchlist()
            self._send_json_response(200, {"ok": True, "symbol": symbol})

    # ── In-play universe handlers ──

    def _add_in_play(self, data):
        """Add a symbol to the daily in-play list."""
        global _in_play_symbols
        symbol = (data.get("symbol") or "").strip().upper()
        if not symbol:
            self._send_json_response(400, {"ok": False, "error": "No symbol"})
            return

        with _lock:
            if symbol in _in_play_symbols:
                self._send_json_response(409, {"ok": False, "error": f"{symbol} already in-play"})
                return
            # Check if already in static watchlist — no need to add to in-play
            static_symbols = _load_watchlist()
            if symbol in static_symbols:
                self._send_json_response(409, {"ok": False, "error": f"{symbol} already tracked (static watchlist)"})
                return
            _in_play_symbols.append(symbol)
        _save_in_play()
        _log_in_play_event(symbol, "ADD")
        _save_in_play_snapshot()

        # If symbol not already tracked, auto-subscribe it
        with _lock:
            already_tracked = symbol in _status.get("symbols", {})

        if not already_tracked:
            with _lock:
                _status["symbols"][symbol] = {
                    "bars": 0, "alerts": 0, "last_price": 0,
                    "warming_up": True, "universe": "IN_PLAY",
                }

            async def _setup_in_play():
                global _ib, _cfg, _runners
                try:
                    runner = SymbolRunner(symbol, _ib, _cfg, universe="IN_PLAY")
                    await runner.setup_async()
                    await runner.subscribe_async()
                    with _lock:
                        _runners.append(runner)
                        _status["symbols"][symbol].pop("warming_up", None)
                    _broadcast_sse("status", _status)
                    log.info(f"[{symbol}] In-play symbol subscribed.")
                except Exception as e:
                    log.error(f"[{symbol}] Failed to add in-play: {e}")
                    with _lock:
                        _status["symbols"].pop(symbol, None)
                    _broadcast_sse("status", _status)

            _schedule_on_main(lambda: _setup_in_play())
        else:
            # Already tracked — update universe tag to BOTH
            with _lock:
                _status["symbols"][symbol]["universe"] = "BOTH"
            # Update the runner's engine universe
            for runner in _runners:
                if runner.symbol == symbol:
                    runner.universe = "BOTH"
                    if runner._legacy_engine is not None:
                        runner._legacy_engine._universe_source = "BOTH"
                    break

        with _lock:
            _status["in_play"] = sorted(_in_play_symbols)
        _broadcast_sse("in_play_updated", {
            "symbols": sorted(_in_play_symbols),
            "action": "ADD", "symbol": symbol,
        })
        _broadcast_sse("status", _status)
        self._send_json_response(200, {"ok": True, "symbol": symbol})

    def _bulk_add_in_play(self, data):
        """Add multiple symbols to the in-play list at once.

        Accepts {"symbols": "AAPL, TSLA, NVDA"} or {"symbols": "AAPL\\nTSLA\\nNVDA"}
        Handles comma-separated, space-separated, newline-separated, or any mix.
        Skips duplicates silently.
        """
        global _in_play_symbols
        raw = (data.get("symbols") or "").strip()
        if not raw:
            self._send_json_response(400, {"ok": False, "error": "No symbols provided"})
            return

        # Parse: split on commas, newlines, spaces, tabs
        import re
        tokens = re.split(r'[,\s\n\r\t]+', raw)
        symbols = [t.strip().upper() for t in tokens if t.strip() and t.strip().isalnum()]

        if not symbols:
            self._send_json_response(400, {"ok": False, "error": "No valid symbols found"})
            return

        added = []
        skipped = []
        new_symbols_to_setup = []
        for symbol in symbols:
            with _lock:
                if symbol in _in_play_symbols:
                    skipped.append(symbol)
                    continue
                _in_play_symbols.append(symbol)
            _log_in_play_event(symbol, "ADD")
            added.append(symbol)

            # If not already tracked, subscribe it
            with _lock:
                already_tracked = symbol in _status.get("symbols", {})

            if not already_tracked:
                with _lock:
                    _status["symbols"][symbol] = {
                        "bars": 0, "alerts": 0, "last_price": 0,
                        "warming_up": True, "universe": "IN_PLAY",
                    }
                new_symbols_to_setup.append(symbol)
            else:
                with _lock:
                    _status["symbols"][symbol]["universe"] = "BOTH"
                for runner in _runners:
                    if runner.symbol == symbol:
                        runner.universe = "BOTH"
                        break

        _save_in_play()
        _save_in_play_snapshot()

        with _lock:
            _status["in_play"] = sorted(_in_play_symbols)
        _broadcast_sse("in_play_updated", {
            "symbols": sorted(_in_play_symbols),
            "action": "BULK_ADD", "added": added, "skipped": skipped,
        })
        _broadcast_sse("status", _status)

        # ── Paced bulk setup: subscribe new symbols with IBKR pacing ──
        if new_symbols_to_setup:
            async def _paced_bulk_setup():
                global _ib, _cfg, _runners
                total = len(new_symbols_to_setup)
                for idx, sym in enumerate(new_symbols_to_setup):
                    try:
                        runner = SymbolRunner(sym, _ib, _cfg, universe="IN_PLAY")
                        await runner.setup_async()
                        await runner.subscribe_async()
                        with _lock:
                            _runners.append(runner)
                            _status["symbols"][sym].pop("warming_up", None)
                        _broadcast_sse("status", _status)
                        log.info(f"[{sym}] In-play symbol subscribed (bulk {idx+1}/{total}).")
                    except Exception as e:
                        log.error(f"[{sym}] Failed to add in-play (bulk): {e}")
                        with _lock:
                            _status["symbols"].pop(sym, None)
                        _broadcast_sse("status", _status)
                    # Pace: 2s between symbols to stay under IBKR's 60 req/10min limit
                    await asyncio.sleep(2.0)
                log.info(f"Bulk in-play setup complete: {total} symbols processed.")

            try:
                _schedule_on_main(lambda: _paced_bulk_setup())
            except RuntimeError:
                log.error("Cannot schedule bulk setup — main loop not ready")

        log.info(f"Bulk in-play: added {len(added)}, skipped {len(skipped)} duplicates"
                 f", {len(new_symbols_to_setup)} queued for paced setup")
        self._send_json_response(200, {
            "ok": True,
            "added": added,
            "skipped": skipped,
            "queued_for_setup": new_symbols_to_setup,
            "total_in_play": len(_in_play_symbols),
        })

    def _remove_in_play(self, data):
        """Remove a symbol from the in-play list."""
        global _in_play_symbols
        symbol = (data.get("symbol") or "").strip().upper()
        if not symbol:
            self._send_json_response(400, {"ok": False, "error": "No symbol"})
            return

        with _lock:
            if symbol not in _in_play_symbols:
                self._send_json_response(404, {"ok": False, "error": f"{symbol} not in in-play list"})
                return
            _in_play_symbols = [s for s in _in_play_symbols if s != symbol]
        _save_in_play()
        _log_in_play_event(symbol, "REMOVE")
        _save_in_play_snapshot()

        # Check if symbol is in static watchlist
        static_symbols = _load_watchlist()
        if symbol in static_symbols:
            # Still tracked as STATIC — update universe tag
            with _lock:
                if symbol in _status.get("symbols", {}):
                    _status["symbols"][symbol]["universe"] = "STATIC"
            for runner in _runners:
                if runner.symbol == symbol:
                    runner.universe = "STATIC"
                    if runner._legacy_engine is not None:
                        runner._legacy_engine._universe_source = "STATIC"
                    break
        else:
            # Not in static — unsubscribe entirely on the main event loop
            removed_runner = None
            with _lock:
                for runner in _runners:
                    if runner.symbol == symbol:
                        _runners.remove(runner)
                        removed_runner = runner
                        break
                _status["symbols"].pop(symbol, None)
            if removed_runner:
                removed_runner._removed = True  # immediate kill switch
                async def _unsub(r=removed_runner):
                    try:
                        await r.unsubscribe_async()
                    except Exception as e:
                        log.warning(f"[{r.symbol}] Error during in-play unsubscribe: {e}")
                _schedule_on_main(lambda: _unsub())

        with _lock:
            _status["in_play"] = sorted(_in_play_symbols)
        _broadcast_sse("in_play_updated", {
            "symbols": sorted(_in_play_symbols),
            "action": "REMOVE", "symbol": symbol,
        })
        _broadcast_sse("status", _status)
        self._send_json_response(200, {"ok": True, "symbol": symbol})

    def _clear_in_play(self, data):
        """Clear entire in-play list (morning reset).

        Static symbols that were also in-play (BOTH) revert to STATIC.
        They are never removed from the static watchlist or the dashboard.
        """
        global _in_play_symbols
        static_symbols = set(_load_watchlist())

        with _lock:
            symbols_to_clear = list(_in_play_symbols)
            _in_play_symbols = []
        _save_in_play()
        for sym in symbols_to_clear:
            _log_in_play_event(sym, "CLEAR")
        _save_in_play_snapshot()

        # Unsubscribe symbols that were in-play only (not in static)
        runners_to_unsub = []
        for symbol in symbols_to_clear:
            if symbol not in static_symbols:
                with _lock:
                    for runner in _runners:
                        if runner.symbol == symbol:
                            _runners.remove(runner)
                            runners_to_unsub.append(runner)
                            break
                    _status["symbols"].pop(symbol, None)
            else:
                # Reset BOTH → STATIC (preserve the symbol)
                with _lock:
                    if symbol in _status.get("symbols", {}):
                        _status["symbols"][symbol]["universe"] = "STATIC"
                for runner in _runners:
                    if runner.symbol == symbol:
                        runner.universe = "STATIC"
                        if runner._legacy_engine is not None:
                            runner._legacy_engine._universe_source = "STATIC"
                        break
                log.info(f"[{symbol}] Reverted BOTH → STATIC (preserved in watchlist)")

        # Safety: ensure ALL static symbols are present in _status
        # This catches any edge case where a static symbol was accidentally removed
        with _lock:
            for static_sym in static_symbols:
                if static_sym not in _status.get("symbols", {}):
                    _status["symbols"][static_sym] = {
                        "bars": 0, "alerts": 0, "last_price": 0,
                        "universe": "STATIC",
                    }
                    log.warning(f"[{static_sym}] Restored missing static symbol after in-play clear")

        # Immediate kill switch on all runners being removed
        for r in runners_to_unsub:
            r._removed = True

        # Unsubscribe collected runners on the main event loop
        if runners_to_unsub:
            async def _unsub_all():
                for r in runners_to_unsub:
                    try:
                        await r.unsubscribe_async()
                    except Exception as e:
                        log.warning(f"[{r.symbol}] Error during clear unsubscribe: {e}")
                log.info(f"Clear in-play: unsubscribed {len(runners_to_unsub)} in-play-only symbols.")
            _schedule_on_main(lambda: _unsub_all())

        with _lock:
            _status["in_play"] = []
        log.info(f"Clear in-play: cleared {len(symbols_to_clear)} symbols, "
                 f"{len(static_symbols)} static symbols preserved")
        _broadcast_sse("in_play_updated", {
            "symbols": [],
            "action": "CLEAR", "cleared": symbols_to_clear,
        })
        _broadcast_sse("status", _status)
        self._send_json_response(200, {"ok": True, "cleared": len(symbols_to_clear)})

    def _connect_ibkr(self, data):
        """Trigger IBKR connection from deferred mode.

        Called when user clicks the Connect button after setting up their symbol list.
        Starts the full IBKR connection + warmup on a background thread.
        """
        with _lock:
            if _status.get("connected"):
                self._send_json_response(409, {"ok": False, "error": "Already connected"})
                return
            if _status.get("mode") == "CONNECTING":
                self._send_json_response(409, {"ok": False, "error": "Connection already in progress"})
                return
            _status["mode"] = "CONNECTING"
        _broadcast_sse("status", _status)

        # Use stored deferred params as defaults, allow override from request
        host = data.get("host", _status.get("_deferred_host", "127.0.0.1"))
        port = int(data.get("port", _status.get("_deferred_port", 7497)))
        client_id = int(data.get("client_id", _status.get("_deferred_client_id", 10)))

        # Run connection on a background thread (IBKR blocking calls)
        connect_thread = threading.Thread(
            target=_run_ibkr_connection,
            args=(host, port, client_id),
            daemon=True,
        )
        connect_thread.start()
        self._send_json_response(200, {"ok": True, "message": "Connecting to IBKR..."})


    def _export_alert_history(self):
        """Export alert history as CSV download."""
        import csv
        import io
        output = io.StringIO()
        if _alert_history:
            fieldnames = list(_alert_history[0].keys())
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            for alert in _alert_history:
                row = {}
                for k, v in alert.items():
                    row[k] = ",".join(v) if isinstance(v, list) else v
                writer.writerow(row)
        csv_data = output.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Disposition", "attachment; filename=alert_history.csv")
        self.send_header("Content-Length", str(len(csv_data.encode())))
        self.end_headers()
        self.wfile.write(csv_data.encode())

    def _serve_in_play_history(self):
        """Return in-play event log and snapshot info."""
        history_rows = []
        if IN_PLAY_HISTORY_FILE.exists():
            for line in IN_PLAY_HISTORY_FILE.read_text().splitlines()[1:]:
                parts = line.strip().split(",")
                if len(parts) == 4:
                    history_rows.append({
                        "date": parts[0], "time": parts[1],
                        "symbol": parts[2], "action": parts[3],
                    })
        snapshots = []
        if IN_PLAY_SNAPSHOT_DIR.exists():
            for f in sorted(IN_PLAY_SNAPSHOT_DIR.glob("*.txt")):
                syms = [l.strip() for l in f.read_text().splitlines() if l.strip()]
                snapshots.append({"date": f.stem, "symbols": syms})
        self._serve_json({
            "ok": True,
            "history": history_rows,
            "snapshots": snapshots,
        })

    def _serve_diagnostics(self):
        """Return per-symbol subscription health for debugging.

        Accessible at http://localhost:8877/api/diag
        Shows: tick_count, bars_received, last_tick_age, subscription status.
        """
        now_mono = time.monotonic()
        diag = {
            "total_runners": len(_runners),
            "total_mktdata_lines": 2 + len(_runners),  # +SPY +QQQ
            "spy_snap": {
                "pct_from_open": round(_spy_snap.pct_from_open, 4) if _spy_snap else None,
                "above_vwap": _spy_snap.above_vwap if _spy_snap else None,
            } if _spy_snap else None,
            "symbols": {},
        }

        healthy = 0
        stale = 0
        dead = 0
        for runner in _runners:
            last_tick_mono = _last_bar_timestamps.get(runner.symbol, 0)
            age = round(now_mono - last_tick_mono, 1) if last_tick_mono > 0 else -1
            ticks = runner._tick_count
            bars = runner._bars_received

            if ticks == 0:
                status = "DEAD"
                dead += 1
            elif age > 300:
                status = "STALE"
                stale += 1
            else:
                status = "OK"
                healthy += 1

            ip_status = "N/A"
            if runner._ip_result is not None:
                ip_status = "PASS" if runner._ip_result.passed else f"FAIL({runner._ip_result.reason})"

            diag["symbols"][runner.symbol] = {
                "status": status,
                "ticks": ticks,
                "bars_5m": bars,
                "last_tick_age_s": age,
                "universe": runner.universe,
                "in_play": ip_status,
            }

        diag["summary"] = {
            "healthy": healthy,
            "stale": stale,
            "dead": dead,
        }
        self._serve_json(diag)

    # ── Order placement handlers ──

    def _place_order(self, data):
        """Place a bracket order from the order panel."""
        global _order_manager
        if _order_manager is None:
            self._send_json_response(503, {"ok": False, "error": "Order manager not initialized"})
            return

        try:
            ok, msg = _order_manager.place_bracket(data)
            if ok:
                self._send_json_response(200, {
                    "ok": True,
                    "alert_id": data.get("alert_id"),
                    "order_id": msg,
                    "message": f"Bracket placed for {data.get('symbol', '?')}",
                })
            else:
                self._send_json_response(409, {"ok": False, "error": msg})
        except Exception as e:
            log.error(f"Order endpoint error: {e}", exc_info=True)
            self._send_json_response(500, {"ok": False, "error": str(e)})

    def _cancel_order(self, data):
        """Cancel a bracket order by alert_id."""
        global _order_manager
        if _order_manager is None:
            self._send_json_response(503, {"ok": False, "error": "Order manager not initialized"})
            return

        alert_id = data.get("alert_id", "")
        if not alert_id:
            self._send_json_response(400, {"ok": False, "error": "Missing alert_id"})
            return

        ok, msg = _order_manager.cancel_bracket(alert_id)
        self._send_json_response(200 if ok else 409, {"ok": ok, "message": msg})

    def _send_json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _clear_test_alerts(self):
        """Remove all test alerts from history."""
        with _lock:
            before = len(_alert_history)
            _alert_history[:] = [a for a in _alert_history if not a.get("test", False)]
            removed = before - len(_alert_history)
        _broadcast_sse("history", _alert_history)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "removed": removed}).encode())

    def _delete_alert(self, alert_id):
        """Remove a single alert by ID."""
        with _lock:
            _alert_history[:] = [a for a in _alert_history if a.get("id") != alert_id]
        _broadcast_sse("history", _alert_history)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "deleted": alert_id}).encode())

    def _export_csv(self):
        """Export real alerts (not test) as a CSV download."""
        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["timestamp", "symbol", "direction", "setup", "family",
                         "entry", "stop", "target", "risk", "reward", "rr",
                         "quality", "confluence", "sweeps"])
        with _lock:
            real_alerts = [a for a in _alert_history if not a.get("test", False)]
        for a in real_alerts:
            writer.writerow([
                a.get("timestamp", ""), a.get("symbol", ""), a.get("direction", ""),
                a.get("setup", ""), a.get("family", ""),
                a.get("entry", ""), a.get("stop", ""), a.get("target", ""),
                a.get("risk", ""), a.get("reward", ""), a.get("rr", ""),
                a.get("quality", ""),
                " ".join(a.get("confluence", [])),
                " ".join(a.get("sweeps", [])),
            ])
        csv_bytes = output.getvalue().encode()
        today = datetime.now(EASTERN).strftime("%Y%m%d")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Disposition", f'attachment; filename="alerts_{today}.csv"')
        self.end_headers()
        self.wfile.write(csv_bytes)

    def _send_test_alert(self):
        """Fire a fake alert so the user can see what it looks like."""
        import random
        now = datetime.now(EASTERN)
        symbols = list(_status.get("symbols", {}).keys()) or ["SPY", "TSLA", "AAPL"]
        sym = random.choice(symbols)
        direction = random.choice([1, -1])
        setups = ["BOX_REV", "VWAP_SEP", "MANIP", "EMA_PULL", "VWAP_KISS", "EMA_RETEST", "EMA9_SEP"]
        setup = random.choice(setups)
        families = {"BOX_REV": "REVERSAL", "VWAP_SEP": "REVERSAL", "MANIP": "REVERSAL",
                     "EMA_PULL": "TREND", "VWAP_KISS": "TREND", "EMA_RETEST": "TREND", "EMA9_SEP": "EMA_MEAN_REV"}
        base = round(random.uniform(100, 500), 2)
        risk = round(random.uniform(0.5, 2.0), 2)
        rr = round(random.uniform(1.5, 3.5), 1)
        reward = round(risk * rr, 2)

        universe = _get_universe(sym)
        alert_data = {
            "id": f"test-{now.strftime('%H%M%S')}-{random.randint(100,999)}",
            "test": True,
            "symbol": sym,
            "timestamp": now.strftime("%H:%M:%S"),
            "direction": "LONG" if direction == 1 else "SHORT",
            "setup": setup,
            "family": families.get(setup, "REVERSAL"),
            "entry": base,
            "stop": round(base - risk, 2) if direction == 1 else round(base + risk, 2),
            "target": round(base + reward, 2) if direction == 1 else round(base - reward, 2),
            "risk": risk,
            "reward": reward,
            "rr": rr,
            "quality": random.randint(1, 5),
            "confluence": random.sample(["PDH", "PDL", "ORH", "ORL", "VWAP", "EMA20"], k=random.randint(1, 3)),
            "sweeps": random.sample(["PDH", "PDL", "LOCAL"], k=random.randint(0, 2)),
            "universe": universe,
        }

        with _lock:
            _alert_history.insert(0, alert_data)
            if len(_alert_history) > 100:
                _alert_history.pop()

        log.info(f"[TEST] {alert_data['direction']} {alert_data['setup']} on {sym} @ {base}")
        _broadcast_sse("alert", alert_data)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "alert": alert_data}).encode())

    def _serve_file(self, path, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with open(path, "rb") as f:
            self.wfile.write(f.read())

    def _serve_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = Queue()
        with _lock:
            _sse_clients.append(q)

        try:
            # Send current state immediately
            self.wfile.write(f"event: status\ndata: {json.dumps(_status)}\n\n".encode())
            self.wfile.write(f"event: history\ndata: {json.dumps(_alert_history)}\n\n".encode())
            self.wfile.flush()

            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
                except Exception:
                    # No message in 15s — send heartbeat to keep connection alive
                    self.wfile.write(": heartbeat\n\n".encode())
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with _lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in a separate thread so SSE doesn't block other requests."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        """Suppress noisy ConnectionResetError from browser tab close/refresh."""
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return  # browser closed connection — harmless, don't log
        super().handle_error(request, client_address)


def _run_web_server():
    server = ThreadedHTTPServer(("0.0.0.0", DASHBOARD_PORT), DashboardHandler)
    server.serve_forever()


_last_bar_timestamps: dict = {}  # symbol -> time.monotonic() of last bar received
_resubscribe_in_progress = False

def _check_stale_subscriptions():
    """Detect and recover from silently dead reqMktData subscriptions.

    If during market hours no runner has received a tick in STALE_THRESHOLD
    seconds, cancel and resubscribe all runners.  This catches silent IBKR
    disconnects where subscriptions die without an error.
    """
    global _resubscribe_in_progress
    if _resubscribe_in_progress:
        return
    now_et = datetime.now(EASTERN)
    hhmm = now_et.hour * 100 + now_et.minute
    if now_et.weekday() >= 5 or hhmm < 935 or hhmm >= 1600:
        return  # outside market hours — no ticks expected

    STALE_THRESHOLD = 600  # 10 minutes with no ticks = stale
    now_mono = time.monotonic()

    # Check if ANY runner has received a tick recently
    if not _last_bar_timestamps:
        return  # no data yet — still warming up
    most_recent = max(_last_bar_timestamps.values()) if _last_bar_timestamps else 0
    stale_seconds = now_mono - most_recent
    if stale_seconds < STALE_THRESHOLD:
        return  # all good

    log.warning(f"STALE DATA DETECTED: No ticks received in {stale_seconds:.0f}s. "
                f"Resubscribing all {len(_runners)} runners...")
    _resubscribe_in_progress = True

    async def _do_resubscribe_async():
        """Async resubscribe — must run on main event loop."""
        global _resubscribe_in_progress, _spy_snap, _qqq_snap
        try:
            # ── Resubscribe SPY/QQQ market trackers FIRST (regime depends on them) ──
            for label, tracker in [("SPY", _spy_runner), ("QQQ", _qqq_runner)]:
                if tracker is None:
                    continue
                try:
                    tracker.unsubscribe()
                    # Re-setup to refresh daily data + warm snapshot
                    tracker.setup()
                    tracker.subscribe()
                    if label == "SPY":
                        _spy_snap = tracker.snapshot
                    else:
                        _qqq_snap = tracker.snapshot
                    log.info(f"[MKT:{label}] Resubscribed.")
                except Exception as e:
                    log.error(f"[MKT:{label}] Resubscribe failed: {e}")
                await asyncio.sleep(1.0)

            # ── Resubscribe all symbol runners ──
            for i, runner in enumerate(_runners):
                if runner._removed:
                    continue
                try:
                    # Cancel existing mkt data
                    if runner._ticker is not None:
                        try:
                            runner.ib.cancelMktData(runner.contract)
                        except Exception:
                            pass
                        runner._ticker = None
                        runner._subscription = None
                    await runner.subscribe_async()
                    log.info(f"[{runner.symbol}] Resubscribed.")
                except Exception as e:
                    log.error(f"[{runner.symbol}] Resubscribe failed: {e}")
                # Pace resubscriptions — 0.5s between each (reqMktData is much lighter)
                await asyncio.sleep(0.5)
            log.info(f"Resubscription complete for {len(_runners)} runners + SPY/QQQ.")
        except Exception as e:
            log.error(f"Resubscription error: {e}")
        finally:
            _resubscribe_in_progress = False

    # Schedule on main loop (async-safe)
    if _main_loop:
        asyncio.run_coroutine_threadsafe(_do_resubscribe_async(), _main_loop)
    else:
        log.error("Cannot resubscribe — main event loop not available.")


def _check_per_symbol_stale():
    """Detect individual symbols with silently dead reqMktData subscriptions.

    Unlike _check_stale_subscriptions() which only triggers when ALL symbols
    go stale, this catches the common case where IBKR silently drops some
    subscriptions (e.g., due to market data line limits) while others work fine.

    Resubscribes stale symbols in small batches to avoid overwhelming IBKR.
    """
    now_et = datetime.now(EASTERN)
    hhmm = now_et.hour * 100 + now_et.minute
    if now_et.weekday() >= 5 or hhmm < 940 or hhmm >= 1600:
        return  # outside market hours or too early (warmup still running)

    PER_SYMBOL_STALE_THRESHOLD = 300  # 5 minutes with no tick = stale
    MAX_RESUB_PER_CYCLE = 5           # max resubscribes per heartbeat cycle
    now_mono = time.monotonic()

    stale_runners = []
    for runner in _runners:
        if runner._removed:
            continue
        last_tick = _last_bar_timestamps.get(runner.symbol, 0)
        if last_tick == 0:
            # Never received a tick — stale since startup
            stale_runners.append(runner)
        elif (now_mono - last_tick) > PER_SYMBOL_STALE_THRESHOLD:
            stale_runners.append(runner)

    if not stale_runners:
        return

    active_count = sum(1 for r in _runners if not r._removed)
    stale_pct = len(stale_runners) / max(active_count, 1)

    # If >50% of symbols are stale, do a full resubscribe of ALL runners
    # (likely a competing session or disconnect killed everything)
    if stale_pct > 0.50 and not _resubscribe_in_progress:
        log.warning(f"MASS STALE: {len(stale_runners)}/{active_count} symbols "
                    f"({stale_pct:.0%}) stale. Full resubscribe of all runners + SPY/QQQ.")

        async def _full_resub():
            global _resubscribe_in_progress, _spy_snap, _qqq_snap
            _resubscribe_in_progress = True
            try:
                for label, tracker in [("SPY", _spy_runner), ("QQQ", _qqq_runner)]:
                    if tracker is None:
                        continue
                    try:
                        tracker.unsubscribe()
                        tracker.setup()
                        tracker.subscribe()
                        if label == "SPY":
                            _spy_snap = tracker.snapshot
                        else:
                            _qqq_snap = tracker.snapshot
                        log.info(f"[MKT:{label}] Mass-resubscribed.")
                    except Exception as e:
                        log.error(f"[MKT:{label}] Mass-resubscribe failed: {e}")
                    await asyncio.sleep(1.0)

                for runner in _runners:
                    if runner._removed:
                        continue
                    try:
                        if runner._ticker is not None:
                            try:
                                runner.ib.cancelMktData(runner.contract)
                            except Exception:
                                pass
                            runner._ticker = None
                            runner._subscription = None
                        await runner.subscribe_async()
                    except Exception as e:
                        log.error(f"[{runner.symbol}] Mass-resubscribe failed: {e}")
                    await asyncio.sleep(0.3)

                log.info(f"Mass resubscription complete: {len(_runners)} runners + SPY/QQQ.")
            finally:
                _resubscribe_in_progress = False

        if _main_loop:
            asyncio.run_coroutine_threadsafe(_full_resub(), _main_loop)
        return

    # Small number stale — resubscribe in batches
    batch = stale_runners[:MAX_RESUB_PER_CYCLE]
    symbols_list = [r.symbol for r in batch]
    log.warning(f"PER-SYMBOL STALE: {len(stale_runners)} stale symbols detected. "
                f"Resubscribing batch of {len(batch)}: {symbols_list}")

    async def _resub_batch():
        for runner in batch:
            try:
                # Cancel existing
                if runner._ticker is not None:
                    try:
                        runner.ib.cancelMktData(runner.contract)
                    except Exception:
                        pass
                    runner._ticker = None
                    runner._subscription = None
                await runner.subscribe_async()
                log.info(f"[{runner.symbol}] Per-symbol resubscribed.")
            except Exception as e:
                log.error(f"[{runner.symbol}] Per-symbol resubscribe failed: {e}")
            await asyncio.sleep(0.5)

    if _main_loop:
        asyncio.run_coroutine_threadsafe(_resub_batch(), _main_loop)


def _run_heartbeat():
    """Broadcast status every 30 seconds so the dashboard always shows fresh data."""
    while True:
        time.sleep(30)
        now = datetime.now(EASTERN)
        with _lock:
            _status["last_heartbeat"] = now.strftime("%H:%M:%S")
            _status["market_open"] = (
                now.weekday() < 5 and
                now.hour * 100 + now.minute >= 930 and
                now.hour * 100 + now.minute < 1600
            )
            # Portfolio C regime info from SPY
            _status["regime"] = _get_regime_label()
            # Active books
            regime = _status["regime"]
            if regime.startswith("RED+TREND"):
                _status["active_books"] = "SHORT only (BDR)"
                _status["long_active"] = False
                _status["short_active"] = True
            elif regime.startswith("RED"):
                _status["active_books"] = "NONE (RED+CHOPPY)"
                _status["long_active"] = False
                _status["short_active"] = False
            else:
                _status["active_books"] = "LONG only (VK+SC)"
                _status["long_active"] = True
                _status["short_active"] = False

            # Count stale symbols for dashboard visibility
            now_mono = time.monotonic()
            stale_count = sum(
                1 for r in _runners
                if not r._removed and (
                    _last_bar_timestamps.get(r.symbol, 0) == 0 or
                    (now_mono - _last_bar_timestamps.get(r.symbol, 0)) > 300
                )
            )
            _status["stale_symbols"] = stale_count

        _broadcast_sse("status", _status)

        # Check for silently dead subscriptions during market hours
        try:
            _check_stale_subscriptions()
        except Exception as e:
            log.error(f"Stale check error: {e}")

        # Check per-symbol stale subscriptions (catches partial failures)
        try:
            _check_per_symbol_stale()
        except Exception as e:
            log.error(f"Per-symbol stale check error: {e}")


# ── Automatic session report on shutdown ──

def _print_session_report():
    """Print the session validation report using captured log lines."""
    try:
        from .session_report import analyze, format_report
        if not _session_log_lines:
            return
        report = analyze(_session_log_lines)
        report_text = format_report(report)
        print("\n")
        print(report_text)

        # Log file is already on disk (continuous flush). Save the report next to it.
        report_file = _session_log_file.with_suffix(".report.txt")
        report_file.write_text(report_text)

        print(f"\nLog:    {_session_log_file}")
        print(f"Report: {report_file}")
    except Exception as e:
        print(f"\n[session_report] Failed to generate report: {e}")


# ── Main ──

def main():
    global _ib, _cfg, _runners, _spy_engine, _qqq_engine, _spy_snap, _qqq_snap, _order_manager, _in_play_symbols, _spy_runner, _qqq_runner

    parser = argparse.ArgumentParser(description="Portfolio D — Live Dashboard")
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Symbols to monitor (overrides watchlist.txt)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497)
    parser.add_argument("--client-id", type=int, default=10)
    parser.add_argument("--no-ibkr", action="store_true",
                        help="Run dashboard without IBKR connection (offline/demo mode)")
    parser.add_argument("--deferred", action="store_true",
                        help="Start dashboard immediately, defer IBKR connection until user clicks Connect")
    parser.add_argument("--internal-port", type=int, default=None,
                        help="Override HTTP port (used by launcher for internal routing)")
    args = parser.parse_args()

    # Override dashboard port if running under the launcher
    global DASHBOARD_PORT
    if args.internal_port is not None:
        DASHBOARD_PORT = args.internal_port

    # Determine symbol list: CLI args > watchlist.txt > defaults
    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        symbols = _load_watchlist()
    if not symbols:
        symbols = ["AAPL", "TSLA", "TSLL", "SPY", "QQQ"]

    # Ensure SPY and QQQ are in the list (required for market context)
    for required in ["SPY", "QQQ"]:
        if required not in symbols:
            symbols.append(required)

    # Start web server and heartbeat in background
    web_thread = threading.Thread(target=_run_web_server, daemon=True)
    web_thread.start()
    heartbeat_thread = threading.Thread(target=_run_heartbeat, daemon=True)
    heartbeat_thread.start()
    log.info(f"Dashboard running at http://localhost:{DASHBOARD_PORT}")

    # Open browser (skip in no-ibkr mode or when running under launcher)
    if not args.no_ibkr and args.internal_port is None:
        import webbrowser
        webbrowser.open(f"http://localhost:{DASHBOARD_PORT}")

    cfg = OverlayConfig()
    _cfg = cfg

    # Load daily in-play universe (always, regardless of IBKR)
    _in_play_symbols = _load_in_play()
    if _in_play_symbols:
        log.info(f"Loaded {len(_in_play_symbols)} in-play symbols: {', '.join(_in_play_symbols[:10])}{'...' if len(_in_play_symbols) > 10 else ''}")
    with _lock:
        _status["in_play"] = sorted(_in_play_symbols)
        _status["started_at"] = datetime.now(EASTERN).strftime("%H:%M:%S")
    _refresh_ipl_in_status()  # Load in-play log for dashboard widget

    # ── Offline or Deferred mode ──
    if args.no_ibkr or args.deferred:
        mode_label = "DEFERRED" if args.deferred else "OFFLINE"
        log.info(f"Running in {mode_label} mode. Dashboard available immediately.")
        if args.deferred:
            log.info("Click 'Connect' in the dashboard when ready to start IBKR.")
        with _lock:
            _status["connected"] = False
            _status["mode"] = mode_label
            # Register ALL symbols (static + in-play) with placeholder status
            all_syms = list(symbols)
            for ip_sym in _in_play_symbols:
                if ip_sym not in all_syms:
                    all_syms.append(ip_sym)
            for sym in all_syms:
                in_play = sym in _in_play_symbols
                in_static = sym in symbols
                if in_static and in_play:
                    uni = "BOTH"
                elif in_play:
                    uni = "IN_PLAY"
                else:
                    uni = "STATIC"
                _status["symbols"][sym] = {
                    "bars": 0, "alerts": 0, "last_price": 0,
                    "universe": uni,
                }
            log.info(f"Registered {len(_status['symbols'])} symbols in {mode_label} mode")
            # Store connection params for deferred connect
            _status["_deferred_host"] = args.host
            _status["_deferred_port"] = args.port
            _status["_deferred_client_id"] = args.client_id
        _broadcast_sse("status", _status)
        log.info(f"Dashboard {mode_label} at http://localhost:{DASHBOARD_PORT}")

        if args.deferred:
            import webbrowser
            webbrowser.open(f"http://localhost:{DASHBOARD_PORT}")

        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            log.info("Shutting down...")
        _print_session_report()
        return

    # ── Live mode: connect to IBKR ──
    ib = IB()
    _ib = ib
    log.info(f"Connecting to IBKR at {args.host}:{args.port}...")
    ib.connect(args.host, args.port, clientId=args.client_id)
    log.info("Connected.")

    with _lock:
        _status["connected"] = True
        _status["mode"] = "LIVE"

    # Initialize order manager for bracket order placement (paper trading)
    _order_manager = OrderManager(ib, _broadcast_sse)
    log.info(f"Order manager ready (port {args.port}{'— PAPER' if args.port == 7497 else ''})")

    # ── Set up SPY/QQQ market context trackers FIRST ──
    # These must warm up before symbol runners so market context is available
    log.info("Setting up market context (SPY + QQQ)...")
    spy_tracker = MarketBarTracker("SPY", ib, cfg)
    qqq_tracker = MarketBarTracker("QQQ", ib, cfg)
    try:
        spy_tracker.setup()
        _spy_snap = spy_tracker.snapshot
        _spy_engine = spy_tracker.market_engine
    except Exception as e:
        log.error(f"[SPY] Market tracker failed: {e}")

    try:
        qqq_tracker.setup()
        _qqq_snap = qqq_tracker.snapshot
        _qqq_engine = qqq_tracker.market_engine
    except Exception as e:
        log.error(f"[QQQ] Market tracker failed: {e}")

    # ── Subscribe SPY/QQQ to reqMktData with regime update hooks ──
    # Wrap spy_tracker._on_tick to also recalculate regime on every tick
    _orig_spy_on_tick = spy_tracker._on_tick

    def _spy_tick_with_regime(ticker):
        global _spy_snap
        _orig_spy_on_tick(ticker)
        _spy_snap = spy_tracker.snapshot
        # Recalculate regime on every SPY tick
        with _lock:
            _status["regime"] = _get_regime_label()
            regime = _status["regime"]
            if regime.startswith("RED+TREND"):
                _status["active_books"] = "SHORT only (BDR)"
                _status["long_active"] = False
                _status["short_active"] = True
            elif regime.startswith("RED"):
                _status["active_books"] = "NONE (RED+CHOPPY)"
                _status["long_active"] = False
                _status["short_active"] = False
            else:
                _status["active_books"] = "LONG only (VK+SC)"
                _status["long_active"] = True
                _status["short_active"] = False
        _broadcast_sse("status", _status)

    spy_tracker._on_tick = _spy_tick_with_regime

    _orig_qqq_on_tick = qqq_tracker._on_tick

    def _qqq_tick_with_snap(ticker):
        global _qqq_snap
        _orig_qqq_on_tick(ticker)
        _qqq_snap = qqq_tracker.snapshot

    qqq_tracker._on_tick = _qqq_tick_with_snap

    try:
        spy_tracker.subscribe()
        log.info("[SPY] Market context subscribed (reqMktData)")
    except Exception as e:
        log.error(f"[SPY] Subscribe failed: {e}")

    try:
        qqq_tracker.subscribe()
        log.info("[QQQ] Market context subscribed (reqMktData)")
    except Exception as e:
        log.error(f"[QQQ] Subscribe failed: {e}")

    # Store trackers as globals so the stale-data watchdog can resubscribe them
    _spy_runner = spy_tracker
    _qqq_runner = qqq_tracker

    log.info(f"Regime: {_get_regime_label()}")

    # ── Pause briefly after SPY/QQQ before symbol setup ──
    log.info("Pausing 2s before symbol setup...")
    ib.sleep(2.0)

    # ── Also subscribe any in-play-only symbols not in static list ──
    static_set = set(symbols)
    for ip_sym in _in_play_symbols:
        if ip_sym not in static_set and ip_sym not in ("SPY", "QQQ"):
            symbols.append(ip_sym)

    # ── Set up symbol runners ──
    # setup() fires 1-2 IBKR historical-data requests per symbol:
    #   - Daily bars (always, 30D, small payload)
    #   - Intraday: either delta-only (cached symbols) or full 20D (new symbols)
    # Cached symbols need much less HMDS pacing since they only fetch a small delta.
    SETUP_PACE_CACHED = 0.3    # cached symbol: only daily + small delta
    SETUP_PACE_FULL   = 1.5    # new symbol: daily + full 20D intraday
    SUB_PACE   = 0.2   # reqMktData is lightweight — minimal pacing needed

    # Pre-check which symbols have cache to estimate total time
    cached_set = set()
    for sym in symbols:
        csv_path = _CACHE_DIR_1MIN / f"{sym}_1min.csv"
        if csv_path.exists():
            cached_set.add(sym)
    n_cached = len(cached_set)
    n_new = len(symbols) - n_cached
    est_time = n_cached * SETUP_PACE_CACHED + n_new * SETUP_PACE_FULL
    log.info(f"Starting setup: {n_cached} cached + {n_new} new symbols "
             f"(est. {est_time:.0f}s vs {len(symbols) * 1.5:.0f}s without cache)")

    for i, symbol in enumerate(symbols):
        try:
            universe = _get_universe(symbol)
            runner = SymbolRunner(symbol, ib, cfg, universe=universe)
            runner.setup()
            _runners.append(runner)
            log.info(f"[{symbol}] Ready ({i+1}/{len(symbols)}, universe={universe}).")
        except Exception as e:
            log.error(f"[{symbol}] Failed to set up: {e}")
        # Pace setup() calls — cached symbols need less pacing
        pace = SETUP_PACE_CACHED if symbol in cached_set else SETUP_PACE_FULL
        ib.sleep(pace)

    log.info(f"All {len(_runners)} runners set up. Starting reqMktData subscriptions...")

    # ── IBKR market data line budget ──
    # SPY + QQQ = 2 lines, each runner = 1 line
    total_lines = 2 + len(_runners)
    log.warning(f"MARKET DATA BUDGET: {total_lines} lines needed "
                f"(2 market + {len(_runners)} symbols). "
                f"Check your IBKR account's market data line limit "
                f"(usually 100, can be lower). Excess subscriptions will "
                f"be silently dropped by IBKR.")

    for i, runner in enumerate(_runners):
        try:
            runner.subscribe()
        except Exception as e:
            log.error(f"[{runner.symbol}] Failed to subscribe: {e}")
        # reqMktData is lightweight — minimal pacing needed
        ib.sleep(SUB_PACE)

    # NOTE: Do NOT save watchlist on startup — watchlist.txt is the source of
    # truth for static symbols.  Saving here could corrupt it if universe tags
    # are not yet fully set.  Watchlist is only saved when the user explicitly
    # adds/removes a static symbol via the dashboard UI.

    engine_label = "LEGACY (scan_day)" if _USE_LEGACY_ENGINE else "StrategyManager (step)"
    log.info(f"Portfolio D monitoring {len(_runners)}/{len(symbols)} symbols. "
             f"Engine: {engine_label}. "
             f"Dashboard: http://localhost:{DASHBOARD_PORT}")
    _broadcast_sse("status", _status)

    # Capture the main event loop so HTTP threads can schedule IB work on it
    global _main_loop
    _main_loop = asyncio.get_event_loop()
    log.info("Main event loop captured for cross-thread IB scheduling.")

    try:
        ib.run()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        if ib.isConnected():
            ib.disconnect()
        log.info("Disconnected.")
        _print_session_report()


if __name__ == "__main__":
    main()
