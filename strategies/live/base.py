"""
LiveStrategy — base class for all incremental live strategies.

Each strategy implements:
  - step(snap, market_ctx) → processes one bar, returns Optional[RawSignal]
  - reset_day() → clears state machine for new session
  - name → unique identifier for logging/config

Strategies own ONLY their private state machine fields.
They read shared indicators via the IndicatorSnapshot (read-only).
They cannot modify shared state or affect other strategies.
"""

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, List

from ...models import Bar, NaN
from .shared_indicators import IndicatorSnapshot

_isnan = math.isnan


@dataclass
class RawSignal:
    """Pre-filtered signal emitted by a strategy's step() method.

    This goes through rejection filters + quality scoring before
    becoming a live alert. Lightweight — no heavy objects.
    """
    strategy_name: str
    direction: int             # 1=LONG, -1=SHORT
    entry_price: float
    stop_price: float
    target_price: float
    bar_idx: int
    hhmm: int
    quality: int = 3           # base quality, refined by QualityScorer
    metadata: dict = field(default_factory=dict)  # strategy-specific data


class LiveStrategy(ABC):
    """Base class for all incremental live strategies.

    Subclasses implement step() for per-bar processing and reset_day()
    for session state clearing. All shared indicator data comes via
    the IndicatorSnapshot parameter — strategies never compute their
    own EMAs, VWAP, or ATR.
    """

    def __init__(self, name: str, direction: int = 1, enabled: bool = True,
                 timeframe: int = 5,
                 skip_rejections: Optional[List[str]] = None):
        self.name = name
        self.direction = direction  # 1=LONG, -1=SHORT, 0=BOTH
        self.enabled = enabled
        self.timeframe = timeframe  # 1=runs on 1-min bars, 5=runs on 5-min bars
        self.skip_rejections: List[str] = skip_rejections or []
        self._current_date: Optional[date] = None

    @abstractmethod
    def step(self, snap: IndicatorSnapshot,
             market_ctx=None) -> Optional[RawSignal]:
        """Process one bar. Return a RawSignal if triggered, else None.

        Args:
            snap: Read-only indicator snapshot for this bar
            market_ctx: Optional market context (SPY/QQQ regime, RS, etc.)

        Returns:
            RawSignal if the strategy fires, else None

        Rules:
            - Must be O(1) or O(small_constant) — no re-scanning
            - Must not modify snap or any shared state
            - Must only update self.* fields (private state machine)
        """
        ...

    @abstractmethod
    def reset_day(self):
        """Reset strategy state machine for a new trading day.

        Called automatically by StrategyManager on date change.
        Must clear all phase tracking, flags, levels, counters.
        """
        ...

    def _check_day_reset(self, snap: IndicatorSnapshot):
        """Auto-reset on date change. Called by StrategyManager."""
        if self._current_date is None or snap.day_date != self._current_date:
            self._current_date = snap.day_date
            self.reset_day()

    def __repr__(self):
        state = "ON" if self.enabled else "OFF"
        return f"<{self.name} [{state}]>"
