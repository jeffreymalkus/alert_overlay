# Current Production Strategy Settings

Updated: 2026-03-19

## Active Production Sleeve

| Strategy | IP Threshold | N | PF | TotalR | Key Config |
|---|---|---|---|---|---|
| EMA_FPIP_V3_B | 0.73 | 88 | 2.89 | +51.0 | prev_high_break entry, fixed 2.0R, pb_depth=0.70, pb_vol=0.95 |
| SP_V2_SIMPLE | 0.77 | 66 | 2.56 | +34.7 | time<=14:05, RR<=1.9, measured-move targets |
| HH_QUALITY | 0.79 | 59 | 2.20 | +20.9 | break-entry, consol_min=2, multi-bar drive |
| ORH_FBO_V2_A | 0.78 | 31 | 1.38 | +5.9 | failed breakout short, retest mode A |
| BDR_V3_C | 0.74 | 22 | 2.17 | +9.0 | breakdown-retest-break, 2.0R target, max_bars=16 |
| ORH_FBO_V2_B | 0.80 | 21 | 3.47 | +17.4 | failed breakout short, mode B + PM sleeves |
| EMA9_V5_C | 0.79 | 16 | 1.88 | +1.1 | $0.35 stop floor, $25-250 price band, structural target |
| BS_STRUCT | 0.80 | 8 | 1.91 | +1.7 | backside structure long |
| **TOTAL** | — | **201** | **2.25** | **+81.6R** | MaxDD=6.6R, WR=63.2% |

## IP Threshold Settings (ip_v2_threshold_by_strategy)

```python
{
    "HH_QUALITY": 0.79,
    "EMA_FPIP_V3_B": 0.73,
    "SP_V2_SIMPLE": 0.77,
    "ORH_FBO_V2_B": 0.80,
    "ORH_FBO_V2_A": 0.78,
    "BDR_V3_C": 0.74,
    "EMA9_V5_C": 0.79,
    "BS_STRUCT": 0.80,
}
```

Global default: `ip_v2_threshold_confirmed = 0.74`
(only used for strategies not in the per-strategy dict)

## Why These Thresholds

- **0.73 (FPIP):** PF actually rises as threshold drops — added trades are strongly profitable
- **0.74 (BDR):** Short strategy benefits from wider universe; PF peaks at 0.74
- **0.77 (Spencer):** PF peaks here (2.56); degrades below 0.75
- **0.78 (ORH_V2_A):** N grows without PF loss; good balance
- **0.79 (HH, EMA9):** PF peaks here for HH (2.20); EMA9 picks up 2 good trades
- **0.80 (ORH_V2_B, BS):** Elite sleeves — PF drops sharply at 0.78

## Disabled Strategies (in repo, commented out)

- FL_ANTICHOP: PF=0.61 — core concept broken
- FL_REBUILD_STRUCT_Q7: PF=0.42 — failed authoritative replay
- FL_REBUILD_R10_Q6: PF=0.75 — failed authoritative replay
- SC_SNIPER: PF=0.32
- ORL_FBD_LONG: PF=0.64
- FFT_NEWLOW_REV: PF=0.00
- PDH_FBO_B: N=1
- EMA9_V4_A/B/C/D: superseded by V5
- EMA_FPIP legacy / V3_A / V3_C: superseded by V3_B
- BDR_V3_A/B/D / V4_A/B/C/D: superseded by V3_C
- SP_ATIER legacy / V2_BAL / V2_HQ: superseded by V2_SIMPLE

## Key Architecture Notes

- **InPlayProxyV2** is the active base gate (V1 kept for emergency rollback only)
- Provisional stage (9:40) is informational only — not promotable
- Confirmed stage (10:00 snapshot) is the promotable gate
- Per-strategy thresholds checked against raw score in replay gate
- Strategies with internal quality gates (EMA9_V5, BDR_V4, ORH_B PM sleeves) bypass external A-tier
