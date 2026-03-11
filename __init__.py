"""Consolidated Alert Overlay v4.5 — Python Signal Engine"""

from .config import OverlayConfig
from .models import Bar, Signal, SetupId, SetupFamily, SETUP_DISPLAY_NAME, FAMILY_SOUND
from .engine import SignalEngine
from .indicators import EMA, WildersMA, SMA, VWAPCalc

__all__ = [
    "OverlayConfig",
    "Bar",
    "Signal",
    "SetupId",
    "SetupFamily",
    "SignalEngine",
]
