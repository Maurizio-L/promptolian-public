#!/bin/bash
echo "Starting proxy on port 3002..."
python -m promptolian.proxy --host 0.0.0.0 --port 3002 &
PROXY_PID=$!
echo "Proxy PID: $PROXY_PID"

echo "Starting API on port 3001..."
python api/api.py
