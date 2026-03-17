"""
DEPRECATED — Legacy Strategy Replay Harness.

WARNING: This harness uses EnhancedMarketRegime with END-OF-DAY look-ahead bias.
It is NOT authoritative for PF benchmarking. Use replay.py (the live-path replay)
instead, which uses the same shared gate modules as the live dashboard.

Known issues (from parity audit 2026-03-16):
  1. Look-ahead regime: day labels use SPY end-of-day close (GREEN/RED)
  2. In-play data window: feeds 5-min bars to 1-min config → 75 min window
  3. FAILED_SHORT gate uses old 3-condition logic vs shared 2-condition
  4. Quality regime_score uses look-ahead day label instead of real-time trend
  5. Regime applied at day level (scan_day Step 2) vs per-signal bar level

Kept for raw signal detection reference only.

Old usage:
    python -m alert_overlay.strategies.replay_legacy
"""

import csv
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..backtest import load_bars_from_csv
from ..market_context import SECTOR_MAP, get_sector_etf

from .shared.signal_schema import StrategySignal, StrategyTrade, QualityTier
from .shared.config import StrategyConfig
from .shared.in_play_proxy import InPlayProxy
from .shared.market_regime import EnhancedMarketRegime
from .shared.rejection_filters import RejectionFilters
from .shared.quality_scoring import QualityScorer
from .shared.helpers import simulate_strategy_trade, compute_daily_atr

from .second_chance_sniper import SecondChanceSniperStrategy
from .fl_antichop_only import FLAntiChopStrategy
from .spencer_atier import SpencerATierStrategy
from .hitchhiker_quality import HitchHikerQualityStrategy
from .ema_fpip_atier import EmaFpipATierStrategy
from .bdr_short import BDRShortStrategy
from .ema9_first_touch import EMA9FirstTouchStrategy
from .backside_structure import BacksideStructureStrategy
from .orl_failed_bd_long import ORLFailedBDLongStrategy


DATA_DIR = Path(__file__).parent.parent / "data"
OUT_DIR = Path(__file__).parent.parent / "outputs"


# ════════════════════════════════════════════════════════════════
#  Passthrough proxies for ungated replay mode
# ════════════════════════════════════════════════════════════════

class _PassthroughInPlay:
    """Always-pass in-play proxy. Matches InPlayProxy interface."""

    def precompute(self, symbol, bars):
        pass

    def is_in_play(self, symbol, dt):
        return True, 5.0  # always passes with max score

    def summary_stats(self):
        return {"total_symbol_days": 0, "passed_symbol_days": 0, "pass_rate_pct": 100.0}


class _PassthroughRegime:
    """Always-pass regime proxy. Delegates data methods to real regime,
    but all is_aligned_* gates return True."""

    def __init__(self, real_regime):
        self._real = real_regime

    def __getattr__(self, name):
        """Delegate any unknown method to real regime (data lookups, etc.)."""
        return getattr(self._real, name)

    def is_aligned_long(self, dt):
        return True

    def is_aligned_short(self, dt):
        return True

    def is_aligned_failed_long(self, dt):
        return True

    def is_aligned_failed_short(self, dt):
        return True


# ════════════════════════════════════════════════════════════════
#  Metrics
# ════════════════════════════════════════════════════════════════

def _pf(trades: List[StrategyTrade]) -> float:
    if not trades:
        return 0.0
    gw = sum(t.pnl_rr for t in trades if t.pnl_rr > 0)
    gl = abs(sum(t.pnl_rr for t in trades if t.pnl_rr <= 0))
    return gw / gl if gl > 0 else float("inf")


def pf_str(v: float) -> str:
    return f"{v:.2f}" if v < 999 else "inf"


