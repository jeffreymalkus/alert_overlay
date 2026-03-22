"""
InPlayProxyV2 — Direction-agnostic two-stage objective gate.

Objective 0–10 score built from five bucketed components:
  Move-from-open (0–2.5), RS-vs-SPY (0–2.0), Gap (0–1.5),
  Range-expansion (0–2.0), RVOL (0–2.0).

Gate rule:  PASS if objective_score >= per-strategy hard floor.
No relational score. No cross-sectional ranking. No daily exclusion overlay.

Two stages:
  Provisional: available at configurable early time (default 9:40 ET)
  Confirmed:   available at 10:00 ET

Score is stable across days — a symbol's score depends only on its own
metrics and rolling baselines, not on what other symbols are loaded.
"""

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

NaN = float('nan')
_isnan = math.isnan


@dataclass
class InPlayResultV2:
    """Output of V2 evaluation. Preserves raw scores even on failure."""
    session_date: date = None

    # Scores (0.0 - 10.0 objective)
    score_provisional: float = NaN
    score_confirmed: float = NaN
    score_raw_current: float = NaN  # latest score at whatever stage is active

    # Pass/fail (never zeros the score)
    passed_provisional: bool = False
    passed_confirmed: bool = False

    # Readiness
    is_ready_provisional: bool = False
    is_ready_confirmed: bool = False

    # Active score (convenience — uses the most advanced stage available)
    active_score_kind: str = "NONE"  # NONE / PROVISIONAL / CONFIRMED
    active_score: float = NaN
    active_passed: bool = False

    # Reason flags (human-readable)
    reason_flags: str = "NOT_EVALUATED"

    # Raw metrics
    gap_abs_pct: float = NaN
    abs_move_from_open_pct: float = NaN
    abs_rs_vs_spy_pct: float = NaN
    rvol: float = NaN
    range_expansion: float = NaN
    cutoff_hhmm: int = 0

    # Component score audit fields
    component_move_score: float = 0.0
    component_rs_score: float = 0.0
    component_gap_score: float = 0.0
    component_range_score: float = 0.0
    component_rvol_score: float = 0.0

    # Legacy compatibility
    @property
    def passed(self) -> bool:
        return self.active_passed

    @property
    def score(self) -> float:
        return self.active_score if not _isnan(self.active_score) else 0.0

    @property
    def reason(self) -> str:
        return self.reason_flags


# ══════════════════════════════════════════════════
# Bucket helpers — exact spec tables
# ══════════════════════════════════════════════════

def _bucket_move_score(abs_move_pct: float) -> float:
    """Move-from-open score — max 2.5."""
    if _isnan(abs_move_pct) or abs_move_pct < 0.50:
        return 0.0
    if abs_move_pct < 1.00:
        return 0.625
    if abs_move_pct < 1.75:
        return 1.25
    if abs_move_pct < 3.00:
        return 1.875
    return 2.5


def _bucket_rs_score(abs_rs_pct: float) -> float:
    """RS-vs-SPY score — max 2.0."""
    if _isnan(abs_rs_pct) or abs_rs_pct < 0.50:
        return 0.0
    if abs_rs_pct < 1.00:
        return 0.667
    if abs_rs_pct < 2.00:
        return 1.333
    return 2.0


def _bucket_gap_score(gap_abs_pct: float) -> float:
    """Gap score — max 1.5."""
    if _isnan(gap_abs_pct) or gap_abs_pct < 0.50:
        return 0.0
    if gap_abs_pct < 1.00:
        return 0.50
    if gap_abs_pct < 2.00:
        return 1.00
    return 1.50


def _bucket_range_score(range_expansion: float) -> float:
    """Range-expansion score — max 2.0."""
    if _isnan(range_expansion) or range_expansion < 0.75:
        return 0.0
    if range_expansion < 1.00:
        return 0.50
    if range_expansion < 1.50:
        return 1.00
    if range_expansion < 2.00:
        return 1.50
    return 2.0


def _bucket_rvol_score(rvol: float) -> float:
    """RVOL score — max 2.0."""
    if _isnan(rvol) or rvol < 0.50:
        return 0.0
    if rvol < 0.75:
        return 0.50
    if rvol < 2.00:
        return 1.00
    if rvol < 4.00:
        return 1.50
    return 2.0


