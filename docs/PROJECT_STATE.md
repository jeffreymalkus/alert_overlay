# Portfolio D — Project State

**Last updated:** 2026-03-22

---

## Status Taxonomy

### ACTIVE (live trading — 7 strategies)

**Production sleeve** (`production_sleeve.py` — single source of truth):
| Strategy | Type | N | PF | TotalR |
|----------|------|---|---|--------|
| EMA_FPIP_V3_B | Long continuation | 139 | 2.31 | +61.8 |
| HH_QUALITY | Long continuation | 90 | 2.04 | +27.5 |
| SP_V2_SIMPLE | Long continuation | 61 | 1.90 | +21.3 |
| BIG_DAWG_LONG_V1 | Long continuation | 20 | 5.60 | +7.0 |
| BS_STRUCT | Long continuation | 5 | 7.97 | +2.8 |
| ORH_FBO_V2_B | Short failure | 17 | 5.46 | +17.9 |
| BDR_V3_C | Short breakdown | 15 | 3.09 | +8.7 |
| **TOTAL** | | **347** | **2.37** | **+147.0** |

**Disabled strategies** (`config.disabled_strategies` — Gate -1 silent drop):
| Strategy | Reason | Status |
|----------|--------|--------|
| ORH_FBO_V2_A | PF=0.81, -9.3R worst in portfolio | Permanently disabled |
| EMA9_V5_C | Median RR=0.29, structural R:R problem on 1-min bars | Preserved for research |
| EMA9_V6_A | 5-min redesign, promising R:R but N=5 gated sample | Preserved for research |

**Active infrastructure:**
| Component | Description |
|-----------|-------------|
| Dashboard server | `dashboard.py` + `dashboard.html`, reqMktData streaming |
| Order manager | Bracket order placement via IBKR paper |
| Layered regime gate | SPY/QQQ tape permission for long-side filter |
| InPlayProxyV2 objective gate | 0–10 bucketed score (move, RS, gap, range-exp, RVOL), per-strategy hard floor |
| IP gate bypass set | GGG_LONG_V1, BIG_DAWG_LONG_V1 skip IP gate (internal logic is sufficient) |
| BIG_DAWG admissibility filters | Projected RR band [0.50–1.10], bullish counter-wick ≤0.20 |
| BS_STRUCT admissibility filters | time ≤12:30, close_loc ≥0.70, body ≥0.70, wick ≤0.15, struct_q ≥0.50, confluence ≥2 |
| ORH_FBO_V2_B recovery rule | 12:00–14:00 window + counter_wick ≤0.15 bypass for late-session signals |

### INFRASTRUCTURE COMPLETE (operational, not yet live-validated)
| Component | Description | Dependency |
|-----------|-------------|------------|
| Dual universe (STATIC/IN_PLAY/BOTH) | Full lifecycle: add/remove/clear, auto-subscribe, universe badges | Market data stream |
| In-play history capture | `in_play_history.csv` audit log + `in_play_snapshots/` daily archives | Daily use |
| Habitat validation study | `habitat_validation_study.py` — full R-first metrics, flat + snapshot mode | In-play history |
| Operational test suite | `test_inplay_ops.py` — 25 automated checks against live dashboard | Running dashboard |
| Alert CSV export | `/api/export-alerts` with universe column | None |

### RETIRED (tested, no edge, do not reopen)
| Component | Verdict |
|-----------|---------|
| Long discovery Wave 1 | 5 hypotheses, 18 variants, ALL negative. `LONG_DISCOVERY_LEDGER.md` |
| RS Leader Pullback (H1) | PF 0.42–0.68, all variants fail |
| Failed Auction Reversal (H2) | PF 0.31–0.55, catastrophic |
| Gap-and-Hold (H3) | PF 0.48–0.71, not viable |
| Tight Consolidation (H4) | PF 0.39–0.62, no edge |
| Second-Leg Long (H5) | PF 0.44–0.59, regime-dependent ruin |
| Composite long (exploratory) | VK_green_leader PF 1.32 but GREEN-day is hindsight. `COMPOSITE_LONG_LEDGER.txt` |
| Composite long (live ablation) | ALL live variants net negative, best PF 0.86. `COMPOSITE_LONG_ABLATION.txt` |
| Long regime classifier | Morning features (14) have zero predictive power for afternoon longs. `LONG_REGIME_CLASSIFIER_OUTPUT.txt` |
| Long regime classifier v2 | Multi-cutoff (9:45/10:00/10:15) breadth study. Found separation at 10:15 but not tradeable. `LONG_REGIME_CLASSIFIER_V2_OUTPUT.txt` |
| **Gate B long candidate** | **RETIRED.** PF 1.41 (62d) → 0.98 (207d). Gate separates but deploy side net negative after costs. `REGIME_GATED_LONG_EXPANDED_OUTPUT.txt` |
| SPENCER setup | Removed from config, no edge |
| EMA SCALP setup | Removed from config, no edge |
| SC V2 setup | Removed from config, no edge |
| EMA FPIP setup | Removed from config, no edge |
| Trend setups | Removed from config, no edge |

