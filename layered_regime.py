"""
Layered Regime Framework — Hierarchical Directional Permission Model.

RESEARCH ONLY — does not touch the frozen Portfolio C candidate.

Three layers, evaluated in order.  Each layer can only tighten, never loosen.

Layer 1 — Market Envelope (coarse gate)
    Determines whether the broad environment supports longs, shorts, or neither.
    Uses SPY pct-from-open to classify the day envelope.
    A trade MUST pass its envelope gate to proceed.  No override.

    Envelopes:
      LONG_OK     → SPY pct_from_open >= -0.10%   (green, flat, barely red)
      SHORT_OK    → SPY pct_from_open <= +0.10%    (red, flat, barely green)
      HOSTILE     → both fail (shouldn't happen with overlap, but safety)

    Note: there is deliberate overlap in the FLAT zone (-0.10% to +0.10%)
    where BOTH books are envelope-allowed.  Layer 2 resolves which side
    actually has permission.

Layer 2 — Directional Permission (local tape)
    Within the allowed envelope, grades how favorable conditions are for
    the specific direction using continuous tape signals.

    Components (each scored [-1, +1]):
      - Market VWAP state         (SPY close vs VWAP)
      - Market EMA structure      (SPY EMA9 vs EMA20 + slope)
      - Market pressure           (SPY pct from open, rate of change)
      - Sector VWAP state         (sector ETF vs VWAP)
      - Sector EMA structure      (sector ETF EMA structure)
      - Stock RS vs market        (stock vs SPY pct from open)
      - Stock RS vs sector        (stock vs sector pct from open)
      - Time of day penalty       (late-day trades penalized)

    Weighted aggregate → permission score.
    Long permission = positive score = favorable for longs.
    Short permission = negative score inverted = favorable for shorts.

    A minimum permission threshold is required to proceed.

Layer 3 — Setup Quality (existing)
    The setup itself must meet quality gates:
      - Quality score >= threshold
      - Time gate (e.g., < 15:30 for longs, < 11:00 for BDR shorts)
      - Setup-specific requirements (e.g., wick >= 30% for BDR)

    This layer already exists in the frozen system.  Not reimplemented here.

Usage:
    from alert_overlay.layered_regime import LayeredRegime, RegimeDecision

    regime = LayeredRegime()
    decision = regime.evaluate(
        direction=1,  # +1 long, -1 short
        market_ctx=ctx,
        bar_time_hhmm=1030,
        weights=weights,
    )
    if decision.allowed:
        # trade passes all layers
        print(f"Permission: {decision.permission:.3f}")
"""

import math
from dataclasses import dataclass
from typing import Optional

from .market_context import MarketContext, MarketSnapshot
from .tape_model import (
    TapeReading, TapeWeights, read_tape,
    _score_vwap_state, _score_ema_structure, _score_pressure, _score_rs,
    classify_tape_zone,
)
from .models import NaN


# ═══════════════════════════════════════════════════════════════════
#  Layer 1 — Market Envelope
# ═══════════════════════════════════════════════════════════════════

class Envelope:
    """Market envelope classification."""
    LONG_OK = "LONG_OK"
    SHORT_OK = "SHORT_OK"
    BOTH_OK = "BOTH_OK"       # Overlap zone — both books allowed
    HOSTILE = "HOSTILE"        # Neither book allowed


@dataclass
class EnvelopeConfig:
    """Thresholds for envelope classification."""
    # SPY pct_from_open thresholds
    long_floor: float = -0.10    # longs allowed if SPY >= this
    short_ceiling: float = 0.10  # shorts allowed if SPY <= this


def classify_envelope(
    spy_snap: MarketSnapshot,
    cfg: Optional[EnvelopeConfig] = None,
) -> str:
    """
    Layer 1: Classify the market envelope.

    Returns one of: LONG_OK, SHORT_OK, BOTH_OK, HOSTILE
    """
    if cfg is None:
        cfg = EnvelopeConfig()

    if not spy_snap.ready or math.isnan(spy_snap.pct_from_open):
        return Envelope.HOSTILE

    pfo = spy_snap.pct_from_open
    long_ok = pfo >= cfg.long_floor
    short_ok = pfo <= cfg.short_ceiling

    if long_ok and short_ok:
        return Envelope.BOTH_OK
    elif long_ok:
        return Envelope.LONG_OK
    elif short_ok:
        return Envelope.SHORT_OK
    else:
        return Envelope.HOSTILE


def envelope_allows(envelope: str, direction: int) -> bool:
    """Check if the envelope allows a given direction."""
    if envelope == Envelope.BOTH_OK:
        return True
    if envelope == Envelope.LONG_OK and direction == 1:
        return True
    if envelope == Envelope.SHORT_OK and direction == -1:
        return True
    return False


