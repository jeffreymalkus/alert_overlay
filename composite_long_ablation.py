"""
Composite Long Ablation Study — LIVE-AVAILABLE ONLY

Critical correction from exploratory run: ALL prior variants used GREEN-day
(end-of-day SPY return) as a gate — this is hindsight / non-deployable.

This study uses ONLY live-available market gates:
  ML0: No market gate (all days)
  ML1: SPY above VWAP at signal bar
  ML2: SPY above VWAP + EMA9 > EMA20 at signal bar
  ML3: ML2 + EMA9 rising

Sections:
  1. Identify best live candidate (market gate sweep)
  2. Mandatory ablations (remove each filter layer)
  3. In-play soft gate sweep (no / loose / medium / strict)
  4. Deployability stats
  5. PF/Expectancy decomposition

Usage:
    cd /sessions/inspiring-clever-meitner/mnt
    python -m alert_overlay.composite_long_ablation
"""

import argparse
import math
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .backtest import load_bars_from_csv
from .indicators import EMA, VWAPCalc
from .market_context import MarketEngine, SECTOR_MAP, get_sector_etf
from .models import Bar, NaN

DATA_DIR = Path(__file__).parent / "data"
_isnan = math.isnan


# ════════════════════════════════════════════════════════════════
#  Trade model
# ════════════════════════════════════════════════════════════════

@dataclass
class CTrade:
    symbol: str
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    direction: int = 1
    setup: str = ""
    variant: str = ""
    risk: float = 0.0
    pnl_rr: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0
    gap_pct: float = 0.0
    rvol: float = 0.0
    rs_spy: float = 0.0
    rs_sector: float = 0.0
    market_level: str = ""
    inplay_score: int = 0  # 0-3 soft gate

    @property
    def entry_date(self) -> date:
        return self.entry_time.date()


# ════════════════════════════════════════════════════════════════
#  Data loading
# ════════════════════════════════════════════════════════════════

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


# ════════════════════════════════════════════════════════════════
#  LIVE-ONLY Market context (Layer M) — NO GREEN-day hindsight
# ════════════════════════════════════════════════════════════════

def build_spy_snapshots(spy_bars: list) -> dict:
    me = MarketEngine()
    snapshots = {}
    for b in spy_bars:
        snap = me.process_bar(b)
        d = b.timestamp.date()
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute
        snapshots[(d, hhmm)] = snap
    return snapshots


def check_market_live(spy_ctx: dict, d: date, hhmm: int, m_level: str) -> bool:
    """LIVE-ONLY market gate. No GREEN-day hindsight."""
    if m_level == "ML0":
        return True  # no gate

    snap = spy_ctx.get((d, hhmm))
    if snap is None or not snap.ready:
        return False  # no data = block (conservative for live)

    if m_level == "ML1":
        return snap.above_vwap

    if m_level == "ML2":
        return snap.above_vwap and snap.ema9_above_ema20

    if m_level == "ML3":
        return snap.above_vwap and snap.ema9_above_ema20 and snap.ema9_rising

    return True


# ════════════════════════════════════════════════════════════════
#  In-play proxy (Layer P) — now with soft scoring
# ════════════════════════════════════════════════════════════════

def compute_open_stats(bars: list) -> dict:
    daily = defaultdict(list)
    for b in bars:
        daily[b.timestamp.date()].append(b)
    dates_sorted = sorted(daily.keys())

    stats = {}
    vol_baseline_buf = deque(maxlen=20)

    for idx, d in enumerate(dates_sorted):
        day_bars = daily[d]
        prior_close = None
        if idx > 0:
            prev_d = dates_sorted[idx - 1]
            prev_bars = daily[prev_d]
            if prev_bars:
                prior_close = prev_bars[-1].close

        first3 = day_bars[:3]
        vol_first3 = sum(b.volume for b in first3)
        dolvol_first3 = sum(b.close * b.volume for b in first3)

        gap_pct = 0.0
        if prior_close and prior_close > 0 and day_bars:
            gap_pct = (day_bars[0].open - prior_close) / prior_close * 100

        rvol = 0.0
        if len(vol_baseline_buf) >= 5:
            avg_vol = sum(vol_baseline_buf) / len(vol_baseline_buf)
            if avg_vol > 0:
                rvol = vol_first3 / avg_vol

        vol_baseline_buf.append(vol_first3)

        # Soft score: 0-3 based on three proxies
        score = 0
        if abs(gap_pct) > 1.0:
            score += 1
        if rvol > 2.0:
            score += 1
        if dolvol_first3 > 5_000_000:  # $5M+ in first 15 min
            score += 1

        stats[d] = {
            "gap_pct": gap_pct, "rvol": rvol,
            "dolvol": dolvol_first3, "score": score,
        }

    return stats


