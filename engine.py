"""
Consolidated Alert Overlay v4.5 — Signal Engine

Processes bars one at a time, maintains all state, emits Signal objects.
Faithful port of the ThinkScript logic.
"""

import math
from collections import deque
from statistics import median
from typing import List, Optional, Tuple

from .config import OverlayConfig
from .models import Bar, Signal, DayState, SetupId, SetupFamily, SETUP_FAMILY_MAP, NaN
from .indicators import EMA, WildersMA, SMA, VWAPCalc, TrueRangeCalc, HighestLowest, RSI
from .market_context import MarketContext, MarketTrend, TradabilityScore, compute_tradability


def _isnan(v: float) -> bool:
    return math.isnan(v)


def _hhmm_from_dt(dt) -> int:
    """Extract HHMM integer from datetime."""
    return dt.hour * 100 + dt.minute


def _date_int(dt) -> int:
    """Extract YYYYMMDD integer from datetime."""
    return dt.year * 10000 + dt.month * 100 + dt.day


class SignalEngine:
    """
    Timeframe-agnostic bar-by-bar signal engine.
    Call `process_bar(bar)` for each new bar (any interval).
    Returns a list of Signal objects (usually 0 or 1).
    """

    def __init__(self, cfg: Optional[OverlayConfig] = None, universe_source: str = "STATIC"):
        self.cfg = cfg or OverlayConfig()
        self._universe_source = universe_source
        self._bar_index = 0

        # ── Indicators ──
        self.ema9 = EMA(9)
        self.ema20 = EMA(20)
        self.vwap = VWAPCalc()

        # Daily ATR — we feed it once per session close or from prior-day data
        self._daily_tr = TrueRangeCalc()
        self._daily_atr = WildersMA(self.cfg.atr_len)

        # Intraday ATR (5-min aggregation)
        self._intra_tr = TrueRangeCalc()
        self._intra_atr = WildersMA(self.cfg.atr_len)
        self._intra_atr_prev: float = NaN
        self._intra_agg_bars: List[Bar] = []  # accumulate for 5-min bar building
        self._intra_agg_count = 0

        # Volume MA (SMA of prior bars' volume, offset [1])
        self._vol_buf: deque = deque(maxlen=self.cfg.vol_lookback)
        self._vol_ma: float = NaN

        # Swing high/low for sweep detection
        self._high_buf: deque = deque(maxlen=self.cfg.swing_length)
        self._low_buf: deque = deque(maxlen=self.cfg.swing_length)

        # HTF (hourly) state
        self._htf_bars: List[Bar] = []
        self._htf_ema20 = EMA(20)
        self._htf_close_prev: float = NaN
        self._htf_ema20_prev: float = NaN
        self._htf_ema20_prev2: float = NaN

        # Prior-day data
        self.prev_day_high: float = NaN
        self.prev_day_low: float = NaN

        # Current day aggregation (for computing prev-day at EOD)
        self._cur_day_high: float = NaN
        self._cur_day_low: float = NaN

        # ── RVOL-by-time-of-day tracking ──
        # Keyed by HHMM bucket (5-min intervals), stores list of volumes per bucket across days
        self._vol_by_time: dict = {}       # {hhmm: deque(maxlen=20)} — historical volumes per time slot
        self._vol_by_time_today: dict = {} # {hhmm: volume} — today's volumes (for comparison)

        # ── 5-day high/low for breakout context ──
        self._daily_highs: deque = deque(maxlen=5)   # last 5 days' highs
        self._daily_lows: deque = deque(maxlen=5)     # last 5 days' lows

        # ── Bar history (small window for [1], [2], [3] refs) ──
        self._bars: deque = deque(maxlen=5)

        # ── Extended bar history for Spencer consolidation scan ──
        self._recent_bars: deque = deque(maxlen=20)

        # ── Bar range buffer for median-10 (Second Chance break quality) ──
        self._range_buf: deque = deque(maxlen=10)

        # ── Day state ──
        self.day = DayState()
        self._current_date: Optional[int] = None

        # ── RSI indicator (cross-day warmup — NOT reset on new day) ──
        _rsi_hist_len = max(self.cfg.rsi_impulse_lookback, self.cfg.rsi_pullback_lookback) + 2
        self.rsi = RSI(period=self.cfg.rsi_len, history_len=_rsi_hist_len)

        # ── Regime tracking buffers ──
        self._trend_raw_buf: deque = deque(maxlen=self.cfg.regime_hysteresis)
        self._hold_above_buf: deque = deque(maxlen=self.cfg.trend_day_hold_bars)
        self._hold_below_buf: deque = deque(maxlen=self.cfg.trend_day_hold_bars)

    # ─────────────────────────────────────────────
    # PUBLIC: feed prior-day data before first bar
    # ─────────────────────────────────────────────
    def set_prior_day(self, high: float, low: float):
        """Set prior day's high/low (call before processing current day bars)."""
        self.prev_day_high = high
        self.prev_day_low = low

    def set_daily_atr_history(self, daily_bars: List[dict]):
        """Feed historical daily OHLC to warm up daily ATR.
        Each dict: {'high': float, 'low': float, 'close': float}
        """
        for d in daily_bars:
            tr = self._daily_tr.update(d["high"], d["low"], d["close"])
            self._daily_atr.update(tr)

    # ─────────────────────────────────────────────
    # PUBLIC: process one bar
    # ─────────────────────────────────────────────
    def process_bar(self, bar: Bar, market_ctx: Optional[MarketContext] = None) -> List[Signal]:
        """Process a single bar with optional market context. Returns list of signals (0 or 1 typically)."""
        bar.date = _date_int(bar.timestamp)
        bar.time_hhmm = _hhmm_from_dt(bar.timestamp)

        # ── New day detection ──
        new_day = self._current_date is not None and bar.date != self._current_date
        if new_day:
            self._on_new_day(bar)
        if self._current_date is None:
            self._current_date = bar.date

        # ── Session time flags ──
        cfg = self.cfg
        in_or = bar.time_hhmm >= cfg.or_start and bar.time_hhmm < cfg.or_end
        after_or = bar.time_hhmm >= cfg.or_end
        after_day_type_check = bar.time_hhmm >= cfg.day_type_check_time
        after_late_grace = bar.time_hhmm >= cfg.late_regime_grace_time

        # ── Update indicators ──
        e9 = self.ema9.update(bar.close)
        e20 = self.ema20.update(bar.close)
        tp = (bar.high + bar.low + bar.close) / 3.0
        vw = self.vwap.update(tp, bar.volume)

        # RSI (cross-day state — no reset on new day)
        rsi_val = self.rsi.update(bar.close)

        # Volume MA (offset [1] — use buffer before appending current)
        vol_ma = self._vol_ma
        if len(self._vol_buf) == cfg.vol_lookback:
            vol_ma = sum(self._vol_buf) / cfg.vol_lookback
        self._vol_ma = vol_ma
        self._vol_buf.append(bar.volume)

        # RVOL by time-of-day: compare today's volume at this time slot to historical average
        hhmm = bar.time_hhmm
        self._vol_by_time_today[hhmm] = bar.volume
        rvol_tod = NaN
        if hhmm in self._vol_by_time and len(self._vol_by_time[hhmm]) >= 3:
            hist_avg = sum(self._vol_by_time[hhmm]) / len(self._vol_by_time[hhmm])
            if hist_avg > 0:
                rvol_tod = bar.volume / hist_avg

        # 5-day breakout context
        five_day_high = max(self._daily_highs) if len(self._daily_highs) >= 1 else NaN
        five_day_low = min(self._daily_lows) if len(self._daily_lows) >= 1 else NaN

        # Intraday ATR aggregation (build 5-min bars from 2-min bars)
        self._update_intra_atr(bar)
        i_atr = self._intra_atr_prev if cfg.use_completed_stop_atr else self._intra_atr.value
        i_atr_ready = not _isnan(i_atr) and i_atr > 0
        i_atr_safe = i_atr if i_atr_ready else 0.0

        d_atr = self._daily_atr.value
        d_atr_safe = d_atr if not _isnan(d_atr) else 0.0

        # ── Update day high/low tracking ──
        if _isnan(self._cur_day_high) or bar.high > self._cur_day_high:
            self._cur_day_high = bar.high
        if _isnan(self._cur_day_low) or bar.low < self._cur_day_low:
            self._cur_day_low = bar.low

        # ── Market context extraction (granular, independent fields) ──
        mkt_trend = MarketTrend.NEUTRAL
        sec_trend = MarketTrend.NEUTRAL
        rs_mkt = NaN
        rs_sec = NaN
        mkt_ctx_ready = False
        sec_ctx_ready = False

        # Raw snapshot references (for per-setup inspection)
        spy_snap = None
        sec_snap = None

        if market_ctx is not None and cfg.use_market_context:
            mkt_trend = market_ctx.market_trend
            rs_mkt = market_ctx.rs_market
            rs_sec = market_ctx.rs_sector
            mkt_ctx_ready = market_ctx.spy.ready
            spy_snap = market_ctx.spy

            if cfg.use_sector_context and market_ctx.sector.ready:
                sec_trend = market_ctx.sector.trend
                sec_ctx_ready = True
                sec_snap = market_ctx.sector

        # ── Compute graded tradability scores ──
        trad_score = TradabilityScore()
        if market_ctx is not None and cfg.use_market_context:
            trad_score = compute_tradability(market_ctx, cfg)

        # ── Per-field market state (independently usable) ──
        mkt_bull = mkt_trend == MarketTrend.BULL
        mkt_bear = mkt_trend == MarketTrend.BEAR

        sec_bull = sec_trend == MarketTrend.BULL
        sec_bear = sec_trend == MarketTrend.BEAR

        # RS checks (market + sector, separate)
        rs_mkt_ok_long = (_isnan(rs_mkt) or rs_mkt >= cfg.rs_market_min_long) if mkt_ctx_ready else True
        rs_mkt_ok_short = (_isnan(rs_mkt) or rs_mkt <= cfg.rs_market_max_short) if mkt_ctx_ready else True
        rs_sec_ok_long = (_isnan(rs_sec) or rs_sec >= cfg.rs_sector_min_long) if sec_ctx_ready else True
        rs_sec_ok_short = (_isnan(rs_sec) or rs_sec <= cfg.rs_sector_max_short) if sec_ctx_ready else True

        # ── Per-setup context gate results (uses granular snapshot fields) ──

        # VWAP_KISS: granular market structure check + sector + RS
        # Longs require: market above VWAP + (EMA9 above EMA20 OR EMA9 rising)
        # Shorts require inverse. Falls back to trend label if snapshots missing.
        vk_ctx_ok_long = True
        vk_ctx_ok_short = True
        if mkt_ctx_ready and cfg.vk_require_market_align:
            if spy_snap:
                # Granular: market must not be structurally weak for longs
                mkt_struct_weak = (not spy_snap.above_vwap and
                                   not spy_snap.ema9_above_ema20)
                mkt_struct_strong = (spy_snap.above_vwap and
                                     (spy_snap.ema9_above_ema20 or spy_snap.ema9_rising))
                if mkt_struct_weak:
                    vk_ctx_ok_long = False
                if mkt_struct_strong:
                    vk_ctx_ok_short = False
            else:
                if mkt_bear:
                    vk_ctx_ok_long = False
                if mkt_bull:
                    vk_ctx_ok_short = False
            if cfg.vk_require_rs_market and not rs_mkt_ok_long:
                vk_ctx_ok_long = False
            if cfg.vk_require_rs_market and not rs_mkt_ok_short:
                vk_ctx_ok_short = False
        if sec_ctx_ready and cfg.vk_require_sector_align:
            if sec_snap:
                sec_struct_weak = (not sec_snap.above_vwap and
                                   not sec_snap.ema9_above_ema20)
                sec_struct_strong = (sec_snap.above_vwap and
                                     (sec_snap.ema9_above_ema20 or sec_snap.ema9_rising))
                if sec_struct_weak:
                    vk_ctx_ok_long = False
                if sec_struct_strong:
                    vk_ctx_ok_short = False
            else:
                if sec_bear:
                    vk_ctx_ok_long = False
                if sec_bull:
                    vk_ctx_ok_short = False

        # EMA_SCALP: strictest — requires market + sector structural alignment + RS
        # Longs: market above VWAP, EMA9 > EMA20, EMA9 rising (2 of 3 structural)
        # Also requires sector above VWAP OR EMA9 rising
        es_ctx_ok_long = True
        es_ctx_ok_short = True
        if mkt_ctx_ready and cfg.ema_scalp_require_market_align:
            if spy_snap:
                # Count structural bull/bear signals (granular, not coarse trend)
                es_mkt_bull_ct = sum([
                    spy_snap.above_vwap,
                    spy_snap.ema9_above_ema20,
                    spy_snap.ema9_rising,
                ])
                es_mkt_bear_ct = sum([
                    not spy_snap.above_vwap,
                    not spy_snap.ema9_above_ema20,
                    spy_snap.ema9_falling,
                ])
                if es_mkt_bull_ct < 2:
                    es_ctx_ok_long = False
                if es_mkt_bear_ct < 2:
                    es_ctx_ok_short = False
            else:
                if mkt_bear:
                    es_ctx_ok_long = False
                if mkt_bull:
                    es_ctx_ok_short = False
            if cfg.ema_scalp_require_rs_market and not rs_mkt_ok_long:
                es_ctx_ok_long = False
            if cfg.ema_scalp_require_rs_market and not rs_mkt_ok_short:
                es_ctx_ok_short = False
        if sec_ctx_ready:
            if cfg.ema_scalp_require_sector_align:
                if sec_snap:
                    # Sector needs at least 1 structural sign of alignment
                    sec_supports_long = sec_snap.above_vwap or sec_snap.ema9_rising
                    sec_supports_short = (not sec_snap.above_vwap) or sec_snap.ema9_falling
                    if not sec_supports_long:
                        es_ctx_ok_long = False
                    if not sec_supports_short:
                        es_ctx_ok_short = False
                else:
                    if sec_bear:
                        es_ctx_ok_long = False
                    if sec_bull:
                        es_ctx_ok_short = False
            if cfg.ema_scalp_require_rs_sector and not rs_sec_ok_long:
                es_ctx_ok_long = False
            if cfg.ema_scalp_require_rs_sector and not rs_sec_ok_short:
                es_ctx_ok_short = False

        # SECOND_CHANCE: hostile tape block using granular fields + capped quality bonus
        # Hostile = market below VWAP + EMA9 falling + sector below VWAP + EMA9 falling
        sc_ctx_blocked_long = False
        sc_ctx_blocked_short = False
        sc_quality_bonus_long = 0
        sc_quality_bonus_short = 0
        if cfg.sc_hostile_tape_block and mkt_ctx_ready:
            if spy_snap and sec_snap and sec_ctx_ready:
                # Granular hostile: both market and sector structurally oppose
                hostile_long = ((not spy_snap.above_vwap and spy_snap.ema9_falling) and
                                (not sec_snap.above_vwap and sec_snap.ema9_falling))
                hostile_short = ((spy_snap.above_vwap and spy_snap.ema9_rising) and
                                 (sec_snap.above_vwap and sec_snap.ema9_rising))
            else:
                hostile_long = mkt_bear and (sec_bear if sec_ctx_ready else True)
                hostile_short = mkt_bull and (sec_bull if sec_ctx_ready else True)
            sc_ctx_blocked_long = hostile_long
            sc_ctx_blocked_short = hostile_short
        # Quality bonuses (additive, capped — cannot rescue structurally weak base)
        if mkt_ctx_ready and spy_snap:
            if spy_snap.above_vwap and spy_snap.ema9_above_ema20:
                sc_quality_bonus_long += cfg.sc_market_quality_bonus
            if not spy_snap.above_vwap and not spy_snap.ema9_above_ema20:
                sc_quality_bonus_short += cfg.sc_market_quality_bonus
        elif mkt_ctx_ready:
            if mkt_bull:
                sc_quality_bonus_long += cfg.sc_market_quality_bonus
            if mkt_bear:
                sc_quality_bonus_short += cfg.sc_market_quality_bonus
        if sec_ctx_ready and sec_snap:
            if sec_snap.above_vwap and sec_snap.ema9_above_ema20:
                sc_quality_bonus_long += cfg.sc_sector_quality_bonus
            if not sec_snap.above_vwap and not sec_snap.ema9_above_ema20:
                sc_quality_bonus_short += cfg.sc_sector_quality_bonus
        elif sec_ctx_ready:
            if sec_bull:
                sc_quality_bonus_long += cfg.sc_sector_quality_bonus
            if sec_bear:
                sc_quality_bonus_short += cfg.sc_sector_quality_bonus

        # ── Opening Range ──
        ds = self.day
        if in_or:
            if _isnan(ds.or_high) or bar.high > ds.or_high:
                ds.or_high = bar.high
            if _isnan(ds.or_low) or bar.low < ds.or_low:
                ds.or_low = bar.low
            ds.or_close = bar.close
            if _isnan(ds.or_open):
                ds.or_open = bar.open

        or_range = ds.or_high - ds.or_low if not _isnan(ds.or_high) and not _isnan(ds.or_low) else 0.0
        or_ready = after_or and or_range > 0
        ds.or_ready = or_ready

        # ── Prior-day box ──
        pdh = self.prev_day_high
        pdl = self.prev_day_low
        box_range = pdh - pdl if not _isnan(pdh) and not _isnan(pdl) else 0.0
        box_mid = (pdh + pdl) / 2 if box_range > 0 else NaN
        top_edge = pdh - cfg.box_edge_frac * box_range if box_range > 0 else NaN
        bot_edge = pdl + cfg.box_edge_frac * box_range if box_range > 0 else NaN

        at_top_edge = not _isnan(top_edge) and bar.close >= top_edge
        at_bot_edge = not _isnan(bot_edge) and bar.close <= bot_edge
        in_middle = not _isnan(top_edge) and bar.close < top_edge and bar.close > bot_edge
        box_ok = True if not cfg.require_box_edge else not in_middle

        # ── Derived distances ──
        confluence_dist = cfg.confluence_atr_frac * d_atr_safe
        reclaim_buf = cfg.reclaim_atr_frac * d_atr_safe
        kiss_dist = cfg.kiss_atr_frac * d_atr_safe
        min_stop_dist = cfg.min_stop_intra_atr_mult * i_atr_safe

        # ── OR direction ──
        or_up = False
        or_down = False
        or_close_near_high = False
        or_close_near_low = False
        or_drive_up = False
        or_drive_down = False
        if or_ready and or_range > 0:
            or_up = ds.or_close > ds.or_open + cfg.or_directional_frac * or_range
            or_down = ds.or_close < ds.or_open - cfg.or_directional_frac * or_range
            or_close_near_high = (ds.or_close - ds.or_low) / or_range >= cfg.or_close_extreme_frac
            or_close_near_low = (ds.or_high - ds.or_close) / or_range >= cfg.or_close_extreme_frac
            or_drive_up = or_up and or_close_near_high
            or_drive_down = or_down and or_close_near_low

        # ── Candle anatomy ──
        rng = bar.high - bar.low
        valid_bar = rng > 0
        upper_wick = (bar.high - max(bar.open, bar.close)) if valid_bar else 0.0
        lower_wick = (min(bar.open, bar.close) - bar.low) if valid_bar else 0.0

        strong_bull = valid_bar and bar.close > bar.open and upper_wick <= cfg.wick_oppose_max_frac * rng
        strong_bear = valid_bar and bar.close < bar.open and lower_wick <= cfg.wick_oppose_max_frac * rng

        entry_vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= cfg.entry_vol_min_frac * vol_ma
        pullback_vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume <= cfg.pullback_vol_max_frac * vol_ma
        exceptional_vol = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 1.5 * vol_ma

        bull_wick_reversal = valid_bar and lower_wick >= cfg.wick_reject_frac * rng and bar.close > bar.open
        bear_wick_reversal = valid_bar and upper_wick >= cfg.wick_reject_frac * rng and bar.close < bar.open

        # Engulfing
        body = abs(bar.close - bar.open)
        prev = self._bars[-1] if self._bars else None
        prev2 = self._bars[-2] if len(self._bars) >= 2 else None
        prev3 = self._bars[-3] if len(self._bars) >= 3 else None

        bull_engulf = False
        bear_engulf = False
        body1 = 0.0
        if prev and valid_bar:
            body1 = abs(prev.close - prev.open)
            bull_engulf = (bar.close > bar.open and
                           bar.open <= min(prev.open, prev.close) and
                           bar.close >= max(prev.open, prev.close) and
                           body >= body1)
            bear_engulf = (bar.close < bar.open and
                           bar.open >= max(prev.open, prev.close) and
                           bar.close <= min(prev.open, prev.close) and
                           body >= body1)

        # Push patterns (3-bar close sequence)
        up_push = False
        dn_push = False
        if prev and prev2 and prev3:
            up_push = prev.close > prev2.close and prev2.close > prev3.close
            dn_push = prev.close < prev2.close and prev2.close < prev3.close

        bear_wick_reject = up_push and bear_wick_reversal
        bull_wick_reject = dn_push and bull_wick_reversal

        # Prev-bar volume check
        prev_pullback_vol_ok = False
        if prev and len(self._vol_buf) >= 2:
            # vol_ma at prev bar time (approximate — use current vol_ma which already excludes current)
            prev_pullback_vol_ok = not _isnan(vol_ma) and vol_ma > 0 and prev.volume <= cfg.pullback_vol_max_frac * vol_ma

        # ── HTF alignment ──
        self._update_htf(bar)
        htf_bull = (not _isnan(self._htf_close_prev) and not _isnan(self._htf_ema20_prev)
                    and self._htf_close_prev > self._htf_ema20_prev
                    and self._htf_ema20_prev > self._htf_ema20_prev2)
        htf_bear = (not _isnan(self._htf_close_prev) and not _isnan(self._htf_ema20_prev)
                    and self._htf_close_prev < self._htf_ema20_prev
                    and self._htf_ema20_prev < self._htf_ema20_prev2)
        htf_ok_long = True if not cfg.use_htf_filter else htf_bull
        htf_ok_short = True if not cfg.use_htf_filter else htf_bear

        # ── Confluence ──
        near_pdh = not _isnan(pdh) and abs(bar.close - pdh) <= confluence_dist
        near_pdl = not _isnan(pdl) and abs(bar.close - pdl) <= confluence_dist
        near_orh = or_ready and abs(bar.close - ds.or_high) <= confluence_dist
        near_orl = or_ready and abs(bar.close - ds.or_low) <= confluence_dist
        near_vwap = self.vwap.ready and abs(bar.close - vw) <= confluence_dist
        near_ema20 = self.ema20.ready and abs(bar.close - e20) <= confluence_dist

        hard_confluence = near_pdh or near_pdl or near_orh or near_orl
        soft_confluence = near_vwap or near_ema20
        confluence_ok_reversal = hard_confluence
        confluence_ok_trend = hard_confluence or soft_confluence

        hard_conf_count = sum([near_pdh, near_pdl, near_orh, near_orl])

        # ── Sweep detection ──
        # Update swing buffers (using [1] — prior bar highs/lows)
        recent_swing_high = max(self._high_buf) if self._high_buf else NaN
        recent_swing_low = min(self._low_buf) if self._low_buf else NaN
        self._high_buf.append(bar.high)
        self._low_buf.append(bar.low)

        sweep_pdh_now = not _isnan(pdh) and bar.high > pdh and bar.close < pdh
        sweep_pdl_now = not _isnan(pdl) and bar.low < pdl and bar.close > pdl
        sweep_local_high_now = (not _isnan(recent_swing_high)
                                and bar.high > recent_swing_high
                                and bar.close < recent_swing_high)
        sweep_local_low_now = (not _isnan(recent_swing_low)
                               and bar.low < recent_swing_low
                               and bar.close > recent_swing_low)

        # Update bars-since-sweep counters
        ds.bars_since_sweep_pdh = self._update_sweep_counter(
            ds.bars_since_sweep_pdh, sweep_pdh_now,
            bar.close > pdh + reclaim_buf if not _isnan(pdh) else False)
        ds.bars_since_sweep_pdl = self._update_sweep_counter(
            ds.bars_since_sweep_pdl, sweep_pdl_now,
            bar.close < pdl - reclaim_buf if not _isnan(pdl) else False)
        ds.bars_since_sweep_local_high = self._update_sweep_counter(
            ds.bars_since_sweep_local_high, sweep_local_high_now,
            not _isnan(recent_swing_high) and bar.close > recent_swing_high + reclaim_buf)
        ds.bars_since_sweep_local_low = self._update_sweep_counter(
            ds.bars_since_sweep_local_low, sweep_local_low_now,
            not _isnan(recent_swing_low) and bar.close < recent_swing_low - reclaim_buf)

        sweep_pdh = ds.bars_since_sweep_pdh <= cfg.sweep_memory_bars
        sweep_pdl = ds.bars_since_sweep_pdl <= cfg.sweep_memory_bars
        sweep_local_high = ds.bars_since_sweep_local_high <= cfg.sweep_memory_bars
        sweep_local_low = ds.bars_since_sweep_local_low <= cfg.sweep_memory_bars

        ok_sweep_long_box = (not cfg.require_sweep_for_reversals) or (sweep_pdl and near_pdl)
        ok_sweep_short_box = (not cfg.require_sweep_for_reversals) or (sweep_pdh and near_pdh)
        ok_sweep_long_vwap = (not cfg.require_sweep_for_reversals) or sweep_local_low
        ok_sweep_short_vwap = (not cfg.require_sweep_for_reversals) or sweep_local_high
        ok_sweep_long_ema9 = (not cfg.require_sweep_for_reversals) or sweep_local_low
        ok_sweep_short_ema9 = (not cfg.require_sweep_for_reversals) or sweep_local_high

        # ── Day Type / Regime ──
        or_big = after_or and or_ready and or_range >= cfg.trend_day_or_atr_frac * d_atr_safe
        above_or = or_ready and bar.close > ds.or_high
        below_or = or_ready and bar.close < ds.or_low
        self._hold_above_buf.append(1 if above_or else 0)
        self._hold_below_buf.append(1 if below_or else 0)
        hold_above_or = above_or and sum(self._hold_above_buf) == cfg.trend_day_hold_bars
        hold_below_or = below_or and sum(self._hold_below_buf) == cfg.trend_day_hold_bars

        raw_trend_bull = after_day_type_check and or_big and hold_above_or and bar.close > vw
        raw_trend_bear = after_day_type_check and or_big and hold_below_or and bar.close < vw
        raw_trend = raw_trend_bull or raw_trend_bear

        self._trend_raw_buf.append(1 if raw_trend else 0)

        if after_day_type_check:
            ds.regime_known = True
            trend_count = sum(self._trend_raw_buf)
            fail_count = len(self._trend_raw_buf) - trend_count
            if not ds.is_trend_day_latched and trend_count == cfg.regime_hysteresis:
                ds.is_trend_day_latched = True
            elif ds.is_trend_day_latched and fail_count == cfg.regime_hysteresis:
                ds.is_trend_day_latched = False

        is_trend_day = ds.is_trend_day_latched
        is_rotation_day = ds.regime_known and not is_trend_day

        # ── Setup Detection ──
        manipulation = (after_or and or_ready and
                        or_range >= cfg.manipulation_atr_frac * d_atr_safe and
                        (or_drive_up or or_drive_down))
        manip_short_bias = manipulation and or_drive_up
        manip_long_bias = manipulation and or_drive_down

        box_short_bias = at_top_edge and ok_sweep_short_box and bear_wick_reversal
        box_long_bias = at_bot_edge and ok_sweep_long_box and bull_wick_reversal

        vwap_dist = abs(bar.close - vw) if self.vwap.ready else 0.0
        vwap_separator = vwap_dist >= cfg.vwap_sep_atr_mult * d_atr_safe and d_atr_safe > 0
        sep_short_bias = vwap_separator and bar.close > vw and ok_sweep_short_vwap and bear_wick_reversal
        sep_long_bias = vwap_separator and bar.close < vw and ok_sweep_long_vwap and bull_wick_reversal

        vwap_kiss = self.vwap.ready and abs(bar.close - vw) <= kiss_dist
        ema9_slope_up = self.ema9.ready and e9 > (self._bars[-1]._e9 if self._bars else e9)
        ema9_slope_dn = self.ema9.ready and e9 < (self._bars[-1]._e9 if self._bars else e9)

        # VWAP KISS: per-setup market + sector + RS gate
        # Opposing wick filter: reject trigger bars with excessive rejection wick
        _vk_wick_ok_long = True
        _vk_wick_ok_short = True
        _bar_rng = bar.high - bar.low
        if _bar_rng > 0 and cfg.vk_max_opposing_wick_pct < 1.0:
            _upper_wick_pct = (bar.high - max(bar.close, bar.open)) / _bar_rng
            _lower_wick_pct = (min(bar.close, bar.open) - bar.low) / _bar_rng
            _vk_wick_ok_long = _upper_wick_pct <= cfg.vk_max_opposing_wick_pct
            _vk_wick_ok_short = _lower_wick_pct <= cfg.vk_max_opposing_wick_pct

        kiss_long_bias = vwap_kiss and ema9_slope_up and bar.close > vw and strong_bull and vk_ctx_ok_long and _vk_wick_ok_long
        kiss_short_bias = (not cfg.vk_long_only) and vwap_kiss and ema9_slope_dn and bar.close < vw and strong_bear and vk_ctx_ok_short and _vk_wick_ok_short

        # ── MCS (Momentum Confirm at Structure) ──
        # Two modes:
        #   immediate: fire on the momentum candle itself
        #   confirm:   momentum candle sets latch, next bar confirms if price holds
        mcs_long_bias = False
        mcs_short_bias = False
        if cfg.show_mcs and valid_bar and self.vwap.ready and self.ema9.ready:
            _body_pct = body / rng if rng > 0 else 0.0
            _candle_ok_long = strong_bull and _body_pct >= cfg.mcs_min_body_pct
            _candle_ok_short = strong_bear and _body_pct >= cfg.mcs_min_body_pct
            if cfg.mcs_require_engulf:
                _candle_ok_long = _candle_ok_long and bull_engulf
                _candle_ok_short = _candle_ok_short and bear_engulf
            _mcs_vol_ok = (not _isnan(vol_ma) and vol_ma > 0 and
                           bar.volume >= cfg.mcs_vol_mult * vol_ma)
            # Structure proximity (MCS-specific distance, wider than kiss_dist)
            _mcs_dist = cfg.mcs_structure_atr_frac * d_atr_safe if d_atr_safe > 0 else kiss_dist
            _near_vwap = abs(bar.close - vw) <= _mcs_dist
            _near_ema9 = abs(bar.close - e9) <= _mcs_dist
            if cfg.mcs_structure == "vwap":
                _at_structure = _near_vwap
            elif cfg.mcs_structure == "ema9":
                _at_structure = _near_ema9
            else:  # "vwap_or_ema9"
                _at_structure = _near_vwap or _near_ema9
            # Anti-chase
            _not_chasing_long = (not cfg.mcs_max_above_or) or (not or_ready) or bar.close <= ds.or_high
            _not_chasing_short = (not cfg.mcs_max_above_or) or (not or_ready) or bar.close >= ds.or_low
            # Market alignment
            _mcs_ctx_long = vk_ctx_ok_long if cfg.mcs_require_market_align else True
            _mcs_ctx_short = vk_ctx_ok_short if cfg.mcs_require_market_align else True
            # Trend alignment
            _mcs_trend_long = ema9_slope_up and bar.close > vw
            _mcs_trend_short = ema9_slope_dn and bar.close < vw

            _mcs_setup_long = (_candle_ok_long and _mcs_vol_ok and _at_structure and
                               _not_chasing_long and _mcs_ctx_long and _mcs_trend_long)
            _mcs_setup_short = (not cfg.mcs_long_only and _candle_ok_short and _mcs_vol_ok and
                                _at_structure and _not_chasing_short and _mcs_ctx_short and _mcs_trend_short)

            if cfg.mcs_confirm_bars == 0:
                # Immediate mode: enter on the momentum candle
                mcs_long_bias = _mcs_setup_long
                mcs_short_bias = _mcs_setup_short
            else:
                # Latch mode: momentum candle sets latch, confirm on next bar(s)
                if _mcs_setup_long:
                    ds._mcs_long_latch_high = bar.high
                    ds._mcs_long_latch_mid = (bar.open + bar.close) / 2.0
                    ds._mcs_bars_since_long = 0
                elif hasattr(ds, '_mcs_bars_since_long'):
                    ds._mcs_bars_since_long += 1

                if _mcs_setup_short:
                    ds._mcs_short_latch_low = bar.low
                    ds._mcs_short_latch_mid = (bar.open + bar.close) / 2.0
                    ds._mcs_bars_since_short = 0
                elif hasattr(ds, '_mcs_bars_since_short'):
                    ds._mcs_bars_since_short += 1

                # Confirm long: bar closes above momentum candle midpoint, within N bars
                _mcs_long_valid = (hasattr(ds, '_mcs_bars_since_long') and
                                   0 < ds._mcs_bars_since_long <= cfg.mcs_confirm_bars and
                                   hasattr(ds, '_mcs_long_latch_mid'))
                if _mcs_long_valid:
                    mcs_long_bias = bar.close > ds._mcs_long_latch_mid and strong_bull

                _mcs_short_valid = (hasattr(ds, '_mcs_bars_since_short') and
                                    0 < ds._mcs_bars_since_short <= cfg.mcs_confirm_bars and
                                    hasattr(ds, '_mcs_short_latch_mid'))
                if _mcs_short_valid:
                    mcs_short_bias = bar.close < ds._mcs_short_latch_mid and strong_bear

        # ── VWAP Reclaim + Hold (H1) — long-only state machine ──
        vr_long_bias = False
        vr_stop_override = NaN  # custom stop from hold-period low

        # STATE TRACKING: runs from bar 1 (no after_or gate) so the engine
        # sees below-VWAP conditions during the opening range, matching
        # standalone behavior.  Only needs VWAP to be ready.
        if cfg.show_vwap_reclaim and valid_bar and self.vwap.ready:
            _vr_above_vwap = bar.close > vw

            if _vr_above_vwap and not ds.vr_was_below:
                # Still above VWAP, never went below — nothing to reclaim
                pass
            elif not _vr_above_vwap:
                # Below VWAP — set prerequisite, reset hold
                ds.vr_was_below = True
                ds.vr_hold_count = 0
                ds.vr_hold_low = NaN
            elif ds.vr_was_below and _vr_above_vwap and ds.vr_hold_count == 0:
                # Just reclaimed VWAP — start hold period
                ds.vr_hold_count = 1
                ds.vr_hold_low = bar.low
            elif ds.vr_hold_count > 0 and ds.vr_hold_count < cfg.vr_hold_bars:
                if _vr_above_vwap:
                    ds.vr_hold_count += 1
                    ds.vr_hold_low = min(ds.vr_hold_low, bar.low) if not _isnan(ds.vr_hold_low) else bar.low
                else:
                    # Failed hold — go back to below state
                    ds.vr_was_below = True
                    ds.vr_hold_count = 0
                    ds.vr_hold_low = NaN

        # TRIGGER EMISSION: only after opening range, in the configured time
        # window, with candle/volume/context/day-filter checks.
        if cfg.show_vwap_reclaim and valid_bar and self.vwap.ready and after_or:
            _vr_t = hhmm
            _vr_in_window = cfg.vr_time_start <= _vr_t <= cfg.vr_time_end
            _vr_above_vwap = bar.close > vw

            # Trigger check: hold complete, in time window, not yet triggered today
            if (ds.vr_hold_count >= cfg.vr_hold_bars and _vr_above_vwap and
                    _vr_in_window and not ds.vr_triggered):
                _vr_rng = bar.high - bar.low
                _vr_body = abs(bar.close - bar.open)
                _vr_is_bull = bar.close > bar.open
                _vr_body_pct = _vr_body / _vr_rng if _vr_rng > 0 else 0.0
                _vr_vol_ok = (not cfg.vr_require_vol or
                              (not _isnan(vol_ma) and vol_ma > 0 and
                               bar.volume >= cfg.vr_vol_frac * vol_ma))
                _vr_bull_ok = (not cfg.vr_require_bull) or _vr_is_bull
                _vr_candle_ok = _vr_bull_ok and _vr_body_pct >= cfg.vr_min_body_pct and _vr_vol_ok
                # Market context
                _vr_ctx_ok = vk_ctx_ok_long if cfg.vr_require_market_align else True

                # In-engine SPY day-filter for VR
                _vr_day_ok = True
                if cfg.vr_day_filter in ("green_only", "non_red"):
                    if spy_snap is not None and spy_snap.ready:
                        _spy_pct = spy_snap.pct_from_open if not _isnan(spy_snap.pct_from_open) else 0.0
                        if cfg.vr_day_filter == "green_only":
                            _vr_day_ok = _spy_pct > 0.05   # GREEN: > +0.05%
                        else:  # non_red
                            _vr_day_ok = _spy_pct >= -0.05  # non-RED: >= -0.05%
                    # If no spy_snap available, allow (conservative: don't block on missing data)

                if _vr_candle_ok and _vr_ctx_ok and _vr_day_ok:
                    vr_long_bias = True
                    _vr_hold_low = ds.vr_hold_low if not _isnan(ds.vr_hold_low) else bar.low
                    vr_stop_override = _vr_hold_low - cfg.vr_stop_buffer
                    ds.vr_triggered = True  # one trade per day

        # ── VK Accept (VWAP Kiss + Acceptance) — long-only state machine ──
        vka_long_bias = False
        vka_stop_override = NaN
        if cfg.show_vka and valid_bar and self.vwap.ready and after_or:
            _vka_t = hhmm
            _vka_in_window = cfg.vka_time_start <= _vka_t <= cfg.vka_time_end
            _vka_kiss_dist = cfg.vka_kiss_atr_frac * d_atr_safe if d_atr_safe > 0 else 0.0
            _vka_near_vwap = abs(bar.close - vw) <= _vka_kiss_dist
            _vka_above_vwap = bar.close >= vw  # at or above VWAP

            # State machine: touch → hold → trigger
            if not ds.vka_touched:
                # Looking for initial touch/kiss: price comes near VWAP from below or at level
                if _vka_near_vwap or (bar.low <= vw <= bar.high):
                    ds.vka_touched = True
                    ds.vka_hold_count = 0
                    ds.vka_hold_low = bar.low
                    ds.vka_hold_high = bar.high
            elif ds.vka_touched and ds.vka_hold_count < cfg.vka_hold_bars:
                # In hold period: price must stay near/above VWAP
                if _vka_above_vwap or _vka_near_vwap:
                    ds.vka_hold_count += 1
                    ds.vka_hold_low = min(ds.vka_hold_low, bar.low) if not _isnan(ds.vka_hold_low) else bar.low
                    ds.vka_hold_high = max(ds.vka_hold_high, bar.high) if not _isnan(ds.vka_hold_high) else bar.high
                else:
                    # Failed hold — reset, but if still near VWAP, restart touch
                    ds.vka_touched = False
                    ds.vka_hold_count = 0
                    ds.vka_hold_low = NaN
                    ds.vka_hold_high = NaN
                    # Check if this bar is a new touch
                    if _vka_near_vwap or (bar.low <= vw <= bar.high):
                        ds.vka_touched = True
                        ds.vka_hold_count = 0
                        ds.vka_hold_low = bar.low
                        ds.vka_hold_high = bar.high

            # Trigger check: hold complete, in window, not yet triggered
            if (ds.vka_touched and ds.vka_hold_count >= cfg.vka_hold_bars and
                    _vka_in_window and not ds.vka_triggered):
                _vka_rng = bar.high - bar.low
                _vka_body = abs(bar.close - bar.open)
                _vka_is_bull = bar.close > bar.open
                _vka_body_pct = _vka_body / _vka_rng if _vka_rng > 0 else 0.0
                _vka_vol_ok = (not cfg.vka_require_vol or
                               (not _isnan(vol_ma) and vol_ma > 0 and
                                bar.volume >= cfg.vka_vol_frac * vol_ma))
                _vka_bull_ok = (not cfg.vka_require_bull) or _vka_is_bull
                _vka_candle_ok = _vka_bull_ok and _vka_body_pct >= cfg.vka_min_body_pct and _vka_vol_ok
                # Market context (same gate as VK/VR)
                _vka_ctx_ok = vk_ctx_ok_long if cfg.vka_require_market_align else True

                # In-engine SPY day-filter for VKA (real-time, NOT perfect-foresight)
                _vka_day_ok = True
                if cfg.vka_day_filter in ("green_only", "non_red"):
                    if spy_snap is not None and spy_snap.ready:
                        _spy_pct = spy_snap.pct_from_open if not _isnan(spy_snap.pct_from_open) else 0.0
                        if cfg.vka_day_filter == "green_only":
                            _vka_day_ok = _spy_pct > 0.05
                        else:
                            _vka_day_ok = _spy_pct >= -0.05

                if _vka_candle_ok and _vka_ctx_ok and _vka_day_ok:
                    vka_long_bias = True
                    _vka_hold_low = ds.vka_hold_low if not _isnan(ds.vka_hold_low) else bar.low
                    vka_stop_override = _vka_hold_low - cfg.vka_stop_buffer
                    ds.vka_triggered = True

        # EMA cross tracking
        cross_up = prev and prev.close <= prev._e9 and bar.close > e9
        cross_dn = prev and prev.close >= prev._e9 and bar.close < e9
        if cross_up:
            ds.crossed_up_flag = True
            ds.crossed_dn_flag = False
        elif cross_dn:
            ds.crossed_dn_flag = True
            ds.crossed_up_flag = False

        retest_long = (cfg.show_ema_retest and ds.crossed_up_flag and self.ema9.ready and
                       bar.low <= e9 + kiss_dist and bar.close > e9 and
                       strong_bull and entry_vol_ok)
        retest_short = (cfg.show_ema_retest and ds.crossed_dn_flag and self.ema9.ready and
                        bar.high >= e9 - kiss_dist and bar.close < e9 and
                        strong_bear and entry_vol_ok)

        # EMA pullback
        trend_long = (self.ema9.ready and self.ema20.ready and
                      bar.close > e9 and e9 > e20 and ema9_slope_up and
                      prev and e20 > prev._e20)
        trend_short = (self.ema9.ready and self.ema20.ready and
                       bar.close < e9 and e9 < e20 and ema9_slope_dn and
                       prev and e20 < prev._e20)

        pb_into_zone_long = trend_long and bar.close < bar.open and bar.low <= e9
        pb_into_zone_short = trend_short and bar.close > bar.open and bar.high >= e9

        pullback_long_entry = False
        pullback_short_entry = False
        if prev:
            pullback_long_entry = (prev._pb_into_zone_long and prev_pullback_vol_ok and
                                   strong_bull and bar.high > prev.high and entry_vol_ok)
            pullback_short_entry = (prev._pb_into_zone_short and prev_pullback_vol_ok and
                                    strong_bear and bar.low < prev.low and entry_vol_ok)

        ema_sep = self.ema9.ready and abs(bar.close - e9) >= cfg.ema_sep_atr_mult * d_atr_safe and d_atr_safe > 0
        ema_sep_long = ema_sep and bar.close < e9 and ok_sweep_long_ema9 and bull_wick_reversal and entry_vol_ok
        ema_sep_short = ema_sep and bar.close > e9 and ok_sweep_short_ema9 and bear_wick_reversal and entry_vol_ok

        # ── Reversal Confirmation (v4.5 structure shift) ──
        rev_reject_long_now = box_long_bias or sep_long_bias or manip_long_bias or ema_sep_long
        rev_reject_short_now = box_short_bias or sep_short_bias or manip_short_bias or ema_sep_short

        # Long latch
        if rev_reject_long_now:
            ds.rev_long_latch_high = bar.high
            ds.bars_since_rev_long = 0
        elif ds.bars_since_rev_long < 999:
            ds.bars_since_rev_long += 1

        rev_long_expired = ds.bars_since_rev_long > cfg.rev_confirm_bars
        rev_long_valid = (not rev_long_expired and ds.bars_since_rev_long >= 1 and
                          not _isnan(ds.rev_long_latch_high))
        rev_long_confirmed = rev_long_valid and bar.close > ds.rev_long_latch_high

        # Short latch
        if rev_reject_short_now:
            ds.rev_short_latch_low = bar.low
            ds.bars_since_rev_short = 0
        elif ds.bars_since_rev_short < 999:
            ds.bars_since_rev_short += 1

        rev_short_expired = ds.bars_since_rev_short > cfg.rev_confirm_bars
        rev_short_valid = (not rev_short_expired and ds.bars_since_rev_short >= 1 and
                           not _isnan(ds.rev_short_latch_low))
        rev_short_confirmed = rev_short_valid and bar.close < ds.rev_short_latch_low

        # ── Signal Assembly ──
        long_setup_a_level = (manip_long_bias or box_long_bias) and after_or and box_ok and confluence_ok_reversal
        long_setup_a_sep = sep_long_bias and after_or and box_ok and confluence_ok_reversal
        short_setup_a_level = (manip_short_bias or box_short_bias) and after_or and box_ok and confluence_ok_reversal
        short_setup_a_sep = sep_short_bias and after_or and box_ok and confluence_ok_reversal

        long_signal_a = (cfg.show_reversal_setups and
                         (long_setup_a_level or long_setup_a_sep) and
                         rev_long_confirmed and entry_vol_ok and htf_ok_long)
        short_signal_a = (cfg.show_reversal_setups and
                          (short_setup_a_level or short_setup_a_sep) and
                          rev_short_confirmed and entry_vol_ok and htf_ok_short)

        # EMA_PULL decoupled: fires on show_ema_pullback alone (no show_trend_setups needed).
        # VWAP_KISS, EMA_RETEST, MCS still require show_trend_setups.
        long_signal_b = (htf_ok_long and
                         ((cfg.show_trend_setups and retest_long and confluence_ok_trend) or
                          (cfg.show_ema_pullback and pullback_long_entry and confluence_ok_trend) or
                          (cfg.show_trend_setups and kiss_long_bias and confluence_ok_trend) or
                          (cfg.show_trend_setups and mcs_long_bias)))
        short_signal_b = (htf_ok_short and
                          ((cfg.show_trend_setups and retest_short and confluence_ok_trend) or
                           (cfg.show_ema_pullback and pullback_short_entry and confluence_ok_trend) or
                           (cfg.show_trend_setups and kiss_short_bias and confluence_ok_trend) or
                           (cfg.show_trend_setups and mcs_short_bias)))

        long_signal_c = cfg.show_ema_mean_rev and ema_sep_long and rev_long_confirmed and htf_ok_long
        short_signal_c = cfg.show_ema_mean_rev and ema_sep_short and rev_short_confirmed and htf_ok_short

        # ── Primary Setup ID ──
        ema9_sep_high_pri = not ds.regime_known or is_rotation_day or after_late_grace

        # Gate pullback entry by show flag before passing to ID resolution
        gated_pb_long = pullback_long_entry and cfg.show_ema_pullback
        gated_pb_short = pullback_short_entry and cfg.show_ema_pullback

        primary_long_id = self._resolve_primary_id(
            box_long_bias, manip_long_bias, sep_long_bias, ema_sep_long,
            kiss_long_bias, gated_pb_long, retest_long,
            long_signal_a, long_signal_b, long_signal_c,
            ema9_sep_high_pri, mcs_bias=mcs_long_bias)

        primary_short_id = self._resolve_primary_id(
            box_short_bias, manip_short_bias, sep_short_bias, ema_sep_short,
            kiss_short_bias, gated_pb_short, retest_short,
            short_signal_a, short_signal_b, short_signal_c,
            ema9_sep_high_pri, mcs_bias=mcs_short_bias)

        has_long_signal = primary_long_id != SetupId.NONE and after_or
        has_short_signal = primary_short_id != SetupId.NONE and after_or

        # ── EMA PULL per-setup gates: time cutoff + short-only ──
        if primary_long_id == SetupId.EMA_PULL:
            if bar.time_hhmm >= cfg.ep_time_end:
                has_long_signal = False
            if cfg.ep_short_only:
                has_long_signal = False
        if primary_short_id == SetupId.EMA_PULL:
            if bar.time_hhmm >= cfg.ep_time_end:
                has_short_signal = False

        is_trend_id_long = primary_long_id in (SetupId.VWAP_KISS, SetupId.EMA_PULL, SetupId.EMA_RETEST, SetupId.MCS)
        is_trend_id_short = primary_short_id in (SetupId.VWAP_KISS, SetupId.EMA_PULL, SetupId.EMA_RETEST, SetupId.MCS)
        is_revert_id_long = primary_long_id in (SetupId.BOX_REV, SetupId.MANIP, SetupId.VWAP_SEP)
        is_revert_id_short = primary_short_id in (SetupId.BOX_REV, SetupId.MANIP, SetupId.VWAP_SEP)
        is_ema_id_long = primary_long_id == SetupId.EMA9_SEP
        is_ema_id_short = primary_short_id == SetupId.EMA9_SEP

        # ── Regime fit ──
        long_fits_regime = self._fits_regime(
            after_late_grace, ds.regime_known, is_trend_day, is_rotation_day,
            is_trend_id_long, is_revert_id_long, is_ema_id_long)
        short_fits_regime = self._fits_regime(
            after_late_grace, ds.regime_known, is_trend_day, is_rotation_day,
            is_trend_id_short, is_revert_id_short, is_ema_id_short)

        # ── Stops & Targets ──
        pb_stop_long = prev.low if prev else bar.low
        pb_stop_short = prev.high if prev else bar.high

        raw_long_stop = pb_stop_long if is_trend_id_long else bar.low
        raw_short_stop = pb_stop_short if is_trend_id_short else bar.high

        long_stop = min(raw_long_stop, bar.close - min_stop_dist) if is_trend_id_long else raw_long_stop
        short_stop = max(raw_short_stop, bar.close + min_stop_dist) if is_trend_id_short else raw_short_stop

        long_risk = bar.close - long_stop
        short_risk = short_stop - bar.close

        long_target = self._compute_target(primary_long_id, vw, e9, box_mid, or_range, bar.close, 1, cfg=cfg, d_atr=d_atr_safe)
        short_target = self._compute_target(primary_short_id, vw, e9, box_mid, or_range, bar.close, -1, cfg=cfg, d_atr=d_atr_safe)

        # ── Risk / Reward gating ──
        max_risk_allowed = (min(cfg.max_risk_frac_of_or * or_range, cfg.max_risk_frac_of_atr * d_atr_safe)
                            if or_ready else NaN)

        long_risk_above_floor = i_atr_ready and long_risk >= min_stop_dist
        short_risk_above_floor = i_atr_ready and short_risk >= min_stop_dist

        long_risk_ok = or_ready and not _isnan(max_risk_allowed) and long_risk <= max_risk_allowed and long_risk_above_floor
        short_risk_ok = or_ready and not _isnan(max_risk_allowed) and short_risk <= max_risk_allowed and short_risk_above_floor

        long_target_ok = not _isnan(long_target) and long_target > bar.close
        short_target_ok = not _isnan(short_target) and short_target < bar.close

        long_reward = (long_target - bar.close) if has_long_signal and long_target_ok else 0.0
        short_reward = (bar.close - short_target) if has_short_signal and short_target_ok else 0.0

        long_rr_ok = long_target_ok and long_risk > 0 and (long_reward / long_risk) >= cfg.min_rr
        short_rr_ok = short_target_ok and short_risk > 0 and (short_reward / short_risk) >= cfg.min_rr

        # ── Quality scoring ──
        q_long = self._quality_score(
            cfg, htf_bull, hard_confluence, hard_conf_count, exceptional_vol,
            long_fits_regime, long_rr_ok, long_risk, long_reward,
            is_trend_id_long, is_revert_id_long, is_ema_id_long)
        q_short = self._quality_score(
            cfg, htf_bear, hard_confluence, hard_conf_count, exceptional_vol,
            short_fits_regime, short_rr_ok, short_risk, short_reward,
            is_trend_id_short, is_revert_id_short, is_ema_id_short)

        # ── Hard gated triggers ──
        new_long_raw = has_long_signal and not ds.prev_has_long_signal
        new_short_raw = has_short_signal and not ds.prev_has_short_signal

        # Per-setup regime gate for primary path (EMA_PULL uses ep_require_regime)
        _regime_req_long = cfg.ep_require_regime if primary_long_id == SetupId.EMA_PULL else cfg.require_regime
        _regime_req_short = cfg.ep_require_regime if primary_short_id == SetupId.EMA_PULL else cfg.require_regime

        new_long_signal = (new_long_raw and long_risk_ok and long_target_ok and long_rr_ok
                           and q_long >= cfg.min_quality
                           and (not _regime_req_long or long_fits_regime))
        new_short_signal = (new_short_raw and short_risk_ok and short_target_ok and short_rr_ok
                            and q_short >= cfg.min_quality
                            and (not _regime_req_short or short_fits_regime))

        # ── Per-family cooldown ──
        signals: List[Signal] = []

        if new_long_signal:
            family = SETUP_FAMILY_MAP.get(primary_long_id, SetupFamily.REVERSAL)
            cd_val = self._get_cooldown(ds, 1, family)
            if cd_val >= cfg.alert_cooldown_bars:
                self._reset_cooldown(ds, 1, family)
                sig = self._build_signal(
                    bar, 1, primary_long_id, family, bar.close, long_stop, long_target,
                    long_risk, long_reward, q_long, long_fits_regime, vw,
                    or_up, or_down, near_pdh, near_pdl, near_orh, near_orl, near_vwap, near_ema20,
                    sweep_pdh, sweep_pdl, sweep_local_high, sweep_local_low)
                sig.market_trend = int(mkt_trend)
                sig.rs_market = rs_mkt
                sig.rs_sector = rs_sec
                sig.tradability_long = trad_score.long_score
                sig.tradability_short = trad_score.short_score
                signals.append(sig)
            else:
                self._reset_cooldown(ds, 1, family)

        if new_short_signal:
            family = SETUP_FAMILY_MAP.get(primary_short_id, SetupFamily.REVERSAL)
            cd_val = self._get_cooldown(ds, -1, family)
            if cd_val >= cfg.alert_cooldown_bars:
                self._reset_cooldown(ds, -1, family)
                sig = self._build_signal(
                    bar, -1, primary_short_id, family, bar.close, short_stop, short_target,
                    short_risk, short_reward, q_short, short_fits_regime, vw,
                    or_up, or_down, near_pdh, near_pdl, near_orh, near_orl, near_vwap, near_ema20,
                    sweep_pdh, sweep_pdl, sweep_local_high, sweep_local_low)
                sig.market_trend = int(mkt_trend)
                sig.rs_market = rs_mkt
                sig.rs_sector = rs_sec
                sig.tradability_long = trad_score.long_score
                sig.tradability_short = trad_score.short_score
                signals.append(sig)
            else:
                self._reset_cooldown(ds, -1, family)

        # Tick all cooldowns that didn't fire
        self._tick_all_cooldowns(ds)

        # ── Update line persistence (Option B) ──
        if new_long_signal:
            ds.pos_dir = 1
            ds.stop_line = long_stop
            ds.target_line = long_target
            ds.setup_active = True
        elif new_short_signal:
            ds.pos_dir = -1
            ds.stop_line = short_stop
            ds.target_line = short_target
            ds.setup_active = True

        if ds.setup_active and not _isnan(ds.stop_line) and not _isnan(ds.target_line):
            hit_target = ((ds.pos_dir == 1 and bar.high >= ds.target_line) or
                          (ds.pos_dir == -1 and bar.low <= ds.target_line))
            hit_stop = ((ds.pos_dir == 1 and bar.low <= ds.stop_line) or
                        (ds.pos_dir == -1 and bar.high >= ds.stop_line))
            if hit_target or hit_stop:
                ds.setup_active = False

        # ── Save state for next bar's [1] references ──
        ds.prev_has_long_signal = has_long_signal
        ds.prev_has_short_signal = has_short_signal

        # ── Update session tracking for Spencer (before detection) ──
        if _isnan(ds.session_open):
            ds.session_open = bar.open
        if _isnan(ds.session_low) or bar.low < ds.session_low:
            ds.session_low = bar.low
        if _isnan(ds.session_high) or bar.high > ds.session_high:
            ds.session_high = bar.high

        # ── Second Chance & Spencer Detection ──
        sc_long_signal, sc_short_signal = False, False
        sc_long_stop, sc_short_stop = NaN, NaN
        sc_long_quality, sc_short_quality = 0, 0
        sp_long_signal, sp_short_signal = False, False
        sp_long_stop, sp_short_stop = NaN, NaN
        sp_long_quality, sp_short_quality = 0, 0

        in_sc_time = bar.time_hhmm >= cfg.sc_time_start and bar.time_hhmm <= cfg.sc_time_end
        in_sp_time = bar.time_hhmm >= cfg.sp_time_start and bar.time_hhmm <= cfg.sp_time_end

        if cfg.show_second_chance and after_or and i_atr_ready:
            sc_long_signal, sc_long_stop, sc_long_quality = self._detect_second_chance(
                bar, ds, cfg, 1, e9, vw, vol_ma, i_atr_safe, d_atr_safe,
                in_sc_time, htf_bull)
            sc_short_signal, sc_short_stop, sc_short_quality = self._detect_second_chance(
                bar, ds, cfg, -1, e9, vw, vol_ma, i_atr_safe, d_atr_safe,
                in_sc_time, htf_bear)

        if cfg.show_spencer and after_or and i_atr_ready:
            sp_long_signal, sp_long_stop, sp_long_quality = self._detect_spencer(
                bar, ds, cfg, 1, e9, e20, vw, vol_ma, i_atr_safe, d_atr_safe,
                in_sp_time, htf_bull)
            sp_short_signal, sp_short_stop, sp_short_quality = self._detect_spencer(
                bar, ds, cfg, -1, e9, e20, vw, vol_ma, i_atr_safe, d_atr_safe,
                in_sp_time, htf_bear)

        # ── Second Chance V2 Detection ──
        sc2_long_signal, sc2_short_signal = False, False
        sc2_long_stop, sc2_short_stop = NaN, NaN
        sc2_long_quality, sc2_short_quality = 0, 0
        in_sc2_time = (bar.time_hhmm >= cfg.sc2_time_start and
                       bar.time_hhmm <= cfg.sc2_time_end)

        if cfg.show_sc_v2 and after_or and i_atr_ready:
            self._update_sc2_state(bar, ds, cfg, e9, e20, vw, i_atr_safe,
                                    vol_ma, rvol_tod)
            sc2_long_signal, sc2_long_stop, sc2_long_quality = self._detect_sc2(
                bar, ds, cfg, 1, e9, e20, vw, vol_ma, i_atr_safe, d_atr_safe,
                rvol_tod, in_sc2_time, htf_bull, rs_mkt, rs_sec)
            sc2_short_signal, sc2_short_stop, sc2_short_quality = self._detect_sc2(
                bar, ds, cfg, -1, e9, e20, vw, vol_ma, i_atr_safe, d_atr_safe,
                rvol_tod, in_sc2_time, htf_bear, rs_mkt, rs_sec)

        # ── Failed Bounce Detection (short-only) ──
        fb_signal, fb_stop, fb_quality = False, NaN, 0
        in_fb_time = bar.time_hhmm >= cfg.fb_time_start and bar.time_hhmm <= cfg.fb_time_end

        if cfg.show_failed_bounce and after_or and i_atr_ready:
            fb_signal, fb_stop, fb_quality = self._detect_failed_bounce(
                bar, ds, cfg, e9, vw, vol_ma, i_atr_safe, d_atr_safe,
                in_fb_time, htf_bear)  # always uses htf_bear since it's short-only

        # ── Breakdown-Retest Short Detection ──
        bdr_signal, bdr_stop, bdr_quality = False, NaN, 0
        in_bdr_time = bar.time_hhmm >= cfg.bdr_time_start and bar.time_hhmm <= cfg.bdr_time_end

        if cfg.show_breakdown_retest and after_or and i_atr_ready:
            bdr_signal, bdr_stop, bdr_quality = self._detect_bdr_short(
                bar, ds, cfg, e9, vw, vol_ma, i_atr_safe, d_atr_safe,
                in_bdr_time, htf_bear)

        # ── EMA Scalp Detection ──
        es_long_signal, es_short_signal = False, False
        es_long_stop, es_short_stop = NaN, NaN
        es_long_quality, es_short_quality = 0, 0
        es_long_id, es_short_id = SetupId.NONE, SetupId.NONE
        in_es_time = (bar.time_hhmm >= cfg.ema_scalp_time_start and
                      bar.time_hhmm <= cfg.ema_scalp_time_end)

        if cfg.show_ema_scalp and after_or and i_atr_ready:
            # Update state every bar (impulse + pullback tracking)
            self._update_ema_scalp_state(bar, ds, cfg, e9, e20, vw, i_atr_safe,
                                          rvol_tod, five_day_high, five_day_low)
            # Detect
            es_long_signal, es_long_stop, es_long_quality, es_long_id = self._detect_ema_scalp(
                bar, ds, cfg, 1, e9, e20, vw, vol_ma, i_atr_safe, d_atr_safe,
                rvol_tod, five_day_high, five_day_low, in_es_time, htf_bull)
            es_short_signal, es_short_stop, es_short_quality, es_short_id = self._detect_ema_scalp(
                bar, ds, cfg, -1, e9, e20, vw, vol_ma, i_atr_safe, d_atr_safe,
                rvol_tod, five_day_high, five_day_low, in_es_time, htf_bear)

            # EMA scalp: per-setup market + sector + RS gate (all hard)
            if not es_ctx_ok_long:
                es_long_signal = False
            if not es_ctx_ok_short:
                es_short_signal = False
            # Gate EMA_CONFIRM by dedicated toggle
            if not cfg.show_ema_confirm:
                if es_long_id == SetupId.EMA_CONFIRM:
                    es_long_signal = False
                if es_short_id == SetupId.EMA_CONFIRM:
                    es_short_signal = False

        # ── EMA FPIP Detection ──
        fpip_long_signal, fpip_short_signal = False, False
        fpip_long_stop, fpip_short_stop = NaN, NaN
        fpip_long_quality, fpip_short_quality = 0, 0
        in_fpip_time = (bar.time_hhmm >= cfg.ema_fpip_time_start and
                        bar.time_hhmm <= cfg.ema_fpip_time_end)

        if cfg.show_ema_fpip and after_or and i_atr_ready:
            self._update_fpip_state(bar, ds, cfg, e9, e20, vw, i_atr_safe,
                                     vol_ma, rvol_tod)
            fpip_long_signal, fpip_long_stop, fpip_long_quality = self._detect_fpip(
                bar, ds, cfg, 1, e9, e20, vw, vol_ma, i_atr_safe, d_atr_safe,
                rvol_tod, in_fpip_time, htf_bull)
            fpip_short_signal, fpip_short_stop, fpip_short_quality = self._detect_fpip(
                bar, ds, cfg, -1, e9, e20, vw, vol_ma, i_atr_safe, d_atr_safe,
                rvol_tod, in_fpip_time, htf_bear)

        # ── Breakout setup regime fit ──
        sc_long_fits = self._fits_regime(after_late_grace, ds.regime_known, is_trend_day,
                                          is_rotation_day, True, False, False)
        sc_short_fits = self._fits_regime(after_late_grace, ds.regime_known, is_trend_day,
                                           is_rotation_day, True, False, False)
        sp_long_fits = sc_long_fits  # same logic — breakouts fit trend days
        sp_short_fits = sc_short_fits

        # Failed Bounce fits rotation days (weak/choppy tape) — pass is_revert_id=True
        fb_fits = self._fits_regime(after_late_grace, ds.regime_known, is_trend_day,
                                     is_rotation_day, False, True, False)
        # BDR regime gate: RED+TREND only (Portfolio C frozen rule)
        # RED = SPY pct_from_open < -0.05%
        # TREND = SPY close in bottom 25% of day range (for RED days)
        bdr_fits = True  # default if no market context or gate disabled
        if cfg.bdr_require_red_trend and spy_snap is not None and spy_snap.ready:
            spy_pct = spy_snap.pct_from_open if not _isnan(spy_snap.pct_from_open) else 0.0
            is_spy_red = spy_pct < -0.05
            # TREND character: close in bottom 25% of intraday range
            spy_dh = spy_snap.day_high if not _isnan(spy_snap.day_high) else 0
            spy_dl = spy_snap.day_low if not _isnan(spy_snap.day_low) else 0
            spy_day_range = spy_dh - spy_dl
            if spy_day_range > 0:
                spy_close_pos = (spy_snap.close - spy_dl) / spy_day_range
                is_spy_trend = spy_close_pos <= 0.25
            else:
                is_spy_trend = False
            bdr_fits = is_spy_red and is_spy_trend
        # AM-only time gate (Portfolio C frozen rule: entries before 11:00 only)
        if cfg.bdr_am_only and bar.time_hhmm >= cfg.bdr_am_cutoff:
            bdr_fits = False

        # EMA Scalp fits both trend AND rotation days — it's a micro-structure
        # continuation setup that works in either regime. Always pass regime check.
        es_long_fits = True
        es_short_fits = True
        fpip_long_fits = True
        fpip_short_fits = True
        # SC_V2 is a breakout continuation setup — fits trend days like SC
        sc2_long_fits = self._fits_regime(after_late_grace, ds.regime_known, is_trend_day,
                                           is_rotation_day, True, False, False)
        sc2_short_fits = sc2_long_fits

        # ── VWAP Reclaim: compute stop/target/quality for dedicated emission ──
        vr_signal = False
        vr_stop = NaN
        vr_target = NaN
        vr_quality = 0
        vr_fits = True  # VR is trend-day setup (long continuation)
        if vr_long_bias and not _isnan(vr_stop_override):
            vr_signal = True
            vr_stop = vr_stop_override
            _vr_risk = bar.close - vr_stop
            if _vr_risk > 0:
                vr_target = bar.close + cfg.vr_target_rr * _vr_risk
                # Quality: base 4 + regime bonus + market bonus
                vr_quality = 4
                if long_fits_regime:
                    vr_quality += 1
                if mkt_bull:
                    vr_quality += 1
            else:
                vr_signal = False

        # ── VK Accept: compute stop/target/quality for dedicated emission ──
        vka_signal = False
        vka_stop = NaN
        vka_target = NaN
        vka_quality = 0
        vka_fits = True  # VKA is trend-day setup (long continuation)
        if vka_long_bias and not _isnan(vka_stop_override):
            vka_signal = True
            vka_stop = vka_stop_override
            _vka_risk = bar.close - vka_stop
            if _vka_risk > 0:
                vka_target = bar.close + cfg.vka_target_rr * _vka_risk
                vka_quality = 4
                if long_fits_regime:
                    vka_quality += 1
                if mkt_bull:
                    vka_quality += 1
            else:
                vka_signal = False

        # ── RSI Midline Long Detection ──
        rsi_long_signal, rsi_long_stop, rsi_long_quality = False, NaN, 0
        if cfg.show_rsi_midline_long and self.rsi.ready and self.ema20.ready and self.vwap.ready and prev:
            _rsi_in_time = cfg.rsi_long_time_start <= hhmm <= cfg.rsi_long_time_end
            if _rsi_in_time and i_atr_ready:
                rsi_long_signal, rsi_long_stop, rsi_long_quality = self._detect_rsi_midline_long(
                    bar, prev, cfg, e20, vw, i_atr_safe, spy_snap)

        # ── RSI Bouncefail Short Detection ──
        rsi_short_signal, rsi_short_stop, rsi_short_quality = False, NaN, 0
        if cfg.show_rsi_bouncefail_short and self.rsi.ready and self.ema20.ready and self.vwap.ready and prev:
            _rsi_s_in_time = cfg.rsi_short_time_start <= hhmm <= cfg.rsi_short_time_end
            if _rsi_s_in_time and i_atr_ready:
                rsi_short_signal, rsi_short_stop, rsi_short_quality = self._detect_rsi_bouncefail_short(
                    bar, prev, cfg, e20, vw, i_atr_safe, spy_snap)

        # RSI regime fit: setup-native filters, no coarse regime by default
        rsi_long_fits = True if not cfg.rsi_require_regime else long_fits_regime
        rsi_short_fits = True if not cfg.rsi_require_regime else short_fits_regime

        # ── Emit breakout + short-struct + ema_scalp + vwap_reclaim + vka signals ──
        breakout_signals = []

        for (has_sig, direction, setup_id, stop_price, quality, fits_regime_val) in [
            (sc_long_signal, 1, SetupId.SECOND_CHANCE, sc_long_stop, sc_long_quality, sc_long_fits),
            (sc_short_signal and not cfg.sc_long_only, -1, SetupId.SECOND_CHANCE, sc_short_stop, sc_short_quality, sc_short_fits),
            (sp_long_signal, 1, SetupId.SPENCER, sp_long_stop, sp_long_quality, sp_long_fits),
            (sp_short_signal and not cfg.sp_long_only, -1, SetupId.SPENCER, sp_short_stop, sp_short_quality, sp_short_fits),
            (fb_signal, -1, SetupId.FAILED_BOUNCE, fb_stop, fb_quality, fb_fits),
            (bdr_signal, -1, SetupId.BDR_SHORT, bdr_stop, bdr_quality, bdr_fits),
            (es_long_signal, 1, es_long_id, es_long_stop, es_long_quality, es_long_fits),
            (es_short_signal, -1, es_short_id, es_short_stop, es_short_quality, es_short_fits),
            (fpip_long_signal, 1, SetupId.EMA_FPIP, fpip_long_stop, fpip_long_quality, fpip_long_fits),
            (fpip_short_signal, -1, SetupId.EMA_FPIP, fpip_short_stop, fpip_short_quality, fpip_short_fits),
            (sc2_long_signal, 1, SetupId.SC_V2, sc2_long_stop, sc2_long_quality, sc2_long_fits),
            (sc2_short_signal, -1, SetupId.SC_V2, sc2_short_stop, sc2_short_quality, sc2_short_fits),
            (vr_signal, 1, SetupId.VWAP_RECLAIM, vr_stop, vr_quality, vr_fits),
            (vka_signal, 1, SetupId.VKA, vka_stop, vka_quality, vka_fits),
            (rsi_long_signal, 1, SetupId.RSI_MIDLINE_LONG, rsi_long_stop, rsi_long_quality, rsi_long_fits),
            (rsi_short_signal, -1, SetupId.RSI_BOUNCEFAIL_SHORT, rsi_short_stop, rsi_short_quality, rsi_short_fits),
        ]:
            if not has_sig:
                continue
            # SECOND_CHANCE: hostile tape hard block + quality bonuses
            if setup_id == SetupId.SECOND_CHANCE:
                # Hard block only when BOTH market and sector oppose
                if direction == 1 and sc_ctx_blocked_long:
                    continue
                if direction == -1 and sc_ctx_blocked_short:
                    continue
                # Pre-bonus floor: base quality must meet minimum BEFORE bonus
                # Context can refine/rank valid signals, NOT rescue weak ones
                if quality < cfg.sc_pre_bonus_min_quality:
                    continue
                # Quality bonuses from aligned market + sector (additive, capped)
                bonus = sc_quality_bonus_long if direction == 1 else sc_quality_bonus_short
                quality += min(bonus, cfg.sc_max_quality_bonus)

            # SC_V2: same hostile tape block, capped context bonus
            if setup_id == SetupId.SC_V2:
                if direction == 1 and sc_ctx_blocked_long:
                    continue
                if direction == -1 and sc_ctx_blocked_short:
                    continue
                bonus = sc_quality_bonus_long if direction == 1 else sc_quality_bonus_short
                quality += min(bonus, cfg.sc2_context_bonus_cap)

            # Use setup-specific quality gates
            if setup_id == SetupId.FAILED_BOUNCE:
                q_gate = cfg.fb_min_quality
            elif setup_id == SetupId.BDR_SHORT:
                q_gate = cfg.bdr_min_quality
            elif setup_id == SetupId.SECOND_CHANCE:
                q_gate = cfg.sc_min_quality
            elif setup_id in (SetupId.EMA_RECLAIM, SetupId.EMA_CONFIRM):
                q_gate = cfg.ema_scalp_min_quality
            elif setup_id == SetupId.EMA_FPIP:
                q_gate = cfg.ema_fpip_min_quality
            elif setup_id == SetupId.SC_V2:
                q_gate = cfg.sc2_min_quality
            elif setup_id in (SetupId.RSI_MIDLINE_LONG, SetupId.RSI_BOUNCEFAIL_SHORT):
                q_gate = 0  # RSI setups are self-gated by alignment filters
            else:
                q_gate = cfg.min_quality
            if quality < q_gate:
                continue

            # Per-setup regime gating (BDR, SC, RSI use own flags; others use global)
            if setup_id == SetupId.BDR_SHORT:
                _regime_req = cfg.bdr_require_regime
            elif setup_id == SetupId.SECOND_CHANCE:
                _regime_req = cfg.sc_require_regime
            elif setup_id in (SetupId.RSI_MIDLINE_LONG, SetupId.RSI_BOUNCEFAIL_SHORT):
                _regime_req = cfg.rsi_require_regime
            else:
                _regime_req = cfg.require_regime
            if _regime_req and not fits_regime_val:
                continue

            family = SETUP_FAMILY_MAP.get(setup_id, SetupFamily.BREAKOUT)
            risk = abs(bar.close - stop_price)
            if risk <= 0:
                continue
            # VWAP_RECLAIM and VKA use static target (R:R based); others use trail/time exit.
            if setup_id == SetupId.VWAP_RECLAIM and not _isnan(vr_target):
                target = vr_target
                reward = target - bar.close if direction == 1 else bar.close - target
                rr = reward / risk if risk > 0 else 0.0
            elif setup_id == SetupId.VKA and not _isnan(vka_target):
                target = vka_target
                reward = target - bar.close if direction == 1 else bar.close - target
                rr = reward / risk if risk > 0 else 0.0
            elif setup_id in (SetupId.RSI_MIDLINE_LONG, SetupId.RSI_BOUNCEFAIL_SHORT):
                # RSI setups use fixed R:R target (1.5R)
                target = bar.close + direction * cfg.rsi_target_r * risk
                reward = abs(target - bar.close)
                rr = reward / risk if risk > 0 else 0.0
            else:
                # Breakout setups do NOT use static-target RR gating.
                # Exit is trail/time-based; a fake OR-range target would suppress valid trades.
                # Set 1R reference target for logging only (not used for exit or gating).
                target = bar.close + direction * risk   # 1R reference
                reward = risk                           # placeholder — exit engine decides real P&L
                rr = 1.0                                # neutral — no RR gate
            # Risk floor check only (no upper cap — position sizing handles it)
            # EMA Scalp and SHORT_STRUCT use micro-structure stops; skip the wide floor
            if family not in (SetupFamily.EMA_SCALP, SetupFamily.SHORT_STRUCT):
                if i_atr_ready and risk < min_stop_dist:
                    continue
            if family == SetupFamily.SHORT_STRUCT:
                cd_attr = "cd_short_struct"
            elif family == SetupFamily.EMA_SCALP:
                cd_attr = f"cd_{'long' if direction == 1 else 'short'}_ema_scalp"
            elif family == SetupFamily.TREND:
                cd_attr = f"cd_{'long' if direction == 1 else 'short'}_trend"
            else:
                cd_attr = f"cd_{'long' if direction == 1 else 'short'}_breakout"
            cd_val = getattr(ds, cd_attr, 999)
            if cd_val < cfg.alert_cooldown_bars:
                setattr(ds, cd_attr, 1)
                continue
            setattr(ds, cd_attr, 0)

            sig = self._build_signal(
                bar, direction, setup_id, family, bar.close, stop_price, target,
                risk, reward, quality, fits_regime_val, vw,
                or_up, or_down, near_pdh, near_pdl, near_orh, near_orl, near_vwap, near_ema20,
                sweep_pdh, sweep_pdl, sweep_local_high, sweep_local_low)
            # Attach market context data to signal
            sig.market_trend = int(mkt_trend)
            sig.rs_market = rs_mkt
            sig.rs_sector = rs_sec
            sig.tradability_long = trad_score.long_score
            sig.tradability_short = trad_score.short_score
            breakout_signals.append(sig)

        signals.extend(breakout_signals)

        # ── Update line persistence for breakout signals ──
        for bsig in breakout_signals:
            ds.pos_dir = bsig.direction
            ds.stop_line = bsig.stop_price
            ds.target_line = bsig.target_price
            ds.setup_active = True

        # Attach computed values to bar for next-bar lookback
        bar._e9 = e9
        bar._e20 = e20
        bar._vwap = vw
        bar._pb_into_zone_long = pb_into_zone_long
        bar._pb_into_zone_short = pb_into_zone_short

        self._bars.append(bar)
        self._recent_bars.append(bar)
        self._range_buf.append(bar.high - bar.low)
        self._bar_index += 1

        # ── Optional tradability gate (when use_tradability_gate=True) ──
        if cfg.use_tradability_gate:
            gated_signals = []
            for sig in signals:
                if sig.direction == 1:
                    if trad_score.long_score >= cfg.tradability_long_threshold:
                        gated_signals.append(sig)
                else:  # short
                    if trad_score.short_score <= -cfg.tradability_short_threshold:
                        gated_signals.append(sig)
            signals = gated_signals

        return signals

    # ═════════════════════════════════════════════
    # PRIVATE HELPERS
    # ═════════════════════════════════════════════

    def _on_new_day(self, bar: Bar):
        """Reset per-day state."""
        # Save current day as prior day
        if not _isnan(self._cur_day_high):
            self.prev_day_high = self._cur_day_high
            self.prev_day_low = self._cur_day_low
            # Feed daily ATR
            # (we need close — use last bar's close)
            if self._bars:
                last_close = self._bars[-1].close
                tr = self._daily_tr.update(self._cur_day_high, self._cur_day_low, last_close)
                self._daily_atr.update(tr)
            # Store 5-day highs/lows for breakout context
            self._daily_highs.append(self._cur_day_high)
            self._daily_lows.append(self._cur_day_low)

        # Roll RVOL: move today's time-slot volumes into historical buffer
        for hhmm, vol in self._vol_by_time_today.items():
            if hhmm not in self._vol_by_time:
                self._vol_by_time[hhmm] = deque(maxlen=20)
            self._vol_by_time[hhmm].append(vol)
        self._vol_by_time_today = {}

        self._cur_day_high = NaN
        self._cur_day_low = NaN
        self._current_date = bar.date

        # Reset day state
        self.day = DayState()

        # Reset VWAP
        self.vwap.reset()

        # Reset regime buffers
        self._trend_raw_buf.clear()
        self._hold_above_buf.clear()
        self._hold_below_buf.clear()

        # Reset intraday ATR aggregation
        self._intra_agg_bars.clear()
        self._intra_agg_count = 0

        # Reset extended bar history and range buffer for new day
        self._recent_bars.clear()
        self._range_buf.clear()

    def _update_intra_atr(self, bar: Bar):
        """Aggregate 2-min bars into 5-min bars for intraday ATR."""
        self._intra_agg_bars.append(bar)
        bars_per_agg = max(1, self.cfg.stop_atr_agg_minutes // self.cfg.bar_interval_minutes)
        if len(self._intra_agg_bars) >= bars_per_agg:
            agg_high = max(b.high for b in self._intra_agg_bars)
            agg_low = min(b.low for b in self._intra_agg_bars)
            agg_close = self._intra_agg_bars[-1].close
            tr = self._intra_tr.update(agg_high, agg_low, agg_close)
            current = self._intra_atr.update(tr)
            self._intra_atr_prev = current
            self._intra_agg_bars.clear()

    def _update_htf(self, bar: Bar):
        """Accumulate bars into HTF candles using timestamp-based rollover.

        Uses actual bar timestamps to detect when the HTF period boundary
        is crossed, rather than counting bars. This prevents time-drift
        if bars are missing (e.g., zero-volume gaps from IBKR).

        Buckets are offset by 30 minutes so hourly candles align with the
        09:30 market open (09:30-10:30, 10:30-11:30, etc.) rather than
        clock hours (09:00-10:00) which would create a short first candle.
        """
        htf_minutes = self.cfg.htf_agg_minutes

        # Offset by 30 min so buckets align to 09:30 market open
        bar_minutes = bar.timestamp.hour * 60 + bar.timestamp.minute - 30
        bar_bucket = bar_minutes // htf_minutes

        # Check if we've crossed into a new HTF period
        prev_bar = self._bars[-1] if self._bars else None
        rolled_over = False
        if prev_bar and prev_bar.timestamp:
            prev_minutes = prev_bar.timestamp.hour * 60 + prev_bar.timestamp.minute - 30
            prev_bucket = prev_minutes // htf_minutes
            prev_date = prev_bar.timestamp.date() if hasattr(prev_bar.timestamp, 'date') else None
            cur_date = bar.timestamp.date() if hasattr(bar.timestamp, 'date') else None
            rolled_over = (bar_bucket != prev_bucket) or (prev_date != cur_date)

        if rolled_over and self._htf_bars:
            # Finalize the previous HTF candle
            htf_close = self._htf_bars[-1].close
            self._htf_ema20_prev2 = self._htf_ema20_prev
            self._htf_ema20_prev = self._htf_ema20.value if self._htf_ema20.ready else NaN
            self._htf_ema20.update(htf_close)
            self._htf_close_prev = htf_close
            self._htf_bars.clear()

        self._htf_bars.append(bar)

    @staticmethod
    def _update_sweep_counter(current: int, sweep_now: bool, reclaim: bool) -> int:
        if sweep_now:
            return 0
        if reclaim:
            return 999
        if current < 999:
            return current + 1
        return 999

    @staticmethod
    def _resolve_primary_id(box_bias, manip_bias, sep_bias, ema_sep,
                            kiss_bias, pullback_entry, retest,
                            signal_a, signal_b, signal_c,
                            ema9_sep_high_pri, mcs_bias=False) -> SetupId:
        if box_bias and signal_a:
            return SetupId.BOX_REV
        if manip_bias and signal_a:
            return SetupId.MANIP
        if sep_bias and signal_a:
            return SetupId.VWAP_SEP
        if ema9_sep_high_pri and ema_sep and signal_c:
            return SetupId.EMA9_SEP
        if kiss_bias and signal_b:
            return SetupId.VWAP_KISS
        if mcs_bias and signal_b:
            return SetupId.MCS
        if pullback_entry and signal_b:
            return SetupId.EMA_PULL
        if retest and signal_b:
            return SetupId.EMA_RETEST
        if not ema9_sep_high_pri and ema_sep and signal_c:
            return SetupId.EMA9_SEP
        return SetupId.NONE

    @staticmethod
    def _fits_regime(after_late_grace, regime_known, is_trend_day, is_rotation_day,
                     is_trend_id, is_revert_id, is_ema_id) -> bool:
        if after_late_grace:
            return True
        if not regime_known:
            return True
        if is_trend_day and is_trend_id:
            return True
        if is_rotation_day and is_revert_id:
            return True
        if is_ema_id:
            return True
        return False

    @staticmethod
    def _compute_target(setup_id: SetupId, vwap: float, ema9: float,
                        box_mid: float, or_range: float, close: float,
                        direction: int, cfg=None, d_atr: float = 0.0) -> float:
        if setup_id == SetupId.VWAP_SEP:
            return vwap
        if setup_id == SetupId.EMA9_SEP:
            return ema9
        if setup_id == SetupId.BOX_REV:
            return box_mid
        if setup_id == SetupId.MANIP:
            return vwap
        if setup_id == SetupId.VWAP_KISS:
            if cfg is not None and getattr(cfg, 'vk_target_mode', 'or_range') == 'atr' and d_atr > 0:
                return close + direction * cfg.vk_target_atr_mult * d_atr
            return close + direction * or_range
        if setup_id in (SetupId.EMA_PULL, SetupId.EMA_RETEST):
            return close + direction * or_range
        if setup_id == SetupId.MCS:
            if cfg is not None and d_atr > 0:
                mode = getattr(cfg, 'mcs_target_mode', 'atr')
                if mode == 'atr':
                    return close + direction * getattr(cfg, 'mcs_target_atr_mult', 0.40) * d_atr
            return close + direction * or_range  # fallback
        return NaN

    @staticmethod
    def _quality_score(cfg, htf_aligned, hard_confluence, hard_conf_count,
                       exceptional_vol, fits_regime, rr_ok, risk, reward,
                       is_trend_id, is_revert_id, is_ema_id) -> int:
        score = 0
        # HTF bonus (only when filter is off but alignment present)
        if not cfg.use_htf_filter and htf_aligned:
            score += 1
        # Confluence bonus
        if is_trend_id and hard_confluence:
            score += 1
        elif is_revert_id and hard_conf_count >= 2:
            score += 1
        elif is_ema_id and hard_confluence:
            score += 1
        # Volume bonus
        if exceptional_vol:
            score += 1
        # Regime bonus
        if fits_regime:
            score += 1
        # R:R bonus
        if rr_ok and risk > 0 and (reward / risk) >= cfg.min_rr + 0.5:
            score += 1
        return score

    def _get_cooldown(self, ds: DayState, direction: int, family: SetupFamily) -> int:
        if family == SetupFamily.SHORT_STRUCT:
            return ds.cd_short_struct
        if family == SetupFamily.EMA_SCALP:
            return ds.cd_long_ema_scalp if direction == 1 else ds.cd_short_ema_scalp
        if direction == 1:
            if family == SetupFamily.REVERSAL:
                return ds.cd_long_rev
            elif family == SetupFamily.TREND:
                return ds.cd_long_trend
            elif family == SetupFamily.BREAKOUT:
                return ds.cd_long_breakout
            else:
                return ds.cd_long_ema
        else:
            if family == SetupFamily.REVERSAL:
                return ds.cd_short_rev
            elif family == SetupFamily.TREND:
                return ds.cd_short_trend
            elif family == SetupFamily.BREAKOUT:
                return ds.cd_short_breakout
            else:
                return ds.cd_short_ema

    def _reset_cooldown(self, ds: DayState, direction: int, family: SetupFamily):
        if family == SetupFamily.SHORT_STRUCT:
            ds.cd_short_struct = 0
            return
        if family == SetupFamily.EMA_SCALP:
            if direction == 1:
                ds.cd_long_ema_scalp = 0
            else:
                ds.cd_short_ema_scalp = 0
            return
        if direction == 1:
            if family == SetupFamily.REVERSAL:
                ds.cd_long_rev = 0
            elif family == SetupFamily.TREND:
                ds.cd_long_trend = 0
            elif family == SetupFamily.BREAKOUT:
                ds.cd_long_breakout = 0
            else:
                ds.cd_long_ema = 0
        else:
            if family == SetupFamily.REVERSAL:
                ds.cd_short_rev = 0
            elif family == SetupFamily.TREND:
                ds.cd_short_trend = 0
            elif family == SetupFamily.BREAKOUT:
                ds.cd_short_breakout = 0
            else:
                ds.cd_short_ema = 0

    def _tick_all_cooldowns(self, ds: DayState):
        """Increment all cooldown counters by 1 (except those just reset this bar)."""
        for attr in ['cd_long_rev', 'cd_long_trend', 'cd_long_ema',
                      'cd_short_rev', 'cd_short_trend', 'cd_short_ema',
                      'cd_long_breakout', 'cd_short_breakout',
                      'cd_short_struct',
                      'cd_long_ema_scalp', 'cd_short_ema_scalp']:
            val = getattr(ds, attr)
            if 0 < val < 999:  # 0 means just reset this bar, skip
                setattr(ds, attr, val + 1)
            elif val == 0:
                setattr(ds, attr, 1)  # advance from 0 to 1 for next bar

    # ═════════════════════════════════════════════
    # RSI SETUP DETECTION HELPERS
    # ═════════════════════════════════════════════

    def _detect_rsi_midline_long(self, bar: Bar, prev: Bar, cfg: 'OverlayConfig',
                                  e20: float, vw: float, i_atr: float,
                                  spy_snap) -> Tuple[bool, float, int]:
        """Detect RSI Midline Long setup. Returns (signal, stop, quality)."""
        rsi = self.rsi
        # Need enough RSI history: impulse_lookback PRIOR bars + 1 current bar
        if len(rsi.history) < cfg.rsi_impulse_lookback + 1:
            return False, NaN, 0

        # RSI state checks using STRICTLY PRIOR bars only.
        # rsi.history[-1] is the CURRENT bar (just updated); exclude it.
        hist = list(rsi.history)
        impulse_window = hist[-(cfg.rsi_impulse_lookback + 1):-1]   # prior 12 bars
        pullback_window = hist[-(cfg.rsi_pullback_lookback + 1):-1]  # prior 6 bars

        impulse_max = max(impulse_window)
        pullback_min = min(pullback_window)
        integrity_min = min(impulse_window)  # same prior 12-bar window

        # Gate 1: Prior bullish impulse
        if impulse_max < cfg.rsi_long_impulse_min:
            return False, NaN, 0

        # Gate 2: Controlled reset — min RSI in [45, 55]
        if pullback_min < cfg.rsi_long_pullback_min_low or pullback_min > cfg.rsi_long_pullback_min_high:
            return False, NaN, 0

        # Gate 3: Range integrity — min RSI >= 40
        if integrity_min < cfg.rsi_long_integrity_min:
            return False, NaN, 0

        # Gate 4: RSI reclaim above 50 (cross detection)
        if not (rsi.prev_value <= cfg.rsi_long_reclaim_level and rsi.value > cfg.rsi_long_reclaim_level):
            return False, NaN, 0

        # Gate 5: Price confirmation — close > prior high
        if bar.close <= prev.high:
            return False, NaN, 0

        # Gate 6: VWAP alignment
        if cfg.rsi_require_vwap_align and bar.close <= vw:
            return False, NaN, 0

        # Gate 7: EMA20 alignment
        if cfg.rsi_require_ema20_align and bar.close <= e20:
            return False, NaN, 0

        # Gate 8: SPY alignment — spy_close > spy_vwap AND spy_close > spy_ema20
        if cfg.rsi_require_spy_align:
            if spy_snap is None or not spy_snap.ready:
                return False, NaN, 0
            if not spy_snap.above_vwap or not spy_snap.above_ema20:
                return False, NaN, 0

        # Stop: min(signal_bar_low, recent_pullback_low) - buffer
        # Recent pullback low: lowest low in pullback window (prior 6 bars)
        recent_bars = list(self._bars)[-cfg.rsi_pullback_lookback:]
        recent_low = min(b.low for b in recent_bars) if recent_bars else bar.low
        stop_anchor = min(bar.low, recent_low)
        stop_price = stop_anchor - cfg.rsi_stop_buffer_atr * i_atr

        # Quality: base 5 + simple bonuses
        quality = 5
        if impulse_max >= cfg.rsi_long_impulse_min + 5:
            quality += 1
        if bar.close > vw and bar.close > e20 and (bar.close - max(vw, e20)) > 0.1 * i_atr:
            quality += 1

        return True, stop_price, quality

    def _detect_rsi_bouncefail_short(self, bar: Bar, prev: Bar, cfg: 'OverlayConfig',
                                      e20: float, vw: float, i_atr: float,
                                      spy_snap) -> Tuple[bool, float, int]:
        """Detect RSI Bouncefail Short setup. Returns (signal, stop, quality)."""
        rsi = self.rsi
        # Need enough RSI history: impulse_lookback PRIOR bars + 1 current bar
        if len(rsi.history) < cfg.rsi_impulse_lookback + 1:
            return False, NaN, 0

        # RSI state checks using STRICTLY PRIOR bars only.
        # rsi.history[-1] is the CURRENT bar (just updated); exclude it.
        hist = list(rsi.history)
        impulse_window = hist[-(cfg.rsi_impulse_lookback + 1):-1]   # prior 12 bars
        bounce_window = hist[-(cfg.rsi_pullback_lookback + 1):-1]    # prior 6 bars

        impulse_min = min(impulse_window)
        bounce_max = max(bounce_window)
        integrity_max = max(impulse_window)  # same prior 12-bar window

        # Gate 1: Prior bearish impulse
        if impulse_min > cfg.rsi_short_impulse_max:
            return False, NaN, 0

        # Gate 2: Weak bounce — max RSI in [45, 55]
        if bounce_max < cfg.rsi_short_bounce_max_low or bounce_max > cfg.rsi_short_bounce_max_high:
            return False, NaN, 0

        # Gate 3: Range integrity — max RSI <= 60
        if integrity_max > cfg.rsi_short_integrity_max:
            return False, NaN, 0

        # Gate 4: RSI rollover below 45 (cross detection)
        if not (rsi.prev_value >= cfg.rsi_short_rollover_level and rsi.value < cfg.rsi_short_rollover_level):
            return False, NaN, 0

        # Gate 5: Price confirmation — close < prior low
        if bar.close >= prev.low:
            return False, NaN, 0

        # Gate 6: VWAP alignment
        if cfg.rsi_require_vwap_align and bar.close >= vw:
            return False, NaN, 0

        # Gate 7: EMA20 alignment
        if cfg.rsi_require_ema20_align and bar.close >= e20:
            return False, NaN, 0

        # Gate 8: SPY alignment — spy_close < spy_vwap AND spy_close < spy_ema20
        if cfg.rsi_require_spy_align:
            if spy_snap is None or not spy_snap.ready:
                return False, NaN, 0
            if spy_snap.above_vwap or spy_snap.above_ema20:
                return False, NaN, 0

        # Stop: max(signal_bar_high, recent_bounce_high) + buffer
        recent_bars = list(self._bars)[-cfg.rsi_pullback_lookback:]
        recent_high = max(b.high for b in recent_bars) if recent_bars else bar.high
        stop_anchor = max(bar.high, recent_high)
        stop_price = stop_anchor + cfg.rsi_stop_buffer_atr * i_atr

        # Quality: base 5 + simple bonuses
        quality = 5
        if impulse_min <= cfg.rsi_short_impulse_max - 5:
            quality += 1
        if bar.close < vw and bar.close < e20 and (min(vw, e20) - bar.close) > 0.1 * i_atr:
            quality += 1

        return True, stop_price, quality

    def _build_signal(self, bar, direction, setup_id, family, entry, stop, target,
                      risk, reward, quality, fits_regime, vwap_val,
                      or_up, or_down,
                      near_pdh, near_pdl, near_orh, near_orl, near_vwap, near_ema20,
                      sweep_pdh, sweep_pdl, sweep_local_high, sweep_local_low) -> Signal:
        rr = reward / risk if risk > 0 else 0.0
        vwap_bias = "ABV" if bar.close > vwap_val else "BLW"
        or_dir = "UP" if or_up else ("DN" if or_down else "FLAT")

        conf_tags = []
        if near_pdh: conf_tags.append("PDH")
        if near_pdl: conf_tags.append("PDL")
        if near_orh: conf_tags.append("ORH")
        if near_orl: conf_tags.append("ORL")
        if near_vwap: conf_tags.append("VWAP")
        if near_ema20: conf_tags.append("EMA20")

        sweep_tags = []
        if sweep_pdh: sweep_tags.append("PDH")
        if sweep_pdl: sweep_tags.append("PDL")
        if sweep_local_high or sweep_local_low: sweep_tags.append("LOCAL")

        return Signal(
            bar_index=self._bar_index,
            timestamp=bar.timestamp,
            direction=direction,
            setup_id=setup_id,
            family=family,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            risk=risk,
            reward=reward,
            rr_ratio=rr,
            quality_score=quality,
            fits_regime=fits_regime,
            vwap_bias=vwap_bias,
            or_direction=or_dir,
            confluence_tags=conf_tags,
            sweep_tags=sweep_tags,
            universe=self._universe_source,
        )

    # ═════════════════════════════════════════════
    # SECOND CHANCE DETECTION
    # ═════════════════════════════════════════════

    def _detect_second_chance(self, bar: Bar, ds: DayState, cfg,
                               direction: int, e9: float, vw: float,
                               vol_ma: float, i_atr: float, d_atr: float,
                               in_time: bool, htf_aligned: bool
                               ) -> Tuple[bool, float, int]:
        """
        Detect Second Chance breakout-retest-confirm setup.
        Returns (signal_fired, stop_price, quality_score).
        """
        if not in_time or _isnan(vol_ma) or vol_ma <= 0 or i_atr <= 0:
            self._sc_tick_state(ds, direction, bar)
            return False, NaN, 0

        # Shortcuts for long/short state attrs
        pfx = "sc_long_" if direction == 1 else "sc_short_"
        get = lambda attr: getattr(ds, pfx + attr)
        put = lambda attr, val: setattr(ds, pfx + attr, val)

        rng = bar.high - bar.low
        bar_bullish = bar.close > bar.open
        bar_bearish = bar.close < bar.open
        is_bull = direction == 1

        # ── Step 1: Detect new breakout ──
        if not get("active"):
            key_level, level_tag = self._sc_find_key_level(ds, direction, bar)
            if _isnan(key_level):
                return False, NaN, 0

            # Break condition checks
            if is_bull:
                broke = bar.close > key_level + cfg.sc_break_atr_min * i_atr
            else:
                broke = bar.close < key_level - cfg.sc_break_atr_min * i_atr

            if not broke:
                return False, NaN, 0

            # Bar range check vs median-10
            if len(self._range_buf) >= 5:
                med_range = median(list(self._range_buf))
                if rng < cfg.sc_break_bar_range_frac * med_range:
                    return False, NaN, 0

            # Volume check
            if bar.volume < cfg.sc_break_vol_frac * vol_ma:
                return False, NaN, 0

            # Strong breakout volume gate (from feature study)
            if cfg.sc_require_strong_bo_vol:
                if bar.volume < cfg.sc_strong_bo_vol_mult * vol_ma:
                    return False, NaN, 0

            # Close position in bar
            if rng > 0:
                close_pct = (bar.close - bar.low) / rng if is_bull else (bar.high - bar.close) / rng
                if close_pct < cfg.sc_break_close_pct:
                    return False, NaN, 0

            # Latch breakout
            put("active", True)
            put("level", key_level)
            put("bo_high", bar.high)
            put("bo_low", bar.low)
            put("bo_vol", bar.volume)
            put("bars_since_bo", 0)
            put("retested", False)
            put("bars_since_retest", 999)
            put("level_tag", level_tag)
            if is_bull:
                put("retest_bar_low", NaN)
                put("retest_bar_high", NaN)
                put("lowest_since_retest", NaN)
            else:
                put("retest_bar_high", NaN)
                put("retest_bar_low", NaN)
                put("highest_since_retest", NaN)
            return False, NaN, 0  # breakout bar itself is not the entry

        # ── Active breakout — tick counter ──
        bars_since = get("bars_since_bo") + 1
        put("bars_since_bo", bars_since)
        level = get("level")

        # Expire if too many bars
        if bars_since > cfg.sc_retest_window + cfg.sc_confirm_window + 1:
            put("active", False)
            return False, NaN, 0

        # ── Step 2: Detect retest ──
        if not get("retested") and bars_since <= cfg.sc_retest_window:
            proximity = cfg.sc_retest_proximity_atr * i_atr
            max_depth = cfg.sc_retest_max_depth_atr * i_atr

            if is_bull:
                touches_level = bar.low <= level + proximity
                holds_level = bar.close > level
                not_too_deep = bar.low >= level - max_depth
                not_bearish_expansion = not (bar_bearish and rng > 1.5 * i_atr)
                vol_ok = bar.volume <= cfg.sc_retest_max_vol_frac * vol_ma
            else:
                touches_level = bar.high >= level - proximity
                holds_level = bar.close < level
                not_too_deep = bar.high <= level + max_depth
                not_bearish_expansion = not (bar_bullish and rng > 1.5 * i_atr)
                vol_ok = bar.volume <= cfg.sc_retest_max_vol_frac * vol_ma

            if touches_level and holds_level and not_too_deep and not_bearish_expansion and vol_ok:
                # Shallow reset gate (from feature study)
                if cfg.sc_require_shallow_reset:
                    bo_high = get("bo_high")
                    bo_low = get("bo_low")
                    impulse = bo_high - bo_low
                    if impulse > 0:
                        if is_bull:
                            depth = bo_high - bar.low
                        else:
                            depth = bar.high - bo_low
                        if depth / impulse > cfg.sc_max_reset_depth_pct:
                            put("active", False)
                            return False, NaN, 0

                put("retested", True)
                put("retest_bar_high", bar.high)
                put("retest_bar_low", bar.low)
                put("bars_since_retest", 0)
                if is_bull:
                    put("lowest_since_retest", bar.low)
                else:
                    put("highest_since_retest", bar.high)
                return False, NaN, 0

        # ── Step 3: Confirmation after retest ──
        if get("retested"):
            retest_bars = get("bars_since_retest") + 1
            put("bars_since_retest", retest_bars)

            # Track extremes since retest
            if is_bull:
                lowest = get("lowest_since_retest")
                if _isnan(lowest) or bar.low < lowest:
                    put("lowest_since_retest", bar.low)
            else:
                highest = get("highest_since_retest")
                if _isnan(highest) or bar.high > highest:
                    put("highest_since_retest", bar.high)

            if retest_bars > cfg.sc_confirm_window:
                put("active", False)
                return False, NaN, 0

            # Confirm conditions
            retest_high = get("retest_bar_high")
            retest_low = get("retest_bar_low")

            if is_bull:
                confirmed = (bar.close > retest_high and bar_bullish and
                             bar.volume >= cfg.sc_confirm_vol_frac * vol_ma and
                             bar.close > vw and bar.close > e9)
            else:
                confirmed = (bar.close < retest_low and bar_bearish and
                             bar.volume >= cfg.sc_confirm_vol_frac * vol_ma and
                             bar.close < vw and bar.close < e9)

            if confirmed:
                # Compute stop
                if is_bull:
                    lowest = get("lowest_since_retest")
                    raw_stop = min(retest_low, lowest if not _isnan(lowest) else retest_low)
                    stop = raw_stop - cfg.sc_stop_buffer
                else:
                    highest = get("highest_since_retest")
                    raw_stop = max(retest_high, highest if not _isnan(highest) else retest_high)
                    stop = raw_stop + cfg.sc_stop_buffer

                # Floor check
                min_stop = cfg.min_stop_intra_atr_mult * i_atr
                if abs(bar.close - stop) < min_stop:
                    stop = bar.close - direction * min_stop

                # Quality scoring
                quality = self._sc_quality(
                    cfg, bar, ds, direction, vol_ma, vw, e9,
                    get("bo_vol"), get("level_tag"), htf_aligned)

                put("active", False)
                return True, stop, quality

        return False, NaN, 0

    def _sc_tick_state(self, ds: DayState, direction: int, bar: Bar):
        """Tick Second Chance state counters even when not in time window."""
        pfx = "sc_long_" if direction == 1 else "sc_short_"
        if getattr(ds, pfx + "active"):
            bars = getattr(ds, pfx + "bars_since_bo") + 1
            setattr(ds, pfx + "bars_since_bo", bars)
            if bars > 12:  # hard expire
                setattr(ds, pfx + "active", False)

    def _sc_find_key_level(self, ds: DayState, direction: int,
                           bar: Bar) -> Tuple[float, str]:
        """Find the key level for Second Chance breakout.
        Uses levels from PRIOR bars only (excluding current bar).
        """
        is_bull = direction == 1
        candidates = []

        # OR high/low — these are fixed after OR completes
        if ds.or_ready:
            if is_bull and not _isnan(ds.or_high):
                candidates.append((ds.or_high, "ORH"))
            if not is_bull and not _isnan(ds.or_low):
                candidates.append((ds.or_low, "ORL"))

        # Prior bar swing high/low from _recent_bars (excludes current bar)
        # _recent_bars is appended AFTER detection runs, so it contains bars before current
        prior_bars = list(self._recent_bars)
        if len(prior_bars) >= 5:
            if is_bull:
                swing_high = max(b.high for b in prior_bars)
                # Only use swing high if it's NOT at the current bar's price
                # (i.e., it was established by a prior bar)
                candidates.append((swing_high, "SWING"))
            else:
                swing_low = min(b.low for b in prior_bars)
                candidates.append((swing_low, "SWING"))

        if not candidates:
            return NaN, ""

        # For longs, prefer OR level if close enough to swing, otherwise take highest
        # For shorts, prefer OR level if close enough, otherwise take lowest
        if is_bull:
            candidates.sort(key=lambda x: x[0], reverse=True)
        else:
            candidates.sort(key=lambda x: x[0])

        return candidates[0]

    @staticmethod
    def _sc_quality(cfg, bar, ds, direction, vol_ma, vw, e9,
                    bo_vol, level_tag, htf_aligned) -> int:
        """Quality score for Second Chance setup."""
        score = 0
        # Breakout through OR/PDH/PDL
        if level_tag in ("ORH", "ORL"):
            score += 1
        # Breakout volume strong
        if bo_vol >= 1.25 * vol_ma:
            score += 1
        # Retest volume lower than breakout (implied by retest vol filter)
        if bar.volume < bo_vol:
            score += 1
        # Confirmation above VWAP and EMA9
        if direction == 1 and bar.close > vw and bar.close > e9:
            score += 1
        elif direction == -1 and bar.close < vw and bar.close < e9:
            score += 1
        # HTF alignment
        if htf_aligned:
            score += 1
        return score

    # ═════════════════════════════════════════════
    # SECOND CHANCE V2 DETECTION
    # ═════════════════════════════════════════════

    def _update_sc2_state(self, bar, ds, cfg, e9, e20, vw, i_atr,
                           vol_ma, rvol_tod):
        """Update SC_V2 expansion and reset tracking for both sides."""
        self._update_sc2_side(bar, ds, cfg, 1, e9, e20, i_atr, vol_ma)
        self._update_sc2_side(bar, ds, cfg, -1, e9, e20, i_atr, vol_ma)

    def _update_sc2_side(self, bar, ds, cfg, direction, e9, e20, i_atr, vol_ma):
        """Track expansion legs and reset phases for SC_V2 on one side."""
        is_bull = direction == 1
        pre = "sc2_long_" if is_bull else "sc2_short_"

        exp_active = getattr(ds, pre + "expansion_active")
        exp_bars = getattr(ds, pre + "expansion_bars")
        qual_exp = getattr(ds, pre + "qual_expansion_exists")
        reset_active = getattr(ds, pre + "reset_active")
        reset_bars = getattr(ds, pre + "reset_bars")
        reattack_used = getattr(ds, pre + "reattack_used")

        bar_range = bar.high - bar.low
        bar_bullish = bar.close > bar.open
        bar_bearish = bar.close < bar.open

        # ── Find key level for expansion ──
        level, level_tag = self._sc_find_key_level(ds, direction, bar)

        # ── Phase 1: Track expansion leg ──
        if not qual_exp and not reset_active and not reattack_used:
            if not exp_active:
                # Check for expansion start: directional bar breaking a key level
                if _isnan(level) or i_atr <= 0:
                    return
                min_penetration = cfg.sc2_break_level_atr_min * i_atr
                if is_bull:
                    starts = (bar_bullish and bar.close > level + min_penetration
                              and bar.volume >= vol_ma)
                else:
                    starts = (bar_bearish and bar.close < level - min_penetration
                              and bar.volume >= vol_ma)
                if starts:
                    setattr(ds, pre + "expansion_active", True)
                    setattr(ds, pre + "expansion_bars", 1)
                    setattr(ds, pre + "expansion_high", bar.high)
                    setattr(ds, pre + "expansion_low", bar.low)
                    setattr(ds, pre + "expansion_total_vol", bar.volume)
                    setattr(ds, pre + "expansion_overlap_bars", 0)
                    setattr(ds, pre + "expansion_prev_close", bar.close)
                    setattr(ds, pre + "expansion_level", level)
                    setattr(ds, pre + "expansion_level_tag", level_tag)
            else:
                # Continue expansion
                exp_bars += 1
                setattr(ds, pre + "expansion_bars", exp_bars)

                # Overlap detection
                prev_close = getattr(ds, pre + "expansion_prev_close")
                if not _isnan(prev_close):
                    if is_bull:
                        if bar.low < prev_close:
                            overlap = getattr(ds, pre + "expansion_overlap_bars")
                            setattr(ds, pre + "expansion_overlap_bars", overlap + 1)
                    else:
                        if bar.high > prev_close:
                            overlap = getattr(ds, pre + "expansion_overlap_bars")
                            setattr(ds, pre + "expansion_overlap_bars", overlap + 1)
                setattr(ds, pre + "expansion_prev_close", bar.close)

                # Update extremes
                exp_high = getattr(ds, pre + "expansion_high")
                exp_low = getattr(ds, pre + "expansion_low")
                setattr(ds, pre + "expansion_high", max(exp_high, bar.high))
                setattr(ds, pre + "expansion_low", min(exp_low, bar.low))

                total_vol = getattr(ds, pre + "expansion_total_vol") + bar.volume
                setattr(ds, pre + "expansion_total_vol", total_vol)

                # Check if expansion has expired (too many bars)
                if exp_bars > cfg.sc2_expansion_max_bars:
                    setattr(ds, pre + "expansion_active", False)
                    return

                # Check for expansion trend break → possibly qualify and end
                if is_bull:
                    trend_broken = bar.close < e9
                else:
                    trend_broken = bar.close > e9

                # Check if expansion qualifies
                exp_h = getattr(ds, pre + "expansion_high")
                exp_l = getattr(ds, pre + "expansion_low")
                distance = (exp_h - exp_l) if is_bull else (exp_h - exp_l)
                overlap_bars = getattr(ds, pre + "expansion_overlap_bars")
                overlap_ratio = overlap_bars / exp_bars if exp_bars > 0 else 1.0
                avg_vol = total_vol / exp_bars if exp_bars > 0 else 0.0

                qualifies = (
                    exp_bars >= cfg.sc2_expansion_min_bars
                    and i_atr > 0
                    and distance >= cfg.sc2_expansion_min_atr * i_atr
                    and overlap_ratio <= cfg.sc2_max_initial_overlap_ratio
                    and avg_vol >= cfg.sc2_expansion_min_vol_ma * vol_ma
                )

                if qualifies and (trend_broken or exp_bars >= cfg.sc2_expansion_max_bars):
                    # Expansion leg complete, qualified
                    setattr(ds, pre + "expansion_active", False)
                    setattr(ds, pre + "expansion_avg_vol", avg_vol)
                    setattr(ds, pre + "expansion_distance", distance)
                    setattr(ds, pre + "qual_expansion_exists", True)
                elif trend_broken:
                    # Trend broken but didn't qualify → kill
                    setattr(ds, pre + "expansion_active", False)

        # ── Phase 2: Track reset after qualified expansion ──
        if qual_exp and not reattack_used:
            exp_high = getattr(ds, pre + "expansion_high")
            exp_low = getattr(ds, pre + "expansion_low")
            exp_dist = getattr(ds, pre + "expansion_distance")
            exp_avg_vol = getattr(ds, pre + "expansion_avg_vol")

            if not reset_active:
                # Check for reset start: price pulling back toward level
                if is_bull:
                    reset_starts = bar.close < exp_high  # price retreating from highs
                else:
                    reset_starts = bar.close > exp_low
                if reset_starts:
                    setattr(ds, pre + "reset_active", True)
                    setattr(ds, pre + "reset_bars", 1)
                    if is_bull:
                        setattr(ds, pre + "reset_low", bar.low)
                        setattr(ds, pre + "reset_high", bar.high)
                    else:
                        setattr(ds, pre + "reset_high", bar.high)
                        setattr(ds, pre + "reset_low", bar.low)
                    setattr(ds, pre + "reset_total_vol", bar.volume)
                    setattr(ds, pre + "reset_total_range", bar_range)
                    heavy = 1 if bar.volume > exp_avg_vol else 0
                    setattr(ds, pre + "reset_heavy_bars", heavy)
            else:
                # Continue reset tracking
                reset_bars += 1
                setattr(ds, pre + "reset_bars", reset_bars)

                total_vol = getattr(ds, pre + "reset_total_vol") + bar.volume
                setattr(ds, pre + "reset_total_vol", total_vol)
                setattr(ds, pre + "reset_avg_vol", total_vol / reset_bars)
                total_range = getattr(ds, pre + "reset_total_range") + bar_range
                setattr(ds, pre + "reset_total_range", total_range)

                # Update extremes
                if is_bull:
                    cur_low = getattr(ds, pre + "reset_low")
                    setattr(ds, pre + "reset_low", min(cur_low, bar.low))
                    cur_high = getattr(ds, pre + "reset_high")
                    setattr(ds, pre + "reset_high", max(cur_high, bar.high))
                else:
                    cur_high = getattr(ds, pre + "reset_high")
                    setattr(ds, pre + "reset_high", max(cur_high, bar.high))
                    cur_low = getattr(ds, pre + "reset_low")
                    setattr(ds, pre + "reset_low", min(cur_low, bar.low))

                heavy = getattr(ds, pre + "reset_heavy_bars")
                if bar.volume > exp_avg_vol:
                    setattr(ds, pre + "reset_heavy_bars", heavy + 1)

                # Check reset depth
                if is_bull:
                    depth = exp_high - getattr(ds, pre + "reset_low")
                else:
                    depth = getattr(ds, pre + "reset_high") - exp_low

                # Check disqualifiers: too deep, too long, too heavy
                if exp_dist > 0 and depth / exp_dist > cfg.sc2_max_reset_depth_pct:
                    # Reset too deep → kill entire sequence
                    setattr(ds, pre + "reset_active", False)
                    setattr(ds, pre + "qual_expansion_exists", False)
                    return
                if reset_bars > cfg.sc2_max_reset_bars:
                    setattr(ds, pre + "reset_active", False)
                    setattr(ds, pre + "qual_expansion_exists", False)
                    return

    def _detect_sc2(self, bar, ds, cfg, direction, e9, e20, vw,
                     vol_ma, i_atr, d_atr, rvol_tod,
                     in_time, htf_aligned, rs_mkt, rs_sec):
        """Detect SC_V2 trigger. Returns (signal, stop, quality)."""
        if not in_time or i_atr <= 0 or _isnan(vol_ma) or vol_ma <= 0:
            return False, NaN, 0

        is_bull = direction == 1
        pre = "sc2_long_" if is_bull else "sc2_short_"

        # Must have qualified expansion + active reset + no prior reattack
        if not getattr(ds, pre + "qual_expansion_exists"):
            return False, NaN, 0
        if not getattr(ds, pre + "reset_active"):
            return False, NaN, 0
        if cfg.sc2_first_reattack_only and getattr(ds, pre + "reattack_used"):
            return False, NaN, 0

        exp_high = getattr(ds, pre + "expansion_high")
        exp_low = getattr(ds, pre + "expansion_low")
        exp_dist = getattr(ds, pre + "expansion_distance")
        exp_avg_vol = getattr(ds, pre + "expansion_avg_vol")
        exp_bars = getattr(ds, pre + "expansion_bars")
        exp_level = getattr(ds, pre + "expansion_level")
        exp_level_tag = getattr(ds, pre + "expansion_level_tag")
        reset_bars = getattr(ds, pre + "reset_bars")
        reset_low = getattr(ds, pre + "reset_low")
        reset_high = getattr(ds, pre + "reset_high")
        reset_total_vol = getattr(ds, pre + "reset_total_vol")
        reset_avg_vol = reset_total_vol / reset_bars if reset_bars > 0 else 0.0
        reset_heavy = getattr(ds, pre + "reset_heavy_bars")
        reset_total_range = getattr(ds, pre + "reset_total_range")

        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return False, NaN, 0
        bar_bullish = bar.close > bar.open
        bar_bearish = bar.close < bar.open

        # ── Hard disqualifier gates ──

        # Gate 1: Timing — max bars since expansion ended
        total_bars = exp_bars + reset_bars
        if total_bars > cfg.sc2_max_bars_since_expansion:
            return False, NaN, 0

        # Gate 2: Reset depth must be contained
        if is_bull:
            reset_depth = exp_high - reset_low
        else:
            reset_depth = reset_high - exp_low
        if exp_dist > 0 and reset_depth / exp_dist > cfg.sc2_max_reset_depth_pct:
            return False, NaN, 0

        # Gate 3: Reset not too long
        if reset_bars > cfg.sc2_max_reset_bars:
            return False, NaN, 0

        # Gate 4: Reset volume contraction
        if exp_avg_vol > 0 and reset_avg_vol / exp_avg_vol > cfg.sc2_max_reset_volume_ratio:
            return False, NaN, 0

        # Gate 5: Not too many heavy reset bars
        if reset_heavy > cfg.sc2_max_heavy_reset_bars:
            return False, NaN, 0

        # Gate 6: Trigger bar direction
        if is_bull and not bar_bullish:
            return False, NaN, 0
        if not is_bull and not bar_bearish:
            return False, NaN, 0

        # Gate 7: Trigger bar close position
        if is_bull:
            close_pct = (bar.close - bar.low) / bar_range
        else:
            close_pct = (bar.high - bar.close) / bar_range
        if close_pct < cfg.sc2_min_trigger_close_pct:
            return False, NaN, 0

        # Gate 8: Trigger bar body quality
        body = abs(bar.close - bar.open)
        body_pct = body / bar_range
        if body_pct < cfg.sc2_min_trigger_body_pct:
            return False, NaN, 0

        # Gate 9: Trigger volume re-expansion
        if reset_avg_vol > 0:
            if bar.volume / reset_avg_vol < cfg.sc2_trigger_volume_vs_reset_min:
                return False, NaN, 0

        # Gate 10: Trigger must re-attack above/below reset extreme
        if is_bull:
            if bar.close <= reset_high:
                return False, NaN, 0
        else:
            if bar.close >= reset_low:
                return False, NaN, 0

        # Gate 11: VWAP alignment
        if cfg.sc2_require_vwap_align:
            if is_bull and bar.close < vw:
                return False, NaN, 0
            if not is_bull and bar.close > vw:
                return False, NaN, 0

        # Gate 12: EMA9 alignment
        if cfg.sc2_require_ema9_align:
            if is_bull and bar.close < e9:
                return False, NaN, 0
            if not is_bull and bar.close > e9:
                return False, NaN, 0

        # Gate 13: Extension from VWAP
        if not _isnan(vw) and i_atr > 0:
            dist_vwap = abs(bar.close - vw) / i_atr
            if dist_vwap > cfg.sc2_max_dist_vwap_atr:
                return False, NaN, 0

        # Gate 14: Extension from EMA9
        if not _isnan(e9) and i_atr > 0:
            dist_ema9 = abs(bar.close - e9) / i_atr
            if dist_ema9 > cfg.sc2_max_dist_ema9_atr:
                return False, NaN, 0

        # Gate 15: Total extension from session open
        if not _isnan(ds.session_open) and i_atr > 0:
            total_ext = abs(bar.close - ds.session_open) / i_atr
            if total_ext > cfg.sc2_max_total_extension_atr:
                return False, NaN, 0

        # Gate 16: RS alignment (optional)
        if cfg.sc2_require_positive_rs_market and rs_mkt is not None:
            if is_bull and rs_mkt < 0:
                return False, NaN, 0
            if not is_bull and rs_mkt > 0:
                return False, NaN, 0

        if cfg.sc2_require_positive_rs_sector and rs_sec is not None:
            if is_bull and rs_sec < 0:
                return False, NaN, 0
            if not is_bull and rs_sec > 0:
                return False, NaN, 0

        # ── All gates passed → compute stop and quality ──
        stop_buffer = cfg.sc2_stop_buffer_atr * i_atr
        if is_bull:
            raw_stop = reset_low - stop_buffer
        else:
            raw_stop = reset_high + stop_buffer

        quality = self._sc2_quality(
            cfg, bar, direction, vol_ma, vw, e9,
            exp_avg_vol, exp_level_tag, reset_avg_vol, reset_depth,
            exp_dist, reset_bars, htf_aligned, rs_mkt, rs_sec)

        # Mark reattack used
        setattr(ds, pre + "reattack_used", True)

        return True, raw_stop, quality

    @staticmethod
    def _sc2_quality(cfg, bar, direction, vol_ma, vw, e9,
                      exp_avg_vol, level_tag, reset_avg_vol, reset_depth,
                      exp_dist, reset_bars, htf_aligned, rs_mkt, rs_sec):
        """Quality score for SC_V2. Max = 7."""
        score = 0
        is_bull = direction == 1

        # +1: Broke OR level (higher quality than swing level)
        if level_tag in ("ORH", "ORL"):
            score += 1

        # +1: Strong expansion volume (well above average)
        if exp_avg_vol >= 1.25 * vol_ma:
            score += 1

        # +1: Shallow reset (< 30% retrace)
        if exp_dist > 0 and reset_depth / exp_dist < 0.30:
            score += 1

        # +1: Light reset volume (< 60% of expansion avg)
        if exp_avg_vol > 0 and reset_avg_vol / exp_avg_vol < 0.60:
            score += 1

        # +1: Quick reset (2 bars or fewer)
        if reset_bars <= 2:
            score += 1

        # +1: Trigger volume strong (> 1.5× reset avg)
        if reset_avg_vol > 0 and bar.volume / reset_avg_vol > 1.5:
            score += 1

        # +1: HTF aligned
        if htf_aligned:
            score += 1

        return score

    # ═════════════════════════════════════════════
    # SPENCER DETECTION
    # ═════════════════════════════════════════════

    def _detect_spencer(self, bar: Bar, ds: DayState, cfg,
                         direction: int, e9: float, e20: float, vw: float,
                         vol_ma: float, i_atr: float, d_atr: float,
                         in_time: bool, htf_aligned: bool
                         ) -> Tuple[bool, float, int]:
        """
        Detect Spencer tight-consolidation breakout setup.
        Returns (signal_fired, stop_price, quality_score).
        """
        if not in_time or _isnan(vol_ma) or vol_ma <= 0 or i_atr <= 0:
            return False, NaN, 0

        is_bull = direction == 1
        recent = list(self._recent_bars)
        if len(recent) < cfg.sp_box_min_bars + 1:
            return False, NaN, 0

        # ── Precondition: trend context ──
        # EMA alignment
        if is_bull:
            if not (self.ema9.ready and self.ema20.ready and e9 > e20):
                return False, NaN, 0
            # EMA9 slope positive (compare to prev bar)
            if self._bars and e9 <= self._bars[-1]._e9:
                return False, NaN, 0
            # Price above VWAP
            if bar.close <= vw:
                return False, NaN, 0
        else:
            if not (self.ema9.ready and self.ema20.ready and e9 < e20):
                return False, NaN, 0
            if self._bars and e9 >= self._bars[-1]._e9:
                return False, NaN, 0
            if bar.close >= vw:
                return False, NaN, 0

        # Trend advance check
        if is_bull:
            if not _isnan(ds.session_low) and d_atr > 0:
                advance = bar.close - ds.session_low
                if advance < cfg.sp_trend_advance_atr * d_atr:
                    # Try VWAP alternative
                    vwap_advance = bar.close - vw
                    if vwap_advance < cfg.sp_trend_advance_vwap_atr * i_atr:
                        return False, NaN, 0
            # At least one 10-bar high in recent bars
            if len(recent) >= 10:
                ten_bar_high = max(b.high for b in recent[-10:])
                has_new_high = any(b.close >= ten_bar_high - 0.01 for b in recent[-10:])
                if not has_new_high:
                    return False, NaN, 0
        else:
            if not _isnan(ds.session_high) and d_atr > 0:
                decline = ds.session_high - bar.close
                if decline < cfg.sp_trend_advance_atr * d_atr:
                    vwap_decline = vw - bar.close
                    if vwap_decline < cfg.sp_trend_advance_vwap_atr * i_atr:
                        return False, NaN, 0
            if len(recent) >= 10:
                ten_bar_low = min(b.low for b in recent[-10:])
                has_new_low = any(b.close <= ten_bar_low + 0.01 for b in recent[-10:])
                if not has_new_low:
                    return False, NaN, 0

        # Extension filter: reject if too extended
        if not _isnan(ds.session_open) and d_atr > 0:
            if is_bull and (bar.close - ds.session_open) > cfg.sp_extension_atr * d_atr:
                return False, NaN, 0
            if not is_bull and (ds.session_open - bar.close) > cfg.sp_extension_atr * d_atr:
                return False, NaN, 0

        # ── Scan for consolidation box ──
        # Try windows from sp_box_min_bars to sp_box_max_bars
        # The box is the N bars BEFORE the current bar; current bar is the potential breakout
        best_box = None
        for window in range(cfg.sp_box_min_bars, min(cfg.sp_box_max_bars + 1, len(recent))):
            box_bars = recent[-(window + 1):-1]  # N bars before current
            if len(box_bars) < cfg.sp_box_min_bars:
                continue

            box_high = max(b.high for b in box_bars)
            box_low = min(b.low for b in box_bars)
            box_range = box_high - box_low

            # Range check
            if box_range > cfg.sp_box_max_range_atr * i_atr:
                continue
            if box_range <= 0:
                continue

            # Upper-half close check
            box_mid = (box_high + box_low) / 2
            if is_bull:
                upper_closes = sum(1 for b in box_bars if b.close >= box_mid)
            else:
                upper_closes = sum(1 for b in box_bars if b.close <= box_mid)  # lower half for shorts
            if upper_closes / len(box_bars) < cfg.sp_box_upper_close_pct:
                continue

            # Box midpoint in upper third of day's range (for longs)
            if not _isnan(ds.session_high) and not _isnan(ds.session_low):
                day_range = ds.session_high - ds.session_low
                if day_range > 0:
                    if is_bull:
                        box_position = (box_mid - ds.session_low) / day_range
                        if box_position < 0.67:
                            continue
                    else:
                        box_position = (ds.session_high - box_mid) / day_range
                        if box_position < 0.67:
                            continue

            # Max closes below EMA9
            below_ema9_count = 0
            for b in box_bars:
                if is_bull and b.close < b._e9:
                    below_ema9_count += 1
                elif not is_bull and b.close > b._e9:
                    below_ema9_count += 1
            if below_ema9_count > cfg.sp_box_max_below_ema9:
                continue

            # Volume check
            avg_vol = sum(b.volume for b in box_bars) / len(box_bars)
            if avg_vol < cfg.sp_box_min_vol_frac * vol_ma:
                continue

            # Failed breakout filter
            failed_bo = 0
            threshold = cfg.sp_box_failed_bo_atr * i_atr
            for b in box_bars:
                if is_bull and b.high > box_high - threshold and b.close < box_high:
                    failed_bo += 1
                elif not is_bull and b.low < box_low + threshold and b.close > box_low:
                    failed_bo += 1
            if failed_bo >= cfg.sp_box_failed_bo_limit:
                continue

            best_box = (box_high, box_low, box_range, len(box_bars), avg_vol, failed_bo)
            break  # use first valid (shortest) window

        if best_box is None:
            return False, NaN, 0

        box_high, box_low, box_range, box_len, box_avg_vol, box_failed = best_box

        # ── Breakout trigger ──
        clearance = cfg.sp_break_clearance_atr * i_atr
        rng = bar.high - bar.low

        if is_bull:
            broke_out = bar.close > box_high + clearance
        else:
            broke_out = bar.close < box_low - clearance

        if not broke_out:
            return False, NaN, 0

        # Breakout volume
        if bar.volume < cfg.sp_break_vol_frac * vol_ma:
            return False, NaN, 0

        # Close position in bar
        if rng > 0:
            if is_bull:
                close_pct = (bar.close - bar.low) / rng
            else:
                close_pct = (bar.high - bar.close) / rng
            if close_pct < cfg.sp_break_close_pct:
                return False, NaN, 0

        # Must remain above VWAP/EMA9
        if is_bull and (bar.close <= vw or bar.close <= e9):
            return False, NaN, 0
        if not is_bull and (bar.close >= vw or bar.close >= e9):
            return False, NaN, 0

        # ── Stop ──
        if is_bull:
            stop = box_low - cfg.sp_stop_buffer
        else:
            stop = box_high + cfg.sp_stop_buffer

        # Floor check
        min_stop = cfg.min_stop_intra_atr_mult * i_atr
        if abs(bar.close - stop) < min_stop:
            stop = bar.close - direction * min_stop

        # ── Quality scoring ──
        quality = self._sp_quality(cfg, box_len, box_range, i_atr, bar.volume,
                                    vol_ma, box_failed, htf_aligned,
                                    ds.session_low if is_bull else ds.session_high,
                                    ds.session_high if is_bull else ds.session_low)

        return True, stop, quality

    @staticmethod
    def _sp_quality(cfg, box_len, box_range, i_atr, bo_vol, vol_ma,
                    failed_bo, htf_aligned, session_extreme, session_other) -> int:
        """Quality score for Spencer setup.
        Calibrated for 5-min bars where valid boxes can be up to 2.0 ATR.
        Max score = 7.
        """
        score = 0
        # Box duration: longer consolidation = stronger base
        if box_len >= 6:
            score += 2
        elif box_len >= 5:
            score += 1
        # Box tightness — scaled to match sp_box_max_range_atr = 2.00
        if i_atr > 0:
            if box_range <= 0.75 * i_atr:
                score += 2   # very tight for 5-min
            elif box_range <= 1.25 * i_atr:
                score += 1   # acceptably tight
        # Breakout volume
        if vol_ma > 0 and bo_vol >= 1.25 * vol_ma:
            score += 1
        # No failed breakout attempts (clean consolidation)
        if failed_bo == 0:
            score += 1
        # HTF alignment
        if htf_aligned:
            score += 1
        return score

    # ═════════════════════════════════════════════
    # FAILED BOUNCE DETECTION (short-only)
    # ═════════════════════════════════════════════

    def _detect_failed_bounce(self, bar: Bar, ds: DayState, cfg,
                               e9: float, vw: float,
                               vol_ma: float, i_atr: float, d_atr: float,
                               in_time: bool, htf_aligned: bool
                               ) -> Tuple[bool, float, int]:
        """
        Detect Failed Bounce short setup.
        Short-only: breakdown → weak bounce toward level → rejection → continuation down.
        Returns (signal_fired, stop_price, quality_score).
        """
        if not in_time or _isnan(vol_ma) or vol_ma <= 0 or i_atr <= 0:
            self._fb_tick_state(ds, bar)
            return False, NaN, 0

        rng = bar.high - bar.low
        bar_bearish = bar.close < bar.open
        bar_bullish = bar.close > bar.open

        # ── Step 1: Detect new breakdown ──
        if not ds.fb_active:
            key_level, level_tag = self._fb_find_key_level(ds, bar, vw)
            if _isnan(key_level):
                return False, NaN, 0

            # Break condition: close decisively below key level
            broke = bar.close < key_level - cfg.fb_break_atr_min * i_atr

            if not broke:
                return False, NaN, 0

            # Bar range check vs median-10
            if len(self._range_buf) >= 5:
                med_range = median(list(self._range_buf))
                if rng < cfg.fb_break_bar_range_frac * med_range:
                    return False, NaN, 0

            # Volume check
            if bar.volume < cfg.fb_break_vol_frac * vol_ma:
                return False, NaN, 0

            # Close in lower portion of bar (bearish conviction)
            if rng > 0:
                close_pct = (bar.high - bar.close) / rng  # distance from high as fraction
                if close_pct < cfg.fb_break_close_pct:
                    return False, NaN, 0

            # Latch breakdown
            ds.fb_active = True
            ds.fb_level = key_level
            ds.fb_level_tag = level_tag
            ds.fb_bd_bar_high = bar.high
            ds.fb_bd_bar_low = bar.low
            ds.fb_bd_vol = bar.volume
            ds.fb_bars_since_bd = 0
            ds.fb_bounced = False
            ds.fb_bars_since_bounce = 999
            ds.fb_bounce_bar_high = NaN
            ds.fb_bounce_bar_low = NaN
            ds.fb_highest_since_bounce = NaN
            ds.fb_bounce_has_wick = False
            return False, NaN, 0  # breakdown bar itself is not the entry

        # ── Active breakdown — tick counter ──
        ds.fb_bars_since_bd += 1

        # Expire if too many bars
        if ds.fb_bars_since_bd > cfg.fb_bounce_window + cfg.fb_confirm_window + 1:
            ds.fb_active = False
            return False, NaN, 0

        level = ds.fb_level

        # ── Step 2: Detect bounce (failed reclaim attempt) ──
        if not ds.fb_bounced and ds.fb_bars_since_bd <= cfg.fb_bounce_window:
            proximity = cfg.fb_bounce_proximity_atr * i_atr
            max_reclaim = cfg.fb_bounce_max_reclaim_atr * i_atr

            # Bounce approaches level from below
            touches_level = bar.high >= level - proximity
            # But close stays below level (failed reclaim)
            failed_reclaim = bar.close < level
            # Doesn't blast through to the upside
            not_too_high = bar.high <= level + max_reclaim
            # Volume is weak (no conviction on bounce)
            vol_ok = bar.volume <= cfg.fb_bounce_max_vol_frac * vol_ma
            # Not a strong bullish expansion bar
            not_bull_expansion = not (bar_bullish and rng > 1.5 * i_atr)

            if touches_level and failed_reclaim and not_too_high and vol_ok and not_bull_expansion:
                # Check for upper wick rejection (trapped buyers)
                upper_wick = bar.high - max(bar.open, bar.close)
                has_wick = rng > 0 and (upper_wick / rng) >= cfg.fb_bounce_wick_frac

                ds.fb_bounced = True
                ds.fb_bounce_bar_high = bar.high
                ds.fb_bounce_bar_low = bar.low
                ds.fb_bars_since_bounce = 0
                ds.fb_highest_since_bounce = bar.high
                ds.fb_bounce_has_wick = has_wick
                return False, NaN, 0

        # ── Step 3: Confirmation after bounce (rejection continues down) ──
        if ds.fb_bounced:
            ds.fb_bars_since_bounce += 1

            # Track highest since bounce (for stop placement)
            if _isnan(ds.fb_highest_since_bounce) or bar.high > ds.fb_highest_since_bounce:
                ds.fb_highest_since_bounce = bar.high

            if ds.fb_bars_since_bounce > cfg.fb_confirm_window:
                ds.fb_active = False
                return False, NaN, 0

            # Confirm conditions: price breaks below bounce bar, bearish, with volume
            bounce_low = ds.fb_bounce_bar_low
            confirmed = (bar.close < bounce_low and
                         bar_bearish and
                         bar.volume >= cfg.fb_confirm_vol_frac * vol_ma and
                         bar.close < vw and bar.close < e9)

            if confirmed:
                # Compute stop: above bounce high + buffer
                highest = ds.fb_highest_since_bounce
                raw_stop = max(ds.fb_bounce_bar_high,
                               highest if not _isnan(highest) else ds.fb_bounce_bar_high)
                stop = raw_stop + cfg.fb_stop_buffer

                # Floor check: ensure adequate risk
                min_stop = cfg.min_stop_intra_atr_mult * i_atr
                if abs(bar.close - stop) < min_stop:
                    stop = bar.close + min_stop

                # Quality scoring
                quality = self._fb_quality(
                    cfg, bar, ds, vol_ma, vw, e9, htf_aligned)

                ds.fb_active = False
                return True, stop, quality

        return False, NaN, 0

    def _fb_tick_state(self, ds: DayState, bar: Bar):
        """Tick Failed Bounce state counters even when not in time window."""
        if ds.fb_active:
            ds.fb_bars_since_bd += 1
            if ds.fb_bars_since_bd > 12:  # hard expire
                ds.fb_active = False

    def _fb_find_key_level(self, ds: DayState, bar: Bar, vw: float) -> Tuple[float, str]:
        """Find key level for Failed Bounce breakdown.
        Prioritizes VWAP (most common failed-reclaim level for shorts),
        then OR low, then prior swing low.
        """
        candidates = []

        # VWAP — primary level for failed bounces
        if self.vwap.ready and not _isnan(vw):
            candidates.append((vw, "VWAP"))

        # OR low — secondary level
        if ds.or_ready and not _isnan(ds.or_low):
            candidates.append((ds.or_low, "ORL"))

        # Prior swing low from _recent_bars
        prior_bars = list(self._recent_bars)
        if len(prior_bars) >= 5:
            swing_low = min(b.low for b in prior_bars)
            candidates.append((swing_low, "SWING"))

        if not candidates:
            return NaN, ""

        # For shorts: prefer the level closest to current price from above
        # (i.e., the level that was just broken)
        valid = [(lvl, tag) for lvl, tag in candidates if bar.close < lvl]
        if valid:
            # Closest level from above = smallest distance
            valid.sort(key=lambda x: x[0] - bar.close)
            return valid[0]

        return NaN, ""

    @staticmethod
    def _fb_quality(cfg, bar, ds, vol_ma, vw, e9, htf_aligned) -> int:
        """Quality score for Failed Bounce setup.
        Max score = 6.
        """
        score = 0
        # Level broken was OR low (structural level)
        if ds.fb_level_tag == "ORL":
            score += 1
        # Breakdown volume strong
        if vol_ma > 0 and ds.fb_bd_vol >= 1.25 * vol_ma:
            score += 1
        # Bounce volume weaker than breakdown (conviction divergence)
        if bar.volume < ds.fb_bd_vol:
            score += 1
        # Upper wick rejection on bounce bar (trapped buyers)
        if ds.fb_bounce_has_wick:
            score += 1
        # Confirmation below both VWAP and EMA9
        if bar.close < vw and bar.close < e9:
            score += 1
        # HTF alignment (bearish)
        if htf_aligned:
            score += 1
        return score

    # ═════════════════════════════════════════════
    # BREAKDOWN-RETEST SHORT DETECTION
    # ═════════════════════════════════════════════

    def _detect_bdr_short(self, bar: Bar, ds: DayState, cfg,
                           e9: float, vw: float,
                           vol_ma: float, i_atr: float, d_atr: float,
                           in_time: bool, htf_aligned: bool
                           ) -> Tuple[bool, float, int]:
        """
        Detect Breakdown-Retest short setup.
        Short-only: break of support → weak retest → rejection bar → short entry.
        Returns (signal_fired, stop_price, quality_score).
        """
        if not in_time or _isnan(vol_ma) or vol_ma <= 0 or i_atr <= 0:
            self._bdr_tick_state(ds)
            return False, NaN, 0

        rng = bar.high - bar.low
        bar_bearish = bar.close < bar.open
        bar_bullish = bar.close > bar.open

        # ── Step 1: Detect new breakdown ──
        if not ds.bdr_active:
            key_level, level_tag = self._bdr_find_key_level(ds, bar, vw)
            if _isnan(key_level):
                return False, NaN, 0

            # Break condition: close decisively below key level
            if bar.close >= key_level - cfg.bdr_break_atr_min * i_atr:
                return False, NaN, 0

            # Must be bearish
            if not bar_bearish:
                return False, NaN, 0

            # Bar range check vs median
            if len(self._range_buf) >= 5:
                med_range = median(list(self._range_buf))
                if rng < cfg.bdr_break_bar_range_frac * med_range:
                    return False, NaN, 0

            # Volume check
            if bar.volume < cfg.bdr_break_vol_frac * vol_ma:
                return False, NaN, 0

            # Close in lower portion of bar (bearish conviction)
            if rng > 0:
                close_pct = (bar.high - bar.close) / rng
                if close_pct < cfg.bdr_break_close_pct:
                    return False, NaN, 0

            # Latch breakdown
            ds.bdr_active = True
            ds.bdr_level = key_level
            ds.bdr_level_tag = level_tag
            ds.bdr_bd_bar_high = bar.high
            ds.bdr_bd_bar_low = bar.low
            ds.bdr_bd_vol = bar.volume
            ds.bdr_bars_since_bd = 0
            ds.bdr_retested = False
            ds.bdr_bars_since_retest = 999
            ds.bdr_retest_bar_high = NaN
            ds.bdr_retest_bar_low = NaN
            ds.bdr_retest_vol = 0.0
            return False, NaN, 0  # breakdown bar is not the entry

        # ── Active breakdown — tick counter ──
        ds.bdr_bars_since_bd += 1

        # Expire if too many bars
        if ds.bdr_bars_since_bd > cfg.bdr_retest_window + cfg.bdr_confirm_window + 1:
            ds.bdr_active = False
            return False, NaN, 0

        level = ds.bdr_level

        # ── Step 2: Detect retest (approach to broken level from below) ──
        if not ds.bdr_retested and ds.bdr_bars_since_bd <= cfg.bdr_retest_window:
            proximity = cfg.bdr_retest_proximity_atr * i_atr
            max_reclaim = cfg.bdr_retest_max_reclaim_atr * i_atr

            touches_level = bar.high >= level - proximity
            failed_reclaim = bar.close < level
            not_too_high = bar.high <= level + max_reclaim
            not_bull_expansion = not (bar_bullish and rng > 1.5 * i_atr)

            # Track running low/vol across all pre-retest bars (matches standalone scanner)
            if _isnan(ds.bdr_retest_bar_low) or bar.low < ds.bdr_retest_bar_low:
                ds.bdr_retest_bar_low = bar.low
            ds.bdr_retest_vol += bar.volume  # accumulate retest-phase volume

            if touches_level and failed_reclaim and not_too_high:
                ds.bdr_retested = True
                ds.bdr_retest_bar_high = bar.high
                ds.bdr_bars_since_retest = 0
                return False, NaN, 0

        # ── Step 3: Rejection bar confirmation ──
        if ds.bdr_retested:
            ds.bdr_bars_since_retest += 1

            # Track highest retest bar for stop placement
            if _isnan(ds.bdr_retest_bar_high) or bar.high > ds.bdr_retest_bar_high:
                ds.bdr_retest_bar_high = bar.high

            if ds.bdr_bars_since_retest > cfg.bdr_confirm_window:
                ds.bdr_active = False
                return False, NaN, 0

            # Rejection bar must: close below retest low, be bearish
            retest_low = ds.bdr_retest_bar_low
            if not (bar.close < retest_low and bar_bearish):
                return False, NaN, 0

            # Volume check on rejection bar
            if bar.volume < cfg.bdr_confirm_vol_frac * vol_ma:
                return False, NaN, 0

            # BIG REJECTION WICK FILTER (from feature study)
            if rng > 0:
                upper_wick = bar.high - max(bar.open, bar.close)
                wick_pct = upper_wick / rng
            else:
                wick_pct = 0.0

            if wick_pct < cfg.bdr_min_rejection_wick_pct:
                ds.bdr_active = False  # doesn't meet wick filter, cancel
                return False, NaN, 0

            # Compute stop: above retest high + buffer
            raw_stop = ds.bdr_retest_bar_high
            stop = raw_stop + cfg.bdr_stop_buffer_atr * i_atr

            # Floor check
            min_stop = cfg.min_stop_intra_atr_mult * i_atr
            if abs(bar.close - stop) < min_stop:
                stop = bar.close + min_stop

            # Quality scoring
            quality = self._bdr_quality(cfg, bar, ds, vol_ma, vw, e9, htf_aligned)

            ds.bdr_active = False
            return True, stop, quality

        return False, NaN, 0

    def _bdr_tick_state(self, ds: DayState):
        """Tick BDR state counters even when not in time window."""
        if ds.bdr_active:
            ds.bdr_bars_since_bd += 1
            if ds.bdr_bars_since_bd > 15:  # hard expire
                ds.bdr_active = False

    def _bdr_find_key_level(self, ds: DayState, bar: Bar, vw: float) -> Tuple[float, str]:
        """Find key level for BDR breakdown.
        Same approach as FB: VWAP → OR low → swing low.
        """
        candidates = []

        if self.vwap.ready and not _isnan(vw):
            candidates.append((vw, "VWAP"))

        if ds.or_ready and not _isnan(ds.or_low):
            candidates.append((ds.or_low, "ORL"))

        prior_bars = list(self._recent_bars)
        if len(prior_bars) >= 5:
            swing_low = min(b.low for b in prior_bars)
            if swing_low < bar.open:  # must be established before current bar
                candidates.append((swing_low, "SWING"))

        if not candidates:
            return NaN, ""

        # For shorts: prefer the closest level from above that was just broken
        valid = [(lvl, tag) for lvl, tag in candidates if bar.close < lvl]
        if valid:
            valid.sort(key=lambda x: x[0] - bar.close)
            return valid[0]

        return NaN, ""

    @staticmethod
    def _bdr_quality(cfg, bar, ds, vol_ma, vw, e9, htf_aligned) -> int:
        """Quality score for BDR setup. Max = 5."""
        score = 0
        # Level was OR low (structural)
        if ds.bdr_level_tag == "ORL":
            score += 1
        # Strong breakdown volume
        if vol_ma > 0 and ds.bdr_bd_vol >= 1.25 * vol_ma:
            score += 1
        # Retest volume weaker than breakdown
        if ds.bdr_retest_vol < ds.bdr_bd_vol:
            score += 1
        # Confirmation below both VWAP and EMA9
        if bar.close < vw and bar.close < e9:
            score += 1
        # HTF alignment
        if htf_aligned:
            score += 1
        return score

    # ═════════════════════════════════════════════
    # EMA SCALP DETECTION (Reclaim + Confirm)
    # ═════════════════════════════════════════════

    def _update_ema_scalp_state(self, bar: Bar, ds: DayState, cfg,
                                 e9: float, e20: float, vw: float, i_atr: float,
                                 rvol_tod: float, five_day_high: float, five_day_low: float):
        """Update impulse tracking and pullback state for EMA scalp setups.
        Called every bar to maintain state. Detection happens in _detect_ema_scalp.
        """
        if not self.ema9.ready or _isnan(e9) or i_atr <= 0:
            return

        prev = self._bars[-1] if self._bars else None
        if not prev:
            return

        # ── Impulse tracking: detect new impulse moves ──
        # A new bullish impulse: strong bar closes well above EMA9, trend intact
        impulse_long = (bar.close > e9 and bar.close > bar.open and
                        (bar.close - e9) > 0.10 * i_atr and
                        e9 > e20)
        impulse_short = (bar.close < e9 and bar.close < bar.open and
                         (e9 - bar.close) > 0.10 * i_atr and
                         e9 < e20)

        # If we get a new impulse, reset retest counter
        if impulse_long and not ds.ema_long_pb_active:
            if _isnan(ds.ema_long_impulse_high) or bar.high > ds.ema_long_impulse_high:
                ds.ema_long_impulse_high = bar.high
                ds.ema_long_retest_count = 0
        if impulse_short and not ds.ema_short_pb_active:
            if _isnan(ds.ema_short_impulse_low) or bar.low < ds.ema_short_impulse_low:
                ds.ema_short_impulse_low = bar.low
                ds.ema_short_retest_count = 0

        # ── Pullback detection: entering the EMA9 zone ──
        # Long: bar dips into or toward EMA9 (within ~15% of intra ATR)
        if not ds.ema_long_pb_active:
            if bar.low <= e9 + 0.15 * i_atr and bar.close > e9 * 0.97:  # approaching EMA9
                ds.ema_long_pb_active = True
                ds.ema_long_pb_low = bar.low
                ds.ema_long_pb_bars = 1
                ds.ema_long_pb_closes_below = 1 if bar.close < e9 else 0
        else:
            ds.ema_long_pb_bars += 1
            if bar.low < ds.ema_long_pb_low:
                ds.ema_long_pb_low = bar.low
            if bar.close < e9:
                ds.ema_long_pb_closes_below += 1
            # Expire pullback if it goes too deep or too long
            pb_depth = e9 - ds.ema_long_pb_low if not _isnan(ds.ema_long_pb_low) else 0
            if (pb_depth > cfg.ema_scalp_max_pb_depth_atr * i_atr * 2 or
                    ds.ema_long_pb_bars > 8):
                ds.ema_long_pb_active = False

        # Short: bar pushes into or toward EMA9 (within ~15% of intra ATR)
        if not ds.ema_short_pb_active:
            if bar.high >= e9 - 0.15 * i_atr and bar.close < e9 * 1.03:
                ds.ema_short_pb_active = True
                ds.ema_short_pb_high = bar.high
                ds.ema_short_pb_bars = 1
                ds.ema_short_pb_closes_above = 1 if bar.close > e9 else 0
        else:
            ds.ema_short_pb_bars += 1
            if bar.high > ds.ema_short_pb_high:
                ds.ema_short_pb_high = bar.high
            if bar.close > e9:
                ds.ema_short_pb_closes_above += 1
            pb_depth = ds.ema_short_pb_high - e9 if not _isnan(ds.ema_short_pb_high) else 0
            if (pb_depth > cfg.ema_scalp_max_pb_depth_atr * i_atr * 2 or
                    ds.ema_short_pb_bars > 8):
                ds.ema_short_pb_active = False

    def _detect_ema_scalp(self, bar: Bar, ds: DayState, cfg,
                           direction: int, e9: float, e20: float, vw: float,
                           vol_ma: float, i_atr: float, d_atr: float,
                           rvol_tod: float, five_day_high: float, five_day_low: float,
                           in_time: bool, htf_aligned: bool
                           ) -> Tuple[bool, float, int, SetupId]:
        """
        Detect EMA Scalp setups: RECLAIM or CONFIRM.
        Returns (signal_fired, stop_price, quality_score, setup_id).
        """
        if (not in_time or _isnan(vol_ma) or vol_ma <= 0 or
                i_atr <= 0 or not self.ema9.ready or not self.ema20.ready):
            return False, NaN, 0, SetupId.NONE

        prev = self._bars[-1] if self._bars else None
        if not prev:
            return False, NaN, 0, SetupId.NONE

        is_bull = direction == 1
        rng = bar.high - bar.low

        # ═══ CONTEXT GATE: must pass before ANY evaluation ═══

        # 1. RVOL by time-of-day (if we have enough history)
        rvol_ok = _isnan(rvol_tod) or rvol_tod >= cfg.ema_scalp_rvol_min
        # Be permissive if RVOL data not yet available (first few days)
        if not _isnan(rvol_tod) and rvol_tod < cfg.ema_scalp_rvol_min:
            return False, NaN, 0, SetupId.NONE

        # 2. EMA alignment (EMA9 > EMA20 for trend, EMA20 slope for persistence)
        #    EMA9 slope check removed — during a pullback, EMA9 naturally flattens
        #    while the trend (EMA20) remains intact. Use EMA20 slope instead.
        if is_bull:
            if not (e9 > e20 and e20 > prev._e20):
                return False, NaN, 0, SetupId.NONE
        else:
            if not (e9 < e20 and e20 < prev._e20):
                return False, NaN, 0, SetupId.NONE

        # 3. VWAP alignment
        if is_bull and bar.close < vw:
            return False, NaN, 0, SetupId.NONE
        if not is_bull and bar.close > vw:
            return False, NaN, 0, SetupId.NONE

        # 4. Breakout context (optional but scored)
        has_breakout_context = False
        if is_bull and not _isnan(five_day_high):
            has_breakout_context = bar.close > five_day_high or ds.session_high > five_day_high
        elif not is_bull and not _isnan(five_day_low):
            has_breakout_context = bar.close < five_day_low or ds.session_low < five_day_low
        if cfg.ema_scalp_require_breakout and not has_breakout_context:
            return False, NaN, 0, SetupId.NONE

        # 5. Retest count: first retest only
        pfx = "ema_long_" if is_bull else "ema_short_"
        retest_count = getattr(ds, pfx + "retest_count")
        if retest_count >= cfg.ema_scalp_max_retests:
            return False, NaN, 0, SetupId.NONE

        # 6. Midday dead zone check
        if (not cfg.ema_scalp_allow_midday and
                bar.time_hhmm >= cfg.ema_scalp_dead_start and
                bar.time_hhmm <= cfg.ema_scalp_dead_end):
            return False, NaN, 0, SetupId.NONE

        # ═══ PULLBACK STATE CHECK ═══
        pb_active = getattr(ds, pfx + "pb_active")
        if not pb_active:
            return False, NaN, 0, SetupId.NONE

        pb_low = getattr(ds, pfx + ("pb_low" if is_bull else "pb_high"))
        pb_bars = getattr(ds, pfx + "pb_bars")
        pb_closes_wrong = getattr(ds, pfx + ("pb_closes_below" if is_bull else "pb_closes_above"))

        # Pullback depth check
        if is_bull:
            pb_depth = e9 - pb_low if not _isnan(pb_low) else 999
        else:
            pb_depth = pb_low - e9 if not _isnan(pb_low) else 999  # pb_low is actually pb_high for shorts
        if pb_depth > cfg.ema_scalp_max_pb_depth_atr * i_atr:
            return False, NaN, 0, SetupId.NONE

        # Max closes on wrong side
        if pb_closes_wrong > cfg.ema_scalp_max_closes_below:
            return False, NaN, 0, SetupId.NONE

        # ═══ TRY RECLAIM FIRST ═══
        # Reclaim: bar closes back above EMA9, strong candle, on same or next bar after dip
        if is_bull:
            reclaim = (bar.close > e9 and bar.close > bar.open and
                       prev.low <= e9 + 0.15 * i_atr)  # prev bar dipped toward EMA9
        else:
            reclaim = (bar.close < e9 and bar.close < bar.open and
                       prev.high >= e9 - 0.15 * i_atr)

        if reclaim:
            # Check reclaim candle quality
            if rng > 0:
                if is_bull:
                    close_pct = (bar.close - bar.low) / rng
                else:
                    close_pct = (bar.high - bar.close) / rng
                if close_pct < cfg.ema_scalp_reclaim_close_pct:
                    reclaim = False  # candle not strong enough

            # Volume expansion on reclaim
            if bar.volume < cfg.ema_scalp_reclaim_vol_frac * vol_ma:
                reclaim = False

        if reclaim:
            # Compute stop: below pullback low
            if is_bull:
                stop = pb_low - cfg.ema_scalp_stop_buffer_atr * i_atr
            else:
                stop = pb_low + cfg.ema_scalp_stop_buffer_atr * i_atr  # pb_low is pb_high for shorts

            risk = abs(bar.close - stop)
            if risk <= 0 or risk > cfg.ema_scalp_max_stop_atr * i_atr:
                return False, NaN, 0, SetupId.NONE

            # Quality scoring
            quality = self._ema_scalp_quality(
                cfg, bar, ds, direction, vol_ma, vw, e9, i_atr,
                rvol_tod, has_breakout_context, retest_count,
                pb_depth, pb_closes_wrong, htf_aligned, is_reclaim=True)

            # Mark retest used + reset pullback
            setattr(ds, pfx + "retest_count", retest_count + 1)
            setattr(ds, pfx + "pb_active", False)

            return True, stop, quality, SetupId.EMA_RECLAIM

        # ═══ TRY CONFIRM ═══
        # Confirm: pullback holds near EMA9 for <= N bars, higher low forms,
        # trigger above prior bar high/micro pivot
        if pb_bars <= cfg.ema_scalp_max_pb_bars:
            if is_bull:
                # Higher low: current bar low > pullback low
                higher_low = bar.low > pb_low + 0.01
                # Trigger: close above prior bar high
                trigger = bar.close > prev.high
                # Above key levels
                above_key = bar.close > e9 and bar.close > vw
                # Bullish candle
                candle_ok = bar.close > bar.open
            else:
                higher_low = bar.high < pb_low - 0.01  # lower high (pb_low = pb_high for shorts)
                trigger = bar.close < prev.low
                above_key = bar.close < e9 and bar.close < vw
                candle_ok = bar.close < bar.open

            if higher_low and trigger and above_key and candle_ok:
                # Volume on confirmation
                if bar.volume < cfg.ema_scalp_confirm_vol_frac * vol_ma:
                    return False, NaN, 0, SetupId.NONE

                # Compute stop: below pullback low (micro structure)
                if is_bull:
                    stop = pb_low - cfg.ema_scalp_stop_buffer_atr * i_atr
                else:
                    stop = pb_low + cfg.ema_scalp_stop_buffer_atr * i_atr

                risk = abs(bar.close - stop)
                if risk <= 0 or risk > cfg.ema_scalp_max_stop_atr * i_atr:
                    return False, NaN, 0, SetupId.NONE

                quality = self._ema_scalp_quality(
                    cfg, bar, ds, direction, vol_ma, vw, e9, i_atr,
                    rvol_tod, has_breakout_context, retest_count,
                    pb_depth, pb_closes_wrong, htf_aligned, is_reclaim=False)

                setattr(ds, pfx + "retest_count", retest_count + 1)
                setattr(ds, pfx + "pb_active", False)

                return True, stop, quality, SetupId.EMA_CONFIRM

        return False, NaN, 0, SetupId.NONE

    @staticmethod
    def _ema_scalp_quality(cfg, bar, ds, direction, vol_ma, vw, e9, i_atr,
                           rvol_tod, has_breakout_context, retest_count,
                           pb_depth, pb_closes_wrong, htf_aligned,
                           is_reclaim: bool) -> int:
        """Quality score for EMA Scalp setups.
        Max score = 7.
        """
        score = 0
        # First retest: +2 (this is the single most predictive factor)
        if retest_count == 0:
            score += 2
        # RVOL >= 1.5: +1
        if not _isnan(rvol_tod) and rvol_tod >= 1.5:
            score += 1
        # Breakout context present: +1
        if has_breakout_context:
            score += 1
        # Pullback depth <= 0.20 ATR (very shallow = strong): +1
        if i_atr > 0 and pb_depth <= 0.20 * i_atr:
            score += 1
        # Volume expansion on reclaim/confirm: +1
        if vol_ma > 0 and bar.volume >= 1.2 * vol_ma:
            score += 1
        # HTF aligned: +1
        if htf_aligned:
            score += 1
        return score

    # ═════════════════════════════════════════════
    # EMA FIRST PULLBACK IN PLAY (FPIP)
    # ═════════════════════════════════════════════

    def _update_fpip_state(self, bar, ds, cfg, e9, e20, vw, i_atr,
                           vol_ma, rvol_tod):
        """
        Track expansion legs and first pullback for EMA_FPIP.
        Called every bar. Detection happens in _detect_fpip.

        Expansion: clean directional impulse (E9>E20, limited overlap, strong bars).
        Pullback: first retracement after expansion (volume contraction expected).
        """
        if not self.ema9.ready or _isnan(e9) or i_atr <= 0 or _isnan(vol_ma) or vol_ma <= 0:
            return

        prev = self._bars[-1] if self._bars else None
        if not prev:
            return

        # ── LONG EXPANSION & PULLBACK ──
        self._update_fpip_side(bar, ds, cfg, e9, e20, i_atr, vol_ma, prev,
                               direction=1)

        # ── SHORT EXPANSION & PULLBACK ──
        self._update_fpip_side(bar, ds, cfg, e9, e20, i_atr, vol_ma, prev,
                               direction=-1)

    def _update_fpip_side(self, bar, ds, cfg, e9, e20, i_atr, vol_ma, prev,
                          direction: int):
        """Update FPIP state for one side (long or short)."""
        is_bull = direction == 1
        pfx = "fpip_long_" if is_bull else "fpip_short_"

        exp_active = getattr(ds, pfx + "expansion_active")
        qual_exists = getattr(ds, pfx + "qual_expansion_exists")
        pb_started = getattr(ds, pfx + "pb_started")

        # ── EXPANSION TRACKING ──
        if not exp_active and not qual_exists:
            # Try to start a new expansion
            if is_bull:
                is_impulse = (bar.close > bar.open and
                              (bar.close - e9) > 0.10 * i_atr and
                              e9 > e20)
            else:
                is_impulse = (bar.close < bar.open and
                              (e9 - bar.close) > 0.10 * i_atr and
                              e9 < e20)

            if is_impulse:
                setattr(ds, pfx + "expansion_active", True)
                if is_bull:
                    setattr(ds, pfx + "expansion_high", bar.high)
                    setattr(ds, pfx + "expansion_low", bar.low)
                else:
                    setattr(ds, pfx + "expansion_low", bar.low)
                    setattr(ds, pfx + "expansion_high", bar.high)
                setattr(ds, pfx + "expansion_bars", 1)
                if is_bull:
                    setattr(ds, pfx + "expansion_distance", bar.high - bar.low)
                else:
                    setattr(ds, pfx + "expansion_distance", bar.high - bar.low)
                setattr(ds, pfx + "expansion_total_vol", bar.volume)
                setattr(ds, pfx + "expansion_avg_vol", bar.volume)
                setattr(ds, pfx + "expansion_overlap_bars", 0)
                setattr(ds, pfx + "expansion_prev_close", bar.close)
                setattr(ds, pfx + "pb_started", False)

        elif exp_active:
            exp_bars = getattr(ds, pfx + "expansion_bars")

            # Continue expansion if trend intact and not too long
            if is_bull:
                trend_ok = bar.close > e9 and e9 > e20
            else:
                trend_ok = bar.close < e9 and e9 < e20

            if trend_ok and exp_bars < cfg.ema_fpip_expansion_max_bars:
                exp_bars += 1
                setattr(ds, pfx + "expansion_bars", exp_bars)

                if is_bull:
                    old_high = getattr(ds, pfx + "expansion_high")
                    if bar.high > old_high:
                        setattr(ds, pfx + "expansion_high", bar.high)
                    exp_low = getattr(ds, pfx + "expansion_low")
                    setattr(ds, pfx + "expansion_distance",
                            getattr(ds, pfx + "expansion_high") - exp_low)
                else:
                    old_low = getattr(ds, pfx + "expansion_low")
                    if bar.low < old_low:
                        setattr(ds, pfx + "expansion_low", bar.low)
                    exp_high = getattr(ds, pfx + "expansion_high")
                    setattr(ds, pfx + "expansion_distance",
                            exp_high - getattr(ds, pfx + "expansion_low"))

                total_vol = getattr(ds, pfx + "expansion_total_vol") + bar.volume
                setattr(ds, pfx + "expansion_total_vol", total_vol)
                setattr(ds, pfx + "expansion_avg_vol", total_vol / exp_bars)

                # Count overlap: body overlaps with previous bar body
                prev_close = getattr(ds, pfx + "expansion_prev_close")
                if not _isnan(prev_close):
                    cur_body_lo = min(bar.open, bar.close)
                    cur_body_hi = max(bar.open, bar.close)
                    prev_body_lo = min(prev.open, prev.close)
                    prev_body_hi = max(prev.open, prev.close)
                    if cur_body_lo < prev_body_hi and cur_body_hi > prev_body_lo:
                        overlap = getattr(ds, pfx + "expansion_overlap_bars") + 1
                        setattr(ds, pfx + "expansion_overlap_bars", overlap)

                setattr(ds, pfx + "expansion_prev_close", bar.close)
            else:
                # Expansion ending — check if it qualifies
                exp_dist = getattr(ds, pfx + "expansion_distance")
                exp_avg_vol = getattr(ds, pfx + "expansion_avg_vol")
                overlap_bars = getattr(ds, pfx + "expansion_overlap_bars")
                overlap_ratio = overlap_bars / max(exp_bars, 1)

                if (exp_bars >= cfg.ema_fpip_expansion_min_bars and
                    exp_dist >= cfg.ema_fpip_min_expansion_atr * i_atr and
                    exp_avg_vol >= cfg.ema_fpip_min_expansion_avg_vol * vol_ma and
                    overlap_ratio <= cfg.ema_fpip_max_impulse_overlap_ratio):
                    setattr(ds, pfx + "qual_expansion_exists", True)
                # Reset expansion tracking
                setattr(ds, pfx + "expansion_active", False)

        # ── PULLBACK TRACKING ──
        if qual_exists and not pb_started:
            # Try to start pullback: bar dips into EMA9 zone
            if is_bull:
                dips_into_zone = bar.low <= e9 + 0.15 * i_atr and bar.close > e9 * 0.97
            else:
                dips_into_zone = bar.high >= e9 - 0.15 * i_atr and bar.close < e9 * 1.03

            if dips_into_zone:
                setattr(ds, pfx + "pb_started", True)
                setattr(ds, pfx + "pb_bars", 1)
                if is_bull:
                    setattr(ds, pfx + "pb_low", bar.low)
                else:
                    setattr(ds, pfx + "pb_high", bar.high)
                setattr(ds, pfx + "pb_total_vol", bar.volume)
                setattr(ds, pfx + "pb_avg_vol", bar.volume)
                exp_avg = getattr(ds, pfx + "expansion_avg_vol")
                heavy = 1 if bar.volume >= exp_avg else 0
                setattr(ds, pfx + "pb_heavy_bars", heavy)

        elif pb_started:
            pb_bars = getattr(ds, pfx + "pb_bars") + 1
            setattr(ds, pfx + "pb_bars", pb_bars)

            if is_bull:
                old_low = getattr(ds, pfx + "pb_low")
                if not _isnan(old_low):
                    setattr(ds, pfx + "pb_low", min(old_low, bar.low))
                else:
                    setattr(ds, pfx + "pb_low", bar.low)
            else:
                old_high = getattr(ds, pfx + "pb_high")
                if not _isnan(old_high):
                    setattr(ds, pfx + "pb_high", max(old_high, bar.high))
                else:
                    setattr(ds, pfx + "pb_high", bar.high)

            total_vol = getattr(ds, pfx + "pb_total_vol") + bar.volume
            setattr(ds, pfx + "pb_total_vol", total_vol)
            setattr(ds, pfx + "pb_avg_vol", total_vol / pb_bars)

            exp_avg = getattr(ds, pfx + "expansion_avg_vol")
            if bar.volume >= exp_avg:
                heavy = getattr(ds, pfx + "pb_heavy_bars") + 1
                setattr(ds, pfx + "pb_heavy_bars", heavy)

            # Expire pullback if too deep, too long, or trend broken
            exp_dist = getattr(ds, pfx + "expansion_distance")
            if is_bull:
                pb_extreme = getattr(ds, pfx + "pb_low")
                pb_depth = e9 - pb_extreme if not _isnan(pb_extreme) else 0
            else:
                pb_extreme = getattr(ds, pfx + "pb_high")
                pb_depth = pb_extreme - e9 if not _isnan(pb_extreme) else 0

            if is_bull:
                trend_broken = e9 < e20
            else:
                trend_broken = e9 > e20

            if (pb_depth > cfg.ema_fpip_max_pullback_depth_pct * max(exp_dist, 0.01) or
                pb_bars > cfg.ema_fpip_max_pullback_bars or
                trend_broken):
                setattr(ds, pfx + "pb_started", False)
                if cfg.ema_fpip_first_pullback_only:
                    setattr(ds, pfx + "qual_expansion_exists", False)

    def _detect_fpip(self, bar, ds, cfg, direction, e9, e20, vw,
                     vol_ma, i_atr, d_atr, rvol_tod, in_time, htf_aligned):
        """
        Detect EMA First Pullback In Play setup.
        Returns (signal_fired, stop_price, quality_score).

        Hard disqualifier chain — any single failure rejects the signal.
        """
        if (not in_time or _isnan(vol_ma) or vol_ma <= 0 or
                i_atr <= 0 or not self.ema9.ready or not self.ema20.ready):
            return False, NaN, 0

        prev = self._bars[-1] if self._bars else None
        if not prev:
            return False, NaN, 0

        is_bull = direction == 1
        rng = bar.high - bar.low
        pfx = "fpip_long_" if is_bull else "fpip_short_"

        # ═══ GATE 1: RVOL — stock must be in play ═══
        if _isnan(rvol_tod) or rvol_tod < cfg.ema_fpip_rvol_tod_min:
            return False, NaN, 0

        # ═══ GATE 2: EMA alignment ═══
        if is_bull:
            if not (e9 > e20 and e20 > prev._e20):
                return False, NaN, 0
        else:
            if not (e9 < e20 and e20 < prev._e20):
                return False, NaN, 0

        # ═══ GATE 3: VWAP alignment ═══
        if cfg.ema_fpip_require_vwap_align:
            if is_bull and bar.close < vw:
                return False, NaN, 0
            if not is_bull and bar.close > vw:
                return False, NaN, 0

        # ═══ GATE 4: Market alignment ═══
        if cfg.ema_fpip_require_market_align:
            if not htf_aligned:
                return False, NaN, 0

        # ═══ GATE 5: Qualified expansion must exist ═══
        if not getattr(ds, pfx + "qual_expansion_exists", False):
            return False, NaN, 0

        # ═══ GATE 6: Active pullback ═══
        if not getattr(ds, pfx + "pb_started", False):
            return False, NaN, 0

        # Read pullback state
        pb_bars = getattr(ds, pfx + "pb_bars", 0)
        pb_avg_vol = getattr(ds, pfx + "pb_avg_vol", vol_ma)
        pb_heavy = getattr(ds, pfx + "pb_heavy_bars", 0)
        exp_dist = getattr(ds, pfx + "expansion_distance", 0.0)
        exp_avg_vol = getattr(ds, pfx + "expansion_avg_vol", vol_ma)

        if is_bull:
            pb_extreme = getattr(ds, pfx + "pb_low", NaN)
            pb_depth = e9 - pb_extreme if not _isnan(pb_extreme) else 999
        else:
            pb_extreme = getattr(ds, pfx + "pb_high", NaN)
            pb_depth = pb_extreme - e9 if not _isnan(pb_extreme) else 999

        # ═══ GATE 7: Pullback depth ═══
        if exp_dist > 0 and pb_depth > cfg.ema_fpip_max_pullback_depth_pct * exp_dist:
            return False, NaN, 0

        # ═══ GATE 8: Pullback volume contraction ═══
        if exp_avg_vol > 0 and pb_avg_vol > cfg.ema_fpip_max_pullback_volume_ratio * exp_avg_vol:
            return False, NaN, 0

        # ═══ GATE 9: Heavy pullback bars ═══
        if pb_heavy > cfg.ema_fpip_max_heavy_pullback_bars:
            return False, NaN, 0

        # ═══ TRIGGER: Bar closes back through EMA9 ═══
        if is_bull:
            trigger = (bar.close > e9 and bar.close > bar.open and
                       prev.low <= e9 + 0.15 * i_atr)
        else:
            trigger = (bar.close < e9 and bar.close < bar.open and
                       prev.high >= e9 - 0.15 * i_atr)

        if not trigger:
            return False, NaN, 0

        # ═══ GATE 10: Trigger bar close quality ═══
        if rng > 0:
            if is_bull:
                close_pct = (bar.close - bar.low) / rng
            else:
                close_pct = (bar.high - bar.close) / rng
            if close_pct < cfg.ema_fpip_min_trigger_close_pct:
                return False, NaN, 0

        # ═══ GATE 11: Trigger bar body size ═══
        body = abs(bar.close - bar.open)
        if rng > 0 and body / rng < cfg.ema_fpip_min_trigger_body_pct:
            return False, NaN, 0

        # ═══ GATE 12: Trigger volume expansion vs pullback ═══
        if pb_avg_vol > 0 and bar.volume < cfg.ema_fpip_trigger_volume_vs_pullback_min * pb_avg_vol:
            return False, NaN, 0

        # ═══ COMPUTE STOP & RISK ═══
        stop_buffer = cfg.ema_scalp_stop_buffer_atr * i_atr
        if is_bull:
            stop = pb_extreme - stop_buffer
        else:
            stop = pb_extreme + stop_buffer

        risk = abs(bar.close - stop)
        if risk <= 0 or risk > cfg.ema_scalp_max_stop_atr * i_atr:
            return False, NaN, 0

        # ═══ QUALITY SCORING ═══
        quality = self._fpip_quality(cfg, bar, pb_depth, exp_dist,
                                      pb_avg_vol, exp_avg_vol, rvol_tod,
                                      htf_aligned)

        # Clear expansion state — first pullback only
        setattr(ds, pfx + "qual_expansion_exists", False)
        setattr(ds, pfx + "pb_started", False)

        return True, stop, quality

    @staticmethod
    def _fpip_quality(cfg, bar, pb_depth, exp_dist, pb_avg_vol,
                      exp_avg_vol, rvol_tod, htf_aligned) -> int:
        """Quality score for EMA FPIP. Max = 6.
        Hard gates already filter; scoring ranks valid candidates.
        """
        score = 0
        # First pullback (inherent in setup design): +1
        score += 1
        # Shallow pullback (<25% of expansion): +1
        if exp_dist > 0 and pb_depth < 0.25 * exp_dist:
            score += 1
        # Strong trigger volume (>= 1.2× expansion avg): +1
        if exp_avg_vol > 0 and bar.volume >= 1.2 * exp_avg_vol:
            score += 1
        # RVOL >= 2.0 (very active stock): +1
        if not _isnan(rvol_tod) and rvol_tod >= 2.0:
            score += 1
        # Light pullback volume (<60% expansion avg): +1
        if exp_avg_vol > 0 and pb_avg_vol < 0.60 * exp_avg_vol:
            score += 1
        # HTF aligned: +1
        if htf_aligned:
            score += 1
        return min(score, 6)