def compute_metrics(trades: List[StrategyTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0, "pf": 0, "exp": 0, "total_r": 0, "max_dd_r": 0,
                "avg_win": 0, "avg_loss": 0,
                "train_pf": 0, "test_pf": 0, "train_n": 0, "test_n": 0,
                "wf_pf": 0, "wf_n": 0,
                "ex_best_day_pf": 0, "ex_top_sym_pf": 0,
                "stop_rate": 0, "target_rate": 0, "time_rate": 0, "avg_bars_held": 0,
                "days_active": 0, "avg_per_day": 0, "pct_pos_days": 0}

    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in trades)
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")

    avg_win = gw / len(wins) if wins else 0
    avg_loss = gl / len(losses) if losses else 0

    # Max drawdown
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: (x.entry_date, x.signal.timestamp)):
        cum += t.pnl_rr
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    # Time-ordered train/test split: first 60% of dates = train, last 40% = test
    all_dates = sorted(set(t.entry_date for t in trades))
    split_idx = int(len(all_dates) * 0.60)
    train_dates = set(all_dates[:split_idx])
    test_dates = set(all_dates[split_idx:])
    train = [t for t in trades if t.entry_date in train_dates]
    test = [t for t in trades if t.entry_date in test_dates]

    # Walk-forward: first 67% train, last 33% test
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

    days_active = len(set(t.entry_date for t in trades))
    avg_per_day = n / days_active if days_active else 0
    day_pnl = list(day_r.values())
    pos_days = sum(1 for d in day_pnl if d > 0)
    pct_pos = pos_days / len(day_pnl) * 100 if day_pnl else 0
    avg_bars = sum(t.bars_held for t in trades) / n

    return {
        "n": n, "wr": len(wins) / n * 100, "pf": pf, "exp": total_r / n,
        "total_r": total_r, "max_dd_r": max_dd,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "train_pf": _pf(train), "test_pf": _pf(test),
        "train_n": len(train), "test_n": len(test),
        "wf_pf": _pf(wf_test), "wf_n": len(wf_test),
        "ex_best_day_pf": _pf(ex_best), "ex_top_sym_pf": _pf(ex_top),
        "stop_rate": stopped / n * 100, "target_rate": target / n * 100,
        "time_rate": timed / n * 100, "avg_bars_held": avg_bars,
        "days_active": days_active, "avg_per_day": avg_per_day,
        "pct_pos_days": pct_pos,
    }


def _print_detail(label: str, trades: List[StrategyTrade]) -> dict:
    m = compute_metrics(trades)
    print(f"\n  ── {label} ──")
    print(f"  N={m['n']}  PF={pf_str(m['pf'])}  Exp={m['exp']:+.3f}R  TotalR={m['total_r']:+.1f}  "
          f"WR={m['wr']:.1f}%  MaxDD={m['max_dd_r']:.1f}R")
    print(f"  AvgWin={m['avg_win']:+.2f}R  AvgLoss={m['avg_loss']:.2f}R")
    print(f"  Train: PF={pf_str(m['train_pf'])} N={m['train_n']}  |  "
          f"Test: PF={pf_str(m['test_pf'])} N={m['test_n']}  |  "
          f"WalkFwd: PF={pf_str(m['wf_pf'])} N={m['wf_n']}")
    print(f"  ExDay={pf_str(m['ex_best_day_pf'])}  ExSym={pf_str(m['ex_top_sym_pf'])}")
    print(f"  Stop={m['stop_rate']:.1f}%  Tgt={m['target_rate']:.1f}%  Time={m['time_rate']:.1f}%  "
          f"AvgBars={m['avg_bars_held']:.1f}")
    print(f"  Days={m['days_active']}  Avg/day={m['avg_per_day']:.2f}  %Pos={m['pct_pos_days']:.1f}%")

    # Promotion criteria
    c1 = m["pf"] > 1.0
    c2 = m["exp"] > 0
    c3 = m["train_pf"] > 0.80
    c4 = m["test_pf"] > 0.80
    c5 = m["wf_pf"] > 0.80
    n_ok = m["n"] >= 10
    all_pass = c1 and c2 and c3 and c4 and c5 and n_ok
    print(f"  Criteria: N≥10={'P' if n_ok else 'F'}  PF>1.0={'P' if c1 else 'F'}  "
          f"Exp>0={'P' if c2 else 'F'}  TrnPF>0.80={'P' if c3 else 'F'}  "
          f"TstPF>0.80={'P' if c4 else 'F'}  WfPF>0.80={'P' if c5 else 'F'}  "
          f"→ {'PROMOTED' if all_pass else 'FAIL'}")

    # Monthly
    monthly_r = defaultdict(float)
    monthly_n = defaultdict(int)
    for t in trades:
        key = t.entry_date.strftime("%Y-%m")
        monthly_r[key] += t.pnl_rr
        monthly_n[key] += 1
    cum = 0.0
    print(f"  Monthly:")
    for mo in sorted(monthly_r):
        cum += monthly_r[mo]
        print(f"    {mo}  N={monthly_n[mo]:3d}  R={monthly_r[mo]:+7.1f}  cum={cum:+.1f}")

    return m


