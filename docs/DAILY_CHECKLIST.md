# Portfolio D — Daily Execution Checklist (Variant 2)

Use this checklist every trading day. Do not deviate from frozen rules.

---

## Pre-Market (before 9:30 AM)

- [ ] Check economic calendar — any FOMC, CPI, NFP today? (note in daily summary)
- [ ] Confirm watchlist loaded (87 symbols, excludes index/sector ETFs)
- [ ] Confirm risk per trade = $100 (or current fixed R-unit)
- [ ] Open paper_trade_log.xlsx — Daily Summary tab ready

## Opening Range (9:30–10:00 AM)

- [ ] Watch SPY open direction — note opening price
- [ ] Do NOT take any trades during OR (BDR scan starts at 10:00)

## Regime Classification (10:00 AM onward)

- [ ] Check dashboard regime label (computed automatically from SPY)
- [ ] If RED+TREND: short book active. Long book filtered by tape permission.
- [ ] Log regime estimate in Daily Summary

## Long Book — Active When Tape Permission ≥ 0.40

Entry scan (VK + SC):

- [ ] Dashboard will only show alerts that pass tape permission ≥ 0.40
- [ ] Tape permission tag shown on each long alert card (e.g., "Tape +0.52")
- [ ] VWAP_KISS: Price separated ≥1.25 ATR from VWAP, then kisses back
- [ ] SECOND_CHANCE: Breakout with volume ≥1.25× → retest → confirmation above VWAP+EMA9
- [ ] Verify quality ≥ 2 before entering
- [ ] Verify time < 15:30
- [ ] Compute position size: shares = $100 / (entry − stop)
- [ ] Log trade immediately in Trade Log tab (include tape permission score)

Exit management:

- [ ] Monitor EMA9 trail — exit if close below EMA9
- [ ] Monitor stop — exit if price hits stop level
- [ ] Time stop: 20 bars max for breakout-family trades
- [ ] Hard close by 15:55

## Short Book — Active on RED+TREND Days Only

Pre-condition check:

- [ ] SPY is RED (open-to-close < −0.05%)
- [ ] Character is TREND (close in bottom 25% of day range)
- [ ] If not RED+TREND → **no short entries today**

Entry scan (BDR, 10:00–11:00 AM only):

- [ ] Identify breakdown below support (ORL, VWAP, or swing low)
- [ ] Breakdown bar: range ≥70% median, volume ≥90% vol_ma, close in lower 60%
- [ ] Retest: high approaches broken level within 0.30 ATR, within 8 bars
- [ ] Rejection bar: upper wick ≥30% of bar range
- [ ] Entry must be before 11:00 AM — **no PM shorts**
- [ ] Stop = retest high + 0.30 ATR
- [ ] Compute position size: shares = $100 / (stop − entry)
- [ ] Log trade immediately

Exit management:

- [ ] Monitor stop — exit if bar high reaches stop price
- [ ] Time stop: 8 bars max
- [ ] Hard close by 15:55

## End of Day (after 15:55)

- [ ] All positions closed
- [ ] Complete Trade Log entries (exit price, exit time, exit reason, bars held)
- [ ] Verify PnL(R) formula computed correctly for each trade
- [ ] Complete Daily Summary row (regime, SPY change, notes)
- [ ] Check Running Stats tab — compare live PF(R) and Exp(R) to backtest reference
- [ ] Note any rule violations or edge cases in Notes column

## Weekly Review (Friday EOD)

- [ ] Count trades this week by book (long / short)
- [ ] Compare weekly WR, PF(R), Exp(R) to backtest reference
- [ ] Check for symbol concentration — any single symbol > 20% of week's R?
- [ ] Check for day concentration — any single day > 50% of week's R?
- [ ] Note RED+TREND day count this week
- [ ] Review any rule violations logged during the week
- [ ] Update promotion tracker (see PROMOTION_CRITERIA.md)

---

## Red Flags — Stop Trading and Review If:

1. Cumulative R drops below −15R from starting point
2. 5 consecutive losing trades on either book
3. You catch yourself violating a frozen rule (e.g., PM short entry, RED day long)
4. A trade is taken that doesn't match any setup definition in SYSTEM_SPEC.md
5. Stop rate exceeds 40% over 20+ trade window

---

## Regime Quick Reference (Portfolio D)

| SPY Condition | Long Book | Short Book |
|---------------|-----------|------------|
| Any day with tape perm ≥ 0.40 | ACTIVE | OFF (unless also RED+TREND) |
| RED+TREND | Filtered by tape (may or may not pass) | **ACTIVE** |
| RED+CHOPPY | Filtered by tape (usually blocked) | OFF |
| GREEN/FLAT with weak tape | OFF (tape < 0.40) | OFF |

**Key change from Portfolio C:** Longs are no longer gated by day color. They are gated by
the continuous tape permission score (≥ 0.40). The dashboard computes this automatically
and only shows long alerts that pass. Each long alert card displays the tape score.
