"""
Production Strategy Sleeve — Single source of truth.

Both replay.py and dashboard.py import from here.
When strategies are added, removed, or reconfigured, change THIS file only.
"""

from copy import deepcopy
from .shared.config import StrategyConfig
from .live.hitchhiker_live import HitchHikerLive
from .live.ema_fpip_live import EmaFpipLive
from .live.spencer_atier_live import SpencerATierLive
from .live.orh_fbo_short_v2_live import ORHFBOShortV2Live
from .live.ema9_ft_live import EMA9FirstTouchLive
from .live.bdr_short_live import BDRShortLive
from .live.backside_live import BacksideStructureLive
from .live.gap_give_go_live import GapGiveGoLive


# ══════════════════════════════════════════════════════════════
# VARIANT PRESET HELPERS
# ══════════════════════════════════════════════════════════════

def _make_fpip_v3_b(base):
    """EMA_FPIP V3_B: prev-high-break entry, fixed 2.0R, relaxed pullback."""
    c = deepcopy(base)
    c.fpip_max_pullback_depth = 0.70
    c.fpip_max_pullback_vol_ratio = 0.95
    c.fpip_trigger_require_close_above_prev_high = True
    c.fpip_entry_mode = "prev_high_break"
    c.fpip_entry_buffer = 0.01
    c.fpip_target_mode = "fixed_rr"
    c.fpip_target_rr = 2.0
    return c


def _make_sp_v2_simple(base):
    """SP_V2_SIMPLE: time<=14:05, RR<=1.9, no struct_q floor."""
    c = deepcopy(base)
    c.sp_v2_enabled = True
    c.sp_v2_max_selected_rr = 1.9
    c.sp_v2_min_structure_quality = 0.0
    c.sp_v2_min_bar_return_pct = 0.0
    return c


def _make_bdr_v3_c(base):
    """BDR_V3_C: weak retest break, ORL+swing, no VWAP, 2.0R target."""
    c = deepcopy(base)
    c.bdr_v3_enabled = True
    c.bdr_use_orl_level = True
    c.bdr_use_swing_low_level = True
    c.bdr_use_vwap_level = False
    c.bdr_v3_time_start = 1025
    c.bdr_v3_time_end = 1040
    c.bdr_setup_mode = "weak_retest_break"
    c.bdr_max_reclaim_above_level_atr = 0.10
    c.bdr_retest_close_max_pos = 0.55
    c.bdr_retest_body_max_pct = 0.55
    c.bdr_retest_min_upper_wick_pct = 0.10
    c.bdr_require_retest_below_vwap = True
    c.bdr_require_retest_below_ema9 = True
    c.bdr_require_trigger_below_vwap = True
    c.bdr_require_trigger_below_ema9 = True
    c.bdr_require_retest_vol_not_stronger_than_breakdown = True
    c.bdr_entry_mode = "retest_low_break"
    c.bdr_entry_buffer = 0.01
    c.bdr_trigger_bars_after_retest = 2
    c.bdr_stop_mode = "retest_high"
    c.bdr_v3_stop_buffer = 0.01
    c.bdr_target_mode_v3 = "fixed_rr"
    c.bdr_target_rr_v3 = 2.0
    c.bdr_skip_generic_trigger_body_filter = True
    c.bdr_skip_generic_trigger_vol_filter = True
    c.bdr_require_red_trend = False
    return c


def _make_ema9_v5_c(base):
    """EMA9_V5_C: $0.35 stop floor, $25-250 price band, structural target."""
    c = deepcopy(base)
    c.ema9_v5_enabled = True
    c.ema9_v5_time_start = 950
    c.ema9_v5_time_end = 1130
    c.ema9_v5_min_stop_dollar = 0.35
    c.ema9_v5_min_ip_score = 0.80
    c.ema9_v5_min_quality_score = 3.0
    c.ema9_v5_price_min = 25.0
    c.ema9_v5_price_max = 150.0
    c.ema9_v5_struct_min_rr = 0.0
    c.ema9_v5_struct_max_rr = 5.0
    return c


# ══════════════════════════════════════════════════════════════
# PRODUCTION SLEEVE — THE SINGLE SOURCE OF TRUTH
# ══════════════════════════════════════════════════════════════

def build_production_strategies(strat_cfg: StrategyConfig) -> list:
    """Build the production strategy list.

    Called by BOTH replay.py and dashboard.py.
    Change strategies here, and both paths update automatically.

    Returns: list of LiveStrategy instances
    """
    return [
        # ── LONG CONTINUATION ──
        HitchHikerLive(strat_cfg),                                                      # HH_QUALITY
        EmaFpipLive(_make_fpip_v3_b(strat_cfg), strategy_name="EMA_FPIP_V3_B"),        # FPIP V3_B
        SpencerATierLive(_make_sp_v2_simple(strat_cfg), strategy_name="SP_V2_SIMPLE"),  # Spencer V2
        EMA9FirstTouchLive(_make_ema9_v5_c(strat_cfg), strategy_name="EMA9_V5_C"),      # EMA9 V5_C
        BacksideStructureLive(strat_cfg),                                                # BS_STRUCT
        GapGiveGoLive(strat_cfg),                                                            # GGG_LONG_V1

        # ── SHORT FAILURE / BREAKDOWN ──
        ORHFBOShortV2Live(strat_cfg),                                                    # ORH_FBO_V2_A + V2_B
        BDRShortLive(_make_bdr_v3_c(strat_cfg), strategy_name="BDR_V3_C"),              # BDR V3_C
    ]