def _compute_objective_score(features: dict) -> dict:
    """Build objective 0–10 score from raw feature dict.

    Returns dict with score_total and all component scores.
    """
    move_s = _bucket_move_score(features.get("abs_move_from_open_pct", NaN))
    rs_s = _bucket_rs_score(features.get("abs_rs_vs_spy_pct", NaN))
    gap_s = _bucket_gap_score(features.get("gap_abs_pct", NaN))
    range_s = _bucket_range_score(features.get("range_expansion", NaN))
    rvol_s = _bucket_rvol_score(features.get("rvol", NaN))

    return {
        "score_total": move_s + rs_s + gap_s + range_s + rvol_s,
        "move_score": move_s,
        "rs_score": rs_s,
        "gap_score": gap_s,
        "range_score": range_s,
        "rvol_score": rvol_s,
    }


class InPlayProxyV2:
    """Direction-agnostic two-stage objective gate.

    Gate rule: PASS if objective_score >= hard_floor (per strategy).
    No cross-sectional ranking. No relational score.

    Usage (replay):
        v2 = InPlayProxyV2(cfg)
        v2.precompute_day(bars_by_symbol, spy_bars, session_date, prior_closes,
                          vol_baselines, range_baselines)
        result = v2.get_result(symbol, session_date, hhmm)

    Usage (live):
        v2 = InPlayProxyV2(cfg)
        result = v2.update_live(symbol, session_date, ...)
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._provisional_hhmm = getattr(cfg, 'ip_v2_provisional_hhmm', 940)
        self._confirmed_hhmm = getattr(cfg, 'ip_v2_confirmed_hhmm', 1000)

        # Per-symbol rolling baseline buffers (20-day)
        self._vol_baselines: Dict[str, deque] = {}   # symbol -> deque of first-window volumes
        self._range_baselines: Dict[str, deque] = {}  # symbol -> deque of first-window ranges

        # Cached results
        self._results: Dict[str, Dict[date, InPlayResultV2]] = {}

    # ══════════════════════════════════════════════════
    # Rolling baseline management
    # ══════════════════════════════════════════════════

    def _get_vol_baseline(self, symbol: str) -> float:
        """Return 20-day avg first-window volume for symbol. NaN if < 5 days."""
        buf = self._vol_baselines.get(symbol)
        if buf is None or len(buf) < 5:
            return NaN
        return sum(buf) / len(buf)

    def _get_range_baseline(self, symbol: str) -> float:
        """Return 20-day avg first-window range for symbol. NaN if < 5 days."""
        buf = self._range_baselines.get(symbol)
        if buf is None or len(buf) < 5:
            return NaN
        return sum(buf) / len(buf)

    def _record_day_baselines(self, symbol: str, first_window_vol: float,
                               first_window_range: float):
        """Record today's first-window volume and range into rolling buffers.

        Call AFTER computing scores so current day is excluded from its own baseline.
        """
        if symbol not in self._vol_baselines:
            self._vol_baselines[symbol] = deque(maxlen=20)
        if symbol not in self._range_baselines:
            self._range_baselines[symbol] = deque(maxlen=20)

        if not _isnan(first_window_vol) and first_window_vol > 0:
            self._vol_baselines[symbol].append(first_window_vol)
        if not _isnan(first_window_range) and first_window_range > 0:
            self._range_baselines[symbol].append(first_window_range)

    # ══════════════════════════════════════════════════
    # REPLAY PATH: batch precompute
    # ══════════════════════════════════════════════════

    def precompute_day(self, bars_by_symbol: Dict[str, List], spy_bars: List,
                       session_date: date, prior_closes: Dict[str, float] = None,
                       vol_baselines: Dict[str, float] = None,
                       range_baselines: Dict[str, float] = None):
        """Precompute V2 objective scores for all symbols on a given day.

        Args:
            bars_by_symbol: {symbol: [Bar, ...]} for this day
            spy_bars: SPY bars for this day
            session_date: the trading day
            prior_closes: {symbol: float} prior close prices
            vol_baselines: {symbol: float} externally supplied vol baselines (optional)
            range_baselines: {symbol: float} externally supplied range baselines (optional)
        """
        if prior_closes is None:
            prior_closes = {}
        if vol_baselines is None:
            vol_baselines = {}
        if range_baselines is None:
            range_baselines = {}

        # Extract SPY open and bar data
        spy_open = NaN
        spy_bars_by_hhmm = {}
        for b in spy_bars:
            hhmm = b.timestamp.hour * 100 + b.timestamp.minute
            if _isnan(spy_open) and hhmm >= 930:
                spy_open = b.open
            spy_bars_by_hhmm[hhmm] = b

        # For each symbol, compute raw features at provisional and confirmed cutoffs
        for symbol, bars in bars_by_symbol.items():
            if not bars:
                continue

            sym_open = NaN
            prior_close = prior_closes.get(symbol, NaN)
            bars_by_hhmm = {}

            for b in bars:
                hhmm = b.timestamp.hour * 100 + b.timestamp.minute
                if _isnan(sym_open) and hhmm >= 930:
                    sym_open = b.open
                bars_by_hhmm[hhmm] = b

            if _isnan(sym_open):
                continue

            # Get baselines — prefer external, fall back to internal rolling
            vol_bl = vol_baselines.get(symbol, self._get_vol_baseline(symbol))
            range_bl = range_baselines.get(symbol, self._get_range_baseline(symbol))

            # Compute features at provisional cutoff
            prov_features = self._compute_features_at_cutoff(
                sym_open, prior_close, bars_by_hhmm,
                spy_open, spy_bars_by_hhmm,
                self._provisional_hhmm, vol_bl, range_bl
            )

            # Compute features at confirmed cutoff
            conf_features = self._compute_features_at_cutoff(
                sym_open, prior_close, bars_by_hhmm,
                spy_open, spy_bars_by_hhmm,
                self._confirmed_hhmm, vol_bl, range_bl
            )

            # Compute objective scores
            prov_obj = _compute_objective_score(prov_features) if prov_features else None
            conf_obj = _compute_objective_score(conf_features) if conf_features else None

            # Record day baselines for future days (after scoring, so excluded from own baseline)
            if conf_features:
                self._record_day_baselines(
                    symbol,
                    conf_features.get("first_window_volume", NaN),
                    conf_features.get("first_window_range", NaN),
                )
            elif prov_features:
                self._record_day_baselines(
                    symbol,
                    prov_features.get("first_window_volume", NaN),
                    prov_features.get("first_window_range", NaN),
                )

            # Build result
            result = InPlayResultV2(session_date=session_date)

            # Provisional
            if prov_obj is not None:
                result.score_provisional = prov_obj["score_total"]
                result.is_ready_provisional = True
                # Pass/fail deferred to get_result() using per-strategy hard floor
                result.passed_provisional = False  # placeholder — resolved at query time
                if prov_features:
                    result.gap_abs_pct = prov_features.get("gap_abs_pct", NaN)
                    result.abs_move_from_open_pct = prov_features.get("abs_move_from_open_pct", NaN)
                    result.abs_rs_vs_spy_pct = prov_features.get("abs_rs_vs_spy_pct", NaN)
                    result.rvol = prov_features.get("rvol", NaN)
                    result.range_expansion = prov_features.get("range_expansion", NaN)

            # Confirmed
            if conf_obj is not None:
                result.score_confirmed = conf_obj["score_total"]
                result.is_ready_confirmed = True
                result.passed_confirmed = False  # placeholder — resolved at query time
                if conf_features:
                    result.gap_abs_pct = conf_features.get("gap_abs_pct", NaN)
                    result.abs_move_from_open_pct = conf_features.get("abs_move_from_open_pct", NaN)
                    result.abs_rs_vs_spy_pct = conf_features.get("abs_rs_vs_spy_pct", NaN)
                    result.rvol = conf_features.get("rvol", NaN)
                    result.range_expansion = conf_features.get("range_expansion", NaN)
                # Store component audit
                result.component_move_score = conf_obj["move_score"]
                result.component_rs_score = conf_obj["rs_score"]
                result.component_gap_score = conf_obj["gap_score"]
                result.component_range_score = conf_obj["range_score"]
                result.component_rvol_score = conf_obj["rvol_score"]
            elif prov_obj is not None:
                # Only provisional available — store its components
                result.component_move_score = prov_obj["move_score"]
                result.component_rs_score = prov_obj["rs_score"]
                result.component_gap_score = prov_obj["gap_score"]
                result.component_range_score = prov_obj["range_score"]
                result.component_rvol_score = prov_obj["rvol_score"]

            # Active score — use best available stage
            if result.is_ready_confirmed:
                result.active_score_kind = "CONFIRMED"
                result.active_score = result.score_confirmed
                result.score_raw_current = result.score_confirmed
                result.cutoff_hhmm = self._confirmed_hhmm
                result.reason_flags = "CONFIRMED"
            elif result.is_ready_provisional:
                result.active_score_kind = "PROVISIONAL"
                result.active_score = result.score_provisional
                result.score_raw_current = result.score_provisional
                result.cutoff_hhmm = self._provisional_hhmm
                result.reason_flags = "PROVISIONAL"
            else:
                result.active_score_kind = "NONE"
                result.reason_flags = "NOT_READY"

            if symbol not in self._results:
                self._results[symbol] = {}
            self._results[symbol][session_date] = result

    def get_result(self, symbol: str, session_date: date,
                   hhmm: int = 9999, hard_floor: float = 5.0) -> InPlayResultV2:
        """Get V2 result for a symbol-day at a given time.

        Pass/fail is computed here using the provided hard_floor.
        For replay: returns the precomputed result, respecting time staging.
        """
        stored = (self._results.get(symbol, {}).get(session_date)
                  or InPlayResultV2(session_date=session_date,
                                    reason_flags="NOT_EVALUATED"))

        # Time-gated access
        if hhmm < self._provisional_hhmm:
            return InPlayResultV2(
                session_date=session_date,
                active_score_kind="NONE",
                reason_flags="NOT_READY_PROVISIONAL",
            )
        elif hhmm < self._confirmed_hhmm:
            # Provisional only
            score = stored.score_provisional
            passed = (not _isnan(score) and score >= hard_floor)
            return InPlayResultV2(
                session_date=session_date,
                score_provisional=score,
                is_ready_provisional=stored.is_ready_provisional,
                passed_provisional=passed,
                active_score_kind="PROVISIONAL",
                active_score=score,
                active_passed=passed,
                score_raw_current=score,
                gap_abs_pct=stored.gap_abs_pct,
                abs_move_from_open_pct=stored.abs_move_from_open_pct,
                abs_rs_vs_spy_pct=stored.abs_rs_vs_spy_pct,
                rvol=stored.rvol,
                range_expansion=stored.range_expansion,
                cutoff_hhmm=self._provisional_hhmm,
                component_move_score=stored.component_move_score,
                component_rs_score=stored.component_rs_score,
                component_gap_score=stored.component_gap_score,
                component_range_score=stored.component_range_score,
                component_rvol_score=stored.component_rvol_score,
                reason_flags="PASS_PROVISIONAL" if passed else "FAIL_THRESHOLD",
            )
        else:
            # Confirmed (or best available)
            score = stored.active_score
            passed = (not _isnan(score) and score >= hard_floor)

            # Return a copy with pass/fail resolved against hard_floor
            return InPlayResultV2(
                session_date=session_date,
                score_provisional=stored.score_provisional,
                score_confirmed=stored.score_confirmed,
                score_raw_current=stored.score_raw_current,
                passed_provisional=stored.passed_provisional,
                passed_confirmed=passed,
                is_ready_provisional=stored.is_ready_provisional,
                is_ready_confirmed=stored.is_ready_confirmed,
                active_score_kind=stored.active_score_kind,
                active_score=score,
                active_passed=passed,
                gap_abs_pct=stored.gap_abs_pct,
                abs_move_from_open_pct=stored.abs_move_from_open_pct,
                abs_rs_vs_spy_pct=stored.abs_rs_vs_spy_pct,
                rvol=stored.rvol,
                range_expansion=stored.range_expansion,
                cutoff_hhmm=stored.cutoff_hhmm,
                component_move_score=stored.component_move_score,
                component_rs_score=stored.component_rs_score,
                component_gap_score=stored.component_gap_score,
                component_range_score=stored.component_range_score,
                component_rvol_score=stored.component_rvol_score,
                reason_flags="PASS_CONFIRMED" if passed else "FAIL_THRESHOLD",
            )

    def get_result_for_replay(self, symbol: str, session_date: date,
                               hard_floor: float = 5.0) -> InPlayResultV2:
        """Convenience: get the confirmed result (or best available) for replay gating."""
        return self.get_result(symbol, session_date, hhmm=9999, hard_floor=hard_floor)

    # ══════════════════════════════════════════════════
    # LIVE PATH: incremental updates
    # ══════════════════════════════════════════════════

    def update_live(self, symbol: str, session_date: date,
                    sym_open: float, prior_close: float,
                    current_close: float, current_hhmm: int,
                    spy_open: float, spy_current_close: float,
                    session_high: float = NaN, session_low: float = NaN,
                    first_window_volume: float = NaN,
                    vol_baseline: float = NaN,
                    range_baseline: float = NaN,
                    hard_floor: float = 5.0) -> InPlayResultV2:
        """Compute objective score for a single symbol in real time.

        Called on each bar. No cross-sectional logic.

        Args:
            session_high: high through current cutoff
            session_low: low through current cutoff
            first_window_volume: cumulative volume through cutoff
            vol_baseline: 20-day avg first-window volume
            range_baseline: 20-day avg first-window range
            hard_floor: per-strategy objective score floor
        """
        result = InPlayResultV2(session_date=session_date)

        # Compute raw features
        features = self._compute_features_direct(
            sym_open, prior_close, current_close,
            spy_open, spy_current_close,
            session_high, session_low,
            first_window_volume, vol_baseline, range_baseline
        )
        if features is None:
            result.reason_flags = "MISSING_DATA"
            return result

        # Compute objective score
        obj = _compute_objective_score(features)
        score = obj["score_total"]

        result.gap_abs_pct = features["gap_abs_pct"]
        result.abs_move_from_open_pct = features["abs_move_from_open_pct"]
        result.abs_rs_vs_spy_pct = features["abs_rs_vs_spy_pct"]
        result.rvol = features.get("rvol", NaN)
        result.range_expansion = features.get("range_expansion", NaN)
        result.cutoff_hhmm = current_hhmm
        result.component_move_score = obj["move_score"]
        result.component_rs_score = obj["rs_score"]
        result.component_gap_score = obj["gap_score"]
        result.component_range_score = obj["range_score"]
        result.component_rvol_score = obj["rvol_score"]

        passed = score >= hard_floor

        # Stage assignment
        if current_hhmm < self._provisional_hhmm:
            result.active_score_kind = "NONE"
            result.reason_flags = "NOT_READY_PROVISIONAL"
        elif current_hhmm < self._confirmed_hhmm:
            result.score_provisional = score
            result.is_ready_provisional = True
            result.passed_provisional = passed
            result.active_score_kind = "PROVISIONAL"
            result.active_score = score
            result.active_passed = passed
            result.score_raw_current = score
            result.reason_flags = "PASS_PROVISIONAL" if passed else "FAIL_THRESHOLD"
        else:
            result.score_confirmed = score
            result.is_ready_confirmed = True
            result.passed_confirmed = passed
            result.is_ready_provisional = True
            result.active_score_kind = "CONFIRMED"
            result.active_score = score
            result.active_passed = passed
            result.score_raw_current = score
            result.reason_flags = "PASS_CONFIRMED" if passed else "FAIL_THRESHOLD"

        return result

    # ══════════════════════════════════════════════════
    # CORE: feature computation
    # ══════════════════════════════════════════════════

    def _compute_features_at_cutoff(self, sym_open, prior_close, bars_by_hhmm,
                                     spy_open, spy_bars_by_hhmm, cutoff_hhmm,
                                     vol_baseline=NaN, range_baseline=NaN):
        """Compute raw features for a symbol at a given cutoff time.

        Includes volume, range, RVOL, and range-expansion metrics.
        """
        # Find the latest bar at or before cutoff
        current_close = NaN
        session_high = NaN
        session_low = NaN
        first_window_volume = 0.0

        sorted_hhmms = sorted(bars_by_hhmm.keys())
        for hhmm in sorted_hhmms:
            if hhmm > cutoff_hhmm:
                break
            if hhmm < 930:
                continue
            b = bars_by_hhmm[hhmm]
            current_close = b.close
            if _isnan(session_high) or b.high > session_high:
                session_high = b.high
            if _isnan(session_low) or b.low < session_low:
                session_low = b.low
            first_window_volume += getattr(b, 'volume', 0) or 0

        if _isnan(current_close):
            return None

        # Find SPY close at cutoff
        spy_close = NaN
        for hhmm in sorted(spy_bars_by_hhmm.keys(), reverse=True):
            if hhmm <= cutoff_hhmm:
                spy_close = spy_bars_by_hhmm[hhmm].close
                break

        # First-window range
        first_window_range = NaN
        if not _isnan(session_high) and not _isnan(session_low):
            first_window_range = session_high - session_low

        # RVOL and range expansion
        rvol = NaN
        if not _isnan(vol_baseline) and vol_baseline > 0 and first_window_volume > 0:
            rvol = first_window_volume / vol_baseline

        range_exp = NaN
        if (not _isnan(range_baseline) and range_baseline > 0 and
                not _isnan(first_window_range) and first_window_range > 0):
            range_exp = first_window_range / range_baseline

        features = self._compute_features_direct(
            sym_open, prior_close, current_close,
            spy_open, spy_close,
            session_high, session_low,
            first_window_volume, vol_baseline, range_baseline
        )
        if features is not None:
            features["first_window_volume"] = first_window_volume
            features["first_window_range"] = first_window_range if not _isnan(first_window_range) else 0.0
        return features

    @staticmethod
    def _compute_features_direct(sym_open, prior_close, current_close,
                                  spy_open, spy_current_close,
                                  session_high=NaN, session_low=NaN,
                                  first_window_volume=NaN,
                                  vol_baseline=NaN, range_baseline=NaN):
        """Compute all V2 features from raw inputs.

        Returns dict with all raw metrics including rvol and range_expansion.
        Returns None if essential data is missing.
        """
        if _isnan(sym_open) or sym_open <= 0:
            return None
        if _isnan(current_close) or current_close <= 0:
            return None

        # A. gap_abs_pct
        gap_abs_pct = 0.0
        if not _isnan(prior_close) and prior_close > 0:
            gap_abs_pct = abs((sym_open - prior_close) / prior_close) * 100.0

        # B. abs_move_from_open_pct
        stock_move_pct = ((current_close - sym_open) / sym_open) * 100.0
        abs_move_from_open_pct = abs(stock_move_pct)

        # C. abs_rs_vs_spy_pct
        abs_rs_vs_spy_pct = 0.0
        if (not _isnan(spy_open) and spy_open > 0 and
                not _isnan(spy_current_close) and spy_current_close > 0):
            spy_pct = ((spy_current_close - spy_open) / spy_open) * 100.0
            abs_rs_vs_spy_pct = abs(stock_move_pct - spy_pct)
        else:
            abs_rs_vs_spy_pct = abs_move_from_open_pct

        # D. First-window range
        first_window_range = NaN
        if not _isnan(session_high) and not _isnan(session_low):
            first_window_range = session_high - session_low

        # E. RVOL
        rvol = NaN
        if (not _isnan(vol_baseline) and vol_baseline > 0 and
                not _isnan(first_window_volume) and first_window_volume > 0):
            rvol = first_window_volume / vol_baseline

        # F. Range expansion
        range_expansion = NaN
        if (not _isnan(range_baseline) and range_baseline > 0 and
                not _isnan(first_window_range) and first_window_range > 0):
            range_expansion = first_window_range / range_baseline

        return {
            "gap_abs_pct": gap_abs_pct,
            "abs_move_from_open_pct": abs_move_from_open_pct,
            "abs_rs_vs_spy_pct": abs_rs_vs_spy_pct,
            "rvol": rvol,
            "range_expansion": range_expansion,
            "first_window_volume": first_window_volume if not _isnan(first_window_volume) else 0.0,
            "first_window_range": first_window_range if not _isnan(first_window_range) else 0.0,
        }

    # ══════════════════════════════════════════════════
    # LEGACY COMPATIBILITY
    # ══════════════════════════════════════════════════

    def is_in_play(self, symbol: str, session_date: date,
                   hard_floor: float = 5.0) -> tuple:
        """Legacy-compatible wrapper. Returns (passed, score)."""
        result = self.get_result_for_replay(symbol, session_date, hard_floor=hard_floor)
        return result.active_passed, result.active_score if not _isnan(result.active_score) else 0.0
