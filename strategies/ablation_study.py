"""
Ablation + Threshold Tuning Study for SC_SNIPER and FL_ANTICHOP.

1. Time-ordered train/test split (first 60% = train, last 40% = test) + walk-forward
2. Per-layer ablation: raw → +in-play → +regime → +rejection → +A-tier
3. Identify load-bearing thresholds
4. Narrow tuning on those thresholds
5. Full report with required metrics

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.strategies.ablation_study
"""

import csv
import math
from collections import defaultdict, deque
from datetime import date, datetime
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional, Tuple

from ..backtest import load_bars_from_csv
from ..indicators import EMA, VWAPCalc, ATRPair
from ..market_context import SECTOR_MAP, get_sector_etf
from ..models import Bar, NaN

from .shared.signal_schema import StrategySignal, StrategyTrade, QualityTier
from .shared.config import StrategyConfig
from .shared.in_play_proxy import InPlayProxy
from .shared.market_regime import EnhancedMarketRegime
from .shared.rejection_filters import RejectionFilters
from .shared.quality_scoring import QualityScorer
from .shared.helpers import (
    trigger_bar_quality, bar_body_ratio, get_hhmm, simulate_strategy_trade,
    compute_daily_atr,
)
from .second_chance_sniper import SecondChanceSniperStrategy
from .fl_antichop_only import FLAntiChopStrategy
from .spencer_atier import SpencerATierStrategy
from .hitchhiker_quality import HitchHikerQualityStrategy

DATA_DIR = Path(__file__).parent.parent / "data"
OUT_DIR = Path(__file__).parent.parent / "outputs"

_isnan = math.isnan


# ════════════════════════════════════════════════════════════════
#  Metrics with time-ordered split
# ════════════════════════════════════════════════════════════════

def _pf(trades: List[StrategyTrade]) -> float:
    if not trades:
        return 0.0
    gw = sum(t.pnl_rr for t in trades if t.pnl_rr > 0)
    gl = abs(sum(t.pnl_rr for t in trades if t.pnl_rr <= 0))
    return gw / gl if gl > 0 else float("inf")


def pf_s(v: float) -> str:
    return f"{v:.2f}" if v < 999 else "inf"


def compute_metrics(trades: List[StrategyTrade], all_dates: List[date] = None) -> dict:
    n = len(trades)
    empty = {"n": 0, "wr": 0, "pf": 0, "exp": 0, "total_r": 0, "max_dd_r": 0,
             "avg_win": 0, "avg_loss": 0,
             "train_pf": 0, "test_pf": 0, "train_n": 0, "test_n": 0,
             "wf_pf": 0, "wf_n": 0,
             "ex_best_day_pf": 0, "ex_top_sym_pf": 0,
             "stop_rate": 0, "target_rate": 0, "time_rate": 0, "avg_bars": 0}
    if n == 0:
        return empty

    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in trades)
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")

    avg_win = gw / len(wins) if wins else 0
    avg_loss = gl / len(losses) if losses else 0

    # Max drawdown
    cum = peak = max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.signal.timestamp):
        cum += t.pnl_rr
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    # Time-ordered train/test: first 60% of dates = train, last 40% = test
    if all_dates is None:
        all_dates = sorted(set(t.entry_date for t in trades))
    split_idx = int(len(all_dates) * 0.60)
    train_dates = set(all_dates[:split_idx])
    test_dates = set(all_dates[split_idx:])
    train = [t for t in trades if t.entry_date in train_dates]
    test = [t for t in trades if t.entry_date in test_dates]

    # Walk-forward: split into 3 equal time segments, train on first 2, test on last
    wf_split = int(len(all_dates) * 0.67)
    wf_test_dates = set(all_dates[wf_split:])
    wf_test = [t for t in trades if t.entry_date in wf_test_dates]

    # Ex-best-day, ex-top-symbol
    day_r = defaultdict(float)
    for t in trades:
        day_r[t.entry_date] += t.pnl_rr
    best_day = max(day_r, key=day_r.get) if day_r else None
    ex_best = [t for t in trades if t.entry_date != best_day] if best_day else trades

    sym_r = defaultdict(float)
    for t in trades:
        sym_r[t.symbol] += t.pnl_rr
    top_sym = max(sym_r, key=sym_r.get) if sym_r else None
    ex_top = [t for t in trades if t.symbol != top_sym] if top_sym else trades

    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    target = sum(1 for t in trades if t.exit_reason == "target")
    timed = sum(1 for t in trades if t.exit_reason in ("eod", "trail", "time"))

    return {
        "n": n, "wr": len(wins) / n * 100, "pf": pf,
        "exp": total_r / n, "total_r": total_r, "max_dd_r": max_dd,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "train_pf": _pf(train), "test_pf": _pf(test),
        "train_n": len(train), "test_n": len(test),
        "wf_pf": _pf(wf_test), "wf_n": len(wf_test),
        "ex_best_day_pf": _pf(ex_best), "ex_top_sym_pf": _pf(ex_top),
        "stop_rate": stopped / n * 100,
        "target_rate": target / n * 100,
        "time_rate": timed / n * 100,
        "avg_bars": sum(t.bars_held for t in trades) / n,
    }


def print_metrics(label: str, m: dict):
    print(f"\n  ── {label} ──")
    if m["n"] == 0:
        print("  N=0 (no trades)")
        return
    print(f"  N={m['n']}  PF={pf_s(m['pf'])}  WR={m['wr']:.1f}%  Exp={m['exp']:+.3f}R  "
          f"TotalR={m['total_r']:+.1f}  MaxDD={m['max_dd_r']:.1f}R")
    print(f"  AvgWin={m['avg_win']:+.2f}R  AvgLoss={m['avg_loss']:.2f}R  "
          f"Stop={m['stop_rate']:.0f}%  Tgt={m['target_rate']:.0f}%  Time={m['time_rate']:.0f}%  "
          f"AvgBars={m['avg_bars']:.0f}")
    print(f"  Train: PF={pf_s(m['train_pf'])} N={m['train_n']}  |  "
          f"Test: PF={pf_s(m['test_pf'])} N={m['test_n']}  |  "
          f"WalkFwd: PF={pf_s(m['wf_pf'])} N={m['wf_n']}")
    print(f"  ExDay={pf_s(m['ex_best_day_pf'])}  ExSym={pf_s(m['ex_top_sym_pf'])}")


