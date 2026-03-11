# Portfolio D — Frozen Two-Sided System Spec (Variant 2)

**Status:** Candidate system under evaluation
**Promoted from:** Portfolio C (Variant 2 recalibration)
**Validated:** 2026-03-09
**Data range:** 2026-01-23 to 2026-03-04 (32 trading days, 19 active days)
**Universe:** 87 symbols (excludes ETF indexes and sector ETFs)

---

## Change Log

| Date | Change | Rationale |
|------|--------|-----------|
| 2026-03-09 | Portfolio C frozen | Binary regime (Non-RED / RED+TREND) |
| 2026-03-09 | Promote Variant 2 → Portfolio D | Layered regime (L2=0.40) improves long book; frozen shorts unchanged |

**What changed from Portfolio C:**
- Long book regime gate replaced: Non-RED day filter → Layered L2 tape permission ≥ 0.40
- Short book unchanged (RED+TREND, wick≥30%, AM-only)
- Net effect: 7 fewer trades, higher PF, lower drawdown, more total R

---

## Portfolio Summary

| Metric | Combined | Long Book | Short Book |
|--------|----------|-----------|------------|
| N | 69 | 37 | 32 |
| PF(R) | 2.23 | 1.74 | 4.23 |
| Exp(R) | +0.461R | +0.414R | +0.515R |
| TotalR | +31.79R | +15.32R | +16.47R |
| MaxDD(R) | 5.29R | 5.92R | 2.01R |
| Stop Rate | 14.5% | 16.2% | 12.5% |
| Win Rate | 59.4% | 45.9% | 75.0% |

---

## Long Book — VWAP_KISS + SECOND_CHANCE

### Entry Rules

**Setup: VWAP_KISS (VK)**

1. Price moves ≥1.25 ATR away from VWAP (separation gate)
2. Price retraces ("kisses") back to within 0.03 ATR of VWAP
3. Reclaim: price can penetrate up to 0.10 ATR below VWAP
4. Confluence bonus: ±0.05 ATR of key levels
5. Market alignment required (SPY trend must not oppose)
6. Sector alignment required (sector ETF must not oppose)
7. Relative strength vs market required (rs_market ≥ 0.0)
8. Long only (shorts disabled)

**Setup: SECOND_CHANCE (SC)**

1. Breakout bar: close ≥0.10 ATR beyond key level, range ≥80% of median-10, volume ≥1.10× vol_ma, close in upper 40% of bar
2. Strong breakout volume gate: volume ≥1.25× vol_ma (required)
3. Retest: within 6 bars, low touches level within 0.05 ATR, pullback ≤0.35 ATR below level, retest volume ≤1.0× vol_ma
4. Confirmation: within 3 bars of retest, volume ≥0.90× vol_ma, close above retest high AND above both VWAP and EMA9
5. Time window: 9:45 AM to 2:30 PM
6. Long only (shorts disabled)

### Long Filters (Locked — Variant 2)

- **Tape permission ≥ 0.40:** Layered regime Layer 2 directional permission score must reach 0.40
  - Replaces the crude Non-RED day filter from Portfolio C
  - Tape components (weighted): mkt_vwap (1.0), mkt_ema (0.8), mkt_pressure (0.6), sec_vwap (0.5), sec_ema (0.4), rs_market (0.7), rs_sector (0.3), time_penalty (0.3)
  - Permission = weighted tape score, direction-adjusted (positive = favorable for longs)
  - Computed at signal time from live SPY/QQQ/sector snapshots + stock RS + time-of-day
- **Quality ≥ 2:** Minimum quality score threshold
- **Entry before 15:30:** No entries in last 30 minutes

### Long Stop Logic

- **VK:** Stop below entry bar low, minimum 1.0× intra-ATR
- **SC:** Stop $0.02 below retest low

### Long Exit Logic (priority order)

1. Day boundary → exit at previous bar close
2. End of session (≥15:55) → exit at bar close
3. Stop hit → exit at stop price
4. EMA9 trail exit → exit at bar close if price closes below EMA9
5. Time stop (20 bars for breakout family) → exit at bar close

---

## Short Book — BDR_SHORT (Breakdown-Retest)

### Regime Permission

- **RED+TREND days only:** SPY open-to-close < -0.05% AND close in bottom 25% of day range
- No short entries on any other regime

### Entry Rules

1. **Breakdown detection:**
   - Swing low identification (10-bar lookback)
   - Breakdown bar closes ≥0.15 ATR below support level
   - Breakdown bar range ≥70% of median-10 bar range
   - Breakdown bar volume ≥90% of vol_ma
   - Breakdown bar close in lower 60% of bar range
   - Level types: ORL (opening range low), VWAP, SWING low

