"""
StrategyConfig — timeframe-aware configuration for new strategy framework.
Uses {1: val, 5: val} dicts for timeframe-specific thresholds.
Access: cfg.get(cfg.ip_gap_min) → returns value for current timeframe_min.
"""

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class StrategyConfig:
    """Central config for all new strategies. Timeframe-aware."""

    timeframe_min: int = 5      # 1 or 5

    # ── In-play proxy (V1 — legacy, kept for fallback) ──
    ip_mode: str = "hybrid"  # "list_only" | "proxy_only" | "hybrid"
    ip_gap_min: Dict[int, float] = field(default_factory=lambda: {1: 0.005, 5: 0.01})
    ip_rvol_min: Dict[int, float] = field(default_factory=lambda: {1: 1.5, 5: 2.0})
    ip_dolvol_min: Dict[int, float] = field(default_factory=lambda: {1: 500_000, 5: 1_000_000})
    ip_range_expansion_min: Dict[int, float] = field(default_factory=lambda: {1: 1.5, 5: 1.5})
    ip_first_n_bars: Dict[int, int] = field(default_factory=lambda: {1: 15, 5: 3})

    # ── In-play proxy V2 (percentile-ranked, two-stage, rolling) ──
    ip_v2_enabled: bool = True
    ip_v2_threshold_provisional: float = 0.80
    ip_v2_threshold_confirmed: float = 0.80
    ip_v2_provisional_hhmm: int = 940
    ip_v2_confirmed_hhmm: int = 1000
    ip_v2_recompute_confirmed_each_bar: bool = True
    ip_v2_debug_logging: bool = True

    # ── Market regime ──
    regime_require_green: bool = True
    regime_require_spy_above_vwap: bool = False
    regime_require_ema_aligned: bool = False  # EMA9 > EMA20 on SPY

    # ── Rejection filter thresholds ──
    rej_chop_lookback: Dict[int, int] = field(default_factory=lambda: {1: 30, 5: 6})
    rej_chop_overlap_max: float = 0.70       # max overlap ratio to not be "choppy"
    rej_maturity_bars: Dict[int, int] = field(default_factory=lambda: {1: 120, 5: 24})
    rej_distance_atr_max: float = 2.5         # max distance from VWAP in ATR
    rej_trigger_body_min_pct: float = 0.40    # min body ratio for trigger bar
    rej_trigger_vol_min_frac: float = 0.80    # trigger bar vol >= this * vol_ma
    rej_bigger_picture_enabled: bool = True

    # ── Quality scoring ──
    quality_a_min: int = 6    # score >= 6 → A tier
    quality_b_min: int = 4    # score >= 4 → B tier, else C

    # ── Confluence gate (live quality filter) ──
    # Per-strategy minimum confluence count to pass quality gate.
    # Default=3 for most strategies. Shorts (ORH/PDH) benefit from tighter gates.
    # Data-driven thresholds from counterfactual on 210-day replay:
    #   ORH_FBO_V2_A conf>=4: PF 1.11→1.25, R +30.4→+46.1 (+15.7R)
    #   ORH_FBO_V2_B conf>=4: PF 1.35→1.42, R +32.9→+37.9 (+5.0R)
    #   PDH_FBO_B    conf>=5: PF 1.01→1.26, R +0.1→+4.6  (+4.5R)
    min_confluence_count: int = 3  # universal default
    min_confluence_by_strategy: Dict[str, int] = field(default_factory=lambda: {
        "ORH_FBO_V2_A": 4,
        "ORH_FBO_V2_B": 4,
        "PDH_FBO_B": 5,
    })

    # ── Strategy enables ──
    enable_sc_sniper: bool = True
    enable_fl_antichop: bool = True

    # ── SC Sniper thresholds ──
    sc_time_start: Dict[int, int] = field(default_factory=lambda: {1: 1000, 5: 1000})
    sc_time_end: Dict[int, int] = field(default_factory=lambda: {1: 1400, 5: 1400})
    sc_break_atr_min: float = 0.10            # min break distance in ATR
    sc_break_vol_frac: float = 1.10           # break bar vol >= this * vol_ma
    sc_strong_bo_vol_mult: float = 1.25       # strong breakout volume multiplier
    sc_break_bar_range_frac: float = 0.60     # break bar range >= frac * median(10)
    sc_break_close_pct: float = 0.50          # close in top/bottom 50% of bar
    sc_retest_window: Dict[int, int] = field(default_factory=lambda: {1: 18, 5: 6})
    sc_retest_proximity_atr: float = 0.05     # how close to level counts as "touch"
    sc_retest_max_depth_atr: float = 0.35     # max overshoot past level
    sc_retest_max_vol_frac: float = 1.5       # retest bar vol cap
    sc_confirm_window: Dict[int, int] = field(default_factory=lambda: {1: 9, 5: 3})
    sc_confirm_vol_frac: float = 0.80         # confirm bar vol minimum
    sc_stop_buffer: float = 0.02              # dollar buffer below stop
    sc_target_rr: float = 2.0                 # fallback / cap in R multiples
    sc_target_mode: str = "structural"        # "fixed_rr" | "structural"
    sc_struct_min_rr: float = 1.0
    sc_struct_max_rr: float = 3.0
    sc_max_bars: Dict[int, int] = field(default_factory=lambda: {1: 234, 5: 78})

    # ── FL AntiChop thresholds ──
    fl_time_start: Dict[int, int] = field(default_factory=lambda: {1: 1030, 5: 1030})
    fl_time_end: Dict[int, int] = field(default_factory=lambda: {1: 1300, 5: 1300})
    fl_min_decline_atr: float = 3.0           # min decline in ATR to qualify
    fl_hl_tolerance_atr: float = 0.15         # tolerance for higher-low detection
    fl_turn_confirm_bars: Dict[int, int] = field(default_factory=lambda: {1: 12, 5: 4})
    fl_max_base_bars: Dict[int, int] = field(default_factory=lambda: {1: 30, 5: 10})
    fl_cross_close_above_vwap: bool = True
    fl_cross_body_pct: float = 0.40
    fl_cross_vol_min_rvol: float = 0.50
    fl_stop_mode: str = "source_faithful"     # "current_hybrid" | "source_faithful"
    fl_stop_frac: float = 0.50               # fraction of decline for stop distance (fallback)
    fl_stop_buffer_atr: float = 0.10          # ATR buffer below turn_low for structural stop
    fl_target_rr: float = 1.5                  # fallback / cap
    fl_target_mode: str = "structural"        # "fixed_rr" | "structural"
    fl_struct_min_rr: float = 1.0
    fl_struct_max_rr: float = 3.0
    fl_max_bars: Dict[int, int] = field(default_factory=lambda: {1: 156, 5: 52})
    fl_chop_lookback: Dict[int, int] = field(default_factory=lambda: {1: 20, 5: 5})
    fl_chop_overlap_max: float = 0.60         # stricter than universal (anti-chop)

    # ── Spencer A-Tier thresholds ──
    enable_spencer: bool = True
    sp_time_start: Dict[int, int] = field(default_factory=lambda: {1: 1000, 5: 1000})
    sp_time_end: Dict[int, int] = field(default_factory=lambda: {1: 1430, 5: 1430})
    sp_trend_advance_atr: float = 0.75       # min advance from session low (daily ATR)
    sp_trend_advance_vwap_atr: float = 0.50  # or min above VWAP (intra ATR)
    sp_box_min_bars: Dict[int, int] = field(default_factory=lambda: {1: 20, 5: 4})
    sp_box_max_bars: Dict[int, int] = field(default_factory=lambda: {1: 40, 5: 8})
    sp_box_max_range_atr: float = 2.00       # max box range / intra ATR
    sp_box_upper_close_pct: float = 0.75     # fraction of closes in upper half
    sp_box_max_below_ema9: int = 1           # max closes below EMA9 in box
    sp_box_min_vol_frac: float = 0.70        # min avg vol in box vs vol_ma
    sp_box_failed_bo_limit: int = 2          # max failed breakout attempts
    sp_box_failed_bo_atr: float = 0.03       # overshoot threshold for failed BO
    sp_break_clearance_atr: float = 0.03     # min close above box high
    sp_break_vol_frac: float = 1.10          # breakout vol >= this * vol_ma
    sp_break_close_pct: float = 0.70         # close in upper 30% of bar
    sp_extension_atr: float = 1.50           # reject if too extended from open
    sp_stop_buffer: float = 0.02             # $ buffer below box low
    sp_target_rr: float = 3.0               # fallback / cap
    sp_target_mode: str = "structural"      # "fixed_rr" | "structural"
    sp_struct_min_rr: float = 1.0
    sp_struct_max_rr: float = 3.0
    sp_max_bars: Dict[int, int] = field(default_factory=lambda: {1: 234, 5: 78})

    # ── HitchHiker Program Quality thresholds (SMB-faithful v2) ──
    enable_hitchhiker: bool = True
    hh_time_start: Dict[int, int] = field(default_factory=lambda: {1: 935, 5: 935})
    hh_time_end: Dict[int, int] = field(default_factory=lambda: {1: 1100, 5: 1100})   # SMB ideal <9:59; allow to 11:00 for 5m sample
    hh_drive_min_atr: float = 1.0            # opening drive >= 1.0 intra ATR
    hh_consol_min_bars: Dict[int, int] = field(default_factory=lambda: {1: 10, 5: 2})  # SMB: 5 min min; 2 bars on 5m filters noise single-bar "consol"
    hh_consol_max_bars: Dict[int, int] = field(default_factory=lambda: {1: 60, 5: 12}) # SMB: 20 min max; allow wider for 5m (60min)
    hh_consol_max_range_atr: float = 2.0     # consolidation range <= 2.0 ATR
    hh_consol_upper_pct: float = 0.50        # Keep upper half (0.66 too strict on 5m)
    hh_break_vol_frac: float = 1.10          # breakout vol >= 110% of consol avg
    hh_max_wick_pct: float = 0.70            # max avg wick pct (reject choppy)
    hh_stop_buffer: float = 0.02             # SMB: $0.02 below consol low
    hh_target_rr: float = 1.9               # SMB: 1.9:1 reward-to-risk
    hh_target_mode: str = "structural"      # "fixed_rr" | "structural"
    hh_struct_min_rr: float = 1.0
    hh_struct_max_rr: float = 3.0
    hh_max_bars: Dict[int, int] = field(default_factory=lambda: {1: 120, 5: 20})

    # ── EMA FPIP A-Tier thresholds ──
    enable_fpip: bool = True
    fpip_time_start: Dict[int, int] = field(default_factory=lambda: {1: 940, 5: 940})
    fpip_time_end: Dict[int, int] = field(default_factory=lambda: {1: 1530, 5: 1400})
    fpip_expansion_min_bars: int = 2        # min bars in expansion leg
    fpip_expansion_max_bars: int = 6        # max bars (cap impulse tracking)
    fpip_min_expansion_atr: float = 0.40    # expansion >= 0.40 intra ATR
    fpip_min_expansion_avg_vol: float = 0.80  # exp avg vol >= this * vol_ma (relaxed from 1.0)
    fpip_max_impulse_overlap: float = 0.35  # max body overlap ratio (relaxed from 0.30)
    fpip_max_pullback_depth: float = 0.55   # PB depth <= this * expansion dist (relaxed from 0.50)
    fpip_max_pullback_bars: Dict[int, int] = field(default_factory=lambda: {1: 6, 5: 6})  # relaxed from 4
    fpip_max_pullback_vol_ratio: float = 0.80  # PB avg vol <= this * exp avg vol (relaxed from 0.75)
    fpip_max_heavy_pb_bars: int = 2         # max bars at expansion volume (relaxed from 1)
    fpip_min_trigger_close_pct: float = 0.55  # close in top 55% (relaxed from 0.60)
    fpip_min_trigger_body_pct: float = 0.45   # body >= 45% (relaxed from 0.50)
    fpip_trigger_vol_vs_pb: float = 1.20    # trigger vol >= this * pb_avg_vol (relaxed from 1.3)
    fpip_first_pullback_only: bool = True   # only first pullback
    fpip_stop_buffer: float = 0.02          # $ buffer below PB low
    fpip_target_rr: float = 2.0             # fallback / cap
    fpip_target_mode: str = "structural"    # "fixed_rr" | "structural"
    # FPIP V3 controls (leave False/"close" for legacy behavior)
    fpip_trigger_require_close_above_prev_high: bool = False
    fpip_entry_mode: str = "close"          # "close" | "prev_high_break"
    fpip_entry_buffer: float = 0.01
    fpip_struct_min_rr: float = 1.0
    fpip_struct_max_rr: float = 3.0
    fpip_max_bars: Dict[int, int] = field(default_factory=lambda: {1: 156, 5: 24})

    # ── BDR SHORT thresholds ──
    enable_bdr: bool = True
    bdr_time_start: Dict[int, int] = field(default_factory=lambda: {1: 1000, 5: 1000})
    bdr_time_end: Dict[int, int] = field(default_factory=lambda: {1: 1100, 5: 1100})  # AM only
    bdr_break_atr_min: float = 0.15         # min close distance below level
    bdr_break_bar_range_frac: float = 0.70  # break bar range >= frac * median(10)
    bdr_break_vol_frac: float = 0.90        # break bar vol >= this * vol_ma
    bdr_break_close_pct: float = 0.40       # close in bottom 60% of bar (0.40 = 60% from high→low)
    bdr_retest_window: Dict[int, int] = field(default_factory=lambda: {1: 40, 5: 8})
    bdr_retest_proximity_atr: float = 0.30  # high within 0.3 ATR of level
    bdr_retest_max_reclaim_atr: float = 0.30  # max overshoot above level
    bdr_confirm_window: Dict[int, int] = field(default_factory=lambda: {1: 20, 5: 4})
    bdr_min_rejection_wick_pct: float = 0.30  # KEY: wick >= 30% of bar range
    bdr_stop_buffer_atr: float = 0.30       # stop buffer in ATR above retest high
    bdr_min_stop_atr: float = 0.50          # minimum stop distance in ATR
    bdr_target_rr: float = 2.0              # fallback / cap in R multiples
    bdr_target_mode: str = "structural"    # "fixed_rr" | "structural"
    bdr_struct_min_rr: float = 1.0         # skip if structural target < 1.0R away
    bdr_struct_max_rr: float = 3.0         # cap structural target at 3.0R
    bdr_max_bars: Dict[int, int] = field(default_factory=lambda: {1: 40, 5: 8})
    # BDR regime: RED + TREND (SPY close in bottom 25% of range)
    bdr_require_red_trend: bool = True
    bdr_spy_trend_pct: float = 0.25         # SPY close in bottom X% of day range

    # ── BDR V3 controls ──
    bdr_v3_enabled: bool = False
    bdr_use_orl_level: bool = True
    bdr_use_swing_low_level: bool = True
    bdr_use_vwap_level: bool = True          # legacy True; V3 presets set False
    bdr_v3_time_start: int = 1000
    bdr_v3_time_end: int = 1100
    bdr_setup_mode: str = "legacy_rejection_close"  # "legacy_rejection_close" | "weak_retest_break" | "failed_reclaim_break"
    bdr_max_reclaim_above_level_atr: float = 0.10
    bdr_retest_close_max_pos: float = 0.55
    bdr_retest_body_max_pct: float = 0.55
    bdr_retest_min_upper_wick_pct: float = 0.10
    bdr_require_retest_below_vwap: bool = False
    bdr_require_retest_below_ema9: bool = False
    bdr_require_trigger_below_vwap: bool = False
    bdr_require_trigger_below_ema9: bool = False
    bdr_require_retest_vol_not_stronger_than_breakdown: bool = False
    bdr_entry_mode: str = "close"            # "close" | "retest_low_break"
    bdr_entry_buffer: float = 0.01
    bdr_trigger_bars_after_retest: int = 2
    bdr_stop_mode: str = "retest_high"       # "retest_high" | "trigger_bar_high"
    bdr_v3_stop_buffer: float = 0.01
    bdr_target_mode_v3: str = "fixed_rr"
    bdr_target_rr_v3: float = 1.5
    bdr_skip_generic_trigger_body_filter: bool = False
    bdr_skip_generic_trigger_vol_filter: bool = False

    # ── EMA9 FirstTouch Only thresholds ──
    enable_ema9ft: bool = True
    e9ft_time_start: Dict[int, int] = field(default_factory=lambda: {1: 935, 5: 935})
    e9ft_time_end: Dict[int, int] = field(default_factory=lambda: {1: 1115, 5: 1115})
    e9ft_drive_min_atr: float = 0.80        # min opening drive (high - open) in intra ATR
    e9ft_above_vwap: bool = True             # price must be above VWAP at pullback
    e9ft_ema9_above_ema20: bool = True       # 5m E9 > E20 structure
    e9ft_max_pullback_depth_atr: float = 0.60  # max pullback depth from drive high
    e9ft_pullback_must_hold_vwap: bool = True  # PB low must stay above VWAP
    e9ft_first_pullback_only: bool = True    # only first meaningful PB
    e9ft_trigger_close_above_ema9: bool = True  # trigger: first close back above E9
    e9ft_trigger_body_min_pct: float = 0.30  # trigger bar body >= 30% of range (relaxed for 5min)
    e9ft_trigger_close_pct: float = 0.45     # close in upper 45% of bar (relaxed for 5min)
    e9ft_stop_buffer: float = 0.02           # $ buffer below PB low
    e9ft_target_rr: float = 2.0              # fallback / cap
    e9ft_target_mode: str = "structural"    # "fixed_rr" | "structural"
    e9ft_struct_min_rr: float = 1.0
    e9ft_struct_max_rr: float = 3.0
    e9ft_max_bars: Dict[int, int] = field(default_factory=lambda: {1: 120, 5: 24})
    e9ft_min_rs_vs_spy: float = 0.0005       # positive RS threshold (0.05%)

    # ── EMA9 V4 controls ──
    ema9_v4_enabled: bool = False
    ema9_v4_time_start: int = 935
    ema9_v4_time_end: int = 1115
    ema9_require_relative_impulse_vs_spy: bool = False
    ema9_min_relative_impulse_vs_spy: float = 0.0
    ema9_require_soft_trigger_bar: bool = False
    ema9_max_trigger_close_location: float = 1.0
    ema9_max_trigger_body_fraction: float = 1.0
    ema9_entry_mode_v4: str = "close"
    ema9_stop_mode_v4: str = "pullback_low"    # "pullback_low" | "setup_bar_low" | "two_bar_low"
    ema9_stop_buffer_v4: float = 0.01
    ema9_target_mode_v4: str = "fixed_rr"
    ema9_target_rr_v4: float = 1.25
    ema9_require_5m_context: bool = False
    ema9_5m_require_above_vwap: bool = False
    ema9_5m_require_ema9_gt_ema20: bool = False
    ema9_5m_touch_count_max: int = 999

    # ── Backside Structure Only thresholds ──
    enable_backside: bool = True
    bs_time_start: Dict[int, int] = field(default_factory=lambda: {1: 1000, 5: 1000})
    bs_time_end: Dict[int, int] = field(default_factory=lambda: {1: 1330, 5: 1330})
    bs_min_decline_atr: float = 1.5          # min decline from HOD to LOD in ATR
    bs_min_hh_count: int = 1                 # at least 1 higher-high after LOD
    bs_min_hl_count: int = 1                 # at least 1 higher-low after LOD
    bs_ema9_rising_bars: int = 3             # E9 must be rising for N bars
    bs_range_min_bars: Dict[int, int] = field(default_factory=lambda: {1: 8, 5: 3})
    bs_range_max_bars: Dict[int, int] = field(default_factory=lambda: {1: 40, 5: 8})
    bs_range_max_atr: float = 2.0            # max range width in ATR
    bs_range_above_ema9_pct: float = 0.70    # >= 70% of range bars above E9
    bs_range_midpoint_vwap_frac: float = 0.40  # range midpoint >= LOD + frac*(VWAP-LOD)
    bs_break_vol_frac: float = 1.00          # breakout vol >= this * vol_ma
    bs_break_close_pct: float = 0.60         # close in upper 60% of bar
    bs_stop_buffer: float = 0.02             # $ buffer below most recent HL
    bs_target_mode: str = "structural"       # "structural" | "fixed_rr"
    bs_target_rr: float = 2.0               # fallback / cap
    bs_struct_min_rr: float = 1.0
    bs_struct_max_rr: float = 3.0
    bs_max_bars: Dict[int, int] = field(default_factory=lambda: {1: 156, 5: 30})
    bs_one_and_done: bool = True             # single attempt only

    # ── ORH Failed Breakout Short thresholds ──
    enable_orh_fbo: bool = True
    orh_time_start: Dict[int, int] = field(default_factory=lambda: {1: 1000, 5: 1000})
    orh_time_end: Dict[int, int] = field(default_factory=lambda: {1: 1300, 5: 1300})
    orh_break_min_dist_atr: float = 0.08    # min close above ORH in ATR (loosened: trap exists even on small pokes)
    orh_break_vol_frac: float = 0.80        # breakout bar vol >= this * vol_ma (loosened)
    orh_break_body_min: float = 0.30        # breakout bar body ratio >= 30%
    orh_failure_window: Dict[int, int] = field(default_factory=lambda: {1: 30, 5: 6})  # 6 bars=30 min (widened: some failures take a few bars)
    orh_retest_window: Dict[int, int] = field(default_factory=lambda: {1: 30, 5: 6})   # bars to wait for retest from below
    orh_retest_proximity_atr: float = 0.30  # high must come within this ATR of ORH (loosened)
    orh_retest_max_reclaim_atr: float = 0.25  # max overshoot above ORH on retest
    orh_confirm_body_min: float = 0.25      # rejection bar body ratio (loosened)
    orh_confirm_wick_min: float = 0.15      # upper wick pct on rejection bar (loosened)
    orh_stop_buffer_atr: float = 0.30       # stop above retest high
    orh_min_stop_atr: float = 0.40          # minimum stop distance
    orh_target_mode: str = "structural"      # "structural" | "fixed_rr"
    orh_target_rr: float = 2.0             # fallback / cap
    orh_struct_min_rr: float = 1.0
    orh_struct_max_rr: float = 3.0
    orh_max_bars: Dict[int, int] = field(default_factory=lambda: {1: 120, 5: 24})
    orh_min_vwap_dist_atr: float = 0.50    # min VWAP distance for target

    # ── ORL Failed Breakdown Long thresholds ──
    enable_orl_fbd: bool = True
    orl_time_start: Dict[int, int] = field(default_factory=lambda: {1: 1000, 5: 1000})
    orl_time_end: Dict[int, int] = field(default_factory=lambda: {1: 1300, 5: 1300})
    orl_break_min_dist_atr: float = 0.08    # min close below ORL in ATR (loosened)
    orl_break_vol_frac: float = 0.80        # breakdown bar vol >= this * vol_ma (loosened)
    orl_break_body_min: float = 0.30        # breakdown bar body ratio >= 30%
    orl_failure_window: Dict[int, int] = field(default_factory=lambda: {1: 30, 5: 6})  # bars for reclaim (widened)
    orl_reclaim_confirm_bars: Dict[int, int] = field(default_factory=lambda: {1: 20, 5: 4})  # bars for HH confirm (widened)
    orl_reclaim_body_min: float = 0.25      # reclaim bar body ratio (loosened)
    orl_hh_clearance_atr: float = 0.05      # HH must exceed prior high by this ATR
    orl_stop_buffer_atr: float = 0.30       # stop below lowest low since breakdown
    orl_min_stop_atr: float = 0.40          # minimum stop distance
    orl_target_mode: str = "structural"      # "structural" | "fixed_rr"
    orl_target_rr: float = 2.0             # fallback / cap
    orl_struct_min_rr: float = 1.0
    orl_struct_max_rr: float = 3.0
    orl_max_bars: Dict[int, int] = field(default_factory=lambda: {1: 120, 5: 24})
    orl_min_vwap_dist_atr: float = 0.50    # min VWAP distance for target

    # ── ORH Failed Breakout Short v2 (hybrid 5m+1m) ──
    enable_orh_fbo_v2: bool = True
    orh2_time_start: int = 1000
    orh2_time_end: int = 1300
    # Breakout quality (1-min detection, 5-min ATR context)
    orh2_break_min_dist_atr: float = 0.10     # close above ORH in 5m-ATR units
    orh2_break_body_min: float = 0.35         # body ratio of breakout bar
    orh2_break_vol_frac: float = 0.80         # volume relative to 1m vol_ma
    # Failure timing
    orh2_failure_window: int = 15             # 1-min bars for failure (15 min)
    # Mode A: failed retest from below
    orh2_retest_window: int = 15              # bars after failure to wait for retest
    orh2_retest_proximity_atr: float = 0.25   # high must be within this of ORH
    orh2_rejection_body_min: float = 0.30     # rejection bar body ratio
    orh2_rejection_wick_min: float = 0.20     # upper wick pct on rejection bar
    orh2_rejection_lookback: int = 3          # bars after retest to find rejection
    # Mode B: immediate continuation without retest
    orh2_mode_b_no_retest_wait: int = 8       # bars after failure: if no retest, look for continuation
    orh2_mode_b_body_min: float = 0.35        # continuation bar body ratio (strict)
    orh2_mode_b_window: int = 5               # bars after wait period to find continuation
    # Stops / targets
    orh2_stop_buffer_atr: float = 0.40        # stop above retest high (Mode A) or above ORH (Mode B) — widened to reduce stop-outs
    orh2_min_stop_atr: float = 0.30           # minimum stop distance
    orh2_target_rr: float = 1.5               # fallback / cap
    orh2_target_mode: str = "structural"       # "structural" | "fixed_rr"
    orh2_struct_min_rr: float = 1.0
    orh2_struct_max_rr: float = 3.0
    orh2_min_vwap_dist_atr: float = 0.40      # min VWAP distance for target
    orh2_max_bars: int = 60                   # max hold in 1-min bars
    # Structural filters
    orh2_block_leader_rs: float = 0.003       # block if RS > 0.3% AND above VWAP AND ema9>ema20
    orh2_block_hh_count: int = 3              # block if 3+ consecutive higher 5m highs in last 6

    # ── ORL Failed Breakdown Long v2 (hybrid 5m+1m) ──
    enable_orl_fbd_v2: bool = True
    orl2_time_start: int = 1000
    orl2_time_end: int = 1300
    # Breakdown quality (1-min detection, 5-min ATR context)
    orl2_break_min_dist_atr: float = 0.20     # close below ORL in 5m-ATR units (need real breakdown to trap shorts)
    orl2_break_body_min: float = 0.35         # body ratio of breakdown bar
    orl2_break_vol_frac: float = 0.80         # volume relative to 1m vol_ma
    # Reclaim timing
    orl2_reclaim_window: int = 12             # 1-min bars for reclaim (12 min)
    orl2_reclaim_body_min: float = 0.30       # reclaim bar body ratio
    # Shelf + HH trigger
    orl2_shelf_min_bars: int = 5              # bars where low stays above ORL (shelf) — 5 min = 1 five-min bar equivalent
    orl2_hh_trigger_window: int = 20          # bars after reclaim to find HH trigger (wider to compensate for stricter shelf)
    orl2_hh_trigger_body_min: float = 0.40    # HH trigger bar body ratio (stricter)
    # Stops / targets
    orl2_stop_buffer_atr: float = 0.15        # stop below lowest low since breakdown
    orl2_min_stop_atr: float = 0.30           # minimum stop distance
    orl2_target_rr: float = 1.5               # fallback / cap
    orl2_target_mode: str = "structural"       # "structural" | "fixed_rr"
    orl2_struct_min_rr: float = 1.0
    orl2_struct_max_rr: float = 3.0
    orl2_min_vwap_dist_atr: float = 0.40      # min VWAP distance for target
    orl2_max_bars: int = 60                   # max hold in 1-min bars
    orl2_be_trail_threshold: float = 1.0      # move stop to BE after +1.0R

    # ── FFT New-Low Reversal Long (1-min) ──
    enable_fft_newlow: bool = True
    fft_time_start: Dict[int, int] = field(default_factory=lambda: {1: 945, 5: 945})
    fft_time_end: Dict[int, int] = field(default_factory=lambda: {1: 1400, 5: 1400})
    # Flush / spring detection
    fft_allow_session_low: bool = True          # enable session-low undercut variant
    fft_allow_pdl: bool = True                  # enable prior-day-low undercut variant
    fft_dedup_below_orl: bool = True            # only fire if old session low was below OR_low
    fft_spring_clearance_atr: float = 0.03      # close must be this far above old level
    # Confirmation
    fft_confirm_window: int = 5                 # 1-min bars to find HH confirmation
    fft_confirm_body_min: float = 0.35          # body ratio for confirmation bar
    fft_confirm_vol_frac: float = 0.80          # confirmation bar vol >= this * vol_ma
    fft_hh_clearance_atr: float = 0.03          # confirm bar high must exceed spring high by this
    fft_require_above_vwap: bool = True         # confirmation bar close > VWAP
    # Stop
    fft_stop_buffer_atr: float = 0.10           # buffer below spring bar wick
    fft_min_stop_atr: float = 0.30              # minimum stop distance in ATR
    fft_max_stop_atr: float = 2.5               # SKIP if stop distance > this * ATR
    # Target
    fft_target_rr: float = 2.0                  # fallback / cap
    fft_target_mode: str = "structural"         # "fixed_rr" | "structural"
    fft_struct_min_rr: float = 1.0
    fft_struct_max_rr: float = 3.0
    fft_max_bars: int = 60                      # max hold in 1-min bars

    # ── PDH Failed Breakout Short (hybrid 5m+1m) ──
    enable_pdh_fbo: bool = True
    pdh_time_start: int = 1000
    pdh_time_end: int = 1400               # wider window — PDH tests can happen later than ORH
    # Breakout quality (1-min detection, 5-min ATR context)
    pdh_break_min_dist_atr: float = 0.10     # close above PDH in 5m-ATR units
    pdh_break_body_min: float = 0.35         # body ratio of breakout bar
    pdh_break_vol_frac: float = 0.80         # volume relative to 1m vol_ma
    # Failure timing
    pdh_failure_window: int = 15             # 1-min bars for failure (15 min)
    # Mode A: failed retest from below
    pdh_retest_window: int = 15              # bars after failure to wait for retest
    pdh_retest_proximity_atr: float = 0.25   # high must be within this of PDH
    pdh_rejection_body_min: float = 0.30     # rejection bar body ratio
    pdh_rejection_wick_min: float = 0.20     # upper wick pct on rejection bar
    pdh_rejection_lookback: int = 3          # bars after retest to find rejection
    # Mode B: immediate continuation without retest
    pdh_mode_b_no_retest_wait: int = 8       # bars after failure: if no retest, look for continuation
    pdh_mode_b_body_min: float = 0.35        # continuation bar body ratio (strict)
    pdh_mode_b_window: int = 5               # bars after wait period to find continuation
    # Stops / targets
    pdh_stop_buffer_atr: float = 0.20        # stop above retest high (Mode A) or above PDH (Mode B)
    pdh_min_stop_atr: float = 0.30           # minimum stop distance
    pdh_target_rr: float = 1.5               # fallback / cap
    pdh_target_mode: str = "structural"       # "structural" | "fixed_rr"
    pdh_struct_min_rr: float = 1.0
    pdh_struct_max_rr: float = 3.0
    pdh_min_vwap_dist_atr: float = 0.40      # min VWAP distance for target
    pdh_max_bars: int = 60                   # max hold in 1-min bars
    # Confluence: ORH+PDH proximity for quality boost
    pdh_orh_confluence_atr: float = 0.15     # ORH within this ATR of PDH = confluence

    def get(self, param_dict):
        """Get timeframe-specific value from a {1: val, 5: val} dict."""
        if isinstance(param_dict, dict):
            return param_dict.get(self.timeframe_min, param_dict.get(5))
        return param_dict