# ════════════════════════════════════════════════════════════════
#  Data loading
# ════════════════════════════════════════════════════════════════

def load_universe():
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    sector_etfs = sorted(set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    excluded = {"SPY", "QQQ", "IWM"} | set(sector_etfs)
    symbols = sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])
    all_dates = sorted(set(b.timestamp.date() for b in spy_bars))
    return spy_bars, symbols, all_dates


# ════════════════════════════════════════════════════════════════
#  Unified signal detection — delegates to strategy class methods
#  to guarantee ablation and replay use identical detection logic.
# ════════════════════════════════════════════════════════════════

def _detect_all_days_via_strategy(strategy_obj, detect_method_name: str,
                                  sym: str, bars: List[Bar]) -> list:
    """Call a strategy class's _detect_*_signals once for ALL days (single pass).
    Returns list of tuples matching the strategy's internal format.

    Passes day=None which tells the detect method to collect signals from
    every day in a single O(N) pass instead of per-day O(N²)."""
    detect_fn = getattr(strategy_obj, detect_method_name)
    return detect_fn(sym, bars, None, 8.0)  # day=None = all days, ip_score=8.0 (dummy)


# Pre-built strategy instances are created once per ablation run in
# run_ablation() and passed through.  These thin wrappers exist for
# backward-compat with callers that pass (sym, bars, cfg) directly
# (e.g. tuning and the __main__ block).

_sc_strategy_cache = {}
_fl_strategy_cache = {}
_sp_strategy_cache = {}
_hh_strategy_cache = {}


def _get_or_make_strategy(cache, cls, cfg, spy_bars=None):
    """Lazily create a strategy instance with null gates (for detection only)."""
    key = id(cfg)
    if key not in cache:
        in_play = InPlayProxy(cfg)
        if spy_bars is None:
            spy_path = DATA_DIR / "1min" / "SPY_1min.csv"
            spy_bars = load_bars_from_csv(str(spy_path)) if spy_path.exists() else []
        regime = EnhancedMarketRegime(spy_bars, cfg)
        regime.precompute()
        rejection = RejectionFilters(cfg)
        quality = QualityScorer(cfg)
        cache[key] = cls(cfg, in_play, regime, rejection, quality)
    return cache[key]


def detect_sc_all_days(sym: str, bars: List[Bar],
                       cfg: StrategyConfig, strategy=None) -> list:
    """Detect raw SC signals for ALL days. Returns [(signal, bar_idx, bar, atr, e9, vw, vol_ma)]."""
    if strategy is None:
        strategy = _get_or_make_strategy(_sc_strategy_cache, SecondChanceSniperStrategy, cfg)
    return _detect_all_days_via_strategy(strategy, '_detect_sc_signals', sym, bars)


def detect_fl_all_days(sym: str, bars: List[Bar],
                       cfg: StrategyConfig, strategy=None) -> list:
    """Detect raw FL signals for ALL days. Returns [(signal, bar_idx, bar, atr, e9, vw, vol_ma, rvol)]."""
    if strategy is None:
        strategy = _get_or_make_strategy(_fl_strategy_cache, FLAntiChopStrategy, cfg)
    return _detect_all_days_via_strategy(strategy, '_detect_fl_signals', sym, bars)


def detect_sp_all_days(sym: str, bars: List[Bar],
                       cfg: StrategyConfig, strategy=None) -> list:
    """Detect raw Spencer signals for ALL days. Returns [(signal, bar_idx, bar, atr, e9, vw, vol_ma)]."""
    if strategy is None:
        strategy = _get_or_make_strategy(_sp_strategy_cache, SpencerATierStrategy, cfg)
    return _detect_all_days_via_strategy(strategy, '_detect_sp_signals', sym, bars)


def detect_hh_all_days(sym: str, bars: List[Bar],
                       cfg: StrategyConfig, strategy=None) -> list:
    """Detect raw HitchHiker signals for ALL days. Returns [(signal, bar_idx, bar, atr, e9, vw, vol_ma)]."""
    if strategy is None:
        strategy = _get_or_make_strategy(_hh_strategy_cache, HitchHikerQualityStrategy, cfg)
    return _detect_all_days_via_strategy(strategy, '_detect_hh_signals', sym, bars)


# ---- Legacy detection functions removed ----
# The above functions now delegate to strategy class methods to ensure
# ablation and replay use identical detection logic, including structural
# targets, stop modes, and all strategy-specific features.


# ════════════════════════════════════════════════════════════════
#  Ablation runner
# ════════════════════════════════════════════════════════════════

