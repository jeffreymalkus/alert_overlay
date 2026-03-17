"""
Data models for bars, signals, and state tracking.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Optional
import math

NaN = float("nan")


class SetupId(IntEnum):
    NONE = 0
    BOX_REV = 1
    MANIP = 2
    VWAP_SEP = 3
    EMA9_SEP = 4
    VWAP_KISS = 5
    EMA_PULL = 6
    EMA_RETEST = 7
    SECOND_CHANCE = 8
    SPENCER = 9
    FAILED_BOUNCE = 10
    EMA_RECLAIM = 11     # 9EMA reclaim scalp (quick dip → reclaim → continuation)
    EMA_CONFIRM = 12     # 9EMA confirmation scalp (hold near EMA → higher low → trigger)
    EMA_FPIP = 13        # 9EMA First Pullback In Play (strict in-play + volume contraction)
    SC_V2 = 14           # Second Chance V2 (strict expansion → contained reset → strong re-attack)
    BDR_SHORT = 15       # Breakdown-Retest Short (break support → weak retest → rejection)
    MCS = 16             # Momentum Confirm at Structure (engulf/strong candle + vol at VWAP/EMA9)
    VWAP_RECLAIM = 17    # VWAP Reclaim + Hold (below VWAP → reclaim → hold N bars → trigger)
    VKA = 18             # VWAP Kiss Accept (touch VWAP → hold near → expansion trigger)
    RSI_MIDLINE_LONG = 19    # RSI midline reclaim long (AM range-shift continuation)
    RSI_BOUNCEFAIL_SHORT = 20  # RSI bounce-fail short (AM weak-bounce rollover)
    HITCHHIKER = 21          # Opening drive → tight consolidation → breakout (institutional program)
    FASHIONABLY_LATE = 22    # 9EMA crosses VWAP after reversal (momentum convergence)
    BACKSIDE = 23            # Extended from VWAP → HH/HL → consolidation above 9EMA → snap to VWAP
    RUBBERBAND = 24          # Overextended acceleration → snapback candle (double-bar break)
    FL_MOMENTUM_REBUILD = 25  # 9EMA crosses above VWAP after meaningful decline + turn (long only)
    EMA9_FIRST_PB = 26        # First meaningful pullback to rising 9EMA after opening drive (long only)
    EMA9_BACKSIDE_RB = 27     # Extended below VWAP → HH/HL → range above rising 9EMA → break (long only)


class SetupFamily(IntEnum):
    REVERSAL = 1    # BOX_REV, MANIP, VWAP_SEP
    TREND = 2       # VWAP_KISS, EMA_PULL, EMA_RETEST
    MEAN_REV = 3    # EMA9_SEP
    BREAKOUT = 4    # SECOND_CHANCE, SPENCER
    SHORT_STRUCT = 5  # FAILED_BOUNCE — dedicated short-side family
    EMA_SCALP = 6     # EMA_RECLAIM, EMA_CONFIRM — tight 9EMA continuation scalps
    MOMENTUM = 7      # FASHIONABLY_LATE, RUBBERBAND — momentum/mean-rev snapback
    CONSOL_BREAK = 8  # HITCHHIKER, BACKSIDE — consolidation breakout patterns


SETUP_FAMILY_MAP = {
    SetupId.BOX_REV: SetupFamily.REVERSAL,
    SetupId.MANIP: SetupFamily.REVERSAL,
    SetupId.VWAP_SEP: SetupFamily.REVERSAL,
    SetupId.EMA9_SEP: SetupFamily.MEAN_REV,
    SetupId.VWAP_KISS: SetupFamily.TREND,
    SetupId.EMA_PULL: SetupFamily.TREND,
    SetupId.EMA_RETEST: SetupFamily.TREND,
    SetupId.SECOND_CHANCE: SetupFamily.BREAKOUT,
    SetupId.SPENCER: SetupFamily.BREAKOUT,
    SetupId.FAILED_BOUNCE: SetupFamily.SHORT_STRUCT,
    SetupId.BDR_SHORT: SetupFamily.SHORT_STRUCT,
    SetupId.EMA_RECLAIM: SetupFamily.EMA_SCALP,
    SetupId.EMA_CONFIRM: SetupFamily.EMA_SCALP,
    SetupId.EMA_FPIP: SetupFamily.EMA_SCALP,
    SetupId.SC_V2: SetupFamily.BREAKOUT,
    SetupId.MCS: SetupFamily.TREND,
    SetupId.VWAP_RECLAIM: SetupFamily.TREND,
    SetupId.VKA: SetupFamily.TREND,
    SetupId.RSI_MIDLINE_LONG: SetupFamily.TREND,
    SetupId.RSI_BOUNCEFAIL_SHORT: SetupFamily.SHORT_STRUCT,
    SetupId.HITCHHIKER: SetupFamily.CONSOL_BREAK,
    SetupId.FASHIONABLY_LATE: SetupFamily.MOMENTUM,
    SetupId.BACKSIDE: SetupFamily.CONSOL_BREAK,
    SetupId.RUBBERBAND: SetupFamily.MOMENTUM,
    SetupId.FL_MOMENTUM_REBUILD: SetupFamily.MOMENTUM,
    SetupId.EMA9_FIRST_PB: SetupFamily.EMA_SCALP,
    SetupId.EMA9_BACKSIDE_RB: SetupFamily.CONSOL_BREAK,
}

# Display names matching TOS alert strings exactly
SETUP_DISPLAY_NAME = {
    SetupId.NONE: "NONE",
    SetupId.BOX_REV: "BOX REV",
    SetupId.MANIP: "MANIP FADE",
    SetupId.VWAP_SEP: "VWAP SEP",
    SetupId.EMA9_SEP: "9EMA SEP",
    SetupId.VWAP_KISS: "VWAP KISS",
    SetupId.EMA_PULL: "EMA PULL",
    SetupId.EMA_RETEST: "9EMA RETEST",
    SetupId.SECOND_CHANCE: "2ND CHANCE",
    SetupId.SPENCER: "SPENCER",
    SetupId.FAILED_BOUNCE: "FAIL BOUNCE",
    SetupId.EMA_RECLAIM: "9EMA RECLAIM",
    SetupId.EMA_CONFIRM: "9EMA CONFIRM",
    SetupId.EMA_FPIP: "9EMA FPIP",
    SetupId.SC_V2: "2ND CHANCE V2",
    SetupId.BDR_SHORT: "BDR SHORT",
    SetupId.MCS: "MCS",
    SetupId.VWAP_RECLAIM: "VWAP RECLAIM",
    SetupId.VKA: "VK ACCEPT",
    SetupId.RSI_MIDLINE_LONG: "RSI MIDLINE LONG",
    SetupId.RSI_BOUNCEFAIL_SHORT: "RSI BOUNCEFAIL SHORT",
    SetupId.HITCHHIKER: "HITCHHIKER",
    SetupId.FASHIONABLY_LATE: "FASH LATE",
    SetupId.BACKSIDE: "BACKSIDE",
    SetupId.RUBBERBAND: "RUBBERBAND",
    SetupId.FL_MOMENTUM_REBUILD: "FL 9EMA REBUILD",
    SetupId.EMA9_FIRST_PB: "9EMA 1ST PB",
    SetupId.EMA9_BACKSIDE_RB: "9EMA BACKSIDE RB",
}

# Sound per family (matches TOS: Ding=reversal, Ring=trend, Chimes=mean-rev)
FAMILY_SOUND = {
    SetupFamily.REVERSAL: "Ding",
    SetupFamily.TREND: "Ring",
    SetupFamily.MEAN_REV: "Chimes",
    SetupFamily.BREAKOUT: "Bell",
    SetupFamily.SHORT_STRUCT: "Alert",
    SetupFamily.EMA_SCALP: "Ring",
    SetupFamily.MOMENTUM: "Chimes",
    SetupFamily.CONSOL_BREAK: "Bell",
}


@dataclass
class Bar:
    """Single OHLCV bar with timestamp metadata."""
    timestamp: datetime
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    # computed session info (set by engine)
    date: Optional[int] = None  # YYYYMMDD
    time_hhmm: int = 0          # HHMM
    # engine-computed indicator snapshots for [1] lookback
    _e9: float = 0.0
    _e20: float = 0.0
    _vwap: float = 0.0
    _pb_into_zone_long: bool = False
    _pb_into_zone_short: bool = False


@dataclass
class Signal:
    """Emitted when a validated setup fires."""
    bar_index: int
    timestamp: object
    direction: int              # 1 = long, -1 = short
    setup_id: SetupId = SetupId.NONE
    family: SetupFamily = SetupFamily.REVERSAL
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    risk: float = 0.0
    reward: float = 0.0
    rr_ratio: float = 0.0
    quality_score: int = 0
    fits_regime: bool = False
    vwap_bias: str = ""         # "ABV" / "BLW"
    or_direction: str = ""      # "UP" / "DN" / "FLAT"
    confluence_tags: list = field(default_factory=list)
    sweep_tags: list = field(default_factory=list)
    # Market context (populated when use_market_context=True)
    market_trend: int = 0          # -1=bear, 0=neutral, 1=bull
    rs_market: float = NaN         # stock RS vs SPY
    rs_sector: float = NaN         # stock RS vs sector ETF
    # Graded tradability scores (populated when use_market_context=True)
    tradability_long: float = 0.0   # higher = more favorable for longs
    tradability_short: float = 0.0  # lower (more negative) = more favorable for shorts
    # Universe source tagging
    universe: str = "STATIC"        # "STATIC", "IN_PLAY", or "BOTH"

    @property
    def setup_name(self) -> str:
        """TOS-matching display name for the setup method."""
        return SETUP_DISPLAY_NAME.get(self.setup_id, self.setup_id.name)

    @property
    def label(self) -> str:
        dir_str = "LONG" if self.direction == 1 else "SHORT"
        return f"{dir_str} {self.setup_name}"

    @property
    def sound(self) -> str:
        """TOS-matching sound name for the setup family."""
        return FAMILY_SOUND.get(self.family, "Ding")

    def to_tos_alert_string(self) -> str:
        """Format exactly like the TOS alert log output."""
        dir_str = "LONG" if self.direction == 1 else "SHORT"
        regime_str = "R" if self.fits_regime else "r"
        return (
            f"{dir_str} {self.setup_name} | "
            f"ENTRY {self.entry_price:.2f} | "
            f"STOP {self.stop_price:.2f} | "
            f"TGT {self.target_price:.2f} | "
            f"RR {self.rr_ratio:.2f} | "
            f"Q {self.quality_score} | "
            f"VWAP:{self.vwap_bias} | "
            f"OR:{self.or_direction} | "
            f"{regime_str}"
        )


@dataclass
class DayState:
    """Per-day state that resets on new session."""
    # Opening range
    or_high: float = NaN
    or_low: float = NaN
    or_open: float = NaN
    or_close: float = NaN
    or_ready: bool = False

    # Regime
    is_trend_day_latched: bool = False
    regime_known: bool = False

    # Position / line tracking (Option B)
    pos_dir: int = 0
    stop_line: float = NaN
    target_line: float = NaN
    setup_active: bool = False

    # Reversal confirmation latches
    rev_long_latch_high: float = NaN
    bars_since_rev_long: int = 999
    rev_short_latch_low: float = NaN
    bars_since_rev_short: int = 999

    # Sweep memory (bars since)
    bars_since_sweep_pdh: int = 999
    bars_since_sweep_pdl: int = 999
    bars_since_sweep_local_high: int = 999
    bars_since_sweep_local_low: int = 999

    # EMA cross flags
    crossed_up_flag: bool = False
    crossed_dn_flag: bool = False

    # Per-family cooldown counters
    cd_long_rev: int = 999
    cd_long_trend: int = 999
    cd_long_ema: int = 999
    cd_short_rev: int = 999
    cd_short_trend: int = 999
    cd_short_ema: int = 999

    # Per-family cooldown: breakout family
    cd_long_breakout: int = 999
    cd_short_breakout: int = 999

    # ── Second Chance state ──
    # Long breakout tracking
    sc_long_active: bool = False
    sc_long_level: float = NaN       # the broken key level
    sc_long_bo_high: float = NaN     # breakout bar high
    sc_long_bo_low: float = NaN      # breakout bar low
    sc_long_bo_vol: float = 0.0      # breakout bar volume
    sc_long_bars_since_bo: int = 999 # bars since breakout
    sc_long_retested: bool = False
    sc_long_retest_bar_high: float = NaN
    sc_long_retest_bar_low: float = NaN
    sc_long_bars_since_retest: int = 999
    sc_long_lowest_since_retest: float = NaN
    sc_long_level_tag: str = ""      # "ORH", "SWING"
    # Short breakout tracking
    sc_short_active: bool = False
    sc_short_level: float = NaN
    sc_short_bo_high: float = NaN
    sc_short_bo_low: float = NaN
    sc_short_bo_vol: float = 0.0
    sc_short_bars_since_bo: int = 999
    sc_short_retested: bool = False
    sc_short_retest_bar_high: float = NaN
    sc_short_retest_bar_low: float = NaN
    sc_short_bars_since_retest: int = 999
    sc_short_highest_since_retest: float = NaN
    sc_short_level_tag: str = ""

    # ── Failed Bounce state (short-only) ──
    fb_active: bool = False
    fb_level: float = NaN            # the broken level
    fb_level_tag: str = ""           # "VWAP", "ORL", "SWING"
    fb_bd_bar_high: float = NaN      # breakdown bar high
    fb_bd_bar_low: float = NaN       # breakdown bar low
    fb_bd_vol: float = 0.0           # breakdown bar volume
    fb_bars_since_bd: int = 999      # bars since breakdown
    fb_bounced: bool = False
    fb_bounce_bar_high: float = NaN  # bounce bar high (becomes stop ref)
    fb_bounce_bar_low: float = NaN
    fb_bars_since_bounce: int = 999
    fb_highest_since_bounce: float = NaN  # track extremes
    fb_bounce_has_wick: bool = False  # upper wick rejection on bounce bar

    # ── Breakdown-Retest state (short-only) ──
    bdr_active: bool = False
    bdr_level: float = NaN            # the broken level (VWAP, ORL, SWING)
    bdr_level_tag: str = ""           # "VWAP", "ORL", "SWING"
    bdr_bd_bar_high: float = NaN      # breakdown bar high
    bdr_bd_bar_low: float = NaN       # breakdown bar low
    bdr_bd_vol: float = 0.0           # breakdown bar volume
    bdr_bars_since_bd: int = 999      # bars since breakdown
    bdr_retested: bool = False
    bdr_retest_bar_high: float = NaN  # retest bar high (becomes stop ref)
    bdr_retest_bar_low: float = NaN   # retest bar low
    bdr_bars_since_retest: int = 999
    bdr_retest_vol: float = 0.0       # volume during retest phase

    # Per-family cooldown: short_struct family
    cd_short_struct: int = 999

    # ── EMA Scalp state ──
    # Per-trend retest counter (first retest only)
    ema_long_retest_count: int = 0     # how many retests since last impulse
    ema_short_retest_count: int = 0
    ema_long_impulse_high: float = NaN  # high of the impulse move (for breakout context)
    ema_short_impulse_low: float = NaN
    # Pullback tracking for confirm sub-type
    ema_long_pb_active: bool = False    # currently tracking a pullback
    ema_long_pb_low: float = NaN       # pullback low (for higher-low check)
    ema_long_pb_bars: int = 0          # bars in pullback zone
    ema_long_pb_closes_below: int = 0  # closes below EMA9 during pullback
    ema_short_pb_active: bool = False
    ema_short_pb_high: float = NaN
    ema_short_pb_bars: int = 0
    ema_short_pb_closes_above: int = 0

    # Per-family cooldown: ema_scalp
    cd_long_ema_scalp: int = 999
    cd_short_ema_scalp: int = 999

    # ── EMA FPIP state (First Pullback In Play) ──
    # Long expansion tracking
    fpip_long_expansion_active: bool = False
    fpip_long_expansion_high: float = NaN
    fpip_long_expansion_low: float = NaN     # low at start of expansion
    fpip_long_expansion_bars: int = 0
    fpip_long_expansion_distance: float = 0.0
    fpip_long_expansion_total_vol: float = 0.0
    fpip_long_expansion_avg_vol: float = 0.0
    fpip_long_expansion_overlap_bars: int = 0
    fpip_long_qual_expansion_exists: bool = False
    fpip_long_expansion_prev_close: float = NaN  # for overlap detection
    # Long pullback tracking
    fpip_long_pb_started: bool = False
    fpip_long_pb_bars: int = 0
    fpip_long_pb_low: float = NaN
    fpip_long_pb_total_vol: float = 0.0
    fpip_long_pb_avg_vol: float = 0.0
    fpip_long_pb_heavy_bars: int = 0         # bars with vol >= expansion_avg_vol

    # Short expansion tracking
    fpip_short_expansion_active: bool = False
    fpip_short_expansion_low: float = NaN
    fpip_short_expansion_high: float = NaN   # high at start of expansion
    fpip_short_expansion_bars: int = 0
    fpip_short_expansion_distance: float = 0.0
    fpip_short_expansion_total_vol: float = 0.0
    fpip_short_expansion_avg_vol: float = 0.0
    fpip_short_expansion_overlap_bars: int = 0
    fpip_short_qual_expansion_exists: bool = False
    fpip_short_expansion_prev_close: float = NaN
    # Short pullback tracking
    fpip_short_pb_started: bool = False
    fpip_short_pb_bars: int = 0
    fpip_short_pb_high: float = NaN
    fpip_short_pb_total_vol: float = 0.0
    fpip_short_pb_avg_vol: float = 0.0
    fpip_short_pb_heavy_bars: int = 0

    # ── Second Chance V2 state (expansion → contained reset → re-attack) ──
    # Long expansion tracking
    sc2_long_expansion_active: bool = False
    sc2_long_expansion_high: float = NaN
    sc2_long_expansion_low: float = NaN
    sc2_long_expansion_bars: int = 0
    sc2_long_expansion_distance: float = 0.0
    sc2_long_expansion_total_vol: float = 0.0
    sc2_long_expansion_avg_vol: float = 0.0
    sc2_long_expansion_overlap_bars: int = 0
    sc2_long_expansion_prev_close: float = NaN
    sc2_long_qual_expansion_exists: bool = False
    sc2_long_expansion_level: float = NaN        # the breakout level
    sc2_long_expansion_level_tag: str = ""        # "ORH", "SWING"
    # Long reset tracking
    sc2_long_reset_active: bool = False
    sc2_long_reset_bars: int = 0
    sc2_long_reset_low: float = NaN
    sc2_long_reset_total_vol: float = 0.0
    sc2_long_reset_avg_vol: float = 0.0
    sc2_long_reset_heavy_bars: int = 0
    sc2_long_reset_high: float = NaN             # highest point during reset
    sc2_long_reset_total_range: float = 0.0      # for avg range calculation
    sc2_long_reattack_used: bool = False          # first reattack consumed
    # Short expansion tracking
    sc2_short_expansion_active: bool = False
    sc2_short_expansion_high: float = NaN
    sc2_short_expansion_low: float = NaN
    sc2_short_expansion_bars: int = 0
    sc2_short_expansion_distance: float = 0.0
    sc2_short_expansion_total_vol: float = 0.0
    sc2_short_expansion_avg_vol: float = 0.0
    sc2_short_expansion_overlap_bars: int = 0
    sc2_short_expansion_prev_close: float = NaN
    sc2_short_qual_expansion_exists: bool = False
    sc2_short_expansion_level: float = NaN
    sc2_short_expansion_level_tag: str = ""
    # Short reset tracking
    sc2_short_reset_active: bool = False
    sc2_short_reset_bars: int = 0
    sc2_short_reset_high: float = NaN
    sc2_short_reset_total_vol: float = 0.0
    sc2_short_reset_avg_vol: float = 0.0
    sc2_short_reset_heavy_bars: int = 0
    sc2_short_reset_low: float = NaN
    sc2_short_reset_total_range: float = 0.0
    sc2_short_reattack_used: bool = False

    # ── Spencer state ──
    # Session tracking for precondition
    session_low: float = NaN
    session_high: float = NaN
    session_open: float = NaN

    # ── VWAP Reclaim + Hold state (long-only) ──
    vr_was_below: bool = False          # price was below VWAP (reclaim prerequisite)
    vr_hold_count: int = 0              # bars held above VWAP after reclaim
    vr_hold_low: float = NaN            # lowest low during hold period (becomes stop)
    vr_triggered: bool = False          # already fired one trade today

    # ── VK Accept state (long-only) ──
    vka_touched: bool = False           # price touched/kissed VWAP (prerequisite)
    vka_hold_count: int = 0             # bars held near/above VWAP after touch
    vka_hold_low: float = NaN           # lowest low during hold period (becomes stop)
    vka_hold_high: float = NaN          # highest high during hold period (micro-high)
    vka_triggered: bool = False         # already fired one trade today

    # ── HitchHiker state ──
    hh_long_drive_confirmed: bool = False     # opening drive higher detected
    hh_long_drive_high: float = NaN           # high of the drive
    hh_long_consol_active: bool = False       # in consolidation phase
    hh_long_consol_start_bar: int = 0         # bar index consol started
    hh_long_consol_bars: int = 0              # bars in consolidation
    hh_long_consol_high: float = NaN          # range high of consolidation
    hh_long_consol_low: float = NaN           # range low of consolidation
    hh_long_consol_total_vol: float = 0.0     # total vol in consolidation
    hh_long_consol_total_wick: float = 0.0    # total wick ratio (choppiness)
    hh_long_triggered: bool = False
    hh_short_drive_confirmed: bool = False
    hh_short_drive_low: float = NaN
    hh_short_consol_active: bool = False
    hh_short_consol_start_bar: int = 0
    hh_short_consol_bars: int = 0
    hh_short_consol_high: float = NaN
    hh_short_consol_low: float = NaN
    hh_short_consol_total_vol: float = 0.0
    hh_short_consol_total_wick: float = 0.0
    hh_short_triggered: bool = False

    # ── Fashionably Late state ──
    fl_long_prior_e9: float = NaN             # previous bar 9EMA
    fl_long_prior_vwap: float = NaN           # previous bar VWAP
    fl_long_crossed: bool = False             # 9EMA crossed above VWAP
    fl_long_cross_price: float = NaN          # price at cross
    fl_long_lod_at_cross: float = NaN         # low of day at time of cross
    fl_long_flat_ema_bars: int = 0            # consecutive bars 9EMA was flat before cross
    fl_long_triggered: bool = False
    fl_short_prior_e9: float = NaN
    fl_short_prior_vwap: float = NaN
    fl_short_crossed: bool = False
    fl_short_cross_price: float = NaN
    fl_short_hod_at_cross: float = NaN
    fl_short_flat_ema_bars: int = 0
    fl_short_triggered: bool = False

    # ── Back$ide state ──
    bs_long_extended: bool = False            # was extended below VWAP
    bs_long_hh_count: int = 0                 # higher highs counted
    bs_long_hl_count: int = 0                 # higher lows counted
    bs_long_prev_swing_high: float = NaN
    bs_long_prev_swing_low: float = NaN
    bs_long_consol_active: bool = False
    bs_long_consol_bars: int = 0
    bs_long_consol_high: float = NaN
    bs_long_consol_low: float = NaN
    bs_long_consol_total_vol: float = 0.0
    bs_long_triggered: bool = False
    bs_short_extended: bool = False
    bs_short_ll_count: int = 0
    bs_short_lh_count: int = 0
    bs_short_prev_swing_high: float = NaN
    bs_short_prev_swing_low: float = NaN
    bs_short_consol_active: bool = False
    bs_short_consol_bars: int = 0
    bs_short_consol_high: float = NaN
    bs_short_consol_low: float = NaN
    bs_short_consol_total_vol: float = 0.0
    bs_short_triggered: bool = False

    # ── RubberBand state ──
    rb_long_trend_bars: int = 0               # bars in controlled downtrend
    rb_long_accel_detected: bool = False       # acceleration phase detected
    rb_long_accel_start_bar: int = 0
    rb_long_prev_leg_avg_vol: float = 0.0     # avg vol of pre-acceleration leg
    rb_long_prev_leg_avg_range: float = 0.0   # avg range of pre-acceleration leg
    rb_long_attempts: int = 0                 # snapback attempts today (max 2)
    rb_long_triggered: bool = False
    rb_short_trend_bars: int = 0
    rb_short_accel_detected: bool = False
    rb_short_accel_start_bar: int = 0
    rb_short_prev_leg_avg_vol: float = 0.0
    rb_short_prev_leg_avg_range: float = 0.0
    rb_short_attempts: int = 0
    rb_short_triggered: bool = False

    # Per-family cooldown: momentum and consol_break
    cd_long_momentum: int = 999
    cd_short_momentum: int = 999
    cd_long_consol_break: int = 999
    cd_short_consol_break: int = 999

    # Volume bars history for RubberBand top-N check
    rb_volume_bars: list = field(default_factory=list)  # list of volumes today

    # ── FL Momentum Rebuild state (long-only) ──
    # Phase 1: Meaningful decline tracking
    flr_session_high: float = NaN             # running session high
    flr_decline_active: bool = False          # currently in decline phase
    flr_decline_high: float = NaN             # high before decline started
    flr_decline_low: float = NaN              # lowest point of decline
    flr_decline_atr: float = 0.0             # decline distance in ATR at time of decline
    # Phase 2: Turn / base detection
    flr_turn_detected: bool = False           # HL confirmed after decline
    flr_turn_hl_bar: int = 0                 # bar index of the HL
    flr_turn_low: float = NaN                # the higher low price
    flr_turn_confirm_bars: int = 0           # consecutive bars confirming HL (for 2-bar confirm)
    flr_base_bars: int = 0                   # bars since turn (for time gate)
    # Phase 3: Cross trigger tracking
    flr_prior_e9: float = NaN                # previous bar 9EMA (for cross detection)
    flr_prior_vwap: float = NaN              # previous bar VWAP
    flr_prior_prior_e9: float = NaN          # 2-bars-ago 9EMA (for slope acceleration)
    flr_cross_fired: bool = False            # cross happened — waiting for confirm bar
    flr_cross_bar_close: float = NaN         # close of the cross bar
    flr_cross_bar_vol: float = 0.0           # volume of cross bar
    # Gating
    flr_triggered: bool = False              # one signal per day

    # ── EMA9 First Pullback state (long-only) ──
    # Phase 1: Opening drive detection
    e9pb_drive_active: bool = False           # opening drive higher detected
    e9pb_drive_high: float = NaN              # high of the drive
    e9pb_drive_bars: int = 0                 # bars of upward movement from open
    e9pb_drive_distance: float = 0.0          # distance from open to drive high in ATR
    e9pb_drive_confirmed: bool = False        # drive meets criteria (distance + bars)
    # Phase 2: Pullback tracking
    e9pb_pb_active: bool = False              # in pullback phase
    e9pb_pb_bars: int = 0                    # bars in pullback
    e9pb_pb_low: float = NaN                 # lowest point of pullback
    e9pb_pb_depth_pct: float = 0.0           # pullback depth as % of drive
    e9pb_pb_vol_total: float = 0.0           # total volume during pullback
    e9pb_pb_closed_below_e9: int = 0         # closes below 9EMA during PB
    # Phase 3: Trigger
    e9pb_triggered: bool = False             # one signal per day

    # ── EMA9 Backside Range Break state (long-only) ──
    # Phase 1: Extension below VWAP
    e9rb_extended: bool = False               # was extended below VWAP
    e9rb_extension_low: float = NaN           # lowest point of extension
    e9rb_extension_distance: float = 0.0      # extension in ATR
    # Phase 2: HH/HL recovery
    e9rb_hh_count: int = 0                   # higher highs counted
    e9rb_hl_count: int = 0                   # higher lows counted
    e9rb_prev_swing_high: float = NaN
    e9rb_prev_swing_low: float = NaN
    e9rb_recovery_confirmed: bool = False     # HH+HL requirements met
    # Phase 3: Range above 9EMA
    e9rb_range_active: bool = False           # consolidation range forming
    e9rb_range_bars: int = 0
    e9rb_range_high: float = NaN
    e9rb_range_low: float = NaN
    e9rb_range_vol_total: float = 0.0
    # Gating
    e9rb_triggered: bool = False             # one signal per day

    # Per-family cooldown for new strategies (reuse existing family cooldowns)
    # FL_MOMENTUM_REBUILD uses cd_long_momentum (MOMENTUM family)
    # EMA9_FIRST_PB uses cd_long_ema_scalp (EMA_SCALP family)
    # EMA9_BACKSIDE_RB uses cd_long_consol_break (CONSOL_BREAK family)

    # Previous bar values needed for [1] references
    prev_has_long_signal: bool = False
    prev_has_short_signal: bool = False
