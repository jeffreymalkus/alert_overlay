#!/bin/bash
# ============================================
# Alert Overlay — IBKR Paper Trading Launcher
# Double-click this file to start
# ============================================

# Move to the project's parent directory
cd "/Users/jeffreymalkus/Projects" || { echo "ERROR: Cannot find /Users/jeffreymalkus/Projects"; read -p "Press Enter to close..."; exit 1; }

echo "============================================"
echo "  Alert Overlay v4.5 — IBKR Paper Trading"
echo "============================================"
echo ""

# Check Python is available
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 not found. Install Python from https://python.org"
    read -p "Press Enter to close..."
    exit 1
fi

# Install ib_insync if not already installed
python3 -c "import ib_insync" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "Installing ib_insync (first time only)..."
    pip3 install ib_insync
    echo ""
fi

echo "Connecting to IBKR Paper Trading (port 7497)..."
echo "Symbol: AAPL"
echo "Bar size: 5 min"
echo "Mode: historical (keepUpToDate)"
echo ""
echo "Make sure TWS is open and logged into Paper Trading."
echo "Press Ctrl+C to stop."
echo ""

# Run the alert overlay
python3 -m alert_overlay.ibkr_live --symbol AAPL --port 7497 --mode historical

echo ""
echo "============================================"
echo "Session ended."
read -p "Press Enter to close..."