def run_ablation(strategy_name: str, cfg: StrategyConfig,
                 spy_bars: List[Bar], symbols: List[str],
                 all_dates: List[date],
                 bars_cache: Dict[str, List[Bar]] = None):
    """
    Run ablation: raw → +in-play → +regime → +rejection → +A-tier.
    Returns dict of {layer_name: [trades]}.
    Uses single-pass detection per symbol for performance.
    """
    # Dual-timeframe: 1-min for in-play gate, cfg (5-min) for strategy detection
    ip_cfg = StrategyConfig(timeframe_min=1)
    in_play = InPlayProxy(ip_cfg)
    regime = EnhancedMarketRegime(spy_bars, cfg)
    regime.precompute()
    rejection = RejectionFilters(cfg)
    quality_scorer = QualityScorer(cfg)

    is_sc = strategy_name == "SC_SNIPER"
    is_fl = strategy_name == "FL_ANTICHOP"
    is_sp = strategy_name == "SP_ATIER"
    is_hh = strategy_name == "HH_QUALITY"

    if is_sc:
        detect_fn = detect_sc_all_days
        max_bars = cfg.get(cfg.sc_max_bars)
        target_rr = cfg.sc_target_rr
        skip_filters = ["distance", "bigger_picture"]
    elif is_fl:
        detect_fn = detect_fl_all_days
        max_bars = cfg.get(cfg.fl_max_bars)
        target_rr = cfg.fl_target_rr
        skip_filters = ["trigger_weakness"]
    elif is_sp:
        detect_fn = detect_sp_all_days
        max_bars = cfg.get(cfg.sp_max_bars)
        target_rr = cfg.sp_target_rr
        skip_filters = ["distance", "bigger_picture"]
    elif is_hh:
        detect_fn = detect_hh_all_days
        max_bars = cfg.get(cfg.hh_max_bars)
        target_rr = cfg.hh_target_rr
        skip_filters = ["distance", "maturity"]
    else:
        raise ValueError(f"Unknown strategy: {strategy_name}")

    # Chop filter for FL
    def antichop(bars_list, bar_idx, atr):
        lookback = cfg.get(cfg.fl_chop_lookback)
        max_overlap = cfg.fl_chop_overlap_max
        if bar_idx < lookback + 1 or atr <= 0:
            return True
        start = max(0, bar_idx - lookback)
        window = bars_list[start:bar_idx + 1]
        if len(window) < 3:
            return True
        total_range = total_overlap = 0.0
        for j in range(1, len(window)):
            p, c = window[j-1], window[j]
            pr, cr = p.high - p.low, c.high - c.low
            total_range += pr + cr
            total_overlap += max(0, min(p.high, c.high) - max(p.low, c.low))
        if total_range <= 0:
            return True
        return total_overlap / (total_range / 2.0) <= max_overlap

    layers = {
        "1_raw": [], "2_+inplay": [], "3_+regime": [],
        "4_+rejection": [], "5_+atier": [],
    }
    reject_counts = defaultdict(int)

    for sym in symbols:
        if bars_cache:
            bars = bars_cache.get(sym)
        else:
            p = DATA_DIR / f"{sym}_5min.csv"
            if not p.exists():
                continue
            bars = load_bars_from_csv(str(p))
        if not bars:
            continue
        in_play.precompute(sym, bars)

        # Single-pass detection: get all raw signals for all days at once
        all_raw = detect_fn(sym, bars, cfg)

        for entry in all_raw:
            if is_fl:
                sig, bar_idx, bar, atr, e9, vw, vol_ma, rvol = entry
            else:
                sig, bar_idx, bar, atr, e9, vw, vol_ma = entry
                rvol = NaN

            day = bar.timestamp.date()

            # Use actual_rr from signal metadata (structural target) or fallback
            actual_rr = sig.metadata.get("actual_rr", target_rr)

            # Layer 1: raw
            trade = simulate_strategy_trade(sig, bars, bar_idx, max_bars, actual_rr)
            layers["1_raw"].append(trade)

            # Layer 2: +in-play
            ip_pass, ip_score = in_play.is_in_play(sym, day)
            if not ip_pass:
                continue
            trade2 = simulate_strategy_trade(sig, bars, bar_idx, max_bars, actual_rr)
            layers["2_+inplay"].append(trade2)

            # Layer 3: +regime
            if not regime.is_aligned_long(bar.timestamp):
                continue
            trade3 = simulate_strategy_trade(sig, bars, bar_idx, max_bars, actual_rr)
            layers["3_+regime"].append(trade3)

            # Layer 4: +rejection
            reasons = rejection.check_all(
                bar, bars, bar_idx, atr, e9, vw, vol_ma,
                skip_filters=skip_filters
            )
            if is_fl and antichop(bars, bar_idx, atr) is False:
                reasons.append("antichop")
            if reasons:
                for r in reasons:
                    reject_counts[r.split("(")[0]] += 1
                continue
            trade4 = simulate_strategy_trade(sig, bars, bar_idx, max_bars, actual_rr)
            layers["4_+rejection"].append(trade4)

            # Layer 5: +A-tier scoring
            snap = regime.get_nearest_regime(bar.timestamp)
            regime_score = {"GREEN": 1.0, "FLAT": 0.5, "RED": 0.0}.get(
                snap.day_label if snap else "", 0.5)
            alignment = 0.0
            if snap:
                if snap.spy_above_vwap: alignment += 0.5
                if snap.ema9_above_ema20: alignment += 0.5

            stock_f = {"in_play_score": ip_score, "rs_market": 0.0,
                       "rs_sector": 0.0, "volume_profile": 0.5}
            market_f = {"regime_score": regime_score, "alignment_score": alignment}
            setup_f = {"trigger_quality": trigger_bar_quality(bar, atr, vol_ma),
                       "structure_quality": sig.metadata.get("structure_quality", 0.5),
                       "confluence_count": 2}
            tier, score = quality_scorer.score(stock_f, market_f, setup_f)
            if tier != QualityTier.A_TIER:
                continue
            trade5 = simulate_strategy_trade(sig, bars, bar_idx, max_bars, actual_rr)
            layers["5_+atier"].append(trade5)

    return layers, dict(reject_counts)


# ════════════════════════════════════════════════════════════════
#  Threshold tuning
# ════════════════════════════════════════════════════════════════

