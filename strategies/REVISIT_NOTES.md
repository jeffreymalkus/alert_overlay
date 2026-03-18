# Strategy Revisit Notes

## BDR_SHORT (V3 family)
**Status:** Repaired and retested. V3_D is profitable. Keep all variants for re-evaluation after IP gate upgrade.
**Date:** 2026-03-18 (repaired)

### What was built
- 4 variants: BDR_V3_A, V3_B, V3_C, V3_D
- New state machine: breakdown → weak retest → retest-low-break entry
- Removed VWAP as level type (ORL + swing low only)
- Fixed-RR exits, stop above retest high
- Per-variant time windows (10:25-10:40 default)

### Repair completed (2026-03-18)
- Fixed: V3 was emitting signals with hardcoded quality=3 and no quality_tier in metadata
  → all signals defaulted to B-tier → blocked by A-tier gate → N=0
- Fixed: _find_support_level now filters by enabled level flags before selection
- Fixed: bdr_skip_generic_trigger_body_filter / vol_filter now wired to skip_filters
- Full rejection + quality pipeline now runs for V3 (inverted scoring for shorts)

### Results after repair
| Variant | N | PF | TotalR | Notes |
|---|---|---|---|---|
| V3_A (balanced) | 18 | 0.44 | -5.7 | Weak retest, 10:25-10:40 |
| V3_B (tight time) | 17 | 0.50 | -4.5 | 10:25-10:35 |
| V3_C (2.0R target) | 18 | 0.54 | -4.7 | Same as A but wider target |
| **V3_D (strict reclaim)** | **5** | **1.50** | **+0.5** | Stricter retest anatomy works |

### What to try when revisiting
- **Re-evaluate all variants after IP gate upgrade** — the current generic IP gate
  blocks 80%+ of BDR signals. A more nuanced short-specific IP gate may unlock
  more opportunity for V3_A/B/C
- V3_D's strict failed-reclaim filter is the only profitable variant — the extra
  retest quality requirements (smaller body, more upper wick) genuinely discriminate
- Test V3_D with wider time window (10:00-11:00) to increase N
- Test with per-strategy IP floor (BDR may need different IP threshold than longs)
- Consider adding short-specific IP scoring (negative RS, below VWAP) as IP boost

---

## EMA9_FT (V4 family)
**Status:** Built, tested, not profitable on current dataset. Code is in repo.
**Date:** 2026-03-18

### What was built
- 4 variants: EMA9_V4_A, V4_B, V4_C, V4_D
- 2-bar-low stop (replaces broad pullback-low stop)
- Relative impulse vs SPY as main trigger filter (≥0.02 default, ≥0.04 for V4_C)
- Fixed-RR exits (1.25R default, 2.0R for V4_B)
- Tighter time window (10:00-10:45)
- Optional 5m context sleeve (V4_D)

### Why it failed
- 2-bar-low stop is too tight for 5-minute bars → 71% stop-out rate
- Average bars held = 3.8 (stopped out almost immediately)
- AvgWin=+1.03R but AvgLoss=-1.43R — losses exceed wins
- V4_D (5m context) produced identical results to V4_A — context filter not binding on 5m data

### What to try next time
- **Primary:** Test on 1-minute bars where 2-bar-low stop gives appropriate room
- Try 3-bar-low or setup-bar-low stop on 5m as intermediate
- Consider wider stop: pullback_low (legacy) with V4 trigger filters might be the better combo
- The relative impulse filter IS producing more signals (118 raw vs 76 legacy) — the trigger selection is working, just the stop model doesn't fit 5m bars
- Test hybrid: V4 trigger logic + legacy pullback-low stop

---

## ALL STRATEGIES — Pending IP Gate Upgrade
**Status:** User is redesigning the in-play gate to be more sophisticated and nuanced.
**Date:** 2026-03-18

### Context
- The current generic IP gate (rvol-based scoring, single threshold) blocks 80%+ of raw signals across all strategies
- Per-strategy IP floors are a workaround (HH=3.0, SP=3.5, ORH_V2B=3.5)
- Short strategies (BDR, ORH) may need fundamentally different IP criteria than longs

### What to re-evaluate after IP gate upgrade
1. **BDR_V3_A/B/C** — currently PF<1.0 but 80% of signals blocked by IP. May improve significantly.
2. **BDR_V3_D** — already profitable (PF=1.50). Should improve further with better IP selection.
3. **EMA9_V4_A/B/C/D** — 71% stop-out rate may partly be from wrong IP names. Re-test.
4. **All per-strategy IP floors** — may become unnecessary if the gate itself is smarter.
5. **Short-side IP scoring** — consider negative RS, below-VWAP as positive signals for shorts.

### Current profitable strategy sleeve (for reference)
| Strategy | N | PF | TotalR | IP Floor |
|---|---|---|---|---|
| HH_QUALITY | 14 | 3.51 | +8.6R | 3.0 |
| EMA_FPIP_V3_B | 21 | 3.03 | +11.4R | 3.0 |
| SP_ATIER | 14 | 1.20 | +1.4R | 3.5 |
| ORH_FBO_V2_B | 12 | 1.18 | +1.3R | 3.5 |
| BDR_V3_D | 5 | 1.50 | +0.5R | 3.0 |
| **Combined** | **66** | — | **+23.2R** | — |
