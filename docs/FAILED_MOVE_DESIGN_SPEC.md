# Design Spec: Three New Strategy Modules

**Date:** 2026-03-12
**Status:** Design checkpoint — not approved for implementation

---

## 1. Repo Fit Summary

### Existing architecture

The codebase has two parallel systems:

- **Replay framework** (`strategies/*.py`): 8 replay strategies, each a `scan_day()` class using the 6-step pipeline (in-play → regime → detect → reject → quality → A-tier). Used for backtesting against 101 symbols × 210 days of 5-min CSV data.
- **Live framework** (`strategies/live/*.py`): 8 `LiveStrategy` subclasses with incremental `step(snap)` methods, fed by `StrategyManager` with dual-timeframe routing (1-min and 5-min).

Both share: `StrategyConfig`, `InPlayProxy`, `EnhancedMarketRegime`, `RejectionFilters`, `QualityScorer`, and helpers in `strategies/shared/`.

### Structurally closest existing strategies

| New strategy | Closest existing | Why |
|---|---|---|
| PMH_ORH_FailedBreakout_Short | **SC_SNIPER** (inverted) + **BDR_SHORT** | SC has breakout→retest→confirm state machine. BDR has short-side regime, rejection wick logic, level-finding. The new strategy is essentially "SC_SNIPER but the breakout fails and you short the reclaim back below." |
| ORL_PML_FailedBreakdown_Reclaim_Long | **BDR_SHORT** (inverted) + **SC_SNIPER** | BDR has breakdown→retest→rejection. The new strategy is "BDR but the breakdown fails and you go long on the reclaim back above." |
| Neutral_RangeEdge_VWAP_Rejection_Fade | **FL_ANTICHOP** (partial) + **BS_STRUCT** (partial) | FL tracks VWAP reconnection. BS tracks structure formation relative to VWAP. But neither is a pure neutral-market fade. This would be genuinely new logic. |

### Shared indicators already available (via IndicatorSnapshot)

- `or_high`, `or_low`, `or_ready` — Opening range (first 30 min)
- `session_high`, `session_low`, `session_open` — Running session extremes
- `ema9`, `ema20`, `vwap` — Session-continuous EMAs, session-local VWAP
- `atr` (5m), `atr_1m`, `daily_atr` — Three ATR timeframes
- `vol_ma_5m`, `vol_ma_1m` — Volume MA for both timeframes
- `recent_bars`, `recent_bars_1m` — Rolling bar windows
- `prior_day_high`, `prior_day_low` — Set from `warm_up_daily()`
- `hhmm`, `bar_idx_5m` — Time and bar count

### What does NOT exist

- **Premarket high/low**: No premarket data in CSV files. In live mode, IBKR `reqHistoricalData` with `useRTH=True` excludes premarket. Premarket levels would require either `useRTH=False` or a separate premarket data pull.
- **Attempt counting**: No shared helper for "how many times has price poked above/below a level." Each strategy implements its own.
- **Acceptance/failure timing**: No shared helper for "how long did price stay above/below a level before returning."
- **Level significance scoring**: Each strategy has its own `_find_key_level()` or `_find_support_level()`. No shared abstraction.

### Where these should live

- Replay: `strategies/pmh_failed_bo_short.py`, `strategies/orl_failed_bd_long.py`, `strategies/neutral_vwap_fade.py`
- Live: `strategies/live/pmh_fbo_short_live.py`, `strategies/live/orl_fbd_long_live.py`, `strategies/live/nvf_live.py`
- Shared: `strategies/shared/level_helpers.py` (new — shared level logic for the failed-move family)

### Runtime concern

None. Current 8 strategies process a 5-min bar in <200μs total. Adding 3 more state machines adds ~75μs. The bottleneck is IBKR tick throughput, not strategy processing.

---

## 2. Shared Family Design: FAILED MOVE

Strategies 1 and 2 share a common structural template:

```
Phase 0: Level identification (which level is being tested)
Phase 1: Impulse (a real breakout/breakdown attempt with conviction)
Phase 2: Failure (price fails to achieve acceptance, returns through level)
Phase 3: Trigger (confirmation of failed move — entry bar)
```

### Shared concepts that should be extracted into `level_helpers.py`

**1. Level proximity check**
```
is_near_level(price, level, atr, tolerance_atr=0.10) → bool
```
Reusable for both strategies and for the neutral fade's edge detection.

**2. Impulse quality scoring**
```
impulse_quality(bar, atr, vol_ma) → float (0.0-1.0)
```
Composite of: distance through level in ATR, bar body ratio, volume vs average. Already partially exists as `trigger_bar_quality()` but that scores the entry bar, not the breakout bar.

**3. Acceptance window check**
```
bars_above_level(recent_bars, level) → int
bars_below_level(recent_bars, level) → int
```
Count how many consecutive bars stayed above/below a level. Used to distinguish "poked through and immediately failed" (1-2 bars) from "held above for a meaningful period" (too late for failed-move trade).

**4. Attempt counter**
```
count_attempts(recent_bars, level, direction, atr) → int
```
How many times has price attempted to break through this level? First attempt failures are highest quality. Third+ attempts are lower quality (level is getting stale).

### What remains strategy-specific

- Level selection (which levels are eligible) — different for each strategy
- Direction and regime rules
- Stop/target logic
- Reclaim vs rejection trigger bar requirements

---

## 3. Strategy 1: PMH_ORH_FailedBreakout_Short

### 3.1 Strategy thesis

Intraday breakouts above significant overhead levels frequently fail. When a stock breaks above the opening range high or prior day high, buyers commit capital above the level. If price fails to hold and reclaims back below, those buyers are trapped long. Their forced exits (stops and capitulation sells) create a predictable move back toward VWAP. This strategy enters short after the failed breakout is confirmed, targeting trapped-buyer liquidation flow toward VWAP.

### 3.2 Exact intended market/environment

