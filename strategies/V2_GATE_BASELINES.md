# V2 In-Play Gate Baseline Comparisons

Generated: 2026-03-18
Gate: InPlayProxyV2 (percentile-ranked, two-stage)
Default: V2 confirmed-only (provisional blocked)

## Portfolio Summary

| Mode | N | PF | TotalR | WR | MaxDD |
|---|---|---|---|---|---|
| **V2 Confirmed-Only** | **265** | **1.20** | **+23.7R** | **51.3%** | **11.6R** |
| V2 Prov+Confirmed | 268 | 1.22 | +26.6R | 51.5% | 11.6R |
| No Gate (V1 min=0) | 267 | 0.82 | -24.3R | 43.8% | 24.3R |

## Strategy-Level Comparison

| Strategy | V2 Confirmed N/PF/R | V2 Prov+Conf N/PF/R | No Gate N/PF/R |
|---|---|---|---|
| BDR_V3_A | 25 / 1.13 / +1.1 | 25 / 1.13 / +1.1 | 27 / 0.62 / -4.7 |
| BDR_V3_B | 22 / 1.11 / +0.8 | 22 / 1.11 / +0.8 | 26 / 0.68 / -3.5 |
| BDR_V3_C | 25 / 1.21 / +1.8 | 25 / 1.21 / +1.8 | 27 / 0.63 / -4.6 |
| BDR_V3_D | 9 / 0.31 / -3.8 | 9 / 0.31 / -3.8 | 8 / 3.51 / +2.7 |
| BS_STRUCT | 8 / 1.91 / +1.7 | 8 / 1.91 / +1.7 | 10 / 0.45 / -2.1 |
| EMA9_V4_A | 41 / 0.30 / -26.5 | 41 / 0.30 / -26.5 | 22 / 0.40 / -11.9 |
| EMA9_V4_B | 41 / 0.30 / -31.0 | 41 / 0.30 / -31.0 | 22 / 0.51 / -11.2 |
| EMA9_V4_C | 22 / 0.36 / -12.6 | 22 / 0.36 / -12.6 | 15 / 0.52 / -5.7 |
| EMA9_V4_D | 31 / 0.24 / -23.0 | 31 / 0.24 / -23.0 | 17 / 0.31 / -11.6 |
| EMA_FPIP | 15 / 0.86 / -1.2 | 15 / 0.86 / -1.2 | 21 / 0.42 / -7.2 |
| EMA_FPIP_V3_A | 60 / 2.15 / +23.3 | 60 / 2.15 / +23.3 | 57 / 1.33 / +7.3 |
| **EMA_FPIP_V3_B** | **60 / 2.42 / +30.4** | **60 / 2.42 / +30.4** | 57 / 1.52 / +12.3 |
| EMA_FPIP_V3_C | 62 / 1.91 / +20.7 | 62 / 1.91 / +20.7 | 59 / 1.28 / +6.4 |
| FFT_NEWLOW_REV | 6 / 0.00 / -6.0 | 6 / 0.00 / -6.0 | 13 / 0.55 / -6.0 |
| **HH_QUALITY** | **57 / 2.14 / +19.9** | **60 / 2.23 / +22.8** | 38 / 3.03 / +16.7 |
| ORH_FBO_V2_A | 26 / 1.45 / +5.4 | 26 / 1.45 / +5.4 | 76 / 0.54 / -26.6 |
| **ORH_FBO_V2_B** | **14 / 1.93 / +5.9** | **14 / 1.93 / +5.9** | 12 / 1.18 / +1.3 |
| ORL_FBD_LONG | 41 / 0.64 / -5.3 | 41 / 0.64 / -5.3 | 68 / 0.77 / -4.5 |
| PDH_FBO_B | 1 / 0.00 / -0.7 | 1 / 0.00 / -0.7 | 7 / 1.41 / +1.2 |
| SC_SNIPER | 6 / 0.32 / -3.1 | 6 / 0.32 / -3.1 | 9 / 0.68 / -1.8 |
| **SP_ATIER** | **91 / 1.16 / +7.2** | **91 / 1.16 / +7.2** | 13 / 1.93 / +4.7 |

## Provisional Impact (B1 vs B2)

Only HH_QUALITY differs: 57 → 60 trades (+3), +19.9 → +22.8R (+2.9R)
All other strategies: identical (they trigger after 10:00)

## Key Observations

1. V2 gate is strongly value-additive: no-gate PF=0.82 (-24.3R) vs V2 PF=1.20 (+23.7R)
2. Top strategies under V2: FPIP_V3_B (+30.4R), HH (+19.9R), FPIP_V3_A (+23.3R), SP (+7.2R), ORH_V2B (+5.9R)
3. EMA9_V4 family is deeply value-destructive (-93.1R total) — needs stop model fix per REVISIT_NOTES
4. BDR_V3_A/B/C turned profitable under V2 (PF 1.11-1.21) — V2 gate selects better short candidates
5. Without the gate, ORH_FBO_V2_A collapses: PF 1.45 → 0.54 (-26.6R) — gate is essential for shorts
