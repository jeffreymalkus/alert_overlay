# Day-Filter Proxy Research — Verdict

**Date:** 2026-03-09
**Universe:** 88 symbols, 2025-12-08 → 2026-03-09
**Probes:** VR (VWAP Reclaim, 3.0R target) + VKA (VWAP Kiss Accept, 2.0R target)

---

## Part 1: Ranked Real-Time Day Proxies

| Rank | Proxy | Combined R | VR R | VKA R | Avg PF | Train+Test>1 |
|------|-------|-----------|------|-------|--------|--------------|
| 1 | SPY EMA9 > EMA20 | +76.38R | +64.17R | +12.21R | 1.06 | 2/4 |
| 2 | SPY pct > 0.10% | +48.27R | +35.55R | +12.72R | 1.04 | 2/4 |
| 3 | SPY pct > 0.03% | +23.74R | +18.52R | +5.22R | 1.02 | 2/4 |
| — | **Current RT (pct>0.05%)** | **+69.34R** | **+38.05R** | **+31.28R** | **1.05** | — |
| — | Perfect-foresight GREEN | +435.10R | +183.79R | +251.31R | 1.41 | — |

**All other proxies tested were worse than the current proxy.**
Specifically, SPY above VWAP, VWAP+pct combos, 3-bar momentum, strong combo, and all delayed-start variants produced negative combined R.

---

## Part 1: Key Findings

### 1. No real-time proxy recovers meaningful edge

The gap between perfect-foresight (+435R) and current RT (+69R) is **365.76R**. The best proxy (EMA9>EMA20) recovers only **+7.05R** of that gap — approximately **2%**. This is negligible.

### 2. EMA structure is the least-bad proxy for VR

SPY EMA9>EMA20 produces VR PF=1.12, +64.17R — the only proxy with VR PF above 1.10. But it still fails VKA (PF=1.01, barely positive) and fails train/test stability on the test half.

### 3. The current proxy (pct>0.05%) is actually near-optimal

Raising the threshold to 0.10% barely moves the needle. Lowering it lets in more poison trades. The current 0.05% threshold is close to the best achievable with a single pct_from_open cutoff.

### 4. Poison trades come from RED and FLAT days

Every proxy still admits RED-day trades with PF 0.45–0.81 and FLAT-day trades with PF 0.33–0.66. The real-time proxy simply cannot distinguish days that will end RED from days that look flat/positive at 10AM. **This is the fundamental problem — it's not a tuning issue, it's an information-availability issue.**

### 5. Delayed starts destroy trade count without helping

10:15 and 10:30 delayed starts dramatically reduce N while worsening PF. SPY direction at 10:30 is no more predictive than at 10:00.

### 6. Combo filters are consistently worse than simpler ones

Adding conditions (VWAP + EMA + pct) reduces trade count without improving quality. The conditions are correlated enough that stacking them adds filtering noise, not signal.

---

## Part 2: Engine Drift Triage

### VR with best proxy (EMA9>EMA20)

| Category | N | PF | TotalR |
|----------|---|------|--------|
| Standalone (EMA9>EMA20) | 869 | 1.12 | +64.17R |
| Engine total | 447 | 0.77 | -49.83R |
| Suppressed by engine | 653 | 1.12 | +50.95R |
| Shared (standalone PnL) | 216 | 1.10 | +13.23R |
| Shared (engine PnL) | 216 | 0.61 | -40.36R |
| Extra from engine | 231 | 0.92 | -9.47R |

### VKA with best proxy (pct>0.10%)

| Category | N | PF | TotalR |
|----------|---|------|--------|
| Standalone (pct>0.10%) | 1552 | 1.01 | +12.72R |
| Engine total | 951 | 0.77 | -108.31R |
| Suppressed by engine | 1089 | 1.16 | +93.90R |
| Shared (standalone PnL) | 463 | 0.70 | -81.18R |
| Shared (engine PnL) | 463 | 0.65 | -81.01R |
| Extra from engine | 488 | 0.89 | -27.31R |

### Engine Drift Root Causes

1. **Engine suppresses the majority of standalone trades (75% for VR, 70% for VKA)** — and the suppressed trades have PF>1.0. The engine is actively hurting performance.

2. **Shared trades perform worse in engine than standalone** — VR shared trades go from PF 1.10 → 0.61 (same entry signal, different exit mechanics). This points to engine exit/target differences, not entry filtering.

3. **Engine adds ~230-490 extra trades** that standalone doesn't generate — these have PF<1.0, indicating engine's own state machine generates false triggers.

4. **The primary engine suppressors are likely:** cooldown gates (one-per-family-per-N-bars), quality gates (minimum Q score thresholds), regime/tape permission (layered_regime gates), and RR floor checks. These need individual audit but the suppression pattern (PF>1.0 suppressed) means the gates are too aggressive.

---

## Verdict

### The deployability problem has two layers, both severe:

**Layer 1 — Day filter (confirmed, unfixable with simple proxies):**
No combination of live-available SPY indicators can replicate end-of-day GREEN classification. The 365R gap between perfect-foresight and best real-time proxy is structural — it reflects the impossibility of predicting the day's outcome at 10AM. All prior research using perfect-foresight GREEN filters produced results that cannot be reproduced in live trading.

**Layer 2 — Engine drift (confirmed, repairable):**
The engine suppresses 70-75% of trades that would be profitable (PF>1.0), and produces worse exit outcomes on shared trades. This is fixable through targeted gate relaxation — but fixing it only helps if the day filter is also addressed.

### Recommendation: Next highest-EV repair move

1. **Accept that perfect-foresight results are a ceiling, not an achievable target.** The realistic live trading baseline is the current RT proxy or EMA structure, both of which produce PF 1.01–1.12 for the best setup (VR). VKA is marginal-to-negative under all real-time conditions.

2. **Audit engine suppression gates one-by-one for VR only.** VR is the only setup with stable positive edge under real-time conditions (PF 1.06-1.12). Identify which gate suppresses the most +EV trades and selectively relax it.

3. **Do not promote VKA to engine validation.** VKA's edge entirely depends on perfect-foresight day selection. Under any real-time filter, VKA is PF ~1.01 standalone — there is no edge to preserve.

4. **Consider whether the ~+38-64R (PF 1.06-1.12) that VR produces under real-time conditions justifies live deployment.** This is thin edge over 869-948 trades across 3 months.

---

## Files

- `dayfilter_proxy_research.py` — Full proxy research + engine drift triage
- `DAYFILTER_PROXY_VERDICT.md` — This document
