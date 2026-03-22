# Alert Overlay — Roadmap

Updated: 2026-03-22

## Recently Completed (2026-03-22)

### In-Play Gate: Objective 0–10 Scoring — DONE
- Replaced percentile-ranked cross-sectional gate with objective bucketed scoring
- Five components: move-from-open, RS-vs-SPY, gap, range-expansion, RVOL
- Per-strategy hard floors replace single threshold
- Rolling 20-day baselines for RVOL and range-expansion
- Score is symbol-independent (no cross-sectional ranking)
- See `V2_GATE_BASELINES.md` and `CURRENT_STRATEGY_SETTINGS.md`

### BIG_DAWG Admissibility Filters — DONE
- Projected actual_rr band [0.50, 1.10] — rejects fixed_rr_fallback and outlier targets
- Bullish trigger bar counter-wick ≤ 0.20
- Added to IP gate bypass (internal logic sufficient)

### BS_STRUCT Admissibility Filters — DONE
- Time end moved from 13:30 → 12:30
- Bar anatomy: close_location ≥0.70, body_fraction ≥0.70, counter_wick ≤0.15
- Structure/confluence: struct_quality ≥0.50, confluence_count ≥2
- Two-tier rejection: bar anatomy keeps pattern alive, structure/confluence kills pattern

### ORH_FBO_V2_B Recovery Rule — DONE
- Late-session bypass: 12:00–14:00 + counter_wick ≤0.15

### Portfolio Cleanup — DONE
- **ORH_FBO_V2_A disabled**: PF=0.81, -9.3R — worst strategy. Added to `disabled_strategies` (Gate -1).
- **BIG_DAWG ungated**: Added to `ip_v2_gate_bypass_strategies` — ungated PF=5.60, +7.0R.
- **EMA9_V5_C demoted**: Full code-driven audit revealed structural R:R problem on 1-min bars (median RR=0.29, AvgWin=+0.22R). Disabled, preserved for research.
- **EMA9_V6_A created**: 5-min bar redesign fixing R:R geometry (median RR=1.60, AvgWin=+0.66R). Gated N=5 insufficient for conclusions. Disabled, preserved for research.

### System Alignment Audit — DONE
- Confirmed Gate -1 (disabled_strategies) implemented in both replay.py and dashboard.py
- Confirmed IP bypass (ip_v2_gate_bypass_strategies) config-driven in both paths
- Documented known `in_play_score` scale mismatch (replay V1 0-7 vs dashboard V2 0-10)
- Updated all strategy docs to match current 7-strategy active sleeve

### Data Pipeline Fix — DONE (2026-03-22)
- **Problem**: Replay was running on a frozen 88-symbol Alpaca pull from March 12, while live traded a rotating 100-140 symbol universe daily. Every trade on an in-play symbol outside the 88 was invisible to replay.
- **Fixes applied**:
  1. Created `scripts/eod_data_maintenance.py` — end-of-day pipeline that aggregates 1min→5min, builds universe manifest, identifies gaps
  2. Updated `scripts/collect_history_alpaca.py` — `get_all_symbols()` now includes watchlist + in-play + snapshots + recorded symbols
  3. Ran initial aggregation: 219 symbols with 1-min live recordings now have 5-min replay data
  4. Created `data/universe_manifest.json` — tracks which symbols were active on each date
- **Result**: Replay universe expanded from 88 → 306 symbols
- **Remaining**: 13 in-play symbols with NO data at all (AAL, CAPR, CPB, DG, etc.) need Alpaca backfill. 15 symbols from original pull stale at March 6-9 (need 1-min recording or Alpaca refresh).
- **Daily workflow**: Run `python -m alert_overlay.scripts.eod_data_maintenance` after market close. Add `--backfill` flag when Alpaca keys are available to pull missing history.

### Low-N Strategy Gate Audit — DONE (2026-03-22)
- **ORH_FBO_V2_B** (N=17, PF=5.46): 6.0 floor is optimal. Maximizes TotalR=+14.2. Lowering to 5.0 cuts PF from 2.06 to 1.30. Recovery rule (12:00-14:00 + CWF≤0.15) is critical — morning signals PF=0.70.
- **BDR_V3_C** (N=15, PF=3.09): 6.5 floor is correct. Downstream gates (regime/tape/quality) filter 24 IP-passing → 15 final, improving PF from 1.25 to 3.09.
- **BS_STRUCT** (N=5, PF=7.97): 5.0 floor adequate. Even ungated PF=2.03 on 25 trades. Internal admissibility filters (bar anatomy + structure) do the heavy lifting. Candidate for IP bypass when N grows.
- All three strategies have correct gate settings. Low N is structural (narrow selective patterns), not a gate misconfiguration.

## Future Work Items

### Dashboard: Trailing Stop OCO Brackets
- Replace fixed stop-loss leg in IBKR bracket orders with `TRAIL` order type
- Requires position monitoring in dashboard to activate trail after threshold (e.g., +1.0R MFE)
- Data supports this: ORH_FBO_V2_A losers on 3/16 had MFE 1.6–2.7R before reversing through fixed stops
- Implementation: monitor fill → watch price → once +1.0R, submit replacement trailing stop via `ib.placeOrder()`
- Priority: HIGH (directly recovers R from current losing trades)

### Fix in_play_score Scale Mismatch
- replay.py sets `snap.in_play_score` from V1 proxy (0-7 scale)
- dashboard.py sets `snap.in_play_score` from V2 (0-10 scale)
- Strategy-internal checks (e.g. `ema9_v5_min_ip_score = 0.80`) behave differently in replay vs live
- Fix: replay should set `snap.in_play_score` from V2 `_ip_v2_result.active_score` (same as dashboard)
- Impact: Low while EMA9 is disabled, but must be fixed before re-enabling any strategy with internal IP checks

### EMA9 Research (when ready to revisit)
- V6_A showed improved R:R geometry (median RR 0.29→1.60, WalkFwd PF 0.29→0.94)
- Still below 1.0 PF ungated — concept may need additional filtering
- Both V5_C and V6_A preserved in code for future research
- Key files: `ema9_ft_live.py` (V5), `ema9_v6a_live.py` (V6_A), config builders in `production_sleeve.py`

### Objective Gate Calibration
- Run replay sweep on objective hard floors to validate optimal per-strategy thresholds
- Compare N/PF/TotalR under new 0–10 scale vs old percentile baselines
- Validate BIG_DAWG_LONG_V1 at 8.5 floor (highest in portfolio — unused since bypassed, but preserved)

### System Cleanup (lower priority)
- Fix ablation_study.py to use shared gates instead of EnhancedMarketRegime
- Update strategy_registry.py with authoritative replay numbers
- Investigate BDR_SHORT N=0 (completely blocked by time-normalized RED/FLAT thresholds)
- Update OPERATOR_SUMMARY.md and DAILY_CHECKLIST.md to reflect current 7-strategy sleeve (currently describe old Portfolio D VK+BDR system)
