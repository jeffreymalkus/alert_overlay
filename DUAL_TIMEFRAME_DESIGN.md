# Dual-Timeframe Shared Indicator Architecture

## Problem

All indicators are computed on 5-minute bars. EMA9 needs 9 bars (45 min) and EMA20 needs 20 bars (100 min) to warm up. This makes EMA9_FT structurally broken (1 usable bar in its window) and degrades EMA_FPIP and SP_ATIER (miss all morning setups).

The root cause: intraday indicators should not be gated by 5-minute warmup. The 1-minute bars already exist in the pipeline — they are built by BarAggregator, then discarded after BarUpsampler produces the 5-min bar.

## Design Principle

**Indicators live on 1-minute bars. Strategies consume whichever timeframe they need.**

One SharedIndicators instance per symbol, updated on every 1-minute bar. It maintains both 1-min and 5-min snapshots. Strategies declare which timeframe they operate on. The manager routes the correct snapshot to each strategy.

## Current Architecture

```
Ticks → BarAggregator(1min) → BarUpsampler(5min) → StrategyManager.on_bar(5min_bar)
                                                          │
                                                     SharedIndicators.update(5min_bar)
                                                          │
                                                     snap → all 8 strategies
```

1-min bars are created, then thrown away after upsampling.

## Proposed Architecture

```
Ticks → BarAggregator(1min) ──┬──→ StrategyManager.on_1min_bar(1min_bar)
                              │         │
                              │    SharedIndicators.update_1min(bar)
                              │         │
                              │    1min_snap → 1min strategies (EMA9_FT)
                              │
                              └──→ BarUpsampler(5min)
                                        │
                                   StrategyManager.on_5min_bar(5min_bar)
                                        │
                                   SharedIndicators.update_5min(bar)
                                        │
                                   5min_snap → 5min strategies (SC, FL, SP, HH, EMA_FPIP, BDR, BS)
```

## Key Design Decisions

### 1. SharedIndicators becomes dual-timeframe

SharedIndicators maintains two parallel indicator sets:

```python
class SharedIndicators:
    # 1-min indicators (updated every 1-min bar)
    ema9_1m = EMA(9)       # ready at 9:39 (9 bars)
    ema20_1m = EMA(20)     # ready at 9:50 (20 bars)
    vwap = VWAPCalc()      # shared — VWAP is timeframe-independent
    atr_1m = ATRPair(14)   # intraday ATR on 1-min
    vol_buf_1m = deque(20) # 1-min volume MA

    # 5-min indicators (updated every 5-min bar)
    ema9_5m = EMA(9)       # ready at 10:15 (9 bars)
    ema20_5m = EMA(20)     # ready at 11:10 (20 bars)
    atr_5m = ATRPair(14)   # intraday ATR on 5-min
    vol_buf_5m = deque(20) # 5-min volume MA

    # Session state (shared, updated on every 1-min bar)
    session_open, session_high, session_low
    or_high, or_low, or_ready
    recent_bars_1m = deque(60)   # last 60 1-min bars (~1 hour)
    recent_bars_5m = deque(20)   # last 20 5-min bars
```

VWAP is computed once on 1-min bars (canonical). Session high/low/open update on 1-min. OR (opening range) tracks on 1-min for precision.

### 2. Two snapshot types from one update path

```python
@dataclass
class IndicatorSnapshot:
    """Common snapshot — includes both timeframes."""
    bar: Bar                # the bar that triggered this snapshot
    timeframe: int          # 1 or 5 (which bar produced this)
    bar_idx_1m: int         # 1-min bar count
    bar_idx_5m: int         # 5-min bar count

    # 1-min indicators (always current)
    ema9_1m: float
    ema20_1m: float
    ema9_1m_ready: bool
    ema20_1m_ready: bool

    # 5-min indicators (current only when timeframe==5)
    ema9_5m: float
    ema20_5m: float
    ema9_5m_ready: bool
    ema20_5m_ready: bool

    # Shared (always current)
    vwap: float
    atr: float              # best available (1m intra → 5m intra → daily fallback)
    daily_atr: float
    vol_ma_1m: float
    vol_ma_5m: float
    session_open, session_high, session_low: float
    or_high, or_low: float
    or_ready: bool
    hhmm: int

    # Lookback
    recent_bars_1m: List[Bar]
    recent_bars_5m: List[Bar]
```

