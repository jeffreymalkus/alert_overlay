# Alert Overlay — Roadmap

## Future Work Items

### Dashboard: Trailing Stop OCO Brackets
- Replace fixed stop-loss leg in IBKR bracket orders with `TRAIL` order type
- Requires position monitoring in dashboard to activate trail after threshold (e.g., +1.0R MFE)
- Data supports this: ORH_FBO_V2_A losers on 3/16 had MFE 1.6–2.7R before reversing through fixed stops
- Implementation: monitor fill → watch price → once +1.0R, submit replacement trailing stop via `ib.placeOrder()`
- Priority: HIGH (directly recovers R from current losing trades)

### PF Optimization Filters (candidates from 3/16 deep-dive)
- Time cutoff: no entries after 12:00 (afternoon = dead money)
- Target RR cap: RR ≤ 2.0 hits; RR ≥ 2.5 mostly expires EOD
- FL_ANTICHOP signal quality: 2W/11L on 3/16, needs tighter entry criteria
- ORH_FBO_V2_A stop management: high MFE but reversals kill P&L
- Risk % filter: stops > 1.0% from entry = 0 wins

### System Cleanup (lower priority)
- Fix ablation_study.py to use shared gates instead of EnhancedMarketRegime
- Update strategy_registry.py with authoritative replay numbers
- Investigate BDR_SHORT N=0 (completely blocked by time-normalized RED/FLAT thresholds)
