# Portfolio D — v2 Strategy Integration Report

## 1. Portfolio Metrics Comparison

All metrics computed on the 1-min aligned date range (Sep 24 2025 → Mar 6 2026, ~124 trading days).

| Configuration | N | PF | Exp/trade | Total R | WR | MaxDD | Days Active | Trades/Day |
|---|---|---|---|---|---|---|---|---|
| Portfolio (9 strats) | 173 | 1.70 | +0.209 | +36.1 | 35.3% | 9.2R | 45 | 3.84 |
| **Portfolio + ORH Mode A** | **189** | **1.74** | **+0.233** | **+44.0** | **36.5%** | **9.2R** | **56** | **3.38** |
| Portfolio + Mode A + B | 207 | 1.69 | +0.220 | +45.6 | 36.2% | 9.2R | 63 | 3.29 |
| ORH Mode A (standalone) | 16 | 1.99 | +0.493 | +7.9 | 50.0% | 2.0R | 11 | 1.45 |
| ORH Mode B (standalone) | 18 | 1.23 | +0.090 | +1.6 | 33.3% | 3.0R | 14 | 1.29 |

**Verdict:** Adding Mode A raises portfolio PF from 1.70 → 1.74, adds +7.9R, increases active days from 45 → 56, and does not increase MaxDD. Adding Mode B on top gives marginal +1.6R but slightly lowers PF (1.74 → 1.69) due to Mode B's weaker edge.

## 2. Overlap / Correlation Analysis

**ORH Mode A fires on days with ZERO existing portfolio trades.** The diversification is structural, not statistical:

- Portfolio: 177 trades on GREEN days, 1 trade on RED days (99.4% GREEN)
- ORH Mode A: 16 trades, ALL on RED days (100% RED)
- Same-day same-symbol overlap: 0 trades
- Same-day any-symbol overlap: 0 trades
- ORH Mode A unique-day trades: 16/16 (100%)

This means ORH Mode A doesn't compete with or dilute the existing portfolio. It fills dead days.

**Daily PnL direction correlation:** Not measurable because there are zero overlapping trading days.

## 3. Strategy Status Labels

| Strategy | Status | Direction | TF | Regime | Family | PF | N | R |
|---|---|---|---|---|---|---|---|---|
| SC_SNIPER | ACTIVE | LONG | 5m | GREEN | breakout_retest | 1.21 | 25 | +3.0 |
| FL_ANTICHOP | ACTIVE | LONG | 5m | GREEN | trend_turn | 1.34 | 111 | +12.9 |
| SP_ATIER | ACTIVE | LONG | 5m | GREEN | consolidation | 1.19 | 13 | +1.0 |
| HH_QUALITY | ACTIVE | LONG | 5m | GREEN | momentum | 1.64 | 46 | +7.0 |
| EMA_FPIP | ACTIVE | LONG | 5m | GREEN | pullback | 2.00 | 24 | +6.0 |
| BDR_SHORT | ACTIVE | SHORT | 5m | RED/FLAT | breakdown | inf | 1 | +0.0 |
| BS_STRUCT | ACTIVE | LONG | 5m | GREEN | structure | inf | 10 | +10.0 |
| EMA9_FT | ACTIVE | LONG | 1m | GREEN | pullback | 2.00 | 4 | +1.0 |
| ORL_FBD_LONG | ACTIVE | LONG | 5m | GREEN | failed_move_long | 1.49 | 36 | +3.0 |
| **ORH_FBO_V2_A** | **PAPER** | SHORT | 1m | RED/FLAT | failed_bo_short | 1.99 | 16 | +7.9 |
| **ORH_FBO_V2_B** | **PROBATIONARY** | SHORT | 1m | RED/FLAT | failed_bo_short | 1.23 | 18 | +1.6 |
| ORL_FBD_V2 | SHELVED | LONG | 1m | GREEN | failed_move_long | 0.64 | 27 | -3.6 |
| ORH_FBO_V1 | SHELVED | SHORT | 5m | any | failed_bo_short | 0.86 | 16 | -1.2 |
| NEUTRAL_VWAP_FADE | DEFERRED | — | — | FLAT | mean_reversion | — | — | — |

Registry implemented in `strategies/shared/strategy_registry.py`.

## 4. Failed-Breakout-Short Family Expansion Design

### The Opportunity Problem

ORH Mode A is the strongest short setup in the portfolio (PF=1.99, 50% WR, 0% time exits). But N=16 over 6 months (~2.7 trades/month) is too sparse to materially solve the portfolio's RED-day exposure gap. The portfolio currently has 0 active trading days on RED days beyond what ORH provides.

### The Thesis (preserved exactly)