One snapshot type. All strategies receive the same object. Each strategy reads the fields it needs. No duplication, no divergence.

### 3. StrategyManager gets two entry points

```python
class StrategyManager:
    def __init__(self, strategies, symbol):
        self.indicators = SharedIndicators()
        self._1min_strategies = [s for s in strategies if s.timeframe == 1]
        self._5min_strategies = [s for s in strategies if s.timeframe == 5]

    def on_1min_bar(self, bar_1m: Bar, market_ctx=None) -> List[RawSignal]:
        """Called on every completed 1-min bar."""
        snap = self.indicators.update_1min(bar_1m)
        signals = []
        for s in self._1min_strategies:
            s._check_day_reset(snap)
            sig = s.step(snap, market_ctx)
            if sig: signals.append(sig)
        return signals

    def on_5min_bar(self, bar_5m: Bar, market_ctx=None) -> List[RawSignal]:
        """Called on every completed 5-min bar."""
        snap = self.indicators.update_5min(bar_5m)
        signals = []
        for s in self._5min_strategies:
            s._check_day_reset(snap)
            sig = s.step(snap, market_ctx)
            if sig: signals.append(sig)
        return signals
```

### 4. LiveStrategy declares its timeframe

```python
class LiveStrategy(ABC):
    def __init__(self, name, direction=1, enabled=True, timeframe=5):
        self.timeframe = timeframe  # 1 or 5
```

Each strategy sets its timeframe in __init__:
- `EMA9FirstTouchLive(cfg)` → timeframe=1
- `SCSniperLive(cfg)` → timeframe=5
- All others → timeframe=5 (for now)

A strategy can be moved between timeframes by changing one line. No code changes inside the strategy — it reads `snap.ema9_1m` or `snap.ema9_5m` as needed.

### 5. SymbolRunner feeds both paths

```python
def _on_tick(self, ticker):
    # ... price extraction, timestamp ...

    # Step 1: Tick → 1-min bar
    completed_1min = self._aggregator.on_tick(price, cum_vol, ts)
    if completed_1min is None:
        return

    # Step 2a: Feed 1-min bar to manager (updates indicators + runs 1-min strategies)
    market_ctx = self._build_market_ctx(completed_1min)
    raw_signals_1m = self._strategy_mgr.on_1min_bar(completed_1min, market_ctx)

    # Step 2b: Upsample to 5-min
    completed_5min = self._upsampler.on_bar(completed_1min)
    if completed_5min is not None:
        # Step 3: Feed 5-min bar to manager (updates 5m indicators + runs 5-min strategies)
        self._bars_received += 1
        market_ctx_5m = self._build_market_ctx(completed_5min)
        raw_signals_5m = self._strategy_mgr.on_5min_bar(completed_5min, market_ctx_5m)
    else:
        raw_signals_5m = []

    # Step 4: Combined alert pipeline
    for sig in raw_signals_1m + raw_signals_5m:
        # ... tape gate → _raw_signal_to_dict → SSE broadcast ...
```

### 6. Warmup uses 1-min historical bars

During `setup()`, the existing IBKR historical data fetch already retrieves 1-min bars for warmup. Currently these are only upsampled to 5-min and fed to the engine. The change: feed them through `on_1min_bar()` first, which also populates the 5-min indicators via the upsampler.

This means by market open:
- EMA9_1m and EMA20_1m are fully warmed from prior-day 1-min bars
- EMA9_5m and EMA20_5m are fully warmed from prior-day 5-min bars
- All strategies ready at 09:30 bar 1

## Strategy Timeframe Assignment

