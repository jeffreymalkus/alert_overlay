# Exit Architecture Audit — All Active Strategies

**Date:** 2026-03-13
**Finding:** All 11 active strategies use synthetic fixed R:R targets. The entry and stop logic is structurally sound. The target logic is not. All prior backtest metrics (PF, WR, total R) were measured against these synthetic targets.

---

## Strategy-by-Strategy Audit

### 1. BDR_SHORT (Breakdown-Retest Short)

**Entry thesis:** Price breaks below a support level (ORL, VWAP, or swing low), retests the broken level from below, and gets rejected with an upper wick. Classic failed-retest-from-below short.

**Stop logic:** Retest bar high + 0.30 ATR buffer, with 0.50 ATR minimum. **STRUCTURAL** — stop is above the level that must hold for the trade thesis to remain valid.

**Current target logic:** `target = entry - risk × 2.0`. **SYNTHETIC** — no relationship to chart structure.

**Config note:** `bdr_target_mode` does not exist. Unlike ORH/PDH/ORL/BS, BDR has no VWAP target option at all.

**Proper structural target(s):**
- **Primary:** VWAP (price should gravitate toward VWAP on a RED day — this is the natural magnet for mean-reversion shorts)
- **Secondary:** Prior day low (PDL) or session low — the next meaningful support level below
- **Tertiary:** Measured move from the breakdown level (breakdown level - distance from prior high to breakdown level)

**Recommended exit method:** VWAP target with PDL as a runner target. If VWAP is too close (<1R), use the session low or PDL. Fixed 2R becomes the fallback ceiling, not the primary target.

**Redesign priority: HIGH** — Active live today, orders sent, most visible problem.

---

### 2. ORH_FBO_V2_A (Opening Range High Failed Breakout Short, v2)

**Entry thesis:** Price breaks above the opening range high, fails to hold, drops back below ORH, retests ORH from below and gets rejected. Failed breakout trap short.

**Stop logic:** Mode A: retest high + 0.20 ATR buffer. Mode B: ORH + 0.20 ATR buffer. Min 0.30 ATR. **STRUCTURAL** — above the level that invalidates the trade.

**Current target logic:** VWAP if distance >= 0.40 ATR, else `entry - risk × 1.5`. **PARTIALLY STRUCTURAL** — uses VWAP when it's far enough, falls back to fixed R:R.

**What's wrong:** The VWAP check is good but the fallback is still synthetic. When VWAP is too close, the strategy should either skip the trade (insufficient reward) or use the next structural level (session low, PDL).

**Proper structural target(s):**
- **Primary:** VWAP (already implemented, working)
- **Secondary:** Opening range low (ORL) — the other side of the opening range
- **Skip rule:** If VWAP is within 1R of entry, the trade doesn't offer enough reward — skip it rather than using synthetic 1.5R

**Recommended exit method:** Keep VWAP target. Replace fixed R:R fallback with ORL target or trade rejection (minimum 1R to VWAP required to take the trade).

**Redesign priority: MEDIUM** — Already partially structural. Needs fallback cleanup.

---

### 3. PDH_FBO_B (Prior Day High Failed Breakout Short)

**Entry thesis:** Same as ORH_FBO but at the prior day's high. Break above PDH, failure, retest, rejection.

**Stop logic:** Mode A: retest high + 0.20 ATR buffer. Mode B: PDH + 0.20 ATR buffer. Min 0.30 ATR. **STRUCTURAL.**

**Current target logic:** VWAP if distance >= 0.40 ATR, else `entry - risk × 1.5`. **PARTIALLY STRUCTURAL** — same as ORH_FBO_V2.

**Proper structural target(s):**
- **Primary:** VWAP
- **Secondary:** ORH (if different from PDH) — next structural level below
- **Skip rule:** Same as ORH_FBO — require minimum 1R to VWAP

**Recommended exit method:** Same as ORH_FBO_V2. VWAP primary, ORH secondary, skip if insufficient reward.

**Redesign priority: MEDIUM** — Same partial fix needed as ORH_FBO_V2.

---

### 4. ORL_FBD_LONG (Opening Range Low Failed Breakdown Long)

**Entry thesis:** Mirror of ORH_FBO. Price breaks below ORL, reclaims back above, confirms with higher high. Failed breakdown trap long.

