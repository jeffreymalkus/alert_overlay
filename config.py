"""
Consolidated Alert Overlay v4.5 — Configuration
All tunable parameters from the ThinkScript inputs.
"""

from dataclasses import dataclass, field


@dataclass
class OverlayConfig:
    # ── Session times (HHMM integers) ──
    or_start: int = 930
    or_end: int = 945
    day_type_check_time: int = 1000
    late_regime_grace_time: int = 1400
    session_end_time: int = 1555  # forced flat / stop evaluating

    # ── Chart timeframe ──
    bar_interval_minutes: int = 5  # chart bar size (e.g., 2, 5, 15)

    # ── ATR ──
    atr_len: int = 14
    stop_atr_agg_minutes: int = 5  # intraday ATR aggregation

    # ── Quality & Risk Controls ──
    min_quality: int = 4
    require_regime: bool = True
    min_rr: float = 1.5
    max_risk_frac_of_or: float = 0.30
    max_risk_frac_of_atr: float = 0.15
    min_stop_intra_atr_mult: float = 1.0

    # ── Sweep Mechanics ──
    require_sweep_for_reversals: bool = True
    swing_length: int = 20
    sweep_memory_bars: int = 12
    rev_confirm_bars: int = 6

    # ── Box logic ──
    require_box_edge: bool = False
    box_edge_frac: float = 0.20

    # ── Day type & Setup thresholds ──
    trend_day_or_atr_frac: float = 0.25
    trend_day_hold_bars: int = 3
    regime_hysteresis: int = 3
    manipulation_atr_frac: float = 0.20
    or_directional_frac: float = 0.60
    or_close_extreme_frac: float = 0.75
    vwap_sep_atr_mult: float = 1.25
    ema_sep_atr_mult: float = 0.80

    # ── Proximity & Buffer thresholds ──
    confluence_atr_frac: float = 0.05
    reclaim_atr_frac: float = 0.10
    kiss_atr_frac: float = 0.03

    # ── Candle / wick filters ──
    wick_reject_frac: float = 0.55
    wick_oppose_max_frac: float = 0.45
    vol_lookback: int = 20
    entry_vol_min_frac: float = 0.90
    pullback_vol_max_frac: float = 1.20

    # ── Higher timeframe alignment ──
    use_htf_filter: bool = False
    htf_agg_minutes: int = 60

    # ── Cooldown ──
    alert_cooldown_bars: int = 5

    # ── Backtest execution model ──
    slippage_per_side: float = 0.02  # dollars per share per entry/exit (round-trip = 2x)
    commission_per_share: float = 0.0  # IBKR tiered: ~$0.005/share; set if desired

    # ── Second Chance parameters ──
    show_second_chance: bool = False    # RETIRED: PF 0.48, sc_repair_study exhaustive, no fix
    sc_time_start: int = 945             # earliest signal time
    sc_time_end: int = 1430              # latest signal time
    sc_break_atr_min: float = 0.10       # min close distance above level as frac of intra ATR
    sc_break_bar_range_frac: float = 0.80  # min bar range vs median-10 bar range
    sc_break_vol_frac: float = 1.10      # min volume vs vol_ma
    sc_break_close_pct: float = 0.60     # close must be in upper X% of bar (0.60 = upper 40%)
    sc_retest_window: int = 6            # max bars after breakout to retest
    sc_retest_proximity_atr: float = 0.05  # how close low must touch level (frac of intra ATR)
    sc_retest_max_depth_atr: float = 0.35  # max pullback below level (frac of intra ATR)
    sc_retest_max_vol_frac: float = 1.0  # retest bar volume <= this × vol_ma
    sc_confirm_window: int = 3           # max bars after retest to confirm
    sc_confirm_vol_frac: float = 0.90    # min confirm bar volume vs vol_ma
    sc_stop_buffer: float = 0.02         # fixed dollar buffer below retest low
    # ── SC refinement gates (from feature study) ──
    sc_require_strong_bo_vol: bool = True    # require breakout vol >= sc_strong_bo_vol_mult × vol_ma
    sc_strong_bo_vol_mult: float = 1.25     # threshold from feature study
    sc_require_shallow_reset: bool = False   # require reset depth <= sc_max_reset_depth_pct of impulse
    sc_max_reset_depth_pct: float = 0.50    # max retrace as fraction of breakout bar range

    # ── Second Chance V2 parameters (expansion → contained reset → re-attack) ──
    show_sc_v2: bool = False     # retired: not in validated 3-setup model
    sc2_time_start: int = 945              # earliest signal time
    sc2_time_end: int = 1430               # latest signal time
    # Expansion (initial breakout attempt) requirements
    sc2_expansion_min_bars: int = 2        # min bars in expansion leg
    sc2_expansion_max_bars: int = 8        # max bars before expansion expires
    sc2_expansion_min_atr: float = 0.40    # min distance of expansion in ATR units
    sc2_max_initial_overlap_ratio: float = 0.30  # max fraction of bars with body overlap
    sc2_expansion_min_vol_ma: float = 1.0  # expansion avg vol must be >= this × vol_ma
    sc2_break_level_atr_min: float = 0.10  # min penetration past key level (ATR fraction)
    # Reset (pullback) requirements
    sc2_max_reset_depth_pct: float = 0.50  # max retrace as % of expansion distance
    sc2_max_reset_bars: int = 5            # max bars in reset
    sc2_max_reset_overlap_ratio: float = 0.60  # max overlap ratio during reset
    sc2_max_reset_volume_ratio: float = 0.75   # reset avg vol / expansion avg vol
    sc2_max_heavy_reset_bars: int = 1      # max reset bars with vol > expansion avg vol
    # Trigger bar requirements
    sc2_min_trigger_close_pct: float = 0.60  # close in upper X% of bar range (longs)
    sc2_min_trigger_body_pct: float = 0.50   # body as % of full range
    sc2_trigger_volume_vs_reset_min: float = 1.3  # trigger vol must be >= this × reset avg vol
    # Extension controls
    sc2_max_dist_vwap_atr: float = 2.0     # max distance from VWAP at trigger in ATR units
    sc2_max_dist_ema9_atr: float = 1.5     # max distance from EMA9 at trigger in ATR
    sc2_max_total_extension_atr: float = 3.0  # max total move from session open in ATR
    # Timing
    sc2_max_bars_since_expansion: int = 12  # max bars from expansion end to trigger
    sc2_first_reattack_only: bool = True    # only first valid re-attack after reset
    # Context requirements
    sc2_require_vwap_align: bool = True     # trigger must be above/below VWAP
    sc2_require_ema9_align: bool = True     # trigger must be above/below EMA9
    sc2_require_positive_rs_market: bool = False  # require RS vs market >= 0
    sc2_require_positive_rs_sector: bool = False  # require RS vs sector >= 0
    sc2_context_bonus_cap: int = 1          # max context bonus (capped)
    # Exit / scoring
    sc2_min_quality: int = 4               # minimum quality to emit
    sc2_exit_mode: str = "hybrid"          # "time", "trail", or "hybrid"
    sc2_time_stop_bars: int = 16           # shorter than SC's 20-bar trail window
    sc2_stop_buffer_atr: float = 0.15      # stop buffer below reset low in ATR

    # ── Spencer parameters ──
    show_spencer: bool = False   # retired: not in validated 3-setup model
    sp_time_start: int = 1000            # earliest signal time
    sp_time_end: int = 1430              # latest signal time
    sp_trend_advance_atr: float = 0.75   # min advance from session low as frac of daily ATR
    sp_trend_advance_vwap_atr: float = 0.50  # or min above VWAP as frac of intra ATR
    sp_box_min_bars: int = 4             # min consolidation bars
    sp_box_max_bars: int = 8             # max consolidation window to scan
    sp_box_max_range_atr: float = 2.00   # max box range as frac of intra ATR (tight for 5-min bars)
    sp_box_upper_close_pct: float = 0.75 # fraction of closes that must be in upper half
    sp_box_max_below_ema9: int = 1       # max closes below EMA9 in box
    sp_box_min_vol_frac: float = 0.70    # min avg volume in box vs vol_ma
    sp_box_failed_bo_limit: int = 2      # max failed breakout attempts allowed
    sp_box_failed_bo_atr: float = 0.03   # overshoot threshold for failed BO (frac of ATR)
    sp_break_clearance_atr: float = 0.03 # min close above box high (frac of intra ATR)
    sp_break_vol_frac: float = 1.10      # min breakout volume vs vol_ma
    sp_break_close_pct: float = 0.70     # close in upper 30% of bar
    sp_extension_atr: float = 1.50       # reject if price > this × daily ATR above session open
    sp_stop_buffer: float = 0.02         # fixed dollar buffer below box low

    # ── Failed Bounce (short-only) parameters ──
    show_failed_bounce: bool = False   # disabled: -43 pts on 94-symbol universe
    fb_time_start: int = 945             # earliest signal time
    fb_time_end: int = 1430              # latest signal time
    # Step 1: Breakdown detection
    fb_break_atr_min: float = 0.10       # min close distance below level (frac of intra ATR)
    fb_break_bar_range_frac: float = 0.80  # min bar range vs median-10
    fb_break_vol_frac: float = 1.10      # min volume vs vol_ma
    fb_break_close_pct: float = 0.60     # close must be in lower 60% of bar for shorts
    # Step 2: Bounce detection
    fb_bounce_window: int = 6            # max bars after breakdown to detect bounce
    fb_bounce_proximity_atr: float = 0.10  # how close high must approach level (frac of intra ATR)
    fb_bounce_max_reclaim_atr: float = 0.20  # max overshoot above level (failed reclaim)
    fb_bounce_max_vol_frac: float = 1.0  # bounce bar volume <= this × vol_ma
    fb_bounce_wick_frac: float = 0.40    # min upper wick as frac of bar range for wick bonus
    # Step 3: Confirmation
    fb_confirm_window: int = 3           # max bars after bounce to confirm
    fb_confirm_vol_frac: float = 0.90    # min confirm bar volume vs vol_ma
    fb_stop_buffer: float = 0.02         # fixed dollar buffer above bounce high
    fb_min_quality: int = 3              # FB-specific quality gate (lower than default min_quality=4)
    # Short-specific exit
    fb_time_stop_bars: int = 8            # shorter hold for shorts — edge collapses past 12 bars
    fb_exit_mode: str = "time"           # time-only exit for shorts (ema9trail hurts WR)

    # ── Breakdown-Retest Short parameters ──
    # PROMOTED: RED+TREND BDR with AM-only filter is Portfolio C short book.
    # PF(R)=4.07, Exp=+0.513R, N=34 on AM-only RED+TREND subset.
    show_breakdown_retest: bool = True
    bdr_time_start: int = 1000             # scan starts after OR forms
    bdr_time_end: int = 1500               # no entries after 15:00
    bdr_require_red_trend: bool = True     # only fire on RED+TREND SPY days
    bdr_am_only: bool = True               # AM entry only (before 11:00)
    bdr_am_cutoff: int = 1100              # cutoff time for AM-only gate
    # Step 1: Breakdown detection (matching standalone study thresholds)
    bdr_break_atr_min: float = 0.15        # min close distance below level (frac of intra ATR)
    bdr_break_bar_range_frac: float = 0.70 # min bar range vs median range
    bdr_break_vol_frac: float = 0.90       # min volume vs vol_ma
    bdr_break_close_pct: float = 0.60      # close in lower 60% of bar
    # Step 2: Retest detection
    bdr_retest_window: int = 8             # max bars after breakdown to detect retest
    bdr_retest_proximity_atr: float = 0.30 # high must get within 0.3 ATR of level
    bdr_retest_max_reclaim_atr: float = 0.30  # max overshoot above level
    bdr_retest_max_vol_frac: float = 1.0   # retest volume <= this × vol_ma
    # Step 3: Rejection / confirmation
    bdr_confirm_window: int = 4            # max bars after retest to find rejection
    bdr_confirm_vol_frac: float = 0.0      # disabled — standalone study had no rejection vol filter
    bdr_stop_buffer_atr: float = 0.30      # stop buffer above retest high (ATR fraction)
    bdr_min_quality: int = 0               # no quality gate (filter is wick-based)
    # Big rejection wick filter (from feature study)
    bdr_min_rejection_wick_pct: float = 0.30  # require upper wick >= 30% of bar range
    # Exit
    bdr_time_stop_bars: int = 8            # matching standalone study
    bdr_exit_mode: str = "time"            # time-only exit for shorts

    # ── EMA Scalp (9EMA Reclaim + Confirm) parameters ──
    show_ema_scalp: bool = False  # retired: not in validated 3-setup model
    ema_scalp_time_start: int = 940     # earliest signal time
    ema_scalp_time_end: int = 1530      # latest signal time
    ema_scalp_dead_start: int = 1115    # midday dead zone start
    ema_scalp_dead_end: int = 1300      # midday dead zone end
    ema_scalp_allow_midday: bool = False  # override to allow midday signals
    # Context gate
    ema_scalp_rvol_min: float = 0.80    # min RVOL by time-of-day (soft gate; 1.5+ scored as quality bonus)
    ema_scalp_require_breakout: bool = True  # require 5-day breakout context
    # Retest counting
    ema_scalp_max_retests: int = 1      # max retests allowed (1 = first only)
    # Pullback quality
    ema_scalp_max_pb_depth_atr: float = 0.30  # max pullback depth below EMA9
    ema_scalp_max_closes_below: int = 1  # max closes below EMA9 during pullback
    ema_scalp_max_pb_bars: int = 3       # max bars in pullback zone (for confirm)
    # Reclaim quality
    ema_scalp_reclaim_close_pct: float = 0.55  # reclaim candle close in top X% of range
    ema_scalp_reclaim_vol_frac: float = 0.90   # reclaim vol >= this × vol_ma (RVOL handles vol context)
    # Confirm quality
    ema_scalp_confirm_vol_frac: float = 0.90   # confirm vol >= this × vol_ma
    # Stop
    ema_scalp_stop_buffer_atr: float = 0.50  # buffer below pullback low as frac of intra ATR
    ema_scalp_max_stop_atr: float = 1.20  # reject if stop distance > this × intra ATR
    # Quality gate
    ema_scalp_min_quality: int = 6       # Q6+ is where the edge lives (Q4-5 are net negative)
    # Exit
    ema_scalp_time_stop_bars: int = 20   # max bars to hold
    ema_scalp_exit_mode: str = "hybrid"  # "time", "ema9_trail", "hybrid", "breakeven_trail"

    # ── Backtest exit mode for new setups ──
    # "time" = exit after N bars, "ema9_trail" = exit on EMA9 cross, "hybrid" = both
    breakout_exit_mode: str = "hybrid"
    breakout_time_stop_bars: int = 20    # max bars to hold

    # ── Feature toggles ──
    show_reversal_setups: bool = False   # BOX_REV, MANIP, VWAP_SEP — disabled after backtest analysis
    show_trend_setups: bool = False      # VWAP_KISS retired: not in validated 3-setup model
    show_ema_retest: bool = False        # EMA_RETEST — disabled: 29% WR unsalvageable
    show_ema_mean_rev: bool = False      # EMA9_SEP — disabled after backtest analysis
    show_ema_pullback: bool = False      # SHELVED: PF 0.90, N=26 — insufficient signal volume
    vk_long_only: bool = True            # VWAP KISS long only (shorts 23% WR, net negative)
    vk_max_opposing_wick_pct: float = 1.0   # disabled (was 0.20, no incremental value on locked baseline)
    sc_long_only: bool = True            # Second Chance long only (shorts lose money)
    sp_long_only: bool = False           # Spencer direction filter
    use_completed_stop_atr: bool = True

    # ── Market Context (SPY/QQQ) ──
    use_market_context: bool = True         # enable market trend + RS gates
    use_sector_context: bool = True         # enable sector RS (requires sector ETF data)

    # ── Graded Tradability Model ──
    use_tradability_gate: bool = False          # False = compute + attach but don't gate
    tradability_long_threshold: float = -0.3    # longs require long_score >= this
    tradability_short_threshold: float = -0.3   # shorts require short_score <= -this
    tradability_w_market: float = 1.0           # weight: market structure (SPY)
    tradability_w_sector: float = 0.6           # weight: sector structure
    tradability_w_rs_market: float = 0.5        # weight: RS vs market
    tradability_w_rs_sector: float = 0.3        # weight: RS vs sector

    # RS thresholds (stock_pct_from_open - spy_pct_from_open)
    rs_market_min_long: float = 0.0         # longs require RS >= 0 (outperforming market)
    rs_market_max_short: float = 0.0        # shorts require RS <= 0 (underperforming market)
    rs_sector_min_long: float = -0.10       # softer sector gate (slight underperformance ok)
    rs_sector_max_short: float = 0.10       # softer sector gate

    # ── Per-setup context gates ──
    # Each setup has independent market + sector gate controls.
    # "hard" = signal suppressed if condition fails
    # "soft" = quality bonus/penalty (does not suppress)

    # VWAP_KISS: hard market gate + hard sector gate, slightly looser than EMA
    vk_require_market_align: bool = True    # hard: market_trend must not oppose
    vk_require_sector_align: bool = True    # hard: sector_trend must not oppose
    vk_require_rs_market: bool = True       # hard: rs_market >= threshold for longs
    vk_require_rs_sector: bool = False      # soft: rs_sector logged but not gated (looser)

    # VK target mode: "or_range" (current: entry ± OR range) or "atr" (entry ± N × daily ATR)
    vk_target_mode: str = "or_range"
    vk_target_atr_mult: float = 2.0        # multiplier when vk_target_mode="atr"

    # MCS (Momentum Confirm at Structure) — long-only momentum entry
    show_mcs: bool = False                  # disabled by default (research only)
    mcs_require_engulf: bool = False        # require bull_engulf (strict) or just strong_bull (loose)
    mcs_min_body_pct: float = 0.50          # min body/range ratio (candle quality)
    mcs_vol_mult: float = 1.0              # min volume vs vol_ma (1.0 = at least average)
    mcs_structure: str = "vwap_or_ema9"     # "vwap", "ema9", "vwap_or_ema9"
    mcs_structure_atr_frac: float = 0.25    # structure proximity in ATR (wider than kiss_dist)
    mcs_target_mode: str = "atr"            # "atr" or "or_range"
    mcs_target_atr_mult: float = 0.40       # target = entry + N × daily ATR
    mcs_max_above_or: bool = True           # block entries above OR high (anti-chase)
    mcs_require_market_align: bool = True   # require market context alignment
    mcs_long_only: bool = True              # long-only for now
    mcs_confirm_bars: int = 0               # 0 = immediate, 1-3 = wait N bars for confirmation

    # ── VWAP Reclaim + Hold (H1) parameters ──
    # PROMOTED: GREEN+AM VWAP Reclaim with hold=3, tgt=3.0R is first stable positive long entry.
    # PF(R)=1.45, Exp=+0.172R, N=156, Train PF 1.63, Test PF 1.24, STABLE.
    show_vwap_reclaim: bool = False       # disabled by default until validated in engine
    vr_time_start: int = 1000             # earliest signal (AM-only: 10:00)
    vr_time_end: int = 1059               # latest signal (AM-only: 10:59)
    vr_hold_bars: int = 3                 # bars above VWAP before trigger eligible
    vr_target_rr: float = 3.0             # target = entry + N × risk
    vr_min_body_pct: float = 0.40         # trigger bar body >= 40% of range
    vr_require_bull: bool = True          # trigger bar must be bullish (close > open)
    vr_require_vol: bool = True           # trigger bar volume >= vr_vol_frac × vol_ma
    vr_vol_frac: float = 0.70             # volume threshold fraction (0.70 = 70% of vol_ma)
    vr_stop_buffer: float = 0.02          # fixed buffer below hold-period low
    vr_long_only: bool = True             # long-only setup
    # vr_day_filter controls in-engine SPY-day gating for VR signals:
    #   "green_only"       = SPY pct_from_open > +0.05% (strictly GREEN)
    #   "non_red"          = SPY pct_from_open >= -0.05% (GREEN + FLAT)
    #   "market_align_only"= no SPY-day gate; relies solely on vr_require_market_align
    #   "none"             = no day filter at all
    vr_day_filter: str = "non_red"        # default: matches harness-level locked filter
    vr_require_market_align: bool = True  # hard: market_trend must not oppose
    vr_require_sector_align: bool = False # soft: sector alignment not required

    # ── VK Accept (VWAP Kiss + Acceptance) parameters ──
    # Acceptance study candidate: VWAP touch → hold 2 bars → expansion close → 2.0R target
    # Engine uses real-time SPY data (no perfect-foresight day filter)
    show_vka: bool = False                # disabled by default until engine-validated
    vka_time_start: int = 1000            # earliest signal (AM-only: 10:00)
    vka_time_end: int = 1059              # latest signal (AM-only: 10:59)
    vka_hold_bars: int = 2                # bars near/above VWAP before trigger eligible
    vka_target_rr: float = 2.0            # target = entry + N × risk
    vka_min_body_pct: float = 0.40        # trigger bar body >= 40% of range
    vka_require_bull: bool = True          # trigger bar must be bullish
    vka_require_vol: bool = True           # trigger bar volume >= vka_vol_frac × vol_ma
    vka_vol_frac: float = 0.70            # volume threshold fraction
    vka_kiss_atr_frac: float = 0.05       # touch = within 5% of ATR from VWAP
    vka_stop_buffer: float = 0.02         # fixed buffer below acceptance-window low
    vka_long_only: bool = True             # long-only setup
    # VKA day filter: same mechanism as VR. Uses real-time SPY pct_from_open.
    #   "green_only"       = SPY pct_from_open > +0.05%
    #   "non_red"          = SPY pct_from_open >= -0.05%
    #   "none"             = no day filter
    vka_day_filter: str = "green_only"     # default: GREEN only (matches standalone study)
    vka_require_market_align: bool = True   # hard: market_trend must not oppose
    vka_require_sector_align: bool = False  # soft: sector alignment not required

    # EMA_RECLAIM: hard market gate + hard sector gate + RS requirement
    ema_scalp_require_market_align: bool = True   # hard: market_trend must not oppose
    ema_scalp_require_sector_align: bool = True   # hard: sector_trend must not oppose
    ema_scalp_require_rs_market: bool = True       # hard: rs_market >= threshold
    ema_scalp_require_rs_sector: bool = True       # hard: rs_sector >= threshold

    # SECOND_CHANCE: no hard gate except hostile tape; quality bonus
    sc_require_market_align: bool = False   # NOT a hard gate (only blocks in hostile)
    sc_hostile_tape_block: bool = True      # hard: block SC when BOTH market AND sector oppose
    sc_market_quality_bonus: int = 1        # +1 quality when market aligns
    sc_sector_quality_bonus: int = 0        # sector bonus disabled — was inflating trade count
    sc_pre_bonus_min_quality: int = 3       # base quality must meet this BEFORE bonus applied
    sc_max_quality_bonus: int = 1           # cap total context bonus (prevents rescue of weak base)

    # Dynamic slippage model
    use_dynamic_slippage: bool = True
    slip_bps: float = 0.0004               # base slippage as fraction of price (4 bps)
    slip_min: float = 0.02                  # minimum slippage floor
    slip_vol_mult_cap: float = 2.0          # max volatility multiplier
    slip_family_mult_reversal: float = 1.3  # reversal setups: wider fills
    slip_family_mult_trend: float = 1.0     # trend setups: normal
    slip_family_mult_breakout: float = 1.5  # breakout setups: worst fills (momentum chase)
    slip_family_mult_ema_scalp: float = 1.1 # tight scalps: slightly worse
    slip_family_mult_short_struct: float = 1.2  # short structure: moderate

    # ── Per-setup regime gating ──
    # When set, these OVERRIDE the global require_regime for the named setup.
    # None = use global require_regime; True/False = setup-specific.
    bdr_require_regime: bool = True         # BDR needs regime (RED+TREND) to filter garbage
    sc_require_regime: bool = False         # SC is destroyed by regime gate
    ep_require_regime: bool = False         # EMA PULL is destroyed by regime gate

    # ── Per-setup quality gating ──
    sc_min_quality: int = 5                 # SC Q>=5 validated: 49 trades, PF 1.19 cost-on

    # ── EMA PULL per-setup controls ──
    ep_time_end: int = 1400                 # EMA PULL early session only (validated)
    ep_short_only: bool = True              # EMA PULL short-only (validated)

    # ── EMA_CONFIRM toggle ──
    show_ema_confirm: bool = False          # disabled: 0% WR, only 3 trades, all losers

    # ── RSI Midline Long / Bouncefail Short parameters ──
    show_rsi_midline_long: bool = False       # disabled by default
    show_rsi_bouncefail_short: bool = False   # disabled by default

    rsi_len: int = 7                          # RSI period
    rsi_long_time_start: int = 1000
    rsi_long_time_end: int = 1300
    rsi_short_time_start: int = 1000
    rsi_short_time_end: int = 1300

    rsi_impulse_lookback: int = 12            # bars to scan for impulse
    rsi_pullback_lookback: int = 6            # bars to scan for pullback/bounce

    # Long candidate thresholds
    rsi_long_impulse_min: float = 70.0        # max RSI over prior 12 bars >= this
    rsi_long_pullback_min_low: float = 45.0   # min RSI over prior 6 bars >= this
    rsi_long_pullback_min_high: float = 55.0  # min RSI over prior 6 bars <= this
    rsi_long_integrity_min: float = 40.0      # min RSI over prior 12 bars >= this
    rsi_long_reclaim_level: float = 50.0      # RSI must cross above this

    # Short candidate thresholds
    rsi_short_impulse_max: float = 30.0       # min RSI over prior 12 bars <= this
    rsi_short_bounce_max_low: float = 45.0    # max RSI over prior 6 bars >= this
    rsi_short_bounce_max_high: float = 55.0   # max RSI over prior 6 bars <= this
    rsi_short_integrity_max: float = 60.0     # max RSI over prior 12 bars <= this
    rsi_short_rollover_level: float = 45.0    # RSI must cross below this

    # Execution
    rsi_stop_buffer_atr: float = 0.15         # stop buffer as fraction of intra ATR
    rsi_target_r: float = 1.5                 # target in R-multiples
    rsi_long_time_stop_bars: int = 8
    rsi_short_time_stop_bars: int = 6

    # Alignment gates
    rsi_require_spy_align: bool = True        # require SPY bullish/bearish alignment
    rsi_require_vwap_align: bool = True       # require close vs VWAP alignment
    rsi_require_ema20_align: bool = True       # require close vs EMA20 alignment

    # Per-setup regime gate override (disabled by default — use native filters)
    rsi_require_regime: bool = False

    # Exit mode for backtest
    rsi_long_exit_mode: str = "hybrid_target_time"   # target OR time stop
    rsi_short_exit_mode: str = "hybrid_target_time"  # target OR time stop

    # ── EMA First Pullback In Play (9EMA FPIP) parameters ──
    show_ema_fpip: bool = False   # retired: not in validated 3-setup model
    ema_fpip_time_start: int = 940           # earliest signal time
    ema_fpip_time_end: int = 1530            # latest signal time

    # Stock-in-play gate (much stricter than EMA_RECLAIM's 0.80)
    ema_fpip_rvol_tod_min: float = 1.5       # hard RVOL gate — "in play" threshold

    # Expansion leg definition
    ema_fpip_min_expansion_atr: float = 0.40   # impulse distance >= 0.40× intra ATR
    ema_fpip_expansion_min_bars: int = 2       # at least 2 bars to form expansion
    ema_fpip_expansion_max_bars: int = 6       # cap expansion tracking at 6 bars
    ema_fpip_max_impulse_overlap_ratio: float = 0.30  # max 30% body-overlap bars
    ema_fpip_min_expansion_avg_vol: float = 1.0  # expansion avg vol >= vol_ma

    # Pullback constraints (hard disqualifiers, not scores)
    ema_fpip_max_pullback_depth_pct: float = 0.50  # max depth as % of expansion distance
    ema_fpip_max_pullback_bars: int = 4        # max bars in pullback (shorter than RECLAIM's 8)
    ema_fpip_max_pullback_volume_ratio: float = 0.75  # PB avg vol <= 75% expansion avg vol
    ema_fpip_max_heavy_pullback_bars: int = 1  # max PB bars with vol >= expansion_avg_vol

    # Trigger bar quality (hard gates)
    ema_fpip_min_trigger_close_pct: float = 0.60   # close in upper 60% of range
    ema_fpip_min_trigger_body_pct: float = 0.50    # min body as % of total range
    ema_fpip_trigger_volume_vs_pullback_min: float = 1.3  # trigger vol >= 1.3× PB avg vol

    # Context gates (all hard)
    ema_fpip_require_vwap_align: bool = True    # must be above/below VWAP
    ema_fpip_require_market_align: bool = True  # market trend must not oppose
    ema_fpip_require_ema20_slope: bool = True   # EMA20 must slope in direction

    # Exit
    ema_fpip_time_stop_bars: int = 12          # faster exit than RECLAIM's 20
    ema_fpip_exit_mode: str = "time"           # simple time exit for v1

    # First pullback only — no second retests
    ema_fpip_first_pullback_only: bool = True

    # Quality gate
    ema_fpip_min_quality: int = 4              # lower than RECLAIM's 6 because hard gates already strict

    # ─── HITCHHIKER ──────────────────────────────────────────────
    show_hitchhiker: bool = False
    hh_time_start: int = 935            # setup begins forming pre-10am
    hh_time_end: int = 1200             # opening-drive trade; extends into late morning
    hh_consol_min_bars: int = 3         # min consolidation bars (3 bars = 15 min)
    hh_consol_max_bars: int = 24        # max consolidation bars (2 hours)
    hh_consol_max_range_atr: float = 2.0  # consolidation range ≤ 2.0× intra ATR
    hh_consol_upper_pct: float = 0.50   # consol low must be in upper half of day range (longs)
    hh_break_vol_frac: float = 1.10     # breakout bar vol ≥ 110% of consol avg vol
    hh_require_drive: bool = True       # require prior directional drive off open
    hh_drive_min_atr: float = 1.0       # drive distance ≥ 1.0× intra ATR
    hh_max_wick_pct: float = 0.70       # reject choppy consol: max avg wick % of bar range
    hh_require_market_align: bool = False  # SPY/QQQ must trend in direction
    hh_min_quality: int = 3
    hh_time_stop_bars: int = 20         # exit on time if no wave detected

    # ─── FASHIONABLY LATE ────────────────────────────────────────
    show_fashionably_late: bool = False
    fl_time_start: int = 1000           # after initial open chop settles
    fl_time_end: int = 1330             # morning + midday
    fl_ema_slope_min: float = 0.02      # 9EMA must be rising: (e9 - e9_prev) / ATR > this
    fl_vwap_slope_max: float = 0.01     # VWAP must be flat/opposing: abs(slope) < this
    fl_no_flat_ema_bars: int = 3        # reject if 9EMA was flat for > N bars before cross
    fl_stop_frac: float = 0.33          # stop = 1/3 distance from VWAP to LOD (longs)
    fl_target_measured_move: bool = True  # target = measured move from LOD to cross
    fl_require_market_align: bool = True
    fl_min_quality: int = 4
    fl_time_stop_bars: int = 16

    # ─── BACKSIDE ────────────────────────────────────────────────
    show_backside: bool = False
    bs_time_start: int = 1000
    bs_time_end: int = 1330
    bs_min_extension_atr: float = 1.5   # must be extended from VWAP by ≥ 1.5× ATR
    bs_require_hh_hl: bool = True       # must see at least 1 HH + 1 HL (longs)
    bs_consol_min_bars: int = 3         # consolidation above 9EMA before break
    bs_consol_max_bars: int = 12
    bs_require_above_ema9: bool = True  # consol must be above rising 9EMA (longs)
    bs_break_vol_frac: float = 1.20     # breakout bar vol ≥ 120% consol avg
    bs_target_vwap: bool = True         # exit entire position at VWAP
    bs_halfway_gate: bool = True        # consol must be > halfway between LOD and VWAP
    bs_require_market_align: bool = True
    bs_min_quality: int = 4
    bs_one_attempt: bool = True         # hard stop, one and done
    bs_time_stop_bars: int = 20

    # ─── RUBBERBAND ──────────────────────────────────────────────
    show_rubberband: bool = False
    rb_time_start: int = 1000
    rb_time_end: int = 1330
    rb_min_extension_atr: float = 3.0   # price extended ≥ 3 ATR from open
    rb_min_rvol: float = 3.0            # relative volume ≥ 3 (source says 5, relaxed for 5-min)
    rb_accel_vol_increase: float = 1.30  # last leg vol ≥ 130% of prior leg vol (acceleration)
    rb_accel_range_increase: float = 1.20  # last leg bar ranges increasing
    rb_snapback_bars: int = 2           # snapback candle clears highs of ≥ 2 prior candles
    rb_snapback_vol_top_n: int = 5      # snapback bar among top-N volume bars of day
    rb_target_rr_1: float = 1.0         # exit 1/3 at 1:1 R:R
    rb_target_rr_2: float = 2.0         # exit 1/3 at 2:1 R:R
    rb_target_vwap: bool = True         # exit final 1/3 at VWAP
    rb_max_attempts: int = 2            # 2 strikes and out
    rb_require_no_trend_fade: bool = True  # don't fade a cleanly trending market
    rb_require_market_align: bool = False  # counter-trend by nature; no market align
    rb_min_quality: int = 4
    rb_time_stop_bars: int = 20

    # ─── FL MOMENTUM REBUILD (long-only) ─────────────────────────
    # 9EMA crosses above VWAP after meaningful decline + turn
    show_fl_momentum_rebuild: bool = True   # PROMOTED: PF 1.11, N=664, 4-bar turn + 0.50 stop
    flr_time_start: int = 1030           # optimized: late morning only (10:30+)
    flr_time_end: int = 1130             # optimized: 10:30-11:30 sweet spot
    # Meaningful decline definition
    flr_min_decline_atr: float = 3.0     # optimized: 3.0 ATR (was 1.5) — much more selective
    flr_min_decline_bars: int = 4        # decline must span ≥ 4 bars (20 min)
    # Turn / higher-low detection
    flr_hl_tolerance_atr: float = 0.3    # HL must be > decline_low + 0.3 ATR
    flr_max_base_bars: int = 20          # turn must lead to cross within 20 bars
    # Cross trigger gates
    flr_ema_slope_min: float = 0.02      # 9EMA must be rising: (e9 - e9_prev) / ATR > this
    flr_cross_vol_min_rvol: float = 1.0  # cross bar rvol_tod ≥ this (convergence volume)
    flr_cross_body_pct: float = 0.60     # optimized: 60% body (was 50%) — cleaner bars
    flr_cross_close_above_vwap: bool = True  # cross bar must close above VWAP
    # Choppiness filter
    flr_max_chop_bars: int = 3           # max bars where 9EMA was flat before cross
    flr_chop_threshold: float = 0.01     # |e9 - e9_prev| / ATR < this = "flat"
    # Stop and target
    flr_stop_frac: float = 0.50          # promoted: 50% of measured move (was 40%, orig 33%) — wider stop reduces stop-out rate
    flr_target_measured_move: bool = True # target = measured move above entry
    flr_target_r: float = 2.0            # fallback R:R if measured move disabled
    # Gating
    flr_require_market_align: bool = False  # optimized: disabled (was True) — counter-trend recovery
    flr_require_above_ema20: bool = False # close above EMA20 at cross time
    flr_min_quality: int = 4
    flr_time_stop_bars: int = 16         # time exit if no target hit
    # Structural improvement: EMA slope acceleration
    flr_require_ema_accel: bool = False   # require 9EMA slope to be accelerating (2nd deriv > 0)
    flr_ema_accel_min: float = 0.005     # minimum slope increase per bar (normalized by ATR)
    # Structural improvement: 2-bar turn confirmation
    flr_turn_confirm_bars: int = 4       # promoted: 4-bar HL confirmation (was 1) — filters choppy turns
    # Structural improvement: adaptive stop sizing
    flr_adaptive_stop: bool = False      # scale stop with decline magnitude
    flr_stop_min_frac: float = 0.25      # minimum stop fraction (for huge declines)
    flr_stop_max_frac: float = 0.50      # maximum stop fraction (for small declines)
    # Structural improvement: volatility regime filter
    flr_require_high_vol_regime: bool = False  # only trade when intraday vol is elevated
    flr_vol_regime_min: float = 1.2      # intraday ATR / daily ATR ratio >= this

    # ─── EMA9 FIRST PULLBACK (long-only) ─────────────────────────
    # First meaningful pullback to rising 9EMA after opening drive
    show_ema9_first_pb: bool = False
    e9pb_time_start: int = 945           # drive must start early
    e9pb_time_end: int = 1200            # pullback entry before noon
    # Opening drive definition
    e9pb_min_drive_atr: float = 1.5      # optimized: 1.5 ATR (was 1.0) — stronger drives only
    e9pb_min_drive_bars: int = 3         # drive spans ≥ 3 bars (15 min)
    e9pb_max_drive_bars: int = 12        # drive must complete within 12 bars
    e9pb_drive_close_above_vwap: bool = True  # drive high bar must be above VWAP
    # Pullback constraints
    e9pb_max_pb_depth_pct: float = 0.50  # PB depth ≤ 50% of drive distance
    e9pb_max_pb_bars: int = 6            # PB must resolve within 6 bars
    e9pb_max_pb_closes_below_e9: int = 2 # max 2 closes below 9EMA during PB
    e9pb_pb_vol_decline: bool = True     # PB avg vol should be lower than drive avg vol
    # Trigger bar quality
    e9pb_trigger_close_pct: float = 0.60 # trigger bar closes in upper 60% of range
    e9pb_trigger_body_pct: float = 0.50  # trigger bar body ≥ 50% of range
    e9pb_trigger_above_e9: bool = True   # trigger bar must close above 9EMA
    # Gating
    e9pb_require_ema9_rising: bool = True  # 9EMA must be rising at trigger time
    e9pb_require_market_align: bool = True # SPY bullish
    e9pb_require_rvol: float = 1.5       # optimized: 1.5 (was 1.2) — stock must be in play
    e9pb_min_quality: int = 4
    e9pb_time_stop_bars: int = 12        # time exit
    e9pb_target_r: float = 2.0           # target in R-multiples
    e9pb_first_pb_only: bool = True      # only first pullback — no second retests
    e9pb_exit_mode: str = "hybrid_target_time"  # NEW: use target+time instead of EMA trail

    # ─── EMA9 BACKSIDE RANGE BREAK (long-only) ───────────────────
    # Extended below VWAP → HH/HL recovery → range above rising 9EMA → break
    show_ema9_backside_rb: bool = False
    e9rb_time_start: int = 1000
    e9rb_time_end: int = 1330
    # Extension definition
    e9rb_min_extension_atr: float = 2.0  # optimized: 2.0 ATR (was 1.5) — more selective
    # HH/HL recovery requirements
    e9rb_require_hh: int = 1             # minimum higher highs
    e9rb_require_hl: int = 1             # minimum higher lows
    e9rb_hl_tolerance_atr: float = 0.2   # HL must be > prev_low + 0.2 ATR
    # Range formation
    e9rb_range_min_bars: int = 3         # range must span ≥ 3 bars
    e9rb_range_max_bars: int = 12        # range must resolve within 12 bars
    e9rb_range_max_width_atr: float = 2.0  # range width ≤ 2 ATR
    e9rb_require_above_e9: bool = True   # range must be above rising 9EMA
    # Breakout trigger
    e9rb_break_vol_frac: float = 1.50    # optimized: 150% (was 120%) — stronger breakout volume
    e9rb_break_close_pct: float = 0.60   # breakout bar close in upper 60% of range
    # Stop and target
    e9rb_stop_below_range: bool = True   # stop below range low
    e9rb_stop_buffer_atr: float = 0.15   # stop buffer below range low
    e9rb_target_vwap: bool = True        # target = VWAP
    e9rb_target_r: float = 2.0           # fallback R:R if VWAP target disabled
    # Gating
    e9rb_require_market_align: bool = False  # optimized: disabled — counter-trend recovery
    e9rb_require_ema9_rising: bool = True # 9EMA must be rising during range
    e9rb_halfway_gate: bool = True       # range must be > halfway from ext low to VWAP
    e9rb_min_quality: int = 4
    e9rb_time_stop_bars: int = 20
