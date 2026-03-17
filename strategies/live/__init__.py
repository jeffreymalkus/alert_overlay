"""
Live strategy runtime — dual-timeframe incremental processing for the live pipeline.

Architecture:
  SharedIndicators → maintains both 1-min and 5-min indicator sets per symbol
  LiveStrategy.step() → each strategy processes one bar incrementally
  StrategyManager → routes bars to strategies by declared timeframe
    - on_1min_bar() → updates 1-min indicators, runs timeframe=1 strategies
    - on_5min_bar() → updates 5-min indicators, runs timeframe=5 strategies

All strategies share read-only indicator state. Each strategy owns only its
private state machine. No strategy can affect another strategy's logic.

Promoted strategies (11 total):
  1-min:
    EMA9_FT       — Drive → first pullback to E9 → reclaim (long)
    ORH_FBO_V2    — ORH break → fail → retest/continuation → short (RED/FLAT only)
    PDH_FBO       — PDH break → fail → continuation → short (RED/FLAT only, Mode B active)
  5-min:
    SC_SNIPER     — Breakout → retest → confirmation (long)
    FL_ANTICHOP   — Decline → turn → EMA9 crosses VWAP (long)
    SP_ATIER      — Uptrend → tight box → breakout (long)
    HH_QUALITY    — Opening drive → consolidation → breakout (long)
    EMA_FPIP      — Expansion → pullback → trigger (long)
    BDR_SHORT     — Breakdown → retest → rejection wick (short)
    BS_STRUCT     — Decline → HH/HL structure → range → breakout (long)
    ORL_FBD_LONG  — Breakdown below ORL → reclaim → HH confirm (long, GREEN days only)
"""

from .base import LiveStrategy, RawSignal
from .shared_indicators import SharedIndicators, IndicatorSnapshot
from .manager import StrategyManager

from .sc_sniper_live import SCSniperLive
from .fl_antichop_live import FLAntiChopLive
from .spencer_atier_live import SpencerATierLive
from .hitchhiker_live import HitchHikerLive
from .ema_fpip_live import EmaFpipLive
from .bdr_short_live import BDRShortLive
from .ema9_ft_live import EMA9FirstTouchLive
from .backside_live import BacksideStructureLive
from .orl_fbd_long_live import ORLFBDLongLive
from .orh_fbo_short_v2_live import ORHFBOShortV2Live
from .pdh_fbo_short_live import PDHFBOShortLive

__all__ = [
    "LiveStrategy", "RawSignal",
    "SharedIndicators", "IndicatorSnapshot",
    "StrategyManager",
    "SCSniperLive", "FLAntiChopLive", "SpencerATierLive", "HitchHikerLive",
    "EmaFpipLive", "BDRShortLive", "EMA9FirstTouchLive", "BacksideStructureLive",
    "ORLFBDLongLive", "ORHFBOShortV2Live", "PDHFBOShortLive",
]
