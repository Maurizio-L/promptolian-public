#!/bin/bash
# Start proxy on port 3002
python -m promptolian.proxy --host 0.0.0.0 --port 3002 &
# Start API on port 3001
python api/api.py