"""Quick smoke test: verify reqMktData streaming works for multiple symbols.

Usage:
    python -m alert_overlay.test_reqmktdata

Tests:
1. Connects to IBKR
2. Subscribes 5 symbols via reqMktData
3. Waits 15 seconds collecting ticks
4. Reports which symbols received data
5. Tests BarAggregator bar construction
"""

import time
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from ib_insync import IB, Stock

# Import our BarAggregator
from ..dashboard import BarAggregator

EASTERN = ZoneInfo("US/Eastern")

TEST_SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "TSLA"]
WAIT_SECONDS = 15


def main():
    ib = IB()
    print(f"Connecting to IBKR at 127.0.0.1:7497 (clientId=99)...")
    try:
        ib.connect("127.0.0.1", 7497, clientId=99)
    except Exception as e:
        print(f"FAILED to connect: {e}")
        print("Make sure TWS or IB Gateway is running.")
        sys.exit(1)
    print("Connected.\n")

    # Track ticks per symbol
    tick_counts = {sym: 0 for sym in TEST_SYMBOLS}
    last_prices = {sym: None for sym in TEST_SYMBOLS}
    aggregators = {sym: BarAggregator(5) for sym in TEST_SYMBOLS}
    bars_completed = {sym: 0 for sym in TEST_SYMBOLS}

    def make_handler(symbol):
        def handler(ticker):
            price = getattr(ticker, 'last', None) or getattr(ticker, 'close', None)
            if price is None or price <= 0:
                price = getattr(ticker, 'marketPrice', None)
                if price is None or price != price or price <= 0:
                    return
            tick_counts[symbol] += 1
            last_prices[symbol] = price

            # Test bar aggregation
            cum_vol = getattr(ticker, 'volume', 0) or 0
            ts = datetime.now(EASTERN)
            completed = aggregators[symbol].on_tick(price, cum_vol, ts)
            if completed is not None:
                bars_completed[symbol] += 1
                print(f"  [{symbol}] BAR COMPLETED: {completed.timestamp.strftime('%H:%M')} "
                      f"O={completed.open:.2f} H={completed.high:.2f} "
                      f"L={completed.low:.2f} C={completed.close:.2f} V={completed.volume:.0f}")
        return handler

    # Subscribe all symbols
    tickers = {}
    for sym in TEST_SYMBOLS:
        contract = Stock(sym, "SMART", "USD")
        ib.qualifyContracts(contract)
        ticker = ib.reqMktData(contract, genericTickList="", snapshot=False, regulatorySnapshot=False)
        ticker.updateEvent += make_handler(sym)
        tickers[sym] = ticker
        print(f"[{sym}] reqMktData subscribed")

    print(f"\nWaiting {WAIT_SECONDS}s for ticks...\n")

    # Wait and let ticks accumulate
    start = time.time()
    while time.time() - start < WAIT_SECONDS:
        ib.sleep(1.0)
        elapsed = int(time.time() - start)
        total_ticks = sum(tick_counts.values())
        if elapsed % 5 == 0:
            print(f"  [{elapsed}s] Total ticks received: {total_ticks}")

    # Report results
    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    all_ok = True
    for sym in TEST_SYMBOLS:
        status = "OK" if tick_counts[sym] > 0 else "FAILED"
        if tick_counts[sym] == 0:
            all_ok = False
        price_str = f"${last_prices[sym]:.2f}" if last_prices[sym] else "N/A"
        forming = aggregators[sym].forming_bar
        forming_str = f"forming: O={forming['open']:.2f} H={forming['high']:.2f} L={forming['low']:.2f} C={forming['close']:.2f} ticks={forming['ticks']}" if forming else "no forming bar"
        print(f"  [{status}] {sym}: {tick_counts[sym]} ticks, last={price_str}, "
              f"bars_completed={bars_completed[sym]}, {forming_str}")

    print(f"\n{'ALL SYMBOLS RECEIVING DATA' if all_ok else 'SOME SYMBOLS FAILED'}")
    print(f"Total ticks: {sum(tick_counts.values())}")

    # Cleanup
    for sym in TEST_SYMBOLS:
        contract = Stock(sym, "SMART", "USD")
        ib.cancelMktData(contract)
    ib.disconnect()
    print("Disconnected.")

    if not all_ok:
        print("\nNOTE: If market is closed, some symbols may show 0 ticks.")
        print("Try again during market hours (9:30-16:00 ET) or extended hours.")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
