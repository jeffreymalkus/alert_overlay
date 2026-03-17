"""
R-Normalized Baseline & RED+TREND Short Research.

1. Establish the long-only R-baseline (VK+SC, locked filters)
2. Re-examine BDR shorts on RED+TREND days ONLY in R-terms
3. Determine whether any short-side approach generates positive
   expectancy in R under consistent risk sizing

All metrics are R-primary. Points shown for reference only.

Usage:
    python -m alert_overlay.r_baseline_and_red_trend_study
"""

import statistics
from collections import defaultdict
from pathlib import Path
from typing import List

from ..backtest import load_bars_from_csv, run_backtest, Trade
from ..config import OverlayConfig
from ..models import SetupId, SETUP_DISPLAY_NAME
from ..market_context import SECTOR_MAP, get_sector_etf

DATA_DIR = Path(__file__).parent.parent / "data"
RISK_PER_TRADE = 100.0


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


class UTrade:
    __slots__ = ("pnl_points", "pnl_rr", "exit_reason", "bars_held",
                 "entry_time", "exit_time", "entry_date", "side", "setup",
                 "sym", "quality", "pnl_dollar")

    def __init__(self, t: Trade, sym: str):
        self.pnl_points = t.pnl_points
        self.pnl_rr = t.pnl_rr
        self.exit_reason = t.exit_reason
        self.bars_held = t.bars_held
        self.entry_time = t.signal.timestamp
        self.exit_time = t.exit_time if t.exit_time else t.signal.timestamp
        self.entry_date = t.signal.timestamp.date()
        self.side = "LONG" if t.signal.direction == 1 else "SHORT"
        self.setup = t.signal.setup_id
        self.sym = sym
        self.quality = t.signal.quality_score
        self.pnl_dollar = t.pnl_rr * RISK_PER_TRADE


def classify_spy_days(spy_bars):
    daily = defaultdict(list)
    for b in spy_bars:
        daily[b.timestamp.date()].append(b)
    day_info = {}
    for d in sorted(daily.keys()):
        bars = daily[d]
        o = bars[0].open
        c = bars[-1].close
        h = max(b.high for b in bars)
        lo = min(b.low for b in bars)
        rng = h - lo
        chg = (c - o) / o * 100 if o > 0 else 0
        if chg > 0.05:
            direction = "GREEN"
        elif chg < -0.05:
            direction = "RED"
        else:
            direction = "FLAT"
        if rng > 0:
            cp = (c - lo) / rng
            character = "TREND" if (cp >= 0.75 or cp <= 0.25) else "CHOPPY"
        else:
            character = "CHOPPY"
        day_info[d] = {"direction": direction, "character": character, "chg": chg}
    return day_info


