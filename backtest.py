"""
Backtest harness — runs the signal engine over historical bar data
and produces trade-level statistics.

Data sources:
  1. CSV file (exported from TOS, IBKR, or any provider)
  2. IBKR historical data pull (via ib_insync)

CSV expected columns: datetime, open, high, low, close, volume
  - datetime format: YYYY-MM-DD HH:MM:SS (or configurable)
"""

import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .config import OverlayConfig
from .models import Bar, Signal, SetupId, SetupFamily, SETUP_FAMILY_MAP, NaN
from .engine import SignalEngine
from .market_context import MarketEngine, MarketContext, MarketTrend, compute_market_context, get_sector_etf


@dataclass
class Trade:
    """A completed trade from signal to exit."""
    signal: Signal
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    exit_reason: str = ""  # "target", "stop", "eod", "opposing"
    pnl_points: float = 0.0
    pnl_rr: float = 0.0
    bars_held: int = 0


@dataclass
class BacktestResult:
    trades: List[Trade] = field(default_factory=list)
    signals_total: int = 0

    @property
    def wins(self) -> List[Trade]:
        return [t for t in self.trades if t.pnl_points > 0]

    @property
    def losses(self) -> List[Trade]:
        return [t for t in self.trades if t.pnl_points <= 0]

    @property
    def win_rate(self) -> float:
        return len(self.wins) / len(self.trades) * 100 if self.trades else 0.0

    @property
    def avg_win(self) -> float:
        w = self.wins
        return sum(t.pnl_points for t in w) / len(w) if w else 0.0

    @property
    def avg_loss(self) -> float:
        l = self.losses
        return sum(t.pnl_points for t in l) / len(l) if l else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl_points for t in self.trades)

    @property
    def avg_rr_realized(self) -> float:
        return sum(t.pnl_rr for t in self.trades) / len(self.trades) if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        gross_win = sum(t.pnl_points for t in self.wins)
        gross_loss = abs(sum(t.pnl_points for t in self.losses))
        return gross_win / gross_loss if gross_loss > 0 else float('inf')

    @property
    def max_drawdown(self) -> float:
        """Max drawdown in points from cumulative equity curve."""
        if not self.trades:
            return 0.0
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in self.trades:
            cum += t.pnl_points
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def summary(self) -> str:
        lines = [
            "═" * 55,
            "  BACKTEST RESULTS",
            "═" * 55,
            f"  Total signals:     {self.signals_total}",
            f"  Total trades:      {len(self.trades)}",
            f"  Wins:              {len(self.wins)}",
            f"  Losses:            {len(self.losses)}",
            f"  Win rate:          {self.win_rate:.1f}%",
            f"  Avg win (pts):     {self.avg_win:.4f}",
            f"  Avg loss (pts):    {self.avg_loss:.4f}",
            f"  Avg R:R realized:  {self.avg_rr_realized:.2f}",
            f"  Profit factor:     {self.profit_factor:.2f}",
            f"  Total PnL (pts):   {self.total_pnl:.4f}",
            f"  Max drawdown (pts):{self.max_drawdown:.4f}",
            "═" * 55,
        ]

        # Breakdown by setup type
        from collections import Counter
        setup_counts = Counter()
        setup_wins = Counter()
        setup_pnl = Counter()
        for t in self.trades:
            name = t.signal.setup_id.name
            setup_counts[name] += 1
            if t.pnl_points > 0:
                setup_wins[name] += 1
            setup_pnl[name] += t.pnl_points

        lines.append("  BY SETUP TYPE:")
        lines.append(f"  {'Setup':<15} {'Trades':>7} {'WinRate':>8} {'PnL':>10}")
        lines.append("  " + "-" * 42)
        for name in sorted(setup_counts.keys()):
            cnt = setup_counts[name]
            wr = setup_wins[name] / cnt * 100 if cnt else 0
            pnl = setup_pnl[name]
            lines.append(f"  {name:<15} {cnt:>7} {wr:>7.1f}% {pnl:>10.4f}")

        # Breakdown by direction
        long_trades = [t for t in self.trades if t.signal.direction == 1]
        short_trades = [t for t in self.trades if t.signal.direction == -1]
        lines.append("")
        lines.append(f"  Long trades:  {len(long_trades)} | "
                     f"Win rate: {sum(1 for t in long_trades if t.pnl_points > 0)/len(long_trades)*100 if long_trades else 0:.1f}% | "
                     f"PnL: {sum(t.pnl_points for t in long_trades):.4f}")
        lines.append(f"  Short trades: {len(short_trades)} | "
                     f"Win rate: {sum(1 for t in short_trades if t.pnl_points > 0)/len(short_trades)*100 if short_trades else 0:.1f}% | "
                     f"PnL: {sum(t.pnl_points for t in short_trades):.4f}")
        lines.append("═" * 55)

        return "\n".join(lines)

    def trade_log(self) -> str:
        """Detailed trade-by-trade log."""
        lines = [f"{'#':>4} {'Time':<20} {'Signal':<20} {'Entry':>9} {'Stop':>9} "
                 f"{'Target':>9} {'Exit':>9} {'PnL':>9} {'R:R':>6} {'Reason':<10} {'Q':>2}"]
        lines.append("-" * 120)
        for i, t in enumerate(self.trades, 1):
            lines.append(
                f"{i:>4} {str(t.signal.timestamp):<20} {t.signal.label:<20} "
                f"{t.signal.entry_price:>9.2f} {t.signal.stop_price:>9.2f} "
                f"{t.signal.target_price:>9.2f} {t.exit_price:>9.2f} "
                f"{t.pnl_points:>9.4f} {t.pnl_rr:>6.2f} {t.exit_reason:<10} "
                f"{t.signal.quality_score:>2}"
            )
        return "\n".join(lines)


