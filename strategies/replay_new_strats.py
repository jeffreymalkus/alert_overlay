"""
Replay evaluation for new strategies: ORH_FBO_SHORT + ORL_FBD_LONG.
Run from: cd /sessions/inspiring-clever-meitner/mnt && python -m alert_overlay.strategies.replay_new_strats
"""

from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import List

from ..backtest import load_bars_from_csv
from ..market_context import SECTOR_MAP, get_sector_etf

from .shared.signal_schema import StrategySignal, StrategyTrade, QualityTier
from .shared.config import StrategyConfig
from .shared.in_play_proxy import InPlayProxy
from .shared.market_regime import EnhancedMarketRegime
from .shared.rejection_filters import RejectionFilters
from .shared.quality_scoring import QualityScorer
from .shared.helpers import simulate_strategy_trade, compute_daily_atr

from .orh_failed_bo_short import ORHFailedBOShortStrategy
from .orl_failed_bd_long import ORLFailedBDLongStrategy

from .replay import _find_bar_idx, _print_detail, _print_pipeline, _export_csv, compute_metrics, pf_str

DATA_DIR = Path(__file__).parent.parent / "data"
OUT_DIR = Path(__file__).parent.parent / "outputs"


def main():
    print("=" * 100)
    print("STRATEGY REPLAY — New Failed-Move Strategies (ORH_FBO_SHORT + ORL_FBD_LONG)")
    print("=" * 100)

    # ── Load data ──
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
    print(f"  Universe: {len(symbols)} symbols, {spy_dates[0]} → {spy_dates[-1]} ({len(spy_dates)} days)")

    # ── Initialize framework ──
    cfg = StrategyConfig(timeframe_min=5)
    in_play = InPlayProxy(cfg)
    regime = EnhancedMarketRegime(spy_bars, cfg)
    rejection = RejectionFilters(cfg)
    quality_scorer = QualityScorer(cfg)

    regime.precompute()
    regime_dist = regime.day_label_distribution()
    print(f"  Regime: {regime_dist}")

    orh_strategy = ORHFailedBOShortStrategy(cfg, in_play, regime, rejection, quality_scorer)
    orl_strategy = ORLFailedBDLongStrategy(cfg, in_play, regime, rejection, quality_scorer)

    orh_trades: List[StrategyTrade] = []
    orl_trades: List[StrategyTrade] = []

    print(f"\n  Processing {len(symbols)} symbols...")
    for idx, sym in enumerate(symbols):
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue

        in_play.precompute(sym, bars)
        sym_dates = sorted(set(b.timestamp.date() for b in bars))

        for day in sym_dates:
            # ORH_FBO_SHORT
            orh_sigs = orh_strategy.scan_day(sym, bars, day, spy_bars)
            for sig in orh_sigs:
                if sig.is_tradeable:
                    bar_idx = _find_bar_idx(bars, sig.timestamp)
                    if bar_idx >= 0:
                        actual_rr = sig.metadata.get("actual_rr", cfg.orh_target_rr)
                        trade = simulate_strategy_trade(
                            sig, bars, bar_idx,
                            max_bars=cfg.get(cfg.orh_max_bars),
                            target_rr=actual_rr,
                        )
                        orh_trades.append(trade)

            # ORL_FBD_LONG
            orl_sigs = orl_strategy.scan_day(sym, bars, day, spy_bars)
            for sig in orl_sigs:
                if sig.is_tradeable:
                    bar_idx = _find_bar_idx(bars, sig.timestamp)
                    if bar_idx >= 0:
                        actual_rr = sig.metadata.get("actual_rr", cfg.orl_target_rr)
                        trade = simulate_strategy_trade(
                            sig, bars, bar_idx,
                            max_bars=cfg.get(cfg.orl_max_bars),
                            target_rr=actual_rr,
                        )
                        orl_trades.append(trade)

        if (idx + 1) % 20 == 0:
            print(f"    {idx + 1}/{len(symbols)} symbols done...")

    # ── Pipeline stats ──
    print(f"\n{'=' * 100}")
    print("PIPELINE VISIBILITY")
    print(f"{'=' * 100}")
    _print_pipeline("ORH_FBO_SHORT", orh_strategy.stats)
    _print_pipeline("ORL_FBD_LONG", orl_strategy.stats)

    # ── Trade metrics ──
    print(f"\n{'=' * 100}")
    print("TRADE METRICS — STANDALONE")
    print(f"{'=' * 100}")

    orh_m = _print_detail("ORH_FBO_SHORT", orh_trades)
    orl_m = _print_detail("ORL_FBD_LONG", orl_trades)

    # Combined
    all_trades = orh_trades + orl_trades
    print(f"\n{'=' * 100}")
    print("COMBINED — BOTH FAILED-MOVE STRATEGIES")
    print(f"{'=' * 100}")
    all_m = _print_detail("COMBINED FAILED-MOVE", all_trades)

    # ── Signal detail dump (first 20) ──
    print(f"\n{'=' * 100}")
    print("SAMPLE SIGNALS — ORH_FBO_SHORT (first 20)")
    print(f"{'=' * 100}")
    for t in sorted(orh_trades, key=lambda x: x.signal.timestamp)[:20]:
        s = t.signal
        print(f"  {s.timestamp.strftime('%Y-%m-%d %H:%M')} {s.symbol:>6s} "
              f"entry={s.entry_price:.2f} stop={s.stop_price:.2f} tgt={s.target_price:.2f} "
              f"pnl={t.pnl_rr:+.2f}R exit={t.exit_reason:<6s} "
              f"regime={s.market_regime} tags={s.confluence_tags}")

    print(f"\n{'=' * 100}")
    print("SAMPLE SIGNALS — ORL_FBD_LONG (first 20)")
    print(f"{'=' * 100}")
    for t in sorted(orl_trades, key=lambda x: x.signal.timestamp)[:20]:
        s = t.signal
        print(f"  {s.timestamp.strftime('%Y-%m-%d %H:%M')} {s.symbol:>6s} "
              f"entry={s.entry_price:.2f} stop={s.stop_price:.2f} tgt={s.target_price:.2f} "
              f"pnl={t.pnl_rr:+.2f}R exit={t.exit_reason:<6s} "
              f"regime={s.market_regime} tags={s.confluence_tags}")

    # ── Regime breakdown ──
    print(f"\n{'=' * 100}")
    print("REGIME BREAKDOWN")
    print(f"{'=' * 100}")
    for label, trades in [("ORH_FBO_SHORT", orh_trades), ("ORL_FBD_LONG", orl_trades)]:
        regime_r = defaultdict(list)
        for t in trades:
            regime_r[t.signal.market_regime].append(t)
        print(f"\n  {label}:")
        for reg in sorted(regime_r.keys()):
            tr = regime_r[reg]
            m = compute_metrics(tr)
            print(f"    {reg:>5s}: N={m['n']:3d}  PF={pf_str(m['pf'])}  Exp={m['exp']:+.3f}R  TotalR={m['total_r']:+.1f}")

    # ── Top symbols ──
    print(f"\n{'=' * 100}")
    print("TOP SYMBOLS")
    print(f"{'=' * 100}")
    for label, trades in [("ORH_FBO_SHORT", orh_trades), ("ORL_FBD_LONG", orl_trades)]:
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
    if orh_trades:
        _export_csv(orh_trades, OUT_DIR / "replay_orh_fbo_short.csv", "orh_fbo_short")
    if orl_trades:
        _export_csv(orl_trades, OUT_DIR / "replay_orl_fbd_long.csv", "orl_fbd_long")

    # ── Final summary ──
    print(f"\n{'=' * 100}")
    print("FINAL SUMMARY")
    print(f"{'=' * 100}")
    print(f"  ORH_FBO_SHORT: N={orh_m['n']}, PF={pf_str(orh_m['pf'])}, "
          f"Exp={orh_m['exp']:+.3f}R, TotalR={orh_m['total_r']:+.1f}")
    print(f"  ORL_FBD_LONG:  N={orl_m['n']}, PF={pf_str(orl_m['pf'])}, "
          f"Exp={orl_m['exp']:+.3f}R, TotalR={orl_m['total_r']:+.1f}")
    print(f"  COMBINED:      N={all_m['n']}, PF={pf_str(all_m['pf'])}, "
          f"Exp={all_m['exp']:+.3f}R, TotalR={all_m['total_r']:+.1f}")

    print(f"\n{'=' * 100}")
    print("DONE.")
    print(f"{'=' * 100}")


if __name__ == "__main__":
    main()