def run_tuning(strategy_name: str, spy_bars: List[Bar],
               symbols: List[str], all_dates: List[date],
               bars_cache: Dict[str, List[Bar]]):
    """
    Narrow tuning on load-bearing thresholds.
    bars_cache: {symbol: bars} preloaded to avoid re-reading disk.
    Returns list of (variant_name, cfg_overrides, metrics_dict).
    """
    is_sc = strategy_name == "SC_SNIPER"
    is_fl = strategy_name == "FL_ANTICHOP"
    is_sp = strategy_name == "SP_ATIER"
    is_hh = strategy_name == "HH_QUALITY"

    if is_sc:
        variants = [
            ("baseline", {}),
            ("tgt_2R", {"sc_target_rr": 2.0}),
            ("tgt_4R", {"sc_target_rr": 4.0}),
            ("bo_vol_1.0", {"sc_strong_bo_vol_mult": 1.0}),
            ("bo_vol_1.5", {"sc_strong_bo_vol_mult": 1.5}),
            ("retest_0.20", {"sc_retest_max_depth_atr": 0.20}),
            ("retest_0.50", {"sc_retest_max_depth_atr": 0.50}),
            ("confirm_0.50", {"sc_confirm_vol_frac": 0.50}),
            ("confirm_1.00", {"sc_confirm_vol_frac": 1.0}),
            ("atier_4", {"quality_a_min": 4}),
            ("atier_5", {"quality_a_min": 5}),
            ("atier_7", {"quality_a_min": 7}),
            ("tgt2R_bo1.0", {"sc_target_rr": 2.0, "sc_strong_bo_vol_mult": 1.0}),
            ("tgt2R_retest0.50", {"sc_target_rr": 2.0, "sc_retest_max_depth_atr": 0.50}),
            ("tgt2R_atier4", {"sc_target_rr": 2.0, "quality_a_min": 4}),
            ("tgt2R_atier5", {"sc_target_rr": 2.0, "quality_a_min": 5}),
        ]
    elif is_fl:
        variants = [
            ("baseline", {}),
            ("tgt_1.5R", {"fl_target_rr": 1.5}),
            ("tgt_3R", {"fl_target_rr": 3.0}),
            ("decline_2ATR", {"fl_min_decline_atr": 2.0}),
            ("decline_4ATR", {"fl_min_decline_atr": 4.0}),
            ("turn_2bar", {"fl_turn_confirm_bars": {1: 6, 5: 2}}),
            ("turn_3bar", {"fl_turn_confirm_bars": {1: 9, 5: 3}}),
            ("turn_6bar", {"fl_turn_confirm_bars": {1: 18, 5: 6}}),
            ("stop_0.30", {"fl_stop_frac": 0.30}),
            ("stop_0.70", {"fl_stop_frac": 0.70}),
            ("chop_0.50", {"fl_chop_overlap_max": 0.50}),
            ("chop_0.70", {"fl_chop_overlap_max": 0.70}),
            ("chop_0.80", {"fl_chop_overlap_max": 0.80}),
            ("tgt1.5_stop0.30", {"fl_target_rr": 1.5, "fl_stop_frac": 0.30}),
            ("tgt3_stop0.70", {"fl_target_rr": 3.0, "fl_stop_frac": 0.70}),
            ("decline2_chop0.70", {"fl_min_decline_atr": 2.0, "fl_chop_overlap_max": 0.70}),
            ("decline2_turn3", {"fl_min_decline_atr": 2.0, "fl_turn_confirm_bars": {1: 9, 5: 3}}),
        ]
    elif is_sp:
        variants = [
            ("baseline", {}),
            ("tgt_2R", {"sp_target_rr": 2.0}),
            ("tgt_4R", {"sp_target_rr": 4.0}),
            ("box_max_6", {"sp_box_max_bars": {1: 30, 5: 6}}),
            ("box_max_12", {"sp_box_max_bars": {1: 60, 5: 12}}),
            ("box_range_1.5", {"sp_box_max_range_atr": 1.5}),
            ("box_range_2.5", {"sp_box_max_range_atr": 2.5}),
            ("break_vol_1.0", {"sp_break_vol_frac": 1.0}),
            ("break_vol_1.25", {"sp_break_vol_frac": 1.25}),
            ("advance_0.50", {"sp_trend_advance_atr": 0.50}),
            ("advance_1.00", {"sp_trend_advance_atr": 1.00}),
            ("extension_1.00", {"sp_extension_atr": 1.00}),
            ("extension_2.00", {"sp_extension_atr": 2.00}),
            ("atier_4", {"quality_a_min": 4}),
            ("atier_5", {"quality_a_min": 5}),
            ("tgt2R_atier4", {"sp_target_rr": 2.0, "quality_a_min": 4}),
            ("tgt2R_atier5", {"sp_target_rr": 2.0, "quality_a_min": 5}),
        ]
    elif is_hh:
        variants = [
            ("baseline", {}),
            ("tgt_1.5R", {"hh_target_rr": 1.5}),
            ("tgt_3R", {"hh_target_rr": 3.0}),
            ("drive_0.75", {"hh_drive_min_atr": 0.75}),
            ("drive_1.50", {"hh_drive_min_atr": 1.50}),
            ("consol_min_2", {"hh_consol_min_bars": {1: 10, 5: 2}}),
            ("consol_min_5", {"hh_consol_min_bars": {1: 25, 5: 5}}),
            ("consol_max_16", {"hh_consol_max_bars": {1: 80, 5: 16}}),
            ("consol_max_36", {"hh_consol_max_bars": {1: 180, 5: 36}}),
            ("wick_0.50", {"hh_max_wick_pct": 0.50}),
            ("wick_0.80", {"hh_max_wick_pct": 0.80}),
            ("range_1.5", {"hh_consol_max_range_atr": 1.5}),
            ("range_2.5", {"hh_consol_max_range_atr": 2.5}),
            ("break_vol_1.0", {"hh_break_vol_frac": 1.0}),
            ("atier_4", {"quality_a_min": 4}),
            ("atier_5", {"quality_a_min": 5}),
            ("tgt1.5_atier4", {"hh_target_rr": 1.5, "quality_a_min": 4}),
        ]
    else:
        raise ValueError(f"Unknown strategy: {strategy_name}")

    # Precompute regime once (same for all variants since regime thresholds don't change)
    base_cfg = StrategyConfig(timeframe_min=5)
    regime = EnhancedMarketRegime(spy_bars, base_cfg)
    regime.precompute()

    results = []
    for vi, (name, overrides) in enumerate(variants):
        # Dual-timeframe: 1-min for in-play, 5-min for strategy params
        ip_cfg = StrategyConfig(timeframe_min=1)
        cfg = StrategyConfig(timeframe_min=5)
        for k, v in overrides.items():
            setattr(cfg, k, v)

        in_play = InPlayProxy(ip_cfg)
        rejection = RejectionFilters(cfg)
        quality_scorer = QualityScorer(cfg)

        if is_sc:
            detect_fn = detect_sc_all_days
            max_bars_val = cfg.get(cfg.sc_max_bars)
            target_rr = cfg.sc_target_rr
            skip_filt = ["distance", "bigger_picture"]
        elif is_fl:
            detect_fn = detect_fl_all_days
            max_bars_val = cfg.get(cfg.fl_max_bars)
            target_rr = cfg.fl_target_rr
            skip_filt = ["trigger_weakness"]
        elif is_sp:
            detect_fn = detect_sp_all_days
            max_bars_val = cfg.get(cfg.sp_max_bars)
            target_rr = cfg.sp_target_rr
            skip_filt = ["distance", "bigger_picture"]
        else:  # is_hh
            detect_fn = detect_hh_all_days
            max_bars_val = cfg.get(cfg.hh_max_bars)
            target_rr = cfg.hh_target_rr
            skip_filt = ["distance", "maturity"]

        chop_lb = cfg.get(cfg.fl_chop_lookback)
        chop_mo = cfg.fl_chop_overlap_max

        trades = []
        for sym in symbols:
            bars = bars_cache.get(sym)
            if not bars:
                continue
            in_play.precompute(sym, bars)

            # Single-pass detection for all days
            all_raw = detect_fn(sym, bars, cfg)
            for entry in all_raw:
                if is_fl:
                    sig, bi, br, atr, e9, vw, vm, rv = entry
                else:
                    sig, bi, br, atr, e9, vw, vm = entry

                day_d = br.timestamp.date()
                ip_pass, ip_sc = in_play.is_in_play(sym, day_d)
                if not ip_pass:
                    continue
                if not regime.is_aligned_long(br.timestamp):
                    continue
                reasons = rejection.check_all(
                    br, bars, bi, atr, e9, vw, vm, skip_filters=skip_filt)
                if is_fl:
                    # Inline antichop
                    if bi >= chop_lb + 1 and atr > 0:
                        start = max(0, bi - chop_lb)
                        w = bars[start:bi + 1]
                        if len(w) >= 3:
                            tr = to = 0.0
                            for j in range(1, len(w)):
                                p2, c2 = w[j-1], w[j]
                                tr += (p2.high-p2.low)+(c2.high-c2.low)
                                to += max(0,min(p2.high,c2.high)-max(p2.low,c2.low))
                            if tr > 0 and to/(tr/2.0) > chop_mo:
                                reasons.append("antichop")
                if reasons:
                    continue

                snap = regime.get_nearest_regime(br.timestamp)
                rs_ = {"GREEN": 1.0, "FLAT": 0.5, "RED": 0.0}.get(
                    snap.day_label if snap else "", 0.5)
                al_ = 0.0
                if snap:
                    if snap.spy_above_vwap: al_ += 0.5
                    if snap.ema9_above_ema20: al_ += 0.5
                tier, _ = quality_scorer.score(
                    {"in_play_score": ip_sc, "rs_market": 0, "rs_sector": 0, "volume_profile": 0.5},
                    {"regime_score": rs_, "alignment_score": al_},
                    {"trigger_quality": trigger_bar_quality(br, atr, vm),
                     "structure_quality": sig.metadata.get("structure_quality", 0.5),
                     "confluence_count": 2})
                if tier != QualityTier.A_TIER:
                    continue
                _actual_rr = sig.metadata.get("actual_rr", target_rr)
                t = simulate_strategy_trade(sig, bars, bi, max_bars_val, _actual_rr)
                trades.append(t)

        m = compute_metrics(trades, all_dates)
        results.append((name, overrides, m))
        print(f"    [{vi+1}/{len(variants)}] {name}: N={m['n']} PF={pf_s(m['pf'])}")

    return results


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    print("=" * 110)
    print("ABLATION + TUNING STUDY — SC_SNIPER & FL_ANTICHOP & SP_ATIER & HH_QUALITY")
    print("=" * 110)

    spy_bars, symbols, all_dates = load_universe()
    print(f"  Universe: {len(symbols)} syms, {len(all_dates)} days "
          f"({all_dates[0]} → {all_dates[-1]})")

    split_idx = int(len(all_dates) * 0.60)
    wf_idx = int(len(all_dates) * 0.67)
    print(f"  Time split: Train={all_dates[0]}→{all_dates[split_idx-1]} ({split_idx}d), "
          f"Test={all_dates[split_idx]}→{all_dates[-1]} ({len(all_dates)-split_idx}d)")
    print(f"  Walk-fwd:   Train={all_dates[0]}→{all_dates[wf_idx-1]}, "
          f"Test={all_dates[wf_idx]}→{all_dates[-1]} ({len(all_dates)-wf_idx}d)")

    # Dual-timeframe: strategy params use 5-min calibration, in-play uses 1-min internally
    cfg = StrategyConfig(timeframe_min=5)

    # Preload all bar data once
    print("  Preloading bar data...")
    bars_cache: Dict[str, List[Bar]] = {}
    for sym in symbols:
        fp = DATA_DIR / f"{sym}_5min.csv"
        if fp.exists():
            bars_cache[sym] = load_bars_from_csv(str(fp))
    print(f"  Loaded {len(bars_cache)} symbols into cache")

    import sys
    # Determine which strategies to run
    strat_list = ["SC_SNIPER", "FL_ANTICHOP", "SP_ATIER", "HH_QUALITY"]
    if "--sp-only" in sys.argv:
        strat_list = ["SP_ATIER"]
    elif "--hh-only" in sys.argv:
        strat_list = ["HH_QUALITY"]
    elif "--sp-hh" in sys.argv:
        strat_list = ["SP_ATIER", "HH_QUALITY"]
    elif "--sc-fl" in sys.argv:
        strat_list = ["SC_SNIPER", "FL_ANTICHOP"]

    for strategy in strat_list:
        print(f"\n{'=' * 110}")
        print(f"  ABLATION: {strategy}")
        print(f"{'=' * 110}")

        layers, reject_counts = run_ablation(strategy, cfg, spy_bars, symbols, all_dates, bars_cache)

        for layer_name in sorted(layers.keys()):
            trades = layers[layer_name]
            m = compute_metrics(trades, all_dates)
            print_metrics(f"{layer_name} ({strategy})", m)

        print(f"\n  Rejection breakdown:")
        for reason, count in sorted(reject_counts.items(), key=lambda x: -x[1]):
            print(f"    {reason}: {count}")

    if "--tuning" in sys.argv:
        for strategy in strat_list:
            print(f"\n{'=' * 110}")
            print(f"  TUNING: {strategy}")
            print(f"{'=' * 110}")

            results = run_tuning(strategy, spy_bars, symbols, all_dates, bars_cache)
            _print_tuning_table(results)
    elif "--tune-sc" in sys.argv:
        print(f"\n{'=' * 110}")
        print(f"  TUNING: SC_SNIPER")
        print(f"{'=' * 110}")
        results = run_tuning("SC_SNIPER", spy_bars, symbols, all_dates, bars_cache)
        _print_tuning_table(results)
    elif "--tune-fl" in sys.argv:
        print(f"\n{'=' * 110}")
        print(f"  TUNING: FL_ANTICHOP")
        print(f"{'=' * 110}")
        results = run_tuning("FL_ANTICHOP", spy_bars, symbols, all_dates, bars_cache)
        _print_tuning_table(results)
    elif "--tune-sp" in sys.argv:
        print(f"\n{'=' * 110}")
        print(f"  TUNING: SP_ATIER")
        print(f"{'=' * 110}")
        results = run_tuning("SP_ATIER", spy_bars, symbols, all_dates, bars_cache)
        _print_tuning_table(results)
    elif "--tune-hh" in sys.argv:
        print(f"\n{'=' * 110}")
        print(f"  TUNING: HH_QUALITY")
        print(f"{'=' * 110}")
        results = run_tuning("HH_QUALITY", spy_bars, symbols, all_dates, bars_cache)
        _print_tuning_table(results)

    print(f"\n{'=' * 110}")
    print("DONE.")
    print(f"{'=' * 110}")


