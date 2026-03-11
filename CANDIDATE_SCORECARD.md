# Candidate Scorecard — Post-VR Setup Scan

**Date:** 2026-03-09
**Context:** VR's raw edge (PF 1.12) does not survive realistic slippage (cost PF 0.85). This scan identified replacements.

---

## Executive Summary

Three setups survive realistic IBKR friction (8 bps round-trip slippage + commission):

| # | Setup | Dir | Trades | Raw PF | Raw TotalR | Cost PF | Cost TotalR | Cost MaxDD | Gate |
|---|-------|-----|--------|--------|------------|---------|-------------|------------|------|
| 1 | **EMA PULL (early)** | SHORT | 72 | 2.47 | +66.12R | **1.45** | **+30.97R** | 15.53R | time <14:00 |
| 2 | **BDR SHORT** | SHORT | 42 | 2.11 | +15.95R | **1.39** | **+7.50R** | 6.04R | require_regime |
| 3 | **2ND CHANCE** | LONG | 49 | 2.25 | +19.49R | **1.19** | **+4.82R** | 7.35R | quality≥5 |

**Combined 3-setup portfolio (cost-on): PF 1.38, +43.29R, 163 trades, MaxDD 15.53R, Recovery 2.8x**

---

## Methodology

1. Ran ALL 16 setup families through engine with all gates OFF + zero slippage (raw scan)
2. Repeated with realistic 8-bps dynamic slippage + commission (cost-on scan)
3. Ranked by raw PF, filtered for ≥20 trades
4. Deep-dived top candidates: isolated single-setup, gate exploration, train/test split
5. Built combined portfolio from survivors

---

## Detailed Results per Candidate

### 1. EMA PULL Short (early, <14:00)

| Metric | Raw | Cost-on |
|--------|-----|---------|
| Trades | 72 | 72 |
| PF(R) | 2.47 | **1.45** |
| TotalR | +66.12 | **+30.97** |
| Exp(R) | +0.918 | +0.430 |
| Win Rate | 34.7% | 33.3% |
| Avg Win R | +4.45 | +4.16 |
| Avg Loss R | -0.96 | -1.44 |
| MaxDD(R) | 10.00 | 15.53 |
| Stop Rate | 61.1% | 61.1% |
| Quick Stop | 13.9% | 13.9% |
| Target Rate | 15.3% | 15.3% |

**Train/Test (cost):** Train PF 1.10 (+3.67R) / Test PF 1.88 (+27.30R) — both positive

**Edge character:** Low win rate but massive winners (avg win +4.16R). Stops often (-1R each) but the 15% that hit target average +5.75R. Very momentum-driven — catches short-side breakdowns.

**Key time distribution (raw):** Best at 10:00-10:59 (PF 8.15) and 12:00-12:59 (PF 3.31). PM edge degrades.

**Caveat:** Requires `show_trend_setups=True` which also enables VWAP KISS. Need either a dedicated config toggle or post-filter. 61% stop rate means high friction — only viable in early session.

### 2. BDR SHORT + Regime Gate

| Metric | Raw | Cost-on |
|--------|-----|---------|
| Trades | 42 | 42 |
| PF(R) | 2.11 | **1.39** |
| TotalR | +15.95 | **+7.50** |
| Exp(R) | +0.380 | +0.179 |
| Win Rate | 52.4% | 47.6% |
| Avg Win R | +1.38 | +1.34 |
| Avg Loss R | -0.72 | -0.87 |
| MaxDD(R) | 3.09 | 6.04 |
| Stop Rate | 26.2% | 26.2% |
| Quick Stop | 7.1% | 7.1% |
| Time Stop Rate | 73.8% | 73.8% |

**Train/Test (cost):** Train PF 0.86 (-1.81R) / Test PF 2.53 (+9.31R) — train marginal, test strong

**Edge character:** High win rate (48%), time-stop exit mode. The regime gate filters out low-quality tape environments, concentrating trades in RED+TREND conditions. Without regime: raw PF drops to 1.11 (360 trades) and cost-on fails (PF 0.64).

**All trades fire 10:00-10:59** (AM-only cutoff at 11:00).

**Caveat:** Train PF 0.86 is below 1.0. Edge may be unstable in some regimes. Only 42 trades limits statistical confidence.

### 3. 2ND CHANCE Long + Quality≥5

| Metric | Raw | Cost-on |
|--------|-----|---------|
| Trades | 49 | 49 |
| PF(R) | 2.25 | **1.19** |
| TotalR | +19.49 | **+4.82** |
| Exp(R) | +0.398 | +0.098 |
| Win Rate | 42.9% | 36.7% |
| Avg Win R | +1.67 | +1.69 |
| Avg Loss R | -0.56 | -0.83 |
| MaxDD(R) | 3.51 | 7.35 |
| Stop Rate | 16.3% | 16.3% |
| Quick Stop | 8.2% | 8.2% |

**Train/Test (cost):** Train PF 1.05 (+0.64R) / Test PF 1.37 (+4.18R) — both positive

**Edge character:** EMA9 trail exit (67% of exits), small losses (-0.83R avg), occasional big time-stop winners (+3.11R avg). Quality gate filters out noisy low-conviction setups. Best time window: 10:00-10:59 (17 trades, PF 4.86).

