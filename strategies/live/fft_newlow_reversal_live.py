"""
FFT_NEWLOW_REV — Live incremental version (1-MINUTE TIMEFRAME).

Failed Follow-Through New-Low Reversal Long:
  Stock prints a new intraday low (or PDL undercut), sellers fail to follow
  through (bar closes back above the prior level), then a confirmation bar
  clears the spring bar's high with conviction.

Edge: Trapped shorts. The flush below session low / PDL invites momentum
sellers. When price immediately reclaims, those shorts are offside and their
covering provides fuel.

State machine phases:
  IDLE → SPRING_DETECTED → SIGNAL (or EXPIRE)

Runs on 1-min bars for precise flush/reclaim detection.
Uses 5-min ATR context via snap.atr_5m for structural sizing.
Session state: snap.vwap, snap.session_high, snap.or_low, snap.or_ready.

Dedup with ORL_FBD: only fires if flush low is below OR_low (distinct event).
Time gate: OR must be established (or_ready) before any detection.
Max stop filter: skip if entry-to-stop exceeds max ATR threshold.
One signal per day max.
"""

import math
from typing import Optional

from ...models import Bar, NaN
from ..shared.config import StrategyConfig
from ..shared.helpers import bar_body_ratio, compute_structural_target_long, trigger_bar_quality
from ..shared.rejection_filters import RejectionFilters
from ..shared.quality_scoring import QualityScorer
from ..shared.signal_schema import QualityTier
from .shared_indicators import IndicatorSnapshot
from .base import LiveStrategy, RawSignal

_isnan = math.isnan

# State constants
IDLE, SPRING_DETECTED = 0, 1


