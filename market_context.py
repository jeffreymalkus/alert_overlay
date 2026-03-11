"""
Market Context Engine — tracks SPY/QQQ (and optional sector ETF) indicators
to provide broader-market trend states and relative strength calculations.

Lightweight: only computes EMA9, EMA20, VWAP, pct-from-open, and trend state.
No signal detection — just context for the main SignalEngine to consume.
"""

import math
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

from .indicators import EMA, VWAPCalc
from .models import Bar, NaN


class MarketTrend(IntEnum):
    """Broad market trend state derived from SPY/QQQ."""
    BEAR = -1
    NEUTRAL = 0
    BULL = 1


@dataclass
class MarketSnapshot:
    """Point-in-time market context snapshot for one index/ETF."""
    ema9: float = NaN
    ema20: float = NaN
    vwap: float = NaN
    close: float = NaN
    day_open: float = NaN
    pct_from_open: float = NaN  # (close - open) / open * 100
    trend: MarketTrend = MarketTrend.NEUTRAL
    ready: bool = False  # True once indicators are warmed up
    # Granular state fields — independently inspectable per setup
    above_vwap: bool = False     # close > VWAP
    above_ema9: bool = False     # close > EMA9
    above_ema20: bool = False    # close > EMA20
    ema9_above_ema20: bool = False  # EMA9 > EMA20
    ema9_rising: bool = False    # EMA9 slope positive
    ema9_falling: bool = False   # EMA9 slope negative
    day_high: float = NaN        # intraday high (for TREND character)
    day_low: float = NaN         # intraday low (for TREND character)


@dataclass
class MarketContext:
    """
    Combined market context passed to SignalEngine.process_bar().
    Contains SPY snapshot, QQQ snapshot, and computed RS values.
    """
    spy: MarketSnapshot = None
    qqq: MarketSnapshot = None
    sector: MarketSnapshot = None

    # Relative strength: stock vs market/sector
    rs_market: float = NaN   # stock_pct_from_open - spy_pct_from_open
    rs_sector: float = NaN   # stock_pct_from_open - sector_pct_from_open

    # Combined market trend (consensus of SPY + QQQ)
    market_trend: MarketTrend = MarketTrend.NEUTRAL

    def __post_init__(self):
        if self.spy is None:
            self.spy = MarketSnapshot()
        if self.qqq is None:
            self.qqq = MarketSnapshot()
        if self.sector is None:
            self.sector = MarketSnapshot()


