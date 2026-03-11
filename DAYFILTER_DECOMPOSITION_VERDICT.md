# Day-Filter Decomposition — Verdict

## The Matrix

|                              | VR (hold=3, 3.0R) |         | VKA (hold=2, 2.0R) |         |
|------------------------------|---:|---:|---:|---:|
|                              | **N** | **TotalR** | **N** | **TotalR** |
| 1. Standalone + Perfect GREEN | 767 | **+183.79R** | 1368 | **+251.31R** |
| 2. Standalone + Real-time SPY | 948 | +38.05R | 1615 | +31.28R |
| 3. Engine + Real-time SPY     | 447 | -49.83R | 951 | -108.31R |

## Two-Stage Collapse

The failure is not a single cause. It's two distinct problems stacking on top of each other.

### Stage 1: Day-filter substitution (perfect → real-time) — ~60% of collapse

| Setup | ΔR | ΔPF | ΔN | % of total |
|-------|---:|----:|---:|-----------:|
| VR  | -145.74R | -0.38 | +181 | 62% |
| VKA | -220.02R | -0.34 | +247 | 61% |

The standalone results already collapse when the perfect-foresight GREEN filter is replaced with the real-time proxy, even though the setup mechanics, trade simulation, and all other logic are identical. VR drops from PF 1.44 to PF 1.06. VKA drops from PF 1.38 to PF 1.03. Both barely positive.

**Root cause — dual failure of the real-time proxy:**

Out of 62 trading days, the real-time filter has:
- **12 false-positive days**: Look GREEN at 10AM, end RED or FLAT. These inject 152-238 RED-day trades (PF 0.53-0.62) and 88-147 FLAT-day trades (PF 0.36-0.48). Total drag: -86 to -118R.
- **3 false-negative days**: Actually GREEN by close, but SPY hasn't moved enough at 10AM. These miss 77-187 high-quality trades (PF 2.45-2.93) worth +59 to +102R.

The real-time proxy is failing in both directions: letting in losers AND missing winners.

### Stage 2: Engine integration drift — ~40% of collapse

| Setup | ΔR | ΔPF | ΔN | % of total |
|-------|---:|----:|---:|-----------:|
| VR  | -87.88R | -0.30 | -501 | 38% |
| VKA | -139.60R | -0.26 | -664 | 39% |

Even with the same real-time filter, the engine produces significantly fewer trades (VR: 447 vs 948, VKA: 951 vs 1615) and worse results than the standalone. The engine suppresses roughly half of the trades the standalone fires.

**Likely causes of engine drift (not yet isolated individually):**
- Cooldown gates (alert_cooldown_bars) suppressing repeat signals
- Quality gate (min_quality) rejecting some otherwise-valid triggers
- Regime requirement (require_regime) blocking signals on rotation days
- Risk floor check (min_stop_dist) rejecting trades with tight stops
- State machine differences: engine VKA touch/hold logic may differ subtly from standalone
- VWAP calculation differences: engine uses running VWAP with potential volume weighting differences

## Shared Trades Analysis

The trades that fire in BOTH perfect-foresight and real-time (same entry, same time) tell the real story:

| Setup | Shared N | Shared PF | Shared TotalR |
|-------|:--------:|:---------:|:-------------:|
| VR    | 690      | 1.32      | +124.37R      |
| VKA   | 1181     | 1.25      | +148.93R      |

These shared trades are solidly positive — PF 1.25-1.32. The setup mechanics work on GREEN days. The problem is exclusively about which days to trade on.

## The Core Finding

**The day-selection model is the primary bottleneck (60%), but engine drift is a real secondary problem (40%).** Both need to be addressed.

The research program's acceptance thesis ("hold → expand beats proximity") was validated by the shared-trades analysis. When the GREEN filter correctly selects the day, VKA's acceptance mechanic produces PF 1.25 with 1181 trades. The setup mechanics are sound. The failure is in day selection.

## Implications

### 1. All prior backtest results that used perfect-foresight GREEN are overstated for live trading

This affects:
- VR validated result (+27.65R, PF 1.22)
- VKA acceptance study (+135.27R, PF 1.48)
- All three-hypothesis study results from prior sessions
- Any setup that was tested only on GREEN days

### 2. The next research priority is a real-time day-selection model

The entry pattern research is done until the day-selection problem is solved. No entry mechanic — acceptance or otherwise — can overcome 12 false-positive days injecting hundreds of losing trades.

### 3. Engine drift is a separate problem worth investigating

Even after solving day-selection, the 40% engine-drift gap means the standalone backtest results won't directly translate. Cooldown, quality gates, and state machine differences need to be audited.

## Next Steps (in priority order)

1. **Build a real-time day-selection model** using only live-available information: SPY price vs VWAP, SPY EMA structure, pre-market data, sector breadth, etc. Test against the 62-day sample to see if any combination can approximate the perfect-foresight classification.

2. **Audit engine drift** on VR (smaller, better understood): run standalone VR with real-time filter, then systematically add engine constraints (cooldown, quality, regime, risk floor) one at a time to find the dominant suppressors.

3. **Do not deploy any long setup to live/paper trading until the day-selection gap is characterized.** The current VR paper candidate is running with a real-time filter that admits ~30% false-positive days.