class FFTNewlowReversalLive(LiveStrategy):
    """Incremental FFT_NEWLOW_REV for live pipeline. Runs on 1-min bars."""

    def __init__(self, cfg: StrategyConfig, rejection: RejectionFilters = None,
                 quality: QualityScorer = None, enabled: bool = True):
        super().__init__(name="FFT_NEWLOW_REV", direction=1, enabled=enabled,
                         skip_rejections=["distance", "bigger_picture"],
                         timeframe=1)  # ← 1-min timeframe
        self.cfg = cfg
        self.rejection = rejection
        self.quality = quality

        self._time_start = cfg.get(cfg.fft_time_start)
        self._time_end = cfg.get(cfg.fft_time_end)

        self._init_state()

    def _init_state(self):
        self._phase: int = IDLE
        self._triggered_today: bool = False

        # Session-low tracking (updated manually, one bar behind snap)
        self._tracked_session_low: float = NaN
        self._session_low_initialized: bool = False

        # Spring bar state
        self._spring_bar_high: float = NaN
        self._spring_bar_low: float = NaN   # the actual flush low (= stop ref)
        self._spring_level: float = NaN     # the level that was undercut
        self._spring_type: str = ""         # "session_low" or "pdl"
        self._bars_since_spring: int = 0

    def reset_day(self):
        self._init_state()

    def step(self, snap: IndicatorSnapshot,
             market_ctx=None) -> Optional[RawSignal]:
        cfg = self.cfg
        hhmm = snap.hhmm
        bar = snap.bar

        # ── Read 1-min indicators ──
        e9 = snap.ema9_1m
        vw = snap.vwap
        i_atr = snap.atr_1m
        vol_ma = snap.vol_ma_1m

        # Fall back to daily ATR if 1-min not ready
        if _isnan(i_atr) or i_atr <= 0:
            i_atr = snap.daily_atr
        if _isnan(i_atr) or i_atr <= 0 or self._triggered_today:
            return None
        if not snap.ema9_1m_ready:
            return None
        if _isnan(vol_ma) or vol_ma <= 0:
            return None

        # ── Time gate: OR must be established ──
        if not snap.or_ready:
            # Still in OR formation — just track session low and return
            self._tracked_session_low = snap.session_low
            self._session_low_initialized = True
            return None

        # Initialize session low tracking on first post-OR bar
        if not self._session_low_initialized:
            self._tracked_session_low = snap.session_low
            self._session_low_initialized = True
            return None

        # ── Time window check ──
        if not (self._time_start <= hhmm <= self._time_end):
            # Still update tracking even outside window
            self._tracked_session_low = min(self._tracked_session_low, bar.low)
            return None

        or_low = snap.or_low if snap.or_ready and not _isnan(snap.or_low) else NaN
        pdl = snap.prior_day_low

        # ── Phase: SPRING_DETECTED — waiting for confirmation ──
        if self._phase == SPRING_DETECTED:
            self._bars_since_spring += 1

            # Expire if too many bars
            if self._bars_since_spring > cfg.fft_confirm_window:
                self._phase = IDLE
                # Update tracked low and continue watching
                self._tracked_session_low = min(self._tracked_session_low, bar.low)
                return None

            # ── Confirmation: bar clears spring bar's high with conviction ──
            clearance = cfg.fft_hh_clearance_atr * i_atr
            if bar.high > self._spring_bar_high + clearance and bar.close > bar.open:
                rng = bar.high - bar.low
                body_r = abs(bar.close - bar.open) / rng if rng > 0 else 0.0

                if body_r < cfg.fft_confirm_body_min:
                    return None
                if bar.volume < cfg.fft_confirm_vol_frac * vol_ma:
                    return None

                # Optional VWAP gate
                if cfg.fft_require_above_vwap and not _isnan(vw):
                    if bar.close <= vw:
                        return None

                # ── COMPUTE STOP ──
                stop_ref_type = "spring_bar_low"
                stop_ref_price = self._spring_bar_low
                raw_stop = stop_ref_price - cfg.fft_stop_buffer_atr * i_atr
                stop = raw_stop
                risk = bar.close - stop

                # Min stop floor
                min_stop = cfg.fft_min_stop_atr * i_atr
                min_stop_rule_applied = False
                if risk < min_stop:
                    stop = bar.close - min_stop
                    risk = min_stop
                    min_stop_rule_applied = True

                if risk <= 0:
                    self._phase = IDLE
                    self._tracked_session_low = min(self._tracked_session_low, bar.low)
                    return None

                # Capture stop metadata
                _stop_meta = {
                    "stop_ref_type": stop_ref_type,
                    "stop_ref_price": round(stop_ref_price, 2),
                    "raw_stop": round(raw_stop, 2),
                    "buffer_type": "atr",
                    "buffer_value": cfg.fft_stop_buffer_atr,
                    "min_stop_rule_applied": min_stop_rule_applied,
                    "min_stop_distance": round(min_stop, 2),
                    "final_stop": round(stop, 2),
                }

                # ── Max stop filter ──
                if risk > cfg.fft_max_stop_atr * i_atr:
                    self._phase = IDLE
                    self._tracked_session_low = min(self._tracked_session_low, bar.low)
                    return None

                # ── COMPUTE TARGET ──
                if cfg.fft_target_mode == "structural":
                    _candidates = []
                    if not _isnan(vw) and vw > bar.close:
                        _candidates.append((vw, "vwap"))
                    if not _isnan(or_low) and not _isnan(snap.or_high) and snap.or_high > bar.close:
                        _candidates.append((snap.or_high, "orh"))
                    if not _isnan(snap.session_high) and snap.session_high > bar.close:
                        _candidates.append((snap.session_high, "session_high"))
                    if not _isnan(pdl) and pdl > bar.close:
                        _candidates.append((pdl, "pdl"))
                    if not _isnan(snap.prior_day_high) and snap.prior_day_high > bar.close:
                        _candidates.append((snap.prior_day_high, "pdh"))

                    target, actual_rr, target_tag, skipped = compute_structural_target_long(
                        bar.close, risk, _candidates,
                        min_rr=cfg.fft_struct_min_rr, max_rr=cfg.fft_struct_max_rr,
                        fallback_rr=cfg.fft_target_rr, mode="structural",
                    )
                    if skipped:
                        self._phase = IDLE
                        self._tracked_session_low = min(self._tracked_session_low, bar.low)
                        return None
                else:
                    target = bar.close + risk * cfg.fft_target_rr
                    actual_rr = cfg.fft_target_rr
                    target_tag = "fixed_rr"

                # ── Quality scoring ──
                struct_q = 0.40
                # Spring type bonus — PDL undercut is higher conviction
                if self._spring_type == "pdl":
                    struct_q += 0.15
                # Fast confirmation bonus
                if self._bars_since_spring <= 2:
                    struct_q += 0.10
                # Volume on confirmation
                if bar.volume >= 1.2 * vol_ma:
                    struct_q += 0.10
                # Above EMA9
                if not _isnan(e9) and bar.close > e9:
                    struct_q += 0.10
                # Above VWAP (already gated, but score it)
                if not _isnan(vw) and bar.close > vw:
                    struct_q += 0.10

                confluence = ["fft_newlow_reversal"]
                if not _isnan(e9) and bar.close > e9:
                    confluence.append("above_ema9")
                if not _isnan(vw) and bar.close > vw:
                    confluence.append("above_vwap")
                if self._spring_type == "pdl":
                    confluence.append("pdl_undercut")
                if self._bars_since_spring <= 2:
                    confluence.append("fast_confirm")

                # ── Quality pipeline ──
                quality_score = 3  # default
                quality_tier = QualityTier.B_TIER
                reject_reasons = []

                if self.rejection and self.quality:
                    reject_reasons = self.rejection.check_all(
                        bar, snap.recent_bars_1m, len(snap.recent_bars_1m) - 1, i_atr, e9, vw, vol_ma,
                        skip_filters=self.skip_rejections
                    )

                    stock_factors = {
                        "in_play_score": snap.in_play_score,
                        "rs_market": snap.rs_market,
                        "rs_sector": 0.0,
                        "volume_profile": min(bar.volume / vol_ma, 1.0) if vol_ma > 0 else 0.0,
                    }
                    market_factors = {
                        "regime_score": snap.regime_score,
                        "alignment_score": snap.alignment_score,
                    }
                    setup_factors = {
                        "trigger_quality": trigger_bar_quality(bar, i_atr, vol_ma),
                        "structure_quality": min(struct_q, 1.0),
                        "confluence_count": len(confluence),
                    }

                    if not reject_reasons:
                        quality_tier, quality_score = self.quality.score(
                            stock_factors, market_factors, setup_factors
                        )
                    else:
                        _, quality_score = self.quality.score(
                            stock_factors, market_factors, setup_factors
                        )
                        quality_tier = QualityTier.B_TIER if quality_score >= self.cfg.quality_b_min else QualityTier.C_TIER

                self._phase = IDLE
                self._triggered_today = True
                # Update tracked low
                self._tracked_session_low = min(self._tracked_session_low, bar.low)

                return RawSignal(
                    strategy_name="FFT_NEWLOW_REV",
                    direction=1,
                    entry_price=bar.close,
                    stop_price=stop,
                    target_price=target,
                    bar_idx=snap.bar_idx,
                    hhmm=hhmm,
                    quality=quality_score,
                    metadata={
                        "spring_type": self._spring_type,
                        "spring_level": self._spring_level,
                        "spring_bar_low": self._spring_bar_low,
                        "spring_bar_high": self._spring_bar_high,
                        "bars_to_confirm": self._bars_since_spring,
                        "flush_depth_atr": (self._spring_level - self._spring_bar_low) / i_atr if i_atr > 0 else 0,
                        "actual_rr": actual_rr,
                        "target_tag": target_tag,
                        "structure_quality": min(struct_q, 1.0),
                        "confluence": confluence,
                        "quality_tier": quality_tier.value,
                        "reject_reasons": reject_reasons,
                        "in_play_score": snap.in_play_score,
                        "timeframe": "1m",
                        **_stop_meta,
                    },
                )

            # Update tracked low while waiting
            self._tracked_session_low = min(self._tracked_session_low, bar.low)
            return None

        # ── Phase: IDLE — watching for flush + spring ──
        if self._phase == IDLE:
            # Detect new low
            session_flush = bar.low < self._tracked_session_low
            pdl_flush = (cfg.fft_allow_pdl
                         and not _isnan(pdl)
                         and bar.low < pdl)

            if session_flush or pdl_flush:
                # Determine which level was undercut (prefer PDL if both)
                if pdl_flush and not _isnan(pdl):
                    flush_level = pdl
                    flush_type = "pdl"
                else:
                    flush_level = self._tracked_session_low
                    flush_type = "session_low"

                # ── Session-low-only variant gate ──
                if flush_type == "session_low" and not cfg.fft_allow_session_low:
                    self._tracked_session_low = min(self._tracked_session_low, bar.low)
                    return None

                # ── Dedup with ORL_FBD: only fire if flush is BELOW OR_low ──
                if cfg.fft_dedup_below_orl and not _isnan(or_low):
                    if flush_type == "session_low" and self._tracked_session_low >= or_low:
                        # The old session low was at or above OR_low;
                        # this is OR_low territory, not a true session flush
                        self._tracked_session_low = min(self._tracked_session_low, bar.low)
                        return None

                # ── Spring bar check: closes back above the old level ──
                spring_clearance = cfg.fft_spring_clearance_atr * i_atr
                if bar.close > flush_level + spring_clearance:
                    # Must be a bullish close (close > open)
                    if bar.close > bar.open:
                        # Spring detected!
                        self._phase = SPRING_DETECTED
                        self._spring_bar_high = bar.high
                        self._spring_bar_low = bar.low
                        self._spring_level = flush_level
                        self._spring_type = flush_type
                        self._bars_since_spring = 0

            # Always update tracked session low
            self._tracked_session_low = min(self._tracked_session_low, bar.low)
            return None

        # Fallback — update tracking
        self._tracked_session_low = min(self._tracked_session_low, bar.low)
        return None