2. **Retest detection (within 8 bars of breakdown):**
   - High approaches broken level within 0.30 ATR
   - High does not exceed level by more than 0.30 ATR
   - Running low tracked across all pre-retest bars

3. **Rejection bar (within 4 bars of retest peak):**
   - Upper wick ≥30% of bar range (big wick filter — locked)
   - Confirms failed reclaim of broken level

4. **AM entry only:** Entry time before 11:00 AM

5. **Scan window:** 10:00 AM (after OR forms) to 3:00 PM

### Short Stop Logic

- Stop = retest high + 0.30× ATR (above the failed retest)

### Short Exit Logic (priority order)

1. Stop hit (bar high ≥ stop price) → exit at stop price
2. Time stop (8 bars) → exit at bar close
3. End of day (≥15:55) or day boundary → exit at bar close

---

## Regime Architecture — Layered Framework

### Long Book: Layer 2 Tape Permission (replaces binary Non-RED gate)

The long book uses a continuous directional permission score computed from 8 weighted components.
Each component is scored [-1, +1]. The weighted average is direction-adjusted so positive = favorable for longs.

| Component | Weight | Source |
|-----------|--------|--------|
| Market VWAP state | 1.0 | SPY close vs VWAP, graded by distance |
| Market EMA structure | 0.8 | SPY EMA9 vs EMA20 + close vs EMA9 + EMA9 slope |
| Market pressure | 0.6 | SPY pct_from_open (rate of change proxy) |
| Sector VWAP state | 0.5 | Sector ETF close vs VWAP |
| Sector EMA structure | 0.4 | Sector ETF EMA structure |
| Stock RS vs market | 0.7 | Stock pct_from_open vs SPY pct_from_open |
| Stock RS vs sector | 0.3 | Stock pct_from_open vs sector pct_from_open |
| Time of day | 0.3 | Morning=+1, midday=0, late afternoon=-0.3, last 30min=-1 |

**Gate:** Long permission must be ≥ 0.40 to generate an alert.

This replaces the old binary regime that blocked ALL longs on RED days. The tape model allows longs on any day where conditions are genuinely favorable, while blocking longs on days where the tape is weak regardless of color.

### Short Book: Binary RED+TREND Gate (unchanged from Portfolio C)

| Component | Rule |
|-----------|------|
| Direction: RED | SPY pct_from_open < -0.05% |
| Character: TREND | SPY close in bottom 25% of intraday range |

Short entries require BOTH conditions. No tape override.

### Book Overlap

Under Variant 2, the books CAN theoretically overlap — a RED+TREND day where the tape permission ≥ 0.40
for a specific stock is possible (strong RS stock on a mild red day). In practice this is rare and the
tape threshold naturally suppresses most cross-regime trades.

---

## Slippage Model

Dynamic per-trade slippage:

```
base_slip = max($0.02, price × 0.0004)
vol_mult = clamp(bar_range / ATR, 0.5, cap)
family_mult = per-setup (trend=1.0, breakout=1.5, short_struct=1.2)
total_slip = base_slip × vol_mult × family_mult
```

Applied to both entry and exit fills.

---

## Portfolio Assumptions

| Parameter | Value |
|-----------|-------|
| Risk per trade | $100 (fixed R-unit) |
| Position sizing | Risk-based: shares = $100 / (entry - stop) |
| Max simultaneous positions | 15 (observed max, no hard cap) |
| Avg simultaneous positions | 2.0 |
| Long+short overlap | 0% (structural — different regime days) |
| One position per symbol | Yes (engine constraint) |
| Daily loss stop | None (naturally self-limiting) |
| Portfolio cap | None (naturally self-limiting) |

---

## Validation Results

### Train/Test Stability

| Split | N | PF(R) | Exp(R) |
|-------|---|-------|--------|
| Train (odd dates) | 48 | 2.80 | +0.567R |
| Test (even dates) | 21 | 1.43 | +0.219R |
| Verdict | | | STABLE — both splits positive |

### Robustness

| Variant | N | PF(R) | Exp(R) | TotalR |
|---------|---|-------|--------|--------|
| Full | 69 | 2.23 | +0.461R | +31.79R |
| Excl best day (Feb 23) | 55 | 1.91 | +0.392R | +21.55R |
| Excl top sym (JNUG) | 67 | 1.98 | +0.376R | +25.21R |

All robustness variants remain R-positive with PF(R) > 1.5.

### Improvement over Portfolio C