def compute_r_metrics(trades: List[UTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0, "pf_r": 0, "exp_r": 0, "total_r": 0,
                "max_dd_r": 0, "total_dollar": 0, "max_dd_dollar": 0,
                "stop_rate": 0, "total_pts": 0}
    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    total_r = sum(t.pnl_rr for t in trades)
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf_r = gw / gl if gl > 0 else float("inf")
    total_dollar = sum(t.pnl_dollar for t in trades)
    cum = pk = dd_r = 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl_rr
        if cum > pk:
            pk = cum
        if pk - cum > dd_r:
            dd_r = pk - cum
    cum = pk = dd_d = 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl_dollar
        if cum > pk:
            pk = cum
        if pk - cum > dd_d:
            dd_d = pk - cum
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    return {
        "n": n, "wr": len(wins) / n * 100,
        "pf_r": pf_r, "exp_r": total_r / n,
        "total_r": total_r, "max_dd_r": dd_r,
        "total_dollar": total_dollar, "max_dd_dollar": dd_d,
        "stop_rate": stopped / n * 100,
        "total_pts": sum(t.pnl_points for t in trades),
    }


def get_regime(t, sdi):
    info = sdi.get(t.entry_date, {})
    return f"{info.get('direction', 'UNK')}+{info.get('character', 'UNK')}"


def main():
    excluded = {"SPY", "QQQ", "IWM"} | (set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"})
    symbols = sorted([
        p.stem.replace("_5min", "")
        for p in DATA_DIR.glob("*_5min.csv")
        if p.stem.replace("_5min", "") not in excluded
    ])

    spy_bars = load_bars_from_csv(str(DATA_DIR / "SPY_5min.csv"))
    qqq_bars = load_bars_from_csv(str(DATA_DIR / "QQQ_5min.csv"))
    sector_bars_dict = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sector_bars_dict[etf] = load_bars_from_csv(str(p))

    sdi = classify_spy_days(spy_bars)

    # Count regime days
    regime_counts = defaultdict(int)
    for info in sdi.values():
        regime_counts[f"{info['direction']}+{info['character']}"] += 1

    print("=" * 120)
    print("R-NORMALIZED BASELINE & RED+TREND SHORT RESEARCH")
    print("=" * 120)
    print(f"Universe: {len(symbols)} symbols  |  Risk: ${RISK_PER_TRADE:.0f}/trade  |  Primary metric: R-multiples")
    print(f"\nRegime distribution:")
    for regime in sorted(regime_counts.keys()):
        print(f"  {regime:<16}  {regime_counts[regime]:3d} days")

    # ═══════════════════════════════════════════════════════════════
    #  RUN 1: LONG-ONLY BASELINE
    # ═══════════════════════════════════════════════════════════════
    cfg_long = OverlayConfig()
    cfg_long.show_ema_scalp = False
    cfg_long.show_failed_bounce = False
    cfg_long.show_spencer = False
    cfg_long.show_ema_fpip = False
    cfg_long.show_sc_v2 = False
    cfg_long.show_breakdown_retest = False  # OFF

    print("\n\nRunning long-only backtest...")
    long_raw = []
    sym_map = {}
    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        result = run_backtest(bars, cfg=cfg_long, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        for t in result.trades:
            long_raw.append(t)
            sym_map[id(t)] = sym

    long_utrades = [UTrade(t, sym_map[id(t)]) for t in long_raw]

    # Apply locked long filters
    def is_red(t): return sdi.get(t.entry_date, {}).get("direction") == "RED"

    long_filtered = []
    for t in long_utrades:
        if t.side == "SHORT":
            continue  # should not exist, but safety
        hhmm = t.entry_time.hour * 100 + t.entry_time.minute
        if is_red(t):
            continue
        if t.quality < 2:
            continue
        if hhmm >= 1530:
            continue
        long_filtered.append(t)

    # ═══════════════════════════════════════════════════════════════
    #  RUN 2: BDR SHORTS (research-only, for RED+TREND analysis)
    # ═══════════════════════════════════════════════════════════════
    cfg_short = OverlayConfig()
    cfg_short.show_ema_scalp = False
    cfg_short.show_failed_bounce = False
    cfg_short.show_spencer = False
    cfg_short.show_ema_fpip = False
    cfg_short.show_sc_v2 = False
    cfg_short.show_breakdown_retest = True
    # Disable VK/SC long to isolate shorts
    cfg_short.show_vwap_kiss = False
    cfg_short.show_second_chance = False

    print("Running BDR-only backtest (research)...")
    short_raw = []
    sym_map2 = {}
    for sym in symbols:
        p = DATA_DIR / f"{sym}_5min.csv"
        if not p.exists():
            continue
        bars = load_bars_from_csv(str(p))
        if not bars:
            continue
        sec_etf = get_sector_etf(sym)
        sec_bars = sector_bars_dict.get(sec_etf) if sec_etf else None
        result = run_backtest(bars, cfg=cfg_short, spy_bars=spy_bars, qqq_bars=qqq_bars,
                              sector_bars=sec_bars)
        for t in result.trades:
            short_raw.append(t)
            sym_map2[id(t)] = sym

    short_utrades = [UTrade(t, sym_map2[id(t)]) for t in short_raw]

    print(f"  Long filtered: {len(long_filtered)}")
    print(f"  BDR shorts (all regimes): {len(short_utrades)}")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 1: LONG-ONLY R-BASELINE
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 1: LONG-ONLY R-BASELINE (ACTIVE CANDIDATE)")
    print("=" * 120)

    m_long = compute_r_metrics(long_filtered)
    print(f"\n  N={m_long['n']}  WR={m_long['wr']:.1f}%  PF(R)={pf_str(m_long['pf_r'])}  "
          f"Exp={m_long['exp_r']:+.3f}R  TotalR={m_long['total_r']:+.2f}  "
          f"MaxDD={m_long['max_dd_r']:.2f}R  ${m_long['total_dollar']:+,.0f}")

    # By setup
    print(f"\n  By setup:")
    print(f"    {'Setup':<16}  {'N':>4}  {'WR':>6}  {'PF(R)':>6}  {'Exp':>8}  {'TotalR':>8}  {'$PnL':>8}")
    print(f"    {'-'*16}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*8}")
    for setup_id in [SetupId.VWAP_KISS, SetupId.SECOND_CHANCE]:
        st = [t for t in long_filtered if t.setup == setup_id]
        m = compute_r_metrics(st)
        name = SETUP_DISPLAY_NAME.get(setup_id, str(setup_id))
        print(f"    {name:<16}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf_r']):>6s}  "
              f"{m['exp_r']:+.3f}R  {m['total_r']:+8.2f}R  ${m['total_dollar']:+7.0f}")

    # By regime
    print(f"\n  By regime:")
    for regime in ["GREEN+TREND", "GREEN+CHOPPY", "FLAT+CHOPPY"]:
        rt = [t for t in long_filtered if get_regime(t, sdi) == regime]
        if not rt:
            continue
        m = compute_r_metrics(rt)
        print(f"    {regime:<16}  N={m['n']:3d}  PF(R)={pf_str(m['pf_r']):>5s}  "
              f"Exp={m['exp_r']:+.3f}R  TotalR={m['total_r']:+.2f}R")

    # Per-symbol in R
    sym_r = defaultdict(lambda: {"r": 0.0, "n": 0})
    for t in long_filtered:
        sym_r[t.sym]["r"] += t.pnl_rr
        sym_r[t.sym]["n"] += 1
    sorted_sr = sorted(sym_r.items(), key=lambda x: x[1]["r"], reverse=True)

    print(f"\n  Top 10 symbols by R:")
    for sym, st in sorted_sr[:10]:
        avg = st["r"] / st["n"]
        print(f"    {sym:<8}  N={st['n']:2d}  TotalR={st['r']:+.2f}  AvgR={avg:+.3f}")
    print(f"  Bottom 5:")
    for sym, st in sorted_sr[-5:]:
        avg = st["r"] / st["n"]
        print(f"    {sym:<8}  N={st['n']:2d}  TotalR={st['r']:+.2f}  AvgR={avg:+.3f}")

    # Train/test
    train = [t for t in long_filtered if t.entry_date.day % 2 == 1]
    test = [t for t in long_filtered if t.entry_date.day % 2 == 0]
    mt = compute_r_metrics(train)
    ms = compute_r_metrics(test)
    wr_d = ms["wr"] - mt["wr"]
    stable = mt["pf_r"] >= 1.0 and ms["pf_r"] >= 1.0 and abs(wr_d) < 5.0
    print(f"\n  Train/test:")
    print(f"    Train: N={mt['n']:3d}  PF(R)={pf_str(mt['pf_r'])}  Exp={mt['exp_r']:+.3f}R  TotalR={mt['total_r']:+.2f}")
    print(f"    Test:  N={ms['n']:3d}  PF(R)={pf_str(ms['pf_r'])}  Exp={ms['exp_r']:+.3f}R  TotalR={ms['total_r']:+.2f}")
    print(f"    WR Δ={wr_d:+.1f}%  {'STABLE' if stable else 'UNSTABLE'}")

    # Concentration
    pos_syms = sum(1 for _, st in sym_r.items() if st["r"] > 0)
    total_syms = len(sym_r)
    top1_pct = sorted_sr[0][1]["r"] / m_long["total_r"] * 100 if m_long["total_r"] != 0 else 0
    print(f"\n  Concentration:")
    print(f"    Positive R symbols: {pos_syms}/{total_syms}")
    print(f"    Top-1: {sorted_sr[0][0]} = {top1_pct:.0f}% of total R")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 2: BDR SHORTS — ALL REGIMES (R reference)
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 2: BDR SHORTS — ALL REGIMES (R-normalized, research reference)")
    print("=" * 120)

    m_all_s = compute_r_metrics(short_utrades)
    print(f"\n  All BDR shorts: N={m_all_s['n']}  PF(R)={pf_str(m_all_s['pf_r'])}  "
          f"Exp={m_all_s['exp_r']:+.3f}R  TotalR={m_all_s['total_r']:+.2f}  "
          f"MaxDD={m_all_s['max_dd_r']:.2f}R  Pts={m_all_s['total_pts']:+.2f}")

    print(f"\n  By regime:")
    print(f"    {'Regime':<16}  {'N':>4}  {'WR':>6}  {'PF(R)':>6}  {'Exp':>8}  "
          f"{'TotalR':>8}  {'MaxDD_R':>8}  {'StpR':>5}")
    print(f"    {'-'*16}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  "
          f"{'-'*8}  {'-'*8}  {'-'*5}")
    for regime in ["GREEN+TREND", "GREEN+CHOPPY", "RED+TREND", "RED+CHOPPY", "FLAT+CHOPPY"]:
        rt = [t for t in short_utrades if get_regime(t, sdi) == regime]
        if not rt:
            continue
        m = compute_r_metrics(rt)
        print(f"    {regime:<16}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf_r']):>6s}  "
              f"{m['exp_r']:+.3f}R  {m['total_r']:+8.2f}R  {m['max_dd_r']:8.2f}R  {m['stop_rate']:4.1f}%")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 3: RED+TREND SHORT DEEP DIVE
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 3: RED+TREND BDR SHORTS — DEEP DIVE")
    print("=" * 120)

    rt_shorts = [t for t in short_utrades if get_regime(t, sdi) == "RED+TREND"]
    m_rt = compute_r_metrics(rt_shorts)

    print(f"\n  RED+TREND BDR shorts:")
    print(f"    N={m_rt['n']}  WR={m_rt['wr']:.1f}%  PF(R)={pf_str(m_rt['pf_r'])}  "
          f"Exp={m_rt['exp_r']:+.3f}R  TotalR={m_rt['total_r']:+.2f}  "
          f"MaxDD={m_rt['max_dd_r']:.2f}R  StpR={m_rt['stop_rate']:.1f}%")

    # R-distribution
    if rt_shorts:
        rr_vals = [t.pnl_rr for t in rt_shorts]
        print(f"\n  R-distribution:")
        print(f"    Mean:   {statistics.mean(rr_vals):+.3f}R")
        print(f"    Median: {statistics.median(rr_vals):+.3f}R")
        print(f"    Stdev:  {statistics.stdev(rr_vals):.3f}R" if len(rr_vals) > 1 else "")
        print(f"    Min:    {min(rr_vals):+.3f}R")
        print(f"    Max:    {max(rr_vals):+.3f}R")

    # Per-symbol
    rt_sym_r = defaultdict(lambda: {"r": 0.0, "n": 0, "pts": 0.0})
    for t in rt_shorts:
        rt_sym_r[t.sym]["r"] += t.pnl_rr
        rt_sym_r[t.sym]["n"] += 1
        rt_sym_r[t.sym]["pts"] += t.pnl_points
    sorted_rt_sr = sorted(rt_sym_r.items(), key=lambda x: x[1]["r"], reverse=True)

    print(f"\n  Per-symbol (R-normalized):")
    print(f"    {'Sym':<8}  {'N':>3}  {'TotalR':>8}  {'AvgR':>7}  {'Pts':>8}")
    print(f"    {'-'*8}  {'-'*3}  {'-'*8}  {'-'*7}  {'-'*8}")
    for sym, st in sorted_rt_sr:
        avg = st["r"] / st["n"]
        print(f"    {sym:<8}  {st['n']:3d}  {st['r']:+8.2f}R  {avg:+6.3f}R  {st['pts']:+8.2f}")

    pos_syms = sum(1 for _, st in rt_sym_r.items() if st["r"] > 0)
    neg_syms = sum(1 for _, st in rt_sym_r.items() if st["r"] <= 0)
    print(f"\n  Symbols: {pos_syms} positive R, {neg_syms} negative R, "
          f"{len(rt_sym_r)} total")

    # By quality
    print(f"\n  By quality score:")
    print(f"    {'Q':>2}  {'N':>4}  {'WR':>6}  {'PF(R)':>6}  {'Exp':>8}  {'TotalR':>8}")
    print(f"    {'-'*2}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*8}")
    q_grp = defaultdict(list)
    for t in rt_shorts:
        q_grp[t.quality].append(t)
    for q in sorted(q_grp.keys()):
        m = compute_r_metrics(q_grp[q])
        print(f"    {q:2d}  {m['n']:4d}  {m['wr']:5.1f}%  {pf_str(m['pf_r']):>6s}  "
              f"{m['exp_r']:+.3f}R  {m['total_r']:+8.2f}R")

    # By exit reason
    print(f"\n  By exit reason:")
    exit_grp = defaultdict(list)
    for t in rt_shorts:
        exit_grp[t.exit_reason].append(t)
    for reason in sorted(exit_grp.keys()):
        m = compute_r_metrics(exit_grp[reason])
        print(f"    {reason:<12}  N={m['n']:3d}  Avg={m['exp_r']:+.3f}R  TotalR={m['total_r']:+.2f}")

    # Per-day
    print(f"\n  Per RED+TREND day:")
    rt_daily = defaultdict(list)
    for t in rt_shorts:
        rt_daily[t.entry_date].append(t)
    print(f"    {'Date':<12}  {'N':>3}  {'WR':>6}  {'TotalR':>8}  {'SPY':>6}")
    print(f"    {'-'*12}  {'-'*3}  {'-'*6}  {'-'*8}  {'-'*6}")
    for d in sorted(rt_daily.keys()):
        dt = rt_daily[d]
        m = compute_r_metrics(dt)
        spy_chg = sdi.get(d, {}).get("chg", 0)
        print(f"    {d}  {m['n']:3d}  {m['wr']:5.1f}%  {m['total_r']:+8.2f}R  {spy_chg:+5.2f}%")

    # Train/test
    if rt_shorts:
        train_rt = [t for t in rt_shorts if t.entry_date.day % 2 == 1]
        test_rt = [t for t in rt_shorts if t.entry_date.day % 2 == 0]
        mt = compute_r_metrics(train_rt)
        ms = compute_r_metrics(test_rt)
        wr_d = ms["wr"] - mt["wr"] if mt["n"] > 0 and ms["n"] > 0 else 0
        stable = mt["pf_r"] >= 1.0 and ms["pf_r"] >= 1.0 and abs(wr_d) < 5.0
        print(f"\n  Train/test:")
        print(f"    Train: N={mt['n']:3d}  PF(R)={pf_str(mt['pf_r'])}  Exp={mt['exp_r']:+.3f}R  TotalR={mt['total_r']:+.2f}")
        print(f"    Test:  N={ms['n']:3d}  PF(R)={pf_str(ms['pf_r'])}  Exp={ms['exp_r']:+.3f}R  TotalR={ms['total_r']:+.2f}")
        print(f"    WR Δ={wr_d:+.1f}%  {'STABLE' if stable else 'UNSTABLE'}")

    # Exclude top symbol
    if sorted_rt_sr:
        top_sym = sorted_rt_sr[0][0]
        excl = [t for t in rt_shorts if t.sym != top_sym]
        m_excl = compute_r_metrics(excl)
        print(f"\n  Excluding top symbol ({top_sym}):")
        print(f"    N={m_excl['n']}  PF(R)={pf_str(m_excl['pf_r'])}  "
              f"Exp={m_excl['exp_r']:+.3f}R  TotalR={m_excl['total_r']:+.2f}")

    # ═══════════════════════════════════════════════════════════════
    #  SECTION 4: HYPOTHETICAL COMBINED (LONG + RED+TREND SHORT)
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("SECTION 4: HYPOTHETICAL — LONG BASELINE + RED+TREND BDR ONLY")
    print("=" * 120)

    combined = long_filtered + rt_shorts
    m_comb = compute_r_metrics(combined)
    print(f"\n  Combined: N={m_comb['n']}  PF(R)={pf_str(m_comb['pf_r'])}  "
          f"Exp={m_comb['exp_r']:+.3f}R  TotalR={m_comb['total_r']:+.2f}  "
          f"MaxDD={m_comb['max_dd_r']:.2f}R  ${m_comb['total_dollar']:+,.0f}")

    print(f"\n  Comparison:")
    print(f"    Long only:           N={m_long['n']:3d}  PF(R)={pf_str(m_long['pf_r'])}  "
          f"Exp={m_long['exp_r']:+.3f}R  TotalR={m_long['total_r']:+.2f}  MaxDD={m_long['max_dd_r']:.2f}R")
    print(f"    RED+TREND short:     N={m_rt['n']:3d}  PF(R)={pf_str(m_rt['pf_r'])}  "
          f"Exp={m_rt['exp_r']:+.3f}R  TotalR={m_rt['total_r']:+.2f}  MaxDD={m_rt['max_dd_r']:.2f}R")
    print(f"    Combined:            N={m_comb['n']:3d}  PF(R)={pf_str(m_comb['pf_r'])}  "
          f"Exp={m_comb['exp_r']:+.3f}R  TotalR={m_comb['total_r']:+.2f}  MaxDD={m_comb['max_dd_r']:.2f}R")

    # Does the short book add R?
    short_adds = m_rt["total_r"] > 0
    combined_better = m_comb["total_r"] > m_long["total_r"]
    dd_acceptable = m_comb["max_dd_r"] < m_long["max_dd_r"] * 1.5

    print(f"\n  Short adds R:        {'YES' if short_adds else 'NO'} ({m_rt['total_r']:+.2f}R)")
    print(f"  Combined > long:     {'YES' if combined_better else 'NO'}")
    print(f"  DD acceptable:       {'YES' if dd_acceptable else 'NO'} "
          f"({m_comb['max_dd_r']:.2f}R vs {m_long['max_dd_r']:.2f}R long-only)")

    # Train/test combined
    train_c = [t for t in combined if t.entry_date.day % 2 == 1]
    test_c = [t for t in combined if t.entry_date.day % 2 == 0]
    mt_c = compute_r_metrics(train_c)
    ms_c = compute_r_metrics(test_c)
    wr_dc = ms_c["wr"] - mt_c["wr"]
    stable_c = mt_c["pf_r"] >= 1.0 and ms_c["pf_r"] >= 1.0 and abs(wr_dc) < 5.0

    print(f"\n  Train/test (combined):")
    print(f"    Train: N={mt_c['n']:3d}  PF(R)={pf_str(mt_c['pf_r'])}  Exp={mt_c['exp_r']:+.3f}R")
    print(f"    Test:  N={ms_c['n']:3d}  PF(R)={pf_str(ms_c['pf_r'])}  Exp={ms_c['exp_r']:+.3f}R")
    print(f"    WR Δ={wr_dc:+.1f}%  {'STABLE' if stable_c else 'UNSTABLE'}")

    # ═══════════════════════════════════════════════════════════════
    #  VERDICT
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 120)
    print("VERDICT")
    print("=" * 120)

    checks = [
        ("Long baseline PF(R) >= 1.0",        m_long["pf_r"] >= 1.0),
        ("Long baseline Exp > 0",              m_long["exp_r"] > 0),
        ("Long train/test stable",             mt["pf_r"] >= 1.0 and ms["pf_r"] >= 1.0 if long_filtered else False),
        ("RED+TREND short PF(R) >= 1.0",       m_rt["pf_r"] >= 1.0),
        ("RED+TREND short Exp > 0",            m_rt["exp_r"] > 0),
        ("RED+TREND short adds R to combined", m_rt["total_r"] > 0),
        ("Combined PF(R) >= 1.0",             m_comb["pf_r"] >= 1.0),
    ]

    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")

    print()


if __name__ == "__main__":
    main()