EASTERN = ZoneInfo("US/Eastern")


def load_bars_from_csv(filepath: str, dt_format: str = "%Y-%m-%d %H:%M:%S",
                       dt_col: str = "datetime",
                       open_col: str = "open", high_col: str = "high",
                       low_col: str = "low", close_col: str = "close",
                       volume_col: str = "volume",
                       tz: Optional[ZoneInfo] = None) -> List[Bar]:
    """Load bar data from CSV. Handles common formats.

    Args:
        tz: Timezone to attach to naive timestamps. Defaults to US/Eastern.
            If your CSV timestamps are already in Eastern, leave as default.
            If they're in UTC, pass ZoneInfo("UTC") and they'll be converted.
    """
    if tz is None:
        tz = EASTERN

    bars = []
    path = Path(filepath)
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            norm = {k.strip().lower(): v.strip() for k, v in row.items()}
            try:
                dt_str = norm.get(dt_col.lower(), norm.get("date", norm.get("time", "")))
                dt = datetime.strptime(dt_str, dt_format)
                # Attach timezone — if source is Eastern, replace; if UTC, convert
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tz)
                if tz != EASTERN:
                    dt = dt.astimezone(EASTERN)
                bar = Bar(
                    timestamp=dt,
                    open=float(norm.get(open_col.lower(), 0)),
                    high=float(norm.get(high_col.lower(), 0)),
                    low=float(norm.get(low_col.lower(), 0)),
                    close=float(norm.get(close_col.lower(), 0)),
                    volume=float(norm.get(volume_col.lower(), 0)),
                )
                bars.append(bar)
            except (ValueError, KeyError) as e:
                continue  # skip malformed rows
    return bars


