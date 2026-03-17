# Portfolio D — Promotion Criteria (Variant 2)

## Purpose

Define the exact conditions required to graduate Portfolio D from **candidate** to **production-ready**. No subjective judgment — all criteria are quantitative and pass/fail.

---

## Minimum Sample Requirements

Before any metric evaluation, the forward-test must accumulate:

| Requirement | Threshold | Rationale |
|-------------|-----------|-----------|
| Total trades (combined) | ≥ 50 | Minimum for statistical meaning |
| Long trades | ≥ 25 | Enough to evaluate long book independently |
| Short trades | ≥ 15 | Enough to evaluate short book independently |
| RED+TREND days | ≥ 6 | Short book needs multiple regime samples |
| Calendar days traded | ≥ 30 | Covers varying market conditions |
| Active trading days | ≥ 15 | Days with at least 1 trade |

**Do not evaluate promotion metrics until ALL minimum sample requirements are met.**

---

## Promotion Metrics — ALL Must Pass

### Tier 1: Core Performance (Must Pass)

| Metric | Threshold | Backtest Reference |
|--------|-----------|-------------------|
| Combined PF(R) | ≥ 1.3 | 2.23 |
| Combined Exp(R) | > +0.10R | +0.461R |
| Combined TotalR | > 0 | +31.79R |
| Combined MaxDD(R) | < 15R | 5.29R |
| Combined Stop Rate | < 30% | 14.5% |

### Tier 2: Book-Level Performance (Must Pass)

| Metric | Threshold | Backtest Reference |
|--------|-----------|-------------------|
| Long book Exp(R) | > 0 | +0.414R |
| Short book Exp(R) | > 0 | +0.515R |
| Long book PF(R) | ≥ 1.0 | 1.74 |
| Short book PF(R) | ≥ 1.0 | 4.23 |

### Tier 3: Robustness (Must Pass)

| Metric | Threshold |
|--------|-----------|
| Excl best day: PF(R) | ≥ 1.0 |
| Excl top symbol: PF(R) | ≥ 1.0 |
| ≥ 50% of trading days profitable | Yes |
| ≥ 50% of symbols with positive R | Yes |
| No single symbol > 30% of total R | Yes |
| No single day > 30% of total R | Yes |

### Tier 4: Consistency (Must Pass)

| Metric | Threshold |
|--------|-----------|
| No drawdown exceeding 15R | Yes |
| No 10-trade losing streak | Yes |
| Win rate within ±15% of backtest (59.4%) | 44%–74% |
| Stop rate within ±10% of backtest (14.5%) | 5%–25% |

---

## Deviation Tolerance

The forward-test does NOT need to match backtest exactly. Expected degradation from backtest to live:

| Metric | Acceptable Range | Red Flag |
|--------|-----------------|----------|
| PF(R) | 1.3 – 3.5 | < 1.0 |
| Exp(R) | +0.10R – +0.80R | < 0 |
| WR | 44% – 74% | < 40% |
| Stop Rate | 5% – 25% | > 30% |
| MaxDD(R) | 0 – 15R | > 15R |

---

## Promotion Decision Process

### Step 1: Sample Check
Are all 6 minimum sample requirements met? If no → keep collecting data.

### Step 2: Tier 1–4 Evaluation
Run the live_vs_backtest comparison script. All tiers must pass.

### Step 3: Decision

| Outcome | Action |
|---------|--------|
| All tiers pass | PROMOTE to production |
| Tier 1 fails | STOP. System does not have edge. Demote to research. |
| Tier 2 fails (one book) | PAUSE that book. Continue other book. Investigate. |
| Tier 3 fails | WARNING. Continue collecting data. Re-evaluate at 2× sample. |
| Tier 4 fails | WARNING. Check for regime shift or execution issues. |

### Step 4: Post-Promotion
After promotion, continue logging. If any Tier 1 metric degrades below threshold over a rolling 50-trade window, demote back to candidate and investigate.

---

## What Promotion Does NOT Allow

- Changing any frozen rule (entry, stop, exit, regime, filters)
- Adding new setups
- Increasing risk per trade
- Removing the paper-trade log requirement

Promotion means: "this system has demonstrated a real R-edge in forward testing and can be traded with real capital at the current fixed risk level."

---

## Tracking Template

Update weekly during Friday review:

```
Week ending: ____
Trades this week: Long ___ / Short ___
Cumulative trades: Long ___ / Short ___
RED+TREND days this week: ___
Cumulative RED+TREND days: ___
Current PF(R): ___
Current Exp(R): ___
Current TotalR: ___
Current MaxDD(R): ___
Sample requirements met? [ ] Yes [ ] No
Promotion metrics evaluated? [ ] Yes [ ] No [ ] N/A (sample not met)
Status: [ ] Collecting data [ ] Ready to evaluate [ ] PROMOTED [ ] DEMOTED
```
