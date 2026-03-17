# New Strategy Validation Verdicts

**Date:** 2026-03-11
**Universe:** 93 symbols, 10 months (2025-05-12 → 2026-03-09)
**Data:** 5-min bars with SPY/QQQ market context + sector ETF RS

## Summary

All 4 new strategies (derived from SMB Trading source material) were implemented,
wired into the engine, and validated via full-universe replay. None meet promotion
criteria in their current configuration.

## Raw Results (Default Config)

| Setup              | N     | PF   | Exp(R)  | WR%   | TrnPF | TstPF | Verdict |
|-------------------|-------|------|---------|-------|-------|-------|---------|
| HITCHHIKER        | 7,124 | 0.70 | -0.164  | 50.0% | 0.71  | 0.70  | FAIL    |
| FASHIONABLY_LATE  | 5,403 | 0.63 | -0.255  | 32.2% | 0.64  | 0.63  | FAIL    |
| BACKSIDE          | 7,443 | 0.69 | -0.202  | 37.7% | 0.67  | 0.70  | FAIL    |
| RUBBERBAND        | 1,043 | 0.36 | -0.276  | 28.1% | 0.44  | 0.30  | FAIL    |

## HitchHiker Long Ablation (Best Candidate)

HitchHiker Long was the most promising at PF 0.75 / WR 51.6%.
Extensive parameter tuning tested time windows, quality gates,
market alignment, consolidation bars, volume requirements, and drive ATR.

### Best Configs on 10-Symbol Sample

| Config                          | N   | PF   | Exp    | WR%   |
|--------------------------------|-----|------|--------|-------|
| t1100+consol5                  | 88  | 1.02 | +0.007 | 56.8% |
| t1100+consol5+vol1.3+Q5       | 18  | 2.22 | +0.235 | 72.2% |
| t1100+consol5+Q5+MktAlign     | 15  | 1.34 | +0.105 | 66.7% |
| t1100+consol5+drive1.5        | 80  | 0.99 | -0.006 | 55.0% |

### Full Universe Validation (93 Symbols)

| Config                          | N   | PF   | Exp    | WR%   | TrnPF | TstPF |
|--------------------------------|-----|------|--------|-------|-------|-------|
| t1100+consol5                  | 813 | 0.85 | -0.076 | 52.6% | 0.89  | 0.81  |
| t1100+consol5+vol1.3+Q5       | 246 | 0.85 | -0.077 | 53.7% | 0.95  | 0.77  |
| t1100+consol5+Q5+MktAlign     | 154 | 0.86 | -0.069 | 54.5% | 0.90  | 0.84  |

**Conclusion:** Tuning improves HitchHiker from PF 0.70 → 0.86 but does not reach
the PF > 1.0 threshold. The pattern shows edge on a small sample but degrades on
the full universe, indicating the 10-symbol sample was over-optimistic.

## Per-Setup Analysis

### HITCHHIKER (PF 0.70 → best 0.86)
- Closest to viability. 50% WR shows the pattern detects real structure.
- Primary issue: consolidation breakouts don't distinguish institutional program
  continuation from noise. The original concept requires order-flow data
  (level 2, time & sales) that we don't have in bar data.
- Time window (935-1100) helps: morning breakouts are higher quality.
- Potential: with L2 data or tick-level volume profiling, this could work.

### FASHIONABLY_LATE (PF 0.63)
- 9EMA/VWAP cross fires too frequently (5,403 trades).
- The cross is a lagging signal — by the time 9EMA crosses VWAP, the move
  is already priced in at the 5-min resolution.
- Would need 1-min bars or intrabar cross detection to catch early enough.
- Stop logic (1/3 VWAP-to-LOD) may be too wide for the setup's edge.

### BACKSIDE (PF 0.69)
- Extension → HH/HL → consolidation → breakout fires reliably (7,443 trades).
- The issue: mean-reversion setups need aggressive target management.
  Fixed 2R target doesn't match the VWAP-snap mechanics of the pattern.
- Potential: adaptive target (partial at VWAP, trail remainder) could help.
- Also needs stronger HH/HL confirmation (currently just 1 HH + 1 HL).

### RUBBERBAND (PF 0.36)
- Overextension snapback is the weakest. Only 1,043 trades but very negative.
- The 3× ATR extension + acceleration filter fires on legitimate moves that
  continue, not just overextensions.
- Counter-trend by nature — fading strong moves is inherently high risk.
- Would need volume exhaustion signals or order-flow divergence to time properly.

## Files Modified

- `models.py` — SetupIds 21-24, SetupFamilies MOMENTUM/CONSOL_BREAK, ~70 DayState fields
- `config.py` — ~80 new parameters across 4 strategy sections
- `engine.py` — 4 _detect_* + 4 _update_*_state methods, wired into dispatch/emission
- `replays/new_strategy_replay.py` — Full validation replay script

## Engine Status

All 4 strategies remain in the codebase with `show_*` toggles defaulting to False.
They can be re-enabled for future refinement without code changes.

**Active promoted setups remain unchanged:**
- BDR Short (PF 1.09)
- SC Long Q≥5
- EP Short
