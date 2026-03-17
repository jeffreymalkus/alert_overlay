"""
Second Chance Feature Study — diagnostic framework.

For every existing SECOND_CHANCE trade, computes SC_V2-style measurements:
  - impulse size, reset quality, trigger bar quality, extension, timing
  - pass/fail flags for each proposed SC_V2 filter

Reports:
  1. Pass rate of each filter across all SC trades
  2. Expectancy / WR / PF for trades that pass each filter
  3. Expectancy / WR / PF for trades that fail each filter
  4. Which filters improve quality with least loss of trade count

Usage:
    python -m alert_overlay.sc_feature_study
    python -m alert_overlay.sc_feature_study --universe all94
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from dataclasses import dataclass, field

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import (
    NaN, SetupId, SetupFamily, SETUP_DISPLAY_NAME, Bar,
)

DATA_DIR = Path(__file__).parent.parent / "data"
WATCHLIST_FILE = Path(__file__).parent.parent / "watchlist.txt"

_isnan = lambda v: v != v


# ── Feature measurements per SC trade ──

@dataclass
class SCFeatures:
    """SC_V2-style measurements attached to a SECOND_CHANCE trade."""
    symbol: str = ""
    trade: Trade = None

    # Impulse / breakout metrics
    impulse_atr: float = NaN          # breakout bar range in ATR units
    bo_vol_vs_volma: float = NaN      # breakout volume / vol_ma
    bo_close_pct: float = NaN         # breakout close position in bar

    # Reset (retest) metrics
    reset_depth_pct: float = NaN      # how deep retest went vs impulse
    reset_bars: int = 0               # bars from breakout to retest
    reset_vol_vs_bo: float = NaN      # retest bar volume / breakout volume
    retest_holds_cleanly: bool = False # closed back above level

    # Trigger (confirm) bar metrics
    trigger_close_pct: float = NaN    # close position in bar range
    trigger_body_pct: float = NaN     # body as % of range
    trigger_vol_vs_retest: float = NaN  # confirm vol / retest vol (proxy)
    trigger_vol_vs_volma: float = NaN   # confirm vol / vol_ma

    # Extension
    dist_from_vwap_atr: float = NaN   # distance from VWAP at trigger in ATR
    dist_from_ema9_atr: float = NaN   # distance from EMA9 at trigger in ATR
    dist_from_open_atr: float = NaN   # total extension from session open

    # Timing
    bars_since_breakout: int = 0      # total bars from breakout to confirm
    level_tag: str = ""               # "ORH", "ORL", "SWING"

    # Context
    rs_market: float = NaN
    rs_sector: float = NaN
    htf_aligned: bool = False

    # Pass/fail flags for each proposed filter
    f_reset_shallow: bool = False     # reset depth <= 50% of impulse
    f_reset_vol_light: bool = False   # reset vol < 75% of breakout vol
    f_trigger_strong_close: bool = False  # trigger close in upper 60%
    f_trigger_strong_body: bool = False   # trigger body >= 50% of range
    f_trigger_vol_expansion: bool = False # trigger vol > 1.3× retest vol
    f_not_extended_vwap: bool = False  # within 2.0 ATR of VWAP
    f_not_extended_ema9: bool = False  # within 1.5 ATR of EMA9
    f_timely: bool = False            # <= 12 bars since breakout
    f_or_level: bool = False          # broke OR level (not swing)
    f_strong_bo_vol: bool = False     # breakout vol >= 1.25 × vol_ma


def compute_sc_features(sym, trade, bars, cfg):
    """Compute SC_V2-style features for one SECOND_CHANCE trade."""
    f = SCFeatures(symbol=sym, trade=trade)
    sig = trade.signal

    # Find the trigger bar and surrounding bars
    trigger_idx = sig.bar_index
    trigger_bar = None
    for b in bars:
        if hasattr(b, 'bar_index') and b.bar_index == trigger_idx:
            trigger_bar = b
            break

    # Fallback: find bar by timestamp
    if trigger_bar is None:
        for b in bars:
            if b.timestamp == sig.timestamp:
                trigger_bar = b
                break

    if trigger_bar is None:
        return f

    is_bull = sig.direction == 1
    bar = trigger_bar
    bar_range = bar.high - bar.low

    # ── Compute intra ATR at trigger time ──
    # Use a simple trailing 14-bar ATR approximation
    bar_list = list(bars)
    bar_idx_in_list = None
    for idx, b in enumerate(bar_list):
        if b.timestamp == bar.timestamp:
            bar_idx_in_list = idx
            break

    if bar_idx_in_list is None or bar_idx_in_list < 20:
        return f

    # ATR from prior 14 bars
    recent = bar_list[max(0, bar_idx_in_list - 14):bar_idx_in_list]
    ranges = [b.high - b.low for b in recent if b.high - b.low > 0]
    i_atr = sum(ranges) / len(ranges) if ranges else 1.0

    # vol_ma from prior 20 bars
    vol_recent = bar_list[max(0, bar_idx_in_list - 20):bar_idx_in_list]
    vols = [b.volume for b in vol_recent if b.volume > 0]
    vol_ma = sum(vols) / len(vols) if vols else 1.0

    # ── Find breakout bar (look backward for the state transition) ──
    # The breakout bar is typically 2-9 bars before the trigger
    # (breakout → retest within 6 bars → confirm within 3 bars)
    # We identify it by looking for a strong directional bar that broke a level

    # Use a heuristic: scan backwards from trigger for the breakout bar
    # It should be the furthest-back bar within the SC window that:
    # - has strong directional close
    # - has volume above vol_ma
    lookback = min(12, bar_idx_in_list)
    candidate_bars = bar_list[bar_idx_in_list - lookback:bar_idx_in_list]

    bo_bar = None
    retest_bar = None

    # Strategy: the breakout is the first strong directional bar in the window
    # The retest is the bar(s) that pulled back closest to the level
    # Since we don't have the exact level stored on the signal, we reconstruct:
    # For longs: breakout bar is the one with highest close relative to prior bars
    #            retest bar is the lowest-low bar after breakout

    if is_bull:
        # Find breakout: strongest bullish bar with good volume
        for i, cb in enumerate(candidate_bars):
            cb_range = cb.high - cb.low
            if (cb.close > cb.open and cb.volume >= vol_ma * 0.9 and
                    cb_range > 0):
                close_pct = (cb.close - cb.low) / cb_range
                if close_pct >= 0.5:
                    bo_bar = cb
                    bo_bar_local_idx = i
                    break
        # Find retest: lowest low after breakout, before trigger
        if bo_bar is not None:
            post_bo = candidate_bars[bo_bar_local_idx + 1:]
            if post_bo:
                retest_bar = min(post_bo, key=lambda b: b.low)
    else:
        # For shorts: strongest bearish bar
        for i, cb in enumerate(candidate_bars):
            cb_range = cb.high - cb.low
            if (cb.close < cb.open and cb.volume >= vol_ma * 0.9 and
                    cb_range > 0):
                close_pct = (cb.high - cb.close) / cb_range
                if close_pct >= 0.5:
                    bo_bar = cb
                    bo_bar_local_idx = i
                    break
        if bo_bar is not None:
            post_bo = candidate_bars[bo_bar_local_idx + 1:]
            if post_bo:
                retest_bar = max(post_bo, key=lambda b: b.high)

    if bo_bar is None:
        return f

    # ── Impulse metrics ──
    bo_range = bo_bar.high - bo_bar.low
    f.impulse_atr = bo_range / i_atr if i_atr > 0 else NaN
    f.bo_vol_vs_volma = bo_bar.volume / vol_ma if vol_ma > 0 else NaN
    if bo_range > 0:
        if is_bull:
            f.bo_close_pct = (bo_bar.close - bo_bar.low) / bo_range
        else:
            f.bo_close_pct = (bo_bar.high - bo_bar.close) / bo_range

    # ── Reset (retest) metrics ──
    if retest_bar is not None:
        if is_bull:
            impulse_size = bo_bar.high - bo_bar.low  # simplified
            reset_depth = bo_bar.high - retest_bar.low
            f.reset_depth_pct = reset_depth / impulse_size if impulse_size > 0 else NaN
        else:
            impulse_size = bo_bar.high - bo_bar.low
            reset_depth = retest_bar.high - bo_bar.low
            f.reset_depth_pct = reset_depth / impulse_size if impulse_size > 0 else NaN

        # Find bars between breakout and retest
        bo_ts = bo_bar.timestamp
        retest_ts = retest_bar.timestamp
        f.reset_bars = sum(1 for b in candidate_bars
                           if b.timestamp > bo_ts and b.timestamp <= retest_ts)

        f.reset_vol_vs_bo = (retest_bar.volume / bo_bar.volume
                              if bo_bar.volume > 0 else NaN)

        # Does retest hold cleanly?
        if is_bull:
            f.retest_holds_cleanly = retest_bar.close > retest_bar.open
        else:
            f.retest_holds_cleanly = retest_bar.close < retest_bar.open

    # ── Trigger bar metrics ──
    if bar_range > 0:
        if is_bull:
            f.trigger_close_pct = (bar.close - bar.low) / bar_range
        else:
            f.trigger_close_pct = (bar.high - bar.close) / bar_range
        body = abs(bar.close - bar.open)
        f.trigger_body_pct = body / bar_range
    f.trigger_vol_vs_volma = bar.volume / vol_ma if vol_ma > 0 else NaN
    if retest_bar is not None and retest_bar.volume > 0:
        f.trigger_vol_vs_retest = bar.volume / retest_bar.volume

    # ── Extension ──
    # Estimate VWAP and EMA9 from recent bars
    # Simple VWAP proxy: volume-weighted avg price of today's bars
    day_date = bar.timestamp.date() if hasattr(bar.timestamp, 'date') else None
    if day_date:
        day_bars = [b for b in bar_list[:bar_idx_in_list + 1]
                    if hasattr(b.timestamp, 'date') and b.timestamp.date() == day_date]
        if day_bars:
            total_vp = sum(b.close * b.volume for b in day_bars)
            total_v = sum(b.volume for b in day_bars)
            vwap_est = total_vp / total_v if total_v > 0 else bar.close
            f.dist_from_vwap_atr = abs(bar.close - vwap_est) / i_atr if i_atr > 0 else NaN

            # Session open
            session_open = day_bars[0].open
            f.dist_from_open_atr = abs(bar.close - session_open) / i_atr if i_atr > 0 else NaN

    # EMA9 proxy: simple 9-bar MA of closes
    if bar_idx_in_list >= 9:
        ema9_bars = bar_list[bar_idx_in_list - 8:bar_idx_in_list + 1]
        ema9_est = sum(b.close for b in ema9_bars) / 9
        f.dist_from_ema9_atr = abs(bar.close - ema9_est) / i_atr if i_atr > 0 else NaN

    # ── Timing ──
    bo_idx = None
    for idx, b in enumerate(bar_list):
        if b.timestamp == bo_bar.timestamp:
            bo_idx = idx
            break
    if bo_idx is not None:
        f.bars_since_breakout = bar_idx_in_list - bo_idx

    # ── Context from signal ──
    f.rs_market = sig.rs_market if not _isnan(sig.rs_market) else NaN
    f.rs_sector = sig.rs_sector if not _isnan(sig.rs_sector) else NaN
    f.level_tag = ""  # not directly available on signal

    # ── Compute pass/fail flags ──
    if not _isnan(f.reset_depth_pct):
        f.f_reset_shallow = f.reset_depth_pct <= 0.50
    if not _isnan(f.reset_vol_vs_bo):
        f.f_reset_vol_light = f.reset_vol_vs_bo < 0.75
    if not _isnan(f.trigger_close_pct):
        f.f_trigger_strong_close = f.trigger_close_pct >= 0.60
    if not _isnan(f.trigger_body_pct):
        f.f_trigger_strong_body = f.trigger_body_pct >= 0.50
    if not _isnan(f.trigger_vol_vs_retest):
        f.f_trigger_vol_expansion = f.trigger_vol_vs_retest >= 1.3
    if not _isnan(f.dist_from_vwap_atr):
        f.f_not_extended_vwap = f.dist_from_vwap_atr <= 2.0
    if not _isnan(f.dist_from_ema9_atr):
        f.f_not_extended_ema9 = f.dist_from_ema9_atr <= 1.5
    f.f_timely = f.bars_since_breakout <= 12
    if not _isnan(f.bo_vol_vs_volma):
        f.f_strong_bo_vol = f.bo_vol_vs_volma >= 1.25

    return f


# ── Stats helpers ──

def stats(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "exp": 0.0}
    wins = [t for t in trades if t.pnl_points > 0]
    losses = [t for t in trades if t.pnl_points <= 0]
    gw = sum(t.pnl_points for t in wins)
    gl = abs(sum(t.pnl_points for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    pnl = sum(t.pnl_points for t in trades)
    exp = sum(t.pnl_rr for t in trades) / n
    return {"n": n, "wr": len(wins) / n * 100, "pf": pf, "pnl": pnl, "exp": exp}


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "∞"


# ── Main ──

def main():
    parser = argparse.ArgumentParser(description="SC Feature Study")
    parser.add_argument("--universe", type=str, default="all94",
                        choices=["orig35", "all94"])
    args = parser.parse_args()

    # Load symbols
    if args.universe == "all94":
        symbols = sorted(set(
            p.stem.replace("_5min", "")
            for p in DATA_DIR.glob("*_5min.csv")
        ))
    else:
        with open(WATCHLIST_FILE) as wf:
            symbols = [l.strip().upper() for l in wf if l.strip() and not l.startswith("#")]

    # Load SPY/QQQ/sector bars
    spy_bars = qqq_bars = None
    sector_bars_dict = {}
    spy_path = DATA_DIR / "SPY_5min.csv"
    qqq_path = DATA_DIR / "QQQ_5min.csv"
    if spy_path.exists():
        spy_bars = load_bars_from_csv(str(spy_path))
    if qqq_path.exists():
        qqq_bars = load_bars_from_csv(str(qqq_path))

    from .market_context import SECTOR_MAP, get_sector_etf
    sector_etfs = set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    # Config: core (VK + SC) — the active baseline
    cfg = OverlayConfig()
    cfg.show_ema_scalp = False
    cfg.show_failed_bounce = False
    cfg.show_spencer = False
    cfg.show_ema_fpip = False
    cfg.show_sc_v2 = False

    # Collect all SC trades with their bars
    all_features = []
    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue

        sec_bars = None
        if sector_bars_dict and cfg.use_sector_context:
            sec_etf = get_sector_etf(sym)
            if sec_etf and sec_etf in sector_bars_dict:
                sec_bars = sector_bars_dict[sec_etf]

        result = run_backtest(bars, cfg=cfg,
                              spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)

        for t in result.trades:
            if t.signal.setup_id == SetupId.SECOND_CHANCE:
                feat = compute_sc_features(sym, t, bars, cfg)
                all_features.append(feat)

    if not all_features:
        print("No SECOND_CHANCE trades found.")
        sys.exit(0)

    trades = [f.trade for f in all_features]
    total = stats(trades)

    print(f"\n{'=' * 100}")
    print(f"  SECOND CHANCE FEATURE STUDY — {len(all_features)} trades [{args.universe}]")
    print(f"{'=' * 100}")
    print(f"  Baseline: N={total['n']}, WR={total['wr']:.1f}%, "
          f"PF={pf_str(total['pf'])}, PnL={total['pnl']:+.2f}, "
          f"Exp={total['exp']:+.3f}R")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 1: Raw feature distributions
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'─' * 100}")
    print("  SECTION 1: Feature Distributions")
    print(f"{'─' * 100}")

    def describe(label, values):
        valid = [v for v in values if not _isnan(v)]
        if not valid:
            print(f"  {label:<30} │ no data")
            return
        avg = sum(valid) / len(valid)
        srt = sorted(valid)
        med = srt[len(srt) // 2]
        mn = srt[0]
        mx = srt[-1]
        p25 = srt[len(srt) // 4]
        p75 = srt[3 * len(srt) // 4]
        print(f"  {label:<30} │ avg={avg:>6.2f}  med={med:>6.2f}  "
              f"[{mn:>6.2f} – {mx:>6.2f}]  p25={p25:>6.2f}  p75={p75:>6.2f}  "
              f"N={len(valid)}")

    describe("Impulse size (ATR)", [f.impulse_atr for f in all_features])
    describe("BO vol / vol_ma", [f.bo_vol_vs_volma for f in all_features])
    describe("BO close position", [f.bo_close_pct for f in all_features])
    describe("Reset depth %", [f.reset_depth_pct for f in all_features])
    describe("Reset bars", [float(f.reset_bars) for f in all_features])
    describe("Reset vol / BO vol", [f.reset_vol_vs_bo for f in all_features])
    describe("Trigger close position", [f.trigger_close_pct for f in all_features])
    describe("Trigger body %", [f.trigger_body_pct for f in all_features])
    describe("Trigger vol / retest vol", [f.trigger_vol_vs_retest for f in all_features])
    describe("Trigger vol / vol_ma", [f.trigger_vol_vs_volma for f in all_features])
    describe("Dist from VWAP (ATR)", [f.dist_from_vwap_atr for f in all_features])
    describe("Dist from EMA9 (ATR)", [f.dist_from_ema9_atr for f in all_features])
    describe("Dist from open (ATR)", [f.dist_from_open_atr for f in all_features])
    describe("Bars since breakout", [float(f.bars_since_breakout) for f in all_features])

    # ══════════════════════════════════════════════════════════════
    #  SECTION 2: Per-filter pass rate + outcome analysis
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'─' * 100}")
    print("  SECTION 2: Per-Filter Analysis (Pass vs Fail)")
    print(f"{'─' * 100}")

    filters = [
        ("Reset shallow (≤50%)",        lambda f: f.f_reset_shallow),
        ("Reset vol light (<75% BO)",   lambda f: f.f_reset_vol_light),
        ("Trigger close strong (≥60%)", lambda f: f.f_trigger_strong_close),
        ("Trigger body strong (≥50%)",  lambda f: f.f_trigger_strong_body),
        ("Trigger vol expansion (≥1.3×)", lambda f: f.f_trigger_vol_expansion),
        ("Not extended VWAP (≤2 ATR)",  lambda f: f.f_not_extended_vwap),
        ("Not extended EMA9 (≤1.5 ATR)", lambda f: f.f_not_extended_ema9),
        ("Timely (≤12 bars)",           lambda f: f.f_timely),
        ("Strong BO vol (≥1.25× vol_ma)", lambda f: f.f_strong_bo_vol),
    ]

    print(f"\n  {'Filter':<35} │ {'── PASS ──':^30} │ {'── FAIL ──':^30} │ {'Δ Exp':>7} {'Keep%':>6}")
    print(f"  {'':35} │ {'N':>4} {'WR%':>6} {'PF':>5} {'Exp':>7} │ "
          f"{'N':>4} {'WR%':>6} {'PF':>5} {'Exp':>7} │")
    print(f"  {'─' * 35}─┼{'─' * 30}─┼{'─' * 30}─┼{'─' * 14}")

    filter_results = []

    for label, fn in filters:
        pass_feats = [f for f in all_features if fn(f)]
        fail_feats = [f for f in all_features if not fn(f)]

        pass_trades = [f.trade for f in pass_feats]
        fail_trades = [f.trade for f in fail_feats]

        ps = stats(pass_trades)
        fs = stats(fail_trades)

        keep_pct = len(pass_feats) / len(all_features) * 100 if all_features else 0
        delta_exp = ps["exp"] - total["exp"] if ps["n"] > 0 else 0

        print(f"  {label:<35} │ "
              f"{ps['n']:>4} {ps['wr']:>5.1f}% {pf_str(ps['pf']):>5} {ps['exp']:>+6.3f} │ "
              f"{fs['n']:>4} {fs['wr']:>5.1f}% {pf_str(fs['pf']):>5} {fs['exp']:>+6.3f} │ "
              f"{delta_exp:>+6.3f} {keep_pct:>5.0f}%")

        filter_results.append((label, ps, fs, delta_exp, keep_pct))

    # ══════════════════════════════════════════════════════════════
    #  SECTION 3: Combined filter analysis (top combinations)
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'─' * 100}")
    print("  SECTION 3: Combined Filter Analysis (top 2-3 filter combos)")
    print(f"{'─' * 100}")

    # Sort filters by delta_exp (descending), pick top ones that keep >= 30% trades
    ranked = sorted(filter_results, key=lambda x: x[3], reverse=True)
    useful = [r for r in ranked if r[4] >= 25]  # keep >= 25% of trades

    print(f"\n  Filters ranked by Exp improvement (keeping ≥25% of trades):")
    for i, (label, ps, fs, delta_exp, keep_pct) in enumerate(useful):
        marker = " ← BEST" if i == 0 else (" ← 2nd" if i == 1 else "")
        print(f"    {i+1}. {label:<35} ΔExp={delta_exp:>+.3f}  Keep={keep_pct:.0f}%{marker}")

    if not useful:
        print("    No filters keep ≥25% of trades. Showing all:")
        for i, (label, ps, fs, delta_exp, keep_pct) in enumerate(ranked):
            print(f"    {i+1}. {label:<35} ΔExp={delta_exp:>+.3f}  Keep={keep_pct:.0f}%")

    # Test top 2 and top 3 combo
    if len(filters) >= 2:
        # Top-2 combo from ranked useful
        top_filters_fns = []
        for label, _, _, _, _ in useful[:3]:
            for fl, fn in filters:
                if fl == label:
                    top_filters_fns.append((fl, fn))
                    break

        if len(top_filters_fns) >= 2:
            for combo_size in [2, 3]:
                if len(top_filters_fns) < combo_size:
                    break
                combo_fns = top_filters_fns[:combo_size]
                combo_feats = [f for f in all_features
                               if all(fn(f) for _, fn in combo_fns)]
                combo_trades = [f.trade for f in combo_feats]
                cs = stats(combo_trades)
                keep = len(combo_feats) / len(all_features) * 100
                delta = cs["exp"] - total["exp"] if cs["n"] > 0 else 0
                names = " + ".join(l for l, _ in combo_fns)
                print(f"\n  Combo (top {combo_size}): {names}")
                print(f"    N={cs['n']}, WR={cs['wr']:.1f}%, PF={pf_str(cs['pf'])}, "
                      f"Exp={cs['exp']:+.3f}R, Keep={keep:.0f}%, ΔExp={delta:+.3f}")

    # ══════════════════════════════════════════════════════════════
    #  SECTION 4: Winners vs Losers feature comparison
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'─' * 100}")
    print("  SECTION 4: Winners vs Losers Feature Comparison")
    print(f"{'─' * 100}")

    winners = [f for f in all_features if f.trade.pnl_points > 0]
    losers = [f for f in all_features if f.trade.pnl_points <= 0]

    def compare(label, getter):
        w_vals = [v for v in [getter(f) for f in winners] if not _isnan(v)]
        l_vals = [v for v in [getter(f) for f in losers] if not _isnan(v)]
        w_avg = sum(w_vals) / len(w_vals) if w_vals else NaN
        l_avg = sum(l_vals) / len(l_vals) if l_vals else NaN
        diff = w_avg - l_avg if not _isnan(w_avg) and not _isnan(l_avg) else NaN
        diff_str = f"{diff:>+6.2f}" if not _isnan(diff) else "   N/A"
        w_str = f"{w_avg:>6.2f}" if not _isnan(w_avg) else "   N/A"
        l_str = f"{l_avg:>6.2f}" if not _isnan(l_avg) else "   N/A"
        print(f"  {label:<30} │ Win avg={w_str}  Loss avg={l_str}  Δ={diff_str}")

    print(f"\n  N winners={len(winners)}, N losers={len(losers)}")
    compare("Impulse size (ATR)", lambda f: f.impulse_atr)
    compare("BO vol / vol_ma", lambda f: f.bo_vol_vs_volma)
    compare("Reset depth %", lambda f: f.reset_depth_pct)
    compare("Reset vol / BO vol", lambda f: f.reset_vol_vs_bo)
    compare("Trigger close pct", lambda f: f.trigger_close_pct)
    compare("Trigger body pct", lambda f: f.trigger_body_pct)
    compare("Trigger vol / retest", lambda f: f.trigger_vol_vs_retest)
    compare("Dist from VWAP (ATR)", lambda f: f.dist_from_vwap_atr)
    compare("Dist from EMA9 (ATR)", lambda f: f.dist_from_ema9_atr)
    compare("Bars since breakout", lambda f: float(f.bars_since_breakout))

    # ══════════════════════════════════════════════════════════════
    #  SECTION 5: Per-trade detail log
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'─' * 100}")
    print("  SECTION 5: Trade Detail Log")
    print(f"{'─' * 100}")

    print(f"\n  {'Sym':<6} {'Date':>10} {'Dir':>5} {'PnL':>7} {'RR':>6} │ "
          f"{'ImpATR':>6} {'RstD%':>6} {'RstVR':>6} {'TrgCl':>6} {'TrgBd':>6} "
          f"{'VWAPd':>6} {'EMA9d':>6} {'Bars':>4} │ "
          f"{'Shlw':>4} {'LtVl':>4} {'StCl':>4} {'StBd':>4} {'VExp':>4} {'VWok':>4} {'E9ok':>4} {'Time':>4}")
    print(f"  {'─' * 6}─{'─' * 10}─{'─' * 5}─{'─' * 7}─{'─' * 6}─┼─"
          f"{'─' * 6}─{'─' * 6}─{'─' * 6}─{'─' * 6}─{'─' * 6}─"
          f"{'─' * 6}─{'─' * 6}─{'─' * 4}─┼─"
          f"{'─' * 4}─{'─' * 4}─{'─' * 4}─{'─' * 4}─{'─' * 4}─{'─' * 4}─{'─' * 4}─{'─' * 4}")

    for f in sorted(all_features, key=lambda x: str(x.trade.signal.timestamp)):
        t = f.trade
        sig = t.signal
        dt = str(sig.timestamp)[:10]
        d = "LONG" if sig.direction == 1 else "SHORT"
        pnl = t.pnl_points

        def fv(v, fmt=".2f"):
            return f"{v:{fmt}}" if not _isnan(v) else "  N/A"

        def fb(v):
            return "  Y" if v else "  n"

        print(f"  {f.symbol:<6} {dt:>10} {d:>5} {pnl:>+6.2f} {t.pnl_rr:>+5.2f} │ "
              f"{fv(f.impulse_atr):>6} {fv(f.reset_depth_pct):>6} {fv(f.reset_vol_vs_bo):>6} "
              f"{fv(f.trigger_close_pct):>6} {fv(f.trigger_body_pct):>6} "
              f"{fv(f.dist_from_vwap_atr):>6} {fv(f.dist_from_ema9_atr):>6} "
              f"{f.bars_since_breakout:>4} │ "
              f"{fb(f.f_reset_shallow):>4} {fb(f.f_reset_vol_light):>4} "
              f"{fb(f.f_trigger_strong_close):>4} {fb(f.f_trigger_strong_body):>4} "
              f"{fb(f.f_trigger_vol_expansion):>4} {fb(f.f_not_extended_vwap):>4} "
              f"{fb(f.f_not_extended_ema9):>4} {fb(f.f_timely):>4}")

    print(f"\n{'=' * 100}")
    print("  RECOMMENDATION: Promote only the top 2-3 filters that improve")
    print("  expectancy meaningfully while keeping ≥50% of trade count.")
    print(f"{'=' * 100}\n")


if __name__ == "__main__":
    main()