# ═══════════════════════════════════════════════════════════════════
#  Layer 2 — Directional Permission (extended tape with time penalty)
# ═══════════════════════════════════════════════════════════════════

@dataclass
class PermissionWeights(TapeWeights):
    """Extended weights including time-of-day penalty."""
    time_penalty: float = 0.3   # Weight for time-of-day component

    def total(self) -> float:
        return super().total() + self.time_penalty

    def as_dict(self) -> dict:
        d = super().as_dict()
        d["time_penalty"] = self.time_penalty
        return d


def _score_time_of_day(hhmm: int, direction: int) -> float:
    """
    Time-of-day quality signal.

    Morning (9:30-10:30): best for both sides → +1
    Mid-morning (10:30-11:30): good → +0.5
    Midday (11:30-13:00): choppy, low edge → 0
    Early afternoon (13:00-14:30): moderate → +0.3
    Late afternoon (14:30-15:30): risky, less time → -0.3
    Last 30 min (15:30-16:00): worst → -1

    Returns [-1, +1] where positive = favorable time.
    """
    if hhmm < 1030:
        return 1.0
    elif hhmm < 1130:
        return 0.5
    elif hhmm < 1300:
        return 0.0
    elif hhmm < 1430:
        return 0.3
    elif hhmm < 1530:
        return -0.3
    else:
        return -1.0


@dataclass
class PermissionReading:
    """Full Layer 2 permission assessment."""
    # Tape components (from tape_model)
    tape: TapeReading = None
    # Time component
    time_score: float = 0.0
    # Final permission (direction-adjusted)
    permission: float = 0.0
    # Direction this was evaluated for
    direction: int = 0

    def __post_init__(self):
        if self.tape is None:
            self.tape = TapeReading()


def compute_permission(
    market_ctx: MarketContext,
    direction: int,
    bar_time_hhmm: int = 1000,
    weights: Optional[PermissionWeights] = None,
) -> PermissionReading:
    """
    Layer 2: Compute directional permission score.

    Combines tape reading with time-of-day signal.
    Returns a PermissionReading with the final permission score.

    Positive permission = conditions favor this direction.
    Negative permission = conditions oppose this direction.
    """
    if weights is None:
        weights = PermissionWeights()

    reading = PermissionReading(direction=direction)

    # Get base tape reading (uses the TapeWeights portion)
    tape = read_tape(market_ctx, weights)
    reading.tape = tape

    # Time component
    reading.time_score = _score_time_of_day(bar_time_hhmm, direction)

    # Recompute weighted aggregate including time
    # We need to redo the weighted sum to include time
    components = [
        (tape.mkt_vwap, weights.mkt_vwap),
        (tape.mkt_ema, weights.mkt_ema),
        (tape.mkt_pressure, weights.mkt_pressure),
    ]

    if market_ctx.sector.ready:
        components.append((tape.sec_vwap, weights.sec_vwap))
        components.append((tape.sec_ema, weights.sec_ema))

    if not math.isnan(market_ctx.rs_market):
        components.append((tape.rs_market, weights.rs_market))
    if not math.isnan(market_ctx.rs_sector):
        components.append((tape.rs_sector, weights.rs_sector))

    # Add time component
    components.append((reading.time_score, weights.time_penalty))

    total_w = sum(w for _, w in components if w > 0)
    weighted_sum = sum(score * w for score, w in components if w > 0)

    if total_w > 0:
        raw_score = weighted_sum / total_w
    else:
        raw_score = 0.0

    # Direction-adjust: positive = favorable for THIS direction
    if direction == 1:
        reading.permission = raw_score   # bullish tape = good for longs
    elif direction == -1:
        reading.permission = -raw_score  # bearish tape = good for shorts
    else:
        reading.permission = 0.0

    return reading


# ═══════════════════════════════════════════════════════════════════
#  Combined Decision
# ═══════════════════════════════════════════════════════════════════

@dataclass
class RegimeDecision:
    """Complete hierarchical regime decision."""
    # Layer 1
    envelope: str = Envelope.HOSTILE
    envelope_allows: bool = False

    # Layer 2
    permission: float = 0.0
    permission_reading: PermissionReading = None
    meets_threshold: bool = False

    # Combined
    allowed: bool = False
    rejection_reason: str = ""

    # Diagnostic
    tape_zone: str = ""
    direction: int = 0

    def __post_init__(self):
        if self.permission_reading is None:
            self.permission_reading = PermissionReading()


@dataclass
class LayeredRegimeConfig:
    """Configuration for the layered regime framework."""
    envelope: EnvelopeConfig = None
    weights: PermissionWeights = None

    # Permission thresholds (direction-specific)
    long_min_permission: float = 0.20   # minimum tape permission for longs
    short_min_permission: float = 0.20  # minimum tape permission for shorts

    def __post_init__(self):
        if self.envelope is None:
            self.envelope = EnvelopeConfig()
        if self.weights is None:
            self.weights = PermissionWeights()


