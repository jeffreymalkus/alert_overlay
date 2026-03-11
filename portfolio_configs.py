"""
Portfolio Configs — Three Explicit Tracks

Track 1: ARCHIVED BASELINE  (frozen_v1_vk_bdr)
Track 2: ACTIVE PAPER       (candidate_v2_vrgreen_bdr)
Track 3: DISCOVERY / SHADOW (discovery_shadow)

Rules:
  - No config may modify OverlayConfig defaults.
  - Each config is built from scratch with explicit overrides.
  - Discovery signals are tagged by setup name; only active setups count as trades.
  - These configs are the SINGLE SOURCE OF TRUTH for all backtests and paper trading.
"""

from .config import OverlayConfig


# ════════════════════════════════════════════════════════════════
#  Shared helpers
# ════════════════════════════════════════════════════════════════

def _disable_all(cfg: OverlayConfig) -> OverlayConfig:
    """Turn off every setup toggle. Caller re-enables what they need."""
    cfg.show_reversal_setups = False
    cfg.show_trend_setups = False       # VK
    cfg.show_ema_retest = False
    cfg.show_ema_mean_rev = False
    cfg.show_ema_pullback = False
    cfg.show_second_chance = False
    cfg.show_sc_v2 = False
    cfg.show_spencer = False
    cfg.show_ema_scalp = False
    cfg.show_ema_fpip = False
    cfg.show_ema_confirm = False
    cfg.show_mcs = False
    cfg.show_vwap_reclaim = False
    cfg.show_failed_bounce = False
    cfg.show_breakdown_retest = False
    return cfg


def _frozen_bdr_short(cfg: OverlayConfig) -> OverlayConfig:
    """Apply frozen BDR_SHORT params (identical across all tracks)."""
    cfg.show_breakdown_retest = True
    cfg.bdr_require_red_trend = True
    cfg.bdr_am_only = True
    cfg.bdr_am_cutoff = 1100
    cfg.bdr_min_rejection_wick_pct = 0.30
    cfg.bdr_time_stop_bars = 8
    cfg.bdr_exit_mode = "time"
    return cfg


def _canonical_vka(cfg: OverlayConfig) -> OverlayConfig:
    """Apply canonical VKA params: VWAP kiss → hold 2 → expansion → 2.0R target."""
    cfg.show_vka = True
    cfg.vka_time_start = 1000
    cfg.vka_time_end = 1059
    cfg.vka_hold_bars = 2
    cfg.vka_target_rr = 2.0
    cfg.vka_min_body_pct = 0.40
    cfg.vka_require_bull = True
    cfg.vka_require_vol = True
    cfg.vka_vol_frac = 0.70
    cfg.vka_kiss_atr_frac = 0.05
    cfg.vka_stop_buffer = 0.02
    cfg.vka_long_only = True
    cfg.vka_day_filter = "green_only"
    cfg.vka_require_market_align = True
    cfg.vka_require_sector_align = False
    return cfg


def _canonical_vr(cfg: OverlayConfig) -> OverlayConfig:
    """Apply canonical VR H1 params with GREEN_ONLY day filter."""
    cfg.show_vwap_reclaim = True
    cfg.vr_time_start = 1000
    cfg.vr_time_end = 1059
    cfg.vr_hold_bars = 3
    cfg.vr_target_rr = 3.0
    cfg.vr_min_body_pct = 0.40
    cfg.vr_require_bull = True
    cfg.vr_require_vol = True
    cfg.vr_vol_frac = 0.70
    cfg.vr_stop_buffer = 0.02
    cfg.vr_long_only = True
    cfg.vr_day_filter = "green_only"
    cfg.vr_require_market_align = True
    cfg.vr_require_sector_align = False
    return cfg


# ════════════════════════════════════════════════════════════════
#  Track 1: ARCHIVED BASELINE
# ════════════════════════════════════════════════════════════════

def frozen_v1_vk_bdr() -> OverlayConfig:
    """
    Track 1 — Archived Baseline (DO NOT MODIFY)

    Long:  VWAP_KISS only (long-only, non-RED harness filter)
    Short: BDR_SHORT (RED+TREND, AM, big wick)

    This is the original frozen portfolio from prior sessions.
    Kept unchanged for audit/control comparison.
    """
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)

    # Long: VK only
    cfg.show_trend_setups = True
    cfg.vk_long_only = True

    # Short: frozen BDR
    cfg = _frozen_bdr_short(cfg)

    return cfg


# ════════════════════════════════════════════════════════════════
#  Track 2: ACTIVE PAPER CANDIDATE
# ════════════════════════════════════════════════════════════════

# Active setup names (used to filter trades in comparison runner)
ACTIVE_SETUPS_V2 = {"VWAP RECLAIM", "BDR SHORT"}

def candidate_v2_vrgreen_bdr() -> OverlayConfig:
    """
    Track 2 — Active Paper Trading Candidate

    Long:  VWAP_RECLAIM (GREEN_ONLY, AM 10:00-10:59, hold=3, tgt=3.0R)
    Short: BDR_SHORT (RED+TREND, AM, big wick)

    This is the ONLY config that generates live paper-trade signals.
    VK is OFF. No other long setups are enabled.
    """
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)

    # Long: VR GREEN_ONLY
    cfg = _canonical_vr(cfg)

    # Short: frozen BDR
    cfg = _frozen_bdr_short(cfg)

    return cfg


