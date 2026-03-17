"""
StrategySignal + StrategyTrade — standalone data model for new strategy framework.
Does NOT modify existing Signal/SetupId in models.py.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class QualityTier(Enum):
    A_TIER = "A"   # tradeable signal
    B_TIER = "B"   # logged, not traded
    C_TIER = "C"   # logged, not traded


@dataclass
class StrategySignal:
    """A raw signal produced by a strategy's detection logic."""
    strategy_name: str          # e.g. "SC_SNIPER", "FL_ANTICHOP"
    symbol: str
    timestamp: datetime
    direction: int              # 1=LONG, -1=SHORT
    entry_price: float
    stop_price: float
    target_price: float
    quality_tier: QualityTier = QualityTier.C_TIER
    quality_score: int = 0      # 0-10 composite
    reject_reasons: List[str] = field(default_factory=list)
    in_play_score: float = 0.0
    market_regime: str = ""     # GREEN/RED/FLAT
    rs_market: float = 0.0
    rs_sector: float = 0.0
    confluence_tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def entry_date(self) -> date:
        return self.timestamp.date()

    @property
    def risk(self) -> float:
        return abs(self.entry_price - self.stop_price)

    @property
    def is_tradeable(self) -> bool:
        return self.quality_tier == QualityTier.A_TIER and len(self.reject_reasons) == 0


@dataclass
class StrategyTrade:
    """A completed trade (signal + simulation result)."""
    signal: StrategySignal
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    exit_reason: str = ""       # "stop", "target", "eod", "trail"
    pnl_rr: float = 0.0
    bars_held: int = 0
    target_type: str = ""       # "structural" or "no_target"

    @property
    def entry_date(self) -> date:
        return self.signal.timestamp.date()

    @property
    def symbol(self) -> str:
        return self.signal.symbol

    @property
    def strategy_name(self) -> str:
        return self.signal.strategy_name