| Strategy    | Current | Proposed | Rationale |
|-------------|---------|----------|-----------|
| EMA9_FT     | 5-min   | **1-min** | Spec: "first close back above 1m 9 EMA". Broken on 5-min. |
| EMA_FPIP    | 5-min   | 5-min    | Works on 5-min with pre-warmed EMAs. Morning gap eliminated. |
| SP_ATIER    | 5-min   | 5-min    | Midday consolidation. Pre-warmed EMAs solve first-hour gap. |
| SC_SNIPER   | 5-min   | 5-min    | Breakout/retest. No 1-min dependency. |
| FL_ANTICHOP | 5-min   | 5-min    | E9/VWAP cross. Could benefit from 1-min precision later. |
| HH_QUALITY  | 5-min   | 5-min    | Opening drive + consolidation. Works on 5-min. |
| BDR_SHORT   | 5-min   | 5-min    | Breakdown/retest. No 1-min dependency. |
| BS_STRUCT   | 5-min   | 5-min    | HH/HL structure + range. Works on 5-min. |

Only EMA9_FT moves to 1-min in the initial change. The architecture supports moving any strategy later without refactoring.

## Performance Impact

Current: 8 strategies × 1 call per 5-min bar = 8 calls per 5 min.

Proposed: 7 strategies × 1 call per 5-min bar + 1 strategy × 1 call per 1-min bar = 7 calls per 5 min + 5 calls per 5 min = 12 calls per 5 min.

SharedIndicators.update_1min() adds ~2-3μs per 1-min bar (EMA + VWAP + session tracking). Negligible — the current total is ~5μs per 5-min bar for all indicators + strategies combined.

## What Changes

### Files modified:
1. **shared_indicators.py** — Dual EMA/ATR/vol sets, two update methods, unified snapshot
2. **manager.py** — Two entry points (on_1min_bar, on_5min_bar), strategy routing by timeframe
3. **base.py** — Add `timeframe` field to LiveStrategy
4. **ema9_ft_live.py** — Change `timeframe=1`, read `snap.ema9_1m` instead of `snap.ema9`
5. **dashboard.py (SymbolRunner._on_tick)** — Feed 1-min bars to manager before upsampling
6. **dashboard.py (SymbolRunner.setup)** — Warmup via 1-min historical replay

### Files NOT modified:
- All other 7 strategy files — they continue reading `snap.ema9_5m` (renamed from `snap.ema9`)
- session_report.py, engine.py, equivalence_test.py — no changes needed
- config.py — no new config params needed

### Snapshot field rename for clarity:
- `snap.ema9` → `snap.ema9_5m` (5-min strategies read this)
- New: `snap.ema9_1m` (1-min strategies read this)
- Same for ema20, vol_ma, atr

This is a breaking rename but only touches the 8 strategy files. Each one gets a find-replace. Clean.

## Migration Path

1. Update SharedIndicators with dual-timeframe support
2. Update IndicatorSnapshot with 1m/5m fields
3. Update StrategyManager with dual entry points
4. Update LiveStrategy base with timeframe field
5. Rename snap.ema9 → snap.ema9_5m in all 7 five-minute strategies (mechanical find-replace)
6. Update EMA9_FT to timeframe=1 and use snap.ema9_1m
7. Update SymbolRunner._on_tick to feed 1-min bars
8. Update SymbolRunner.setup for 1-min warmup path
9. Run equivalence test for 5-min strategies (should be identical — same data)
10. Run targeted test for EMA9_FT on 1-min data (new baseline)

## What This Does NOT Do

- Does not add new strategies
- Does not change any strategy's detection logic
- Does not add new config parameters
- Does not touch the alert pipeline, SSE, or dashboard HTML
- Does not change the legacy rollback path
- Does not require re-testing the 5-min strategies (they get the same 5-min bars and same 5-min indicators)

## Summary

The change is: stop throwing away 1-min bars, compute indicators on them, and let strategies choose their timeframe. One SharedIndicators instance, two update cadences, one unified snapshot, zero duplication of computation.
