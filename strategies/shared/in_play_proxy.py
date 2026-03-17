"""
InPlayProxy — Day-level in-play qualification using open-auction proxies.
Three modes: list_only (dashboard), proxy_only (computed, default for backtest), hybrid (either).

Provides both batch precompute (replay) and incremental evaluate_session (live).
"""

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ...models import Bar, NaN

_isnan = math.isnan


@dataclass
class DayOpenStats:
    """Per-symbol per-day open-auction statistics."""
    gap_pct: float = 0.0        # gap from prior close
    rvol: float = 0.0           # volume of first N bars / 20-day avg of first N bars
    dolvol: float = 0.0         # dollar volume in first N bars
    range_expansion: float = 0.0  # first N bars range / 20-day avg first N bars range
    prior_close: float = NaN
    open_price: float = NaN
    score: float = 0.0          # composite 0-10


@dataclass
class SessionSnapshot:
    """Input container for evaluate_session(). Extensible — add fields as needed.

    Used by both replay (internally, during precompute) and live (externally,
    after first N bars of the session). Ensures identical scoring logic.
    """
    symbol: str
    session_date: date
    open_bars: List[Bar]                 # first N bars of the session
    prev_close: float                    # prior regular-session close
    vol_baseline: float = 0.0            # 20-day avg of first-N-bar volume (0 = not ready)
    range_baseline: float = 0.0          # 20-day avg of first-N-bar range (0 = not ready)
    vol_baseline_depth: int = 0          # how many sessions in the baseline (0-20)
    range_baseline_depth: int = 0        # how many sessions in the baseline (0-20)
    # Future fields (add without breaking callsites):
    # rs_vs_spy: float = 0.0
    # prior_day_displacement: bool = False


@dataclass
class InPlayResult:
    """Output of evaluate_session(). Contains pass/fail, score, and all inputs."""
    passed: bool
    score: float
    stats: DayOpenStats
    data_status: str = "FULL"            # FULL / PARTIAL / NEW
    pass_gap: bool = False
    pass_rvol: bool = False
    pass_dolvol: bool = False
    reason: str = ""                     # human-readable gate result