def _compute_dynamic_slippage(cfg: 'OverlayConfig', bar: Bar, family: SetupFamily,
                               intra_atr: float) -> float:
    """
    Dynamic slippage model: base_slip * vol_mult * family_mult
    - base_slip = max(slip_min, price * slip_bps)
    - vol_mult = clamp(bar_range / ATR, 0.5, slip_vol_mult_cap)
    - family_mult = per-family multiplier from config
    """
    import math
    price = bar.close if bar.close > 0 else 1.0
    base_slip = max(cfg.slip_min, price * cfg.slip_bps)

    # Volatility multiplier: how volatile is this bar vs average?
    bar_range = bar.high - bar.low
    if intra_atr > 0 and not math.isnan(intra_atr):
        vol_mult = min(max(bar_range / intra_atr, 0.5), cfg.slip_vol_mult_cap)
    else:
        vol_mult = 1.0

    # Family multiplier
    family_mult_map = {
        SetupFamily.REVERSAL: cfg.slip_family_mult_reversal,
        SetupFamily.TREND: cfg.slip_family_mult_trend,
        SetupFamily.MEAN_REV: cfg.slip_family_mult_reversal,
        SetupFamily.BREAKOUT: cfg.slip_family_mult_breakout,
        SetupFamily.SHORT_STRUCT: cfg.slip_family_mult_short_struct,
        SetupFamily.EMA_SCALP: cfg.slip_family_mult_ema_scalp,
    }
    family_mult = family_mult_map.get(family, 1.0)

    return base_slip * vol_mult * family_mult