def _print_tuning_table(results):
    print(f"\n  {'Variant':<25s} {'N':>4s} {'PF':>6s} {'WR%':>6s} {'Exp':>7s} "
          f"{'TotalR':>7s} {'AvgW':>6s} {'AvgL':>6s} {'TrnPF':>6s} {'TstPF':>6s} {'WfPF':>6s}")
    print(f"  {'-'*25} {'-'*4} {'-'*6} {'-'*6} {'-'*7} {'-'*7} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6}")
    for name, _, m in results:
        if m["n"] == 0:
            print(f"  {name:<25s}    0")
            continue
        print(f"  {name:<25s} {m['n']:4d} {pf_s(m['pf']):>6s} {m['wr']:5.1f}% "
              f"{m['exp']:+6.3f} {m['total_r']:+6.1f} "
              f"{m['avg_win']:+5.2f} {m['avg_loss']:5.2f} "
              f"{pf_s(m['train_pf']):>6s} {pf_s(m['test_pf']):>6s} {pf_s(m['wf_pf']):>6s}")

    viable = [(n, o, m) for n, o, m in results if m["n"] >= 10]
    if viable:
        best = max(viable, key=lambda x: x[2]["pf"])
        print(f"\n  Best viable (N>=10): {best[0]}  PF={pf_s(best[2]['pf'])}  "
              f"N={best[2]['n']}  TestPF={pf_s(best[2]['test_pf'])}  "
              f"WfPF={pf_s(best[2]['wf_pf'])}")

    print(f"\n{'=' * 110}")
    print("DONE.")
    print(f"{'=' * 110}")


