# RSI Audit Correction Memo

Date: 2026-03-11
Scope: Narrow corrections per ChatGPT audit findings

---

## 1. RSI Prior-Window Contamination — CONFIRMED AND FIXED

**Bug:** The current signal bar's RSI value was included in the "prior impulse" and "prior pullback/bounce" lookback windows.

**Root cause:** `rsi.update(bar.close)` is called at engine.py line 160, appending the current bar's RSI to `rsi.history`. The detection helpers then sliced `hist[-12:]` and `hist[-6:]`, which included the just-appended current bar value.

**Fix (engine.py, both helpers):**
- Changed history requirement from `len(rsi.history) < cfg.rsi_impulse_lookback` to `< cfg.rsi_impulse_lookback + 1`
- Changed impulse window from `hist[-cfg.rsi_impulse_lookback:]` to `hist[-(cfg.rsi_impulse_lookback + 1):-1]`
- Changed pullback/bounce window from `hist[-cfg.rsi_pullback_lookback:]` to `hist[-(cfg.rsi_pullback_lookback + 1):-1]`
- The `:-1` slice excludes `rsi.history[-1]` which is the current bar

**Current bar is now used ONLY for:**
- RSI cross detection (gate 4): `rsi.prev_value` vs `rsi.value`
- Price confirmation (gate 5): `bar.close` vs `prev.high` / `prev.low`
- VWAP alignment (gate 6): `bar.close` vs VWAP
- EMA20 alignment (gate 7): `bar.close` vs EMA20
- SPY alignment (gate 8): `spy_snap.above_vwap` / `spy_snap.above_ema20`

**Stop logic:** Unchanged and correct. `self._bars` does not include the current bar at detection time (appended at line 1461, after detection at line ~1294). Stop = min/max(signal_bar_low/high, recent_pullback/bounce_low/high) ± ATR buffer.

---

## 2. Replay Restructured — 4 Separate Sections

| Section | What runs | Purpose |
|---------|-----------|---------|
| 1. EXISTING-ONLY STANDALONE | SC Long, BDR Short, EMA Pull Short | Baseline for existing setups |
| 2. RSI-ONLY STANDALONE | RSI Midline Long, RSI Bouncefail Short | Judge RSI candidates in isolation |
| 3. COMBINED UNCONSTRAINED | Existing + RSI, no position cap | Overlap/interaction inspection only |
| 4. COMBINED CAPPED | Existing + RSI, max 3 concurrent | Realistic capacity test |

Existing setups are now judged from Section 1 (not from the combined run).
RSI setups are judged from Section 2 (not from the combined run).

---

## 3. Capped Replay — Now Uses Actual Exit Timestamps

**Previous:** Used a crude heuristic based on `entry_date` and `entry_time` comparisons to estimate whether positions had closed. No real exit time was tracked.

**Now:** `PTrade` includes `exit_time` (datetime), populated from `Trade.exit_time` which the backtest already computes. Concurrency enforcement:
```
open_positions = [(exit_t, sym) for (exit_t, sym) in open_positions if exit_t > t.entry_time]
```
A position is "open" if its actual exit timestamp is strictly after the candidate's entry timestamp. No approximations.

**Verification:** Peak concurrent positions after capping = 3 (confirmed in output).

**Priority:** Trades sorted by `(entry_time, is_rsi)` so existing promoted setups are considered before RSI candidates at the same timestamp.

---

## 4. Did Results Change Materially?

### RSI-Only (Section 2) — Before vs After Fix:

| Setup | Metric | Before (contaminated) | After (corrected) | Delta |
|-------|--------|-----------------------|--------------------|-------|
| RSI_MIDLINE_LONG | N | 667 | 654 | -13 |
| RSI_MIDLINE_LONG | PF(R) | 0.81 | 0.78 | -0.03 |
| RSI_MIDLINE_LONG | Exp(R) | -0.123 | -0.150 | -0.027 |
| RSI_MIDLINE_LONG | TotalR | -82.1 | -98.0 | -15.9 |
| RSI_BOUNCEFAIL_SHORT | N | 1719 | 1640 | -79 |
| RSI_BOUNCEFAIL_SHORT | PF(R) | 0.65 | 0.64 | -0.01 |
| RSI_BOUNCEFAIL_SHORT | Exp(R) | -0.258 | -0.263 | -0.005 |
| RSI_BOUNCEFAIL_SHORT | TotalR | -443.9 | -431.3 | +12.6 |

**Assessment:** Small changes. Trade counts dropped slightly (stricter windows filtering out marginal signals). Both setups remain deeply unprofitable. Verdict unchanged: **DO NOT PROMOTE** for either.

---

## 5. Files Changed

| File | Change |
|------|--------|
| `engine.py` | Fixed `_detect_rsi_midline_long()` and `_detect_rsi_bouncefail_short()` — prior-window slicing now excludes current bar |
| `rsi_replay.py` | Full rewrite: 4 separate sections, `_existing_only_cfg()`, actual exit timestamps in PTrade, `_capped_portfolio()` uses real exit times, CSV export per section |

No other files changed. indicators.py, models.py, config.py, backtest.py untouched.

---

## 6. Output Files

- `RSI_REPLAY_OUTPUT_V2.txt` — full console output
- `replay_existing_only.csv` — 352 trades, Section 1
- `replay_rsi_only.csv` — 2294 trades, Section 2
- `replay_combined_unconstrained.csv` — 3043 trades, Section 3
- `replay_combined_capped.csv` — 1675 trades, Section 4

All CSVs include: section, date, entry_time, exit_time, symbol, side, setup, pnl_rr, exit_reason, bars_held, quality.
