#!/bin/bash

# ============================================================
# Market Agent V4.5 — Start the dashboard on this laptop
#
#   bash ~/market-agent/local/start.sh
#
# 1. Re-syncs code from the work folder (recorded by setup_mac.sh)
# 2. Starts trigger.py (API, 127.0.0.1:8080) and local_server.py
#    (dashboard, http://localhost:8000)
# 3. Opens the dashboard in your browser
#
# Runs happen only when you press Run in the dashboard — no schedule.
# Ctrl+C stops everything, INCLUDING a run in progress, so wait for
# a run to finish before stopping.
# ============================================================

BASE_DIR="$HOME/market-agent"
PY="$BASE_DIR/venv/bin/python"

cd "$BASE_DIR" || { echo "✗ Cannot find $BASE_DIR — run local/setup_mac.sh first"; exit 1; }
[ -x "$PY" ] || { echo "✗ No venv — run local/setup_mac.sh first"; exit 1; }

for port in 8080 8000; do
    if lsof -nP -iTCP:$port -sTCP:LISTEN >/dev/null 2>&1; then
        echo "✗ Port $port is already in use — is the dashboard already running?"
        lsof -nP -iTCP:$port -sTCP:LISTEN | tail -n +2
        exit 1
    fi
done

if [ -f "$BASE_DIR/.source_dir" ] && [ -d "$(cat "$BASE_DIR/.source_dir")" ]; then
    SRC="$(cat "$BASE_DIR/.source_dir")"
    bash "$SRC/local/sync_code.sh" "$SRC" "$BASE_DIR" && echo "✓ Code synced from $SRC"
fi

grep -qE '^ANTHROPIC_API_KEY=.+' .env 2>/dev/null || echo "⚠ ANTHROPIC_API_KEY is empty in .env — Run Agent will fail at the Claude step"

"$PY" trigger.py > logs/trigger.log 2>&1 &
TRIGGER_PID=$!
"$PY" local/local_server.py > logs/local_server.log 2>&1 &
SERVER_PID=$!

stop() {
    echo ""
    echo "Stopping dashboard..."
    kill $TRIGGER_PID $SERVER_PID 2>/dev/null
    wait 2>/dev/null
    echo "✓ Stopped"
    exit 0
}
trap stop INT TERM

sleep 1
if ! kill -0 $TRIGGER_PID 2>/dev/null || ! kill -0 $SERVER_PID 2>/dev/null; then
    echo "✗ Failed to start — see logs/trigger.log and logs/local_server.log"
    kill $TRIGGER_PID $SERVER_PID 2>/dev/null
    exit 1
fi

echo ""
echo "======================================================="
echo "  ✅ Market Agent dashboard: http://localhost:8000"
echo "     Token, if asked:  pbcopy < ~/market-agent/.trigger_token"
echo "     Run output:       tail -f ~/market-agent/logs/trigger.log"
echo "     Ctrl+C to stop"
echo "======================================================="
open "http://localhost:8000"
wait