# ════════════════════════════════════════════════════════════════
#  FL Filter Ablation — remove value-destructive layers
# ════════════════════════════════════════════════════════════════

def run_fl_filter_ablation(spy_bars: List[Bar], symbols: List[str],
                           all_dates: List[date],
                           bars_cache: Dict[str, List[Bar]]):
    """
    Test FL_ANTICHOP with tgt_1.5R base across filter removal variants.
    The ablation showed rejection+A-tier hurt PF. Test removing each.
    """
    regime = EnhancedMarketRegime(spy_bars, StrategyConfig(timeframe_min=5))
    regime.precompute()

    # Variants: (name, skip_antichop, skip_distance, a_tier_min)
    variants = [
        ("baseline_tgt1.5R",       False, False, 6),   # current best with all filters
        ("no_antichop",            True,  False, 6),   # remove antichop only
        ("no_distance",            False, True,  6),   # remove distance only
        ("no_antichop_no_distance",True,  True,  6),   # remove both
        ("atier_4",                False, False, 4),   # soften A-tier only
        ("atier_3",                False, False, 3),   # soften further
        ("no_antichop_atier4",     True,  False, 4),   # best combo candidate
        ("no_antichop_no_dist_at4",True,  True,  4),   # most relaxed
        # Also test regime-only (no rejection, no A-tier) with tgt_1.5R
        ("regime_only_tgt1.5R",    None,  None,  None),
    ]

    results = []
    for vi, (name, skip_ac, skip_dist, a_min) in enumerate(variants):
        # Dual-timeframe: 1-min for in-play, 5-min for strategy params
        ip_cfg = StrategyConfig(timeframe_min=1)
        cfg = StrategyConfig(timeframe_min=5)
        cfg.fl_target_rr = 1.5

        in_play = InPlayProxy(ip_cfg)
        rejection = RejectionFilters(cfg)
        quality_scorer = QualityScorer(cfg)

        max_bars_val = cfg.get(cfg.fl_max_bars)
        chop_lb = cfg.get(cfg.fl_chop_lookback)
        chop_mo = cfg.fl_chop_overlap_max

        # Build skip_filters list
        base_skip = ["trigger_weakness"]
        if skip_dist is True:
            base_skip.append("distance")

        trades = []
        for sym in symbols:
            bars = bars_cache.get(sym)
            if not bars:
                continue
            in_play.precompute(sym, bars)

            all_raw = detect_fl_all_days(sym, bars, cfg)
            for entry in all_raw:
                sig, bi, br, atr, e9, vw, vm, rv = entry
                day_d = br.timestamp.date()

                ip_pass, ip_sc = in_play.is_in_play(sym, day_d)
                if not ip_pass:
                    continue
                if not regime.is_aligned_long(br.timestamp):
                    continue

                # regime_only variant: skip rejection + A-tier entirely
                if a_min is None:
                    _ar = sig.metadata.get("actual_rr", 1.5)
                    t = simulate_strategy_trade(sig, bars, bi, max_bars_val, _ar)
                    trades.append(t)
                    continue

                # Rejection filters
                reasons = rejection.check_all(
                    br, bars, bi, atr, e9, vw, vm, skip_filters=base_skip)

                # Antichop (unless skipped)
                if skip_ac is not True:
                    if bi >= chop_lb + 1 and atr > 0:
                        start = max(0, bi - chop_lb)
                        w = bars[start:bi + 1]
                        if len(w) >= 3:
                            tr = to = 0.0
                            for j in range(1, len(w)):
                                p2, c2 = w[j-1], w[j]
                                tr += (p2.high-p2.low)+(c2.high-c2.low)
                                to += max(0,min(p2.high,c2.high)-max(p2.low,c2.low))
                            if tr > 0 and to/(tr/2.0) > chop_mo:
                                reasons.append("antichop")
                if reasons:
                    continue

                # Quality scoring
                snap = regime.get_nearest_regime(br.timestamp)
                rs_ = {"GREEN": 1.0, "FLAT": 0.5, "RED": 0.0}.get(
                    snap.day_label if snap else "", 0.5)
                al_ = 0.0
                if snap:
                    if snap.spy_above_vwap: al_ += 0.5
                    if snap.ema9_above_ema20: al_ += 0.5
                tier, sc = quality_scorer.score(
                    {"in_play_score": ip_sc, "rs_market": 0, "rs_sector": 0, "volume_profile": 0.5},
                    {"regime_score": rs_, "alignment_score": al_},
                    {"trigger_quality": trigger_bar_quality(br, atr, vm),
                     "structure_quality": sig.metadata.get("structure_quality", 0.5),
                     "confluence_count": 2})
                if sc < a_min:
                    continue
                _ar = sig.metadata.get("actual_rr", 1.5)
                t = simulate_strategy_trade(sig, bars, bi, max_bars_val, _ar)
                trades.append(t)

        m = compute_metrics(trades, all_dates)
        results.append((name, m))
        print(f"    [{vi+1}/{len(variants)}] {name}: N={m['n']} PF={pf_s(m['pf'])} "
              f"TestPF={pf_s(m['test_pf'])} WfPF={pf_s(m['wf_pf'])}")

    return results


