"""
Regime Gate Study — Tests hypothesis that in-play stocks don't need regime gating.

Tests 4 regime modes:
  1. GREEN_ONLY   — current default (require SPY up day)
  2. NOT_DEEP_RED — block only strongly negative days (SPY down > 0.50%)
  3. NOT_RED      — block RED days, allow GREEN + FLAT
  4. NO_GATE      — no regime filtering at all

For each mode, runs all 4 strategies through full pipeline and reports metrics.

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.strategies.regime_study
"""

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


DATA_DIR = Path(__file__).parent.parent / "data"


def _find_bar_idx(bars, ts):
    for i, b in enumerate(bars):
        if b.timestamp == ts:
            return i
    return -1


def _pf(trades):
    if not trades:
        return 0.0
    gw = sum(t.pnl_rr for t in trades if t.pnl_rr > 0)
    gl = abs(sum(t.pnl_rr for t in trades if t.pnl_rr <= 0))
    return gw / gl if gl > 0 else float("inf")


def pf_s(v):
    return f"{v:.2f}" if v < 999 else "inf"


def compute_metrics(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0, "pf": 0, "wr": 0, "exp": 0, "total_r": 0, "max_dd": 0,
                "avg_win": 0, "avg_loss": 0, "train_pf": 0, "test_pf": 0,
                "wf_pf": 0, "train_n": 0, "test_n": 0, "wf_n": 0}
    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in trades)
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    avg_win = gw / len(wins) if wins else 0
    avg_loss = gl / len(losses) if losses else 0
    # Max DD
    cum = peak = max_dd = 0.0
    for t in sorted(trades, key=lambda x: (x.entry_date, x.signal.timestamp)):
        cum += t.pnl_rr
        if cum > peak: peak = cum
        dd = peak - cum
        if dd > max_dd: max_dd = dd
    # Train/test
    all_dates = sorted(set(t.entry_date for t in trades))
    si = int(len(all_dates) * 0.60)
    train_d = set(all_dates[:si])
    test_d = set(all_dates[si:])
    train = [t for t in trades if t.entry_date in train_d]
    test = [t for t in trades if t.entry_date in test_d]
    # Walk-forward
    wf_si = int(len(all_dates) * 0.67)
    wf_d = set(all_dates[wf_si:])
    wf = [t for t in trades if t.entry_date in wf_d]
    return {
        "n": n, "pf": pf, "wr": len(wins)/n*100, "exp": total_r/n,
        "total_r": total_r, "max_dd": max_dd,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "train_pf": _pf(train), "test_pf": _pf(test), "wf_pf": _pf(wf),
        "train_n": len(train), "test_n": len(test), "wf_n": len(wf),
    }


class FlexRegime(EnhancedMarketRegime):
    """Extended regime that supports multiple gating modes."""

    def __init__(self, spy_bars, cfg, mode="GREEN_ONLY"):
        super().__init__(spy_bars, cfg)
        self.mode = mode  # GREEN_ONLY, NOT_RED, NOT_DEEP_RED, NO_GATE

    def is_aligned_long(self, dt) -> bool:
        if self.mode == "NO_GATE":
            return True

        d = dt.date() if isinstance(dt, datetime) else dt
        label = self._day_labels.get(d, "FLAT")

        if self.mode == "GREEN_ONLY":
            return label == "GREEN"

        if self.mode == "NOT_RED":
            # Allow GREEN + FLAT, block RED
            return label != "RED"

        if self.mode == "NOT_DEEP_RED":
            # Allow everything except SPY down > 0.50%
            if label != "RED":
                return True
            # Check how deep the red is
            daily_bars = defaultdict(list)
            for b in self._spy_bars:
                if b.timestamp.date() == d:
                    daily_bars[d].append(b)
            bars_today = daily_bars.get(d, [])
            if not bars_today:
                return False
            o = bars_today[0].open
            c = bars_today[-1].close
            chg_pct = (c - o) / o * 100 if o > 0 else 0.0
            return chg_pct > -0.50  # allow mild red (down < 0.50%)

        return True