class InPlayProxy:
    """Day-level in-play qualification for strategy pipeline."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._day_stats: Dict[str, Dict[date, DayOpenStats]] = {}

    # ── Shared core: computes stats from bars + baselines ──

    def _compute_open_stats(self, open_bars: List[Bar], prev_close: float,
                            vol_baseline: float, range_baseline: float) -> DayOpenStats:
        """Core stat computation used by both precompute() and evaluate_session().

        Args:
            open_bars: first N bars of the session
            prev_close: prior regular-session close
            vol_baseline: 20-day average first-N-bar volume (0 = not ready)
            range_baseline: 20-day average first-N-bar range (0 = not ready)
        Returns:
            DayOpenStats with all fields populated
        """
        open_price = open_bars[0].open
        first_n_vol = sum(b.volume for b in open_bars)
        first_n_high = max(b.high for b in open_bars)
        first_n_low = min(b.low for b in open_bars)
        first_n_range = first_n_high - first_n_low
        first_n_dolvol = sum(
            b.volume * (b.high + b.low + b.close) / 3.0 for b in open_bars
        )

        # Gap from prior close
        gap_pct = 0.0
        if not _isnan(prev_close) and prev_close > 0:
            gap_pct = (open_price - prev_close) / prev_close

        # RVOL: first-N-bars volume / baseline avg
        rvol = 0.0
        if vol_baseline > 0:
            rvol = first_n_vol / vol_baseline

        # Range expansion: first-N-bars range / baseline avg
        range_exp = 0.0
        if range_baseline > 0:
            range_exp = first_n_range / range_baseline

        score = self._compute_score(abs(gap_pct), rvol, first_n_dolvol, range_exp)

        return DayOpenStats(
            gap_pct=gap_pct,
            rvol=rvol,
            dolvol=first_n_dolvol,
            range_expansion=range_exp,
            prior_close=prev_close,
            open_price=open_price,
            score=score,
        )

    # ── Incremental evaluation (live pipeline) ──

    def evaluate_session(self, snapshot: 'SessionSnapshot') -> 'InPlayResult':
        """Evaluate a single session snapshot for in-play qualification.

        Called by the live pipeline after first N bars of a session.
        Uses the same scoring logic as precompute() via _compute_open_stats().

        Args:
            snapshot: SessionSnapshot with open_bars, prev_close, baselines
        Returns:
            InPlayResult with pass/fail, score, stats, and diagnostics
        """
        cfg = self.cfg

        if not snapshot.open_bars:
            return InPlayResult(
                passed=False, score=0.0, stats=DayOpenStats(),
                data_status="NEW", reason="no_open_bars")

        # Determine data readiness
        min_depth = 5  # need at least 5 sessions for baseline
        if snapshot.vol_baseline_depth < min_depth:
            data_status = "NEW" if snapshot.vol_baseline_depth == 0 else "PARTIAL"
        elif snapshot.vol_baseline_depth < 20:
            data_status = "PARTIAL"
        else:
            data_status = "FULL"

        # Compute stats using shared core
        stats = self._compute_open_stats(
            open_bars=snapshot.open_bars,
            prev_close=snapshot.prev_close,
            vol_baseline=snapshot.vol_baseline,
            range_baseline=snapshot.range_baseline,
        )

        # Apply pass/fail thresholds (same as is_in_play)
        gap_min = cfg.get(cfg.ip_gap_min)
        rvol_min = cfg.get(cfg.ip_rvol_min)
        dolvol_min = cfg.get(cfg.ip_dolvol_min)

        pass_gap = abs(stats.gap_pct) >= gap_min
        pass_rvol = stats.rvol >= rvol_min
        pass_dolvol = stats.dolvol >= dolvol_min

        # Hard gate — rvol only (gap + dolvol removed; universe is pre-curated, no microcaps)
        passed = pass_rvol

        # Build reason string
        fails = []
        if not pass_rvol:
            fails.append(f"rvol={stats.rvol:.2f}<{rvol_min}")

        gap_note = f"gap={abs(stats.gap_pct):.3f}" if not pass_gap else ""

        if passed:
            reason = f"PASS(score={stats.score:.1f})"
            if gap_note:
                reason += f"[{gap_note}<{gap_min},no_gate]"
        else:
            reason = "FAIL(" + ",".join(fails) + ")"

        # If baselines not ready, RVOL check is unreliable — note in reason
        if data_status != "FULL" and not pass_rvol:
            reason += f"[baseline_depth={snapshot.vol_baseline_depth}]"

        # Cache the result for this symbol-day
        if snapshot.symbol not in self._day_stats:
            self._day_stats[snapshot.symbol] = {}
        self._day_stats[snapshot.symbol][snapshot.session_date] = stats

        return InPlayResult(
            passed=passed,
            score=stats.score,
            stats=stats,
            data_status=data_status,
            pass_gap=pass_gap,
            pass_rvol=pass_rvol,
            pass_dolvol=pass_dolvol,
            reason=reason,
        )

    # ── Batch precompute (replay pipeline) ──

    def precompute(self, symbol: str, bars: List[Bar]):
        """Build per-day open stats for a symbol from its bar data.

        Uses _compute_open_stats() for each day — same scoring logic as
        evaluate_session(). Rolling buffers built internally.
        """
        if not bars:
            return

        cfg = self.cfg
        first_n = cfg.get(cfg.ip_first_n_bars)  # 3 for 5min, 15 for 1min

        # Group bars by date
        days: Dict[date, List[Bar]] = defaultdict(list)
        for b in bars:
            days[b.timestamp.date()].append(b)

        sorted_dates = sorted(days.keys())
        stats: Dict[date, DayOpenStats] = {}

        # Rolling buffers for 20-day averages
        vol_buf = deque(maxlen=20)     # first-N-bars total volume
        range_buf = deque(maxlen=20)   # first-N-bars range

        prev_close = NaN

        for d in sorted_dates:
            day_bars = days[d]
            if len(day_bars) < first_n:
                # Still track prev_close
                if day_bars:
                    prev_close = day_bars[-1].close
                continue

            open_bars = day_bars[:first_n]

            # Compute baselines from rolling buffers
            vol_baseline = 0.0
            if len(vol_buf) >= 5:
                vol_baseline = sum(vol_buf) / len(vol_buf)

            range_baseline = 0.0
            if len(range_buf) >= 5:
                range_baseline = sum(range_buf) / len(range_buf)

            # Use shared core
            day_stats = self._compute_open_stats(
                open_bars, prev_close, vol_baseline, range_baseline)

            stats[d] = day_stats

            # Update rolling buffers AFTER computing stats (same as before)
            first_n_vol = sum(b.volume for b in open_bars)
            first_n_range = max(b.high for b in open_bars) - min(b.low for b in open_bars)
            vol_buf.append(first_n_vol)
            range_buf.append(first_n_range)

            prev_close = day_bars[-1].close

        self._day_stats[symbol] = stats

    def _compute_score(self, abs_gap: float, rvol: float,
                       dolvol: float, range_exp: float) -> float:
        """Composite in-play score 0-7.

        Components (v3 — gap removed 2026-03-17):
          Gap %:           removed  (no independent signal after range boost; hard gate still filters)
          RVOL:            0-3 pts  (validated as correctly weighted)
          Range expansion: 0-4 pts  (strongest PF predictor)
          Dollar volume:   removed  (no discrimination, 96% of trades >$10M)

        Max theoretical = 7.  Gap hard-gate (min 0.5-1%) still filters
        flat-open stocks — only the scoring contribution is removed.
        """
        score = 0.0

        # Gap component: REMOVED from score (hard gate still enforced)

        # RVOL component (0-3) — validated: higher RVOL = better PF
        if rvol >= 5.0:
            score += 3.0
        elif rvol >= 3.0:
            score += 2.0
        elif rvol >= 2.0:
            score += 1.5
        elif rvol >= 1.5:
            score += 1.0

        # Dollar volume: REMOVED (no discrimination power)

        # Range expansion component (0-4) — strongest PF predictor
        # 2.5x bracket tested but diluted PF (1.73→1.07) — reverted to original
        if range_exp >= 4.0:
            score += 4.0
        elif range_exp >= 3.0:
            score += 3.0
        elif range_exp >= 2.0:
            score += 2.0
        elif range_exp >= 1.5:
            score += 1.5
        elif range_exp >= 1.0:
            score += 1.0

        return min(score, 7.0)

    def is_in_play(self, symbol: str, dt: date) -> Tuple[bool, float]:
        """
        Check if symbol is in-play on given date.
        Returns (passes, score).
        """
        cfg = self.cfg
        mode = cfg.ip_mode

        if mode == "list_only":
            # Live-only: check dashboard list. In backtest, always pass.
            return True, 5.0

        # proxy_only or hybrid
        sym_stats = self._day_stats.get(symbol)
        if not sym_stats:
            return False, 0.0

        day_stats = sym_stats.get(dt)
        if not day_stats:
            return False, 0.0

        gap_min = cfg.get(cfg.ip_gap_min)
        rvol_min = cfg.get(cfg.ip_rvol_min)
        dolvol_min = cfg.get(cfg.ip_dolvol_min)

        passes_gap = abs(day_stats.gap_pct) >= gap_min
        passes_rvol = day_stats.rvol >= rvol_min
        passes_dolvol = day_stats.dolvol >= dolvol_min

        # Need gap + rvol + dolvol (all three)
        proxy_pass = passes_gap and passes_rvol and passes_dolvol

        if mode == "hybrid":
            # In hybrid mode, list OR proxy passes
            # For backtest, proxy is the only option
            return proxy_pass, day_stats.score

        # proxy_only
        return proxy_pass, day_stats.score

    def get_stats(self, symbol: str, dt: date) -> Optional[DayOpenStats]:
        """Get raw stats for debugging/logging."""
        sym_stats = self._day_stats.get(symbol)
        if sym_stats:
            return sym_stats.get(dt)
        return None

    def get_pass_rates(self) -> Dict[str, float]:
        """Diagnostic: pass rate per symbol across all dates."""
        rates = {}
        for sym, day_stats in self._day_stats.items():
            total = len(day_stats)
            if total == 0:
                rates[sym] = 0.0
                continue
            passed = sum(1 for d in day_stats.values()
                        if self.is_in_play(sym, list(self._day_stats[sym].keys())[
                            list(self._day_stats[sym].values()).index(d)
                        ])[0])
            # Simpler: just recompute
            n_pass = 0
            for dt, st in day_stats.items():
                p, _ = self.is_in_play(sym, dt)
                if p:
                    n_pass += 1
            rates[sym] = n_pass / total * 100
        return rates

    def summary_stats(self) -> Dict[str, float]:
        """Aggregate stats across all symbols."""
        total_days = 0
        passed_days = 0
        for sym, day_stats in self._day_stats.items():
            for dt in day_stats:
                total_days += 1
                p, _ = self.is_in_play(sym, dt)
                if p:
                    passed_days += 1
        return {
            "total_symbol_days": total_days,
            "passed_symbol_days": passed_days,
            "pass_rate_pct": passed_days / total_days * 100 if total_days > 0 else 0.0,
        }
