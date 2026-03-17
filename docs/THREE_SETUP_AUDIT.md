# Three-Setup Audit: SC, EMA Pull, EMA FPIP

**Date:** 2026-03-11
**Universe:** 88 symbols, 207 trading days (2025-05-12 → 2026-03-09)
**Standard:** Engine-native replay with actual exit timestamps

## Executive Summary

Three unresolved setups were audited. All three fail promotion criteria. The clean promoted stack (BDR Short + FLR) produces PF 1.18 / 198 trades / +17.1R combined, passing all robustness checks.

## 1. SC / 2ND_CHANCE — RETIRE

**Standalone:** PF 0.48, N=236, Exp -0.309R, TotalR -73.0R
**Train/Test:** 0.45 / 0.52 (both FAIL)
**Prior work:** sc_repair_study.py tested 8+ exit modes, 8 combo variants, stop widths, quality/alignment gates. No variant achieved positive R.

**Verdict: RETIRE.** Breakout-retest-confirm mechanism produces no edge on this universe with available data. The pattern may work with real-time Level II / order flow data not currently available.

## 2. EMA PULL SHORT — SHELVE (insufficient data)

**Baseline:** PF 0.90, N=26, Exp -0.096R, TotalR -2.5R
**Train/Test:** 1.25 / 0.62 (Test FAIL — overfitting)

### Ablation Results

| Variant | N | PF | Exp(R) | TotalR |
|---------|---|----|--------|--------|
| Baseline (short, ≤14:00) | 26 | 0.90 | -0.096 | -2.5 |
| Extended (short, ≤15:00) | 74 | 0.78 | -0.202 | -14.9 |
| Full day (short, ≤15:30) | 109 | 0.64 | -0.340 | -37.1 |
| Both sides (≤14:00) | 60 | 0.56 | -0.469 | -28.1 |
| With regime gate | 15 | 1.41 | +0.333 | +5.0 |

**Key findings:**
- Extending time **destroys** performance (PF 0.78→0.64)
- Adding longs is catastrophic (long PF 0.36 vs short PF 0.90)
- Regime gate shows promise (PF 1.41) but N=15 is not statistically meaningful
- Short-only with ≤14:00 window is already the best config; no further optimization possible

**Verdict: SHELVE.** N=26 is too low to draw conclusions. The mechanism may have edge but lacks sufficient signal frequency. Revisit with more data (6+ additional months) or a larger symbol universe.

## 3. EMA FPIP — DEAD

**Baseline (all gates on):** N=2 trades in 10 months
**No context gates:** N=3
**Wide open (all relaxed):** N=6
**Multi-PB + no context:** N=8

**Verdict: DEAD.** The FPIP mechanism (expansion → first pullback → trigger with strict quality gates) is too restrictive for 5-min bars on an 88-symbol universe. Even removing all context gates and relaxing RVOL/quality produces single-digit trades. The setup requires either 1-min data or a much larger universe (500+ symbols) to generate meaningful signal volume.

## 4. Clean Promoted Stack: BDR Short + FLR

### Unconstrained

| Setup | N | PF | Exp(R) | TotalR | Train | Test | ExDay | ExSym |
|-------|---|-----|--------|--------|-------|------|-------|-------|
| BDR Short | 90 | 1.09 | +0.042 | +3.8 | 0.96 | 1.34 | 0.73 | 0.98 |
| FLR | 108 | 1.24 | +0.123 | +13.3 | 1.43 | 1.15 | 1.18 | 1.17 |
| **COMBINED** | **198** | **1.18** | **+0.086** | **+17.1** | **1.15** | **1.20** | **1.02** | **1.11** |

### Capped (max 3 concurrent)

| Setup | N | PF | Exp(R) | TotalR | Train | Test |
|-------|---|-----|--------|--------|-------|------|
| BDR Short | 77 | 0.73 | -0.138 | -10.7 | 0.77 | 0.66 |
| FLR | 88 | 1.49 | +0.245 | +21.5 | 1.54 | 1.46 |
| **CAPPED** | **165** | **1.13** | **+0.066** | **+10.9** | **1.08** | **1.18** |

**Note:** BDR suffers under cap (FLR takes priority due to earlier timestamps in 10:30-11:30 window). FLR improves dramatically under cap (PF 1.24→1.49) — likely because the weakest concurrent FLR trades get dropped.

### Why the clean stack works

The old 3-setup stack (SC+BDR+EP) had PF 0.66 / -70.6R. Removing SC's -73R hole and EP's -2.5R drag produces a stack that is now solidly profitable:

| Stack | N | PF | TotalR |
|-------|---|-----|--------|
| Old (SC+BDR+EP) | 352 | 0.66 | -70.6 |
| **Clean (BDR+FLR)** | **198** | **1.18** | **+17.1** |
| Clean capped | 165 | 1.13 | +10.9 |

## Promoted Stack (updated)

| Setup | Status | Side | PF | N | Exp(R) |
|-------|--------|------|----|---|--------|
| **BDR SHORT** | ★ PROMOTED | SHORT | 1.09 | 90 | +0.042 |
| **FL_MOMENTUM_REBUILD** | ★ PROMOTED | LONG | 1.24 | 108 | +0.123 |
| SC / 2ND_CHANCE | RETIRED | LONG | 0.48 | 236 | -0.309 |
| EMA PULL SHORT | SHELVED | SHORT | 0.90 | 26 | -0.096 |
| EMA FPIP | DEAD | BOTH | — | 2 | — |

## Recommended Next Steps

1. **Update config.py**: Set `show_second_chance = False`, `show_ema_pullback = False`
2. **Update promoted_replay.py**: Remove SC and EP sections, add FLR section
3. **Forward-test clean stack**: BDR Short + FLR in live paper trading
4. **Explore new setups**: The long book currently has only FLR (108 trades/10mo). Capacity is limited. Priority is finding additional long setups with PF > 1.0.
5. **Consider leadership filter on FLR**: The composite study showed RS vs SPY > 0 adds +0.02-0.05 PF consistently. Could be applied to FLR as a confirmation layer.