# ════════════════════════════════════════════════════════════════
#  SC In-Play Expansion Study
# ════════════════════════════════════════════════════════════════

def run_sc_inplay_expansion(spy_bars: List[Bar], symbols: List[str],
                            all_dates: List[date],
                            bars_cache: Dict[str, List[Bar]]):
    """
    Test SC_SNIPER tgt_2R across broadening in-play cohorts.
    Current: gap>=1%, rvol>=2.0, dolvol>=1M (all three required).
    """
    regime = EnhancedMarketRegime(spy_bars, StrategyConfig(timeframe_min=5))
    regime.precompute()

    # (name, gap_min, rvol_min, dolvol_min)
    variants = [
        ("baseline_strict",      0.010, 2.0, 1_000_000),  # current
        ("gap_0.5pct",           0.005, 2.0, 1_000_000),  # loosen gap
        ("rvol_1.5",             0.010, 1.5, 1_000_000),  # loosen rvol
        ("dolvol_500k",          0.010, 2.0,   500_000),  # loosen dolvol
        ("gap0.5_rvol1.5",       0.005, 1.5, 1_000_000),  # loosen gap+rvol
        ("gap0.5_dolvol500k",    0.005, 2.0,   500_000),  # loosen gap+dolvol
        ("rvol1.5_dolvol500k",   0.010, 1.5,   500_000),  # loosen rvol+dolvol
        ("all_loose",            0.005, 1.5,   500_000),  # loosen all three
        ("very_loose",           0.003, 1.2,   250_000),  # wide open
        ("no_inplay",            0.0,   0.0,         0),  # no filter at all
    ]

    results = []
    for vi, (name, gap_min, rvol_min, dolvol_min) in enumerate(variants):
        # Dual-timeframe: in-play thresholds vary per variant, strategy params fixed at 5-min
        ip_cfg = StrategyConfig(timeframe_min=1)
        ip_cfg.ip_gap_min = {1: gap_min, 5: gap_min}
        ip_cfg.ip_rvol_min = {1: rvol_min, 5: rvol_min}
        ip_cfg.ip_dolvol_min = {1: dolvol_min, 5: dolvol_min}

        cfg = StrategyConfig(timeframe_min=5)
        cfg.sc_target_rr = 2.0

        in_play = InPlayProxy(ip_cfg)
        rejection = RejectionFilters(cfg)
        quality_scorer = QualityScorer(cfg)

        max_bars_val = cfg.get(cfg.sc_max_bars)
        skip_filt = ["distance", "bigger_picture"]

        trades = []
        ip_pass_count = 0
        for sym in symbols:
            bars = bars_cache.get(sym)
            if not bars:
                continue
            in_play.precompute(sym, bars)

            all_raw = detect_sc_all_days(sym, bars, cfg)
            for entry in all_raw:
                sig, bi, br, atr, e9, vw, vm = entry
                day_d = br.timestamp.date()

                ip_pass, ip_sc = in_play.is_in_play(sym, day_d)
                if not ip_pass:
                    continue
                ip_pass_count += 1

                if not regime.is_aligned_long(br.timestamp):
                    continue

                reasons = rejection.check_all(
                    br, bars, bi, atr, e9, vw, vm, skip_filters=skip_filt)
                if reasons:
                    continue

                snap = regime.get_nearest_regime(br.timestamp)
                rs_ = {"GREEN": 1.0, "FLAT": 0.5, "RED": 0.0}.get(
                    snap.day_label if snap else "", 0.5)
                al_ = 0.0
                if snap:
                    if snap.spy_above_vwap: al_ += 0.5
                    if snap.ema9_above_ema20: al_ += 0.5
                tier, _ = quality_scorer.score(
                    {"in_play_score": ip_sc, "rs_market": 0, "rs_sector": 0, "volume_profile": 0.5},
                    {"regime_score": rs_, "alignment_score": al_},
                    {"trigger_quality": trigger_bar_quality(br, atr, vm),
                     "structure_quality": sig.metadata.get("structure_quality", 0.5),
                     "confluence_count": 2})
                if tier != QualityTier.A_TIER:
                    continue
                _ar = sig.metadata.get("actual_rr", 2.0)
                t = simulate_strategy_trade(sig, bars, bi, max_bars_val, _ar)
                trades.append(t)

        m = compute_metrics(trades, all_dates)
        results.append((name, m, ip_pass_count))
        print(f"    [{vi+1}/{len(variants)}] {name}: N={m['n']} PF={pf_s(m['pf'])} "
              f"IP_pass={ip_pass_count} TestPF={pf_s(m['test_pf'])} WfPF={pf_s(m['wf_pf'])}")

    return results


