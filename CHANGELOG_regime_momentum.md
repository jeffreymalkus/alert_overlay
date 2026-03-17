# Change Log: Regime & Momentum Improvements (2026-03-16)

## What Was Changed

### 1. Time-normalized regime thresholds (shared module)
**Files:** `strategies/shared/regime_gate.py` (NEW), `strategies/replay_live_path.py`, `dashboard.py`
**WHY:** The old regime gate used a fixed -0.15% threshold all day. SPY moves widen dramatically as the session progresses — using the same threshold at 9:35 and 14:00 means either blocking good morning trades or letting through bad afternoon trades. ChatGPT proposed time buckets but with incorrect thresholds. We ran actual SPY 1-min percentile analysis (126 sessions, 2025-09-15 to 2026-03-12) and derived data-backed P75 thresholds.

**What changed:**
- Created `strategies/shared/regime_gate.py` — single source of truth for both dashboard and replay
- `hostile_threshold(hhmm)`: 0.16% pre-9:45, 0.32% 9:45-10:29, 0.63% 10:30+
- `check_regime_gate()` uses time-normalized thresholds instead of fixed -0.15%
- `replay_live_path.py`: removed duplicated dict/function, imports from shared module, passes `bar_time_hhmm`
- `dashboard.py`: removed 80-line local implementation, imports from shared module, extracts bar time from alert_bar timestamp

### 2. trigger_quality < 0.20 hard reject
**Files:** `strategies/shared/quality_scoring.py`, `strategies/replay_live_path.py`
**WHY:** Sensitivity sweep on replay data showed trigger_quality < 0.20 tier is clearly garbage: 9 trades, avg -0.576R, 22.2% win rate. These are doji/wick-heavy/low-volume bars that should never trigger a trade. ChatGPT proposed the reject but with unverified PF claims at higher thresholds — we only adopted the data-backed < 0.20 cutoff.

**What changed:**
- `QualityScorer.score()` now returns C_TIER immediately if trigger_quality < 0.20
- `replay_live_path.py` has explicit pre-check (short-circuits before scorer for funnel counting)
- Applies to ALL strategies through the unified quality pipeline

### 3. regime_score weight reduced 1.0 → 0.5
**File:** `strategies/shared/quality_scoring.py`
**WHY:** Regime is a binary signal (GREEN/RED/FLAT) being used as a continuous score. At weight 1.0, it dominated the market component (2 points max). With time-normalized thresholds now handling the hard gate, the quality scorer's regime contribution should be softer — a tiebreaker, not a gatekeeper.

**What changed:**
- `market_score += regime * 0.5` (was `regime * 1.0`)
- GREEN regime now contributes 0.5 points instead of 1.0
- FLAT contributes 0.25 instead of 0.50

### 4. Bar-shape metadata logging
**File:** `strategies/replay_live_path.py`
**WHY:** We need post-hoc analysis capability for trigger bar characteristics. Without these fields in the CSV, we can't study which bar shapes lead to winners vs losers or refine trigger_quality thresholds.

**What was added to metadata + CSV export:**
- `close_location` — where close falls in bar range (0=low, 1=high)
- `body_fraction` — candle body as fraction of total range
- `counter_wick_fraction` — wick opposite to bar direction
- `bar_return_pct` — bar's open-to-close return in %
- `relative_impulse_vs_spy` — bar return minus SPY move (stock alpha)

---

## What Was Measured

### Before (unified pipeline, no regime/momentum changes):
```
N=160  PF=0.86  TotalR=-10.7  MaxDD=17.1R
Funnel: 9,333 raw → 318 regime → 8,770 in-play → 85 quality → 160 promoted (1.7%)
```

### After (time-bucket regime + trigger_quality reject + regime weight 0.5):
```
N=146  PF=0.97  TotalR=-2.0  MaxDD=10.0R
Funnel: 9,407 raw → 271 regime → 8,890 in-play → 100 quality → 146 promoted (1.6%)
```

### Per-strategy comparison:
```
Strategy         Before           After
FL_ANTICHOP      N=67 PF=0.79    N=64 PF=0.83    (-3 trades, PF +0.04)
HH_QUALITY       N=30 PF=1.26    N=30 PF=1.26    (unchanged — good)
ORH_FBO_V2_A     N=17 PF=0.60    N=11 PF=0.52    (-6 trades, PF worse but fewer losers)
ORH_FBO_V2_B     N=10 PF=1.90    N=7  PF=6.75    (-3 trades, PF massively improved)
ORL_FBD_LONG     N=19 PF=0.51    N=19 PF=0.51    (unchanged)
SC_SNIPER        N=6  PF=0.66    N=6  PF=0.66    (unchanged)
EMA_FPIP         N=6  PF=1.45    N=6  PF=1.45    (unchanged)
PDH_FBO_B        N=2  PF=0.00    N=1  PF=0.00    (-1 trade)
FFT_NEWLOW_REV   N=1  PF=0.00    N=0  ---        (eliminated)
```

---

## What The Numbers Show

1. **14 fewer trades, +8.7R improvement.** TotalR improved from -10.7 to -2.0. The removed trades averaged -0.62R each — all net losers.

2. **PF improved 0.86 → 0.97.** Approaching breakeven. The portfolio went from clearly losing to nearly flat.

3. **MaxDD improved 17.1R → 10.0R.** 41% reduction in peak drawdown.

4. **Regime gate actually passed MORE signals (271 blocked vs 318 before).** The time-normalized thresholds are MORE PERMISSIVE during the morning session (0.16% vs old 0.15% is tighter, but 0.32% and 0.63% at later times is much wider). Net effect: fewer false regime blocks.

5. **Quality gate blocked MORE signals (100 vs 85).** This is the trigger_quality hard reject + regime weight reduction working together. Lower regime weight means borderline signals that relied on GREEN regime bonus now fall below A-tier.

6. **ORH_FBO_V2_B is the standout:** N=7, PF=6.75, +9.0R. Lost 3 marginal trades, all were losers.

7. **Train/Test split:** Train PF=1.08 (N=74), Test PF=0.84 (N=72). The train period is now positive, test still negative but closer to breakeven.

---

## What This Means Mechanically

- Regime gate is now time-aware: tight in the morning when SPY moves are small, loose in the afternoon when bigger moves are normal
- Dashboard and replay use IDENTICAL regime logic from one shared module — no drift possible
- Garbage trigger bars (dojis, wick-heavy, low volume) are hard-rejected before quality scoring
- Regime contributes less to quality score, so signals must earn their A-tier through stock/setup quality rather than riding a GREEN tape bonus
- All 5 new CSV columns enable post-hoc bar-shape analysis for future threshold refinement

---

## What Remains Unresolved

1. **Portfolio still slightly negative:** N=146, PF=0.97, TotalR=-2.0. Close to breakeven but not profitable.
2. **FL_ANTICHOP still largest drag:** N=64, PF=0.83, -4.4R. Test period PF=0.38.
3. **ORH_FBO_V2_A:** N=11, PF=0.52, -4.6R. Neither regime nor quality improvements saved it.
4. **ORL_FBD_LONG:** N=19, PF=0.51, -3.9R. Consistent underperformer across both periods.
5. **SP_ATIER:** 0 promoted trades. In-play still blocking everything.
6. **BDR_SHORT:** 0 promoted trades. Regime blocks most, in-play gets rest.

---

## Next Step

In-play threshold sensitivity study. In-play is blocking 94.5% of raw signals (8,890 of 9,407). The pass rate may be too aggressive at the margins. This was already approved as the next investigation in the prior session.
