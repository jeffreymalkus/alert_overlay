"""
IBKR Live Connection — Real-time bar subscription and alert generation.

Uses ib_insync for async-friendly TWS API interaction.
Connects to TWS or IB Gateway, subscribes to real-time bars at the
configured interval, feeds them to the SignalEngine, and emits alerts.

Dual-timeframe architecture:
  - Always pulls 1-min bars from IBKR for maximum resolution
  - Upsamples 1-min → 5-min for existing strategy engine
  - 1-min bars available for fine-grained strategies (EMA9_FT, BS_STRUCT)

Requirements:
    pip install ib_insync

Usage:
    python -m alert_overlay.ibkr_live --symbol SPY --host 127.0.0.1 --port 7497
"""

import argparse
import logging
import math
import sys
from datetime import datetime, timedelta, date
from typing import Optional, List
from zoneinfo import ZoneInfo

try:
    from ib_insync import (
        IB, Stock, Contract, BarData,
        util,
    )
except ImportError:
    print("ERROR: ib_insync not installed. Run: pip install ib_insync")
    sys.exit(1)

from .config import OverlayConfig
from .models import Bar, Signal
from .engine import SignalEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ibkr_live")


# IBKR valid barSizeSetting strings for reqHistoricalData
_IBKR_BAR_SIZE_MAP = {
    1: "1 min",
    2: "2 mins",
    3: "3 mins",
    5: "5 mins",
    10: "10 mins",
    15: "15 mins",
    20: "20 mins",
    30: "30 mins",
    60: "1 hour",
}

BASE_BAR_MINUTES = 1    # always pull 1-min from IBKR
ENGINE_BAR_MINUTES = 5  # engine expects 5-min bars

EASTERN = ZoneInfo("US/Eastern")


def _ibkr_bar_size(minutes: int) -> str:
    """Convert bar_interval_minutes to IBKR barSizeSetting string."""
    if minutes in _IBKR_BAR_SIZE_MAP:
        return _IBKR_BAR_SIZE_MAP[minutes]
    raise ValueError(
        f"bar_interval_minutes={minutes} is not a valid IBKR bar size. "
        f"Valid values: {sorted(_IBKR_BAR_SIZE_MAP.keys())}"
    )


