# Retired Strategy Re-Validation — Final Verdicts

Date: 2026-03-11
Engine: Corrected evaluation standard (prior-window fix, dynamic slippage, train/test split)
Universe: 88 symbols (IBKR-extended)
Date range: 2025-05-12 → 2026-03-09 (207 trading days)
Promotion criteria: N ≥ 10, PF > 1.0, Exp > 0, Train PF > 0.80, Test PF > 0.80

---

## Complete Verdict Table

| # | Setup | N | WR% | PF(R) | Exp(R) | TotalR | MaxDD(R) | TrnPF | TstPF | Verdict |
|---|-------|---|-----|-------|--------|--------|----------|-------|-------|---------|
| 1 | Spencer Long | 34 | 11.8% | 0.18 | -0.512 | -17.4 | 18.2 | 0.12 | 0.25 | DOES NOT SURVIVE |
| 2 | Spencer Both | 45 | 8.9% | 0.15 | -0.534 | -24.0 | 25.9 | 0.03 | 0.28 | DOES NOT SURVIVE |
| 3 | SC V2 | 0 | — | — | — | — | — | — | — | DOES NOT SURVIVE (no trades) |
| 4 | VWAP Reclaim | 4001 | 24.8% | 0.78 | -0.107 | -428.5 | 498.9 | 0.74 | 0.82 | DOES NOT SURVIVE |
| 5 | VK Accept | 2943 | 29.8% | 0.82 | -0.091 | -267.8 | 310.2 | 0.85 | 0.79 | DOES NOT SURVIVE |
| 6 | MCS | 912 | 14.0% | 0.43 | -0.496 | -452.2 | 455.0 | 0.46 | 0.39 | DOES NOT SURVIVE |
| 7 | Failed Bounce | 776 | 17.8% | 0.52 | -0.291 | -225.9 | 230.8 | 0.56 | 0.48 | DOES NOT SURVIVE |
| 8 | EMA Reclaim | 215 | 20.5% | 0.50 | -0.369 | -79.3 | 80.1 | 0.52 | 0.47 | DOES NOT SURVIVE |
| 9 | EMA FPIP | 2 | 0.0% | 0.00 | -0.732 | -1.5 | 1.5 | 0.00 | 0.00 | DOES NOT SURVIVE |
| 10 | EMA Confirm | 0 | — | — | — | — | — | — | — | DOES NOT SURVIVE (no trades) |
| 11 | VWAP Kiss | 403 | 23.1% | 0.34 | -0.596 | -240.3 | 253.6 | 0.34 | 0.34 | DOES NOT SURVIVE |
| 12 | EMA Retest | 11831 | 23.1% | 0.37 | -0.576 | -6820.4 | 6831.0 | 0.40 | 0.35 | DOES NOT SURVIVE |
| 13 | Reversals (BOX_REV/MANIP/VWAP_SEP) | 0 | — | — | — | — | — | — | — | DOES NOT SURVIVE (no trades) |
| 14 | EMA9 Sep (Mean Reversion) | 0 | — | — | — | — | — | — | — | DOES NOT SURVIVE (no trades) |

---

## Summary

**0 of 14 retired setups survive.**

Every retired setup either generated zero trades or failed multiple promotion criteria. No setup achieved PF > 1.0 or positive expectancy.

### Closest to survival (by PF):
1. VK Accept — PF 0.82, 2943 trades, Exp -0.091R (best PF but still negative expectancy)
2. VWAP Reclaim — PF 0.78, 4001 trades, Exp -0.107R
3. Failed Bounce — PF 0.52, 776 trades, Exp -0.291R

### Zero-trade setups (detection logic never fires):
- SC V2, EMA Confirm, Reversals (BOX_REV/MANIP/VWAP_SEP), EMA9 Sep

### Worst performers (by Total R):
1. EMA Retest — -6820.4R (11,831 trades, massively overtrades)
2. MCS — -452.2R
3. VWAP Reclaim — -428.5R

---

## Combined with Promoted Stack Validation

From promoted_replay.py (run earlier this session):

| Setup | N | PF(R) | Exp(R) | TotalR | Verdict |
|-------|---|-------|--------|--------|---------|
| SC Long (promoted) | 236 | 0.48 | -0.309 | -73.0 | DOES NOT SURVIVE |
| BDR Short (promoted) | 90 | 1.09 | +0.042 | +3.8 | SURVIVES |
| EMA Pull Short (promoted) | 26 | 0.94 | -0.053 | -1.4 | DOES NOT SURVIVE |

From rsi_replay.py (RSI audit):

| Setup | N | PF(R) | Exp(R) | TotalR | Verdict |
|-------|---|-------|--------|--------|---------|
| RSI Midline Long | 654 | 0.78 | -0.150 | -98.0 | DOES NOT SURVIVE |
| RSI Bouncefail Short | 1640 | 0.64 | -0.263 | -431.3 | DOES NOT SURVIVE |

---

## Engine-Wide Verdict

**Only 1 setup out of 19 evaluated survives: BDR Short (PF 1.09, +3.8R, 90 trades).**

The entire setup library — promoted, retired, and RSI candidates — has been systematically re-validated under the corrected evaluation standard. The overwhelming majority of setups are net-negative after realistic slippage modeling.

---

## CSV Trade Logs

All trade logs exported to the data directory:
- `replay_retired_spencer_long.csv` (34 trades)
- `replay_retired_spencer_both.csv` (45 trades)
- `replay_retired_sc_v2.csv` (0 trades)
- `replay_retired_retired_vwap_reclaim.csv` (4001 trades)
- `replay_retired_retired_vka.csv` (2943 trades)
- `replay_retired_retired_mcs.csv` (912 trades)
- `replay_retired_retired_failed_bounce.csv` (776 trades)
- `replay_retired_retired_ema_reclaim.csv` (215 trades)
- `replay_retired_retired_ema_fpip.csv` (2 trades)
- `replay_retired_retired_ema_confirm.csv` (0 trades)
- `replay_retired_retired_vwap_kiss.csv` (403 trades)
- `replay_retired_retired_ema_retest.csv` (11831 trades)
- `replay_retired_retired_reversals.csv` (0 trades)
- `replay_retired_retired_ema9_sep.csv` (0 trades)