| Metric | Portfolio C | Portfolio D (Variant 2) | Change |
|--------|-------------|------------------------|--------|
| N | 76 | 69 | -7 trades |
| PF(R) | 1.98 | 2.23 | +0.25 |
| Exp(R) | +0.402R | +0.461R | +0.059R |
| TotalR | +30.54R | +31.79R | +1.25R |
| MaxDD(R) | 6.30R | 5.29R | -1.01R (improved) |
| Excl best day PF | 1.61 | 1.91 | +0.30 |
| Test set PF | 1.36 | 1.43 | +0.07 |

---

## Known Limitations

1. **Small sample:** 69 trades across 32 trading days. Only 4 RED+TREND days in dataset.
2. **Short concentration:** ~52% of portfolio R comes from the short book on just 4 days.
3. **Long book test set:** Test split (even dates) shows PF=0.77 for longs alone. Combined portfolio is stable because short book compensates.
4. **L1 long envelope not used:** The recalibration found L2=0.40 does all the work on the long side. L1 envelope was not needed and is effectively disabled (all L1 floors produced identical results at L2=0.40).
5. **L2 blocks positive-R longs on deep-RED days:** 21 longs blocked by L1=-0.05% had +0.630R avg expectancy. The tape L2 rejects them because tape conditions are genuinely bad despite individual trade results being positive. May be small-sample noise.
6. **No extended OOS:** Dataset is 2026-01-21 to 2026-03-06 only.

---

## What This System Is NOT

- Not a fully validated production system (sample too small)
- Not heavily optimized (L2=0.40 was the best of 5 tested values, not a fine-grained sweep)
- Not a guarantee the short-side edge persists (4 RED+TREND days is minimal)
- Not portfolio-capped (naturally self-limits, but no hard risk controls yet)

## Next Steps Before Production

1. Accumulate more RED+TREND days (target: ≥10)
2. Validate tape permission stability with more data
3. Add position-level and portfolio-level risk controls
4. Forward-test with paper trading
5. Monitor tape L2 rejection rate — confirm blocked trades remain negative-expectancy

---

## Active Research Tracks

### Data Expansion (in progress)
- `collect_history.py` pulling 3 months of 5-min bars for 97 symbols from IBKR
- Current dataset: 62 trading days (2025-12-08 → 2026-03-09), 4,743 SPY bars
- Target: ~90+ trading days back to ~September/October 2025
- Also supports `--bar-size 1min` for SPY/QQQ state research on demand
- `bar_recorder.py` available as utility for 1-min recording + 5-min aggregation

### Regression Runner (ready)
- `regression_runner.py` — runs all 19 study scripts, captures metrics, diffs against baseline
- Baseline captured on current dataset (17/19 studies passed)
- After data expansion: `python3 -m alert_overlay.regression_runner --compare`
- Flags critical metric changes (Exp, PF, WR, N, MaxDD) with severity markers

### Intraday State Detection (research — pending data confirmation)
- **Problem:** Current regime uses 2 crude inputs (pct_from_open + close position in day range). Labels like RED+CHOPPY hide meaningfully different market states. On days like 2026-03-09, a -2% selloff that recovers to -0.5% reads as RED+CHOPPY the entire time, missing the RECOVERY transition. This blocks valid long entries.
- **Study:** `intraday_state_study.py` — classifies 6 intraday states inside the broad regime envelope:
  - TREND_DOWN, WASHOUT, RECOVERY, SQUEEZE, SIDEWAYS, TREND_UP
- **Inputs:** directional efficiency (6/12 bar), short-term slope (3/6 bar), range position, momentum acceleration, volume ratio
- **Key findings (preliminary, N is small):**
  - RED+CHOPPY contains: 79% SIDEWAYS, 10% RECOVERY, 4% WASHOUT, 3% TREND_DOWN
  - RED+CHOPPY RECOVERY trades: N=4, WR=0%, Exp=-1.222R (actively destructive for longs)
  - RED+TREND TREND_DOWN trades: N=9, WR=67%, Exp=+0.812R (short book prints here)
  - GREEN+TREND RECOVERY trades: N=4, WR=75%, Exp=+1.380R (tradable)
  - Winners have: lower range position, negative efficiency, lower pct_from_open
- **Blocked by:** Sample size. N=4 per state×regime bucket is not actionable. Need expanded dataset to confirm before integrating.
- **Integration path (if confirmed):** Add Layer 1.5 between envelope (L1) and tape permission (L2). State classifier runs on SPY bars, produces a state label. Regime label becomes e.g. RED+RECOVERY instead of RED+CHOPPY. Tape permission thresholds can then vary by state.
- **Do NOT change frozen system until data confirms findings.**
