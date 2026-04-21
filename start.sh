#!/bin/bash
set -e
cd "$(dirname "$0")"
source venv/bin/activate

echo "Starting API server on :8000..."
python3 dashboard/api_server.py &
API_PID=$!

echo "Starting orchestrator..."
cd gme_trading_system
python3 orchestrator.py &
ORCH_PID=$!

trap "echo 'Stopping...'; kill $API_PID $ORCH_PID 2>/dev/null" EXIT INT TERM
wait