class MarketEngine:
    """
    Lightweight indicator engine for a single index/ETF.
    Feed bars one at a time; read the snapshot for current state.
    """

    def __init__(self):
        self.ema9 = EMA(9)
        self.ema20 = EMA(20)
        self.vwap = VWAPCalc()
        self._day_open: float = NaN
        self._current_date: Optional[int] = None
        self._prev_e9: float = NaN
        self._prev_e20: float = NaN
        self._bar_count: int = 0
        self._day_high: float = NaN
        self._day_low: float = NaN

    def process_bar(self, bar: Bar) -> MarketSnapshot:
        """
        Update indicators with a new bar and return a snapshot.
        Handles day resets automatically.
        """
        date_int = bar.timestamp.year * 10000 + bar.timestamp.month * 100 + bar.timestamp.day

        # New day detection
        if self._current_date is not None and date_int != self._current_date:
            self._on_new_day()
        if self._current_date is None:
            self._current_date = date_int

        # Set day open on first bar of session
        if math.isnan(self._day_open):
            self._day_open = bar.open

        # Track intraday high/low for TREND character detection
        if math.isnan(self._day_high) or bar.high > self._day_high:
            self._day_high = bar.high
        if math.isnan(self._day_low) or bar.low < self._day_low:
            self._day_low = bar.low

        # Update indicators
        self._prev_e9 = self.ema9.value
        self._prev_e20 = self.ema20.value
        e9 = self.ema9.update(bar.close)
        e20 = self.ema20.update(bar.close)
        tp = (bar.high + bar.low + bar.close) / 3.0
        vw = self.vwap.update(tp, bar.volume)
        self._bar_count += 1

        # Pct from open
        pct_from_open = NaN
        if not math.isnan(self._day_open) and self._day_open > 0:
            pct_from_open = (bar.close - self._day_open) / self._day_open * 100.0

        # Compute trend state
        trend = self._compute_trend(bar.close, e9, e20, vw)

        ready = self.ema9.ready and self.ema20.ready

        # Granular state
        e9_rising = (not math.isnan(self._prev_e9) and e9 > self._prev_e9)
        e9_falling = (not math.isnan(self._prev_e9) and e9 < self._prev_e9)

        return MarketSnapshot(
            ema9=e9,
            ema20=e20,
            vwap=vw,
            close=bar.close,
            day_open=self._day_open,
            pct_from_open=pct_from_open,
            trend=trend,
            ready=ready,
            above_vwap=(bar.close > vw if not math.isnan(vw) else False),
            above_ema9=(bar.close > e9 if ready else False),
            above_ema20=(bar.close > e20 if ready else False),
            ema9_above_ema20=(e9 > e20 if ready else False),
            ema9_rising=e9_rising,
            ema9_falling=e9_falling,
            day_high=self._day_high,
            day_low=self._day_low,
        )

    def _compute_trend(self, close: float, e9: float, e20: float, vw: float) -> MarketTrend:
        """
        Bull: 2 of 3 conditions met: close > VWAP, close > EMA9, EMA9 > EMA20
        Bear: 2 of 3 inverse conditions
        Neutral: otherwise
        """
        if not self.ema9.ready or not self.ema20.ready:
            return MarketTrend.NEUTRAL

        bull_count = 0
        bear_count = 0

        # Condition 1: close vs VWAP
        if not math.isnan(vw) and vw > 0:
            if close > vw:
                bull_count += 1
            elif close < vw:
                bear_count += 1

        # Condition 2: close vs EMA9
        if close > e9:
            bull_count += 1
        elif close < e9:
            bear_count += 1

        # Condition 3: EMA9 vs EMA20 (with slope — EMA9 rising)
        e9_rising = (not math.isnan(self._prev_e9) and e9 > self._prev_e9)
        e9_falling = (not math.isnan(self._prev_e9) and e9 < self._prev_e9)

        if e9 > e20 and e9_rising:
            bull_count += 1
        elif e9 < e20 and e9_falling:
            bear_count += 1

        if bull_count >= 2:
            return MarketTrend.BULL
        elif bear_count >= 2:
            return MarketTrend.BEAR
        return MarketTrend.NEUTRAL

    def _on_new_day(self):
        """Reset per-day state."""
        self._day_open = NaN
        self._day_high = NaN
        self._day_low = NaN
        self.vwap.reset()
        self._bar_count = 0


def compute_market_context(
    spy_snapshot: MarketSnapshot,
    qqq_snapshot: MarketSnapshot,
    sector_snapshot: Optional[MarketSnapshot] = None,
    stock_pct_from_open: float = NaN,
) -> MarketContext:
    """
    Build a MarketContext from individual snapshots.
    Computes consensus trend and RS values.
    """
    ctx = MarketContext(spy=spy_snapshot, qqq=qqq_snapshot)

    if sector_snapshot is not None:
        ctx.sector = sector_snapshot

    # Consensus market trend: agree on bull/bear, else neutral
    if spy_snapshot.trend == MarketTrend.BULL and qqq_snapshot.trend == MarketTrend.BULL:
        ctx.market_trend = MarketTrend.BULL
    elif spy_snapshot.trend == MarketTrend.BEAR and qqq_snapshot.trend == MarketTrend.BEAR:
        ctx.market_trend = MarketTrend.BEAR
    else:
        # Mixed signals → neutral
        ctx.market_trend = MarketTrend.NEUTRAL

    # RS vs market (use SPY as primary benchmark)
    if not math.isnan(stock_pct_from_open) and not math.isnan(spy_snapshot.pct_from_open):
        ctx.rs_market = stock_pct_from_open - spy_snapshot.pct_from_open

    # RS vs sector
    if (sector_snapshot is not None and
            not math.isnan(stock_pct_from_open) and
            not math.isnan(sector_snapshot.pct_from_open)):
        ctx.rs_sector = stock_pct_from_open - sector_snapshot.pct_from_open

    return ctx


