# RSI Integration — Promotion Memo

## What Changed

### Code modifications (4 files)

**indicators.py** — Added `RSI` class: Wilder-style incremental RSI with configurable period, rolling history deque for lookback windows, and `prev_value` for cross detection. No external dependencies.

**models.py** — Added `RSI_MIDLINE_LONG = 19` and `RSI_BOUNCEFAIL_SHORT = 20` to SetupId. Mapped to TREND and SHORT_STRUCT families respectively. Added display names. No existing IDs renumbered.

**config.py** — Added ~35 config params for RSI setups: feature toggles (both `False` by default), RSI period, time windows, all threshold params matching spec exactly, stop/target/time-stop params, alignment gates, `rsi_require_regime: bool = False`, and `hybrid_target_time` exit mode.

**engine.py** — Added RSI instance to `SignalEngine.__init__` (NOT reset on new day — cross-day warmup). Added `rsi.update(bar.close)` in `process_bar()`. Created `_detect_rsi_midline_long()` and `_detect_rsi_bouncefail_short()` helper methods implementing all spec gates. Added RSI signals to the breakout_signals emission loop with setup-specific quality gate (0, self-gated), regime gate (`rsi_require_regime`), and R:R target computation (1.5R).

**backtest.py** — Added per-setup-id exit routing for RSI setups. New `hybrid_target_time` exit mode: stop → target → time_stop priority. RSI setups identified and routed before family-level defaults.

**rsi_replay.py** — New replay script with smoke test, candidate-only, combined, capped (max 3), overlap analysis, monthly breakdown, warmup confirmation, and promotion verdicts.

## Ambiguities Resolved

1. **SPY EMA20 alignment**: Spec requires `spy_close > spy_ema20`. The `MarketSnapshot` already exposes `above_ema20` as a boolean field. Used directly — no proxy needed.

2. **Stop anchor for recent pullback low**: Spec says `min(signal_bar_low, recent_pullback_low)`. Used the lowest low from the prior N bars in `self._bars` (N = `rsi_pullback_lookback` = 6). This is conservative and matches the research intent.

3. **Quality scoring**: Spec says "do not over-engineer quality scoring for v1." Used base quality 5 with +1 for strong impulse and +1 for VWAP/EMA20 separation. Quality gate set to 0 (RSI setups are self-gated by alignment filters).

4. **RSI history lookback**: The `history` deque in the RSI class needs to be at least `max(impulse_lookback, pullback_lookback) + 2` = 14 elements. Set dynamically from config.

## Warmup Confirmation

1. **RSI readiness**: RSI(7) instance created in `__init__`, NOT reset in `_on_new_day()`. Carries state across days. Ready after 8 bars (7 close-to-close changes). For any symbol with >1 day of historical data, RSI is ready before market open on day 2+.

2. **EMA20 readiness**: EMA(20) already existed in the engine and carries state across days. Not reset in `_on_new_day()`. Ready after 20 bars.

3. **VWAP resets daily**: Yes. `self.vwap.reset()` is called in `_on_new_day()`. Correct — VWAP is a session indicator.

4. **RSI candidates eligible at 10:00**: Yes. Time windows start at 10:00 (configurable). RSI and EMA20 are warm from cross-day state. VWAP initializes during the opening range (9:30–9:45). By 10:00, all indicators are ready and signals can fire.

## Replay Results

### Dataset
- 207 trading days (2025-05-12 → 2026-03-09)
- 88 trading symbols (expanded watchlist)
- Cost model: dynamic slippage (4bps base) + $0.005/share commission

### Smoke test
- SC baseline with RSI toggles off: 236 trades (matches prior study exactly)
- Existing setups unaffected ✓

### Candidate-only replay

| Setup | N | PF(R) | Exp(R) | TotalR | MaxDD | WR% | Stop% | Tgt% | TrnPF | TstPF |
|-------|---|-------|--------|--------|-------|-----|-------|------|-------|-------|
| RSI_MIDLINE_LONG | 667 | 0.81 | -0.123 | -82.1 | 104.9 | 45.4% | 49.5% | 41.1% | 0.77 | 0.85 |
| RSI_BOUNCEFAIL_SHORT | 1719 | 0.65 | -0.258 | -443.9 | 444.9 | 40.4% | 55.8% | 37.6% | 0.58 | 0.75 |

### Overlap analysis
- Same-day same-symbol overlaps with existing setups: 42 / 2384 (1.8%)
- Exact timestamp collisions: 0
- Overlap source is concurrency, not duplication ✓

### Capped replay (max 3 concurrent)

| Setup | N | PF(R) | Exp(R) | TotalR |
|-------|---|-------|--------|--------|
| Capped - RSI Long | 641 | 0.80 | -0.132 | -84.4 |
| Capped - RSI Short | 1347 | 0.68 | -0.228 | -307.2 |
| Capped - ALL | 2731 | 0.64 | -0.259 | -708.0 |

## Promotion Verdict

### RSI_MIDLINE_LONG: **DO NOT PROMOTE**

- PF 0.81 — below 1.0 threshold
- Expectancy -0.123R — negative
- Negative in every month except Feb 2026 and partial Mar 2026
- Train PF 0.77 — below 0.80 floor
- 667 trades is a large sample; this is not a sample-size issue
- The research harness edge did not survive engine-native execution

### RSI_BOUNCEFAIL_SHORT: **DO NOT PROMOTE**

- PF 0.65 — well below 1.0 threshold
- Expectancy -0.258R — deeply negative
- Negative in 9 of 11 months
- 1719 trades — extremely large sample, all negative
- Train PF 0.58, Test PF 0.75 — both below floor
- This is the worst-performing candidate tested in the engine to date

## Interpretation

The RSI candidates showed a significant **research-to-engine gap**. Possible causes:

1. **Execution model differences**: The research harness likely used different slippage, entry timing, or bar alignment than the engine. The engine applies dynamic slippage (4bps base with volatility adjustment) and enters at signal-bar close with cost applied.

2. **Signal volume**: 667 long trades and 1719 short trades across 207 days on 88 symbols suggests the filters are not selective enough in the engine context. The research universe may have been smaller or the conditions more restrictive.

3. **Indicator state differences**: The engine's RSI is warmed incrementally from all bars (including pre-market and after-hours if present in data). The research harness may have computed RSI differently (session-only bars, different warmup protocol).

4. **SPY alignment gate sensitivity**: The engine uses real-time `spy_snap.above_vwap` and `spy_snap.above_ema20`, which can flicker. The research harness may have used a more stable daily classification.

## Recommendation

Both RSI candidates should remain **disabled** (`show_rsi_midline_long = False`, `show_rsi_bouncefail_short = False`). The code is implemented correctly per spec and can be enabled for future study, but the engine-native results do not support promotion.

If the research team wants to investigate the gap, the replay script (`python -m alert_overlay.rsi_replay`) is ready and produces full diagnostics.
