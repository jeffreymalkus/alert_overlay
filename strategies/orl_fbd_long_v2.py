"""
ORL_FBD_LONG v2 — Hybrid timeframe (5m context + 1m sequencing).

CORE THESIS: A meaningful breakdown below ORL traps shorts and shakes weak longs.
If price quickly reclaims above ORL, those shorts are trapped. Long entry after
structural confirmation (shelf above ORL + HH trigger on 1-min bars).

TIMEFRAME DESIGN:
  5-min bars: ORL definition, broader structure, VWAP context, regime
  1-min bars: breakdown detection, reclaim timing, shelf tracking, HH trigger

ENTRY SEQUENCE (single mode — simplified shelf per user spec):
  1. Breakdown: 1-min close below ORL with volume + bearish body
  2. Reclaim: 1-min close back above ORL within 12 bars
  3. Shelf: at least 2 consecutive 1-min bars with low staying above ORL
  4. HH trigger: 1-min close makes new high above shelf high, bullish body

GREEN-only gate from v1 retained pending reassessment.
"""

import math
from collections import deque
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from ..backtest import load_bars_from_csv
from ..models import Bar, NaN
from ..indicators import EMA, VWAPCalc

from .shared.signal_schema import StrategySignal, StrategyTrade, QualityTier
from .shared.config import StrategyConfig
from .shared.in_play_proxy import InPlayProxy
from .shared.market_regime import EnhancedMarketRegime
from .shared.rejection_filters import RejectionFilters
from .shared.quality_scoring import QualityScorer
from .shared.helpers import (
    bar_body_ratio, compute_rs_from_open, get_hhmm,
    compute_structural_target_long,
)
from .shared.level_helpers import breakout_quality

_isnan = math.isnan


# ── Lightweight 1-min indicator set ──────────────────────────────

class _Indicators1m:
    """Minimal indicators for 1-min bar processing within a day."""
    __slots__ = ('ema9', 'ema20', 'vwap', 'vol_buf', 'vol_ma', '_atr_buf', 'atr')

    def __init__(self):
        self.ema9 = EMA(9)
        self.ema20 = EMA(20)
        self.vwap = VWAPCalc()
        self.vol_buf: deque = deque(maxlen=20)
        self.vol_ma: float = NaN
        self._atr_buf: deque = deque(maxlen=14)
        self.atr: float = NaN

    def update(self, bar: Bar):
        self.ema9.update(bar.close)
        self.ema20.update(bar.close)
        tp = (bar.high + bar.low + bar.close) / 3.0
        self.vwap.update(tp, bar.volume)
        # Volume MA
        self.vol_buf.append(bar.volume)
        if len(self.vol_buf) >= 5:
            self.vol_ma = sum(self.vol_buf) / len(self.vol_buf)
        # ATR (simple TR average for 1-min)
        tr = bar.high - bar.low
        self._atr_buf.append(tr)
        if len(self._atr_buf) >= 5:
            self.atr = sum(self._atr_buf) / len(self._atr_buf)


# ── 5-min context builder ────────────────────────────────────────

class _Context5m:
    """Extracts 5-min context at a given timestamp."""

    def __init__(self):
        self.or_high: float = NaN
        self.or_low: float = NaN
        self.ema9: float = NaN
        self.ema20: float = NaN
        self.vwap: float = NaN
        self.atr: float = NaN

    @staticmethod
    def snapshot_at(bars_5m: List[Bar], dt: datetime) -> '_Context5m':
        """Build context up to a specific timestamp."""
        ctx = _Context5m()
        ema9 = EMA(9)
        ema20 = EMA(20)
        vwap = VWAPCalc()
        atr_buf: deque = deque(maxlen=14)

        day = dt.date()
        for bar in bars_5m:
            if bar.timestamp.date() != day:
                if bar.timestamp.date() > day:
                    break
                # Warm up EMAs from prior days
                ema9.update(bar.close)
                ema20.update(bar.close)
                tr = bar.high - bar.low
                atr_buf.append(tr)
                continue

            if bar.timestamp > dt:
                break

            hhmm = get_hhmm(bar)
            e9 = ema9.update(bar.close)
            e20 = ema20.update(bar.close)
            tp = (bar.high + bar.low + bar.close) / 3.0
            vw = vwap.update(tp, bar.volume)
            tr = bar.high - bar.low
            atr_buf.append(tr)

            if hhmm < 1000:
                if _isnan(ctx.or_high):
                    ctx.or_high = bar.high
                    ctx.or_low = bar.low
                else:
                    ctx.or_high = max(ctx.or_high, bar.high)
                    ctx.or_low = min(ctx.or_low, bar.low)

            ctx.ema9 = e9
            ctx.ema20 = e20
            ctx.vwap = vw
            if len(atr_buf) >= 5:
                ctx.atr = sum(atr_buf) / len(atr_buf)

        return ctx