**Stop logic:** Below lowest low since breakdown - 0.30 ATR buffer. Min 0.40 ATR. **STRUCTURAL.**

**Current target logic:** VWAP if distance >= 0.50 ATR, else `entry + risk × 2.0`. **PARTIALLY STRUCTURAL.**

**Proper structural target(s):**
- **Primary:** VWAP (already implemented)
- **Secondary:** Opening range high (ORH) — the opposite end of the opening range
- **Tertiary:** Session high / prior day high
- **Skip rule:** Require minimum 1R to VWAP to take the trade

**Recommended exit method:** Same pattern. VWAP primary, ORH secondary, skip if insufficient.

**Redesign priority: MEDIUM** — Same partial fix needed.

---

### 5. ORH_FBO (Opening Range High Failed Breakout Short, v1)

**Entry thesis:** Same as v2 but simpler detection (5-min only, no mode A/B split).

**Stop logic:** Retest high + 0.30 ATR buffer. Min 0.40 ATR. **STRUCTURAL.**

**Current target logic:** VWAP if distance >= 0.50 ATR, else `entry - risk × 2.0`. **PARTIALLY STRUCTURAL.**

**Note:** This is the v1 predecessor to ORH_FBO_V2. Same fix applies.

**Redesign priority: LOW** — V2 supersedes this. Fix V2, this follows.

---

### 6. BS_STRUCT (Backside Structure Long)

**Entry thesis:** After a decline, price builds a basing structure (higher highs, higher lows, range above EMA9) and breaks out of the range. Recovery/reversal long.

**Stop logic:** Below the range low (most recent higher low) - $0.02 buffer. **STRUCTURAL.**

**Current target logic:** VWAP if `bs_target_mode == "vwap"` (which it is by default), else `entry + risk × 2.0`. But with a critical flaw: if VWAP target is closer than 1R, it falls back to fixed 2R. This is backwards — if VWAP is close, the stock is already near fair value and the trade has less room, not more.

**Proper structural target(s):**
- **Primary:** VWAP (already the default mode)
- **Secondary:** Session high or ORH — the stock is recovering, the natural target is the prior high
- **Skip rule:** If VWAP is within 1R, the recovery is nearly done — skip the trade

**Recommended exit method:** VWAP primary, session high secondary. Remove the illogical "VWAP too close → use bigger fixed target" fallback.

**Redesign priority: MEDIUM** — The backwards fallback logic actively hurts.

---

### 7. EMA9_FT (EMA9 First Touch Long)

**Entry thesis:** Opening drive extends from open, first pullback touches 9 EMA, first close back above 9 EMA. Momentum continuation long.

**Stop logic:** Below the pullback low - $0.02 buffer. Min 0.15 ATR. **STRUCTURAL** — stop invalidates the pullback thesis.

**Current target logic:** `target = entry + risk × 2.0`. **FULLY SYNTHETIC.**

**Proper structural target(s):**
- **Primary:** Drive high (the high of the initial move). The thesis is "pullback complete, resume the drive" — the natural first target is a retest of the drive high.
- **Secondary:** Measured move (drive high + distance from pullback low to drive high). If it clears the drive high, the measured move projects the next leg.
- **Runner option:** Trail with EMA9 after drive high is hit. The stock is trending — let the 9 EMA manage the exit.

**Recommended exit method:** Drive high as Target 1 (partial exit). EMA9 trail for runner. This is the classic "take half at the prior high, trail the rest" approach.

**Redesign priority: HIGH** — Highest signal count strategy, fully synthetic, and the drive high is already tracked in metadata (`drive_high`).

---

### 8. SC_SNIPER (Support/Consolidation Breakout Long)

**Entry thesis:** Price breaks above a support-turned-resistance level (ORH, swing high), retests it from above (support), and confirms with a higher close on volume. Breakout-retest-go long.

**Stop logic:** Below the retest low - $0.02 buffer. Min 0.15 ATR. **STRUCTURAL.**

**Current target logic:** `target = entry + risk × 2.0`. **FULLY SYNTHETIC.**