class LayeredRegime:
    """
    Hierarchical regime evaluation.

    Usage:
        regime = LayeredRegime(config)
        decision = regime.evaluate(direction=1, market_ctx=ctx, bar_time_hhmm=1030)
        if decision.allowed:
            # proceed with trade
    """

    def __init__(self, config: Optional[LayeredRegimeConfig] = None):
        self.config = config or LayeredRegimeConfig()

    def evaluate(
        self,
        direction: int,
        market_ctx: MarketContext,
        bar_time_hhmm: int = 1000,
    ) -> RegimeDecision:
        """
        Evaluate all three layers for a potential trade.

        Args:
            direction: +1 for long, -1 for short
            market_ctx: current MarketContext with SPY/QQQ/sector snapshots
            bar_time_hhmm: current time as HHMM integer

        Returns:
            RegimeDecision with allowed=True/False and diagnostics
        """
        decision = RegimeDecision(direction=direction)

        # ── Layer 1: Envelope ──
        decision.envelope = classify_envelope(
            market_ctx.spy, self.config.envelope
        )
        decision.envelope_allows = envelope_allows(decision.envelope, direction)

        if not decision.envelope_allows:
            decision.rejection_reason = f"L1: envelope={decision.envelope}, dir={'LONG' if direction==1 else 'SHORT'}"
            return decision

        # ── Layer 2: Permission ──
        perm_reading = compute_permission(
            market_ctx, direction, bar_time_hhmm, self.config.weights
        )
        decision.permission_reading = perm_reading
        decision.permission = perm_reading.permission
        decision.tape_zone = classify_tape_zone(perm_reading.tape.tape_score)

        min_perm = (self.config.long_min_permission if direction == 1
                    else self.config.short_min_permission)
        decision.meets_threshold = perm_reading.permission >= min_perm

        if not decision.meets_threshold:
            decision.rejection_reason = (
                f"L2: perm={perm_reading.permission:+.3f} < "
                f"min={min_perm:.2f} (zone={decision.tape_zone})"
            )
            return decision

        # ── Layer 3: Setup quality ──
        # Not evaluated here — handled by the engine's existing quality gates.
        # This layer just confirms that L1 and L2 passed.

        decision.allowed = True
        return decision

    def describe(self) -> str:
        """Human-readable description of the current config."""
        c = self.config
        return (
            f"Layered Regime Framework\n"
            f"  L1 Envelope:  long_floor={c.envelope.long_floor:+.2f}%, "
            f"short_ceiling={c.envelope.short_ceiling:+.2f}%\n"
            f"  L2 Permission: long_min={c.long_min_permission:.2f}, "
            f"short_min={c.short_min_permission:.2f}\n"
            f"  L2 Weights: {c.weights.as_dict()}"
        )


# ═══════════════════════════════════════════════════════════════════
#  Presets — named configurations for testing
# ═══════════════════════════════════════════════════════════════════

def preset_conservative() -> LayeredRegimeConfig:
    """Tight filters — high permission thresholds, narrow envelope."""
    return LayeredRegimeConfig(
        envelope=EnvelopeConfig(long_floor=-0.05, short_ceiling=0.05),
        weights=PermissionWeights(),
        long_min_permission=0.40,
        short_min_permission=0.40,
    )


def preset_moderate() -> LayeredRegimeConfig:
    """Balanced — moderate thresholds, standard envelope."""
    return LayeredRegimeConfig(
        envelope=EnvelopeConfig(long_floor=-0.10, short_ceiling=0.10),
        weights=PermissionWeights(),
        long_min_permission=0.25,
        short_min_permission=0.25,
    )


def preset_relaxed() -> LayeredRegimeConfig:
    """Wider envelope, lower thresholds — more trades, lower per-trade edge."""
    return LayeredRegimeConfig(
        envelope=EnvelopeConfig(long_floor=-0.15, short_ceiling=0.15),
        weights=PermissionWeights(),
        long_min_permission=0.15,
        short_min_permission=0.15,
    )


def preset_frozen_equivalent() -> LayeredRegimeConfig:
    """
    Approximates the frozen Portfolio C regime logic.
    Long: Non-RED ≈ pfo >= -0.05
    Short: RED+TREND ≈ very negative pfo, high short permission
    """
    return LayeredRegimeConfig(
        envelope=EnvelopeConfig(long_floor=-0.05, short_ceiling=-0.05),
        weights=PermissionWeights(),
        long_min_permission=0.0,   # no tape filter (frozen has none)
        short_min_permission=0.0,  # no tape filter (frozen uses RED+TREND)
    )
