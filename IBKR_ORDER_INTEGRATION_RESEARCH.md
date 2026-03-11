# IBKR Order Integration — Feasibility Research

**Date:** 2026-03-09
**Status:** Research only — no code changes to frozen system

---

## 1. FEASIBILITY: Can we do this?

**Yes.** The pieces are already in place:

| Requirement | Current State |
|---|---|
| IBKR connection | Dashboard already connects via `ib_insync` for bar data |
| Alert data completeness | `_signal_to_dict()` already emits: symbol, direction, entry, stop, target, shares (calculated client-side), book, regime |
| Order API | `ib_insync` provides `ib.bracketOrder()` and `ib.placeOrder()` natively |
| UI framework | Dashboard already has alert cards with action buttons (delete exists) |

The `Signal` dataclass already contains every field needed to construct a bracket order: `entry_price`, `stop_price`, `target_price`, `direction`, and the dashboard UI already computes share count from the risk-per-trade sizing widget.

---

## 2. HOW IT WOULD WORK

### Architecture: Order Panel (recommended — your "safer" option)

```
Alert fires → Card rendered with "Trade" button
  → Click opens Order Panel overlay
  → Panel shows: symbol, direction, entry (limit), stop, target, shares
  → User reviews / adjusts values
  → "Send to IBKR" button
  → Dashboard backend receives POST /api/order
  → Backend constructs bracket order via ib_insync
  → Sends to TWS with transmit flag protocol
  → Confirmation pushed back to UI via SSE
```

### Bracket Order Construction (ib_insync)

```python
from ib_insync import IB, Stock, LimitOrder, StopOrder, Order

# Parent = limit order at entry price
parent = LimitOrder(
    action="BUY",           # or "SELL" for shorts
    totalQuantity=shares,
    lmtPrice=entry_price,
    transmit=False           # DON'T send yet
)

# Take profit = limit on opposite side
take_profit = LimitOrder(
    action="SELL",
    totalQuantity=shares,
    lmtPrice=target_price,
    transmit=False
)
take_profit.parentId = parent.orderId

# Stop loss = stop on opposite side
stop_loss = StopOrder(
    action="SELL",
    totalQuantity=shares,
    auxPrice=stop_price,
    transmit=True            # THIS triggers all three
)
stop_loss.parentId = parent.orderId

# Place all three — only the last one's transmit=True fires the group
for order in [parent, take_profit, stop_loss]:
    ib.placeOrder(contract, order)
```

The `transmit=False` → `transmit=True` pattern is IBKR's atomic bracket protocol. The parent and take-profit are queued but NOT sent to the exchange. When the stop-loss (last child) arrives with `transmit=True`, TWS sends all three atomically. This prevents the "missing parent" error and prevents partial bracket execution.

---

## 3. WHAT'S ALREADY THERE vs. WHAT NEEDS BUILDING

### Already exists:
- **IBKR connection management** — `dashboard.py` connects, qualifies contracts, maintains persistent IB session
- **Complete signal data** — entry, stop, target, direction, quality, regime, book classification
- **Client-side position sizing** — `calcShares(entry, stop)` in `dashboard.html` uses risk-per-trade from the sizing widget
- **SSE event infrastructure** — `_broadcast_sse()` already pushes real-time updates to the UI
- **Alert card UI** — already has action buttons (delete), tags, sizing pills

### Needs building:
1. **Order panel UI** — overlay/modal when clicking alert card "Trade" button
2. **POST /api/order endpoint** — receives order params, constructs bracket, places via ib_insync
3. **Order state tracker** — tracks open orders, fills, cancels (ib_insync provides `ib.openTrades()`, `ib.orderStatusEvent`)
4. **Order confirmation SSE** — push fill/status updates to the UI
5. **Duplicate order guard** — prevent double-clicking from placing duplicate brackets
6. **Paper vs Live port routing** — port 7497 = paper, 7496 = live. Start on paper only.

---

## 4. DERIVED BENEFITS

**Speed.** Current workflow: see alert → open TWS → find symbol → build bracket manually → submit. That's 15-30 seconds on a good day. With the order panel: see alert → click "Trade" → verify → click "Send" → done. Under 3 seconds.

**Accuracy.** Manual entry means fat-fingering stops, wrong share counts, reversed directions. The system pre-fills exact values from the signal — entry, stop, target, shares — all computed from the same logic that generated the alert. No transcription errors.

