"""Shared strategy framework components."""

from .signal_schema import StrategySignal, QualityTier, StrategyTrade
from .config import StrategyConfig
from .in_play_proxy import InPlayProxy
from .market_regime import EnhancedMarketRegime, RegimeSnapshot
from .rejection_filters import RejectionFilters
from .quality_scoring import QualityScorer
from .helpers import (
    trigger_bar_quality, is_expansion_bar, bar_body_ratio,
    compute_rs_from_open, bars_since_open, is_in_time_window,
    simulate_strategy_trade,
)