# ════════════════════════════════════════════════════════════════
#  Main entry point
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if "--fl-filter" in sys.argv:
        print("=" * 110)
        print("FL_ANTICHOP FILTER ABLATION (tgt_1.5R base)")
        print("=" * 110)
        spy_bars, symbols, all_dates = load_universe()
        print(f"  Universe: {len(symbols)} syms, {len(all_dates)} days")
        print("  Preloading bar data...")
        bars_cache: Dict[str, List[Bar]] = {}
        for sym in symbols:
            fp = DATA_DIR / f"{sym}_5min.csv"
            if fp.exists():
                bars_cache[sym] = load_bars_from_csv(str(fp))
        print(f"  Loaded {len(bars_cache)} symbols")
        results = run_fl_filter_ablation(spy_bars, symbols, all_dates, bars_cache)
        print(f"\n  {'Variant':<28s} {'N':>4s} {'PF':>6s} {'WR%':>6s} {'Exp':>7s} "
              f"{'TotalR':>7s} {'AvgW':>6s} {'AvgL':>6s} {'TrnPF':>6s} {'TstPF':>6s} {'WfPF':>6s}")
        print(f"  {'-'*28} {'-'*4} {'-'*6} {'-'*6} {'-'*7} {'-'*7} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6}")
        for n, m in results:
            if m["n"] == 0:
                print(f"  {n:<28s}    0")
                continue
            print(f"  {n:<28s} {m['n']:4d} {pf_s(m['pf']):>6s} {m['wr']:5.1f}% "
                  f"{m['exp']:+6.3f} {m['total_r']:+6.1f} "
                  f"{m['avg_win']:+5.2f} {m['avg_loss']:5.2f} "
                  f"{pf_s(m['train_pf']):>6s} {pf_s(m['test_pf']):>6s} {pf_s(m['wf_pf']):>6s}")

    elif "--sc-inplay" in sys.argv:
        print("=" * 110)
        print("SC_SNIPER IN-PLAY EXPANSION (tgt_2R base)")
        print("=" * 110)
        spy_bars, symbols, all_dates = load_universe()
        print(f"  Universe: {len(symbols)} syms, {len(all_dates)} days")
        print("  Preloading bar data...")
        bars_cache: Dict[str, List[Bar]] = {}
        for sym in symbols:
            fp = DATA_DIR / f"{sym}_5min.csv"
            if fp.exists():
                bars_cache[sym] = load_bars_from_csv(str(fp))
        print(f"  Loaded {len(bars_cache)} symbols")
        results = run_sc_inplay_expansion(spy_bars, symbols, all_dates, bars_cache)
        print(f"\n  {'Variant':<28s} {'N':>4s} {'PF':>6s} {'WR%':>6s} {'Exp':>7s} "
              f"{'IPpass':>6s} {'AvgW':>6s} {'AvgL':>6s} {'TrnPF':>6s} {'TstPF':>6s} {'WfPF':>6s}")
        print(f"  {'-'*28} {'-'*4} {'-'*6} {'-'*6} {'-'*7} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6}")
        for n, m, ip_ct in results:
            if m["n"] == 0:
                print(f"  {n:<28s}    0  {'':>6s} {'':>6s} {'':>7s} {ip_ct:>6d}")
                continue
            print(f"  {n:<28s} {m['n']:4d} {pf_s(m['pf']):>6s} {m['wr']:5.1f}% "
                  f"{m['exp']:+6.3f} {ip_ct:>6d} "
                  f"{m['avg_win']:+5.2f} {m['avg_loss']:5.2f} "
                  f"{pf_s(m['train_pf']):>6s} {pf_s(m['test_pf']):>6s} {pf_s(m['wf_pf']):>6s}")

    else:
        main()