**Discipline.** You can't fudge the numbers. The panel shows exactly what Portfolio D says the trade should be. No widening stops, no oversizing. If you want to override, you consciously edit the values.

**Logging.** Every order sent gets logged automatically with full signal context — setup, quality, regime, tape permission. The paper_trade_log gets populated directly from execution data, not manual entry.

---

## 5. RISKS

### 5A. Technical Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Missing parent error** | Medium | Solved by transmit flag protocol (parent transmit=False, last child transmit=True). Well-documented, reliable. |
| **Duplicate orders from double-click** | High | Implement client-side debounce + server-side order dedup (track alert_id → order_id mapping, reject if already placed) |
| **IBKR connection drops mid-order** | Medium | ib_insync handles reconnection. But if connection drops between parent and child placement, bracket is incomplete. Mitigation: check `ib.isConnected()` before each placement call; if any fails, cancel the parent immediately. |
| **Partial fills** | Low | IBKR brackets are server-side OCO — once parent fills, stop and target are live on the exchange. Partial fill on parent means child orders auto-adjust proportionally (IBKR native behavior). |
| **Stale prices** | Medium | Entry price from 5-min bar may be stale by the time you click. Mitigation: show current bid/ask in order panel (available via `ib.reqMktData()`), let user adjust entry. |
| **Port confusion (paper vs live)** | Critical | Hard-code paper port (7497) initially. Add explicit "PAPER" badge in UI. Require separate config flag + confirmation dialog to enable live trading. |

### 5B. Operational Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Over-reliance / reduced vigilance** | Medium | Keep the order panel as a review step, not one-click fire. Force the user to see and confirm every order. |
| **Trading outside system rules** | Low | The alert only fires when Portfolio D rules are met. But user could edit the panel values. Log original signal values alongside any user overrides. |
| **Multiple concurrent orders** | Medium | Dashboard could fire multiple alerts in the same bar. Need max concurrent positions limit and daily loss limit enforcement. |
| **Order modification after send** | Medium | Once bracket is live, modifying stop/target requires finding the child orders by parentId. ib_insync supports this but it's more complex. Phase 2 feature. |

### 5C. Market Risks

| Risk | Severity | Notes |
|---|---|---|
| **Slippage on limit entries** | Low | Limit orders may not fill if price moves away. This is actually protective — no fill means no trade. |
| **Stop hunting / gaps** | Normal | Same risk as manual trading. Bracket stop is server-side, so it executes even if your machine disconnects. This is an improvement over manual stops. |
| **Fast market / OCO overfill** | Very Low | In extreme volatility, both target and stop could theoretically fill. IBKR docs acknowledge this edge case. Use `ocaType=3` (reduce with block) for overfill protection. |

---

## 6. RECOMMENDED IMPLEMENTATION PATH

### Phase 1 — Paper Trading Only (build first)
- Order panel UI (modal overlay on alert card click)
- POST /api/order endpoint with bracket construction
- Paper port (7497) hard-coded
- Duplicate order guard
- Order status display in UI
- Automatic paper_trade_log population from fills

### Phase 2 — Monitoring & Adjustment
- Real-time P&L display per open position
- Order modification (move stops, adjust targets)
- Cancel order functionality
- Position summary panel

### Phase 3 — Live Trading (only after paper validation)
- Config flag to enable live port (7496)
- Confirmation dialog with explicit "LIVE TRADING" warning
- Daily loss limit enforcement (auto-disable after max loss)
- Fill quality analysis vs signal prices

---

## 7. KEY IMPLEMENTATION DETAIL: Shared IB Connection

The dashboard already maintains an `IB()` connection for bar data. The order system would share this same connection — no second login needed. ib_insync supports running market data and order management on the same connection.

However: the current dashboard uses one `IB()` instance per `SymbolRunner`. For order management, we'd want a single shared `IB()` instance that handles both bar subscriptions AND order placement. This is a minor refactor — centralize the IB connection and pass it to both the bar trackers and the order manager.

---

## 8. VERDICT

**Feasible, high-value, moderate complexity.**

The signal data already contains everything needed. The IBKR connection already exists. The transmit flag protocol solves the bracket atomicity problem. The main work is UI (order panel) and a ~100-line order manager class.

**Start on paper.** The paper trading port is identical to live except no real money moves. Build it, validate it, and only then consider live.

**Biggest risk is not technical — it's behavioral.** Making it too easy to trade can lead to overtrading. The order panel review step is essential friction.
