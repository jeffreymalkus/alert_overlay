"""
Tape Model — Local Directional Permission Framework (RESEARCH ONLY).

Replaces coarse RED/GREEN regime gating with a multi-factor continuous
permission model.  Does NOT touch the frozen Portfolio C candidate.

Goal: answer "is this setup tradable right now?" using:
  1. Market VWAP state      (SPY price vs VWAP)
  2. Market EMA structure   (SPY EMA9 vs EMA20, slope)
  3. Market pressure        (SPY slope / rate of change)
  4. Sector VWAP/EMA state  (sector ETF structure)
  5. Stock RS vs market     (stock pct_from_open - SPY pct_from_open)
  6. Stock RS vs sector     (stock pct_from_open - sector pct_from_open)
  7. Setup direction        (long vs short → inverts the scoring)

Output: TapeReading with a directional_permission score [-1, +1]
  +1 = strongly favors longs, strongly opposes shorts
  -1 = strongly favors shorts, strongly opposes longs
   0 = neutral / no edge either way

Each component is independently scored and weighted so we can:
  - See which factors matter via backtest correlation
  - Tune weights empirically (not curve-fit — robustness-checked)
  - Identify tradable zones even on "wrong-color" days
"""

import math
from dataclasses import dataclass, field
from typing import Optional, List

from .market_context import MarketSnapshot, MarketContext
from .models import NaN


# ═══════════════════════════════════════════════════════════════════
#  Component Signals — each returns a value in [-1, +1]
# ═══════════════════════════════════════════════════════════════════

def _score_vwap_state(snap: MarketSnapshot) -> float:
    """
    Market/sector VWAP position.
    Above VWAP = bullish (+), below = bearish (-).
    Graded by distance: close at VWAP = 0, far above = +1, far below = -1.
    """
    if not snap.ready or math.isnan(snap.vwap) or snap.vwap <= 0:
        return 0.0
    pct_from_vwap = (snap.close - snap.vwap) / snap.vwap * 100.0
    # Cap at ±0.5% for normalization → [-1, +1]
    return max(-1.0, min(1.0, pct_from_vwap / 0.5))


def _score_ema_structure(snap: MarketSnapshot) -> float:
    """
    EMA9 vs EMA20 structure.
    Scores 3 sub-signals each ±1/3:
      - EMA9 > EMA20 (or below)
      - Close > EMA9 (or below)
      - EMA9 rising (or falling)
    Total range: [-1, +1]
    """
    if not snap.ready:
        return 0.0
    score = 0.0
    # Sub-signal 1: EMA9 vs EMA20
    if snap.ema9_above_ema20:
        score += 1.0 / 3.0
    else:
        score -= 1.0 / 3.0
    # Sub-signal 2: Close vs EMA9
    if snap.above_ema9:
        score += 1.0 / 3.0
    else:
        score -= 1.0 / 3.0
    # Sub-signal 3: EMA9 slope
    if snap.ema9_rising:
        score += 1.0 / 3.0
    elif snap.ema9_falling:
        score -= 1.0 / 3.0
    return max(-1.0, min(1.0, score))


def _score_pressure(snap: MarketSnapshot) -> float:
    """
    Market pressure — rate of change from open.
    Uses pct_from_open as a proxy for intraday momentum.
    Graded: +0.3% from open → +1, -0.3% → -1, 0% → 0.
    """
    if not snap.ready or math.isnan(snap.pct_from_open):
        return 0.0
    # Cap at ±0.3% for normalization
    return max(-1.0, min(1.0, snap.pct_from_open / 0.3))


def _score_rs(rs_value: float) -> float:
    """
    Relative strength score.
    RS = stock_pct_from_open - benchmark_pct_from_open
    Positive RS = stock outperforming = bullish for longs.
    Normalized: ±1.0% RS → ±1.
    """
    if math.isnan(rs_value):
        return 0.0
    return max(-1.0, min(1.0, rs_value / 1.0))


# ═══════════════════════════════════════════════════════════════════
#  Tape Reading — aggregated directional assessment
# ═══════════════════════════════════════════════════════════════════

@dataclass
class TapeReading:
    """Complete tape assessment at a point in time."""
    # Individual component scores [-1, +1]
    mkt_vwap: float = 0.0        # SPY VWAP position
    mkt_ema: float = 0.0         # SPY EMA structure
    mkt_pressure: float = 0.0    # SPY pct from open (momentum)
    sec_vwap: float = 0.0        # Sector VWAP position
    sec_ema: float = 0.0         # Sector EMA structure
    rs_market: float = 0.0       # Stock RS vs SPY
    rs_sector: float = 0.0       # Stock RS vs sector

    # Aggregate scores
    tape_score: float = 0.0      # Weighted sum [-1, +1], bullish-biased
    long_permission: float = 0.0  # tape_score mapped for long direction
    short_permission: float = 0.0 # tape_score inverted for short direction

    # Derived flags (for filtering / analysis)
    @property
    def long_favorable(self) -> bool:
        """Tape score > 0 → longs have tailwind."""
        return self.tape_score > 0.0

    @property
    def short_favorable(self) -> bool:
        """Tape score < 0 → shorts have tailwind."""
        return self.tape_score < 0.0

    @property
    def long_tradable(self) -> bool:
        """Long permission above threshold (default > -0.2)."""
        return self.long_permission > -0.2

    @property
    def short_tradable(self) -> bool:
        """Short permission above threshold (default > -0.2)."""
        return self.short_permission > -0.2

    def components_dict(self) -> dict:
        """Return all components as a flat dict for DataFrame construction."""
        return {
            "mkt_vwap": round(self.mkt_vwap, 3),
            "mkt_ema": round(self.mkt_ema, 3),
            "mkt_pressure": round(self.mkt_pressure, 3),
            "sec_vwap": round(self.sec_vwap, 3),
            "sec_ema": round(self.sec_ema, 3),
            "rs_market": round(self.rs_market, 3),
            "rs_sector": round(self.rs_sector, 3),
            "tape_score": round(self.tape_score, 3),
            "long_permission": round(self.long_permission, 3),
            "short_permission": round(self.short_permission, 3),
        }


