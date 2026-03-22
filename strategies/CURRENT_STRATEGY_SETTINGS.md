# Current Production Strategy Settings

Updated: 2026-03-22

## Active Production Sleeve

| Strategy | IP Hard Floor | N | PF | TotalR | Key Config |
|---|---|---|---|---|---|
| EMA_FPIP_V3_B | 5.0 | 139 | 2.31 | +61.8 | prev_high_break entry, fixed 2.0R, pb_depth=0.70, pb_vol=0.95 |
| HH_QUALITY | 6.0 | 90 | 2.04 | +27.5 | break-entry, consol_min=2, multi-bar drive |
| SP_V2_SIMPLE | 6.0 | 61 | 1.90 | +21.3 | time<=14:05, RR<=1.9, measured-move targets |
| BIG_DAWG_LONG_V1 | bypass | 20 | 5.60 | +7.0 | IP gate bypassed — internal RR [0.50–1.10] + counter_wick filter |
| ORH_FBO_V2_B | 6.0 | 17 | 5.46 | +17.9 | failed breakout short, mode B + recovery rule |
| BDR_V3_C | 6.5 | 15 | 3.09 | +8.7 | breakdown-retest-break, 2.0R target, max_bars=16 |
| BS_STRUCT | 5.0 | 5 | 7.97 | +2.8 | backside structure long, admissibility filters |
| **TOTAL** | — | **347** | **2.37** | **+147.0** | |

## IP Hard Floor Settings (ip_v2_threshold_by_strategy)

Gate scale: **0–10 objective score** (changed from 0–1 percentile on 2026-03-22).
See `V2_GATE_BASELINES.md` for full scoring component breakdown.

```python
{
    "HH_QUALITY": 6.0,
    "EMA_FPIP_V3_B": 5.0,
    "SP_V2_SIMPLE": 6.0,
    "BDR_V3_C": 6.5,
    "BIG_DAWG_LONG_V1": 8.5,  # unused — strategy is in bypass set
    "BS_STRUCT": 5.0,
    "ORH_FBO_V2_A": 5.0,      # disabled at Gate -1
    "ORH_FBO_V2_B": 6.0,
    "EMA9_V5_C": 5.0,         # disabled at Gate -1
    "EMA9_V6_A": 5.0,         # disabled at Gate -1
}
```

Global defaults: `ip_v2_threshold_provisional = 5.0`, `ip_v2_threshold_confirmed = 5.0`

## IP Gate Bypass (ip_v2_gate_bypass_strategies)

Strategies whose internal logic is sufficient — skip the IP hard-floor gate entirely:
- **GGG_LONG_V1**: fires 9:30–9:45, before V2 confirmed stage is ready
- **BIG_DAWG_LONG_V1**: internal RR band + counter-wick filter is the real gate

## Disabled Strategies (Gate -1: config.disabled_strategies)

Signals silently dropped before any gate processing:

- **ORH_FBO_V2_A**: PF=0.81, -9.3R — worst strategy in portfolio
- **EMA9_V5_C**: demoted — median RR=0.29 on 1-min bars, structural R:R problem. Preserved for research.
- **EMA9_V6_A**: demoted — 5-min redesign showed improved R:R geometry but insufficient gated sample (N=5). Preserved for research.

## Strategy-Specific Admissibility Filters (added 2026-03-22)

### BIG_DAWG_LONG_V1

Added in `big_dawg_live.py` before signal metadata construction:

| Filter | Config Key | Value |
|---|---|---|
| Projected actual RR band | `bd_min_actual_rr` / `bd_max_actual_rr` | [0.50, 1.10] |
| Bullish trigger bar counter-wick | `bd_max_counter_wick_fraction` | ≤ 0.20 |

Rejects the fixed_rr_fallback case (actual_rr=2.0) which falls outside the band.
Counter-wick is `(bar.open - bar.low) / bar_range` — measures lower shadow on bullish bar.

### BS_STRUCT

Added in `backside_live.py` as two-stage filter:

| Filter | Config Key | Value | On Reject |
|---|---|---|---|
| Time end | `bs_time_end` | 12:30 (was 13:30) | — |
| Close location | `bs_min_close_location` | ≥ 0.70 | Keep pattern alive (retry next bar) |
| Body fraction | `bs_min_body_fraction` | ≥ 0.70 | Keep pattern alive |
| Counter-wick | `bs_max_counter_wick_fraction` | ≤ 0.15 | Keep pattern alive |
| Structure quality | `bs_min_structure_quality` | ≥ 0.50 | Kill pattern (one-and-done) |
| Confluence count | `bs_min_confluence_count` | ≥ 2 | Kill pattern (one-and-done) |

Bar anatomy rejects (close_location, body, wick) keep the pattern alive for a better breakout bar.
Structure/confluence rejects kill the pattern because the setup itself is flawed.

### ORH_FBO_V2_B Recovery Rule

Checked in dashboard signal promotion and replay gate:

| Filter | Config Key | Value |
|---|---|---|
| Time window | `orh_b_time_start` / `orh_b_time_end` | 12:00–14:00 |
| Counter-wick | `orh_b_counter_wick_max` | ≤ 0.15 |

If both conditions met, ORH_B signal bypasses normal gate and is promoted.

## Archived Strategies (in repo, commented out or superseded)

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

- **InPlayProxyV2** uses objective 0–10 scoring (no cross-sectional ranking)
- Five bucketed components: move-from-open, RS-vs-SPY, gap, range-expansion, RVOL
- Rolling 20-day baselines for RVOL and range-expansion (current day excluded)
- Provisional stage (9:40) is informational only — not promotable
- Confirmed stage (10:00 snapshot) is the promotable gate
- Per-strategy hard floors checked at signal promotion time in dashboard and replay

## Known Alignment Issues

- **in_play_score scale mismatch**: replay sets `snap.in_play_score` from V1 proxy (0-7 scale); dashboard sets it from V2 (0-10 scale). Strategy-internal checks (e.g. `ema9_v5_min_ip_score = 0.80`) behave differently in replay vs live. The external IP gate (V2, 0-10) is consistent across both paths. This mismatch only affects strategies that check `snap.in_play_score` internally.
- **production_sleeve.py is the single source of truth** for which strategies run. Both replay.py and dashboard.py import from it. Do NOT add strategies in one without the other.
