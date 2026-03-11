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
from .engine import SignalEngine
from .market_context import MarketEngine, MarketContext, MarketSnapshot, compute_market_context
from .layered_regime import PermissionWeights, compute_permission

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dashboard")

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
_spy_engine: Optional[MarketEngine] = None
_qqq_engine: Optional[MarketEngine] = None
_spy_snap = None
_qqq_snap = None
_spy_runner = None  # SymbolRunner for SPY bar subscription
_qqq_runner = None  # SymbolRunner for QQQ bar subscription

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
        if price <= 0:
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


class MarketBarTracker:
    """Subscribes to SPY/QQQ via reqMktData and updates shared MarketEngine snapshots.

    Uses reqMktData (streaming Level 1 quotes) instead of keepUpToDate to avoid
    IBKR's concurrent historical-data subscription limit (~50 streams).
    Builds 5-min bars locally from tick data using BarAggregator.
    """

    def __init__(self, symbol: str, ib: IB, cfg: OverlayConfig):
        self.symbol = symbol
        self.ib = ib
        self.cfg = cfg
        self.market_engine = MarketEngine()
        self.contract = Stock(symbol, "SMART", "USD")
        self.bar_size_str = _IBKR_BAR_SIZE_MAP[cfg.bar_interval_minutes]
        self._aggregator = BarAggregator(cfg.bar_interval_minutes)
        self._ticker = None       # ib_insync Ticker from reqMktData
        self._subscription = None  # kept for compat with watchdog
        self.snapshot = None
        self._bars_completed = 0

    def setup(self):
        """Qualify contract and warm up with historical bars."""
        self.ib.qualifyContracts(self.contract)
        log.info(f"[MKT:{self.symbol}] Contract qualified")

        # Intraday catch-up (one-shot, no keepUpToDate)
        intraday_bars = self.ib.reqHistoricalData(
            self.contract, endDateTime="", durationStr="1 D",
            barSizeSetting=self.bar_size_str, whatToShow="TRADES",
            useRTH=True, formatDate=1,
        )
        if intraday_bars:
            for ib_bar in intraday_bars:
                bar = self._convert_bar(ib_bar)
                self.snapshot = self.market_engine.process_bar(bar)
            log.info(f"[MKT:{self.symbol}] Caught up with {len(intraday_bars)} bars")

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
        """Called on every tick from reqMktData."""
        price = getattr(ticker, 'last', None) or getattr(ticker, 'close', None)
        if price is None or price <= 0:
            # Try marketPrice as fallback
            price = getattr(ticker, 'marketPrice', None)
            if price is None or price != price or price <= 0:  # NaN check
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

        # Feed aggregator — returns a completed bar if boundary crossed
        completed_bar = self._aggregator.on_tick(price, cum_vol, ts)
        if completed_bar is not None:
            self.snapshot = self.market_engine.process_bar(completed_bar)
            self._bars_completed += 1
            log.info(f"[MKT:{self.symbol}] Bar #{self._bars_completed} closed "
                     f"@ {completed_bar.timestamp.strftime('%H:%M')} C={completed_bar.close:.2f}")

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