### RESEARCH-ONLY (not for live trading yet)
| Component | Status | Next step |
|-----------|--------|-----------|
| Bottleneck analysis | Complete. Regime = dominant factor | `LONG_BOTTLENECK_MEMO.md` |
| True in-play catalyst universe | Proposed, not built | Needs real in-play history first |
| Longer data pull (6–12mo) | **COMPLETE** — 207 trading days (2025-05-12 → 2026-03-09), 101 symbols | Used for Gate B validation |
| `universe_comparison_study.py` | Quick comparison tool | Superseded by habitat study |

---

## Top 3 Blockers

1. **Market data entitlement / real-time stream** — reqMktData subscriptions must produce trustworthy live ticks for all symbols including dynamically-added in-play names. Until confirmed working end-to-end, the dashboard cannot produce reliable live alerts. This is the single gating blocker for live trading.

2. **In-play history accumulation** — The history capture system is built (`in_play_history.csv` + daily snapshots), but zero real trading days have been logged. Need 20+ trading days of daily in-play tagging before `habitat_validation_study.py` can produce meaningful STATIC vs IN_PLAY comparisons.

3. **Long-side edge** — Seven research threads have failed to find a cost-positive long entry on this universe with available data. The long branch is retired. Reopening would require fundamentally different inputs (real premarket catalysts, institutional flow data, or completely different entry mechanics).

---

## In-Play History Format

### Audit log: `in_play_history.csv`
Append-only CSV written on every add/remove/clear action.
```
date,time,symbol,action
2026-03-10,09:15:22,TSLA,ADD
2026-03-10,09:15:45,SMCI,ADD
2026-03-10,15:55:00,TSLA,CLEAR
2026-03-10,15:55:00,SMCI,CLEAR
```

### Daily snapshots: `in_play_snapshots/YYYY-MM-DD.txt`
One file per trading day, one symbol per line. Written on every add/remove/clear (overwrites for that date). Used by `habitat_validation_study.py --snapshots` for date-aware universe assignment.

### API endpoint: `GET /api/iplog`
Returns JSON with both history rows and snapshot summaries.

### Dashboard widget: Session Log
Visible inside the DAILY IN-PLAY collapse panel. Shows today's ADD/REMOVE/CLEAR events with timestamps, colored action tags, and an archive day counter. Data loads via `fetch('/api/iplog')` on page init and updates live via SSE `in_play_updated` events carrying `action` + `symbol`.

### How to verify it is working
```bash
# 1. Start dashboard
python -m alert_overlay.dashboard --no-ibkr

# 2. Run 25-check operational test
python -m alert_overlay.test_inplay_ops
# Expected: 25/25 passed

# 3. Confirm files on disk
cat alert_overlay/in_play_history.csv   # should have rows
ls alert_overlay/in_play_snapshots/     # should have YYYY-MM-DD.txt files

# 4. Confirm API
curl -s http://localhost:8877/api/iplog | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'events={len(d[\"history\"])} snapshots={len(d[\"snapshots\"])}')"
```

### How habitat_validation_study.py uses it
- **Flat mode** (default): reads `in_play.txt` for current-day membership, assigns universe per symbol.
- **Snapshot mode** (`--snapshots in_play_snapshots/`): reads `YYYY-MM-DD.txt` files, assigns universe per trade based on the trade date's snapshot. This is the date-aware mode needed for real historical comparison.
- Both modes run full R-first metrics (PF, Exp, Total R, Max DD, train/test split, ex-best-day, ex-top-symbol) across STATIC vs IN_PLAY vs BOTH.
- Promotion gate: a setup+universe combo must pass all 4 criteria (C1: PF>1.0, C2: Exp>0, C3: train PF>0.8, C4: test PF>0.8) to be flagged for promotion.