def check_inplay(open_stats: dict, d: date, p_level: str) -> Tuple[bool, int]:
    """Returns (passes, score). Score is always returned for metadata."""
    s = open_stats.get(d)
    if s is None:
        return (p_level == "-"), 0

    score = s.get("score", 0)
    if p_level == "-":
        return True, score
    if p_level == "P_LOOSE":   # 1 of 3
        return score >= 1, score
    if p_level == "P_MED":     # 2 of 3
        return score >= 2, score
    if p_level == "P_STRICT":  # 3 of 3
        return score >= 3, score
    return True, score


# ════════════════════════════════════════════════════════════════
#  Leadership (Layer L)
# ════════════════════════════════════════════════════════════════

def build_pct_from_open(bars: list) -> dict:
    daily_open = {}
    result = {}
    for b in bars:
        d = b.timestamp.date()
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute
        if d not in daily_open:
            daily_open[d] = b.open
        o = daily_open[d]
        result[(d, hhmm)] = (b.close - o) / o * 100 if o > 0 else 0.0
    return result


def check_leadership(stock_pfo: dict, spy_pfo: dict, sector_pfo: dict,
                     d: date, hhmm: int, l_level: str) -> Tuple[bool, float, float]:
    if l_level == "-":
        return True, 0.0, 0.0
    s_pct = stock_pfo.get((d, hhmm), 0.0)
    spy_pct = spy_pfo.get((d, hhmm), 0.0)
    sec_pct = sector_pfo.get((d, hhmm), 0.0) if sector_pfo else spy_pct
    rs_spy = s_pct - spy_pct
    rs_sector = s_pct - sec_pct
    if l_level == "L1":
        return rs_spy > 0, rs_spy, rs_sector
    if l_level == "L4":
        return rs_spy > 0 and rs_sector > 0, rs_spy, rs_sector
    return True, rs_spy, rs_sector


# ════════════════════════════════════════════════════════════════
#  Exit mechanics
# ════════════════════════════════════════════════════════════════

def simulate_trade(trade: CTrade, bars: list, bar_idx: int,
                   x_type: str = "X2", ema9: Optional[EMA] = None) -> CTrade:
    risk = trade.risk
    if risk <= 0:
        trade.pnl_rr = 0
        trade.exit_reason = "invalid"
        return trade

    if x_type == "X1":
        target_rr = 2.0
    else:
        target_rr = 3.0

    max_bars = 78
    best_r = 0.0

    for i in range(bar_idx + 1, min(bar_idx + max_bars + 1, len(bars))):
        b = bars[i]
        trade.bars_held += 1
        hhmm = b.timestamp.hour * 100 + b.timestamp.minute

        if ema9 is not None:
            ema9.update(b.close)

        current_r = (b.close - trade.entry_price) / risk
        best_r = max(best_r, current_r)

        if hhmm >= 1555 or (i == len(bars) - 1):
            trade.pnl_rr = (b.close - trade.entry_price) / risk
            trade.exit_reason = "eod"
            return trade

        if b.low <= trade.stop_price:
            trade.pnl_rr = (trade.stop_price - trade.entry_price) / risk
            trade.exit_reason = "stop"
            return trade

        if x_type in ("X1", "X2"):
            target_price = trade.entry_price + target_rr * risk
            if b.high >= target_price:
                trade.pnl_rr = target_rr
                trade.exit_reason = "target"
                return trade

    trade.exit_reason = "eod"
    trade.pnl_rr = 0
    return trade


# ════════════════════════════════════════════════════════════════
#  Cost model
# ════════════════════════════════════════════════════════════════

SLIPPAGE_BPS = 4
COMMISSION_PER_SHARE = 0.005


def apply_costs(trade: CTrade) -> CTrade:
    if trade.risk <= 0:
        return trade
    entry = trade.entry_price
    slip_per_side = entry * SLIPPAGE_BPS / 10000
    total_cost = 2 * (slip_per_side + COMMISSION_PER_SHARE)
    trade.pnl_rr -= total_cost / trade.risk
    return trade


# ════════════════════════════════════════════════════════════════
#  Entry: VK Acceptance (E1) — the only entry under test
# ════════════════════════════════════════════════════════════════

