"""
InPlayProxyV2 — Direction-agnostic two-stage base gate.

Replaces the primitive V1 gate with a percentile-ranked, rolling score system.

Score = mean(rank(gap_abs_pct), rank(abs_move_from_open_pct), rank(abs_rs_vs_spy_pct))

Two stages:
  Provisional: available at 9:40 ET (uses first 10 minutes of data)
  Confirmed:   available at 10:00 ET (uses first 30 minutes, then rolls with each bar)

Key properties:
  - Direction-agnostic (uses absolute values)
  - Never zeros failed names (raw score always preserved)
  - Rolling confirmed score updates after 10:00
  - Cross-sectional percentile rank across same-day universe
  - Unified pass/fail logic (one source of truth)
"""

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

NaN = float('nan')
_isnan = math.isnan


@dataclass
class InPlayResultV2:
    """Output of V2 evaluation. Preserves raw scores even on failure."""
    session_date: date = None

    # Scores (0.0 - 1.0 percentile-based)
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
    cutoff_hhmm: int = 0

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


class InPlayProxyV2:
    """Direction-agnostic two-stage base gate with cross-sectional percentile ranking.

    Usage (replay):
        v2 = InPlayProxyV2(cfg)
        v2.precompute_day(bars_by_symbol, spy_bars, session_date)
        result = v2.get_result(symbol, session_date, hhmm)

    Usage (live):
        v2 = InPlayProxyV2(cfg)
        v2.update_symbol(symbol, bar, spy_bar)  # called on each bar
        result = v2.get_live_result(symbol)
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._provisional_hhmm = getattr(cfg, 'ip_v2_provisional_hhmm', 940)
        self._confirmed_hhmm = getattr(cfg, 'ip_v2_confirmed_hhmm', 1000)
        self._threshold_provisional = getattr(cfg, 'ip_v2_threshold_provisional', 0.80)
        self._threshold_confirmed = getattr(cfg, 'ip_v2_threshold_confirmed', 0.80)
        self._recompute_each_bar = getattr(cfg, 'ip_v2_recompute_confirmed_each_bar', True)

        # Per-symbol per-day raw feature storage
        # {symbol: {date: {"open": float, "prior_close": float, "bars": [...]}}}
        self._symbol_data: Dict[str, dict] = {}

        # SPY data for RS computation
        self._spy_data: dict = {}  # {date: {"open": float, "bars": [...]}}

        # Cached results
        self._results: Dict[str, Dict[date, InPlayResultV2]] = {}

        # Universe for cross-sectional ranking
        self._universe_features: Dict[date, Dict[str, dict]] = {}

    # ══════════════════════════════════════════════════
    # REPLAY PATH: batch precompute
    # ══════════════════════════════════════════════════

    def precompute_day(self, bars_by_symbol: Dict[str, List], spy_bars: List,
                       session_date: date, prior_closes: Dict[str, float] = None):
        """Precompute V2 scores for all symbols on a given day.

        Args:
            bars_by_symbol: {symbol: [Bar, ...]} for this day
            spy_bars: SPY bars for this day
            session_date: the trading day
            prior_closes: {symbol: float} prior close prices
        """
        if prior_closes is None:
            prior_closes = {}

        # Extract SPY open and bar data
        spy_open = NaN
        spy_bars_by_hhmm = {}
        for b in spy_bars:
            hhmm = b.timestamp.hour * 100 + b.timestamp.minute
            if _isnan(spy_open) and hhmm >= 930:
                spy_open = b.open
            spy_bars_by_hhmm[hhmm] = b

        # For each symbol, compute raw features at provisional and confirmed cutoffs
        universe_provisional = {}
        universe_confirmed = {}

        for symbol, bars in bars_by_symbol.items():
            if not bars:
                continue

            # Find open price
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

            # Compute features at provisional cutoff (9:40)
            prov_features = self._compute_features_at_cutoff(
                sym_open, prior_close, bars_by_hhmm,
                spy_open, spy_bars_by_hhmm,
                self._provisional_hhmm
            )
            if prov_features is not None:
                universe_provisional[symbol] = prov_features

            # Compute features at confirmed cutoff (10:00)
            conf_features = self._compute_features_at_cutoff(
                sym_open, prior_close, bars_by_hhmm,
                spy_open, spy_bars_by_hhmm,
                self._confirmed_hhmm
            )
            if conf_features is not None:
                universe_confirmed[symbol] = conf_features

            # Store for later rolling updates
            self._symbol_data[symbol] = {
                "open": sym_open,
                "prior_close": prior_close,
                "bars_by_hhmm": bars_by_hhmm,
            }

        self._spy_data[session_date] = {
            "open": spy_open,
            "bars_by_hhmm": spy_bars_by_hhmm,
        }

        # Compute cross-sectional percentile ranks
        prov_scores = self._rank_universe(universe_provisional)
        conf_scores = self._rank_universe(universe_confirmed)

        # Store results
        for symbol in bars_by_symbol:
            result = InPlayResultV2(session_date=session_date)

            # Provisional
            if symbol in prov_scores:
                result.score_provisional = prov_scores[symbol]
                result.is_ready_provisional = True
                result.passed_provisional = result.score_provisional >= self._threshold_provisional
                feat = universe_provisional[symbol]
                result.gap_abs_pct = feat["gap_abs_pct"]

            # Confirmed
            if symbol in conf_scores:
                result.score_confirmed = conf_scores[symbol]
                result.is_ready_confirmed = True
                result.passed_confirmed = result.score_confirmed >= self._threshold_confirmed
                feat = universe_confirmed[symbol]
                result.abs_move_from_open_pct = feat["abs_move_from_open_pct"]
                result.abs_rs_vs_spy_pct = feat["abs_rs_vs_spy_pct"]

            # Active score
            if result.is_ready_confirmed:
                result.active_score_kind = "CONFIRMED"
                result.active_score = result.score_confirmed
                result.active_passed = result.passed_confirmed
                result.score_raw_current = result.score_confirmed
                result.cutoff_hhmm = self._confirmed_hhmm
                result.reason_flags = "PASS_CONFIRMED" if result.passed_confirmed else "FAIL_THRESHOLD"
            elif result.is_ready_provisional:
                result.active_score_kind = "PROVISIONAL"
                result.active_score = result.score_provisional
                result.active_passed = result.passed_provisional
                result.score_raw_current = result.score_provisional
                result.cutoff_hhmm = self._provisional_hhmm
                result.reason_flags = "PASS_PROVISIONAL" if result.passed_provisional else "FAIL_THRESHOLD"
            else:
                result.active_score_kind = "NONE"
                result.reason_flags = "NOT_READY"

            if symbol not in self._results:
                self._results[symbol] = {}
            self._results[symbol][session_date] = result

    def get_result(self, symbol: str, session_date: date,
                   hhmm: int = 9999) -> InPlayResultV2:
        """Get V2 result for a symbol-day at a given time.

        For replay: returns the precomputed result, respecting time staging.
        """
        result = (self._results.get(symbol, {}).get(session_date)
                  or InPlayResultV2(session_date=session_date,
                                    reason_flags="NOT_EVALUATED"))

        # Time-gated access
        if hhmm < self._provisional_hhmm:
            # Too early — nothing ready
            return InPlayResultV2(
                session_date=session_date,
                active_score_kind="NONE",
                reason_flags="NOT_READY_PROVISIONAL",
            )
        elif hhmm < self._confirmed_hhmm:
            # Provisional only
            out = InPlayResultV2(
                session_date=session_date,
                score_provisional=result.score_provisional,
                is_ready_provisional=result.is_ready_provisional,
                passed_provisional=result.passed_provisional,
                active_score_kind="PROVISIONAL",
                active_score=result.score_provisional,
                active_passed=result.passed_provisional,
                score_raw_current=result.score_provisional,
                gap_abs_pct=result.gap_abs_pct,
                cutoff_hhmm=self._provisional_hhmm,
                reason_flags="PASS_PROVISIONAL" if result.passed_provisional else "FAIL_THRESHOLD",
            )
            if not result.is_ready_provisional:
                out.reason_flags = "NOT_READY_PROVISIONAL"
            return out
        else:
            # Confirmed (or rolling update)
            return result

    def get_result_for_replay(self, symbol: str, session_date: date) -> InPlayResultV2:
        """Convenience: get the confirmed result (or best available) for replay gating."""
        return self.get_result(symbol, session_date, hhmm=9999)

    # ══════════════════════════════════════════════════
    # LIVE PATH: incremental updates
    # ══════════════════════════════════════════════════

    def update_live(self, symbol: str, session_date: date,
                    sym_open: float, prior_close: float,
                    current_close: float, current_hhmm: int,
                    spy_open: float, spy_current_close: float,
                    universe_features: Dict[str, dict] = None) -> InPlayResultV2:
        """Update V2 score for a single symbol in real time.

        Called on each bar. Computes features and ranks against universe.

        Args:
            universe_features: {sym: {"gap_abs_pct":..., "abs_move_from_open_pct":..., "abs_rs_vs_spy_pct":...}}
                If provided, used for cross-sectional ranking. Otherwise uses cached universe.
        """
        result = InPlayResultV2(session_date=session_date)

        # Compute raw features
        features = self._compute_features_direct(
            sym_open, prior_close, current_close,
            spy_open, spy_current_close
        )
        if features is None:
            result.reason_flags = "MISSING_DATA"
            return result

        result.gap_abs_pct = features["gap_abs_pct"]
        result.abs_move_from_open_pct = features["abs_move_from_open_pct"]
        result.abs_rs_vs_spy_pct = features["abs_rs_vs_spy_pct"]
        result.cutoff_hhmm = current_hhmm

        # If universe provided, rank against it
        if universe_features:
            scores = self._rank_universe(universe_features)
            raw_score = scores.get(symbol, NaN)
        else:
            # Fallback: use raw features as rough score (no ranking available)
            # This is a degenerate case — log a warning
            raw_score = NaN

        # Stage assignment
        if current_hhmm < self._provisional_hhmm:
            result.active_score_kind = "NONE"
            result.reason_flags = "NOT_READY_PROVISIONAL"
        elif current_hhmm < self._confirmed_hhmm:
            result.score_provisional = raw_score
            result.is_ready_provisional = True
            result.passed_provisional = (not _isnan(raw_score) and
                                         raw_score >= self._threshold_provisional)
            result.active_score_kind = "PROVISIONAL"
            result.active_score = raw_score
            result.active_passed = result.passed_provisional
            result.score_raw_current = raw_score
            result.reason_flags = ("PASS_PROVISIONAL" if result.passed_provisional
                                   else "FAIL_THRESHOLD")
        else:
            result.score_confirmed = raw_score
            result.is_ready_confirmed = True
            result.passed_confirmed = (not _isnan(raw_score) and
                                       raw_score >= self._threshold_confirmed)
            # Also set provisional if we have it cached
            result.is_ready_provisional = True
            result.active_score_kind = "CONFIRMED"
            result.active_score = raw_score
            result.active_passed = result.passed_confirmed
            result.score_raw_current = raw_score
            result.reason_flags = ("PASS_CONFIRMED" if result.passed_confirmed
                                   else "FAIL_THRESHOLD")

        return result

    # ══════════════════════════════════════════════════
    # CORE: feature computation and ranking
    # ══════════════════════════════════════════════════

    def _compute_features_at_cutoff(self, sym_open, prior_close, bars_by_hhmm,
                                     spy_open, spy_bars_by_hhmm, cutoff_hhmm):
        """Compute raw features for a symbol at a given cutoff time."""
        # Find the latest bar at or before cutoff
        current_close = NaN
        for hhmm in sorted(bars_by_hhmm.keys(), reverse=True):
            if hhmm <= cutoff_hhmm:
                current_close = bars_by_hhmm[hhmm].close
                break
        if _isnan(current_close):
            return None

        # Find SPY close at cutoff
        spy_close = NaN
        for hhmm in sorted(spy_bars_by_hhmm.keys(), reverse=True):
            if hhmm <= cutoff_hhmm:
                spy_close = spy_bars_by_hhmm[hhmm].close
                break

        return self._compute_features_direct(
            sym_open, prior_close, current_close,
            spy_open, spy_close
        )

    @staticmethod
    def _compute_features_direct(sym_open, prior_close, current_close,
                                  spy_open, spy_current_close):
        """Compute the three core V2 features from raw prices.

        All outputs in percent units.
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
        abs_move_from_open_pct = abs((current_close - sym_open) / sym_open) * 100.0

        # C. abs_rs_vs_spy_pct
        abs_rs_vs_spy_pct = 0.0
        if (not _isnan(spy_open) and spy_open > 0 and
                not _isnan(spy_current_close) and spy_current_close > 0):
            stock_pct = ((current_close - sym_open) / sym_open) * 100.0
            spy_pct = ((spy_current_close - spy_open) / spy_open) * 100.0
            abs_rs_vs_spy_pct = abs(stock_pct - spy_pct)
        else:
            # If SPY missing, use abs_move_from_open as fallback for RS
            abs_rs_vs_spy_pct = abs_move_from_open_pct

        return {
            "gap_abs_pct": gap_abs_pct,
            "abs_move_from_open_pct": abs_move_from_open_pct,
            "abs_rs_vs_spy_pct": abs_rs_vs_spy_pct,
        }

    @staticmethod
    def _rank_universe(universe_features: Dict[str, dict]) -> Dict[str, float]:
        """Cross-sectional percentile ranking across the universe.

        For each of the three features, rank all symbols, convert to percentile,
        then average the three percentiles.

        Returns: {symbol: score} where score is 0.0 to 1.0.
        """
        symbols = list(universe_features.keys())
        n = len(symbols)
        if n == 0:
            return {}
        if n == 1:
            # Single symbol gets 0.5 (median)
            return {symbols[0]: 0.50}

        # Extract feature vectors
        features = ["gap_abs_pct", "abs_move_from_open_pct", "abs_rs_vs_spy_pct"]
        percentiles = {sym: [] for sym in symbols}

        for feat_name in features:
            # Get values, handling NaN
            vals = [(sym, universe_features[sym].get(feat_name, 0.0)) for sym in symbols]
            # Sort by value
            vals.sort(key=lambda x: x[1])
            # Assign percentile rank (0 to 1)
            for rank_idx, (sym, _) in enumerate(vals):
                pct = rank_idx / (n - 1) if n > 1 else 0.5
                percentiles[sym].append(pct)

        # Average percentiles
        scores = {}
        for sym in symbols:
            pcts = percentiles[sym]
            scores[sym] = sum(pcts) / len(pcts) if pcts else 0.0

        return scores

    # ══════════════════════════════════════════════════
    # LEGACY COMPATIBILITY
    # ══════════════════════════════════════════════════

    def is_in_play(self, symbol: str, session_date: date) -> tuple:
        """Legacy-compatible wrapper. Returns (passed, score)."""
        result = self.get_result_for_replay(symbol, session_date)
        return result.active_passed, result.active_score if not _isnan(result.active_score) else 0.0
