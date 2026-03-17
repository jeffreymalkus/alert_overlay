"""
Suppression Triage — Disable engine gates one at a time to find dominant suppressors.

Approach: Run engine backtest with progressively relaxed gates:
  Base:        default config (all gates on)
  -regime:     require_regime=False
  -quality:    min_quality=0
  -cooldown:   alert_cooldown_bars=0
  -risk_floor: min_stop_atr_frac=0
  -day_filter: vr_day_filter="none"
  ALL_OFF:     all gates removed

For each variant, count VR trades and compute PF(R)/TotalR.
The gate whose removal adds the most trades is the dominant suppressor.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import List

from ..backtest import run_backtest, load_bars_from_csv, Trade
from ..config import OverlayConfig
from ..market_context import get_sector_etf, SECTOR_MAP
from ..models import NaN, SetupId

DATA_DIR = Path(__file__).parent.parent / "data"
_isnan = math.isnan


def load_bars(sym: str) -> list:
    p = DATA_DIR / f"{sym}_5min.csv"
    return load_bars_from_csv(str(p)) if p.exists() else []


def get_universe() -> list:
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    return sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])


@dataclass
class EngTrade:
    symbol: str
    trade: Trade
    @property
    def pnl_rr(self): return self.trade.pnl_rr
    @property
    def entry_date(self): return self.trade.signal.timestamp.date()
    @property
    def entry_time(self): return self.trade.signal.timestamp
    @property
    def exit_reason(self): return self.trade.exit_reason
    @property
    def hhmm(self): return self.trade.signal.timestamp.hour * 100 + self.trade.signal.timestamp.minute


def run_engine_vr(cfg: OverlayConfig, symbols, spy_bars, qqq_bars) -> List[EngTrade]:
    trades = []
    for sym in symbols:
        bars = load_bars(sym)
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = load_bars(sec_etf) if sec_etf and sec_etf not in {"SPY", "QQQ"} else None
        result = run_backtest(bars, cfg=cfg, spy_bars=spy_bars,
                              qqq_bars=qqq_bars, sector_bars=sec_bars)
        for t in result.trades:
            if t.signal.setup_name == "VWAP RECLAIM":
                trades.append(EngTrade(symbol=sym, trade=t))
    return trades


def compute_metrics(trades):
    if not trades:
        return {"n": 0, "pf_r": 0.0, "total_r": 0.0, "exp_r": 0.0}
    wins = sum(t.pnl_rr for t in trades if t.pnl_rr > 0)
    losses = abs(sum(t.pnl_rr for t in trades if t.pnl_rr < 0))
    total = sum(t.pnl_rr for t in trades)
    return {
        "n": len(trades),
        "pf_r": wins / losses if losses > 0 else float('inf'),
        "total_r": total,
        "exp_r": total / len(trades),
    }


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


def make_base_cfg():
    """Standard VR + BDR config."""
    from .portfolio_configs import candidate_v2_vrgreen_bdr
    return candidate_v2_vrgreen_bdr()


def main():
    symbols = get_universe()
    spy_bars = load_bars("SPY")
    qqq_bars = load_bars("QQQ")

    print("=" * 140)
    print("SUPPRESSION TRIAGE — Gate-by-gate removal for VR")
    print("=" * 140)
    print(f"Universe: {len(symbols)} symbols")

    # Define gate removal variants
    variants = []

    # 0. Base config (all gates on)
    def v_base():
        return make_base_cfg()
    variants.append(("BASE (all gates on)", v_base))

    # 1. Remove day filter
    def v_no_dayfilter():
        cfg = make_base_cfg()
        cfg.vr_day_filter = "none"
        return cfg
    variants.append(("- day_filter (vr_day_filter=none)", v_no_dayfilter))

    # 2. Remove regime requirement
    def v_no_regime():
        cfg = make_base_cfg()
        cfg.require_regime = False
        return cfg
    variants.append(("- regime (require_regime=False)", v_no_regime))

    # 3. Remove quality gate
    def v_no_quality():
        cfg = make_base_cfg()
        cfg.min_quality = 0
        return cfg
    variants.append(("- quality (min_quality=0)", v_no_quality))

    # 4. Remove cooldown
    def v_no_cooldown():
        cfg = make_base_cfg()
        cfg.alert_cooldown_bars = 0
        return cfg
    variants.append(("- cooldown (alert_cooldown_bars=0)", v_no_cooldown))

    # 5. Remove risk floor
    def v_no_risk_floor():
        cfg = make_base_cfg()
        cfg.min_stop_intra_atr_mult = 0.0
        return cfg
    variants.append(("- risk_floor (min_stop_intra_atr_mult=0)", v_no_risk_floor))

    # 6. Remove slippage (to match standalone)
    def v_no_slippage():
        cfg = make_base_cfg()
        cfg.use_dynamic_slippage = False
        cfg.slippage_per_side = 0.0
        cfg.commission_per_share = 0.0
        return cfg
    variants.append(("- slippage (zero slippage+comm)", v_no_slippage))

    # 7. Remove day_filter + regime (the two biggest suspects combined)
    def v_no_dayfilter_regime():
        cfg = make_base_cfg()
        cfg.vr_day_filter = "none"
        cfg.require_regime = False
        return cfg
    variants.append(("- day_filter - regime", v_no_dayfilter_regime))

    # 8. Remove day_filter + regime + cooldown
    def v_no_dayfilter_regime_cd():
        cfg = make_base_cfg()
        cfg.vr_day_filter = "none"
        cfg.require_regime = False
        cfg.alert_cooldown_bars = 0
        return cfg
    variants.append(("- day_filter - regime - cooldown", v_no_dayfilter_regime_cd))

    # 9. ALL gates off + no slippage (should match standalone-no-filter)
    def v_all_off():
        cfg = make_base_cfg()
        cfg.vr_day_filter = "none"
        cfg.require_regime = False
        cfg.min_quality = 0
        cfg.alert_cooldown_bars = 0
        cfg.min_stop_intra_atr_mult = 0.0
        cfg.use_dynamic_slippage = False
        cfg.slippage_per_side = 0.0
        cfg.commission_per_share = 0.0
        cfg.vr_require_market_align = False  # remove market context gate
        return cfg
    variants.append(("ALL GATES OFF + no slippage", v_all_off))

    # 10. Market align removal only (with slippage, to measure its marginal value)
    def v_no_market_align():
        cfg = make_base_cfg()
        cfg.vr_require_market_align = False
        return cfg
    variants.append(("- market_align (vr_require_market_align=False)", v_no_market_align))

    # Run each variant
    results = {}
    for label, cfg_fn in variants:
        print(f"\n  Running: {label}...")
        trades = run_engine_vr(cfg_fn(), symbols, spy_bars, qqq_bars)
        m = compute_metrics(trades)
        results[label] = (trades, m)

    # Results table
    print(f"\n{'=' * 140}")
    print("RESULTS")
    print(f"{'=' * 140}")

    base_n = results[variants[0][0]][1]["n"]
    base_r = results[variants[0][0]][1]["total_r"]

    print(f"\n  {'Variant':45s} {'N':>5s} {'ΔN':>6s} {'PF(R)':>6s} {'TotalR':>9s} {'ΔR':>9s} {'Exp(R)':>8s}")
    print(f"  {'-'*45} {'-'*5} {'-'*6} {'-'*6} {'-'*9} {'-'*9} {'-'*8}")

    for label, _ in variants:
        trades, m = results[label]
        dn = m["n"] - base_n
        dr = m["total_r"] - base_r
        print(f"  {label:45s} {m['n']:5d} {dn:+5d} {pf_str(m['pf_r']):>6s} "
              f"{m['total_r']:+8.2f}R {dr:+8.2f}R {m['exp_r']:+7.3f}R")

    # ── Marginal contribution analysis ──
    print(f"\n{'=' * 140}")
    print("MARGINAL CONTRIBUTION — How much does each gate ADD/REMOVE?")
    print(f"{'=' * 140}")

    base_label = variants[0][0]
    base_m = results[base_label][1]

    print(f"\n  Base: N={base_m['n']}, PF={pf_str(base_m['pf_r'])}, TotalR={base_m['total_r']:+.2f}R")
    print(f"\n  Removing this gate alone:")
    print(f"  {'Gate removed':45s} {'ΔN':>6s} {'ΔR':>9s} {'New PF':>7s} {'Verdict':>20s}")
    print(f"  {'-'*45} {'-'*6} {'-'*9} {'-'*7} {'-'*20}")

    for label, _ in variants[1:]:
        if label.startswith("ALL") or label.count("-") > 1:
            continue  # skip combos
        m = results[label][1]
        dn = m["n"] - base_m["n"]
        dr = m["total_r"] - base_m["total_r"]
        if dn > 0 and dr > 0:
            verdict = "HURTING (remove it)"
        elif dn > 0 and dr <= 0:
            verdict = "helping (keep it)"
        elif dn == 0:
            verdict = "no effect"
        else:
            verdict = "mixed"
        print(f"  {label:45s} {dn:+5d} {dr:+8.2f}R {pf_str(m['pf_r']):>7s} {verdict:>20s}")

    # ── Compare ALL OFF vs standalone ──
    all_off_label = "ALL GATES OFF + no slippage"
    all_off_m = results[all_off_label][1]
    print(f"\n  ALL gates off + no slippage: N={all_off_m['n']}, PF={pf_str(all_off_m['pf_r'])}, "
          f"TotalR={all_off_m['total_r']:+.2f}R")
    print(f"  (Standalone no-filter produced 1288 trades — difference={all_off_m['n'] - 1288} "
          f"indicates remaining engine-vs-standalone state machine divergence)")

    print(f"\n{'=' * 140}")


if __name__ == "__main__":
    main()