---

## Validated Model Numbers

| Metric | Value |
|--------|-------|
| Trades | 72 |
| Win rate | 45.8% |
| PF(R) | 1.42 |
| Expectancy | +0.209R/trade |
| Total R | +15.05 |
| Max DD(R) | 8.73 |
| Train PF | 1.23 |
| Test PF | 1.59 |
| Ex-best-day PF | 1.09 |
| Ex-top-symbol PF | 1.27 |

---

## File Index

| File | Category | Purpose |
|------|----------|---------|
| `config.py` | ACTIVE | Central config — IP hard floors (0–10), disabled_strategies frozenset, ip_v2_gate_bypass_strategies frozenset, BIG_DAWG/BS_STRUCT/ORH_B filter params |
| `models.py` | ACTIVE | Data models (Signal, Bar, SetupId, etc.) |
| `engine.py` | ACTIVE | Signal detection engine |
| `backtest.py` | ACTIVE | Core backtest harness |
| `dashboard.py` | ACTIVE | Live dashboard server |
| `dashboard.html` | ACTIVE | Dashboard UI |
| `order_manager.py` | ACTIVE | IBKR bracket order placement |
| `layered_regime.py` | ACTIVE | Tape permission weights |
| `market_context.py` | ACTIVE | SPY/QQQ market context engine |
| `indicators.py` | ACTIVE | EMA, VWAP, ATR, etc. |
| `habitat_validation_study.py` | INFRASTRUCTURE | Full R-first habitat comparison (flat + snapshot mode) |
| `composite_long_study.py` | RETIRED | Composite long hypothesis — exploratory only, GREEN-day hindsight |
| `composite_long_ablation.py` | RETIRED | Live-only ablation — all variants net negative |
| `long_regime_classifier.py` | RETIRED | Morning feature classifier — no predictive power |
| `regime_gated_long_study.py` | RETIRED | Gate B long — PF 1.41 (62d) → 0.98 (207d), retired |
| `long_regime_classifier_v2.py` | RETIRED | Multi-cutoff regime classifier — separation at 10:15 but not tradeable |
| `REGIME_GATED_LONG_EXPANDED_OUTPUT.txt` | REFERENCE | 207-day expanded validation (Gate B PF 0.98) |
| `extend_history.py` | INFRASTRUCTURE | IBKR historical data extension (monthly chunks) |
| `acceptance_study.py` | REFERENCE | Acceptance entry study (VK PF 1.48, EMA9 PF 1.44, OR PF 1.43) |
| `universe_comparison_study.py` | RESEARCH | Quick universe comparison |
| `test_inplay_ops.py` | INFRASTRUCTURE | Live operational check (10 tests) |
| `in_play.txt` | INFRASTRUCTURE | Current session in-play list |
| `in_play_history.csv` | INFRASTRUCTURE | Append-only audit log |
| `in_play_snapshots/` | INFRASTRUCTURE | Daily snapshot archive |
| `watchlist.txt` | ACTIVE | Static universe symbol list |
| `long_discovery_study.py` | RETIRED | Wave-1 long hypothesis testing |
| `long_bottleneck_study.py` | RETIRED | Regime/habitat/horizon analysis |
| `CANDIDATE_SCORECARD.md` | REFERENCE | Validated model scorecard |
| `LONG_DISCOVERY_LEDGER.md` | REFERENCE | Wave-1 results (all RETIRE) |
| `LONG_BOTTLENECK_MEMO.md` | REFERENCE | Bottleneck diagnosis |
| `COMPOSITE_LONG_LEDGER.txt` | REFERENCE | Exploratory composite long results |
| `COMPOSITE_LONG_ABLATION.txt` | REFERENCE | Live ablation results (all negative) |
| `LONG_REGIME_CLASSIFIER_OUTPUT.txt` | REFERENCE | Morning regime classifier results |
| `ACCEPTANCE_STUDY_LEDGER.md` | REFERENCE | Acceptance entry study results |
| `REGIME_GATED_LONG_OUTPUT.txt` | REFERENCE | Gate B decisive test (PF 1.41, 14d, 139 trades) |
| `LONG_REGIME_CLASSIFIER_V2_OUTPUT.txt` | REFERENCE | Multi-cutoff regime classifier results |
| `PROJECT_STATE.md` | REFERENCE | This file |