def _print_pipeline(label: str, stats: dict):
    """Print pipeline funnel stats."""
    print(f"\n  {label} pipeline:")
    print(f"    Step 1 (in-play):     {stats.get('passed_in_play', 0)} symbol-days passed / "
          f"{stats.get('total_symbol_days', 0)} total")
    print(f"    Step 2 (regime):      {stats.get('passed_regime', 0)} symbol-days passed")
    print(f"    Step 3 (raw detect):  {stats.get('raw_signals', 0)} raw signals")

    rej_total = stats.get('raw_signals', 0) - stats.get('passed_rejection', 0)
    print(f"    Step 4 (rejection):   {stats.get('passed_rejection', 0)} passed "
          f"({rej_total} rejected)")
    if stats.get("reject_reasons"):
        reasons_str = ", ".join(
            f"{k}={v}" for k, v in sorted(stats["reject_reasons"].items(), key=lambda x: -x[1])
        )
        print(f"      Reasons: {reasons_str}")

    print(f"    Step 5 (quality):     A={stats.get('a_tier', 0)}, "
          f"B={stats.get('b_tier', 0)}, C={stats.get('c_tier', 0)}")
    print(f"    Step 6 (A-tier):      {stats.get('a_tier', 0)} trade signals")


def _export_csv(trades: List[StrategyTrade], path: Path, label: str):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["section", "date", "entry_time", "exit_time", "symbol", "side",
                     "strategy", "pnl_rr", "exit_reason", "bars_held", "quality",
                     "quality_tier", "in_play_score", "regime"])
        for t in sorted(trades, key=lambda x: x.signal.timestamp):
            w.writerow([
                label,
                str(t.entry_date),
                t.signal.timestamp.strftime("%Y-%m-%d %H:%M"),
                t.exit_time.strftime("%Y-%m-%d %H:%M") if t.exit_time else "",
                t.symbol,
                "LONG" if t.signal.direction == 1 else "SHORT",
                t.signal.strategy_name,
                f"{t.pnl_rr:+.4f}",
                t.exit_reason,
                t.bars_held,
                t.signal.quality_score,
                t.signal.quality_tier.value,
                f"{t.signal.in_play_score:.1f}",
                t.signal.market_regime,
            ])
    print(f"  → Exported {len(trades)} trades to {path.name}")


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    ungated = "--ungated" in sys.argv
    mode_label = "UNGATED (matches live)" if ungated else "GATED (in-play + regime)"

    print("=" * 100)
    print(f"STRATEGY REPLAY — Portfolio D [{mode_label}]")
    print("=" * 100)

    # ── Load data ──
    print("\n  Loading data...")
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    sector_etfs = sorted(set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    sector_bars_dict = {}
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    excluded = {"SPY", "QQQ", "IWM"} | set(sector_etfs)
    symbols = sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])

    spy_dates = sorted(set(b.timestamp.date() for b in spy_bars))
    date_range = f"{spy_dates[0]} → {spy_dates[-1]} ({len(spy_dates)} days)"
    print(f"  Universe: {len(symbols)} symbols, {date_range}")

    # ── Initialize framework ──
    # Dual-timeframe config matching live dashboard architecture:
    #   ip_cfg  (timeframe_min=1): in-play gate uses first 15 1-min bars, gap>=0.5%, rvol>=1.5, dolvol>=$500K
    #   strat_cfg (timeframe_min=5): strategy detection uses 5-min calibrated params (time windows, bar counts, ATR multiples)
    print("  Initializing framework (dual-timeframe: 1m in-play, 5m strategies)...")
    ip_cfg = StrategyConfig(timeframe_min=1)
    strat_cfg = StrategyConfig(timeframe_min=5)
    real_regime = EnhancedMarketRegime(spy_bars, strat_cfg)
    rejection = RejectionFilters(strat_cfg)
    quality_scorer = QualityScorer(strat_cfg)

    if ungated:
        print("  *** UNGATED MODE: skipping in-play + regime gates ***")
        in_play = _PassthroughInPlay()
        regime = _PassthroughRegime(real_regime)
    else:
        in_play = InPlayProxy(ip_cfg)  # 1-min thresholds for in-play gate
        regime = real_regime

    # Precompute regime
    print("  Precomputing market regime...")
    regime.precompute()
    regime_dist = regime.day_label_distribution()
    print(f"  Regime: {regime_dist}")

    # ── Precompute in-play + run strategies ──
    # Strategies get strat_cfg (5-min params) but in_play proxy uses ip_cfg (1-min thresholds) internally
    sc_strategy = SecondChanceSniperStrategy(strat_cfg, in_play, regime, rejection, quality_scorer)
    fl_strategy = FLAntiChopStrategy(strat_cfg, in_play, regime, rejection, quality_scorer)
    sp_strategy = SpencerATierStrategy(strat_cfg, in_play, regime, rejection, quality_scorer)
    hh_strategy = HitchHikerQualityStrategy(strat_cfg, in_play, regime, rejection, quality_scorer)
    fpip_strategy = EmaFpipATierStrategy(strat_cfg, in_play, regime, rejection, quality_scorer)
    bdr_strategy = BDRShortStrategy(strat_cfg, in_play, regime, rejection, quality_scorer)
    e9ft_strategy = EMA9FirstTouchStrategy(strat_cfg, in_play, regime, rejection, quality_scorer)
    bs_strategy = BacksideStructureStrategy(strat_cfg, in_play, regime, rejection, quality_scorer)
    orl_strategy = ORLFailedBDLongStrategy(strat_cfg, in_play, regime, rejection, quality_scorer)

    sc_trades: List[StrategyTrade] = []
    fl_trades: List[StrategyTrade] = []
    sp_trades: List[StrategyTrade] = []
    hh_trades: List[StrategyTrade] = []
    fpip_trades: List[StrategyTrade] = []
    bdr_trades: List[StrategyTrade] = []
    e9ft_trades: List[StrategyTrade] = []
    bs_trades: List[StrategyTrade] = []
    orl_trades: List[StrategyTrade] = []

    print(f"\n  Processing {len(symbols)} symbols...")
    for idx, sym in enumerate(symbols):
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue

        # Precompute in-play for this symbol
        in_play.precompute(sym, bars)

        # Get sector bars
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None

        # Precompute daily ATR for Spencer
        daily_atr = compute_daily_atr(bars) if strat_cfg.enable_spencer else None

        # Get all trading days for this symbol
        sym_dates = sorted(set(b.timestamp.date() for b in bars))

        for day in sym_dates:
            # SC Sniper
            if strat_cfg.enable_sc_sniper:
                sc_sigs = sc_strategy.scan_day(sym, bars, day, spy_bars, sec_bars)
                for sig in sc_sigs:
                    if sig.is_tradeable:
                        bar_idx = _find_bar_idx(bars, sig.timestamp)
                        if bar_idx >= 0:
                            actual_rr = sig.metadata.get("actual_rr", strat_cfg.sc_target_rr)
                            trade = simulate_strategy_trade(
                                sig, bars, bar_idx,
                                max_bars=strat_cfg.get(strat_cfg.sc_max_bars),
                                target_rr=actual_rr,
                            )
                            sc_trades.append(trade)

            # FL AntiChop
            if strat_cfg.enable_fl_antichop:
                fl_sigs = fl_strategy.scan_day(sym, bars, day, spy_bars, sec_bars)
                for sig in fl_sigs:
                    if sig.is_tradeable:
                        bar_idx = _find_bar_idx(bars, sig.timestamp)
                        if bar_idx >= 0:
                            actual_rr = sig.metadata.get("actual_rr", strat_cfg.fl_target_rr)
                            trade = simulate_strategy_trade(
                                sig, bars, bar_idx,
                                max_bars=strat_cfg.get(strat_cfg.fl_max_bars),
                                target_rr=actual_rr,
                            )
                            fl_trades.append(trade)

            # Spencer A-Tier
            if strat_cfg.enable_spencer:
                sp_sigs = sp_strategy.scan_day(sym, bars, day, spy_bars, sec_bars,
                                               daily_atr=daily_atr)
                for sig in sp_sigs:
                    if sig.is_tradeable:
                        bar_idx = _find_bar_idx(bars, sig.timestamp)
                        if bar_idx >= 0:
                            actual_rr = sig.metadata.get("actual_rr", strat_cfg.sp_target_rr)
                            trade = simulate_strategy_trade(
                                sig, bars, bar_idx,
                                max_bars=strat_cfg.get(strat_cfg.sp_max_bars),
                                target_rr=actual_rr,
                            )
                            sp_trades.append(trade)

            # HitchHiker Quality
            if strat_cfg.enable_hitchhiker:
                hh_sigs = hh_strategy.scan_day(sym, bars, day, spy_bars, sec_bars)
                for sig in hh_sigs:
                    if sig.is_tradeable:
                        bar_idx = _find_bar_idx(bars, sig.timestamp)
                        if bar_idx >= 0:
                            actual_rr = sig.metadata.get("actual_rr", strat_cfg.hh_target_rr)
                            trade = simulate_strategy_trade(
                                sig, bars, bar_idx,
                                max_bars=strat_cfg.get(strat_cfg.hh_max_bars),
                                target_rr=actual_rr,
                            )
                            hh_trades.append(trade)

            # EMA FPIP
            if strat_cfg.enable_fpip:
                fpip_sigs = fpip_strategy.scan_day(sym, bars, day, spy_bars, sec_bars)
                for sig in fpip_sigs:
                    if sig.is_tradeable:
                        bar_idx = _find_bar_idx(bars, sig.timestamp)
                        if bar_idx >= 0:
                            actual_rr = sig.metadata.get("actual_rr", strat_cfg.fpip_target_rr)
                            trade = simulate_strategy_trade(
                                sig, bars, bar_idx,
                                max_bars=strat_cfg.get(strat_cfg.fpip_max_bars),
                                target_rr=actual_rr,
                            )
                            fpip_trades.append(trade)

            # BDR SHORT
            if strat_cfg.enable_bdr:
                bdr_sigs = bdr_strategy.scan_day(sym, bars, day, spy_bars, sec_bars)
                for sig in bdr_sigs:
                    if sig.is_tradeable:
                        bar_idx = _find_bar_idx(bars, sig.timestamp)
                        if bar_idx >= 0:
                            actual_rr = sig.metadata.get("actual_rr", strat_cfg.bdr_target_rr)
                            trade = simulate_strategy_trade(
                                sig, bars, bar_idx,
                                max_bars=strat_cfg.get(strat_cfg.bdr_max_bars),
                                target_rr=actual_rr,
                            )
                            bdr_trades.append(trade)

            # EMA9 FirstTouch
            if strat_cfg.enable_ema9ft:
                e9ft_sigs = e9ft_strategy.scan_day(sym, bars, day, spy_bars, sec_bars)
                for sig in e9ft_sigs:
                    if sig.is_tradeable:
                        bar_idx = _find_bar_idx(bars, sig.timestamp)
                        if bar_idx >= 0:
                            actual_rr = sig.metadata.get("actual_rr", strat_cfg.e9ft_target_rr)
                            trade = simulate_strategy_trade(
                                sig, bars, bar_idx,
                                max_bars=strat_cfg.get(strat_cfg.e9ft_max_bars),
                                target_rr=actual_rr,
                            )
                            e9ft_trades.append(trade)

            # Backside Structure
            if strat_cfg.enable_backside:
                bs_sigs = bs_strategy.scan_day(sym, bars, day, spy_bars, sec_bars)
                for sig in bs_sigs:
                    if sig.is_tradeable:
                        bar_idx = _find_bar_idx(bars, sig.timestamp)
                        if bar_idx >= 0:
                            # Use actual_rr from metadata for VWAP target trades
                            actual_rr = sig.metadata.get("actual_rr", strat_cfg.bs_target_rr)
                            trade = simulate_strategy_trade(
                                sig, bars, bar_idx,
                                max_bars=strat_cfg.get(strat_cfg.bs_max_bars),
                                target_rr=actual_rr,
                            )
                            bs_trades.append(trade)

            # ORL Failed Breakdown Long
            if strat_cfg.enable_orl_fbd:
                orl_sigs = orl_strategy.scan_day(sym, bars, day, spy_bars, sec_bars)
                for sig in orl_sigs:
                    if sig.is_tradeable:
                        bar_idx = _find_bar_idx(bars, sig.timestamp)
                        if bar_idx >= 0:
                            actual_rr = sig.metadata.get("actual_rr", strat_cfg.orl_target_rr)
                            trade = simulate_strategy_trade(
                                sig, bars, bar_idx,
                                max_bars=strat_cfg.get(strat_cfg.orl_max_bars),
                                target_rr=actual_rr,
                            )
                            orl_trades.append(trade)

        if (idx + 1) % 20 == 0:
            print(f"    {idx + 1}/{len(symbols)} symbols done...")

    # ── Pipeline stats ──
    print(f"\n{'=' * 100}")
    print("PIPELINE VISIBILITY")
    print(f"{'=' * 100}")
    _print_pipeline("SC_SNIPER", sc_strategy.stats)
    _print_pipeline("FL_ANTICHOP", fl_strategy.stats)
    _print_pipeline("SP_ATIER", sp_strategy.stats)
    _print_pipeline("HH_QUALITY", hh_strategy.stats)
    _print_pipeline("EMA_FPIP", fpip_strategy.stats)
    _print_pipeline("BDR_SHORT", bdr_strategy.stats)
    _print_pipeline("EMA9_FT", e9ft_strategy.stats)
    _print_pipeline("BS_STRUCT", bs_strategy.stats)
    _print_pipeline("ORL_FBD_LONG", orl_strategy.stats)

    # In-play stats
    ip_stats = in_play.summary_stats()
    print(f"\n  In-play summary: {ip_stats['passed_symbol_days']}/{ip_stats['total_symbol_days']} "
          f"symbol-days passed ({ip_stats['pass_rate_pct']:.1f}%)")

    # ── Trade metrics ──
    print(f"\n{'=' * 100}")
    print("TRADE METRICS — STANDALONE")
    print(f"{'=' * 100}")

    sc_m = _print_detail("SC SNIPER", sc_trades)
    fl_m = _print_detail("FL ANTICHOP", fl_trades)
    sp_m = _print_detail("SP ATIER", sp_trades)
    hh_m = _print_detail("HH QUALITY", hh_trades)
    fpip_m = _print_detail("EMA FPIP", fpip_trades)
    bdr_m = _print_detail("BDR SHORT", bdr_trades)
    e9ft_m = _print_detail("EMA9 FT", e9ft_trades)
    bs_m = _print_detail("BS STRUCT", bs_trades)
    orl_m = _print_detail("ORL FBD LONG", orl_trades)

    # Combined (longs only)
    long_trades = sc_trades + fl_trades + sp_trades + hh_trades + fpip_trades + e9ft_trades + bs_trades + orl_trades
    print(f"\n{'=' * 100}")
    print("COMBINED — ALL LONG STRATEGIES")
    print(f"{'=' * 100}")
    long_m = _print_detail("COMBINED LONG", long_trades)

    # Combined (all)
    all_trades = long_trades + bdr_trades
    print(f"\n{'=' * 100}")
    print("COMBINED — ALL 9 STRATEGIES (LONG + SHORT)")
    print(f"{'=' * 100}")
    all_m = _print_detail("COMBINED ALL", all_trades)

    # ── Top symbols ──
    print(f"\n{'=' * 100}")
    print("TOP SYMBOLS")
    print(f"{'=' * 100}")
    for label, trades in [("SC SNIPER", sc_trades), ("FL ANTICHOP", fl_trades),
                           ("SP ATIER", sp_trades), ("HH QUALITY", hh_trades),
                           ("EMA FPIP", fpip_trades), ("BDR SHORT", bdr_trades),
                           ("EMA9 FT", e9ft_trades), ("BS STRUCT", bs_trades),
                           ("ORL FBD", orl_trades)]:
        sym_r = defaultdict(float)
        sym_n = defaultdict(int)
        for t in trades:
            sym_r[t.symbol] += t.pnl_rr
            sym_n[t.symbol] += 1
        print(f"\n  {label} Top 10:")
        for sym in sorted(sym_r, key=sym_r.get, reverse=True)[:10]:
            print(f"    {sym:>8s}  N={sym_n[sym]:3d}  R={sym_r[sym]:+.1f}")

    # ── Export ──
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _export_csv(all_trades, OUT_DIR / "replay_strategy_combined.csv", "strategy_combined")
    if sc_trades:
        _export_csv(sc_trades, OUT_DIR / "replay_sc_sniper.csv", "sc_sniper")
    if fl_trades:
        _export_csv(fl_trades, OUT_DIR / "replay_fl_antichop.csv", "fl_antichop")
    if sp_trades:
        _export_csv(sp_trades, OUT_DIR / "replay_sp_atier.csv", "sp_atier")
    if hh_trades:
        _export_csv(hh_trades, OUT_DIR / "replay_hh_quality.csv", "hh_quality")
    if fpip_trades:
        _export_csv(fpip_trades, OUT_DIR / "replay_ema_fpip.csv", "ema_fpip")
    if bdr_trades:
        _export_csv(bdr_trades, OUT_DIR / "replay_bdr_short.csv", "bdr_short")
    if e9ft_trades:
        _export_csv(e9ft_trades, OUT_DIR / "replay_ema9_ft.csv", "ema9_ft")
    if bs_trades:
        _export_csv(bs_trades, OUT_DIR / "replay_bs_struct.csv", "bs_struct")
    if orl_trades:
        _export_csv(orl_trades, OUT_DIR / "replay_orl_fbd_long.csv", "orl_fbd_long")

    # ── Final summary ──
    print(f"\n{'=' * 100}")
    print("FINAL SUMMARY")
    print(f"{'=' * 100}")
    print(f"  SC SNIPER:   N={sc_m['n']}, PF={pf_str(sc_m['pf'])}, "
          f"Exp={sc_m['exp']:+.3f}R, TotalR={sc_m['total_r']:+.1f}")
    print(f"  FL ANTICHOP: N={fl_m['n']}, PF={pf_str(fl_m['pf'])}, "
          f"Exp={fl_m['exp']:+.3f}R, TotalR={fl_m['total_r']:+.1f}")
    print(f"  SP ATIER:    N={sp_m['n']}, PF={pf_str(sp_m['pf'])}, "
          f"Exp={sp_m['exp']:+.3f}R, TotalR={sp_m['total_r']:+.1f}")
    print(f"  HH QUALITY:  N={hh_m['n']}, PF={pf_str(hh_m['pf'])}, "
          f"Exp={hh_m['exp']:+.3f}R, TotalR={hh_m['total_r']:+.1f}")
    print(f"  EMA FPIP:    N={fpip_m['n']}, PF={pf_str(fpip_m['pf'])}, "
          f"Exp={fpip_m['exp']:+.3f}R, TotalR={fpip_m['total_r']:+.1f}")
    print(f"  BDR SHORT:   N={bdr_m['n']}, PF={pf_str(bdr_m['pf'])}, "
          f"Exp={bdr_m['exp']:+.3f}R, TotalR={bdr_m['total_r']:+.1f}")
    print(f"  EMA9 FT:     N={e9ft_m['n']}, PF={pf_str(e9ft_m['pf'])}, "
          f"Exp={e9ft_m['exp']:+.3f}R, TotalR={e9ft_m['total_r']:+.1f}")
    print(f"  BS STRUCT:   N={bs_m['n']}, PF={pf_str(bs_m['pf'])}, "
          f"Exp={bs_m['exp']:+.3f}R, TotalR={bs_m['total_r']:+.1f}")
    print(f"  ORL FBD:     N={orl_m['n']}, PF={pf_str(orl_m['pf'])}, "
          f"Exp={orl_m['exp']:+.3f}R, TotalR={orl_m['total_r']:+.1f}")
    print(f"  COMB LONG:   N={long_m['n']}, PF={pf_str(long_m['pf'])}, "
          f"Exp={long_m['exp']:+.3f}R, TotalR={long_m['total_r']:+.1f}")
    print(f"  COMB ALL:    N={all_m['n']}, PF={pf_str(all_m['pf'])}, "
          f"Exp={all_m['exp']:+.3f}R, TotalR={all_m['total_r']:+.1f}")

    print(f"\n{'=' * 100}")
    print("DONE.")
    print(f"{'=' * 100}")


def _find_bar_idx(bars: List, timestamp: datetime) -> int:
    """Find index of bar with matching timestamp."""
    for i, b in enumerate(bars):
        if b.timestamp == timestamp:
            return i
    return -1


if __name__ == "__main__":
    if "--help" in sys.argv:
        print("Usage: python -m alert_overlay.strategies.replay [--ungated]")
        print("  --ungated  Skip in-play + regime gating (matches live pipeline)")
        sys.exit(0)
    main()
