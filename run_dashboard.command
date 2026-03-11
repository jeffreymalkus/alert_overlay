#!/bin/bash
# ============================================
# Alert Overlay — Live Dashboard (Auto-Watchlist)
# Opens a browser dashboard automatically
# ============================================

cd "/Users/jeffreymalkus/Projects" || { echo "ERROR: Cannot find folder"; read -p "Press Enter to close..."; exit 1; }

echo "============================================"
echo "  Portfolio C — Live Dashboard"
echo "  Long (VK+SC) + Short (BDR RED+TREND AM)"
echo "============================================"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 not found."
    read -p "Press Enter to close..."
    exit 1
fi

# Install ib_insync if needed
python3 -c "import ib_insync" 2>/dev/null || pip3 install ib_insync

# Create default watchlist if none exists
WATCHLIST="alert_overlay/watchlist.txt"
if [ ! -f "$WATCHLIST" ]; then
    echo "Creating default watchlist (35 symbols)..."
    cat > "$WATCHLIST" << 'SYMBOLS'
AAPL
AMD
AMZN
BA
COIN
FAS
FNGU
GOOG
JNUG
LABU
MARA
META
MSFT
MSTR
NFLX
NIO
NUGT
NVDA
NVDL
PLTR
QQQ
RIVN
SOFI
SOXL
SPXL
SPXS
SPY
SQQQ
TECL
TNA
TQQQ
TSLA
TSLL
UPRO
WEBL
SYMBOLS
    echo "Saved to $WATCHLIST"
fi

SYMBOL_COUNT=$(grep -c '[A-Z]' "$WATCHLIST" 2>/dev/null || echo "0")
echo "Watchlist: $SYMBOL_COUNT symbols (edit $WATCHLIST or use dashboard UI to change)"
echo "Dashboard will open at: http://localhost:8877"
echo ""
echo "Make sure TWS is open and logged into Paper Trading."
echo "Press Ctrl+C to stop."
echo ""

python3 -m alert_overlay.dashboard --port 7497 --client-id 10

echo ""
read -p "Press Enter to close..."
