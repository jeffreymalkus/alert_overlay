"""
Long Candidate Hunt — Systematic search for profitable long setups.

Strategy: run every long-capable setup in isolation, then explore
gate combinations (regime, quality, time filters, market_align)
to find configurations that survive IBKR friction.

The same approach that rescued EMA PULL short (time filter → PF 1.45 cost-on)
applied to ALL long candidates.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Callable, Tuple

from ..backtest import run_backtest, load_bars_from_csv, Trade
from ..config import OverlayConfig
from ..market_context import get_sector_etf, SECTOR_MAP
from ..models import NaN, SetupId, SETUP_DISPLAY_NAME

DATA_DIR = Path(__file__).parent.parent / "data"
_isnan = math.isnan


def load_bars(sym):
    p = DATA_DIR / f"{sym}_5min.csv"
    return load_bars_from_csv(str(p)) if p.exists() else []


def get_universe():
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    return sorted([p.stem.replace("_5min", "") for p in DATA_DIR.glob("*_5min.csv")
                   if p.stem.replace("_5min", "") not in excluded])


@dataclass
class EngTrade:
    symbol: str
    trade: Trade
    @property
    def pnl_rr(self): return self.trade.pnl_rr
    @property
    def setup_name(self): return self.trade.signal.setup_name
    @property
    def exit_reason(self): return self.trade.exit_reason
    @property
    def bars_held(self): return self.trade.bars_held
    @property
    def direction(self): return self.trade.signal.direction
    @property
    def entry_date(self): return self.trade.signal.timestamp.date()
    @property
    def hhmm(self): return self.trade.signal.timestamp.hour * 100 + self.trade.signal.timestamp.minute


def _disable_all(cfg):
    cfg.show_reversal_setups = False
    cfg.show_trend_setups = False
    cfg.show_ema_retest = False
    cfg.show_ema_mean_rev = False
    cfg.show_ema_pullback = False
    cfg.show_second_chance = False
    cfg.show_sc_v2 = False
    cfg.show_spencer = False
    cfg.show_ema_scalp = False
    cfg.show_ema_fpip = False
    cfg.show_ema_confirm = False
    cfg.show_mcs = False
    cfg.show_vwap_reclaim = False
    cfg.show_vka = False
    cfg.show_failed_bounce = False
    cfg.show_breakdown_retest = False
    return cfg


def run_setup(cfg, symbols, spy_bars, qqq_bars, setup_filter=None, direction_filter=None):
    trades = []
    for sym in symbols:
        bars = load_bars(sym)
        if not bars: continue
        sec_etf = get_sector_etf(sym)
        sec_bars = load_bars(sec_etf) if sec_etf and sec_etf not in {"SPY", "QQQ"} else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars, qqq_bars=qqq_bars, sector_bars=sec_bars)
        for t in result.trades:
            et = EngTrade(symbol=sym, trade=t)
            if setup_filter and et.setup_name != setup_filter:
                continue
            if direction_filter and et.direction != direction_filter:
                continue
            trades.append(et)
    return trades


def metrics(trades):
    if not trades:
        return {"n": 0, "pf_r": 0, "total_r": 0, "exp_r": 0, "win_rate": 0,
                "avg_win_r": 0, "avg_loss_r": 0, "max_dd_r": 0,
                "stop_rate": 0, "quick_stop_rate": 0}
    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    gross_w = sum(t.pnl_rr for t in wins)
    gross_l = abs(sum(t.pnl_rr for t in losses))
    total = sum(t.pnl_rr for t in trades)
    n = len(trades)
    cum = peak = max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.trade.signal.timestamp):
        cum += t.pnl_rr
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    stops = [t for t in trades if t.exit_reason == "stop"]
    quick = [t for t in trades if t.exit_reason == "stop" and t.bars_held <= 2]
    return {
        "n": n, "pf_r": gross_w / gross_l if gross_l > 0 else float('inf'),
        "total_r": total, "exp_r": total / n,
        "win_rate": len(wins) / n * 100,
        "avg_win_r": gross_w / len(wins) if wins else 0,
        "avg_loss_r": -gross_l / len(losses) if losses else 0,
        "max_dd_r": max_dd,
        "stop_rate": len(stops) / n * 100,
        "quick_stop_rate": len(quick) / n * 100,
    }


def pf(v): return "Inf" if v == float('inf') else f"{v:.2f}"


def split_train_test(trades):
    train = [t for t in trades if t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date.day % 2 == 0]
    return train, test


def pm(label, m, indent="    "):
    print(f"{indent}{label:<40s}  N={m['n']:>4d}  PF={pf(m['pf_r']):>6s}  "
          f"TotalR={m['total_r']:>+8.2f}  Exp={m['exp_r']:>+7.3f}  "
          f"WR={m['win_rate']:>5.1f}%  MaxDD={m['max_dd_r']:>7.2f}  "
          f"Stop={m['stop_rate']:>5.1f}%  QStop={m['quick_stop_rate']:>5.1f}%")


def time_filter(trades, max_hhmm):
    return [t for t in trades if t.hhmm < max_hhmm]


def time_range_filter(trades, min_hhmm, max_hhmm):
    return [t for t in trades if min_hhmm <= t.hhmm < max_hhmm]


# ════════════════════════════════════════════════════════════════
#  LONG SETUP CONFIGS
# ════════════════════════════════════════════════════════════════

def cfg_vwap_kiss_long():
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)
    cfg.show_trend_setups = True
    cfg.vk_long_only = True
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    return cfg


def cfg_sc_long():
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)
    cfg.show_second_chance = True
    cfg.sc_long_only = True
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    return cfg


def cfg_sc_v2_long():
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)
    cfg.show_sc_v2 = True
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    return cfg


def cfg_spencer_long():
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)
    cfg.show_spencer = True
    cfg.sp_long_only = False  # allow both directions for exploration
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    return cfg


def cfg_vr_long():
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)
    cfg.show_vwap_reclaim = True
    cfg.vr_long_only = True
    cfg.vr_day_filter = "none"
    cfg.vr_require_market_align = False
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    return cfg


def cfg_vka_long():
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)
    cfg.show_vka = True
    cfg.vka_long_only = True
    cfg.vka_day_filter = "none"
    cfg.vka_require_market_align = False
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    return cfg


def cfg_ema_scalp_long():
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)
    cfg.show_ema_scalp = True
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.ema_scalp_min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    return cfg


def cfg_ema_fpip_long():
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)
    cfg.show_ema_fpip = True
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.ema_fpip_min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    return cfg


def cfg_ep_long():
    """EMA PULL long (decoupled from show_trend_setups)."""
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)
    cfg.show_ema_pullback = True
    # Decoupled: show_trend_setups not needed
    cfg.ep_short_only = False
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    return cfg


def cfg_mcs_long():
    cfg = OverlayConfig()
    cfg = _disable_all(cfg)
    cfg.show_mcs = True
    cfg.show_trend_setups = True  # MCS still needs this parent gate
    cfg.mcs_long_only = True
    cfg.mcs_require_market_align = False
    cfg.require_regime = False
    cfg.min_quality = 0
    cfg.alert_cooldown_bars = 0
    cfg.min_stop_intra_atr_mult = 0.0
    return cfg


# ════════════════════════════════════════════════════════════════
#  ANALYSIS
# ════════════════════════════════════════════════════════════════

def analyze_setup(name, cfg_fn, symbols, spy_bars, qqq_bars, setup_filter=None):
    """Full analysis for one long setup: raw, cost-on, gate exploration, time filters."""
    print(f"\n{'━'*80}")
    print(f"  {name}")
    print(f"{'━'*80}")

    # Raw (no slippage)
    cfg_raw = cfg_fn()
    cfg_raw.use_dynamic_slippage = False
    cfg_raw.slippage_per_side = 0.0
    cfg_raw.commission_per_share = 0.0
    raw_trades = run_setup(cfg_raw, symbols, spy_bars, qqq_bars,
                           setup_filter=setup_filter, direction_filter=1)

    # Cost-on
    cfg_cost = cfg_fn()
    cost_trades = run_setup(cfg_cost, symbols, spy_bars, qqq_bars,
                            setup_filter=setup_filter, direction_filter=1)

    mr = metrics(raw_trades)
    mc = metrics(cost_trades)

    print(f"  Raw baseline:")
    pm("Raw (no slippage)", mr)
    pm("Cost-on (8bps RT)", mc)

    if mc['n'] == 0:
        print(f"  → NO TRADES with cost-on. Skipping.")
        return None

    friction = mr['total_r'] - mc['total_r']
    print(f"  Friction cost: {friction:+.2f}R ({friction/mr['n']:.3f}R/trade)" if mr['n'] > 0 else "")

    # Quick reject: if raw PF < 1.10, no point exploring gates
    if mr['n'] < 15:
        print(f"  → TOO FEW TRADES ({mr['n']}). Skipping gate exploration.")
        return {"name": name, "raw": mr, "cost": mc, "best_gate": None}

    if mr['pf_r'] < 1.10:
        print(f"  → RAW PF {pf(mr['pf_r'])} < 1.10. Not enough raw edge. Skipping.")
        return {"name": name, "raw": mr, "cost": mc, "best_gate": None}

    # ── Gate exploration on cost-on trades ──
    print(f"\n  Gate exploration (applied to cost-on trades):")

    best_label = "baseline"
    best_pf = mc['pf_r']
    best_trades = cost_trades
    best_m = mc

    # Quality filters
    for q in [3, 4, 5, 6]:
        filtered = [t for t in cost_trades if t.trade.signal.quality_score >= q]
        m = metrics(filtered)
        if m['n'] >= 10:
            pm(f"quality >= {q}", m)
            if m['pf_r'] > best_pf and m['total_r'] > 0:
                best_pf = m['pf_r']
                best_label = f"quality>={q}"
                best_trades = filtered
                best_m = m

    # Time filters
    for cutoff in [1200, 1300, 1400]:
        filtered = time_filter(cost_trades, cutoff)
        m = metrics(filtered)
        if m['n'] >= 10:
            pm(f"before {cutoff}", m)
            if m['pf_r'] > best_pf and m['total_r'] > 0:
                best_pf = m['pf_r']
                best_label = f"before_{cutoff}"
                best_trades = filtered
                best_m = m

    # AM-only (10:00–12:00)
    am = time_range_filter(cost_trades, 1000, 1200)
    m_am = metrics(am)
    if m_am['n'] >= 10:
        pm("AM only (10:00-12:00)", m_am)
        if m_am['pf_r'] > best_pf and m_am['total_r'] > 0:
            best_pf = m_am['pf_r']
            best_label = "AM_only"
            best_trades = am
            best_m = m_am

    # Quality + time combos
    for q in [4, 5]:
        for cutoff in [1200, 1300, 1400]:
            filtered = [t for t in cost_trades if t.trade.signal.quality_score >= q and t.hhmm < cutoff]
            m = metrics(filtered)
            if m['n'] >= 10:
                pm(f"q>={q} + before {cutoff}", m)
                if m['pf_r'] > best_pf and m['total_r'] > 0:
                    best_pf = m['pf_r']
                    best_label = f"q>={q}_before_{cutoff}"
                    best_trades = filtered
                    best_m = m

    # Regime filter (applied post-hoc to cost-on trades)
    # We can't easily filter by regime from trade objects, but we can check
    # which gate configs help at the engine level

    # ── Best config train/test ──
    if best_m['n'] >= 10 and best_m['total_r'] > 0:
        print(f"\n  ★ Best gate config: {best_label}")
        pm("Best", best_m)
        train, test = split_train_test(best_trades)
        mt = metrics(train)
        me = metrics(test)
        print(f"    Train: N={mt['n']:>4d}  PF={pf(mt['pf_r']):>6s}  TotalR={mt['total_r']:>+8.2f}")
        print(f"    Test:  N={me['n']:>4d}  PF={pf(me['pf_r']):>6s}  TotalR={me['total_r']:>+8.2f}")

        # Verdict
        survives = best_m['pf_r'] >= 1.10 and best_m['total_r'] > 0
        both_positive = mt['total_r'] > 0 and me['total_r'] > 0
        if survives and both_positive:
            print(f"    VERDICT: ★ CANDIDATE — PF {pf(best_m['pf_r'])} cost-on, train+test positive")
        elif survives:
            print(f"    VERDICT: MARGINAL — PF {pf(best_m['pf_r'])} cost-on but train/test split weak")
        else:
            print(f"    VERDICT: REJECTED — best config PF {pf(best_m['pf_r'])} insufficient")
    else:
        print(f"\n  VERDICT: REJECTED — no viable gate config found")

    return {"name": name, "raw": mr, "cost": mc, "best_gate": best_label, "best_m": best_m}


def run_long_hunt():
    symbols = get_universe()
    spy_bars = load_bars("SPY")
    qqq_bars = load_bars("QQQ")

    print(f"{'='*80}")
    print(f"LONG CANDIDATE HUNT — Systematic gate exploration")
    print(f"{'='*80}")
    print(f"Universe: {len(symbols)} symbols")
    print(f"Method: run each long setup isolated, then explore quality/time/combo filters")
    print(f"Threshold: need PF >= 1.10 cost-on, total R > 0, train+test both positive")

    # All long-capable setups to test
    LONG_SETUPS = [
        ("VWAP KISS long", cfg_vwap_kiss_long, "VWAP KISS"),
        ("2ND CHANCE long", cfg_sc_long, "2ND CHANCE"),
        ("SC V2 long", cfg_sc_v2_long, "SC V2"),
        ("SPENCER long", cfg_spencer_long, "SPENCER"),
        ("VWAP RECLAIM long", cfg_vr_long, "VWAP RECLAIM"),
        ("VKA long", cfg_vka_long, "VKA"),
        ("EMA RECLAIM long", cfg_ema_scalp_long, None),  # filter to long direction only
        ("EMA FPIP long", cfg_ema_fpip_long, None),
        ("EMA PULL long", cfg_ep_long, "EMA PULL"),
        ("MCS long", cfg_mcs_long, "MCS"),
    ]

    results = []
    for name, cfg_fn, setup_filter in LONG_SETUPS:
        r = analyze_setup(name, cfg_fn, symbols, spy_bars, qqq_bars, setup_filter=setup_filter)
        if r:
            results.append(r)

    # ── Summary ──
    print(f"\n{'='*80}")
    print(f"LONG CANDIDATE SUMMARY")
    print(f"{'='*80}")
    print(f"  {'Setup':<25s} {'N_raw':>5s} {'PF_raw':>7s} {'N_cost':>6s} {'PF_cost':>7s} "
          f"{'TotR$':>8s} {'Best Gate':>25s} {'Best PF':>7s}")
    print(f"  {'─'*25} {'─'*5} {'─'*7} {'─'*6} {'─'*7} {'─'*8} {'─'*25} {'─'*7}")

    for r in results:
        best_gate = r.get('best_gate', 'N/A') or 'N/A'
        best_m = r.get('best_m', r['cost'])
        print(f"  {r['name']:<25s} {r['raw']['n']:>5d} {pf(r['raw']['pf_r']):>7s} "
              f"{r['cost']['n']:>6d} {pf(r['cost']['pf_r']):>7s} "
              f"{r['cost']['total_r']:>+8.2f} {best_gate:>25s} "
              f"{pf(best_m['pf_r']) if best_m else 'N/A':>7s}")


if __name__ == "__main__":
    run_long_hunt()
