"""
IBKR Order Manager — Bracket order placement via ib_insync.

Paper trading only (Phase 1). Constructs bracket orders using the
IBKR transmit flag protocol to ensure atomic execution:
  - Parent order: transmit=False (queued, not sent)
  - Take-profit:  transmit=False (queued, not sent)
  - Stop-loss:    transmit=True  (triggers all three atomically)

Threading model:
  ALL IB interactions are dispatched to the main ib_insync event loop
  via asyncio.run_coroutine_threadsafe(). The HTTP handler thread
  submits work and waits for the result. No IB methods are ever called
  from a background thread.

Bracket lifecycle states:
  SUBMITTED      -> all 3 orders sent to TWS
  PRESUBMITTED   -> TWS acknowledged, waiting for market
  ENTRY_FILLED   -> parent filled, exits are live
  EXIT_TARGET    -> take-profit child filled (bracket done)
  EXIT_STOP      -> stop-loss child filled (bracket done)
  CANCELLED      -> user-cancelled or TWS rejected
"""

import asyncio
import logging
import threading
from datetime import datetime
from typing import Optional, Callable
from zoneinfo import ZoneInfo

try:
    from ib_insync import IB, Stock, LimitOrder, StopOrder, Trade, util
except ImportError:
    raise ImportError("ib_insync required: pip install ib_insync")

EASTERN = ZoneInfo("US/Eastern")
log = logging.getLogger("order_manager")

# Timeout for IB operations dispatched to main loop
IB_CALL_TIMEOUT = 15


