# V2 In-Play Gate — Architecture & Baselines

Updated: 2026-03-22

## Current Gate Architecture (Objective 0–10 Scoring)

As of 2026-03-22, InPlayProxyV2 uses an **objective 0–10 score** computed from five
bucketed components. This replaced the earlier percentile-ranked cross-sectional gate.

### Scoring Components (sum to 0–10 max)

| Component | Input | Max | Buckets |
|---|---|---|---|
| Move-from-open | abs(close-open)/open % | 2.5 | <0.50→0, <1.00→0.625, <1.75→1.25, <3.00→1.875, ≥3.00→2.5 |
| RS-vs-SPY | symbol_move − spy_move (pct) | 2.0 | <0.50→0, <1.00→0.667, <2.00→1.333, ≥2.00→2.0 |
| Gap | abs(open−prev_close)/prev_close % | 1.5 | <0.50→0, <1.00→0.50, <2.00→1.00, ≥2.00→1.50 |
| Range expansion | session_range / 20d_avg_range | 2.0 | <0.75→0, <1.00→0.50, <1.50→1.00, <2.00→1.50, ≥2.00→2.0 |
| RVOL | session_vol / 20d_avg_vol | 2.0 | <0.50→0, <0.75→0.50, <2.00→1.00, <4.00→1.50, ≥4.00→2.0 |

### Key Properties

- **No cross-sectional ranking**: a symbol's score depends only on its own metrics, not other loaded symbols.
- **Rolling 20-day baselines**: RVOL and range-expansion use per-symbol deque(maxlen=20). Current day is excluded from its own baseline.
- **Two-stage timing**: PROVISIONAL at 9:40 (informational only), CONFIRMED at 10:00 (promotable gate).
- **Per-strategy hard floor**: each strategy declares a minimum score. PASS if `objective_score >= hard_floor`.

### Per-Strategy Hard Floors (0–10 scale)

```python
{
    "HH_QUALITY": 6.0,
    "EMA_FPIP_V3_B": 5.0,
    "SP_V2_SIMPLE": 6.0,
    "BDR_V3_C": 6.5,
    "BIG_DAWG_LONG_V1": 8.5,
    "BS_STRUCT": 5.0,
    "ORH_FBO_V2_A": 5.0,
    "ORH_FBO_V2_B": 6.0,
    "EMA9_V5_C": 5.0,
}
```

Global defaults: `ip_v2_threshold_provisional = 5.0`, `ip_v2_threshold_confirmed = 5.0`

### ORH_FBO_V2_B Recovery Rule

ORH_B has a special late-session recovery path checked in dashboard and replay:
- Time window: 12:00–14:00 ET
- Trigger bar must have `counter_wick_fraction <= 0.15`
- If both conditions met, signal is promoted even if it would otherwise be gated

---

## Historical Baselines (Pre-Objective Gate)

The numbers below were generated on 2026-03-18 under the **old percentile-ranked gate**
(cross-sectional, 0–1 scale). They are preserved for reference but no longer reflect the
active gate logic.

### Portfolio Summary (old percentile gate)

| Mode | N | PF | TotalR | WR | MaxDD |
|---|---|---|---|---|---|
| **V2 Confirmed-Only** | **265** | **1.20** | **+23.7R** | **51.3%** | **11.6R** |
| V2 Prov+Confirmed | 268 | 1.22 | +26.6R | 51.5% | 11.6R |
| No Gate (V1 min=0) | 267 | 0.82 | -24.3R | 43.8% | 24.3R |

### Strategy-Level Comparison (old percentile gate)

| Strategy | V2 Confirmed N/PF/R | V2 Prov+Conf N/PF/R | No Gate N/PF/R |
|---|---|---|---|
| BDR_V3_A | 25 / 1.13 / +1.1 | 25 / 1.13 / +1.1 | 27 / 0.62 / -4.7 |
| BDR_V3_B | 22 / 1.11 / +0.8 | 22 / 1.11 / +0.8 | 26 / 0.68 / -3.5 |
| BDR_V3_C | 25 / 1.21 / +1.8 | 25 / 1.21 / +1.8 | 27 / 0.63 / -4.6 |
| BDR_V3_D | 9 / 0.31 / -3.8 | 9 / 0.31 / -3.8 | 8 / 3.51 / +2.7 |
| BS_STRUCT | 8 / 1.91 / +1.7 | 8 / 1.91 / +1.7 | 10 / 0.45 / -2.1 |
| EMA9_V4_A | 41 / 0.30 / -26.5 | 41 / 0.30 / -26.5 | 22 / 0.40 / -11.9 |
| EMA_FPIP_V3_A | 60 / 2.15 / +23.3 | 60 / 2.15 / +23.3 | 57 / 1.33 / +7.3 |
| **EMA_FPIP_V3_B** | **60 / 2.42 / +30.4** | **60 / 2.42 / +30.4** | 57 / 1.52 / +12.3 |
| **HH_QUALITY** | **57 / 2.14 / +19.9** | **60 / 2.23 / +22.8** | 38 / 3.03 / +16.7 |
| ORH_FBO_V2_A | 26 / 1.45 / +5.4 | 26 / 1.45 / +5.4 | 76 / 0.54 / -26.6 |
| **ORH_FBO_V2_B** | **14 / 1.93 / +5.9** | **14 / 1.93 / +5.9** | 12 / 1.18 / +1.3 |
| **SP_ATIER** | **91 / 1.16 / +7.2** | **91 / 1.16 / +7.2** | 13 / 1.93 / +4.7 |

### Key Observations (old gate)

1. V2 gate was strongly value-additive: no-gate PF=0.82 (-24.3R) vs V2 PF=1.20 (+23.7R)
2. Top strategies under V2: FPIP_V3_B (+30.4R), HH (+19.9R), FPIP_V3_A (+23.3R), SP (+7.2R), ORH_V2B (+5.9R)
3. Without the gate, ORH_FBO_V2_A collapses: PF 1.45 → 0.54 (-26.6R) — gate is essential for shorts