- **Ideal environment**: FLAT or mildly RED day on SPY. Market is not providing lift. Breakout attempts are more likely to fail without broad market support.
- **Acceptable environment**: GREEN day, but SPY has stalled or is pulling back from its own high (SPY below its own session VWAP at signal time). Individual stock breakouts can fail even on green days when the broader market isn't supporting continuation.
- **Blocked environment**: SPY strongly trending up (SPY above VWAP AND EMA9 > EMA20 AND SPY pct_from_open > +0.30%). In a strong trending market, breakouts above resistance tend to hold. The failure rate is too low to justify the short.

**Operationalized regime gate:**
```
allow_signal = NOT (spy_above_vwap AND spy_ema9_above_ema20 AND spy_pct_from_open > 0.003)
```
This is NOT the old `regime_require_green`. It's a conditional block on the strongest bull configuration only.

### 3.3 Exact eligible stock profile

Same in-play proxy as existing strategies. The stock must be active (gap + rvol + dolvol thresholds). No change needed to `InPlayProxy`.

### 3.4 Exact level/reference types allowed (v1)

**Only two levels for v1:**

1. **Opening Range High (ORH)**: `snap.or_high` after `snap.or_ready == True`. This is the single most structurally significant overhead level available in our data. OR is a well-established institutional reference.

2. **Prior Day High (PDH)**: `snap.prior_day_high` (already set via `warm_up_daily()`). Only eligible if price has traded near it intraday (within 1.0 ATR at some point during the session). A PDH that's 5 ATR above the day's range is not "active."

**Why not Premarket High:** No premarket data in backtest CSVs. Cannot be backtested. Could be added as a live-only enhancement in v2 if ORH + PDH prove the thesis. Not designed into v1.

**Why not swing highs or EMA levels:** Too ambiguous. "Swing high" requires defining lookback, prominence, and recency — each adds a free parameter. ORH and PDH are objective, universally recognized, and mechanically unambiguous.

### 3.5 Exact sequence of events required

**Phase 0: Level identification**
- OR must be ready (hhmm > 1000, at least 6 five-min bars of OR data)
- At least one eligible level (ORH or PDH) must exist within reachable range (price is within 2.0 daily ATR of the level)

**Phase 1: Breakout above level**
- Bar closes above level + `break_clearance_atr` × intra_ATR (min 0.05 ATR — must clear the level, not just poke)
- Bar must be bullish (close > open)
- Bar range >= 0.50 × median(last 10 bar ranges) — not a tiny doji
- Bar volume >= 0.80 × vol_ma — must have some conviction (note: lower threshold than SC_SNIPER's 1.25× because we're looking for "real enough to trap buyers" not "strong enough to hold")
- Close in upper 60% of bar range

**Phase 2: Failure (within failure_window bars, default 6 on 5-min)**
- Price fails to achieve acceptance above the level
- Acceptance failure defined as: price closes back below the level within the failure window
- The reclaim bar must be bearish (close < open) OR the close must be meaningfully below the level (> 0.05 ATR below)
- The high of the failure window must not have extended more than 1.5 ATR above the level (if it ran 2+ ATR above before failing, the trade thesis weakens — this is more of an extended move that reversed, not a failed breakout)

**Phase 3: Trigger (within confirm_window bars after failure, default 3 on 5-min)**
- First bar that confirms the failure
- Requirements:
  - Close below the level (still below — not a re-breakout)
  - Bar is bearish (close < open) — momentum confirming down
  - Body ratio >= 0.35 (not a doji)
  - Close below EMA9 OR EMA9 is below the level (structural weakness confirmed)
- Entry at the close of the trigger bar

### 3.6 Exact trigger options allowed

Only one trigger type for v1: **bearish bar closing below the level after reclaim**. No "failed retest from below" variant — that adds a fourth phase and significantly more parameters. Keep it simple. If v1 shows promise, a retest variant can be explored in v2.

### 3.7 Exact stop logic candidates

**Primary (v1):** Stop above the highest high recorded during the breakout + failure window, plus a buffer.

```
stop = max_high_during_phases_1_2 + stop_buffer_atr × intra_ATR
```

Default `stop_buffer_atr` = 0.20. This is the invalidation point — if price reclaims back above the failed breakout's highs, the thesis is dead.

**Minimum stop distance:** 0.40 × intra_ATR. Prevents unrealistically tight stops on low-volatility bars.

### 3.8 Exact target logic candidates

**Primary target:** VWAP.

```
target = snap.vwap
```

VWAP is the natural magnet for failed-move reversion. It represents fair value for the session. Trapped buyers exiting above the level will push price toward VWAP.

**Constraint:** VWAP must be at least 0.50 ATR below entry price. If VWAP is too close, there's no room-to-target and the risk/reward is unacceptable.

**Fallback:** If VWAP is too close, use fixed 1.5R target:
```
target = entry - risk × 1.5
```

### 3.9 Exact invalidation conditions

- Price reclaims back above the breakout level AND closes above it for 2 consecutive bars after Phase 2 failure was detected → abort, the failure was false
- Price makes a new high above Phase 1 breakout high → abort
- Max bars from Phase 1 to trigger: 12 (on 5-min). If trigger hasn't occurred within ~1 hour of the breakout, the pattern is stale.
- Time gate: no new Phase 1 detections after 14:00. Exits still allowed.
- One signal per level per day (one_and_done per level, but can fire on both ORH and PDH if both set up)

### 3.10 Exact avoid/reject conditions