def run_backtest(bars: List[Bar],
                 cfg: Optional[OverlayConfig] = None,
                 daily_history: Optional[List[dict]] = None,
                 prior_day_high: float = NaN,
                 prior_day_low: float = NaN,
                 session_end_hhmm: int = 1555,
                 spy_bars: Optional[List[Bar]] = None,
                 qqq_bars: Optional[List[Bar]] = None,
                 sector_bars: Optional[List[Bar]] = None) -> BacktestResult:
    """
    Run the signal engine over a list of bars and simulate trades.

    Trade rules:
      - Enter at signal bar close + slippage (adverse direction)
      - Exit at target, stop, opposing signal, or end of day
      - One position at a time (new signal closes prior position)
      - Slippage applied on both entry and exit
      - Commission applied per side (from cfg.commission_per_share)

    Market context:
      - If spy_bars/qqq_bars are provided and cfg.use_market_context is True,
        market trend and RS will be computed and passed to the signal engine.
      - Bars must be time-aligned (same timestamps as symbol bars).
    """
    if cfg is None:
        cfg = OverlayConfig()

    engine = SignalEngine(cfg)
    comm = cfg.commission_per_share

    # Market context engines
    spy_engine = MarketEngine() if spy_bars else None
    qqq_engine = MarketEngine() if qqq_bars else None
    sector_engine = MarketEngine() if sector_bars else None

    # Build timestamp→index maps for market bars (for alignment)
    def _build_ts_map(market_bars):
        ts_map = {}
        for idx, mb in enumerate(market_bars):
            ts_map[mb.timestamp] = idx
        return ts_map

    spy_ts_map = _build_ts_map(spy_bars) if spy_bars else {}
    qqq_ts_map = _build_ts_map(qqq_bars) if qqq_bars else {}
    sector_ts_map = _build_ts_map(sector_bars) if sector_bars else {}

    # Track market engine state per bar (process sequentially via index)
    spy_idx = 0
    qqq_idx = 0
    sector_idx = 0
    spy_snap = None
    qqq_snap = None
    sector_snap = None

    if daily_history:
        engine.set_daily_atr_history(daily_history)
    if not (prior_day_high != prior_day_high):  # not NaN
        engine.set_prior_day(prior_day_high, prior_day_low)

    result = BacktestResult()
    open_trade: Optional[dict] = None

    # For dynamic slippage: we need intra ATR from the engine
    # We'll track it via a simple ATR proxy from the bar data
    from .indicators import TrueRangeCalc, WildersMA
    _slip_tr = TrueRangeCalc()
    _slip_atr = WildersMA(14)
    _slip_atr_val = NaN

    def _get_slip(bar, sig):
        """Get slippage for a trade, either dynamic or flat."""
        if cfg.use_dynamic_slippage:
            family = SETUP_FAMILY_MAP.get(sig.setup_id, SetupFamily.BREAKOUT)
            return _compute_dynamic_slippage(cfg, bar, family, _slip_atr_val)
        return cfg.slippage_per_side

    def _close_trade(sig, filled_entry, exit_price, exit_time, exit_reason, bars_held, exit_bar):
        """Compute PnL with slippage on the exit side too."""
        exit_slip = _get_slip(exit_bar, sig) if cfg.use_dynamic_slippage else cfg.slippage_per_side
        exit_cost = exit_slip + comm
        adjusted_exit = exit_price - (exit_cost * sig.direction)
        trade = Trade(
            signal=sig,
            exit_price=exit_price,
            exit_time=exit_time,
            exit_reason=exit_reason,
            bars_held=bars_held,
        )
        trade.pnl_points = (adjusted_exit - filled_entry) * sig.direction
        trade.pnl_rr = trade.pnl_points / sig.risk if sig.risk > 0 else 0
        return trade

    for i, bar in enumerate(bars):
        # Update ATR for dynamic slippage
        tr = _slip_tr.update(bar.high, bar.low, bar.close)
        _slip_atr.update(tr)
        if _slip_atr.ready:
            _slip_atr_val = _slip_atr.value

        # Build market context for this bar
        market_ctx = None
        if cfg.use_market_context and spy_bars and qqq_bars:
            # Advance market engines to match current bar's timestamp
            ts = bar.timestamp
            if ts in spy_ts_map and spy_ts_map[ts] >= spy_idx:
                while spy_idx <= spy_ts_map[ts]:
                    spy_snap = spy_engine.process_bar(spy_bars[spy_idx])
                    spy_idx += 1
            if ts in qqq_ts_map and qqq_ts_map[ts] >= qqq_idx:
                while qqq_idx <= qqq_ts_map[ts]:
                    qqq_snap = qqq_engine.process_bar(qqq_bars[qqq_idx])
                    qqq_idx += 1
            if sector_bars and ts in sector_ts_map and sector_ts_map[ts] >= sector_idx:
                while sector_idx <= sector_ts_map[ts]:
                    sector_snap = sector_engine.process_bar(sector_bars[sector_idx])
                    sector_idx += 1

            # Compute stock's pct from day open
            stock_day_open = getattr(engine, '_day_open_for_rs', NaN)
            # Track day open for RS calculation
            date_int = bar.timestamp.year * 10000 + bar.timestamp.month * 100 + bar.timestamp.day
            if not hasattr(engine, '_rs_date') or engine._rs_date != date_int:
                engine._rs_date = date_int
                engine._day_open_for_rs = bar.open
                stock_day_open = bar.open
            else:
                stock_day_open = engine._day_open_for_rs

            stock_pct = (bar.close - stock_day_open) / stock_day_open * 100.0 if stock_day_open > 0 else NaN

            from .market_context import MarketSnapshot
            if spy_snap and qqq_snap:
                market_ctx = compute_market_context(
                    spy_snap, qqq_snap,
                    sector_snapshot=sector_snap,
                    stock_pct_from_open=stock_pct)

        signals = engine.process_bar(bar, market_ctx=market_ctx)
        result.signals_total += len(signals)

        # Check open trade exits BEFORE processing new signals
        if open_trade is not None:
            sig = open_trade["signal"]
            filled_entry = open_trade["filled_entry"]
            exited = False

            # Day-boundary exit: if this bar is on a different day than the
            # entry bar, force close at previous bar's close (intraday only).
            entry_bar = bars[open_trade["entry_bar_idx"]]
            if bar.timestamp.date() != entry_bar.timestamp.date():
                prev_bar = bars[i - 1] if i > 0 else bar
                trade = _close_trade(sig, filled_entry, prev_bar.close, prev_bar.timestamp,
                                     "eod", i - 1 - open_trade["entry_bar_idx"], prev_bar)
                result.trades.append(trade)
                open_trade = None
                exited = True

            # End of day exit (highest priority)
            if not exited and bar.time_hhmm >= session_end_hhmm:
                trade = _close_trade(sig, filled_entry, bar.close, bar.timestamp,
                                     "eod", i - open_trade["entry_bar_idx"], bar)
                result.trades.append(trade)
                open_trade = None
                exited = True

            # Target / Stop with intrabar path dependency guard:
            # If both hit on same bar, assume stop first (conservative).
            if not exited:
                hit_stop = ((sig.direction == 1 and bar.low <= sig.stop_price) or
                            (sig.direction == -1 and bar.high >= sig.stop_price))

                # For BREAKOUT / SHORT_STRUCT family, use hybrid exit (time stop + EMA9 trail)
                sig_family = SETUP_FAMILY_MAP.get(sig.setup_id)
                is_breakout = sig_family == SetupFamily.BREAKOUT
                is_short_struct = sig_family == SetupFamily.SHORT_STRUCT
                is_ema_scalp = sig_family == SetupFamily.EMA_SCALP
                is_rsi_setup = sig.setup_id in (SetupId.RSI_MIDLINE_LONG, SetupId.RSI_BOUNCEFAIL_SHORT)
                uses_trail_exit = is_breakout or is_short_struct or is_ema_scalp or is_rsi_setup
                bars_held = i - open_trade["entry_bar_idx"]

                # Determine exit mode and time stop bars per family/setup
                if sig.setup_id == SetupId.RSI_MIDLINE_LONG:
                    exit_mode = cfg.rsi_long_exit_mode
                    time_stop_bars = cfg.rsi_long_time_stop_bars
                elif sig.setup_id == SetupId.RSI_BOUNCEFAIL_SHORT:
                    exit_mode = cfg.rsi_short_exit_mode
                    time_stop_bars = cfg.rsi_short_time_stop_bars
                elif is_ema_scalp and sig.setup_id == SetupId.EMA_FPIP:
                    exit_mode = cfg.ema_fpip_exit_mode
                    time_stop_bars = cfg.ema_fpip_time_stop_bars
                elif is_ema_scalp:
                    exit_mode = cfg.ema_scalp_exit_mode
                    time_stop_bars = cfg.ema_scalp_time_stop_bars
                elif is_short_struct:
                    if sig.setup_id == SetupId.BDR_SHORT:
                        exit_mode = cfg.bdr_exit_mode
                        time_stop_bars = cfg.bdr_time_stop_bars
                    else:  # FAILED_BOUNCE
                        exit_mode = cfg.fb_exit_mode
                        time_stop_bars = cfg.fb_time_stop_bars
                elif is_breakout and sig.setup_id == SetupId.SC_V2:
                    exit_mode = cfg.sc2_exit_mode
                    time_stop_bars = cfg.sc2_time_stop_bars
                elif is_breakout:
                    exit_mode = cfg.breakout_exit_mode
                    time_stop_bars = cfg.breakout_time_stop_bars
                else:
                    exit_mode = "target"
                    time_stop_bars = 999

                if uses_trail_exit and exit_mode in ("time", "hybrid"):
                    hit_time_stop = bars_held >= time_stop_bars
                else:
                    hit_time_stop = False

                if uses_trail_exit and exit_mode in ("ema9_trail", "hybrid"):
                    # EMA9 close-cross exit (need current bar's EMA9)
                    bar_e9 = getattr(bar, '_e9', 0.0)
                    if bar_e9 > 0 and bars_held >= 2:  # give at least 2 bars before trailing
                        ema9_exit = ((sig.direction == 1 and bar.close < bar_e9) or
                                     (sig.direction == -1 and bar.close > bar_e9))
                    else:
                        ema9_exit = False
                else:
                    ema9_exit = False

                # hybrid_target_time: stop > target > time_stop (RSI setups)
                # Trail-exit setups: stop > time stop > ema9 trail (no static target)
                # Non-breakout:      stop > target
                if exit_mode == "hybrid_target_time":
                    hit_target = ((sig.direction == 1 and bar.high >= sig.target_price) or
                                  (sig.direction == -1 and bar.low <= sig.target_price))
                    if hit_stop:
                        exit_reason = "stop"
                        exit_price = sig.stop_price
                    elif hit_target:
                        exit_reason = "target"
                        exit_price = sig.target_price
                    elif hit_time_stop:
                        exit_reason = "time"
                        exit_price = bar.close
                    else:
                        exit_reason = None
                        exit_price = 0.0
                elif uses_trail_exit:
                    if hit_stop:
                        exit_reason = "stop"
                        exit_price = sig.stop_price
                    elif hit_time_stop:
                        exit_reason = "time"
                        exit_price = bar.close
                    elif ema9_exit:
                        exit_reason = "ema9trail"
                        exit_price = bar.close
                    else:
                        exit_reason = None
                        exit_price = 0.0
                else:
                    hit_target = ((sig.direction == 1 and bar.high >= sig.target_price) or
                                  (sig.direction == -1 and bar.low <= sig.target_price))
                    if hit_stop:
                        exit_reason = "stop"
                        exit_price = sig.stop_price
                    elif hit_target:
                        exit_reason = "target"
                        exit_price = sig.target_price
                    else:
                        exit_reason = None
                        exit_price = 0.0

                if exit_reason:
                    trade = _close_trade(sig, filled_entry, exit_price, bar.timestamp,
                                         exit_reason, i - open_trade["entry_bar_idx"], bar)
                    result.trades.append(trade)
                    open_trade = None
                    exited = True

        # Process new signals
        for sig in signals:
            # Close existing if opposing
            if open_trade is not None:
                old_sig = open_trade["signal"]
                if old_sig.direction != sig.direction:
                    trade = _close_trade(old_sig, open_trade["filled_entry"],
                                         bar.close, bar.timestamp, "opposing",
                                         i - open_trade["entry_bar_idx"], bar)
                    result.trades.append(trade)
                    open_trade = None

            # Open new trade with entry slippage
            # Long: fill slightly higher than signal close
            # Short: fill slightly lower than signal close
            if open_trade is None:
                entry_slip = _get_slip(bar, sig)
                entry_cost = entry_slip + comm
                filled_entry = sig.entry_price + (entry_cost * sig.direction)
                open_trade = {
                    "signal": sig,
                    "entry_bar_idx": i,
                    "filled_entry": filled_entry,
                }

    # Close any remaining open trade at last bar
    if open_trade is not None:
        sig = open_trade["signal"]
        last_bar = bars[-1]
        trade = _close_trade(sig, open_trade["filled_entry"], last_bar.close,
                             last_bar.timestamp, "eod",
                             len(bars) - 1 - open_trade["entry_bar_idx"], last_bar)
        result.trades.append(trade)

    return result


# ─── CLI entry point ───
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m alert_overlay.backtest <csv_file> [dt_format]")
        print("  Default dt_format: %Y-%m-%d %H:%M:%S")
        sys.exit(1)

    csv_path = sys.argv[1]
    dt_fmt = sys.argv[2] if len(sys.argv) > 2 else "%Y-%m-%d %H:%M:%S"

    print(f"Loading bars from {csv_path}...")
    bars = load_bars_from_csv(csv_path, dt_format=dt_fmt)
    print(f"Loaded {len(bars)} bars")

    if not bars:
        print("No bars loaded. Check CSV format.")
        sys.exit(1)

    print("Running backtest...")
    result = run_backtest(bars)
    print(result.summary())
    print()
    print(result.trade_log())
