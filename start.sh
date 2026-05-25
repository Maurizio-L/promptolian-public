#!/bin/bash
echo "MASTER_KEY set: ${PROMPTOLIAN_MASTER_KEY:+yes}"
echo "DATABASE_URL set: ${DATABASE_URL:+yes}"

echo "Starting proxy on port 3002..."
python -m promptolian.proxy --host 0.0.0.0 --port 3002 &
PROXY_PID=$!
echo "Proxy PID: $PROXY_PID"

echo "Starting API on port 3001..."
python api/api.py