# VKA Engine Validation — Verdict

## Bottom Line

**VKA does NOT survive engine integration.** The standalone edge (+135R, PF 1.48) collapses to -108R, PF 0.77 in-engine. The same problem affects VR: -49.83R, PF 0.77 in-engine vs +31R, PF 1.34 with perfect-foresight filtering.

**Root cause: the real-time SPY day filter is a poor proxy for end-of-day GREEN classification.** Both setups depend entirely on the GREEN filter for their edge. Without it, both are net losers.

## Numbers

| Portfolio | N | PF(R) | Exp(R) | TotalR | MaxDD | Train/Test |
|-----------|--:|------:|-------:|-------:|------:|-----------:|
| VKA engine (real-time SPY) | 951 | 0.77 | -0.114R | -108.31R | 145.53R | 0.85/0.71 |
| VKA standalone (perfect-foresight GREEN) | 664 | 1.48 | +0.204R | +135.27R | 22.93R | 1.42/1.55 |
| VR engine (real-time SPY) | 447 | 0.77 | -0.111R | -49.83R | 82.54R | — |
| VR standalone (perfect-foresight GREEN) | 259 | 1.34 | +0.121R | +31.36R | 17.95R | 1.63/1.04 |

## What Went Wrong

The acceptance study used `classify_spy_days()` — a function that knows SPY's full-day return and classifies each date as GREEN (>+0.05%), RED (<-0.05%), or FLAT. This is **perfect foresight**: at 10AM signal time, the study already knows whether the day will end GREEN.

The engine uses `spy_snap.pct_from_open` at signal time — a real-time reading of SPY's current % change from open. At 10AM, SPY might be +0.10% (looks GREEN), but by close it's -0.50% (actually RED). The real-time filter admits ~30-40% more trades than the perfect-foresight filter. Those extra trades are overwhelmingly losers.

### VKA Specifically

Engine VKA produced 951 trades (43% more than standalone's 664). The extra 287 trades fired on days that looked GREEN at signal time but didn't end GREEN. VKA monthly breakdown in-engine: Dec -33.61R, Jan -43.41R, Feb -52.32R, Mar +21.02R — only March is positive.

## Implications

### For VKA

VKA's acceptance mechanic (touch VWAP → hold 2 bars → expansion close → 2.0R target) is not inherently broken. The state machine produces reasonable entries. The problem is that on non-GREEN days, those entries get stopped out. The setup needs GREEN-day support to work, and we can't reliably identify GREEN days in real-time at 10AM.

### For VR (Current Active Candidate)

VR has the same problem. The validated +27.65R result from the robustness study used a harness-level GREEN filter (perfect foresight). In-engine with real-time SPY, VR is -49.83R. **The current paper-trade candidate may be trading on days the harness wouldn't have approved.**

This means:
1. Track 2 (candidate_v2_vrgreen_bdr) paper trading results may not match the backtest because the backtest used perfect-foresight filtering while the engine uses real-time filtering.
2. Any setup that depends on GREEN-day filtering faces the same gap between backtest and live.

### For the Research Program

The "acceptance beats proximity" thesis was validated in the standalone study — but the standalone study's GREEN filter was doing most of the work, not the acceptance mechanic. The acceptance mechanic improves entries vs baselines (fewer stops, lower MaxDD), but the absolute edge requires GREEN-day selection.

## What Could Fix This

Without building a better real-time GREEN proxy, these setups remain backtest-only artifacts. Some options to explore (not implemented — user said don't optimize broadly):

1. **Tighter real-time threshold**: Require SPY pct_from_open > +0.15% or +0.20% at signal time (filters more aggressively, but also kills good trades early in the day when SPY hasn't moved much)
2. **SPY above VWAP proxy**: Instead of pct_from_open, require SPY close > VWAP (already available via spy_snap.above_vwap). This is structural rather than threshold-based.
3. **Multi-bar momentum**: Require SPY rising over last 3-5 bars (not just current bar vs open)
4. **Delayed entry window**: Only trade after 10:30 when SPY character is more established
5. **Accept the gap**: Use the GREEN filter as a day-selection rule that requires human judgment or an overnight model, not real-time automation

## Recommendation

1. **Do not promote VKA over VR.** VKA is worse in-engine than VR (both bad, but VKA is worse).
2. **Investigate the VR paper-trade gap.** If VR paper results are tracking closer to the -49.83R engine result than the +27.65R backtest result, the GREEN-day dependency is confirmed as a live problem.
3. **Research a better real-time GREEN proxy.** The acceptance mechanics are sound — the problem is tradeable day selection. This is the next research frontier.
4. **Keep VKA wired in shadow mode.** If a better GREEN proxy is found, VKA's underlying acceptance edge could re-emerge.