# ── Graded Directional Tradability Score ──

@dataclass
class TradabilityScore:
    """Per-bar graded tradability assessment for long and short directions."""
    long_score: float = 0.0    # higher = more favorable for longs
    short_score: float = 0.0   # lower (more negative) = more favorable for shorts
    # Diagnostic components
    mkt_component: float = 0.0   # market structure component [-1, +1]
    sec_component: float = 0.0   # sector structure component [-1, +1]
    rs_mkt_component: float = 0.0  # RS vs market component [-0.5, +0.5]
    rs_sec_component: float = 0.0  # RS vs sector component [-0.5, +0.5]


def _bull_signal_count(snap: MarketSnapshot) -> int:
    """Count bull signals from a snapshot: above_vwap, ema9>ema20, ema9_rising."""
    count = 0
    if snap.above_vwap:
        count += 1
    if snap.ema9_above_ema20:
        count += 1
    if snap.ema9_rising:
        count += 1
    return count


def compute_tradability(market_ctx: MarketContext, cfg) -> TradabilityScore:
    """
    Compute graded directional tradability scores.

    4 weighted components, each normalized:
    - Market structure (SPY): bull signal count → [-1, +1]
    - Sector structure: bull signal count → [-1, +1]
    - Stock RS vs market: capped ±2%, normalized → [-0.5, +0.5]
    - Stock RS vs sector: capped ±2%, normalized → [-0.5, +0.5]

    Long score = weighted sum / total weight (higher = better for longs)
    Short score = inverted (lower = better for shorts)
    """
    ts = TradabilityScore()

    w_market = getattr(cfg, 'tradability_w_market', 1.0)
    w_sector = getattr(cfg, 'tradability_w_sector', 0.6)
    w_rs_market = getattr(cfg, 'tradability_w_rs_market', 0.5)
    w_rs_sector = getattr(cfg, 'tradability_w_rs_sector', 0.3)

    total_weight = 0.0
    weighted_sum_long = 0.0

    # Component 1: Market structure (SPY)
    if market_ctx.spy.ready:
        bull_count = _bull_signal_count(market_ctx.spy)
        # Normalize: (count - 1.5) / 1.5 → [-1, +1]
        mkt_norm = (bull_count - 1.5) / 1.5
        ts.mkt_component = mkt_norm
        weighted_sum_long += w_market * mkt_norm
        total_weight += w_market

    # Component 2: Sector structure
    if market_ctx.sector.ready:
        sec_bull_count = _bull_signal_count(market_ctx.sector)
        sec_norm = (sec_bull_count - 1.5) / 1.5
        ts.sec_component = sec_norm
        weighted_sum_long += w_sector * sec_norm
        total_weight += w_sector

    # Component 3: RS vs market (capped ±2%, normalized to [-0.5, +0.5])
    if not math.isnan(market_ctx.rs_market):
        rs_capped = max(-2.0, min(2.0, market_ctx.rs_market))
        rs_norm = rs_capped / 4.0  # → [-0.5, +0.5]
        ts.rs_mkt_component = rs_norm
        weighted_sum_long += w_rs_market * rs_norm
        total_weight += w_rs_market

    # Component 4: RS vs sector (capped ±2%, normalized to [-0.5, +0.5])
    if not math.isnan(market_ctx.rs_sector):
        rs_capped = max(-2.0, min(2.0, market_ctx.rs_sector))
        rs_norm = rs_capped / 4.0  # → [-0.5, +0.5]
        ts.rs_sec_component = rs_norm
        weighted_sum_long += w_rs_sector * rs_norm
        total_weight += w_rs_sector

    # Compute final scores
    if total_weight > 0:
        ts.long_score = weighted_sum_long / total_weight
        # Short score: invert market/sector, keep RS inverted
        # (bearish market = good for shorts, strong RS = bad for shorts)
        ts.short_score = -ts.long_score
    else:
        ts.long_score = 0.0
        ts.short_score = 0.0

    return ts


