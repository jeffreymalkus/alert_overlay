# Failed-Breakout-Short Family — Core Selection Note

## Decision

The family core is **ORH_A + PDH_B**. This is the only configuration that improves on every metric that matters without degrading any.

| Config | N | PF | Exp | TotalR | MaxDD |
|---|---|---|---|---|---|
| ORH_A alone | 16 | 1.99 | +0.493 | +7.9 | 2.0 |
| **ORH_A + PDH_B** | **27** | **2.25** | **+0.463** | **+12.5** | **2.0** |
| ORH_A + ORH_B + PDH_B | 45 | 1.83 | +0.314 | +14.1 | 3.0 |
| Full family (all 4) | 54 | 1.67 | +0.272 | +14.7 | 4.4 |

## Why ORH_A (not ORH_B)

ORH_A is the premium failed-retest entry. Break above ORH → failure → retest from below → bearish rejection candle. This is the textbook trapped-participant sequence: longs who bought the breakout, held through the failure, and re-entered on the retest are now trapped with stops above ORH.

ORH_A: PF=1.99, 50% WR, 0% time exits. Every trade resolves cleanly at stop or target. Walk-forward PF=2.52. Adding ORH_B degrades family PF from 2.25 to 1.83 and increases MaxDD from 2.0 to 3.0 for +1.6R of marginal gain.

## Why PDH_B (not PDH_A)

The inverted mode dynamics between ORH and PDH reveal how the two levels differ structurally. At ORH, the level is close to the open so retests are precise and the trapped-participant mechanics are tight — Mode A (retest+rejection) works well. At PDH, the level is further from the open so the breakout crowd is thinner and retests are less precise — Mode A has N=9 and PF=1.11.

But at PDH, when a breakout fails and no retest materializes, the continuation short works better than at ORH. PDH_B: PF=3.31, WalkFwd PF=3.00, only 18.2% stop rate. The trapped longs at PDH who don't get a retest opportunity to exit are still holding when price breaks the failure bar low — they capitulate in bulk.

PDH_A is held back: N=9 (fails N≥10 threshold), PF=1.11, train PF=0.86. Adding PDH_A to the core would push MaxDD from 2.0 to 4.4 for +0.6R.

## Structural Diversification

ORH_A and PDH_B have zero day+symbol overlap. They fire on completely different symbols on different days. This is genuine diversification within a single thesis family.

Against the full portfolio (9 strategies, 186 trades aligned):

- Portfolio is 99.5% LONG, 99.5% GREEN-day trades
- Family core is 100% SHORT, 100% RED-day trades
- Zero common trading days between portfolio and family core
- Family core adds 18 new active days where the portfolio had zero exposure
- Portfolio PF: 1.81 → 1.88 with family core added
- Portfolio TotalR: +43.5 → +56.0 (+12.5R added)
- Portfolio MaxDD: 9.2 → 9.2 (unchanged)

The family core gives the portfolio its first real short-side exposure on RED days where the long portfolio is idle.

## Status Labels

| Sub-strategy | Status | Default |
|---|---|---|
| ORH_FBO_V2_A | PAPER (active paper candidate) | On |
| PDH_FBO_B | PAPER (active paper candidate) | On |
| ORH_FBO_V2_B | PROBATIONARY | Off by default |
| PDH_FBO_A | SHELVED | Off |

## Files

- `live/orh_fbo_short_v2_live.py` — ORH_A + ORH_B live (both modes active in live, tracked separately)
- `live/pdh_fbo_short_live.py` — PDH_B live (Mode A off, Mode B on)
- `shared/strategy_registry.py` — canonical status labels
- `shared/config.py` — `pdh_` config params
- `pdh_fbo_short.py` — PDH replay strategy
- `dashboard.py` — 11 strategies total (3 on 1-min, 8 on 5-min)