def entry_vk(bars: list, sym: str, spy_ctx: dict,
             open_stats: dict, stock_pfo: dict, spy_pfo: dict, sector_pfo: dict,
             m_level: str = "ML1", p_level: str = "-", l_level: str = "L1",
             x_type: str = "X2",
             time_start: int = 1000, time_end: int = 1130,
             hold_bars: int = 2, min_body_pct: float = 0.40,
             kiss_atr_frac: float = 0.05,
             use_entry: bool = True) -> List[CTrade]:
    """VK acceptance with live-only gates.

    use_entry=False: skip the acceptance mechanic entirely — fire on any
    bullish bar above VWAP that passes other filters. For ablation only.
    """
    trades = []
    ema9 = EMA(9)
    vwap = VWAPCalc()
    vol_buf = deque(maxlen=20)
    vol_ma = NaN
    atr_buf = deque(maxlen=14)
    intra_atr = NaN

    touched = False
    hold_count = 0
    hold_low = NaN
    micro_high = NaN
    triggered_today = None
    prev_date = None

    for i, bar in enumerate(bars):
        e9 = ema9.update(bar.close)
        tp = (bar.high + bar.low + bar.close) / 3.0
        d = bar.timestamp.date()
        hhmm = bar.timestamp.hour * 100 + bar.timestamp.minute

        if d != prev_date:
            vwap.reset()
            touched = False
            hold_count = 0
            hold_low = NaN
            micro_high = NaN
            triggered_today = None
            prev_date = d

        vw = vwap.update(tp, bar.volume)

        if len(vol_buf) == 20:
            vol_ma = sum(vol_buf) / 20
        vol_buf.append(bar.volume)

        tr = bar.high - bar.low
        atr_buf.append(tr)
        if len(atr_buf) >= 5:
            intra_atr = sum(atr_buf) / len(atr_buf)

        if not vwap.ready or _isnan(intra_atr):
            continue

        # Live market gate (NO GREEN DAY)
        if not check_market_live(spy_ctx, d, hhmm, m_level):
            continue
        # In-play gate
        ip_ok, ip_score = check_inplay(open_stats, d, p_level)
        if not ip_ok:
            continue
        if hhmm < time_start or hhmm > time_end:
            continue
        if triggered_today == d:
            continue

        # Leadership gate
        l_ok, rs_spy, rs_sec = check_leadership(stock_pfo, spy_pfo, sector_pfo,
                                                 d, hhmm, l_level)
        if not l_ok:
            continue

        kiss_dist = kiss_atr_frac * intra_atr
        above_vwap = bar.close > vw
        near_vwap = abs(bar.low - vw) <= kiss_dist or bar.low <= vw <= bar.high

        # ── Controlled entry ablation ──
        if not use_entry:
            # No acceptance — just fire on any bullish bar above VWAP
            if above_vwap:
                rng = bar.high - bar.low
                body = abs(bar.close - bar.open)
                is_bull = bar.close > bar.open
                body_pct = body / rng if rng > 0 else 0
                vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 0.70 * vol_ma
                if is_bull and body_pct >= min_body_pct and vol_ok:
                    stop = bar.low - 0.02
                    stop = min(stop, vw - 0.02)
                    risk = bar.close - stop
                    if risk > 0:
                        os = open_stats.get(d, {})
                        t = CTrade(
                            symbol=sym, entry_time=bar.timestamp,
                            entry_price=bar.close, stop_price=stop,
                            target_price=bar.close + 3.0 * risk,
                            setup="VK_NOACCEPT", risk=risk,
                            gap_pct=os.get("gap_pct", 0), rvol=os.get("rvol", 0),
                            rs_spy=rs_spy, rs_sector=rs_sec,
                            market_level=m_level, inplay_score=ip_score,
                        )
                        trail_ema = EMA(9)
                        for j in range(max(0, i - 20), i + 1):
                            trail_ema.update(bars[j].close)
                        t = simulate_trade(t, bars, i, x_type=x_type, ema9=trail_ema)
                        t = apply_costs(t)
                        trades.append(t)
                        triggered_today = d
            continue

        # ── Standard VK acceptance state machine ──
        if near_vwap and above_vwap:
            if not touched:
                touched = True
                hold_count = 1
                hold_low = bar.low
                micro_high = bar.high
            elif hold_count > 0 and hold_count < hold_bars:
                hold_count += 1
                hold_low = min(hold_low, bar.low)
                micro_high = max(micro_high, bar.high)
        elif above_vwap and touched and hold_count > 0:
            if hold_count < hold_bars:
                hold_count += 1
                hold_low = min(hold_low, bar.low)
                micro_high = max(micro_high, bar.high)
        elif not above_vwap:
            touched = False
            hold_count = 0
            hold_low = NaN
            micro_high = NaN

        # Trigger
        if hold_count >= hold_bars and above_vwap and touched:
            rng = bar.high - bar.low
            body = abs(bar.close - bar.open)
            is_bull = bar.close > bar.open
            body_pct = body / rng if rng > 0 else 0
            trigger_ok = is_bull and body_pct >= min_body_pct
            vol_ok = not _isnan(vol_ma) and vol_ma > 0 and bar.volume >= 0.70 * vol_ma

            if trigger_ok and vol_ok:
                stop = (hold_low if not _isnan(hold_low) else bar.low) - 0.02
                stop = min(stop, vw - 0.02)
                risk = bar.close - stop
                if risk > 0:
                    os = open_stats.get(d, {})
                    t = CTrade(
                        symbol=sym, entry_time=bar.timestamp,
                        entry_price=bar.close, stop_price=stop,
                        target_price=bar.close + 3.0 * risk,
                        setup="VK_ACC", risk=risk,
                        gap_pct=os.get("gap_pct", 0), rvol=os.get("rvol", 0),
                        rs_spy=rs_spy, rs_sector=rs_sec,
                        market_level=m_level, inplay_score=ip_score,
                    )
                    trail_ema = EMA(9)
                    for j in range(max(0, i - 20), i + 1):
                        trail_ema.update(bars[j].close)
                    t = simulate_trade(t, bars, i, x_type=x_type, ema9=trail_ema)
                    t = apply_costs(t)
                    trades.append(t)
                    triggered_today = d
                    touched = False
                    hold_count = 0
                    hold_low = NaN
                    micro_high = NaN

    return trades


