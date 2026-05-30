#!/bin/bash
python -m promptolian.proxy --host 0.0.0.0 --port 3002 --compress &
python api/api.py