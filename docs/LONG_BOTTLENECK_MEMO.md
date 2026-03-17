# Long-Side Bottleneck Memo
**Date:** 2026-03-10
**Probe signal:** Simple VWAP reclaim long (identical across all tests)
**Universe:** 64 symbols, 62 trading days (Dec 8 2025 – Mar 9 2026)

---

## Diagnosis: The bottleneck is REGIME, with partial contributions from HABITAT.

Horizon plays almost no role. The long edge is present on GREEN SPY days and absent otherwise. No exit mode fixes it.

---

## Evidence

### 1. REGIME is the dominant factor

The same probe signal, same universe, same exit — split only by SPY day color:

| Regime | Trades | WR | PF(R) | Exp(R) | TotalR |
|--------|--------|----|-------|--------|--------|
| GREEN (SPY >+0.15%) | 1,122 | 37.3% | **1.03** | +0.018 | **+20.3** |
| FLAT | 727 | 29.3% | 0.71 | -0.214 | -155.3 |
| RED (SPY <-0.15%) | 842 | 20.4% | 0.44 | -0.469 | **-394.5** |

GREEN longs are ~breakeven. RED longs are catastrophic. The full-sample negative result is entirely driven by RED and FLAT days.

The sample period is: 40% GREEN, 23% FLAT, 37% RED — an almost even split, not a biased sample. December and January were net bearish; February was mixed; March (5 days) was strongly green.

**Monthly breakdown (EOD exit):**
| Month | Trades | PF | TotalR |
|-------|--------|----|--------|
| Dec 2025 | 725 | 0.57 | -240 |
| Jan 2026 | 848 | 0.60 | -268 |
| Feb 2026 | 803 | 0.74 | -148 |
| Mar 2026 | 315 | **1.79** | **+126** |

March is the only month with a tradeable long edge. The first two months are devastating.

### 2. HABITAT matters but doesn't save the trade

High-RVOL names (≥1.5x, proxy for in-play) consistently outperform low-RVOL static names:

| Group | PF (eod) | Exp(R) | TotalR |
|-------|----------|--------|--------|
| High RVOL | 0.81 | -0.132 | -229 |
| Low RVOL | 0.59 | -0.315 | -300 |

High-RVOL longs are ~40% less negative per trade — a real difference, but still net negative. Habitat improves but doesn't fix the core problem.

### 3. HORIZON doesn't matter

All exit modes produce nearly identical PF and expectancy:

| Exit Mode | PF | Exp(R) | TotalR |
|-----------|-----|--------|--------|
| Hold to close | 0.73 | -0.197 | -530 |
| 2R target | 0.73 | -0.185 | -499 |
| 3R target | 0.73 | -0.190 | -510 |
| Hold to next open | 0.71 | -0.252 | -679 |
| Hybrid EMA9 trail | 0.53 | -0.197 | -531 |

PF stays 0.71-0.73 regardless of whether you hold for 10 minutes or overnight. The overnight hold is actually worse. The EMA9 trail kills WR without improving edge. Changing hold period cannot fix the underlying signal quality problem.

### 4. BEST-CASE stack is still not viable

GREEN day + high RVOL + RS > 0 (triple-filtered):

| Exit | Trades | PF | Exp(R) | TotalR |
|------|--------|----|--------|--------|
| EOD | 695 | 0.98 | -0.011 | -7.3 |
| 3R target | 695 | 0.98 | -0.010 | -7.0 |

Even with every favorable filter stacked, PF barely touches 0.98 — still negative. 695 trades at PF 0.98 is statistically indistinguishable from random.

---

## What This Means

1. **No 5-min long setup on the static 64-symbol watchlist will produce a deployable edge in RED or FLAT market regimes.** This is a structural finding, not a trigger design problem.

2. **GREEN-day-only longs are breakeven at best.** Even with perfect regime identification, the probe hits PF 1.03 — not enough to survive real-world costs with any confidence.

3. **Habitat helps but can't compensate.** High-RVOL names are structurally better for longs, but the improvement (~40% better expectancy) is from "terrible" to "bad."

4. **The existing SC long (Q≥5, PF 1.19) is surprisingly good** given this backdrop. It survives because the quality gate (Q≥5) selects only the highest-conviction breakout retests — a much narrower filter than any generic long probe. There may not be a materially better long approach in this data.

---

## Concrete Plan: Next Long-Side Test Branch

### Priority 1: TRUE IN-PLAY CATALYST UNIVERSE

**Why:** Habitat is the only factor that showed partial uplift. The static watchlist mixes good and bad names. A daily-filtered universe of actual catalyst names might concentrate enough edge to flip PF above 1.0.

**What to build:**
- Morning scanner that identifies true in-play names by: overnight gap > 2%, first-15-min dollar volume in top decile, news/catalyst presence
- Run long probes ONLY on scanner-selected names for each day
- Compare PF against the static universe

**Blocker:** We need historical in-play lists per day. Options: (a) reconstruct from opening volume/gap data already in the CSVs, (b) pull historical scanner results from IBKR, (c) simulate a gap+volume screen retrospectively.

**Recommendation:** Build a retrospective in-play classifier using gap + first-bar RVOL from existing data. Test long probes on that filtered universe. This is the single most likely path to a deployable long edge.

### Priority 2: GREEN-DAY-ONLY SC LONG

**Why:** SC already works. GREEN days are the only regime where longs aren't destroyed. Stack them.

**What to build:**
- Re-run SC long backtest with a GREEN-day-only gate (SPY pct_from_open > +0.15% at signal time)
- Check whether the narrower universe improves PF from 1.19 to something more convincing
- Live-available: use real-time SPY pct_from_open (already in the engine)

**Risk:** May reduce SC trade count below useful levels.

### Priority 3: LONGER DATA PULL

**Why:** March 2026 showed PF 1.79 on the probe. December/January were hostile. 62 days is too short to know the true long-side base rate.

**What to build:**
- Pull 6-12 months of 5-min data from IBKR (requires TWS connection + weekend pull)
- Re-run the bottleneck study on the larger sample
- Determine whether the Dec-Jan destruction was anomalous or structural

### Do NOT pursue:
- More 5-min trigger variants on the static watchlist (exhaustively tested, all fail)
- Longer hold periods (no edge improvement demonstrated)
- Overnight holds (worse, not better)
