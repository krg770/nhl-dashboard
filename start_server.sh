#!/bin/bash
# Starts the NHL dashboard's uvicorn server in the background, if it isn't
# already running. Triggered at Windows logon by a Startup-folder launcher
# (and by run_pipeline.sh), so the server survives a laptop restart without
# manual intervention.

set -e
cd "$(dirname "$0")"

# run_pipeline.sh holds its flock on fd 200 and starts this script. Without this, the
# long-lived server inherits that fd and keeps the pipeline lock forever (every later
# scheduled run then logs "Skipped (already running)" and does nothing).
exec 200>&-

PORT=8002
LOG_FILE=/tmp/nhl-dashboard-start.log

if curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT" 2>/dev/null | grep -q '^200$'; then
    echo "$(date): already running on port $PORT, skipping" >> "$LOG_FILE"
    exit 0
fi

source venv/bin/activate
nohup uvicorn main:app --port "$PORT" > /tmp/nhl-dashboard.log 2>&1 &
disown
echo "$(date): started uvicorn on port $PORT (pid $!)" >> "$LOG_FILE"
