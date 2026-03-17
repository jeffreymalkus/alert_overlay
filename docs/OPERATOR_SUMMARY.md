# Paper Trading Operator Summary — candidate_v2_vrgreen_bdr

## What This Portfolio Trades

### LONG: VWAP Reclaim + Hold (VWAP RECLAIM)
- **When**: GREEN SPY days only (SPY % from open > +0.05%)
- **Window**: 10:00–10:59 AM ET (one 5-min bar window)
- **Setup**: Price was below VWAP → reclaims above → holds above for 3+ bars → bullish trigger bar fires
- **Entry**: Close of trigger bar (bullish candle, body ≥40% of range, volume ≥70% of 20-bar vol MA)
- **Stop**: Hold-period low minus $0.02 buffer
- **Target**: Entry + 3.0× risk (R:R = 3.0)
- **Exit**: Target hit, stop hit, or EOD flat (whichever comes first)
- **Context gates**: Market trend must not oppose (market_align = hard gate)
- **One trade per symbol per day**

### SHORT: Breakdown-Retest Short (BDR SHORT)
- **When**: RED+TREND SPY days only (SPY % from open < −0.05%, close in bottom 25% of day range)
- **Window**: Before 11:00 AM ET
- **Setup**: Support breakdown → weak retest toward level → rejection with big upper wick (≥30% of bar range)
- **Entry**: Close of rejection bar
- **Stop**: Above retest high + 0.30× intra ATR buffer
- **Target**: None (time exit)
- **Exit**: After 8 bars or EOD, whichever first
- **No quality gate** (wick filter is the sole quality control)

## What This Portfolio Does NOT Trade

| Setup | Status | Reason |
|---|---|---|
| VWAP KISS (VK) | Retired from active | +1.58R on 33 trades, 51.5% stop rate, ex-best-day negative |
| Second Chance (SC) | Shadow only | Negative expectancy, asymmetric exit problem |
| Spencer | Shadow only | Not validated for this portfolio |
| 9EMA Reclaim | Shadow only | Not validated for this portfolio |
| 9EMA FPIP | Shadow only | Not validated for this portfolio |
| SC V2 | Shadow only | Not validated for this portfolio |
| Failed Bounce | Rejected | -43 pts, no path to positive |
| MCS | Rejected | 75% EOD WR but stops destroy the book |
| Reversals | Rejected | BOX_REV, MANIP, VWAP_SEP — all negative |

## Key Numbers (Backtest: Dec 2025 – Mar 2026)

| Metric | Value |
|---|---|
| Total trades | 330 |
| PF(R) | 1.22 |
| Expectancy | +0.084R per trade |
| Total R | +27.65R |
| Max DD | 26.86R |
| Train/Test PF | 1.25 / 1.18 |
| Stop rate | 25.8% |
| Quick-stop rate | 2.1% |

## Risk Warnings

1. **Ex-best-day fragile**: Remove Mar 9 (+25.75R) → book drops to +1.90R
2. **Monthly inconsistency**: Dec −12.31R, Jan −8.82R, Feb +8.62R, Mar +40.16R
3. **DD duration**: Longest drawdown was 151 trades (~2 months)
4. **Single entry window**: All VR longs fire at 10:00–10:59. No afternoon entries.

## Daily Checklist

1. Check SPY direction by 10:00 AM — if GREEN, VR is active; if RED, only BDR is active
2. VR signals fire between 10:00–10:59 — monitor that window
3. BDR signals fire between 10:00–11:00 on RED+TREND days
4. All positions flatten at EOD or on stop/target hit
5. Max one VR trade per symbol per day
6. Log all signals including shadow setups for research

## Config Reference

```
Config file:  alert_overlay/portfolio_configs.py
Function:     candidate_v2_vrgreen_bdr()
Track:        2 — ACTIVE PAPER CANDIDATE
```
