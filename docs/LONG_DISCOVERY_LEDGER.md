# Long-Side Discovery Research Ledger
**Date:** 2026-03-10
**Universe:** 64 symbols (watchlist.txt) | 62 trading days (2025-12-08 → 2026-03-09)
**Exit modes tested:** 2R target, 3R target, hybrid (EMA9 trail + 20-bar time stop)

---

## EXECUTIVE SUMMARY

**All 5 hypothesis families failed. Every variant across all exit modes produced negative expectancy.**

Not a single variant achieved PF(R) ≥ 1.0 with meaningful trade count. The best results were:
- H2c (open reclaim, higher-low trigger): PF 0.82, -35R on 469 trades
- H3a (strong gap, strict in-play): PF 0.85, -26R on 259 trades
- H1c (SPY+sector RS, 2R target): PF 0.89, -3R on 41 trades (too few)

None of these are remotely promotable.

---

## HYPOTHESIS-BY-HYPOTHESIS RESULTS

### H1: Relative-Strength Leader Pullback Long — RETIRED

| Variant | Exit | Trades | PF(R) | Exp(R) | TotalR | Train/Test |
|---------|------|--------|-------|--------|--------|------------|
| H1c SPY+sector shallow | 2R | 41 | 0.89 | -0.065 | -2.6 | 1.25/0.68 |
| H1a SPY15 shallow | hybrid | 9 | 0.94 | -0.030 | -0.3 | 2.41/0.00 |
| H1d moderate reclaim | 2R | 141 | 0.72 | -0.185 | -26.1 | 0.63/0.79 |

**Verdict:** RETIRE. The RS filter produces too few trades (9-41) when strict, and negative edge when loose (140 trades, PF 0.48-0.72). The RS-leader concept on 5-min bars doesn't survive cost-on. The shallow pullback filter doesn't select structurally different opportunities — it just re-discovers the same generic long entries with an RS overlay.

### H2: Failed Downside Auction → Reversal Long — RETIRED

| Variant | Exit | Trades | PF(R) | Exp(R) | TotalR | Train/Test |
|---------|------|--------|-------|--------|--------|------------|
| H2c open reclaim HL | 3R | 469 | 0.82 | -0.075 | -35.3 | 0.85/0.79 |
| H2c open reclaim HL | 2R | 469 | 0.81 | -0.079 | -37.2 | 0.81/0.81 |
| H2a VWAP moderate | 2R | 1498 | 0.70 | -0.157 | -236 | 0.72/0.67 |

**Verdict:** RETIRE. High trade counts confirm the thesis generates plenty of entries, but the edge is decisively negative across all variants and exits. The "open reclaim + higher-low" variant (H2c) was the least bad at PF 0.82 — still not viable. The reclaim-after-flush thesis doesn't work because the flush itself indicates real weakness, and reclaims in weak stocks are traps more often than reversals.

### H3: Gap-and-Hold Long in True In-Play Names — RETIRED

| Variant | Exit | Trades | PF(R) | Exp(R) | TotalR | Train/Test |
|---------|------|--------|-------|--------|--------|------------|
| H3a strong gap strict | 3R | 259 | 0.85 | -0.099 | -25.7 | 0.91/0.81 |
| H3a strong gap strict | hybrid | 259 | 0.83 | -0.100 | -25.9 | 0.94/0.76 |
| H3b modest gap loose | 3R | 512 | 0.78 | -0.154 | -79.0 | 0.78/0.78 |

**Verdict:** RETIRE. Closest to interesting with PF 0.85-0.94 in some splits, but still net negative. The gap filter selects the right stocks but the pullback-hold-reexpansion entry doesn't capture edge. High quick-stop rate (28%) indicates entries are catching continuation on exhausted moves. Tightening the gap filter reduces trades without improving PF.

### H4: Tight Consolidation After Strong Opening Drive — RETIRED

| Variant | Exit | Trades | PF(R) | Exp(R) | TotalR | Train/Test |
|---------|------|--------|-------|--------|--------|------------|
| H4c moderate ATR expansion | 2R | 790 | 0.68 | -0.180 | -142.4 | 0.83/0.57 |
| H4a/b/d range boxbreak | all | 0-1 | n/a | n/a | n/a | n/a |

**Verdict:** RETIRE. The "range" compression type found almost zero qualifying consolidations in the data — these patterns simply don't occur frequently enough on 5-min bars in this universe. The ATR compression variant fires frequently (790 trades) but is deeply negative. The consolidation-breakout concept is too noisy at 5-min resolution.

### H5: Trend-Day Second-Leg Long — RETIRED

| Variant | Exit | Trades | PF(R) | Exp(R) | TotalR | Train/Test |
|---------|------|--------|-------|--------|--------|------------|
| H5a moderate shallow | 3R | 1157 | 0.80 | -0.099 | -114 | 0.91/0.71 |
| H5a moderate shallow | 2R | 1157 | 0.78 | -0.109 | -126 | 0.88/0.70 |
| H5c moderate broader | all | 2275 | 0.61-0.73 | neg | -370+ | all neg |

**Verdict:** RETIRE. Generates the most trades but all variants are negative. The second-leg thesis is plausible in theory but the scanner captures too many false trend-days where the "first leg" was noise. Even the strictest variant (0.80 ATR first leg, shallow pullback, second-leg break, VWAP-must-hold) still loses at PF 0.80.

---

## CROSS-CUTTING FINDINGS

### Why All Long Hypotheses Failed

1. **Stop rates are high (30-60%).** Long entries on 5-min bars in this market period get stopped out at elevated rates across all structural mechanisms. This suggests the period tested (Dec 2025 - Mar 2026) is hostile to intraday long strategies broadly — not just specific setup types.

2. **The EMA9 trail kills long trades.** The hybrid exit mode (EMA9 trail + time stop) produces ~25% WR vs ~40% WR with a 2R fixed target. But even the 2R target versions are net negative — the WR improvement isn't enough to overcome the negative edge.

3. **Broader universe dilution.** Running 64 symbols means many entries are in names with poor intraday characteristics. However, the gap-and-hold variant (H3a) which explicitly filters for in-play names also failed, so this alone doesn't explain the failure.

4. **Market regime.** This 62-day period may represent a regime where intraday long edge is structurally thin. If SPY was choppy or downtrending, all long mechanisms would underperform.

### What This Means for the Portfolio

The current SC long (Q≥5, 49 trades, PF 1.19 cost-on) may be the only viable long mechanism in this data period — and it barely survives costs. The discovery program found no structurally different long mechanism that outperforms it.

**Possible next directions:**
- Accept that the long side is weak in this regime and keep the short-heavy portfolio
- Test whether SC long improves with a market-regime filter (GREEN days only)
- Pull a longer data period (6+ months) to test whether long edge exists in different market conditions
- Consider longer hold periods (swing, overnight) which may capture different edge
- Test pure momentum / breakout entries on truly in-play names with fresh catalyst data (not just the static watchlist)