class BarUpsampler:
    """Aggregates completed 1-min Bar objects into 5-min (or any N-min) bars.

    Identical to dashboard.BarUpsampler — duplicated here so ibkr_live.py
    can run standalone without importing the dashboard module.
    """

    def __init__(self, target_minutes: int = 5):
        self.interval = target_minutes
        self._buf: List[Bar] = []
        self._boundary: Optional[datetime] = None

    def _bar_boundary(self, ts: datetime) -> datetime:
        minute = (ts.minute // self.interval) * self.interval
        return ts.replace(minute=minute, second=0, microsecond=0)

    def on_bar(self, bar: Bar) -> Optional[Bar]:
        boundary = self._bar_boundary(bar.timestamp)
        if self._boundary is None:
            self._boundary = boundary
            self._buf = [bar]
            return None
        if boundary == self._boundary:
            self._buf.append(bar)
            return None
        completed = self._emit()
        self._boundary = boundary
        self._buf = [bar]
        return completed

    def _emit(self) -> Optional[Bar]:
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
        if not self._buf:
            return None
        completed = self._emit()
        self._buf = []
        self._boundary = None
        return completed


class AlertHandler:
    """Handles signal output — console, sound, desktop notifications, CSV log.

    Alert format matches TOS alert log strings exactly so you can compare
    side-by-side during validation.
    """

    def __init__(self, log_dir: Optional[str] = None):
        self._log_dir = log_dir
        self._csv_file = None
        self._csv_writer = None
        if log_dir:
            import os, csv as csvmod
            os.makedirs(log_dir, exist_ok=True)
            today = datetime.now(EASTERN).strftime("%Y%m%d")
            path = os.path.join(log_dir, f"alerts_{today}.csv")
            self._csv_file = open(path, "a", newline="")
            self._csv_writer = csvmod.writer(self._csv_file)
            # Write header if file is empty
            if os.path.getsize(path) == 0:
                self._csv_writer.writerow([
                    "timestamp", "direction", "setup", "family", "sound",
                    "entry", "stop", "target", "risk", "reward", "rr",
                    "quality", "regime_fit", "vwap_bias", "or_dir",
                    "confluence", "sweeps"
                ])

    def on_signal(self, signal: Signal):
        """Called when a new signal fires."""

        # ── TOS-format alert string (matches alert log export) ──
        tos_str = signal.to_tos_alert_string()
        conf_str = " ".join(signal.confluence_tags) or "NONE"
        sweep_str = " ".join(signal.sweep_tags) or "NONE"

        msg = (
            f"\n{'=' * 70}\n"
            f"  ALERT: {tos_str}\n"
            f"  CONF: {conf_str}  |  SWEEP: {sweep_str}\n"
            f"  TIME: {signal.timestamp}  |  SOUND: {signal.sound}\n"
            f"{'=' * 70}"
        )
        log.info(msg)

        # ── CSV log for trade journal / analysis ──
        if self._csv_writer:
            self._csv_writer.writerow([
                signal.timestamp,
                "LONG" if signal.direction == 1 else "SHORT",
                signal.setup_name,
                signal.family.name,
                signal.sound,
                f"{signal.entry_price:.2f}",
                f"{signal.stop_price:.2f}",
                f"{signal.target_price:.2f}",
                f"{signal.risk:.4f}",
                f"{signal.reward:.4f}",
                f"{signal.rr_ratio:.2f}",
                signal.quality_score,
                "R" if signal.fits_regime else "r",
                signal.vwap_bias,
                signal.or_direction,
                conf_str,
                sweep_str,
            ])
            self._csv_file.flush()

        # ── Desktop notification (macOS) — non-blocking ──
        # Uses the family-specific sound name from TOS (Glass as fallback)
        dir_str = "LONG" if signal.direction == 1 else "SHORT"
        try:
            import subprocess
            subprocess.Popen([
                "osascript", "-e",
                f'display notification "{tos_str}" '
                f'with title "Alert: {dir_str} {signal.setup_name}" '
                f'sound name "Glass"'
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        # ── Terminal bell ──
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass

    def close(self):
        """Flush and close the CSV log."""
        if self._csv_file:
            self._csv_file.close()


class IBKRLiveRunner:
    """Manages the IBKR connection and real-time bar processing.

    Dual-timeframe architecture:
      - Always pulls 1-min bars from IBKR for maximum resolution
      - Upsamples 1-min → 5-min for existing strategy engine
      - 1-min bars available for fine-grained strategies (EMA9_FT, BS_STRUCT)
    """

    def __init__(self, symbol: str, host: str = "127.0.0.1", port: int = 7497,
                 client_id: int = 1, cfg: Optional[OverlayConfig] = None,
                 log_dir: Optional[str] = None):
        self.symbol = symbol
        self.host = host
        self.port = port
        self.client_id = client_id
        self.cfg = cfg or OverlayConfig()
        self.bar_size_str = _ibkr_bar_size(ENGINE_BAR_MINUTES)
        self.bar_seconds = BASE_BAR_MINUTES * 60  # aggregate 5-sec → 1-min
        self.ib = IB()
        self.engine = SignalEngine(self.cfg)
        self.alert_handler = AlertHandler(log_dir=log_dir)
        self.contract: Optional[Contract] = None
        self._bars_received = 0
        # Upsampler: 1-min → 5-min for engine
        self._upsampler = BarUpsampler(ENGINE_BAR_MINUTES)

    def connect(self):
        """Connect to TWS/Gateway."""
        log.info(f"Connecting to IBKR at {self.host}:{self.port} (client {self.client_id})...")
        self.ib.connect(self.host, self.port, clientId=self.client_id)
        log.info("Connected.")

    def setup_contract(self):
        """Create and qualify the stock contract."""
        self.contract = Stock(self.symbol, "SMART", "USD")
        self.ib.qualifyContracts(self.contract)
        log.info(f"Contract qualified: {self.contract}")

    def warm_up(self):
        """Pull historical data to warm up indicators (daily ATR, prior-day levels)."""
        log.info("Warming up with historical data...")

        # Daily bars for ATR (last 30 trading days)
        daily_bars = self.ib.reqHistoricalData(
            self.contract,
            endDateTime="",
            durationStr="30 D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )

        if daily_bars:
            daily_history = [
                {"high": b.high, "low": b.low, "close": b.close}
                for b in daily_bars
            ]
            self.engine.set_daily_atr_history(daily_history)

            # Prior day high/low from the last completed daily bar
            last_day = daily_bars[-1]
            self.engine.set_prior_day(last_day.high, last_day.low)
            log.info(f"Daily ATR warmed up with {len(daily_bars)} days. "
                     f"Prior day H:{last_day.high:.2f} L:{last_day.low:.2f}")

        # Today's intraday bars — pull 1-min and aggregate to 5-min
        intraday_bars = self.ib.reqHistoricalData(
            self.contract,
            endDateTime="",
            durationStr="1 D",
            barSizeSetting=_ibkr_bar_size(BASE_BAR_MINUTES),
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )

        bars_1min_count = 0
        if intraday_bars:
            log.info(f"Processing {len(intraday_bars)} historical 1-min bars...")
            for ib_bar in intraday_bars:
                bar_1min = self._convert_bar(ib_bar)
                bars_1min_count += 1

                # TODO: process bar_1min through 1-min strategies when wired

                # Aggregate 1-min → 5-min for engine
                bar_5min = self._upsampler.on_bar(bar_1min)
                if bar_5min is not None:
                    signals = self.engine.process_bar(bar_5min)
                    self._bars_received += 1
                    # Don't alert on historical bars — just warm up state

            # Flush any remaining partial 5-min bar at end of catch-up
            final_5min = self._upsampler.flush()
            if final_5min is not None:
                self.engine.process_bar(final_5min)
                self._bars_received += 1

            log.info(f"Caught up: {bars_1min_count} 1-min bars → "
                     f"{self._bars_received} 5-min bars. Engine state warm.")

    def subscribe_realtime(self):
        """Subscribe to real-time bars via 5-sec aggregation.

        Dual-timeframe flow:
          5-sec native bars → aggregate to 1-min → upsampler → 5-min → engine
        """
        log.info(f"Subscribing to real-time 1-min bars for {self.symbol} "
                 f"(aggregating from 5-sec bars, upsampling to 5-min)...")

        bars = self.ib.reqRealTimeBars(
            self.contract,
            barSize=5,  # IBKR only supports 5-sec real-time bars natively
            whatToShow="TRADES",
            useRTH=True,
        )
        # Aggregate 5-sec bars into 1-min bars first
        self._rt_bar_agg = []
        self._rt_agg_start = None
        bars.updateEvent += self._on_realtime_bar

        log.info("Subscribed. Waiting for bars...")

    def _on_realtime_bar(self, bars, hasNewBar):
        """Callback for each 5-second real-time bar.

        Aggregates 5-sec → 1-min, then upsamples 1-min → 5-min for engine.
        """
        if not hasNewBar or not bars:
            return

        latest = bars[-1]
        now = datetime.fromtimestamp(latest.time, tz=EASTERN)

        if self._rt_agg_start is None:
            self._rt_agg_start = now
            self._rt_bar_agg = []

        self._rt_bar_agg.append(latest)

        # Check if 1-min interval has elapsed
        elapsed = (now - self._rt_agg_start).total_seconds()
        if elapsed >= self.bar_seconds:
            # Build the 1-min bar from 5-sec bars
            agg_open = self._rt_bar_agg[0].open
            agg_high = max(b.high for b in self._rt_bar_agg)
            agg_low = min(b.low for b in self._rt_bar_agg)
            agg_close = self._rt_bar_agg[-1].close
            agg_volume = sum(b.volume for b in self._rt_bar_agg)

            bar_1min = Bar(
                timestamp=self._rt_agg_start,
                open=agg_open,
                high=agg_high,
                low=agg_low,
                close=agg_close,
                volume=agg_volume,
            )

            # TODO: process bar_1min through 1-min strategies when wired

            # Upsample 1-min → 5-min for engine
            bar_5min = self._upsampler.on_bar(bar_1min)
            if bar_5min is not None:
                self._bars_received += 1
                signals = self.engine.process_bar(bar_5min)

                for sig in signals:
                    self.alert_handler.on_signal(sig)

            # Reset 1-min aggregation
            self._rt_bar_agg = []
            self._rt_agg_start = None

    def _convert_bar(self, ib_bar) -> Bar:
        """Convert an ib_insync BarData to our Bar model.

        All timestamps are normalized to US/Eastern so that session
        time checks (0930, 0945, 1400, etc.) work correctly regardless
        of the system's local timezone or deployment location.
        """
        if hasattr(ib_bar, 'date'):
            if isinstance(ib_bar.date, str):
                try:
                    ts = datetime.strptime(ib_bar.date, "%Y%m%d  %H:%M:%S")
                except ValueError:
                    ts = datetime.strptime(ib_bar.date, "%Y%m%d")
                # IBKR historical strings are exchange-local (Eastern for US equities)
                # but naive — attach the timezone explicitly
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=EASTERN)
            else:
                # datetime object — convert to Eastern if tz-aware, attach if naive
                if ib_bar.date.tzinfo is not None:
                    ts = ib_bar.date.astimezone(EASTERN)
                else:
                    ts = ib_bar.date.replace(tzinfo=EASTERN)
        else:
            ts = datetime.now(EASTERN)

        return Bar(
            timestamp=ts,
            open=ib_bar.open,
            high=ib_bar.high,
            low=ib_bar.low,
            close=ib_bar.close,
            volume=ib_bar.volume if hasattr(ib_bar, 'volume') else 0,
        )

    def run(self):
        """Main loop — connect, warm up, subscribe, run forever."""
        try:
            self.connect()
            self.setup_contract()
            self.warm_up()
            self.subscribe_realtime()

            log.info("Running. Press Ctrl+C to stop.")
            self.ib.run()

        except KeyboardInterrupt:
            log.info("Shutting down...")
        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)
        finally:
            self.alert_handler.close()
            if self.ib.isConnected():
                self.ib.disconnect()
            log.info("Disconnected.")


class IBKRHistoricalLiveRunner(IBKRLiveRunner):
    """
    Uses reqHistoricalData with keepUpToDate=True for cleaner bar delivery.
    This avoids manual aggregation of 5-second bars.
    Requires TWS API v9.73+.

    Dual-timeframe: requests 1-min bars with keepUpToDate, upsamples to 5-min.
    """

    def subscribe_realtime(self):
        """Subscribe using keepUpToDate 1-min historical bars."""
        base_bar_str = _ibkr_bar_size(BASE_BAR_MINUTES)
        log.info(f"Subscribing to updating {base_bar_str} bars for {self.symbol} "
                 f"(upsampling to {self.bar_size_str})...")

        bars = self.ib.reqHistoricalData(
            self.contract,
            endDateTime="",
            durationStr="1 D",
            barSizeSetting=base_bar_str,
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
            keepUpToDate=True,
        )

        # Set up the update callback for new bars
        bars.updateEvent += self._on_historical_update
        self._last_bar_count = len(bars)

        log.info(f"Subscribed with keepUpToDate ({base_bar_str}). "
                 f"{len(bars)} initial bars. Waiting for updates...")

    def _on_historical_update(self, bars, hasNewBar):
        """Called when a new 1-min bar completes. Upsamples to 5-min for engine."""
        if hasNewBar and len(bars) > self._last_bar_count:
            latest = bars[-1]
            bar_1min = self._convert_bar(latest)

            # TODO: process bar_1min through 1-min strategies when wired

            # Upsample 1-min → 5-min for engine
            bar_5min = self._upsampler.on_bar(bar_1min)
            if bar_5min is not None:
                self._bars_received += 1
                signals = self.engine.process_bar(bar_5min)
                for sig in signals:
                    self.alert_handler.on_signal(sig)

            self._last_bar_count = len(bars)


# ─── CLI ───
def main():
    parser = argparse.ArgumentParser(description="Alert Overlay v4.5 — IBKR Live (1-min → 5-min)")
    parser.add_argument("--symbol", required=True, help="Ticker symbol (e.g., SPY, QQQ, AAPL)")
    parser.add_argument("--host", default="127.0.0.1", help="TWS/Gateway host")
    parser.add_argument("--port", type=int, default=7497, help="TWS port (7497=paper, 7496=live)")
    parser.add_argument("--client-id", type=int, default=1, help="API client ID")
    parser.add_argument("--mode", choices=["realtime", "historical"], default="historical",
                        help="Bar subscription mode (historical=keepUpToDate, realtime=5sec agg)")
    parser.add_argument("--log-dir", default="./alert_logs",
                        help="Directory for CSV alert logs (default: ./alert_logs)")

    args = parser.parse_args()

    cfg = OverlayConfig()
    # Engine stays at 5-min; base bars are always 1-min now

    if args.mode == "historical":
        runner = IBKRHistoricalLiveRunner(
            symbol=args.symbol, host=args.host,
            port=args.port, client_id=args.client_id,
            cfg=cfg, log_dir=args.log_dir)
    else:
        runner = IBKRLiveRunner(
            symbol=args.symbol, host=args.host,
            port=args.port, client_id=args.client_id,
            cfg=cfg, log_dir=args.log_dir)

    runner.run()


if __name__ == "__main__":
    main()