class OrderManager:
    """Manages bracket order lifecycle on a shared IB connection."""

    def __init__(self, ib: IB, broadcast_fn: Optional[Callable] = None):
        self.ib = ib
        self._broadcast = broadcast_fn or (lambda *a, **kw: None)
        self._lock = threading.Lock()

        # Capture the main event loop (the one ib.run() drives).
        # ALL IB calls are dispatched here via run_coroutine_threadsafe.
        self._loop = util.getLoop()

        # Track placed brackets: alert_id -> bracket info dict
        self._brackets: dict = {}
        self._placed_alerts: set = set()

        # Map ALL order IDs (parent + children) back to alert_id
        # Each entry: orderId -> (alert_id, role) where role is "parent", "tp", or "sl"
        self._orderid_to_alert: dict = {}

        # Subscribe to order status events
        self.ib.orderStatusEvent += self._on_order_status

    # ── Dispatch helper ──

    def _run_on_loop(self, coro):
        """Submit a coroutine to the main IB event loop and block until done.
        Safe to call from any thread (HTTP handler, etc.)."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=IB_CALL_TIMEOUT)

    # ── Async IB operations (run ON the main event loop) ──

    async def _async_place_bracket(self, symbol, direction, entry_price,
                                    stop_price, target_price, shares):
        """Runs entirely on the main IB event loop thread.
        Qualifies contract, builds bracket, places all 3 orders."""

        # Qualify contract (async version — native to this loop)
        contract = Stock(symbol, "SMART", "USD")
        log.info(f"[ORDER] Qualifying contract for {symbol}...")
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            return None, None, None, None, f"Failed to qualify contract for {symbol}"
        log.info(f"[ORDER] Contract qualified: {symbol} conId={contract.conId}")

        # Build bracket orders
        parent_action = "BUY" if direction == 1 else "SELL"
        child_action = "SELL" if direction == 1 else "BUY"

        # Step 1: Place parent (transmit=False — queued only)
        parent = LimitOrder(
            action=parent_action,
            totalQuantity=shares,
            lmtPrice=round(entry_price, 2),
            transmit=False,
        )
        log.info(f"[ORDER] Placing parent {parent_action} {shares} {symbol} @ {entry_price}...")
        parent_trade = self.ib.placeOrder(contract, parent)
        log.info(f"[ORDER] Parent placed: orderId={parent.orderId}")

        # Step 2: Take-profit child (transmit=False)
        take_profit = LimitOrder(
            action=child_action,
            totalQuantity=shares,
            lmtPrice=round(target_price, 2),
            transmit=False,
        )
        take_profit.parentId = parent.orderId
        log.info(f"[ORDER] Placing TP @ {target_price} (parentId={parent.orderId})...")
        tp_trade = self.ib.placeOrder(contract, take_profit)
        log.info(f"[ORDER] TP placed: orderId={take_profit.orderId}")

        # Step 3: Stop-loss child (transmit=True — fires entire bracket)
        stop_loss = StopOrder(
            action=child_action,
            totalQuantity=shares,
            stopPrice=round(stop_price, 2),
            transmit=True,
        )
        stop_loss.parentId = parent.orderId
        log.info(f"[ORDER] Placing SL @ {stop_price} (parentId={parent.orderId}, transmit=True)...")
        sl_trade = self.ib.placeOrder(contract, stop_loss)
        log.info(f"[ORDER] SL placed: orderId={stop_loss.orderId} — bracket transmitted!")

        return parent, take_profit, stop_loss, parent_action, None

    async def _async_cancel_bracket(self, target_ids):
        """Cancel all live legs of a bracket. Runs on main IB event loop."""
        cancelled_count = 0
        for trade in self.ib.openTrades():
            if trade.order.orderId in target_ids:
                log.info(f"[ORDER] Cancelling orderId={trade.order.orderId}")
                self.ib.cancelOrder(trade.order)
                cancelled_count += 1
        return cancelled_count

    # ── Public API ──

    def place_bracket(self, req: dict) -> tuple:
        """
        Place a bracket order from order panel data.
        Called from HTTP handler thread. All IB work dispatched to main loop.

        Returns:
            (True, parent_order_id) on success
            (False, error_message) on failure
        """
        alert_id = req.get("alert_id", "")
        symbol = (req.get("symbol") or "").upper()
        direction = int(req.get("direction", 0))
        entry_price = float(req.get("entry_price", 0))
        stop_price = float(req.get("stop_price", 0))
        target_price = float(req.get("target_price", 0))
        shares = int(req.get("shares", 0))

        # ── Validation (fast, no IB calls) ──
        if not alert_id:
            return False, "Missing alert_id"
        if not symbol:
            return False, "Missing symbol"
        if direction not in (1, -1):
            return False, f"Invalid direction: {direction}"
        if shares <= 0:
            return False, f"Invalid shares: {shares}"
        if entry_price <= 0:
            return False, f"Invalid entry price: {entry_price}"
        if stop_price <= 0:
            return False, f"Invalid stop price: {stop_price}"
        if target_price <= 0:
            return False, f"Invalid target price: {target_price}"

        if direction == 1:
            if stop_price >= entry_price:
                return False, f"Long stop ({stop_price}) must be below entry ({entry_price})"
            if target_price <= entry_price:
                return False, f"Long target ({target_price}) must be above entry ({entry_price})"
        else:
            if stop_price <= entry_price:
                return False, f"Short stop ({stop_price}) must be above entry ({entry_price})"
            if target_price >= entry_price:
                return False, f"Short target ({target_price}) must be below entry ({entry_price})"

        with self._lock:
            if alert_id in self._placed_alerts:
                return False, f"Order already placed for alert {alert_id}"

        if not self.ib.isConnected():
            return False, "IBKR not connected"

        # ── Dispatch entire bracket placement to main IB event loop ──
        try:
            parent, take_profit, stop_loss, parent_action, error = self._run_on_loop(
                self._async_place_bracket(
                    symbol, direction, entry_price,
                    stop_price, target_price, shares,
                )
            )
        except Exception as e:
            log.error(f"[ORDER] Bracket placement failed: {e}", exc_info=True)
            return False, f"Bracket placement failed: {e}"

        if error:
            return False, error

        # Record the bracket
        bracket_info = {
            "alert_id": alert_id,
            "symbol": symbol,
            "direction": "LONG" if direction == 1 else "SHORT",
            "shares": shares,
            "entry": entry_price,
            "stop": stop_price,
            "target": target_price,
            "parent_order_id": parent.orderId,
            "tp_order_id": take_profit.orderId,
            "sl_order_id": stop_loss.orderId,
            "status": "SUBMITTED",
            "submitted_at": datetime.now(EASTERN).strftime("%H:%M:%S"),
            "filled_at": None,
            "exit_type": None,
            "exit_fill_price": None,
        }

        with self._lock:
            self._placed_alerts.add(alert_id)
            self._brackets[alert_id] = bracket_info
            self._orderid_to_alert[parent.orderId] = (alert_id, "parent")
            self._orderid_to_alert[take_profit.orderId] = (alert_id, "tp")
            self._orderid_to_alert[stop_loss.orderId] = (alert_id, "sl")

        log.info(
            f"[ORDER] Bracket complete: {parent_action} {shares} {symbol} "
            f"@ {entry_price} | Stop {stop_price} | Target {target_price} "
            f"| IDs: parent={parent.orderId} tp={take_profit.orderId} sl={stop_loss.orderId}")

        self._broadcast("order", {"type": "placed", **bracket_info})
        return True, str(parent.orderId)

    def cancel_bracket(self, alert_id: str) -> tuple:
        """Cancel a bracket order by alert_id. Cancels all live legs."""
        with self._lock:
            bracket = self._brackets.get(alert_id)
            if not bracket:
                return False, f"No bracket found for alert {alert_id}"

        if not self.ib.isConnected():
            return False, "IBKR not connected"

        target_ids = {
            bracket["parent_order_id"],
            bracket["tp_order_id"],
            bracket["sl_order_id"],
        }

        try:
            cancelled_count = self._run_on_loop(
                self._async_cancel_bracket(target_ids))
        except Exception as e:
            log.error(f"[ORDER] Cancel failed for {alert_id}: {e}")
            return False, f"Cancel failed: {e}"

        with self._lock:
            terminal = {"EXIT_TARGET", "EXIT_STOP", "CANCELLED"}
            if bracket["status"] not in terminal:
                bracket["status"] = "CANCELLED"
            self._placed_alerts.discard(alert_id)

        log.info(f"[ORDER] Bracket cancelled: {alert_id} ({cancelled_count} legs)")
        self._broadcast("order", {"type": "cancelled", "alert_id": alert_id})
        return True, f"Bracket {alert_id} cancelled ({cancelled_count} legs)"

    def get_open_orders(self) -> list:
        """Return all tracked brackets as a list of dicts."""
        with self._lock:
            return list(self._brackets.values())

    def is_ordered(self, alert_id: str) -> bool:
        """Check if an alert already has a bracket order."""
        with self._lock:
            return alert_id in self._placed_alerts

    # ── Internal callbacks ──

    def _on_order_status(self, trade: Trade):
        """Called by ib_insync when any order status changes.
        Tracks parent fill (entry), child fills (exits), and cancellations
        across all three legs of the bracket."""
        order_id = trade.order.orderId

        with self._lock:
            lookup = self._orderid_to_alert.get(order_id)
            if not lookup:
                return
            alert_id, role = lookup
            bracket = self._brackets.get(alert_id)
            if not bracket:
                return

        status = trade.orderStatus.status

        if status == "Filled":
            fill_price = trade.orderStatus.avgFillPrice
            now_str = datetime.now(EASTERN).strftime("%H:%M:%S")

            if role == "parent":
                with self._lock:
                    bracket["status"] = "ENTRY_FILLED"
                    bracket["filled_at"] = now_str
                    bracket["entry_fill_price"] = fill_price

                log.info(
                    f"[ORDER] ENTRY FILLED: {bracket['symbol']} {bracket['direction']} "
                    f"{bracket['shares']}sh @ {fill_price}")
                self._broadcast("order", {
                    "type": "entry_filled",
                    "alert_id": alert_id,
                    "fill_price": fill_price,
                })

            elif role == "tp":
                with self._lock:
                    bracket["status"] = "EXIT_TARGET"
                    bracket["exit_type"] = "TARGET"
                    bracket["exit_fill_price"] = fill_price
                    bracket["exit_at"] = now_str
                    self._placed_alerts.discard(alert_id)

                log.info(
                    f"[ORDER] TARGET HIT: {bracket['symbol']} {bracket['direction']} "
                    f"exit @ {fill_price}")
                self._broadcast("order", {
                    "type": "exit_filled",
                    "alert_id": alert_id,
                    "exit_type": "TARGET",
                    "fill_price": fill_price,
                })

            elif role == "sl":
                with self._lock:
                    bracket["status"] = "EXIT_STOP"
                    bracket["exit_type"] = "STOP"
                    bracket["exit_fill_price"] = fill_price
                    bracket["exit_at"] = now_str
                    self._placed_alerts.discard(alert_id)

                log.info(
                    f"[ORDER] STOPPED OUT: {bracket['symbol']} {bracket['direction']} "
                    f"exit @ {fill_price}")
                self._broadcast("order", {
                    "type": "exit_filled",
                    "alert_id": alert_id,
                    "exit_type": "STOP",
                    "fill_price": fill_price,
                })

        elif status == "Cancelled":
            if role == "parent":
                with self._lock:
                    bracket["status"] = "CANCELLED"
                    self._placed_alerts.discard(alert_id)

                log.info(f"[ORDER] CANCELLED: {bracket['symbol']} {alert_id}")
                self._broadcast("order", {"type": "cancelled", "alert_id": alert_id})
            else:
                log.info(f"[ORDER] Child {role} cancelled for {alert_id} (orderId={order_id})")

        elif status in ("PreSubmitted", "Submitted"):
            if role == "parent":
                with self._lock:
                    bracket["status"] = status.upper()
                self._broadcast("order", {
                    "type": "status", "alert_id": alert_id, "status": status})
