#!/bin/bash
cd "/Users/jeffreymalkus/Projects" || { echo "ERROR: Cannot find folder"; read -p "Press Enter to close..."; exit 1; }
echo "Alert Overlay — TSLL Paper Trading"
echo "==================================="
python3 -c "import ib_insync" 2>/dev/null || pip3 install ib_insync
python3 -m alert_overlay.ibkr_live --symbol TSLL --port 7497 --client-id 3 --mode historical
read -p "Press Enter to close..."