# ════════════════════════════════════════════════════════════════
#  Track 3: DISCOVERY / SHADOW RESEARCH
# ════════════════════════════════════════════════════════════════

# Discovery setups that are logged but NOT active trade signals
SHADOW_SETUPS = {
    "VWAP KISS", "2ND CHANCE", "SPENCER", "9EMA RECLAIM",
    "9EMA FPIP", "2ND CHANCE V2", "VK ACCEPT",
}

def discovery_shadow() -> OverlayConfig:
    """
    Track 3 — Discovery / Shadow Research

    Enables ALL research setups alongside the active candidate.
    Signals from SHADOW_SETUPS are logged for analysis but are
    NOT live trade signals. Only ACTIVE_SETUPS_V2 are tradeable.

    Use this config to:
      - Measure shadow setups in parallel
      - Detect if a retired setup starts showing edge
      - Compare discovery results to the active candidate
    """
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)

    # Active candidate setups
    cfg = _canonical_vr(cfg)
    cfg = _frozen_bdr_short(cfg)

    # Shadow/research setups (enabled but not tradeable)
    cfg.show_trend_setups = True       # VK
    cfg.vk_long_only = True
    cfg.show_second_chance = True      # SC
    cfg.sc_long_only = True
    cfg.show_spencer = True            # Spencer
    cfg.show_ema_scalp = True          # 9EMA Reclaim
    cfg.show_ema_fpip = True           # 9EMA FPIP
    cfg.show_sc_v2 = True             # SC V2

    # Shadow: VKA acceptance study candidate
    cfg = _canonical_vka(cfg)

    # Keep disabled (rejected, no research value)
    cfg.show_reversal_setups = False
    cfg.show_ema_retest = False
    cfg.show_ema_mean_rev = False
    cfg.show_ema_pullback = False
    cfg.show_ema_confirm = False
    cfg.show_mcs = False
    cfg.show_failed_bounce = False

    return cfg


# ════════════════════════════════════════════════════════════════
#  VKA Portfolio Comparison Configs
# ════════════════════════════════════════════════════════════════

def vka_only_bdr() -> OverlayConfig:
    """VKA long + BDR short (VR disabled)."""
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)
    cfg = _canonical_vka(cfg)
    cfg = _frozen_bdr_short(cfg)
    return cfg


def vr_plus_vka_bdr() -> OverlayConfig:
    """VR long + VKA long + BDR short (both long setups active)."""
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)
    cfg = _canonical_vr(cfg)
    cfg = _canonical_vka(cfg)
    cfg = _frozen_bdr_short(cfg)
    return cfg


# ════════════════════════════════════════════════════════════════
#  Track 2 v3: 3-SETUP SURVIVOR PORTFOLIO
# ════════════════════════════════════════════════════════════════

ACTIVE_SETUPS_V3 = {"2ND CHANCE", "BDR SHORT", "EMA PULL"}

def candidate_v3_sc_bdr_ep() -> OverlayConfig:
    """
    Track 2 v3 — 3-Setup Survivor Portfolio (per-setup gating)

    Long:  2ND CHANCE (quality≥5, no regime, hybrid exit, long-only)
    Short: BDR_SHORT (regime required, RED+TREND, AM, big wick)
    Short: EMA PULL  (short-only, early session <14:00, no regime)

    Selected via full candidate scan: these are the ONLY setups
    that survive realistic IBKR slippage (8 bps round-trip).

    Per-setup gating solves the global gate conflict:
      - BDR needs require_regime=True
      - SC needs require_regime=False + quality≥5
      - EP needs require_regime=False + time<14:00
    """
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)

    # ── Long: 2ND CHANCE (quality≥5, no regime) ──
    cfg.show_second_chance = True
    cfg.sc_long_only = True
    cfg.sc_min_quality = 5             # critical: quality≥5 filters 149→49 trades
    cfg.sc_require_regime = False      # regime destroys SC (29 trades at PF 0.68)

    # ── Short: BDR_SHORT (regime required) ──
    cfg = _frozen_bdr_short(cfg)
    cfg.bdr_require_regime = True      # regime filters BDR to 42 high-quality trades

    # ── Short: EMA PULL (decoupled, short-only, early session) ──
    cfg.show_ema_pullback = True       # decoupled: no longer needs show_trend_setups
    cfg.ep_short_only = True           # long EP has no edge
    cfg.ep_time_end = 1400             # PM trades (PF 0.83 cost-on) destroy edge
    cfg.ep_require_regime = False      # regime suppresses EP to ~3 trades

    # ── Global gate settings (defaults for setups without per-setup overrides) ──
    cfg.require_regime = False         # global OFF — each setup uses its own flag
    cfg.min_quality = 4                # global default (SC overrides to 5)
    cfg.alert_cooldown_bars = 0        # no cooldown (tested: zero effect)
    cfg.min_stop_intra_atr_mult = 0.0  # no risk floor (tested: zero effect)

    return cfg


# ════════════════════════════════════════════════════════════════
#  Config registry
# ════════════════════════════════════════════════════════════════

CONFIGS = {
    "frozen_v1_vk_bdr":          frozen_v1_vk_bdr,
    "candidate_v2_vrgreen_bdr":  candidate_v2_vrgreen_bdr,
    "candidate_v3_sc_bdr_ep":    candidate_v3_sc_bdr_ep,
    "discovery_shadow":          discovery_shadow,
    "vka_only_bdr":              vka_only_bdr,
    "vr_plus_vka_bdr":           vr_plus_vka_bdr,
}