@dataclass
class TapeWeights:
    """
    Configurable weights for each tape component.
    Default weights reflect prior belief — to be validated by research.
    All weights are positive; direction comes from the component scores.
    """
    mkt_vwap: float = 1.0       # Market VWAP — strong signal
    mkt_ema: float = 0.8        # Market EMA structure — moderate
    mkt_pressure: float = 0.6   # Market momentum — moderate
    sec_vwap: float = 0.5       # Sector VWAP — supplementary
    sec_ema: float = 0.4        # Sector EMA — supplementary
    rs_market: float = 0.7      # RS vs market — important for stock selection
    rs_sector: float = 0.3      # RS vs sector — supplementary

    def total(self) -> float:
        return (self.mkt_vwap + self.mkt_ema + self.mkt_pressure +
                self.sec_vwap + self.sec_ema + self.rs_market + self.rs_sector)

    def as_dict(self) -> dict:
        return {
            "mkt_vwap": self.mkt_vwap,
            "mkt_ema": self.mkt_ema,
            "mkt_pressure": self.mkt_pressure,
            "sec_vwap": self.sec_vwap,
            "sec_ema": self.sec_ema,
            "rs_market": self.rs_market,
            "rs_sector": self.rs_sector,
        }


def read_tape(
    market_ctx: MarketContext,
    weights: Optional[TapeWeights] = None,
) -> TapeReading:
    """
    Compute a TapeReading from the current MarketContext.

    Returns a reading with all components scored and a weighted aggregate.
    The tape_score is bullish-positive: >0 favors longs, <0 favors shorts.

    long_permission = tape_score (positive = go, negative = caution)
    short_permission = -tape_score (positive = go, negative = caution)
    """
    if weights is None:
        weights = TapeWeights()

    reading = TapeReading()

    # ── Score each component ──
    reading.mkt_vwap = _score_vwap_state(market_ctx.spy)
    reading.mkt_ema = _score_ema_structure(market_ctx.spy)
    reading.mkt_pressure = _score_pressure(market_ctx.spy)

    if market_ctx.sector.ready:
        reading.sec_vwap = _score_vwap_state(market_ctx.sector)
        reading.sec_ema = _score_ema_structure(market_ctx.sector)

    reading.rs_market = _score_rs(market_ctx.rs_market)
    reading.rs_sector = _score_rs(market_ctx.rs_sector)

    # ── Weighted aggregate ──
    w = weights
    total_w = 0.0
    weighted_sum = 0.0

    def _add(component_score, weight):
        nonlocal total_w, weighted_sum
        if weight > 0:
            weighted_sum += component_score * weight
            total_w += weight

    _add(reading.mkt_vwap, w.mkt_vwap)
    _add(reading.mkt_ema, w.mkt_ema)
    _add(reading.mkt_pressure, w.mkt_pressure)

    # Only add sector if we have sector data
    if market_ctx.sector.ready:
        _add(reading.sec_vwap, w.sec_vwap)
        _add(reading.sec_ema, w.sec_ema)

    # Only add RS if we have RS data
    if not math.isnan(market_ctx.rs_market):
        _add(reading.rs_market, w.rs_market)
    if not math.isnan(market_ctx.rs_sector):
        _add(reading.rs_sector, w.rs_sector)

    if total_w > 0:
        reading.tape_score = weighted_sum / total_w
    else:
        reading.tape_score = 0.0

    reading.long_permission = reading.tape_score
    reading.short_permission = -reading.tape_score

    return reading


# ═══════════════════════════════════════════════════════════════════
#  Tape Zones — discretized labels for analysis grouping
# ═══════════════════════════════════════════════════════════════════

TAPE_ZONES = {
    "STRONG_BULL":   (0.5, 1.01),    # tape_score >= 0.5
    "MILD_BULL":     (0.15, 0.5),    # 0.15 <= tape_score < 0.5
    "NEUTRAL":       (-0.15, 0.15),  # -0.15 <= tape_score < 0.15
    "MILD_BEAR":     (-0.5, -0.15),  # -0.5 <= tape_score < -0.15
    "STRONG_BEAR":   (-1.01, -0.5),  # tape_score < -0.5
}


def classify_tape_zone(tape_score: float) -> str:
    """Classify a tape_score into a named zone."""
    if tape_score >= 0.5:
        return "STRONG_BULL"
    elif tape_score >= 0.15:
        return "MILD_BULL"
    elif tape_score >= -0.15:
        return "NEUTRAL"
    elif tape_score >= -0.5:
        return "MILD_BEAR"
    else:
        return "STRONG_BEAR"


def permission_for_direction(reading: TapeReading, direction: int) -> float:
    """
    Get directional permission for a setup.
    direction: +1 = long, -1 = short
    Returns: permission score where positive = favorable.
    """
    if direction == 1:
        return reading.long_permission
    elif direction == -1:
        return reading.short_permission
    return 0.0