def run_regime_variant(mode, spy_bars, symbols, sector_bars_dict, data_dir, cfg_base):
    """Run all 4 strategies with a specific regime mode."""
    cfg = StrategyConfig(timeframe_min=5)
    # Copy tuned params
    cfg.hh_target_rr = cfg_base.hh_target_rr

    in_play = InPlayProxy(cfg)
    regime = FlexRegime(spy_bars, cfg, mode=mode)
    rejection = RejectionFilters(cfg)
    quality = QualityScorer(cfg)

    regime.precompute()

    sc_strat = SecondChanceSniperStrategy(cfg, in_play, regime, rejection, quality)
    fl_strat = FLAntiChopStrategy(cfg, in_play, regime, rejection, quality)
    sp_strat = SpencerATierStrategy(cfg, in_play, regime, rejection, quality)
    hh_strat = HitchHikerQualityStrategy(cfg, in_play, regime, rejection, quality)

    sc_trades, fl_trades, sp_trades, hh_trades = [], [], [], []

    for sym in symbols:
        p = data_dir / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue

        in_play.precompute(sym, bars)
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf)
        daily_atr = compute_daily_atr(bars)
        sym_dates = sorted(set(b.timestamp.date() for b in bars))

        for day in sym_dates:
            # SC
            for sig in sc_strat.scan_day(sym, bars, day, spy_bars, sec_bars):
                if sig.is_tradeable:
                    bi = _find_bar_idx(bars, sig.timestamp)
                    if bi >= 0:
                        sc_trades.append(simulate_strategy_trade(
                            sig, bars, bi, max_bars=cfg.get(cfg.sc_max_bars),
                            target_rr=cfg.sc_target_rr))

            # FL
            for sig in fl_strat.scan_day(sym, bars, day, spy_bars, sec_bars):
                if sig.is_tradeable:
                    bi = _find_bar_idx(bars, sig.timestamp)
                    if bi >= 0:
                        fl_trades.append(simulate_strategy_trade(
                            sig, bars, bi, max_bars=cfg.get(cfg.fl_max_bars),
                            target_rr=cfg.fl_target_rr))

            # SP
            for sig in sp_strat.scan_day(sym, bars, day, spy_bars, sec_bars, daily_atr=daily_atr):
                if sig.is_tradeable:
                    bi = _find_bar_idx(bars, sig.timestamp)
                    if bi >= 0:
                        sp_trades.append(simulate_strategy_trade(
                            sig, bars, bi, max_bars=cfg.get(cfg.sp_max_bars),
                            target_rr=cfg.sp_target_rr))

            # HH
            for sig in hh_strat.scan_day(sym, bars, day, spy_bars, sec_bars):
                if sig.is_tradeable:
                    bi = _find_bar_idx(bars, sig.timestamp)
                    if bi >= 0:
                        hh_trades.append(simulate_strategy_trade(
                            sig, bars, bi, max_bars=cfg.get(cfg.hh_max_bars),
                            target_rr=cfg.hh_target_rr))

    return {
        "SC_SNIPER": sc_trades,
        "FL_ANTICHOP": fl_trades,
        "SP_ATIER": sp_trades,
        "HH_QUALITY": hh_trades,
    }