# ════════════════════════════════════════════════════════════════
#  Metrics
# ════════════════════════════════════════════════════════════════

def compute_r_metrics(trades: List[CTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "pf_r": 0, "exp_r": 0, "total_r": 0, "max_dd_r": 0,
                "wr": 0, "stop_rate": 0, "avg_win": 0, "avg_loss": 0,
                "median_r": 0, "target_rate": 0}

    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]
    gw = sum(t.pnl_rr for t in wins)
    gl = abs(sum(t.pnl_rr for t in losses))
    pf = gw / gl if gl > 0 else float("inf")
    total_r = sum(t.pnl_rr for t in trades)
    wr = len(wins) / n * 100
    stopped = sum(1 for t in trades if t.exit_reason == "stop")
    targets = sum(1 for t in trades if t.exit_reason == "target")
    avg_win = gw / len(wins) if wins else 0
    avg_loss = -gl / len(losses) if losses else 0

    all_rr = sorted([t.pnl_rr for t in trades])
    median_r = all_rr[n // 2] if n else 0

    cum = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: t.entry_time):
        cum += t.pnl_rr
        if cum > pk:
            pk = cum
        if pk - cum > dd:
            dd = pk - cum

    return {"n": n, "pf_r": pf, "exp_r": total_r / n, "total_r": total_r,
            "max_dd_r": dd, "wr": wr, "stop_rate": stopped / n * 100,
            "avg_win": avg_win, "avg_loss": avg_loss, "median_r": median_r,
            "target_rate": targets / n * 100}


def robustness(trades: List[CTrade]) -> dict:
    daily_r = defaultdict(float)
    sym_r = defaultdict(float)
    for t in trades:
        daily_r[t.entry_date] += t.pnl_rr
        sym_r[t.symbol] += t.pnl_rr

    best_day = max(daily_r, key=daily_r.get) if daily_r else None
    top_sym = max(sym_r, key=sym_r.get) if sym_r else None

    ex_day = [t for t in trades if t.entry_date != best_day] if best_day else trades
    ex_sym = [t for t in trades if t.symbol != top_sym] if top_sym else trades
    ex_day_m = compute_r_metrics(ex_day)
    ex_sym_m = compute_r_metrics(ex_sym)

    train = [t for t in trades if t.entry_date.day % 2 == 1]
    test = [t for t in trades if t.entry_date.day % 2 == 0]
    tr_m = compute_r_metrics(train)
    te_m = compute_r_metrics(test)

    monthly = defaultdict(float)
    for t in trades:
        monthly[t.entry_date.strftime("%Y-%m")] += t.pnl_rr
    months_pos = sum(1 for v in monthly.values() if v > 0)

    return {
        "ex_best_day_pf": ex_day_m["pf_r"],
        "ex_top_sym_pf": ex_sym_m["pf_r"],
        "train_pf": tr_m["pf_r"], "test_pf": te_m["pf_r"],
        "train_n": tr_m["n"], "test_n": te_m["n"],
        "months_pos": months_pos, "months_total": len(monthly),
        "stable": (tr_m["pf_r"] >= 0.80 and te_m["pf_r"] >= 0.80
                   and tr_m["n"] >= 10 and te_m["n"] >= 10),
    }


def pf_str(pf):
    return f"{pf:.2f}" if pf < 999 else "inf"


# ════════════════════════════════════════════════════════════════
#  Variant runner
# ════════════════════════════════════════════════════════════════

def run_variant(symbols: list, spy_ctx: dict,
                all_open_stats: dict, spy_pfo: dict, sector_pfos: dict,
                all_bars: dict,
                m_level: str = "ML1", p_level: str = "-",
                l_level: str = "L1", x_type: str = "X2",
                use_entry: bool = True) -> List[CTrade]:
    all_trades = []
    for sym in symbols:
        bars = all_bars.get(sym)
        if not bars:
            continue
        open_stats = all_open_stats.get(sym, {})
        stock_pfo = build_pct_from_open(bars)
        sec_etf = get_sector_etf(sym)
        sector_pfo = sector_pfos.get(sec_etf, spy_pfo)

        trades = entry_vk(
            bars, sym, spy_ctx,
            open_stats, stock_pfo, spy_pfo, sector_pfo,
            m_level=m_level, p_level=p_level, l_level=l_level,
            x_type=x_type, use_entry=use_entry,
        )
        all_trades.extend(trades)
    return all_trades