A quality breakout above an **objective, widely-watched resistance level** traps breakout longs. When price fails quickly back below that level, those longs are trapped and selling pressure creates a directional short. Entry only after structural confirmation (retest rejection or continuation).

**Key constraint:** The level must be objective (not fitted) and the trapped-participant mechanism must be real (not synthetic). This rules out arbitrary swing highs, computed levels, or moving averages.

### Candidate Levels Ranked by Defensibility

#### Tier 1: PDH Failed Breakout Short (RECOMMENDED NEXT)

**Level:** Prior Day High — objective, universally watched, plotted on every institutional chart.

**Thesis validity:** Strong. PDH is the most watched resistance after ORH. Breakout above PDH traps the same participant class (breakout longs). Failed PDH breakouts are a well-known institutional pattern.

**Data assessment:**
- PDH > ORH on 60% of trading days across 20 symbols — so PDH is a distinct level from ORH on the majority of days
- Estimated universe: ~5,900 symbol-days where PDH test is possible (vs ~7,800 for ORH)
- PDH is computable from existing 5-min data (prior day's bars) with zero new data requirements

**Architecture:** Identical to ORH v2 hybrid — 5-min context (PDH level, structure), 1-min sequencing (break, fail, retest/continuation). Same state machine, same Mode A/B structure. Only the level source changes.

**Regime gate:** Same RED/FLAT-only gate.

**Expected N uplift:** If PDH-FBO fires at even 50% the rate of ORH-FBO (given the level is tested less often since it's further away), that's ~8 additional trades over 6 months. Combined family: ~24 trades.

**Risk:** PDH breakouts may fail less cleanly than ORH breakouts because PDH is further from the opening range and trapped-participant dynamics may be weaker. This must be validated empirically, not assumed.

**Implementation cost:** Low — clone `orh_fbo_short_v2.py`, replace `or_high` with prior-day-high lookup. ~2 hours of work.

#### Tier 2: ORH + PDH Confluence Failed Breakout Short

**Level:** Days where ORH and PDH are within 0.15 ATR of each other (confluence zone).

**Thesis validity:** Very strong. Two independent resistance levels at the same price means more participants are watching, more longs get trapped, and the failure is more violent. This is the highest-conviction variant.

**Data assessment:**
- ORH and PDH converge on ~15-25% of days (estimated from the 60% PDH > ORH stat)
- Very low N expected — maybe 3-5 trades over 6 months
- But each trade should have the highest PF of the family

**Architecture:** Requires checking both ORH and PDH proximity at breakout time. Straightforward filter on top of either ORH or PDH strategy.

**Recommendation:** Build as a **quality filter/tag** on the PDH strategy, not as a separate strategy. When ORH ≈ PDH, the signal gets a confluence bonus that increases quality score and conviction.

#### Tier 3: PMH Failed Breakout Short (DEFERRED — DATA LIMITATION)

**Level:** Premarket High — the highest price traded before the opening bell.

**Thesis validity:** Moderate. PMH traps a different participant class (premarket/extended-hours traders). The failure mechanism is real but weaker because premarket volume is thin and the level is less universally watched.

**Data assessment:**
- 1-min data has inconsistent premarket coverage (some days start at 9:30, others at 8:55 or 9:04)
- PMH cannot be reliably computed from current data
- Would require a dedicated premarket data feed

**Recommendation:** Defer until consistent premarket data is available. Do not attempt to synthesize PMH from partial data.

#### Tier 4: Weekly High Failed Breakout Short (ASSESS LATER)

**Level:** Prior week's high — a swing-level analog of PDH.

**Thesis validity:** Weaker. Weekly highs are less precisely watched intraday, and the trapped-participant mechanism operates on a different timescale. More of a swing trading level.

**Recommendation:** Assess after PDH is built and validated. This would likely need a wider time window and different exit mechanics than the intraday family.

### Recommended Implementation Order

1. **PDH_FBO_SHORT v1** — clone ORH v2 architecture, swap level. Build replay + evaluate. Expect 1-2 sessions of work.
2. **ORH+PDH confluence tag** — add to both ORH and PDH strategies as a quality boost, not a separate strategy. Minimal work.
3. **PMH_FBO_SHORT** — only when premarket data is reliable.
4. **Weekly high** — only if PDH validates the thesis at the daily level.

### What NOT to Do

- Do not loosen ORH Mode A parameters to increase N. The 50% WR and 0% time exits are the edge.
- Do not combine Mode A and Mode B into a single signal type. They have structurally different risk profiles.
- Do not add synthetic levels (VWAP, moving averages, computed pivots). These don't trap participants the same way.
- Do not expand to other timeframes (daily, weekly) until the intraday PDH variant is validated.