def main():
    print("=" * 110)
    print("REGIME GATE STUDY — In-play stocks vs market regime")
    print("=" * 110)

    # Load data
    print("\n  Loading data...")
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))

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
    print(f"  Universe: {len(symbols)} symbols, {len(spy_dates)} days")

    # Regime day distribution
    cfg_base = StrategyConfig(timeframe_min=5)
    temp_regime = FlexRegime(spy_bars, cfg_base, "GREEN_ONLY")
    temp_regime.precompute()
    dist = temp_regime.day_label_distribution()
    print(f"  Day distribution: {dist}")

    # Count mild vs deep red
    deep_red = 0
    mild_red = 0
    for d in spy_dates:
        label = temp_regime.get_day_label(d)
        if label == "RED":
            # Get SPY change
            day_bars = [b for b in spy_bars if b.timestamp.date() == d]
            if day_bars:
                o = day_bars[0].open
                c = day_bars[-1].close
                chg = (c - o) / o * 100
                if chg <= -0.50:
                    deep_red += 1
                else:
                    mild_red += 1
    print(f"  RED breakdown: mild (>-0.50%) = {mild_red}, deep (<=-0.50%) = {deep_red}")

    modes = ["GREEN_ONLY", "NOT_RED", "NOT_DEEP_RED", "NO_GATE"]
    mode_labels = {
        "GREEN_ONLY": "GREEN only (current)",
        "NOT_RED": "GREEN + FLAT (block all RED)",
        "NOT_DEEP_RED": "Block only deep RED (SPY >-0.50%)",
        "NO_GATE": "No regime gate",
    }

    all_results = {}
    for mode in modes:
        print(f"\n{'='*110}")
        print(f"  REGIME MODE: {mode_labels[mode]}")
        print(f"{'='*110}")

        result = run_regime_variant(mode, spy_bars, symbols, sector_bars_dict, DATA_DIR, cfg_base)
        all_results[mode] = result

        combined = []
        for strat_name in ["SC_SNIPER", "FL_ANTICHOP", "SP_ATIER", "HH_QUALITY"]:
            trades = result[strat_name]
            m = compute_metrics(trades)
            print(f"\n  {strat_name:12s}  N={m['n']:3d}  PF={pf_s(m['pf']):>5s}  "
                  f"WR={m['wr']:5.1f}%  Exp={m['exp']:+.3f}R  TotalR={m['total_r']:+6.1f}  "
                  f"MaxDD={m['max_dd']:5.1f}R  "
                  f"TrnPF={pf_s(m['train_pf']):>5s}(N={m['train_n']:2d})  "
                  f"TstPF={pf_s(m['test_pf']):>5s}(N={m['test_n']:2d})  "
                  f"WfPF={pf_s(m['wf_pf']):>5s}(N={m['wf_n']:2d})")
            combined.extend(trades)

        m = compute_metrics(combined)
        print(f"\n  {'COMBINED':12s}  N={m['n']:3d}  PF={pf_s(m['pf']):>5s}  "
              f"WR={m['wr']:5.1f}%  Exp={m['exp']:+.3f}R  TotalR={m['total_r']:+6.1f}  "
              f"MaxDD={m['max_dd']:5.1f}R  "
              f"TrnPF={pf_s(m['train_pf']):>5s}(N={m['train_n']:2d})  "
              f"TstPF={pf_s(m['test_pf']):>5s}(N={m['test_n']:2d})  "
              f"WfPF={pf_s(m['wf_pf']):>5s}(N={m['wf_n']:2d})")

    # ── Comparison summary ──
    print(f"\n{'='*110}")
    print("COMPARISON SUMMARY")
    print(f"{'='*110}")
    print(f"\n  {'Mode':<35s} {'N':>4s} {'PF':>6s} {'WR%':>6s} {'Exp':>8s} {'TotalR':>8s} "
          f"{'MaxDD':>7s} {'TrnPF':>6s} {'TstPF':>6s} {'WfPF':>6s}")
    print(f"  {'-'*33} {'-'*4} {'-'*6} {'-'*6} {'-'*8} {'-'*8} {'-'*7} {'-'*6} {'-'*6} {'-'*6}")
    for mode in modes:
        result = all_results[mode]
        combined = []
        for strat_name in ["SC_SNIPER", "FL_ANTICHOP", "SP_ATIER", "HH_QUALITY"]:
            combined.extend(result[strat_name])
        m = compute_metrics(combined)
        print(f"  {mode_labels[mode]:<35s} {m['n']:4d} {pf_s(m['pf']):>6s} {m['wr']:5.1f}% "
              f"{m['exp']:+7.3f}R {m['total_r']:+7.1f}R {m['max_dd']:6.1f}R "
              f"{pf_s(m['train_pf']):>6s} {pf_s(m['test_pf']):>6s} {pf_s(m['wf_pf']):>6s}")

    # ── Per-strategy comparison ──
    for strat_name in ["SC_SNIPER", "FL_ANTICHOP", "SP_ATIER", "HH_QUALITY"]:
        print(f"\n  {strat_name}:")
        print(f"  {'Mode':<35s} {'N':>4s} {'PF':>6s} {'Exp':>8s} {'TotalR':>8s} "
              f"{'TrnPF':>6s} {'TstPF':>6s} {'WfPF':>6s}")
        print(f"  {'-'*33} {'-'*4} {'-'*6} {'-'*8} {'-'*8} {'-'*6} {'-'*6} {'-'*6}")
        for mode in modes:
            trades = all_results[mode][strat_name]
            m = compute_metrics(trades)
            print(f"  {mode_labels[mode]:<35s} {m['n']:4d} {pf_s(m['pf']):>6s} "
                  f"{m['exp']:+7.3f}R {m['total_r']:+7.1f}R "
                  f"{pf_s(m['train_pf']):>6s} {pf_s(m['test_pf']):>6s} {pf_s(m['wf_pf']):>6s}")

    print(f"\n{'='*110}")
    print("DONE.")
    print(f"{'='*110}")


if __name__ == "__main__":
    main()