- Skip if stock is already down > 2.0 ATR from session high at the time of Phase 1 (the breakout is happening from weakness — different thesis)
- Skip if OR range > 3.0 × daily_ATR (extremely wide OR suggests high volatility / news day — levels lose significance)
- Skip if volume on breakout bar is > 3.0 × vol_ma (massive volume breakouts are more likely to hold)
- Apply universal choppiness filter (existing `RejectionFilters.choppiness`)
- Skip trigger_weakness filter (the trigger bar is bearish by definition — existing filter is long-biased)
- Skip bigger_picture filter (existing filter rejects shorts below VWAP — but that's exactly where we want to be)
- Skip distance filter (we want extended-from-VWAP entries for shorts fading back to VWAP)

### 3.11 Existing shared framework to reuse

| Component | Reuse? | Notes |
|---|---|---|
| InPlayProxy | Yes, as-is | Same in-play gate |
| EnhancedMarketRegime | Partially | Need custom `is_aligned_failed_bo_short()` — not the existing GREEN-only or RED-only gates |
| RejectionFilters | Partially | Use choppiness only; skip bigger_picture, distance, trigger_weakness |
| QualityScorer | Yes, with inverted regime scoring | Same as BDR_SHORT: RED=1.0, FLAT=0.5, GREEN=0.0 for shorts |
| trigger_bar_quality() | Yes | For the entry trigger bar scoring |
| bar_body_ratio() | Yes | Phase 1 and 3 bar checks |
| is_in_time_window() | Yes | Time gating |
| simulate_strategy_trade() | Yes, with direction=-1 | For replay trade simulation |

### 3.12 New helpers needed

| Helper | Location | Purpose |
|---|---|---|
| `bars_above_level()` | `level_helpers.py` | Count consecutive bars with close above level (acceptance check) |
| `impulse_quality()` | `level_helpers.py` | Score breakout/breakdown bar quality (distance through level, body ratio, volume) |
| `count_attempts()` | `level_helpers.py` | Count prior attempts to break a level (first attempt = highest quality) |
| `is_pdh_active()` | `level_helpers.py` | Check if prior day high is "active" (price has been within 1.0 ATR during session) |
| `is_aligned_failed_move_short()` | New method on `EnhancedMarketRegime` | Custom regime gate: block only on strongest bull config |

### 3.13 Highest risk for ambiguity / overfitting

1. **Failure window size (Phase 2):** 6 bars = 30 minutes. This is a judgment call. Too tight (3 bars) misses legitimate failures that take 20 minutes to develop. Too wide (12 bars) includes moves that held above the level long enough to suggest genuine acceptance. **Risk: medium. Mitigated by:** keeping it fixed at 6, not optimizing.

2. **"Close below level" as failure signal:** On 5-min bars, a single bar closing 1 cent below the level could trigger Phase 2. That's noise. The `break_clearance_atr` threshold on Phase 1 helps (it had to close meaningfully above), but the failure detection itself needs the minimum distance check (0.05 ATR below level). **Risk: medium.**

3. **Breakout volume threshold too low:** We set 0.80 × vol_ma (lower than SC's 1.25×). This risks capturing breakouts that had no conviction and were never going to hold anyway — those aren't trapping anyone. **Risk: low.** If the breakout had zero conviction, there are no trapped buyers, and the trade just becomes random noise. The fix is to raise this if backtest shows excessive signal count with poor edge. Start conservative at 0.80 and observe.

4. **PDH "active" check:** The 1.0 ATR proximity threshold is somewhat arbitrary. **Risk: low.** PDH is a secondary level. The strategy works primarily on ORH. PDH adds marginal signal count.

### 3.14 5-minute vs 1-minute resolution risk

**5-min is sufficient for v1.** The phases are multi-bar by design:
- Phase 1 (breakout): A 5-min bar that closes above the level captures the breakout.
- Phase 2 (failure): 6 bars = 30 minutes — plenty of resolution.
- Phase 3 (trigger): The confirmation bar is a complete 5-min bar.

**Where 1-min would improve fidelity:**
- Phase 2 precision: On 5-min bars, a bar might open above the level, trade below it mid-bar, and close above. The 5-min bar looks like the level held, but on 1-min you'd see it failed. This creates false negatives (missed failures). Not false positives.
- Stop precision: The stop is placed above the highest high during phases 1-2. On 5-min, this includes any mid-bar wick that might have been a single tick above. Stops may be slightly wider on 5-min. Acceptable.

**1-min refinement would improve the strategy but is not required for v1 validation.**

### 3.15 Priority recommendation

**HIGH PRIORITY.** This is the most specific, narrowest, and most defensible of the three. The thesis is clear (trapped buyers), the levels are objective (ORH, PDH), the state machine is tractable (4 phases with clear transitions), and the existing infrastructure supports it well. BDR_SHORT's architecture is directly reusable as a template.

The main question is whether 5-min data resolution is sufficient to reliably detect failed breakouts. The backtest will answer that.

### 3.16 Why this might still fail

- **5-min bar resolution may blur Phase 2 too much.** A breakout that fails within 2-3 minutes might look like a single ambiguous 5-min bar. The state machine might either miss it or trigger on noise. If the backtest shows N < 5 or N > 50, resolution is likely the issue.
- **No premarket high means the strongest version of this trade is untestable.** PMH is arguably the single most significant overhead level for failed-breakout shorts. By limiting to ORH + PDH, we're testing a weaker variant. If ORH + PDH produce edge, PMH will likely amplify it in live. If they don't, we can't conclude the thesis is wrong — only that these specific levels don't capture it on 5-min bars.
- **The regime gate may be too permissive.** Allowing this on GREEN days (if SPY has pulled back) might let in too many signals where the breakout was actually correct and just needed time. The regime gate needs to be tested as a variable, not treated as fixed.
- **Overfitting risk on failure_window and confirmation requirements.** With ~210 trading days and 88 symbols, there may be only 15-30 raw signals. That's a small sample to train/test split on.
- **PDH level reliability.** PDH is set from a single `warm_up_daily()` call. If the stock gapped significantly, yesterday's high may be irrelevant. The "active" check mitigates this, but imperfectly.

---

## 4. Strategy 2: ORL_PML_FailedBreakdown_Reclaim_Long

### 4.1 Strategy thesis

The mirror of Strategy 1. When a stock breaks below the opening range low or prior day low, shorts commit capital below the level. If price fails to continue lower and reclaims back above the level, those shorts are trapped. Their forced covering creates a predictable squeeze back toward VWAP. This strategy enters long after the failed breakdown is confirmed, targeting trapped-short covering flow toward VWAP.

This is NOT generic dip-buying. It requires:
1. A specific level break (not just "price is low")
2. A genuine failure to continue (not just a bounce)
3. A structural reclaim back above the level (not just a wick)
4. Confirmation that the reclaim is holding

### 4.2 Exact intended market/environment

- **Ideal environment**: FLAT or mildly GREEN day. Market is not collapsing. Breakdown attempts are more likely to fail when the broader market is providing a floor.
- **Acceptable environment**: RED day, but SPY has stabilized or is bouncing (SPY above its session low by at least 0.15% AND SPY bar-level pct_from_open > -0.50%). A failed breakdown reclaim on a red day is a higher-conviction signal — it's happening against the grain.
- **Blocked environment**: SPY strongly trending down (SPY below VWAP AND EMA9 < EMA20 AND SPY pct_from_open < -0.50%). In a strong downtrend, breakdowns below support tend to continue. The failure rate is too low.

**Operationalized regime gate:**
```
allow_signal = NOT (NOT spy_above_vwap AND NOT spy_ema9_above_ema20 AND spy_pct_from_open < -0.005)
```

### 4.3 Exact eligible stock profile

Same in-play proxy as existing strategies. No changes.

### 4.4 Exact level/reference types allowed (v1)

**Only two levels for v1:**

1. **Opening Range Low (ORL)**: `snap.or_low` after `snap.or_ready == True`. Mirror of ORH for Strategy 1.

2. **Prior Day Low (PDL)**: `snap.prior_day_low`. Same "active" check as Strategy 1 — price must have traded within 1.0 ATR of PDL during the session.

**Why not Premarket Low:** Same as Strategy 1. No premarket data in CSV.

### 4.5 Exact sequence of events required

**Phase 0: Level identification**
- OR must be ready (hhmm > 1000)
- At least one eligible level (ORL or PDL) within reachable range

**Phase 1: Breakdown below level**
- Bar closes below level - `break_clearance_atr` × intra_ATR (min 0.05 ATR below)
- Bar must be bearish (close < open)
- Bar range >= 0.50 × median(last 10 bar ranges)
- Bar volume >= 0.80 × vol_ma
- Close in lower 60% of bar range

**Phase 2: Failure + Reclaim (within failure_window bars, default 8 on 5-min)**

This phase is deliberately longer than Strategy 1's (8 bars vs 6). Reason: breakdowns into support tend to generate more churn and basing before reclaiming. A breakout above resistance either fails fast or holds. A breakdown below support can grind, build a base, and then reclaim. The wider window accommodates this.

- Price closes back above the level within the failure window
- The reclaim bar must be bullish (close > open) OR close must be meaningfully above the level (> 0.05 ATR above)
- The low of the failure window must not have extended more than 2.0 ATR below the level (if it cratered 3 ATR below before bouncing, this is a V-bottom, not a failed breakdown)

**Phase 3: Trigger (within confirm_window bars after reclaim, default 4 on 5-min)**

Wider confirmation window than Strategy 1 (4 bars vs 3). Reason: on the long side, we want to see the reclaim hold for slightly longer before committing. A single bar closing above the level could be a dead cat bounce.

- Requirements:
  - Close above the level (still above — not re-breaking down)
  - Bar is bullish (close > open) — momentum confirming up
  - Body ratio >= 0.35
  - Close above EMA9 OR EMA9 is above the level (structural strength confirmed)
  - **Additional: close above VWAP OR within 0.20 ATR of VWAP** (unlike Strategy 1, the long side benefits from VWAP proximity at entry, not distance from VWAP)
- Entry at the close of the trigger bar

### 4.6 Exact trigger options allowed

Only one for v1: bullish bar closing above the level after reclaim. No "higher-low retest" variant yet.

### 4.7 Exact stop logic candidates

**Primary (v1):**
```
stop = min_low_during_phases_1_2 - stop_buffer_atr × intra_ATR
```
Default `stop_buffer_atr` = 0.20.

**Minimum stop distance:** 0.40 × intra_ATR.

### 4.8 Exact target logic candidates

**Primary target:** VWAP (same logic as Strategy 1 but directionally inverted).

```
target = snap.vwap
```

**Constraint:** VWAP must be at least 0.50 ATR above entry price. If VWAP is too close (stock is already near VWAP), use session midpoint or 1.5R fixed:
```
target = entry + risk × 1.5
```

### 4.9 Exact invalidation conditions

- Price breaks back below the level AND closes below it for 2 consecutive bars after Phase 2 reclaim → abort
- Price makes a new low below Phase 1 breakdown low → abort
- Max bars from Phase 1 to trigger: 15 (on 5-min, wider than Strategy 1 due to longer basing behavior)
- Time gate: no new Phase 1 detections after 14:00
- One signal per level per day

### 4.10 Exact avoid/reject conditions

- Skip if stock is already up > 2.0 ATR from session low at time of Phase 1 (breakdown from strength is different)
- Skip if OR range > 3.0 × daily_ATR
- Skip if volume on breakdown bar > 3.0 × vol_ma (massive volume breakdowns are more likely to continue)
- Apply universal choppiness filter
- Skip maturity filter (the trade is contrarian by nature — "too many bars" doesn't apply the same way)
- Apply bigger_picture filter (stock below both VWAP and EMA9 is structurally weak — but this is the condition we're trading FROM, not INTO. **Rewrite: skip this filter.**)
- Apply trigger_weakness filter (want a strong reclaim bar)

### 4.11 Existing shared framework to reuse

Same as Strategy 1, with long-oriented regime scoring (GREEN=1.0 etc.) and standard quality scorer.

### 4.12 New helpers needed

Same shared helpers as Strategy 1 (`bars_below_level`, `impulse_quality`, `count_attempts`, `is_pdl_active`). Plus:

| Helper | Purpose |
|---|---|
| `is_aligned_failed_move_long()` | Custom regime gate: block only on strongest bear config |

### 4.13 Highest risk for ambiguity / overfitting

1. **Phase 2 length (8 bars = 40 minutes):** Longer window means more noise and more false reclaims. A bounce that lasts 30 minutes before rolling over again would trigger Phase 2 but the trade would lose. **Risk: medium-high.** This is the single biggest ambiguity in the design.

2. **VWAP proximity requirement on trigger:** Requiring close "above VWAP or within 0.20 ATR" may be too tight. After a failed breakdown, price might reclaim the level but still be well below VWAP. Requiring VWAP proximity filters out the trade when the room-to-target is actually largest. **Risk: medium.** Counter-argument: if price is far below VWAP, the reclaim is weak and VWAP is a distant hope. Start with the requirement; relax if signal count is too low.

3. **Asymmetry with Strategy 1:** Failed breakdowns and failed breakouts are not symmetric. Breakdowns tend to be more gradual, more noisy, and involve more retests than breakouts. The wider windows (8 vs 6, 4 vs 3, 15 vs 12) are an attempt to accommodate this, but they're based on market microstructure intuition, not data. **Risk: low-medium.** The windows are not being optimized — they're set once and tested.

### 4.14 5-minute vs 1-minute resolution risk

**5-min is sufficient for v1** but with more caveats than Strategy 1.

The reclaim phase is noisier than the failure phase in Strategy 1. A stock bouncing off support generates more back-and-forth price action than one failing at resistance. On 5-min bars, this churn is compressed. The wider Phase 2 and 3 windows partially compensate.

**Where 1-min would improve:**
- Phase 2 precision: The exact bar where price reclaims the level is important. On 5-min, a bar might open below, trade through the level mid-bar, and close above. The 5-min bar shows a reclaim. On 1-min, you'd see the actual reclaim candle and could time it better.
- Trigger quality: The confirmation bar on 1-min would be more precise.

### 4.15 Priority recommendation

**HIGH PRIORITY** but with a caveat. The thesis is equally strong as Strategy 1 (trapped shorts mirror trapped longs). The levels are equally objective. The state machine is tractable. But the asymmetry between breakout failure and breakdown reclaim adds more uncertainty to the parameter choices. This should be built and tested, but expect more iteration.

### 4.16 Why this might still fail

- **Everything from Strategy 1's "why this might fail" applies here** — resolution risk, no premarket levels, small sample size, regime gate uncertainty.
- **Long reclaims are harder to time than short failures.** A failed breakout tends to drop quickly (panic selling). A failed breakdown reclaim tends to grind up slowly (cautious buying). The 5-min bar resolution may blur the grind into noise.
- **False reclaims are more common than false failures.** Dead cat bounces are a well-known pattern. A stock breaks below ORL, bounces back above for 2-3 bars, then rolls over and continues down. The Phase 3 confirmation window (4 bars) is meant to filter these, but it may not be enough.
- **The VWAP proximity requirement is a double-edged sword.** It improves entry quality but reduces signal count. On the short side (Strategy 1), VWAP is the target and distance from it is good. On the long side, VWAP proximity means less trapped-short pain ahead.
- **Combined with Strategy 1, these two might be correlated.** If ORH fails short on a FLAT day, ORL might also set up a failed breakdown long. But if both fire on the same stock on the same day, something is wrong — it means both levels failed, which suggests random chop, not trapped participants.

---

## 5. Strategy 3: Neutral_RangeEdge_VWAP_Rejection_Fade

### 5.1 Strategy thesis

In a neutral (FLAT) market with no directional bias, price oscillates around VWAP within a day range. When price reaches a local range extreme AND becomes meaningfully separated from VWAP AND shows degradation in continuation momentum, it tends to reject and reconnect toward VWAP. This strategy fades the extension when specific structural conditions confirm the rejection.

### 5.2 Exact intended market/environment

**REVISED (v2):** The original FLAT definition (SPY ±0.05%) was too literal — only 16/210 days qualified. Empirical analysis of SPY's 210-day dataset shows:

| Abs daily change | Days | Cumulative % |
|---|---|---|
| ≤ 0.05% | 16 | 7.6% |
| ≤ 0.25% | 74 | 35.2% |
| ≤ 0.50% | 125 | 59.5% |
| ≤ 0.75% | 175 | 83.3% |

VWAP cross count (measure of intraday chop): 81% of days have ≥4 VWAP crosses, 66% have ≥6. Cross-tab shows small-change days are almost always choppy (69 of 74 days with abs change <0.25% also have ≥4 crosses).

**Revised definition — "Non-Trending" regime:**

The strategy should fire on days where the market lacks directional conviction. This is broader than "flat" — it includes choppy days where SPY oscillates without resolving directionally. Two independent signals of non-trending behavior:

- **Low net movement:** abs(SPY open-to-close change) ≤ 0.50% (captures 125 days, 59.5%)
- **High chop despite movement:** ≥ 4 VWAP crosses AND abs change ≤ 1.0% (captures days that moved but didn't trend cleanly)

Combined: `abs_pct <= 0.50% OR (vwap_crosses >= 4 AND abs_pct <= 1.0%)` → **172 days (81.9%)**

That's too broad — it includes nearly every day. We need a tighter definition that captures the non-trending sweet spot without including mildly trending days where directional strategies already work.

**Recommended definition for v1:**

```
is_non_trending(spy_bars_today):
    # Real-time computable (no lookahead)
    pct_from_open = abs(spy_close - spy_open) / spy_open
    vwap_crosses >= 4   # computed incrementally

    EITHER:
      pct_from_open <= 0.0025   (currently within 0.25% of open)
    OR:
      pct_from_open <= 0.0050 AND vwap_crosses >= 6  (moved some but very choppy)
```

**Backtest proxy (end-of-day, for replay):**
```
abs(day_pct_change) <= 0.25%
OR (abs(day_pct_change) <= 0.50% AND vwap_crosses >= 6)
```

Estimated eligible days: **~90-110 of 210** (~45-52%). This is a dramatic improvement over 16 FLAT days while still excluding strong trending days (>0.50% with clean directional flow).

- **Additional bar-level gate:** At signal time, SPY's EMA9 and EMA20 must be within 0.20% of each other (confirms the market is currently non-trending, not just flat on the day).
- **Blocked:** Days where abs SPY change > 0.75% AND VWAP crosses < 4 (clean trending days).

### 5.3 Exact eligible stock profile

Same in-play proxy. However, there's an inherent tension: in-play requires gap + rvol, which implies directional interest. A stock that's truly "neutral" might not pass in-play. **This is a design risk.** In-play is calibrated for momentum stocks. A neutral-market fade might need a relaxed in-play threshold (lower gap_min, lower rvol_min) or an alternative qualification (just dolvol + range_expansion).

**Decision for v1:** Use standard in-play. With ~90-110 non-trending days (revised definition), signal count should be sufficient for evaluation. If N < 15 after in-play filtering, consider relaxing in-play thresholds (lower gap_min to 0.005, lower rvol_min to 1.0).

### 5.4 Exact level/reference types allowed

**Edge/reference:** The "range edge" is defined as:

```
range_top = session_high (running)
range_bottom = session_low (running)
```

Price must be within 0.20 ATR of either edge to qualify as "at the range edge."

**VWAP separation requirement:**
```
abs(price - vwap) >= 1.0 × intra_ATR
```

This is the core filter. Without meaningful separation from VWAP, there's no room-to-target and the "reconnection toward VWAP" thesis has no teeth.

**Additional structural reference (optional confluence):**
- EMA9: If price is extended above EMA9 AND EMA9 is extended above VWAP, there are two mean-reversion magnets. Higher quality.

### 5.5 Exact sequence of events required

**Phase 0: Environment qualification**
- Non-trending environment confirmed (revised definition: abs change ≤ 0.25% OR (abs change ≤ 0.50% AND VWAP crosses ≥ 6))
- OR is ready (hhmm >= 1000)
- Time window: 10:30 — 14:30 (avoid first hour noise and last-hour EOD flow)

**Phase 1: Extension detection**
- Price near a range edge (within 0.20 ATR of session high or session low)
- Price separated from VWAP by >= 1.0 ATR
- At least 3 bars in the current direction (trending toward the edge, not a single spike)

**Phase 2: Continuation degradation**
- At least ONE of:
  - Bar range contracting: current bar range < 0.50 × average of prior 3 bars (impulse dying)
  - Volume declining: current bar volume < 0.70 × average of prior 3 bars (participation dying)
  - Doji or small body: body ratio < 0.30 (indecision)
- This is the "continuation quality degradation" check. Without it, you're just fading momentum, which is suicidal.

**Phase 3: Rejection bar**
- A bar that reverses the move toward VWAP:
  - For fading a high: bearish bar (close < open) with close below the prior bar's low
  - For fading a low: bullish bar (close > open) with close above the prior bar's high
- Body ratio >= 0.40 (conviction)
- Volume >= 0.70 × vol_ma (some participation in the rejection)

### 5.6 Exact trigger options allowed

Only one: the rejection bar itself is the trigger. No multi-bar confirmation. Rationale: adding a confirmation phase for a mean-reversion trade loses too much of the move. The rejection bar must be high-quality enough to stand alone.

### 5.7 Exact stop logic candidates

**Stop above/below the range edge + buffer:**
```
# Fading high:
stop = session_high + 0.30 × intra_ATR

# Fading low:
stop = session_low - 0.30 × intra_ATR
```

If price breaks to a new session extreme after rejection, the thesis is dead.

### 5.8 Exact target logic candidates

**VWAP:** Always.
```
target = snap.vwap
```

No fallback. VWAP is the only target that makes sense for a reconnection trade. If VWAP is too close (< 0.50 ATR), the trade doesn't qualify in Phase 1 anyway.

### 5.9 Exact invalidation conditions

- Price makes new session high (if fading high) or new session low (if fading low) → stop out
- Price reclaims the rejection bar's extreme in the direction of the original move → abort
- Max hold: 20 bars (on 5-min, ~100 minutes). If VWAP isn't reached in 20 bars, exit at market.
- One signal per side per day (can fade both the high and the low, but only once each)

### 5.10 Exact avoid/reject conditions

- Skip if OR range > 3.0 × daily_ATR (wide OR on a flat day means the stock is volatile despite the market being flat — unreliable for mean reversion)
- Skip if session range > 3.0 × daily_ATR (same logic at session level)
- Skip if the stock has made a new intraday high or low in the last 3 bars (you're fading the tip of an impulse, not a stalled range edge)
- Apply choppiness filter INVERTED — we actually want some chop. If the last 6 bars have low overlap (trending cleanly), this is a trend, not a range. **Skip the standard choppiness filter; instead reject if overlap < 0.30** (the opposite of the normal gate).
- Skip distance filter (we want extended price)
- Skip bigger_picture filter (direction-dependent)
- Skip maturity filter (irrelevant for mean reversion)

### 5.11 Existing shared framework to reuse

| Component | Reuse? | Notes |
|---|---|---|
| InPlayProxy | Yes, may need relaxed thresholds if N low | Non-trending days have less gap activity |
| EnhancedMarketRegime | Yes, needs `is_non_trending()` method | Uses abs pct_from_open + VWAP cross count + EMA convergence |
| RejectionFilters | Minimally | Most filters are long-momentum oriented; need inverted choppiness |
| QualityScorer | Yes, regime weight uses chop quality | Higher chop (more VWAP crosses) = higher regime score for this strategy |
| trigger_bar_quality() | Yes | For rejection bar scoring |

### 5.12 New helpers needed

| Helper | Purpose |
|---|---|
| `is_at_range_edge()` | Check if price is within tolerance of session high/low |
| `is_continuation_degrading()` | Check if bar range, volume, body are declining vs prior 3 bars |
| `is_non_trending()` | SPY-level non-trending check: abs pct_from_open + VWAP cross count + EMA9/EMA20 convergence |
| Inverted choppiness check | Reject if overlap ratio is TOO LOW (trending, not ranging) |

### 5.13 Highest risk for ambiguity / overfitting

1. **~~"Neutral market" definition is fragile.~~** ~~The FLAT day classification uses end-of-day SPY change (±0.05%).~~ **RESOLVED (v2):** Revised to use composite non-trending definition (abs change + VWAP cross count). The real-time version uses `pct_from_open` (no lookahead) + cumulative VWAP cross count. The bar-level EMA convergence gate adds a second confirmation layer. **Risk: MEDIUM.** Still imperfect (a day can start choppy then trend hard in the afternoon), but dramatically better than the ±0.05% FLAT label.

2. **~~Extremely small sample.~~** ~~Only 16 FLAT days.~~ **RESOLVED (v2):** Revised definition captures ~90-110 non-trending days out of 210 (45-52%). Even conservative estimates suggest 50-150 raw signals, 20-60 after in-play. Sufficient for train/test split. **Risk: LOW-MEDIUM.** Sample is no longer the bottleneck.

3. **Session range edge is not a real "level."** Unlike ORH, PDH, ORL which are established institutional references, "session high" is just "the highest price so far today." It changes every time there's a new high. It has no structural significance — it's just where the market happened to reach. **Risk: HIGH.** The counter-argument is that on a non-trending day, the session range IS the range, and edges are where mean reversion starts. On choppy days specifically, these edges may actually be stronger references because price repeatedly tests and rejects them.

4. **Continuation degradation is hard to measure on 5-min bars.** "Bar range contracting" across 3 bars is 15 minutes of data. That's very noisy. A single wide bar (news tick, block trade) followed by two normal bars looks like "contraction" but means nothing. **Risk: MEDIUM-HIGH.**

5. **The trade is two-sided.** Fading highs (short) and fading lows (long) have different risk profiles. Combining them into one strategy doubles the parameter space and makes evaluation harder. **Risk: MEDIUM.**

6. **Non-trending definition may overlap with directional strategies' eligible days.** At ~50% of days qualifying as "non-trending," there's significant overlap with GREEN and RED regime days where SC_SNIPER, FL_ANTICHOP, and BDR_SHORT already fire. The strategy's value-add depends on it finding opportunities the directional strategies miss — not just firing on the same days with a weaker thesis. **Risk: MEDIUM.** Mitigation: check signal correlation with existing strategies during backtest.

### 5.14 5-minute vs 1-minute resolution risk

**5-min is marginal for this strategy.** Mean-reversion trades at range edges are inherently shorter-duration than trend continuation trades. The rejection bar on 5-min represents 5 minutes of action. On 1-min, you'd see the actual rejection candle with much higher fidelity.

The continuation degradation check (Phase 2) is particularly weak on 5-min. Three 5-min bars = 15 minutes. That's a coarse measurement of "momentum dying."

**If this strategy is built, it should be tagged as 5-min-provisional with explicit plans to evaluate on 1-min data if the concept shows any promise.**

### 5.15 Priority recommendation

**CONDITIONAL — build after Strategies 1 and 2 are evaluated.**

The two critical blockers identified in v1 are now resolved:
1. ~~Only 16 FLAT days~~ → Revised non-trending definition: ~90-110 eligible days (45-52% of dataset)
2. ~~End-of-day lookahead bias~~ → Real-time computable: pct_from_open + cumulative VWAP cross count

Remaining risks:
1. Session range edge is not a structural level — still valid concern
2. Continuation degradation is noisy on 5-min — still valid
3. Two-sided complexity — still valid

**Recommendation:** Build after S1+S2 evaluation, with these modifications already incorporated:
- Use revised `is_non_trending()` regime gate (real-time computable, ~50% of days)
- Consider short-side only (fade highs on non-trending days) as the narrower first variant
- Tag as 5-min-provisional with explicit 1-min evaluation plan
- Add correlation check against existing strategies during backtest (since ~50% overlap with directional strategy days)

### 5.16 Why this might still fail

Everything in 5.13 plus:
- **It's a fundamentally different thesis than the other two.** Strategies 1 and 2 trade trapped participants (specific, identifiable, motivated sellers/buyers). Strategy 3 trades mean reversion to VWAP (statistical tendency, no specific trapped participants). The edge, if it exists, is weaker and more fragile.
- **Non-trending days may not have enough intraday movement to matter.** On choppy days with abs change < 0.25%, stocks may still move 1-2 ATR intraday, but the range might be traversed multiple times with no clean entry. After stop and target placement, the risk/reward may be consistently marginal.
- **It might just be random fading in chop.** Despite all the structural requirements, the core bet is "price is far from VWAP, so it will go back." That's true on average but very noisy bar-by-bar.
- **The broader regime definition may dilute the edge.** At ~50% of days qualifying, the strategy is no longer exploiting a specific rare market condition. It's trying to find mean-reversion setups on roughly half of all days. The selectivity that makes S1/S2 compelling (rare trapped-participant event) may be missing here.

---

## 6. Shared Helper Map: Reuse vs New

### Existing — reuse as-is

| Helper | Used by |
|---|---|
| `trigger_bar_quality()` | All 3 |
| `bar_body_ratio()` | All 3 |
| `is_in_time_window()` | All 3 |
| `get_hhmm()` | All 3 |
| `compute_rs_from_open()` | Strategies 1, 2 |
| `simulate_strategy_trade()` | All 3 (replay) |
| `is_expansion_bar()` | Strategies 1, 2 (Phase 1 bar check) |

### Existing — reuse with modifications

| Component | Modification | Used by |
|---|---|---|
| `EnhancedMarketRegime` | Add `is_aligned_failed_move_short()`, `is_aligned_failed_move_long()`, `is_non_trending()` | All 3 |
| `RejectionFilters` | Add `inverted_choppiness()` method | Strategy 3 |
| `QualityScorer` | No code change; use inverted regime scoring for shorts (already done in BDR) | Strategies 1, 2 |

### New — create in `strategies/shared/level_helpers.py`

| Helper | Used by | Lines (est.) |
|---|---|---|
| `is_near_level(price, level, atr, tol)` | 1, 2, 3 | 5 |
| `bars_above_level(recent_bars, level)` | 1, 2 | 8 |
| `bars_below_level(recent_bars, level)` | 1, 2 | 8 |
| `impulse_quality(bar, atr, vol_ma, level, direction)` | 1, 2 | 20 |
| `count_attempts(recent_bars, level, direction, atr)` | 1, 2 | 15 |
| `is_level_active(price_history, level, atr, threshold)` | 1, 2 (PDH/PDL) | 10 |

### New — strategy-specific (not shared)

| Helper | Strategy | Reason not shared |
|---|---|---|
| `is_continuation_degrading()` | 3 | Specific to neutral fade; not applicable to failed-move family |
| `is_at_range_edge()` | 3 | Uses session_high/low, specific to neutral fade |

---

## 7. Key Ambiguity / Risk List

Ranked by severity:

| # | Risk | Strategy | Severity | Mitigation |
|---|---|---|---|---|
| 1 | ~~Only 16 FLAT days~~ **RESOLVED:** Revised non-trending definition yields ~90-110 days | 3 | ~~CRITICAL~~ LOW | Revised composite regime definition |
| 2 | ~~End-of-day FLAT label creates lookahead bias~~ **RESOLVED:** Real-time version uses pct_from_open + cumulative VWAP crosses | 3 | ~~HIGH~~ LOW | Real-time computable regime gate |
| 3 | No premarket data for backtesting PMH/PML | 1, 2 | HIGH | Use ORH/PDH only in v1; add PMH/PML as live-only v2 |
| 4 | 5-min resolution blurs Phase 2 failure/reclaim detection | 1, 2 | MEDIUM | Wide failure windows + minimum distance thresholds |
| 5 | Session range edge is not a structural level | 3 | MEDIUM-HIGH | On choppy days, repeatedly tested range edges gain some structural significance |
| 6 | Phase 2 failure_window is a judgment call (6 vs 8 bars) | 1, 2 | MEDIUM | Fix at design values; don't optimize |
| 7 | Small expected sample size (15-30 signals per strategy) | 1, 2 | MEDIUM | Accept; monitor walk-forward stability |
| 8 | Correlated signals (same stock fires both S1 and S2) | 1, 2 | LOW | Track and report, but don't block |
| 9 | PDH/PDL "active" check threshold arbitrary | 1, 2 | LOW | Fix at 1.0 ATR; not sensitive |

---

## 8. Recommended Build Order

1. **Shared helpers first** (`level_helpers.py`): ~60 lines. Dependency for both Strategy 1 and 2.
2. **Config params**: Add to `StrategyConfig`. ~50 lines per strategy.
3. **Strategy 1 (PMH_ORH_FailedBreakout_Short)**: Replay version first. ~400 lines. Run backtest.
4. **Evaluate Strategy 1 results**: If N >= 10 and PF > 0.8, proceed. If N < 5, the thesis is untestable on this data — stop and reconsider level selection.
5. **Strategy 2 (ORL_PML_FailedBreakdown_Reclaim_Long)**: Replay version. ~400 lines. Run backtest.
6. **Evaluate Strategy 2 results**: Same gate.
7. **Combined evaluation**: Run replay with all 10 strategies. Check correlation between S1 and S2 signals. Check portfolio impact.
8. **Live versions**: Only after both replay strategies pass promotion criteria.
9. **Strategy 3 (Neutral_RangeEdge_VWAP_Rejection_Fade)**: Build after S1 + S2 evaluation. Revised non-trending regime makes it testable. ~350 lines. Consider short-side-only variant first.

---

## 9. Readiness Recommendation

| Strategy | Ready for implementation? | Confidence |
|---|---|---|
| PMH_ORH_FailedBreakout_Short | **YES** — proceed to implementation | HIGH. Narrow, specific, well-grounded thesis. Clear state machine. Directly mirrors proven SC_SNIPER architecture. Main unknown is 5-min resolution adequacy, which only backtest can answer. |
| ORL_PML_FailedBreakdown_Reclaim_Long | **YES** — proceed to implementation | MEDIUM-HIGH. Same structural quality as Strategy 1 but with more parameter uncertainty (longer windows, VWAP proximity question). Worth building and testing. |
| Neutral_RangeEdge_VWAP_Rejection_Fade | **CONDITIONAL YES** — proceed after S1+S2, with revised regime | MEDIUM. The two biggest blockers (16-day sample size, lookahead bias) are resolved by the revised non-trending definition (~90-110 eligible days, real-time computable). Session range edge weakness and two-sided complexity remain real risks. The thesis is weaker than the failed-move family but now has enough data to test. Build after S1+S2 evaluation confirms the portfolio needs it. |

**Summary:** Build Strategies 1 and 2 first. Strategy 3 is now **conditionally approved** — the revised non-trending regime definition (abs change ≤ 0.25% OR choppy with ≥6 VWAP crosses) resolves the sample size and lookahead problems that previously blocked it. Build S3 after S1+S2 backtest results are in, as a third phase. If S1+S2 already bring total daily opportunity to 4+ trades/day, S3 becomes lower priority. If gaps remain on non-trending days, S3 fills them.