**Caveat:** 49 trades is modest. Friction per trade (0.299R) eats most of the raw edge. Thin margin of safety.

---

## Rejected Candidates

| Setup | Raw PF | Cost PF | N | Reason |
|-------|--------|---------|---|--------|
| VR long (any gate config) | 1.12 | 0.85 | 1072 | Edge too thin for costs |
| VKA long | 1.02 | 0.71 | 2082 | Near-random raw, destroyed by friction |
| MCS long | 0.98 | 0.54 | 744 | Negative raw edge, 58% stop rate |
| EMA PULL short (PM) | 1.55 | 0.83 | 170 | PM friction destroys edge |
| BDR SHORT (no regime) | 1.11 | 0.64 | 360 | Raw edge too thin |
| VWAP KISS long | 0.93 | 0.51 | 756 | Negative raw, high stop rate |
| FAIL BOUNCE short | 0.83 | 0.49 | 218 | Negative raw |
| 9EMA RETEST | 0.98 | 0.42 | 14998 | Massive volume, no edge |

---

## Combined Portfolio Performance

**3-Setup Combined (cost-on): SC(q5) + BDR(regime) + EP-short(early)**

| Metric | Value |
|--------|-------|
| Total Trades | 163 |
| PF(R) | **1.38** |
| TotalR | **+43.29R** |
| Exp(R) | +0.266 |
| MaxDD(R) | 15.53R |
| Recovery Ratio | 2.8x |

**Train/Test (cost):** Train PF 1.04 (+2.50R) / Test PF 1.84 (+40.79R)

**Equity curve:** Initial drawdown (first 40 trades, -5.56R), recovery by trade 50, steady accumulation to +48.63R peak, slight pullback to +43.29R final.

---

## CRITICAL: Engine Per-Setup Gating Problem

**The engine applies gates globally.** Running all 3 setups in a single engine pass produces degraded results because:

- **BDR needs `require_regime=True`** — without it, 360 trades at PF 0.64 (destroyed)
- **SC needs `require_regime=False`** — with regime, 29 trades at PF 0.68 (destroyed)
- **EMA PULL needs `require_regime=False`** — regime suppresses trade count to ~3

**Integrated engine test (all 3 together, regime=True):** PF 0.78, -11.42R — FAILS.
**With regime=False + quality=5:** BDR balloons to 360 garbage trades — FAILS.

### The solution: Per-setup gate configuration

The engine needs setup-level gate overrides, e.g.:
```python
cfg.bdr_require_regime = True    # BDR gets its own regime check
cfg.sc_require_regime = False    # SC bypasses global regime
cfg.ep_require_regime = False    # EP bypasses global regime
```

Until this is implemented, the portfolio must run as **isolated passes** where each setup uses its own config. The isolated-sum numbers (PF 1.38, +43.29R) represent the achievable target.

### Isolated-sum performance (verified):

| Setup | Config | N | PF | TotalR | MaxDD |
|-------|--------|---|-----|--------|-------|
| SC (q5, no regime) | alone | 49 | 1.19 | +4.82R | 7.35R |
| BDR (regime) | alone | 42 | 1.39 | +7.50R | 6.04R |
| EP early short (no regime) | alone | 72 | 1.45 | +30.97R | 15.53R |
| **COMBINED** | **sum** | **163** | **1.38** | **+43.29R** | **15.65R** |

Train/Test (cost-on): **Train PF 1.04 (+2.50R) / Test PF 1.84 (+40.79R)** — both positive.

---

## Implementation Requirements

### Priority 1: Per-setup regime gating (engine.py)
Add `bdr_require_regime`, `sc_require_regime` config fields. Apply the correct regime check per setup ID in the emission pipeline. This allows a single engine pass to use different gates for different setups.

### Priority 2: EMA PULL time cutoff
Add `ep_time_end` config parameter. Currently EP fires all day; only early session (<14:00) has positive edge. PM trades (PF 0.83 cost-on) destroy the edge.

### Priority 3: EMA PULL parent gate decoupling
Currently requires `show_trend_setups=True` which also enables VWAP KISS. Need to decouple so EP can be enabled independently without VK generating unwanted signals.

### Priority 4: Quality score validation
Quality≥5 filters SC from 149→49 trades. Verify the quality score is measuring genuine signal conviction (candle structure, volume confirmation, market context) and not an artifact of the scoring formula.

---

## Promotion Decision

**Recommended: Implement per-setup gating, then promote 3-setup portfolio to Track 2.**

The combined portfolio (PF 1.38, +43.29R) substantially outperforms VR at any configuration. All three components are independently positive after costs. The diversification across long (SC) and short (BDR, EP) provides directional balance.

**Immediate next step:** Implement per-setup regime gating in engine.py so the 3 setups can run in a single engine pass with their optimal gate configurations.

---

## Files
- `candidate_scan.py` — Full 16-setup scan (raw + cost-on)
- `candidate_deep_dive.py` — Isolated deep dives with gate exploration
- `CANDIDATE_SCORECARD.md` — This document