# ════════════════════════════════════════════════════════════════
#  Section 1: Market gate sweep (find best live candidate)
# ════════════════════════════════════════════════════════════════

def section1_market_sweep(symbols, spy_ctx, all_open_stats, spy_pfo, sector_pfos, all_bars):
    print(f"\n{'='*140}")
    print("SECTION 1 — LIVE MARKET GATE SWEEP")
    print(f"{'='*140}")
    print("  Entry: VK acceptance (hold=2, expansion close)")
    print("  Leadership: L1 (RS > SPY)")
    print("  In-play: none (test separately)")
    print("  Exit: X2 (3.0R target)")
    print("  Cost: 4bps/side + $0.005/share")

    header = (f"\n  {'Variant':28s} {'N':>5s} {'WR%':>5s} {'PF(R)':>6s} {'Exp(R)':>8s} "
              f"{'TotalR':>8s} {'MaxDD':>7s} {'Stop%':>6s} "
              f"{'TrnPF':>6s} {'TstPF':>6s} {'Stbl':>4s} {'Mo+':>5s}")
    print(header)
    sep = (f"  {'-'*28} {'-'*5} {'-'*5} {'-'*6} {'-'*8} "
           f"{'-'*8} {'-'*7} {'-'*6} "
           f"{'-'*6} {'-'*6} {'-'*4} {'-'*5}")
    print(sep)

    best = None
    for ml in ["ML0", "ML1", "ML2", "ML3"]:
        trades = run_variant(symbols, spy_ctx, all_open_stats, spy_pfo, sector_pfos,
                             all_bars, m_level=ml, l_level="L1")
        m = compute_r_metrics(trades)
        rob = robustness(trades) if m["n"] >= 10 else {
            "train_pf": 0, "test_pf": 0, "stable": False,
            "months_pos": 0, "months_total": 0}
        mo = f"{rob.get('months_pos',0)}/{rob.get('months_total',0)}"
        label = f"VK_acc+L1+{ml}"
        print(f"  {label:28s} {m['n']:5d} {m['wr']:5.1f} {pf_str(m['pf_r']):>6s} {m['exp_r']:+7.3f}R "
              f"{m['total_r']:+7.2f}R {m['max_dd_r']:6.2f}R {m['stop_rate']:5.1f}% "
              f"{pf_str(rob.get('train_pf',0)):>6s} {pf_str(rob.get('test_pf',0)):>6s} "
              f"{'YES' if rob.get('stable') else ' NO':>4s} {mo:>5s}")

        if m["n"] >= 20 and (best is None or m["pf_r"] > best[1]["pf_r"]):
            best = (ml, m, rob, trades)

    if best:
        print(f"\n  >>> BEST LIVE CANDIDATE: VK_acc+L1+{best[0]}  "
              f"PF={pf_str(best[1]['pf_r'])}  Exp={best[1]['exp_r']:+.3f}R  N={best[1]['n']}")
    return best


# ════════════════════════════════════════════════════════════════
#  Section 2: Mandatory ablations
# ════════════════════════════════════════════════════════════════

def section2_ablations(symbols, spy_ctx, all_open_stats, spy_pfo, sector_pfos, all_bars,
                       best_ml: str, best_m: dict):
    print(f"\n{'='*140}")
    print(f"SECTION 2 — MANDATORY ABLATIONS (base: VK_acc+L1+{best_ml})")
    print(f"{'='*140}")
    print("  Remove each filter layer one at a time. Same exit (X2).")

    ablations = [
        ("FULL (baseline)",        {"m_level": best_ml, "l_level": "L1", "use_entry": True}),
        ("Remove market support",  {"m_level": "ML0",   "l_level": "L1", "use_entry": True}),
        ("Remove leadership",      {"m_level": best_ml, "l_level": "-",  "use_entry": True}),
        ("Remove controlled entry",{"m_level": best_ml, "l_level": "L1", "use_entry": False}),
    ]

    header = (f"\n  {'Configuration':30s} {'N':>5s} {'WR%':>5s} {'PF(R)':>6s} {'Exp(R)':>8s} "
              f"{'TotalR':>8s} {'MaxDD':>7s} {'Stop%':>6s} {'dPF':>6s} {'dExp':>7s}")
    print(header)
    sep = (f"  {'-'*30} {'-'*5} {'-'*5} {'-'*6} {'-'*8} "
           f"{'-'*8} {'-'*7} {'-'*6} {'-'*6} {'-'*7}")
    print(sep)

    base_pf = best_m["pf_r"]
    base_exp = best_m["exp_r"]

    for desc, kwargs in ablations:
        trades = run_variant(symbols, spy_ctx, all_open_stats, spy_pfo, sector_pfos,
                             all_bars, x_type="X2", **kwargs)
        m = compute_r_metrics(trades)
        dpf = m["pf_r"] - base_pf
        dexp = m["exp_r"] - base_exp
        dpf_s = f"{dpf:+5.2f}" if desc != "FULL (baseline)" else "  —"
        dexp_s = f"{dexp:+6.3f}" if desc != "FULL (baseline)" else "    —"
        print(f"  {desc:30s} {m['n']:5d} {m['wr']:5.1f} {pf_str(m['pf_r']):>6s} {m['exp_r']:+7.3f}R "
              f"{m['total_r']:+7.2f}R {m['max_dd_r']:6.2f}R {m['stop_rate']:5.1f}% "
              f"{dpf_s:>6s} {dexp_s:>7s}")