class SymbolRunner:
    """Manages one symbol's streaming subscription and signal engine.

    Uses reqMktData (Level 1 quotes) instead of keepUpToDate to avoid
    IBKR's concurrent historical-data subscription limit (~50 streams).
    Builds 5-min bars locally from tick data using BarAggregator.
    """

    def __init__(self, symbol: str, ib: IB, cfg: OverlayConfig, universe: str = "STATIC"):
        self.symbol = symbol
        self.universe = universe
        self.ib = ib
        self.cfg = cfg
        self.engine = SignalEngine(cfg, universe_source=universe)
        self.contract = Stock(symbol, "SMART", "USD")
        self.bar_size_str = _IBKR_BAR_SIZE_MAP[cfg.bar_interval_minutes]
        self._aggregator = BarAggregator(cfg.bar_interval_minutes)
        self._ticker = None       # ib_insync Ticker from reqMktData
        self._bars_received = 0
        self._subscription = None  # kept for compat with watchdog
        self._removed = False  # kill switch: stops callbacks from firing after removal
        self._day_open_for_rs: float = float('nan')
        self._rs_date: Optional[int] = None

    def setup(self):
        """Qualify contract and warm up engine."""
        self.ib.qualifyContracts(self.contract)
        log.info(f"[{self.symbol}] Contract qualified: {self.contract}")

        # Daily bars for ATR
        daily_bars = self.ib.reqHistoricalData(
            self.contract, endDateTime="", durationStr="30 D",
            barSizeSetting="1 day", whatToShow="TRADES",
            useRTH=True, formatDate=1,
        )
        if daily_bars:
            daily_history = [
                {"high": b.high, "low": b.low, "close": b.close}
                for b in daily_bars
            ]
            self.engine.set_daily_atr_history(daily_history)
            last_day = daily_bars[-1]
            self.engine.set_prior_day(last_day.high, last_day.low)
            log.info(f"[{self.symbol}] ATR warmed: {len(daily_bars)} days, "
                     f"PD H:{last_day.high:.2f} L:{last_day.low:.2f}")

        # Intraday catch-up
        intraday_bars = self.ib.reqHistoricalData(
            self.contract, endDateTime="", durationStr="1 D",
            barSizeSetting=self.bar_size_str, whatToShow="TRADES",
            useRTH=True, formatDate=1,
        )
        if intraday_bars:
            for ib_bar in intraday_bars:
                bar = self._convert_bar(ib_bar)
                market_ctx = self._build_market_ctx(bar)
                self.engine.process_bar(bar, market_ctx=market_ctx)
                self._bars_received += 1
            log.info(f"[{self.symbol}] Caught up with {len(intraday_bars)} bars")

        with _lock:
            _status["symbols"][self.symbol] = {
                "bars": self._bars_received,
                "alerts": 0,
                "last_price": intraday_bars[-1].close if intraday_bars else 0,
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
            daily_history = [
                {"high": b.high, "low": b.low, "close": b.close}
                for b in daily_bars
            ]
            self.engine.set_daily_atr_history(daily_history)
            last_day = daily_bars[-1]
            self.engine.set_prior_day(last_day.high, last_day.low)
            log.info(f"[{self.symbol}] ATR warmed: {len(daily_bars)} days, "
                     f"PD H:{last_day.high:.2f} L:{last_day.low:.2f}")

        intraday_bars = await self.ib.reqHistoricalDataAsync(
            self.contract, endDateTime="", durationStr="1 D",
            barSizeSetting=self.bar_size_str, whatToShow="TRADES",
            useRTH=True, formatDate=1,
        )
        if intraday_bars:
            for ib_bar in intraday_bars:
                bar = self._convert_bar(ib_bar)
                market_ctx = self._build_market_ctx(bar)
                self.engine.process_bar(bar, market_ctx=market_ctx)
                self._bars_received += 1
            log.info(f"[{self.symbol}] Caught up with {len(intraday_bars)} bars")

        with _lock:
            _status["symbols"][self.symbol] = {
                "bars": self._bars_received,
                "alerts": 0,
                "last_price": intraday_bars[-1].close if intraday_bars else 0,
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
        """Called on every tick from reqMktData. Builds bars via BarAggregator."""
        if self._removed:
            return

        price = getattr(ticker, 'last', None) or getattr(ticker, 'close', None)
        if price is None or price <= 0:
            price = getattr(ticker, 'marketPrice', None)
            if price is None or price != price or price <= 0:  # NaN check
                return

        # Record that this runner is receiving data (for stale-data watchdog)
        _last_bar_timestamps[self.symbol] = time.monotonic()

        # Always update the live price
        with _lock:
            sym_status = _status.get("symbols", {}).get(self.symbol)
            if sym_status is not None:
                sym_status["last_price"] = round(price, 2)

        ts = datetime.now(EASTERN)
        cum_vol = getattr(ticker, 'volume', 0) or 0

        # Feed aggregator — returns a completed bar if boundary crossed
        completed_bar = self._aggregator.on_tick(price, cum_vol, ts)
        if completed_bar is None:
            return  # still forming — nothing more to do

        # ── New bar completed — process through signal engine ──
        self._bars_received += 1
        market_ctx = self._build_market_ctx(completed_bar)
        signals = self.engine.process_bar(completed_bar, market_ctx=market_ctx)

        # Diagnostic: log every 10th bar per symbol + any bar that produces signals
        if self._bars_received % 10 == 0 or signals:
            log.info(f"[{self.symbol}] bar #{self._bars_received} "
                     f"@ {completed_bar.timestamp.strftime('%H:%M') if completed_bar.timestamp else '?'} "
                     f"C={completed_bar.close:.2f} | signals={len(signals)}")

        with _lock:
            _status["symbols"][self.symbol]["bars"] = self._bars_received

        for sig in signals:
            # ── Portfolio D Variant 2: tape permission gate for longs ──
            if sig.direction == 1 and market_ctx is not None:
                h = (completed_bar.timestamp.hour * 100 + completed_bar.timestamp.minute
                     if completed_bar.timestamp else 1000)
                perm_reading = compute_permission(
                    market_ctx, direction=1, bar_time_hhmm=h,
                    weights=_TAPE_WEIGHTS)
                if perm_reading.permission < _LONG_TAPE_THRESHOLD:
                    log.info(f"[{self.symbol}] BLOCKED long {sig.setup_name}: "
                             f"tape perm={perm_reading.permission:+.3f} < {_LONG_TAPE_THRESHOLD}")
                    continue  # skip this signal

            alert_data = _signal_to_dict(sig, self.symbol)
            # Add tape permission to alert data for display
            if sig.direction == 1 and market_ctx is not None:
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
        elif path == "/api/clear-in-play":
            self._clear_in_play(data)
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
        with _lock:
            _status["symbols"][symbol] = {"bars": 0, "alerts": 0, "last_price": 0, "warming_up": True}
        _broadcast_sse("status", _status)

        # Schedule IB work on the main event loop (ib_insync requires it)
        async def _setup_and_subscribe():
            global _ib, _cfg, _runners
            try:
                universe = _get_universe(symbol)
                runner = SymbolRunner(symbol, _ib, _cfg, universe=universe)
                await runner.setup_async()
                await runner.subscribe_async()
                with _lock:
                    _runners.append(runner)
                    _status["symbols"][symbol].pop("warming_up", None)
                    _status["symbols"][symbol]["universe"] = universe
                _save_watchlist()
                _broadcast_sse("status", _status)
                log.info(f"[{symbol}] Added to watchlist (universe={universe}).")
            except Exception as e:
                log.error(f"[{symbol}] Failed to add: {e}")
                with _lock:
                    _status["symbols"].pop(symbol, None)
                _broadcast_sse("status", _status)

        _schedule_on_main(lambda: _setup_and_subscribe())
        self._send_json_response(200, {"ok": True, "symbol": symbol, "message": f"Adding {symbol}..."})

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

            _schedule_on_main(lambda: _unsub())
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
                    runner.engine._universe_source = "BOTH"
                    break

        with _lock:
            _status["in_play"] = sorted(_in_play_symbols)
        _broadcast_sse("in_play_updated", {
            "symbols": sorted(_in_play_symbols),
            "action": "ADD", "symbol": symbol,
        })
        _broadcast_sse("status", _status)
        self._send_json_response(200, {"ok": True, "symbol": symbol})

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
                    runner.engine._universe_source = "STATIC"
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
        """Clear entire in-play list (morning reset)."""
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
                # Reset BOTH → STATIC
                with _lock:
                    if symbol in _status.get("symbols", {}):
                        _status["symbols"][symbol]["universe"] = "STATIC"
                for runner in _runners:
                    if runner.symbol == symbol:
                        runner.universe = "STATIC"
                        runner.engine._universe_source = "STATIC"
                        break

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
        _broadcast_sse("in_play_updated", {
            "symbols": [],
            "action": "CLEAR", "cleared": symbols_to_clear,
        })
        _broadcast_sse("status", _status)
        self._send_json_response(200, {"ok": True, "cleared": len(symbols_to_clear)})

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
        _broadcast_sse("status", _status)

        # Check for silently dead subscriptions during market hours
        try:
            _check_stale_subscriptions()
        except Exception as e:
            log.error(f"Stale check error: {e}")


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
    args = parser.parse_args()

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

    # Open browser (skip in no-ibkr mode to avoid clutter)
    if not args.no_ibkr:
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

    # ── Offline/demo mode: no IBKR, but HTTP server + in-play panel fully functional ──
    if args.no_ibkr:
        log.info("Running in OFFLINE mode (--no-ibkr). No market data.")
        log.info("In-play panel, test alerts, history capture, and export all operational.")
        with _lock:
            _status["connected"] = False
            _status["mode"] = "OFFLINE"
            # Register ALL symbols (static + in-play) with placeholder status
            all_syms = list(symbols)  # static watchlist + SPY/QQQ
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
            log.info(f"Registered {len(_status['symbols'])} symbols in offline mode")
        _broadcast_sse("status", _status)
        log.info(f"Dashboard OFFLINE at http://localhost:{DASHBOARD_PORT} with {len(symbols)} symbols")
        try:
            # Keep the main thread alive so HTTP daemon threads stay up
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            log.info("Shutting down offline dashboard...")
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
    # setup() fires 2 historical-data requests (daily + intraday) per symbol.
    # subscribe() fires 1 reqMktData request (lightweight, no HMDS pacing concern).
    # We still pace setup() to avoid HMDS pacing violations.
    SETUP_PACE = 1.5   # seconds between each setup() call
    SUB_PACE   = 0.2   # reqMktData is lightweight — minimal pacing needed

    for i, symbol in enumerate(symbols):
        try:
            universe = _get_universe(symbol)
            runner = SymbolRunner(symbol, ib, cfg, universe=universe)
            runner.setup()
            _runners.append(runner)
            log.info(f"[{symbol}] Ready ({i+1}/{len(symbols)}, universe={universe}).")
        except Exception as e:
            log.error(f"[{symbol}] Failed to set up: {e}")
        # Pace setup() calls — HMDS historical data pacing still applies
        ib.sleep(SETUP_PACE)

    log.info(f"All {len(_runners)} runners set up. Starting reqMktData subscriptions...")

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

    log.info(f"Portfolio D monitoring {len(_runners)}/{len(symbols)} symbols. "
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


if __name__ == "__main__":
    main()
