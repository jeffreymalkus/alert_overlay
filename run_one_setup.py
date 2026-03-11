"""
Run a single retired setup replay. Minimal memory footprint.
Usage: python -m alert_overlay.run_one_setup <setup_name>
Names: spencer_long, spencer_both, sc_v2, vwap_reclaim, vka, mcs,
       failed_bounce, ema_reclaim, ema_fpip, ema_confirm,
       vwap_kiss, ema_retest, reversals, ema9_sep
"""
import sys, gc
from alert_overlay.retired_replay import (
    _base_cfg, _spencer_cfg, _spencer_both_cfg, _sc_v2_cfg,
    _vwap_reclaim_cfg, _vka_cfg, _mcs_cfg, _failed_bounce_cfg,
    _ema_reclaim_cfg, _ema_fpip_cfg, _ema_confirm_cfg,
    _vwap_kiss_cfg, _ema_retest_cfg, _reversals_cfg, _ema9_sep_cfg,
    _run_setup_section, DATA_DIR,
)
from alert_overlay.backtest import load_bars_from_csv
from alert_overlay.models import SetupId
from alert_overlay.market_context import SECTOR_MAP, get_sector_etf

SETUP_DEFS = {
    "spencer_long":   (1, "SPENCER LONG-ONLY", _spencer_cfg, {SetupId.SPENCER}),
    "spencer_both":   (2, "SPENCER BOTH DIRECTIONS", _spencer_both_cfg, {SetupId.SPENCER}),
    "sc_v2":          (3, "SC V2 (SECOND CHANCE V2)", _sc_v2_cfg, {SetupId.SC_V2}),
    "vwap_reclaim":   (4, "VWAP RECLAIM LONG", _vwap_reclaim_cfg, {SetupId.VWAP_RECLAIM}),
    "vka":            (5, "VK ACCEPT LONG", _vka_cfg, {SetupId.VKA}),
    "mcs":            (6, "MCS LONG-ONLY", _mcs_cfg, {SetupId.MCS}),
    "failed_bounce":  (7, "FAILED BOUNCE SHORT", _failed_bounce_cfg, {SetupId.FAILED_BOUNCE}),
    "ema_reclaim":    (8, "EMA RECLAIM (9EMA SCALP)", _ema_reclaim_cfg, {SetupId.EMA_RECLAIM}),
    "ema_fpip":       (9, "EMA FPIP (FIRST PULLBACK IN PLAY)", _ema_fpip_cfg, {SetupId.EMA_FPIP}),
    "ema_confirm":    (10, "EMA CONFIRM", _ema_confirm_cfg, {SetupId.EMA_CONFIRM}),
    "vwap_kiss":      (11, "VWAP KISS LONG-ONLY", _vwap_kiss_cfg, {SetupId.VWAP_KISS}),
    "ema_retest":     (12, "EMA RETEST", _ema_retest_cfg, {SetupId.EMA_RETEST}),
    "reversals":      (13, "REVERSALS (BOX_REV/MANIP/VWAP_SEP)", _reversals_cfg,
                       {SetupId.BOX_REV, SetupId.MANIP, SetupId.VWAP_SEP}),
    "ema9_sep":       (14, "EMA9 SEP (MEAN REVERSION)", _ema9_sep_cfg, {SetupId.EMA9_SEP}),
}

def main():
    name = sys.argv[1] if len(sys.argv) > 1 else None
    if not name or name not in SETUP_DEFS:
        print(f"Usage: python -m alert_overlay.run_one_setup <name>")
        print(f"Available: {', '.join(SETUP_DEFS.keys())}")
        sys.exit(1)

    sec_num, label, cfg_fn, setup_filter = SETUP_DEFS[name]

    print(f"Loading market data...")
    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))

    sector_bars_dict = {}
    sector_etfs = sorted(set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    for etf in sector_etfs:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    spy_dates = sorted(set(b.timestamp.date() for b in spy_bars))
    date_range = f"{spy_dates[0]} → {spy_dates[-1]} ({len(spy_dates)} trading days)"

    excluded = {"SPY", "QQQ", "IWM"} | set(sector_etfs)
    all_data_files = sorted(DATA_DIR.glob("*_5min.csv"))
    symbols = sorted([
        p.stem.replace("_5min", "")
        for p in all_data_files
        if p.stem.replace("_5min", "") not in excluded
    ])

    cfg = cfg_fn()
    _run_setup_section(
        sec_num, label, cfg, setup_filter, f"retired_{name}",
        symbols, spy_bars, qqq_bars, sector_bars_dict, date_range, len(symbols))

    sys.stdout.flush()

if __name__ == "__main__":
    main()
