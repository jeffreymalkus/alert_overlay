#!/bin/bash
cd "/Users/jeffreymalkus/Projects" || { echo "ERROR: Cannot find folder"; read -p "Press Enter to close..."; exit 1; }
echo "Alert Overlay — QQQ Paper Trading"
echo "==================================="
python3 -c "import ib_insync" 2>/dev/null || pip3 install ib_insync
python3 -m alert_overlay.ibkr_live --symbol QQQ --port 7497 --client-id 5 --mode historical
read -p "Press Enter to close..."