# ════════════════════════════════════════════════════════════════
#  Section 3: In-play soft gate sweep
# ════════════════════════════════════════════════════════════════

def section3_inplay_sweep(symbols, spy_ctx, all_open_stats, spy_pfo, sector_pfos, all_bars,
                          best_ml: str):
    print(f"\n{'='*140}")
    print(f"SECTION 3 — IN-PLAY SOFT GATE SWEEP (base: VK_acc+L1+{best_ml})")
    print(f"{'='*140}")
    print("  Score = gap>1% + RVOL>2 + dolVol>$5M (0-3)")

    header = (f"\n  {'In-play gate':28s} {'N':>5s} {'WR%':>5s} {'PF(R)':>6s} {'Exp(R)':>8s} "
              f"{'TotalR':>8s} {'MaxDD':>7s} {'Stop%':>6s} "
              f"{'TrnPF':>6s} {'TstPF':>6s}")
    print(header)
    sep = (f"  {'-'*28} {'-'*5} {'-'*5} {'-'*6} {'-'*8} "
           f"{'-'*8} {'-'*7} {'-'*6} "
           f"{'-'*6} {'-'*6}")
    print(sep)

    for plabel, plevel in [("No requirement", "-"),
                           ("Loose (1-of-3)", "P_LOOSE"),
                           ("Medium (2-of-3)", "P_MED"),
                           ("Strict (3-of-3)", "P_STRICT")]:
        trades = run_variant(symbols, spy_ctx, all_open_stats, spy_pfo, sector_pfos,
                             all_bars, m_level=best_ml, p_level=plevel, l_level="L1")
        m = compute_r_metrics(trades)
        rob = robustness(trades) if m["n"] >= 10 else {"train_pf": 0, "test_pf": 0}
        print(f"  {plabel:28s} {m['n']:5d} {m['wr']:5.1f} {pf_str(m['pf_r']):>6s} {m['exp_r']:+7.3f}R "
              f"{m['total_r']:+7.2f}R {m['max_dd_r']:6.2f}R {m['stop_rate']:5.1f}% "
              f"{pf_str(rob.get('train_pf',0)):>6s} {pf_str(rob.get('test_pf',0)):>6s}")


# ════════════════════════════════════════════════════════════════
#  Section 4: Deployability stats
# ════════════════════════════════════════════════════════════════