# ── State machine phases ─────────────────────────────────────────

IDLE = 0
BROKE_DOWN = 1
RECLAIMED = 2
SHELF = 3      # reclaim confirmed + holding above ORL


class ORLFailedBDLongV2Strategy:
    """
    Hybrid-timeframe ORL Failed Breakdown Long.

    Replay strategy: iterates 1-min bars for event detection,
    uses 5-min context for ORL, structure, environment.

    Pipeline:
    1. In-play check (day level, from 5-min data)
    2. Market regime (GREEN-only day gate, per-signal intraday check)
    3. Raw detection: hybrid state machine on 1-min bars
    4. Quality scoring
    5. A-tier gating
    """

    def __init__(self, cfg: StrategyConfig, in_play: InPlayProxy,
                 regime: EnhancedMarketRegime, rejection: RejectionFilters,
                 quality: QualityScorer):
        self.cfg = cfg
        self.in_play = in_play
        self.regime = regime
        self.rejection = rejection
        self.quality = quality
        self.stats = {
            "total_symbol_days": 0, "passed_in_play": 0, "passed_regime": 0,
            "raw_signals": 0, "passed_rejection": 0,
            "reject_reasons": {},
            "a_tier": 0, "b_tier": 0, "c_tier": 0,
            "blocked_regime": 0, "blocked_bearish_intraday": 0,
            "breakdowns_detected": 0, "reclaims_detected": 0,
            "shelves_detected": 0,
        }

    def scan_day(self, symbol: str, bars_5m: List[Bar], bars_1m: List[Bar],
                 day: date, spy_bars: Optional[List[Bar]] = None,
                 sector_bars: Optional[List[Bar]] = None,
                 **kwargs) -> List[StrategySignal]:
        """Run full hybrid pipeline for one symbol-day."""
        cfg = self.cfg
        self.stats["total_symbol_days"] += 1

        # ── Step 1: In-play check (uses 5-min precomputed data) ──
        ip_pass, ip_score = self.in_play.is_in_play(symbol, day)
        if not ip_pass:
            return []
        self.stats["passed_in_play"] += 1

        # ── Step 2: GREEN-only day gate ──
        day_label = self.regime.get_day_label(day)
        if day_label != "GREEN":
            self.stats["blocked_regime"] += 1
            return []
        self.stats["passed_regime"] += 1

        # ── Get 1-min bars for this day ──
        day_bars_1m = [b for b in bars_1m if b.timestamp.date() == day]
        if len(day_bars_1m) < 30:
            return []

        # ── Get ORL from 1-min OR period (more precise than 5-min) ──
        or_bars_1m = [b for b in day_bars_1m if get_hhmm(b) < 1000]
        if not or_bars_1m:
            return []
        or_low = min(b.low for b in or_bars_1m)
        or_high = max(b.high for b in or_bars_1m)

        # ── Initialize 1-min indicators ──
        ind = _Indicators1m()

        # ── State machine ──
        phase = IDLE
        bars_since_bd = 0
        lowest_since_bd = NaN
        bd_quality = 0.0
        bd_bar_vol = 0.0
        reclaim_bar_high = NaN
        shelf_high = NaN
        shelf_low = NaN
        shelf_bars = 0
        bars_since_reclaim = 0
        triggered_today = False

        results = []

        for i, bar in enumerate(day_bars_1m):
            hhmm = get_hhmm(bar)
            ind.update(bar)

            # Skip OR period
            if hhmm < cfg.orl2_time_start or hhmm > cfg.orl2_time_end:
                continue

            # Need indicators ready
            atr_proxy = ind.atr
            if _isnan(atr_proxy) or atr_proxy <= 0:
                continue
            if _isnan(ind.vol_ma) or ind.vol_ma <= 0:
                continue

            if triggered_today:
                continue

            # ── Track lowest since breakdown ──
            if phase in (BROKE_DOWN, RECLAIMED, SHELF):
                if _isnan(lowest_since_bd) or bar.low < lowest_since_bd:
                    lowest_since_bd = bar.low

            # ── Tick state counters ──
            if phase == BROKE_DOWN:
                bars_since_bd += 1
                if bars_since_bd > cfg.orl2_reclaim_window:
                    phase = IDLE
                    continue

            elif phase in (RECLAIMED, SHELF):
                bars_since_reclaim += 1
                if bars_since_reclaim > cfg.orl2_hh_trigger_window:
                    phase = IDLE
                    continue

            # ── Phase 0 → 1: Breakdown detection ──
            if phase == IDLE:
                dist_below = or_low - bar.close
                if dist_below >= cfg.orl2_break_min_dist_atr * atr_proxy:
                    body_r = bar_body_ratio(bar)
                    if body_r >= cfg.orl2_break_body_min and bar.close < bar.open:
                        vol_ok = bar.volume >= cfg.orl2_break_vol_frac * ind.vol_ma
                        if vol_ok:
                            phase = BROKE_DOWN
                            bars_since_bd = 0
                            lowest_since_bd = bar.low
                            bd_quality = breakout_quality(bar, or_low, atr_proxy, ind.vol_ma, -1)
                            bd_bar_vol = bar.volume
                            self.stats["breakdowns_detected"] += 1
                            continue
                continue

            # ── Phase 1 → 2: Reclaim ──
            if phase == BROKE_DOWN:
                if bar.close > or_low:
                    body_r = bar_body_ratio(bar)
                    if body_r >= cfg.orl2_reclaim_body_min and bar.close > bar.open:
                        phase = RECLAIMED
                        reclaim_bar_high = bar.high
                        shelf_high = bar.high
                        shelf_low = bar.low
                        shelf_bars = 1 if bar.low >= or_low else 0
                        bars_since_reclaim = 0
                        self.stats["reclaims_detected"] += 1
                continue

            # ── Phase 2 → 3: Shelf detection ──
            if phase == RECLAIMED:
                # Track shelf: consecutive bars with low >= ORL
                if bar.low >= or_low - 0.02 * atr_proxy:  # small tolerance
                    shelf_bars += 1
                    shelf_high = max(shelf_high, bar.high)
                    shelf_low = min(shelf_low, bar.low)
                else:
                    # Price dipped below ORL — break shelf, but stay in RECLAIMED
                    shelf_bars = 0
                    shelf_high = bar.high
                    shelf_low = bar.low

                # Shelf confirmed when enough bars hold above ORL
                if shelf_bars >= cfg.orl2_shelf_min_bars:
                    phase = SHELF
                    self.stats["shelves_detected"] += 1
                    # Don't continue — check for HH trigger on this same bar

            # ── Phase 3 → Signal: HH trigger ──
            if phase == SHELF:
                # Continue tracking shelf high
                if bar.high > shelf_high:
                    shelf_high = bar.high

                # HH trigger: bar closes above reclaim_bar_high with bullish body + volume
                # Must make a meaningful new high, not just shelf-building noise
                if (bar.close > reclaim_bar_high and
                        bar.close > bar.open and
                        bar_body_ratio(bar) >= cfg.orl2_hh_trigger_body_min and
                        bar.volume >= 0.80 * ind.vol_ma):  # need volume confirmation

                    # ── Build signal ──
                    sig = self._build_signal(
                        symbol, bar, or_low, or_high, atr_proxy, ind, ip_score,
                        bars_5m, day, spy_bars,
                        bd_quality=bd_quality,
                        bd_bar_vol=bd_bar_vol,
                        bars_since_bd=bars_since_bd + bars_since_reclaim,
                        lowest_since_bd=lowest_since_bd,
                        shelf_bars=shelf_bars,
                    )
                    if sig is not None:
                        results.append(sig)
                    triggered_today = True
                    phase = IDLE
                    continue

                # If bar drops below ORL, reset shelf
                if bar.low < or_low - 0.05 * atr_proxy:
                    phase = IDLE
                    continue

        return results

    def _build_signal(self, symbol: str, bar: Bar, or_low: float, or_high: float,
                      atr: float, ind: _Indicators1m, ip_score: float,
                      bars_5m: List[Bar], day: date,
                      spy_bars: Optional[List[Bar]],
                      bd_quality: float, bd_bar_vol: float,
                      bars_since_bd: int, lowest_since_bd: float,
                      shelf_bars: int) -> Optional[StrategySignal]:
        """Build and filter a signal. Returns None if blocked."""
        cfg = self.cfg

        # ── Get 5-min context at signal time ──
        ctx_full = _Context5m.snapshot_at(bars_5m, bar.timestamp)
        atr_5m = ctx_full.atr if not _isnan(ctx_full.atr) and ctx_full.atr > 0 else atr
        vwap = ind.vwap.value if not _isnan(ind.vwap.value) else ctx_full.vwap

        self.stats["raw_signals"] += 1

        # ── Step 2b: Per-signal intraday check — block strongly bearish ──
        regime_snap = self.regime.get_nearest_regime(bar.timestamp)
        regime_label = regime_snap.day_label if regime_snap else "GREEN"
        if regime_snap and (not regime_snap.spy_above_vwap and
                not regime_snap.ema9_above_ema20 and
                regime_snap.spy_pct_from_open < -0.003):
            self.stats["blocked_bearish_intraday"] += 1
            return None

        # ── Stop / Target ──
        stop_ref = lowest_since_bd if not _isnan(lowest_since_bd) else or_low
        stop = stop_ref - cfg.orl2_stop_buffer_atr * atr_5m
        min_stop = cfg.orl2_min_stop_atr * atr_5m
        risk = bar.close - stop
        if risk < min_stop:
            stop = bar.close - min_stop
            risk = min_stop
        if risk <= 0:
            return None

        # Target: Structural or fixed RR
        if cfg.orl2_target_mode == "structural":
            _candidates = []
            if not _isnan(vwap) and vwap > bar.close:
                _candidates.append((vwap, "vwap"))
            if not _isnan(or_high) and or_high > bar.close:
                _candidates.append((or_high, "orh"))
            ctx_session_high = ctx_full.or_high
            if not _isnan(ctx_session_high) and ctx_session_high > bar.close:
                _candidates.append((ctx_session_high, "session_high"))
            target, actual_rr, target_tag, skipped = compute_structural_target_long(
                bar.close, risk, _candidates,
                min_rr=cfg.orl2_struct_min_rr, max_rr=cfg.orl2_struct_max_rr,
                fallback_rr=cfg.orl2_target_rr, mode="structural",
            )
            if skipped:
                return None  # skip signal if no valid structural target
        else:
            target = bar.close + cfg.orl2_target_rr * risk
            actual_rr = cfg.orl2_target_rr
            target_tag = "fixed_rr"

        # ── Step 4: Rejection filters — skip for hybrid (4-phase structure IS quality) ──
        self.stats["passed_rejection"] += 1

        # ── Step 5: Quality scoring ──
        regime_score = {"GREEN": 1.0, "FLAT": 0.5, "RED": 0.0}.get(regime_label, 0.5)

        # Trap depth: how far below ORL the breakdown went
        trap_depth = (or_low - lowest_since_bd) / atr_5m if atr_5m > 0 and not _isnan(lowest_since_bd) else 0
        trap_bonus = min(trap_depth * 0.15, 0.15)

        # Structure quality
        struct_q = 0.30
        if bd_quality >= 0.60:
            struct_q += 0.15
        if bars_since_bd <= 6:  # fast resolution (in 1-min bars)
            struct_q += 0.10
        if shelf_bars >= 4:
            struct_q += 0.10
        e9_val = ind.ema9.value
        if not _isnan(e9_val) and bar.close > e9_val:
            struct_q += 0.10
        if not _isnan(vwap) and vwap > bar.close:
            struct_q += 0.10  # room to VWAP
        if bar.volume >= 1.0 * ind.vol_ma:
            struct_q += 0.05

        quality_score = (
            0.25 * regime_score +
            0.25 * min(struct_q, 1.0) +
            0.20 * min(actual_rr / 2.0, 1.0) +
            0.15 * min(ip_score / 6.0, 1.0) +
            0.15 + trap_bonus
        )
        quality_score = min(quality_score, 1.0)

        if quality_score >= 0.55:
            tier = QualityTier.A_TIER
            self.stats["a_tier"] += 1
        elif quality_score >= 0.40:
            tier = QualityTier.B_TIER
            self.stats["b_tier"] += 1
        else:
            tier = QualityTier.C_TIER
            self.stats["c_tier"] += 1

        # ── Confluence tags ──
        tags = ["orl_fbd_v2"]
        if not _isnan(e9_val) and bar.close > e9_val:
            tags.append("above_ema9")
        if not _isnan(vwap) and vwap > bar.close:
            tags.append("room_to_vwap")
        if bd_quality >= 0.60:
            tags.append("strong_trap")
        if bars_since_bd <= 6:
            tags.append("fast_reclaim")
        if shelf_bars >= 4:
            tags.append("strong_shelf")
        if bar.volume >= 1.2 * ind.vol_ma:
            tags.append("vol_confirm")

        sig = StrategySignal(
            strategy_name="ORL_FBD_V2",
            symbol=symbol,
            timestamp=bar.timestamp,
            direction=1,  # LONG
            entry_price=bar.close,
            stop_price=stop,
            target_price=target,
            quality_score=quality_score,
            quality_tier=tier,
            in_play_score=ip_score,
            market_regime=regime_label,
            confluence_tags=tags,
            metadata={
                "or_low": or_low,
                "bd_quality": round(bd_quality, 3),
                "bd_bar_vol_ratio": round(bd_bar_vol / ind.vol_ma, 2) if ind.vol_ma > 0 else 0,
                "bars_to_reclaim": bars_since_bd,
                "lowest_since_bd": round(lowest_since_bd, 2) if not _isnan(lowest_since_bd) else None,
                "trap_depth_atr": round(trap_depth, 3),
                "shelf_bars": shelf_bars,
                "actual_rr": round(actual_rr, 3),
                "target_tag": target_tag,
                "vwap_at_signal": round(vwap, 2) if not _isnan(vwap) else None,
            },
        )

        if tier != QualityTier.A_TIER:
            sig.reject_reasons = ["quality_below_a"]

        return sig
