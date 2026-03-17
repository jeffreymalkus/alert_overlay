"""
Replay evaluation for v2 hybrid strategies: ORH_FBO_SHORT_V2 + PDH_FBO_SHORT + ORL_FBD_LONG_V2.
Uses both 5-min and 1-min data for hybrid timeframe strategies.

Run from: cd /sessions/inspiring-clever-meitner/mnt && python -m alert_overlay.strategies.replay_v2_strats
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

from .replay import _find_bar_idx, _print_detail, _print_pipeline, _export_csv, compute_metrics, pf_str

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_1M_DIR = DATA_DIR / "1min"
OUT_DIR = Path(__file__).parent.parent / "outputs"


def main():
    print("=" * 100)
    print("STRATEGY REPLAY v2 — Hybrid Timeframe Failed-Move Strategies")
    print("=" * 100)

    # ── Load 5-min data ──
    print("\n  Loading 5-min data...")
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))

    sector_etfs = sorted(set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    sector_bars_dict = {}
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    excluded = {"SPY", "QQQ", "IWM"} | set(sector_etfs)

    # ── Find symbols with BOTH 5-min and 1-min data ──
    symbols_5m = set(
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    )
    symbols_1m = set(
        p.stem.replace("_1min", "")
        for p in DATA_1M_DIR.glob("*_1min.csv")
        if p.stem.replace("_1min", "") not in excluded
    )
    symbols = sorted(symbols_5m & symbols_1m)
    print(f"  Symbols with both 5m+1m data: {len(symbols)}")

    # ── Date range: only days in 1-min data ──
    # Load a sample to get date range
    sample_1m = load_bars_from_csv(str(DATA_1M_DIR / f"{symbols[0]}_1min.csv"))
    dates_1m = sorted(set(b.timestamp.date() for b in sample_1m))
    print(f"  1-min date range: {dates_1m[0]} → {dates_1m[-1]} ({len(dates_1m)} days)")

    spy_dates = sorted(set(b.timestamp.date() for b in spy_bars))
    # Restrict to dates where 1m data exists
    valid_dates = set(dates_1m) & set(spy_dates)
    print(f"  Valid overlapping dates: {len(valid_dates)}")

    # ── Initialize framework ──
    cfg = StrategyConfig(timeframe_min=5)
    in_play = InPlayProxy(cfg)
    regime = EnhancedMarketRegime(spy_bars, cfg)
    rejection = RejectionFilters(cfg)
    quality_scorer = QualityScorer(cfg)

    regime.precompute()
    regime_dist = regime.day_label_distribution()
    print(f"  Regime: {regime_dist}")

    # ── Import strategies ──
    from .orh_fbo_short_v2 import ORHFailedBOShortV2Strategy
    orh_v2 = ORHFailedBOShortV2Strategy(cfg, in_play, regime, rejection, quality_scorer)

    from .pdh_fbo_short import PDHFailedBOShortStrategy
    pdh_v1 = PDHFailedBOShortStrategy(cfg, in_play, regime, rejection, quality_scorer)

    orl_v2_strategy = None
    orl_v2_trades: List[StrategyTrade] = []
    try:
        from .orl_fbd_long_v2 import ORLFailedBDLongV2Strategy
        orl_v2 = ORLFailedBDLongV2Strategy(cfg, in_play, regime, rejection, quality_scorer)
        orl_v2_strategy = orl_v2
    except ImportError:
        print("  [ORL_FBD_LONG_V2 not yet built — skipping]")

    orh_trades_a: List[StrategyTrade] = []
    orh_trades_b: List[StrategyTrade] = []
    orh_trades_all: List[StrategyTrade] = []

    pdh_trades_a: List[StrategyTrade] = []
    pdh_trades_b: List[StrategyTrade] = []
    pdh_trades_all: List[StrategyTrade] = []

    print(f"\n  Processing {len(symbols)} symbols...")
    for idx, sym in enumerate(symbols):
        p_5m = DATA_DIR / f"{sym}_5min.csv"
        p_1m = DATA_1M_DIR / f"{sym}_1min.csv"
        if not p_5m.exists() or not p_1m.exists():
            continue
        bars_5m = load_bars_from_csv(str(p_5m))
        bars_1m = load_bars_from_csv(str(p_1m))
        if not bars_5m or not bars_1m:
            continue

        # Precompute in-play for this symbol (uses 5-min data)
        in_play.precompute(sym, bars_5m)

        # Get trading days from 1-min data
        sym_dates = sorted(set(b.timestamp.date() for b in bars_1m) & valid_dates)

        for day in sym_dates:
            # ── ORH_FBO_SHORT v2 ──
            orh_sigs = orh_v2.scan_day(sym, bars_5m, bars_1m, day, spy_bars)
            for sig in orh_sigs:
                if sig.is_tradeable:
                    # Find bar index in 1-min data for simulation
                    bar_idx = _find_bar_idx(bars_1m, sig.timestamp)
                    if bar_idx >= 0:
                        actual_rr = sig.metadata.get("actual_rr", cfg.orh2_target_rr)
                        trade = simulate_strategy_trade(
                            sig, bars_1m, bar_idx,
                            max_bars=cfg.orh2_max_bars,
                            target_rr=actual_rr,
                        )
                        orh_trades_all.append(trade)
                        if sig.metadata.get("mode") == "A":
                            orh_trades_a.append(trade)
                        else:
                            orh_trades_b.append(trade)

            # ── PDH_FBO_SHORT v1 ──
            pdh_sigs = pdh_v1.scan_day(sym, bars_5m, bars_1m, day, spy_bars)
            for sig in pdh_sigs:
                if sig.is_tradeable:
                    bar_idx = _find_bar_idx(bars_1m, sig.timestamp)
                    if bar_idx >= 0:
                        actual_rr = sig.metadata.get("actual_rr", cfg.pdh_target_rr)
                        trade = simulate_strategy_trade(
                            sig, bars_1m, bar_idx,
                            max_bars=cfg.pdh_max_bars,
                            target_rr=actual_rr,
                        )
                        pdh_trades_all.append(trade)
                        if sig.metadata.get("mode") == "A":
                            pdh_trades_a.append(trade)
                        else:
                            pdh_trades_b.append(trade)

            # ── ORL_FBD_LONG v2 (if available) ──
            if orl_v2_strategy:
                orl_sigs = orl_v2_strategy.scan_day(sym, bars_5m, bars_1m, day, spy_bars)
                for sig in orl_sigs:
                    if sig.is_tradeable:
                        bar_idx = _find_bar_idx(bars_1m, sig.timestamp)
                        if bar_idx >= 0:
                            actual_rr = sig.metadata.get("actual_rr", cfg.orl2_target_rr)
                            trade = simulate_strategy_trade(
                                sig, bars_1m, bar_idx,
                                max_bars=cfg.orl2_max_bars,
                                target_rr=actual_rr,
                            )
                            orl_v2_trades.append(trade)

        if (idx + 1) % 20 == 0:
            print(f"    {idx + 1}/{len(symbols)} symbols done...")

    # ── Pipeline stats ──
    print(f"\n{'=' * 100}")
    print("PIPELINE VISIBILITY")
    print(f"{'=' * 100}")
    _print_pipeline("ORH_FBO_V2 (all)", orh_v2.stats)
    print(f"    Breakouts detected: {orh_v2.stats['breakouts_detected']}")
    print(f"    Failures detected:  {orh_v2.stats['failures_detected']}")
    print(f"    Raw Mode A: {orh_v2.stats['raw_mode_a']}  Raw Mode B: {orh_v2.stats['raw_mode_b']}")
    print(f"    Blocked — leader: {orh_v2.stats['blocked_leader']}  "
          f"hh_trend: {orh_v2.stats['blocked_hh_trend']}  "
          f"no_damage: {orh_v2.stats['blocked_no_damage']}  "
          f"regime: {orh_v2.stats['blocked_regime']}")
    _print_pipeline("PDH_FBO (all)", pdh_v1.stats)
    print(f"    Breakouts detected: {pdh_v1.stats['breakouts_detected']}")
    print(f"    Failures detected:  {pdh_v1.stats['failures_detected']}")
    print(f"    Raw Mode A: {pdh_v1.stats['raw_mode_a']}  Raw Mode B: {pdh_v1.stats['raw_mode_b']}")
    print(f"    Blocked — no_damage: {pdh_v1.stats['blocked_no_damage']}  "
          f"regime: {pdh_v1.stats['blocked_regime']}")
    print(f"    PDH not available: {pdh_v1.stats['pdh_not_available']}  "
          f"PDH == ORH: {pdh_v1.stats['pdh_equals_orh']}  "
          f"Confluence: {pdh_v1.stats['confluence_signals']}")
    if orl_v2_strategy:
        _print_pipeline("ORL_FBD_V2", orl_v2_strategy.stats)

    # ── Trade metrics — Mode A separate ──
    print(f"\n{'=' * 100}")
    print("TRADE METRICS — ORH_FBO_V2 MODE A (premium)")
    print(f"{'=' * 100}")
    orh_a_m = _print_detail("ORH_V2 Mode A", orh_trades_a)

    # ── Trade metrics — Mode B separate ──
    print(f"\n{'=' * 100}")
    print("TRADE METRICS — ORH_FBO_V2 MODE B (continuation)")
    print(f"{'=' * 100}")
    orh_b_m = _print_detail("ORH_V2 Mode B", orh_trades_b)

    # ── Trade metrics — Combined ──
    print(f"\n{'=' * 100}")
    print("TRADE METRICS — ORH_FBO_V2 COMBINED (A + B)")
    print(f"{'=' * 100}")
    orh_all_m = _print_detail("ORH_V2 Combined", orh_trades_all)

    # ── PDH Trade metrics — Mode A ──
    print(f"\n{'=' * 100}")
    print("TRADE METRICS — PDH_FBO MODE A (premium)")
    print(f"{'=' * 100}")
    pdh_a_m = _print_detail("PDH Mode A", pdh_trades_a)

    # ── PDH Trade metrics — Mode B ──
    print(f"\n{'=' * 100}")
    print("TRADE METRICS — PDH_FBO MODE B (continuation)")
    print(f"{'=' * 100}")
    pdh_b_m = _print_detail("PDH Mode B", pdh_trades_b)

    # ── PDH Trade metrics — Combined ──
    print(f"\n{'=' * 100}")
    print("TRADE METRICS — PDH_FBO COMBINED (A + B)")
    print(f"{'=' * 100}")
    pdh_all_m = _print_detail("PDH Combined", pdh_trades_all)

    # ── ORL v2 if available ──
    orl_v2_m = None
    if orl_v2_trades:
        print(f"\n{'=' * 100}")
        print("TRADE METRICS — ORL_FBD_V2")
        print(f"{'=' * 100}")
        orl_v2_m = _print_detail("ORL_V2", orl_v2_trades)

    # ── Sample signals ──
    print(f"\n{'=' * 100}")
    print("SAMPLE SIGNALS — ORH_V2 Mode A (first 20)")
    print(f"{'=' * 100}")
    for t in sorted(orh_trades_a, key=lambda x: x.signal.timestamp)[:20]:
        s = t.signal
        print(f"  {s.timestamp.strftime('%Y-%m-%d %H:%M')} {s.symbol:>6s} "
              f"entry={s.entry_price:.2f} stop={s.stop_price:.2f} tgt={s.target_price:.2f} "
              f"pnl={t.pnl_rr:+.2f}R exit={t.exit_reason:<6s} "
              f"regime={s.market_regime} trap={s.metadata.get('trap_depth_atr', 0):.2f} "
              f"tags={s.confluence_tags}")

    print(f"\n{'=' * 100}")
    print("SAMPLE SIGNALS — ORH_V2 Mode B (first 20)")
    print(f"{'=' * 100}")
    for t in sorted(orh_trades_b, key=lambda x: x.signal.timestamp)[:20]:
        s = t.signal
        print(f"  {s.timestamp.strftime('%Y-%m-%d %H:%M')} {s.symbol:>6s} "
              f"entry={s.entry_price:.2f} stop={s.stop_price:.2f} tgt={s.target_price:.2f} "
              f"pnl={t.pnl_rr:+.2f}R exit={t.exit_reason:<6s} "
              f"regime={s.market_regime} trap={s.metadata.get('trap_depth_atr', 0):.2f} "
              f"tags={s.confluence_tags}")

    # ── PDH Sample signals ──
    print(f"\n{'=' * 100}")
    print("SAMPLE SIGNALS — PDH Mode A (first 20)")
    print(f"{'=' * 100}")
    for t in sorted(pdh_trades_a, key=lambda x: x.signal.timestamp)[:20]:
        s = t.signal
        print(f"  {s.timestamp.strftime('%Y-%m-%d %H:%M')} {s.symbol:>6s} "
              f"entry={s.entry_price:.2f} stop={s.stop_price:.2f} tgt={s.target_price:.2f} "
              f"pnl={t.pnl_rr:+.2f}R exit={t.exit_reason:<6s} "
              f"regime={s.market_regime} trap={s.metadata.get('trap_depth_atr', 0):.2f} "
              f"confl={'Y' if s.metadata.get('has_confluence') else 'N'} "
              f"tags={s.confluence_tags}")

    print(f"\n{'=' * 100}")
    print("SAMPLE SIGNALS — PDH Mode B (first 20)")
    print(f"{'=' * 100}")
    for t in sorted(pdh_trades_b, key=lambda x: x.signal.timestamp)[:20]:
        s = t.signal
        print(f"  {s.timestamp.strftime('%Y-%m-%d %H:%M')} {s.symbol:>6s} "
              f"entry={s.entry_price:.2f} stop={s.stop_price:.2f} tgt={s.target_price:.2f} "
              f"pnl={t.pnl_rr:+.2f}R exit={t.exit_reason:<6s} "
              f"regime={s.market_regime} trap={s.metadata.get('trap_depth_atr', 0):.2f} "
              f"confl={'Y' if s.metadata.get('has_confluence') else 'N'} "
              f"tags={s.confluence_tags}")

    # ── Regime breakdown ──
    print(f"\n{'=' * 100}")
    print("REGIME BREAKDOWN")
    print(f"{'=' * 100}")
    for label, trades in [("ORH_V2 Mode A", orh_trades_a),
                           ("ORH_V2 Mode B", orh_trades_b),
                           ("ORH_V2 All", orh_trades_all),
                           ("PDH Mode A", pdh_trades_a),
                           ("PDH Mode B", pdh_trades_b),
                           ("PDH All", pdh_trades_all)]:
        regime_r = defaultdict(list)
        for t in trades:
            regime_r[t.signal.market_regime].append(t)
        print(f"\n  {label}:")
        for reg in sorted(regime_r.keys()):
            tr = regime_r[reg]
            m = compute_metrics(tr)
            print(f"    {reg:>5s}: N={m['n']:3d}  PF={pf_str(m['pf'])}  "
                  f"Exp={m['exp']:+.3f}R  TotalR={m['total_r']:+.1f}")

    # ── Export ──
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if orh_trades_all:
        _export_csv(orh_trades_all, OUT_DIR / "replay_orh_fbo_v2.csv", "orh_fbo_v2")
    if pdh_trades_all:
        _export_csv(pdh_trades_all, OUT_DIR / "replay_pdh_fbo.csv", "pdh_fbo")
    if orl_v2_trades:
        _export_csv(orl_v2_trades, OUT_DIR / "replay_orl_fbd_v2.csv", "orl_fbd_v2")

    # ── Overlap analysis: ORH vs PDH ──
    print(f"\n{'=' * 100}")
    print("OVERLAP ANALYSIS — ORH_V2 vs PDH_FBO")
    print(f"{'=' * 100}")
    orh_day_syms = set()
    for t in orh_trades_all:
        s = t.signal
        orh_day_syms.add((s.timestamp.date(), s.symbol))
    pdh_day_syms = set()
    for t in pdh_trades_all:
        s = t.signal
        pdh_day_syms.add((s.timestamp.date(), s.symbol))
    overlap = orh_day_syms & pdh_day_syms
    orh_only = orh_day_syms - pdh_day_syms
    pdh_only = pdh_day_syms - orh_day_syms
    print(f"  ORH trades: {len(orh_day_syms)} unique (day,sym)")
    print(f"  PDH trades: {len(pdh_day_syms)} unique (day,sym)")
    print(f"  Same day+sym overlap: {len(overlap)}")
    print(f"  ORH-only: {len(orh_only)}   PDH-only: {len(pdh_only)}")
    if overlap:
        print(f"  Overlapping (day,sym):")
        for ds in sorted(overlap)[:20]:
            print(f"    {ds[0]} {ds[1]}")

    # ── Confluence analysis ──
    confl_trades = [t for t in pdh_trades_all if t.signal.metadata.get("has_confluence")]
    non_confl_trades = [t for t in pdh_trades_all if not t.signal.metadata.get("has_confluence")]
    print(f"\n  PDH with ORH+PDH confluence: N={len(confl_trades)}")
    if confl_trades:
        cm = compute_metrics(confl_trades)
        print(f"    PF={pf_str(cm['pf'])}  Exp={cm['exp']:+.3f}R  TotalR={cm['total_r']:+.1f}")
    print(f"  PDH without confluence: N={len(non_confl_trades)}")
    if non_confl_trades:
        ncm = compute_metrics(non_confl_trades)
        print(f"    PF={pf_str(ncm['pf'])}  Exp={ncm['exp']:+.3f}R  TotalR={ncm['total_r']:+.1f}")

    # ── Combined family metrics ──
    family_all = orh_trades_all + pdh_trades_all
    if family_all:
        print(f"\n{'=' * 100}")
        print("FAILED-BREAKOUT-SHORT FAMILY COMBINED (ORH + PDH)")
        print(f"{'=' * 100}")
        fam_m = _print_detail("FBO Family", family_all)

    # ── Final summary ──
    print(f"\n{'=' * 100}")
    print("FINAL SUMMARY")
    print(f"{'=' * 100}")
    print(f"  ORH_V2 Mode A: N={orh_a_m['n']}, PF={pf_str(orh_a_m['pf'])}, "
          f"Exp={orh_a_m['exp']:+.3f}R, TotalR={orh_a_m['total_r']:+.1f}")
    print(f"  ORH_V2 Mode B: N={orh_b_m['n']}, PF={pf_str(orh_b_m['pf'])}, "
          f"Exp={orh_b_m['exp']:+.3f}R, TotalR={orh_b_m['total_r']:+.1f}")
    print(f"  ORH_V2 ALL:    N={orh_all_m['n']}, PF={pf_str(orh_all_m['pf'])}, "
          f"Exp={orh_all_m['exp']:+.3f}R, TotalR={orh_all_m['total_r']:+.1f}")
    print(f"  PDH Mode A:    N={pdh_a_m['n']}, PF={pf_str(pdh_a_m['pf'])}, "
          f"Exp={pdh_a_m['exp']:+.3f}R, TotalR={pdh_a_m['total_r']:+.1f}")
    print(f"  PDH Mode B:    N={pdh_b_m['n']}, PF={pf_str(pdh_b_m['pf'])}, "
          f"Exp={pdh_b_m['exp']:+.3f}R, TotalR={pdh_b_m['total_r']:+.1f}")
    print(f"  PDH ALL:       N={pdh_all_m['n']}, PF={pf_str(pdh_all_m['pf'])}, "
          f"Exp={pdh_all_m['exp']:+.3f}R, TotalR={pdh_all_m['total_r']:+.1f}")
    if family_all:
        print(f"  FAMILY TOTAL:  N={fam_m['n']}, PF={pf_str(fam_m['pf'])}, "
              f"Exp={fam_m['exp']:+.3f}R, TotalR={fam_m['total_r']:+.1f}")
    if orl_v2_m:
        print(f"  ORL_V2:        N={orl_v2_m['n']}, PF={pf_str(orl_v2_m['pf'])}, "
              f"Exp={orl_v2_m['exp']:+.3f}R, TotalR={orl_v2_m['total_r']:+.1f}")

    print(f"\n{'=' * 100}")
    print("DONE.")
    print(f"{'=' * 100}")


if __name__ == "__main__":
    main()
