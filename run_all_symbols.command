#!/bin/bash
# ============================================
# Alert Overlay — Launch ALL symbols at once
# AAPL, TSLA, TSLL, SPY, QQQ
# Double-click this file to start all 5
# ============================================

DIR="/Users/jeffreymalkus/Projects/alert_overlay"

echo "Launching all 5 symbols..."
echo ""

open "$DIR/run_ibkr_paper.command"
sleep 1
open "$DIR/run_tsla.command"
sleep 1
open "$DIR/run_tsll.command"
sleep 1
open "$DIR/run_spy.command"
sleep 1
open "$DIR/run_qqq.command"

echo "All 5 launched. Check your Terminal windows."
echo ""
echo "  AAPL  (client 1)"
echo "  TSLA  (client 2)"
echo "  TSLL  (client 3)"
echo "  SPY   (client 4)"
echo "  QQQ   (client 5)"
echo ""
echo "Close each Terminal tab/window to stop that symbol."
