# Strategy Revisit Notes

## BDR_SHORT (V3 family)
**Status:** Built, tested, not profitable on current dataset. Code is in repo.
**Date:** 2026-03-18

### What was built
- 4 variants: BDR_V3_A, V3_B, V3_C, V3_D
- New state machine: breakdown → weak retest → retest-low-break entry
- Removed VWAP as level type (ORL + swing low only)
- Fixed-RR exits, stop above retest high
- Per-variant time windows (10:25-10:40 default)

### Why it failed
- RED+TREND regime requirement starves the strategy — very few qualifying days in dataset
- Even with regime completely removed, IP 2.0, all context requirements off, quality gate bypassed: N=37, PF=0.37, TotalR=-11.1
- The short-side breakdown-retest-continuation pattern has no edge on this dataset period

### What to try next time
- Test on a dataset with more RED days (bear market period)
- Consider relaxing to RED/FLAT regime (not just RED+TREND)
- Consider 1-minute native execution (current is 5m bars)
- May need fundamentally different level types or entry mechanics
- The V3 architecture is sound — the market regime just doesn't support it in this sample

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