# ── Symbol-to-sector ETF mapping ──
SECTOR_MAP = {
    # Technology
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AMD": "XLK", "INTC": "XLK",
    "AVGO": "XLK", "CRM": "XLK", "ORCL": "XLK", "ADBE": "XLK", "NOW": "XLK",
    "CSCO": "XLK", "IBM": "XLK", "QCOM": "XLK", "TXN": "XLK", "AMAT": "XLK",
    "MU": "XLK", "LRCX": "XLK", "KLAC": "XLK", "SNPS": "XLK", "CDNS": "XLK",
    "MRVL": "XLK", "ON": "XLK", "SMCI": "XLK", "DELL": "XLK", "HPE": "XLK",
    "PLTR": "XLK", "CRWD": "XLK", "PANW": "XLK", "FTNT": "XLK", "ZS": "XLK",
    # Communication Services
    "GOOGL": "XLC", "GOOG": "XLC", "META": "XLC", "NFLX": "XLC", "DIS": "XLC",
    "CMCSA": "XLC", "T": "XLC", "VZ": "XLC", "TMUS": "XLC", "SNAP": "XLC",
    "PINS": "XLC", "ROKU": "XLC", "TTD": "XLC", "SPOT": "XLC",
    # Consumer Discretionary
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "MCD": "XLY", "NKE": "XLY",
    "SBUX": "XLY", "LOW": "XLY", "TJX": "XLY", "BKNG": "XLY", "CMG": "XLY",
    "ABNB": "XLY", "GM": "XLY", "F": "XLY", "RIVN": "XLY", "LCID": "XLY",
    "DKNG": "XLY", "DASH": "XLY", "UBER": "XLY", "LYFT": "XLY",
    # Financials
    "JPM": "XLF", "BAC": "XLF", "WFC": "XLF", "GS": "XLF", "MS": "XLF",
    "C": "XLF", "SCHW": "XLF", "BLK": "XLF", "AXP": "XLF", "V": "XLF",
    "MA": "XLF", "PYPL": "XLF", "XYZ": "XLF", "COIN": "XLF", "HOOD": "XLF",
    "SOFI": "XLF",
    # Healthcare
    "UNH": "XLV", "JNJ": "XLV", "LLY": "XLV", "PFE": "XLV", "ABBV": "XLV",
    "MRK": "XLV", "TMO": "XLV", "ABT": "XLV", "BMY": "XLV", "AMGN": "XLV",
    "GILD": "XLV", "MRNA": "XLV", "ISRG": "XLV", "DXCM": "XLV", "HUM": "XLV",
    # Energy
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "SLB": "XLE", "EOG": "XLE",
    "MPC": "XLE", "OXY": "XLE", "DVN": "XLE", "HAL": "XLE", "VLO": "XLE",
    # Industrials
    "BA": "XLI", "CAT": "XLI", "HON": "XLI", "UPS": "XLI", "RTX": "XLI",
    "DE": "XLI", "LMT": "XLI", "GE": "XLI", "MMM": "XLI", "UNP": "XLI",
    # Consumer Staples
    "PG": "XLP", "KO": "XLP", "PEP": "XLP", "COST": "XLP", "WMT": "XLP",
    "PM": "XLP", "MO": "XLP", "CL": "XLP", "MDLZ": "XLP",
    # Materials
    "LIN": "XLB", "APD": "XLB", "SHW": "XLB", "NEM": "XLB", "FCX": "XLB",
    # Utilities
    "NEE": "XLU", "DUK": "XLU", "SO": "XLU", "AEP": "XLU",
    # Real Estate
    "AMT": "XLRE", "PLD": "XLRE", "CCI": "XLRE", "SPG": "XLRE",
    # Broad market (self-referential — no sector needed)
    "SPY": "SPY", "QQQ": "QQQ", "IWM": "IWM",
}


def get_sector_etf(symbol: str) -> Optional[str]:
    """Return sector ETF for a symbol, or None if unmapped."""
    return SECTOR_MAP.get(symbol.upper())