def section4_deployability(trades: List[CTrade], label: str):
    print(f"\n{'='*140}")
    print(f"SECTION 4 — DEPLOYABILITY STATS ({label})")
    print(f"{'='*140}")

    if not trades:
        print("  No trades.")
        return

    # Trades per day
    daily_counts = defaultdict(int)
    daily_r = defaultdict(float)
    all_dates = set()
    for t in trades:
        daily_counts[t.entry_date] += 1
        daily_r[t.entry_date] += t.pnl_rr
        all_dates.add(t.entry_date)

    # Get total trading days from any symbol's bar data
    # Use all unique dates that appear in any bar series
    all_trading_dates = set()
    for t in trades:
        all_trading_dates.add(t.entry_date)
    # But also count days with zero trades — need bar data for that
    # Approximate: use SPY days
    counts = sorted(daily_counts.values())
    zero_days = len(all_trading_dates) - len(daily_counts)  # This is 0 by construction
    # Better: count from total possible trading days
    n_trades = len(trades)
    n_days = len(daily_counts)
    total_r = sum(t.pnl_rr for t in trades)
    avg_trades_day = n_trades / n_days if n_days > 0 else 0
    median_count = counts[len(counts) // 2] if counts else 0

    print(f"\n  Trading frequency:")
    print(f"    Total trades: {n_trades}")
    print(f"    Days with trades: {n_days}")
    print(f"    Avg trades/day: {avg_trades_day:.1f}")
    print(f"    Median trades/day: {median_count}")
    print(f"    Min/Max trades/day: {counts[0] if counts else 0} / {counts[-1] if counts else 0}")

    # Daily R distribution
    daily_rs = sorted(daily_r.values())
    pos_days = sum(1 for v in daily_rs if v > 0)
    median_daily_r = daily_rs[len(daily_rs) // 2] if daily_rs else 0
    print(f"\n  Daily R distribution:")
    print(f"    Positive days: {pos_days}/{n_days} ({pos_days/n_days*100:.1f}%)")
    print(f"    Median daily R: {median_daily_r:+.3f}R")
    print(f"    Avg daily R: {total_r/n_days:+.3f}R")

    # Time-of-day buckets
    time_buckets = defaultdict(list)
    for t in trades:
        h = t.entry_time.hour
        m = t.entry_time.minute
        hhmm = h * 100 + m
        if hhmm < 1015:
            bucket = "10:00-10:14"
        elif hhmm < 1030:
            bucket = "10:15-10:29"
        elif hhmm < 1045:
            bucket = "10:30-10:44"
        elif hhmm < 1100:
            bucket = "10:45-10:59"
        elif hhmm < 1115:
            bucket = "11:00-11:14"
        else:
            bucket = "11:15-11:30"
        time_buckets[bucket].append(t.pnl_rr)

    print(f"\n  Time-of-day:")
    print(f"    {'Bucket':16s} {'N':>5s} {'WR%':>5s} {'Exp(R)':>8s} {'TotalR':>8s}")
    print(f"    {'-'*16} {'-'*5} {'-'*5} {'-'*8} {'-'*8}")
    for bucket in sorted(time_buckets.keys()):
        rs = time_buckets[bucket]
        bn = len(rs)
        bwr = sum(1 for r in rs if r > 0) / bn * 100 if bn else 0
        bexp = sum(rs) / bn if bn else 0
        btot = sum(rs)
        print(f"    {bucket:16s} {bn:5d} {bwr:5.1f} {bexp:+7.3f}R {btot:+7.2f}R")

    # Symbol concentration
    sym_r = defaultdict(float)
    sym_n = defaultdict(int)
    for t in trades:
        sym_r[t.symbol] += t.pnl_rr
        sym_n[t.symbol] += 1

    top3_syms = sorted(sym_r.items(), key=lambda x: x[1], reverse=True)[:3]
    top3_r = sum(v for _, v in top3_syms)
    ex_top_sym = sorted(sym_r.items(), key=lambda x: x[1], reverse=True)
    ex_top_pf = 0
    if len(ex_top_sym) > 1:
        ex_trades = [t for t in trades if t.symbol != ex_top_sym[0][0]]
        ex_m = compute_r_metrics(ex_trades)
        ex_top_pf = ex_m["pf_r"]

    print(f"\n  Symbol concentration:")
    print(f"    Top-3 symbols by R:")
    for s, v in top3_syms:
        print(f"      {s:8s} {v:+7.2f}R  ({sym_n[s]} trades)")
    print(f"    Top-3 contribution: {top3_r:+.2f}R / {total_r:+.2f}R "
          f"({top3_r/total_r*100:.1f}%)" if total_r != 0 else "")
    print(f"    Ex-top-symbol PF: {pf_str(ex_top_pf)}")

    # Day concentration
    top3_days = sorted(daily_r.items(), key=lambda x: x[1], reverse=True)[:3]
    top3_day_r = sum(v for _, v in top3_days)
    print(f"\n  Day concentration:")
    print(f"    Top-3 days by R:")
    for d, v in top3_days:
        print(f"      {d}  {v:+7.2f}R  ({daily_counts[d]} trades)")
    print(f"    Top-3 contribution: {top3_day_r:+.2f}R / {total_r:+.2f}R "
          f"({top3_day_r/total_r*100:.1f}%)" if total_r != 0 else "")


# ════════════════════════════════════════════════════════════════
#  Section 5: PF/Expectancy decomposition
# ════════════════════════════════════════════════════════════════

def section5_decomposition(trades: List[CTrade], label: str):
    print(f"\n{'='*140}")
    print(f"SECTION 5 — PF/EXPECTANCY DECOMPOSITION ({label})")
    print(f"{'='*140}")

    if not trades:
        print("  No trades.")
        return

    n = len(trades)
    wins = [t for t in trades if t.pnl_rr > 0]
    losses = [t for t in trades if t.pnl_rr <= 0]

    wr = len(wins) / n * 100
    avg_win = sum(t.pnl_rr for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnl_rr for t in losses) / len(losses) if losses else 0

    all_rr = sorted([t.pnl_rr for t in trades])
    median_r = all_rr[n // 2]
    p25 = all_rr[n // 4]
    p75 = all_rr[3 * n // 4]

    # Skew: (mean - median) / std
    mean_r = sum(all_rr) / n
    var = sum((r - mean_r) ** 2 for r in all_rr) / n
    std = var ** 0.5
    skew = (mean_r - median_r) / std if std > 0 else 0

    # Tail contribution: what % of total R comes from top 10% of trades
    top_10pct = sorted(all_rr, reverse=True)[:max(1, n // 10)]
    top_10_r = sum(top_10pct)
    total_r = sum(all_rr)

    # Exit breakdown
    exit_counts = defaultdict(int)
    exit_r = defaultdict(float)
    for t in trades:
        exit_counts[t.exit_reason] += 1
        exit_r[t.exit_reason] += t.pnl_rr

    print(f"\n  Core metrics:")
    print(f"    N = {n}")
    print(f"    Win rate: {wr:.1f}%")
    print(f"    Avg winner: {avg_win:+.3f}R")
    print(f"    Avg loser: {avg_loss:+.3f}R")
    print(f"    Avg win / |avg loss|: {abs(avg_win/avg_loss):.2f}" if avg_loss != 0 else "")

    print(f"\n  Distribution:")
    print(f"    Mean R/trade: {mean_r:+.3f}R")
    print(f"    Median R/trade: {median_r:+.3f}R")
    print(f"    P25 / P75: {p25:+.3f}R / {p75:+.3f}R")
    print(f"    Std dev: {std:.3f}R")
    print(f"    Skew (mean-median)/std: {skew:+.3f}")

    print(f"\n  Tail contribution:")
    print(f"    Top 10% of trades ({len(top_10pct)}): {top_10_r:+.2f}R "
          f"({top_10_r/total_r*100:.1f}% of total)" if total_r != 0 else "")

    print(f"\n  Exit breakdown:")
    print(f"    {'Reason':10s} {'N':>5s} {'%':>6s} {'TotalR':>8s} {'AvgR':>7s}")
    print(f"    {'-'*10} {'-'*5} {'-'*6} {'-'*8} {'-'*7}")
    for reason in ["target", "stop", "eod", "trail", "time"]:
        if exit_counts[reason] > 0:
            en = exit_counts[reason]
            er = exit_r[reason]
            print(f"    {reason:10s} {en:5d} {en/n*100:5.1f}% {er:+7.2f}R {er/en:+6.3f}R")

    # SC Q>=5 baseline comparison
    print(f"\n  vs SC Q>=5 baseline (PF 1.19, 49 trades, Exp +0.173R):")
    m = compute_r_metrics(trades)
    print(f"    This candidate: PF {pf_str(m['pf_r'])}, {m['n']} trades, Exp {m['exp_r']:+.3f}R")
    if m["pf_r"] > 1.19:
        print(f"    PF delta: +{m['pf_r']-1.19:.2f} (BETTER)")
    else:
        print(f"    PF delta: {m['pf_r']-1.19:+.2f} (WORSE)")
    if m["exp_r"] > 0.173:
        print(f"    Exp delta: +{m['exp_r']-0.173:.3f}R (BETTER)")
    else:
        print(f"    Exp delta: {m['exp_r']-0.173:+.3f}R (WORSE)")
    print(f"    Trade count: {m['n']} vs 49 ({m['n']/49:.1f}x)")


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    print("=" * 140)
    print("COMPOSITE LONG ABLATION — LIVE-AVAILABLE ONLY")
    print("=" * 140)
    print("  CORRECTION: All prior variants used GREEN-day (end-of-day SPY return) = HINDSIGHT.")
    print("  This study uses ONLY real-time market gates. No future information.")

    print("\n  Loading data...")
    symbols = get_universe()
    spy_bars = load_bars("SPY")
    spy_ctx = build_spy_snapshots(spy_bars)
    spy_pfo = build_pct_from_open(spy_bars)

    sector_pfos = {}
    for etf in set(SECTOR_MAP.values()) - {"SPY", "QQQ", "IWM"}:
        p = DATA_DIR / f"{etf}_5min.csv"
        if p.exists():
            sb = load_bars_from_csv(str(p))
            sector_pfos[etf] = build_pct_from_open(sb)

    print(f"  Loading {len(symbols)} symbols...")
    all_bars = {}
    all_open_stats = {}
    for sym in symbols:
        bars = load_bars(sym)
        if bars:
            all_bars[sym] = bars
            all_open_stats[sym] = compute_open_stats(bars)

    print(f"  Data: {spy_bars[0].timestamp.date()} -> {spy_bars[-1].timestamp.date()}")
    print(f"  Universe: {len(all_bars)} symbols")
    print(f"  Cost: 4bps/side + $0.005/share")

    # ── Section 1: Find best live candidate ──
    best = section1_market_sweep(symbols, spy_ctx, all_open_stats, spy_pfo, sector_pfos, all_bars)
    if not best:
        print("\n  ERROR: No viable live candidate found.")
        return

    best_ml, best_m, best_rob, best_trades = best

    # ── Section 2: Ablations ──
    section2_ablations(symbols, spy_ctx, all_open_stats, spy_pfo, sector_pfos, all_bars,
                       best_ml, best_m)

    # ── Section 3: In-play soft gate ──
    section3_inplay_sweep(symbols, spy_ctx, all_open_stats, spy_pfo, sector_pfos, all_bars,
                          best_ml)

    # ── Section 4: Deployability ──
    label = f"VK_acc+L1+{best_ml}"
    section4_deployability(best_trades, label)

    # ── Section 5: Decomposition ──
    section5_decomposition(best_trades, label)

    print(f"\n{'='*140}")
    print("ABLATION STUDY COMPLETE")
    print(f"{'='*140}")


if __name__ == "__main__":
    main()