**Proper structural target(s):**
- **Primary:** Session high or prior day high — the next resistance overhead
- **Secondary:** VWAP-based measured move (if the breakout level is well above VWAP, there's room; if near VWAP, less room)
- **Runner option:** Trail with EMA9 after first target hit

**Recommended exit method:** Next resistance level (session high, PDH) as Target 1. EMA9 trail for runner. If no clear resistance overhead within 3R, use the original breakout impulse distance as a measured move.

**Redesign priority: HIGH** — Fully synthetic, high signal count.

---

### 9. SP_ATIER (Spencer A-Tier Consolidation Breakout Long)

**Entry thesis:** Tight consolidation box (low range, most bars in upper half, above EMA9) breaks out with volume. Premium breakout pattern.

**Stop logic:** Below the box low - $0.02 buffer. Min 0.15 ATR. **STRUCTURAL** — stop invalidates the consolidation base.

**Current target logic:** `target = entry + risk × 3.0`. **FULLY SYNTHETIC** — and the most extreme. 3R means the target is 3× the box height above entry. On a tight box (good setup), this might be achievable. On a wider box, it's asking for a massive move.

**Proper structural target(s):**
- **Primary:** Session high or prior day high
- **Secondary:** Measured move = box width projected above box high (classic rectangle breakout target)
- **Runner option:** Trail with EMA9

**Recommended exit method:** Measured move from box (box high + box width) as Target 1. Session high/PDH as an alternative if closer. Trail runner with EMA9. The box width measured move is the textbook target for a consolidation breakout.

**Redesign priority: HIGH** — 3R fixed target is the most aggressive synthetic number in the portfolio.

---

### 10. HH_QUALITY (HitchHiker Consolidation Breakout Long)

**Entry thesis:** Opening drive → tight consolidation → breakout above consolidation high. Continuation long after a pause in the trend.

**Stop logic:** Below consolidation low - $0.02 buffer. Min 0.15 ATR. **STRUCTURAL.**

**Current target logic:** `target = entry + risk × 1.5`. **FULLY SYNTHETIC.**

**Proper structural target(s):**
- **Primary:** Drive high (same as EMA9_FT — the prior move's high is the natural target)
- **Secondary:** Measured move = consolidation breakout level + opening drive distance
- **Runner option:** Trail with EMA9

**Recommended exit method:** Drive high as Target 1, trail runner with EMA9. Very similar to EMA9_FT since both are momentum continuation patterns after a pause.

**Redesign priority: MEDIUM** — 1.5R is less egregious than 2R or 3R, but still synthetic.

---

### 11. EMA_FPIP (EMA First Pullback in Play Long)

**Entry thesis:** Strong expansion leg (impulse), clean pullback on declining volume, first close back above with volume expansion. Impulse-pullback-continuation.

**Stop logic:** Below pullback low - $0.02 buffer. Min 0.20 ATR. **STRUCTURAL.**

**Current target logic:** `target = entry + risk × 2.0`. **FULLY SYNTHETIC.**

**Proper structural target(s):**
- **Primary:** Expansion leg high (the prior impulse high — the trade thesis is "pullback done, resume the impulse")
- **Secondary:** Measured move = expansion distance projected from pullback low
- **Runner option:** Trail with EMA20 (since this is a higher-timeframe continuation pattern, EMA20 is more appropriate than EMA9)

**Recommended exit method:** Expansion leg high as Target 1 (already tracked as `exp_high` in state). Trail runner with EMA20.

**Redesign priority: HIGH** — Fully synthetic. The expansion high is already computed and available.

---

### 12. FL_ANTICHOP (Failed Low / Anti-Chop Recovery Long)

**Entry thesis:** Strong decline, then a turn (higher lows confirmed), then a clean cross above VWAP. Mean-reversion long.

**Stop logic:** `decline_distance × fl_stop_frac (0.50)` below entry. **SEMI-STRUCTURAL** — proportional to the decline but not anchored to a specific level. This is also a candidate for improvement (should use the higher-low level as stop reference).

**Current target logic:** `target = entry + risk × 1.5`. **FULLY SYNTHETIC.**

**Proper structural target(s):**
- **Primary:** VWAP (if entering below VWAP, VWAP is the target; if entering at VWAP, session high or ORH)
- **Secondary:** Decline origin (the high before the decline started) — full mean-reversion target
- **Runner option:** Not recommended. Mean-reversion trades should have fixed targets, not runners, since they're fading a move rather than riding a trend.

**Recommended exit method:** VWAP or decline origin as the target (whichever gives better R:R). This is a mean-reversion play — the target is the mean (VWAP) or the origin of the move.

**Redesign priority: MEDIUM** — Both the stop and target need structural rework.

---

## Summary: Target Logic Classification

| Strategy | Stop | Target | Structural Target Exists? |
|----------|------|--------|--------------------------|
| BDR_SHORT | STRUCTURAL | SYNTHETIC 2.0R | No |
| ORH_FBO_V2_A | STRUCTURAL | PARTIAL (VWAP/1.5R) | Yes, needs fallback fix |
| PDH_FBO_B | STRUCTURAL | PARTIAL (VWAP/1.5R) | Yes, needs fallback fix |
| ORL_FBD_LONG | STRUCTURAL | PARTIAL (VWAP/2.0R) | Yes, needs fallback fix |
| ORH_FBO | STRUCTURAL | PARTIAL (VWAP/2.0R) | Yes, needs fallback fix |
| BS_STRUCT | STRUCTURAL | PARTIAL (VWAP/2.0R, backwards) | Yes, fallback logic inverted |
| EMA9_FT | STRUCTURAL | SYNTHETIC 2.0R | No |
| SC_SNIPER | STRUCTURAL | SYNTHETIC 2.0R | No |
| SP_ATIER | STRUCTURAL | SYNTHETIC 3.0R | No |
| HH_QUALITY | STRUCTURAL | SYNTHETIC 1.5R | No |
| EMA_FPIP | STRUCTURAL | SYNTHETIC 2.0R | No |
| FL_ANTICHOP | SEMI-STRUCTURAL | SYNTHETIC 1.5R | No |

---

## Redesign Priority Ranking

**Tier 1 — HIGH (fully synthetic, high impact):**
1. **BDR_SHORT** — Live orders out today, fully synthetic, config has no VWAP option
2. **EMA9_FT** — Highest signal count, fully synthetic, drive_high already in metadata
3. **SC_SNIPER** — Fully synthetic, high signal count
4. **SP_ATIER** — 3.0R is the most extreme fixed target
5. **EMA_FPIP** — Fully synthetic, expansion high already computed

**Tier 2 — MEDIUM (partially structural, need fallback fix):**
6. **ORH_FBO_V2_A** — VWAP logic works, fallback needs fixing
7. **PDH_FBO_B** — Same as ORH_FBO_V2
8. **ORL_FBD_LONG** — Same pattern
9. **BS_STRUCT** — VWAP logic inverted (backwards fallback)
10. **HH_QUALITY** — 1.5R is less egregious
11. **FL_ANTICHOP** — Both stop and target need rework

**Tier 3 — LOW:**
12. **ORH_FBO (v1)** — Superseded by V2

---

## Proposed Exit Architecture (Universal Pattern)

Every strategy should follow this pattern:

```
1. Compute structural target(s) from chart levels
2. Compute risk (entry to stop)
3. Compute actual R:R = (entry to structural target) / risk
4. If actual R:R < 1.0 → SKIP the trade (insufficient reward)
5. If actual R:R > 3.0 → CAP target at 3R (unrealistic intraday)
6. Place bracket order: entry, stop, structural target
7. Optional: partial exit at Target 1, trail runner with EMA9/EMA20
```

The fixed R:R becomes a FILTER and a CAP, not the target itself:
- **Minimum R:R filter:** Don't take trades offering less than 1R
- **Maximum R:R cap:** Don't expect more than 3R intraday (cap the target)
- **Target itself:** Always derived from structure

---

## Impact on Backtest Infrastructure

`simulate_strategy_trade()` in `helpers.py` currently takes `target_price` from the signal and checks if price hits it. The function itself doesn't need to change — it's the strategies that need to pass structural targets instead of synthetic ones.

The replay versions of each strategy will need the same target logic updates as the live versions. After updating, ALL strategy metrics must be re-run and the registry refreshed.

---

## Recommended Implementation Order

1. Update `simulate_strategy_trade()` to support partial exits (Target 1 + trail)
2. Fix BDR_SHORT (add VWAP target, live + replay)
3. Fix EMA9_FT (add drive_high target, live + replay)
4. Fix remaining Tier 1 strategies
5. Fix Tier 2 fallback logic
6. Re-run full replay suite
7. Update all registry metrics
8. Validate live behavior
