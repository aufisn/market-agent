#!/bin/bash

# ============================================================
# Market Agent V4.5 — Pre-Market Intelligence Engine
# premarket_run.sh
#
# Independent pipeline — runs separately from run_agent.sh
# Triggered via dashboard button or manually via SSH
#
# Steps:
#   Step 1  — Market holiday check (skip if no US market today)
#   Step 2  — Fetch pre-market prices for all tickers
#   Step 3  — Validate existing recommendations vs pre-market
#   Step 4  — Scan sector leaders for new opportunities
#   Step 5  — Claude API analysis of movers
#   Step 6  — Send pre-market Telegram brief
# ============================================================

BASE_DIR="${MA_BASE_DIR:-$HOME/market-agent}"   # GitHub Actions sets MA_BASE_DIR to the checkout
VENV="$BASE_DIR/venv/bin/activate"

# Write Python output to the log as it happens, not in buffered chunks
export PYTHONUNBUFFERED=1
LOG_FILE="$BASE_DIR/logs/premarket.log"

echo ""
echo "======================================================="
echo "  MARKET AGENT V4.5 — Pre-Market Intelligence Engine"
echo "  $(date '+%A %d %B %Y  %H:%M')"
echo "======================================================="
echo ""

cd "$BASE_DIR" || { echo "✗ Cannot find $BASE_DIR"; exit 1; }
if [ -f "$VENV" ]; then
    source "$VENV" || { echo "✗ Cannot activate venv"; exit 1; }
elif [ -z "$CI" ]; then
    echo "✗ Cannot find venv at $VENV"; exit 1
fi   # on GitHub Actions (CI set) packages are installed system-wide

# ── RUN LOCK + RUN ID ─────────────────────────────────────────
# One Claude-spending run at a time: a double-tap on Run, or two open
# tabs, used to start two full paid pipelines writing the same files.
# mkdir is atomic (macOS has no flock); a lock left by a dead process
# is cleared automatically.
LOCK_DIR="$BASE_DIR/logs/run.lock"
mkdir -p "$BASE_DIR/logs"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    OTHER_PID=$(cat "$LOCK_DIR/pid" 2>/dev/null)
    if [ -n "$OTHER_PID" ] && kill -0 "$OTHER_PID" 2>/dev/null; then
        echo "✗ Another run is already in progress (pid $OTHER_PID: $(cat "$LOCK_DIR/what" 2>/dev/null)) — not starting a second one"
        exit 3
    fi
    rm -rf "$LOCK_DIR" && mkdir "$LOCK_DIR"
fi
echo $$ > "$LOCK_DIR/pid"
echo "premarket" > "$LOCK_DIR/what"
trap 'rm -rf "$LOCK_DIR"' EXIT
export RUN_ID="premarket_$(date -u +%Y%m%dT%H%M%S)"

STATUS_FILE="$BASE_DIR/logs/premarket_status.json"
STARTED_AT=$(python3 -c "from datetime import datetime,timezone; print(datetime.now(timezone.utc).isoformat())")
TOTAL_STEPS=3
progress() { python3 "$BASE_DIR/write_progress.py" "$STATUS_FILE" "$1" "$TOTAL_STEPS" "$2" "$STARTED_AT"; }

progress 1 "Checking market calendar"

# ── FAILURE ALERT ─────────────────────────────────────────────
send_failure_alert() {
    local step="$1"
    local reason="$2"
    if [ -f "$BASE_DIR/.env" ]; then
        export $(grep -v '^#' "$BASE_DIR/.env" | xargs) 2>/dev/null
    fi
    local token="${TELEGRAM_TOKEN}"
    local chat="${TELEGRAM_CHAT_ID}"
    local msg="🚨 Pre-Market Engine FAILED%0AStep: ${step}%0AReason: ${reason}%0ATime: $(date '+%H:%M') UTC"
    if [ -n "$token" ] && [ -n "$chat" ]; then
        curl -s "https://api.telegram.org/bot${token}/sendMessage?chat_id=${chat}&text=${msg}" > /dev/null
        echo "✓ Failure alert sent to Telegram"
    fi
    python3 "$BASE_DIR/write_progress.py" "$STATUS_FILE" 0 "$TOTAL_STEPS" "Failed: ${step}" "$STARTED_AT" failed
}

# Step 1 — Market holiday / weekend check
echo "📅 Step 1: Checking market calendar..."
python3 premarket_scanner.py --check-only
CHECK_EXIT=$?
if [ $CHECK_EXIT -eq 2 ]; then
    echo "⏭  Market closed today — pre-market run skipped"
    python3 write_progress.py "$STATUS_FILE" "$TOTAL_STEPS" "$TOTAL_STEPS" "Market closed — skipped" "$STARTED_AT" done
    exit 0
fi

# Step 2-5 — Pre-market scan, validate, analyse, save brief
progress 2 "Scanning pre-market data and running analysis"
echo ""
echo "🌅 Step 2-5: Running pre-market scanner and analysis..."
python3 premarket_scanner.py
SCAN_EXIT=$?
if [ $SCAN_EXIT -ne 0 ]; then
    echo "✗ premarket_scanner.py failed — stopping"
    send_failure_alert "Pre-Market Scan" "premarket_scanner.py exited with error"
    exit 1
fi

# Step 6 — Send Telegram brief
progress 3 "Sending Telegram brief"
echo ""
echo "📱 Step 6: Sending pre-market Telegram brief..."
python3 premarket_brief.py
BRIEF_EXIT=$?
if [ $BRIEF_EXIT -ne 0 ]; then
    echo "✗ premarket_brief.py failed — brief not sent"
    send_failure_alert "Pre-Market Brief" "premarket_brief.py failed to send Telegram message"
    exit 1
fi

python3 write_progress.py "$STATUS_FILE" "$TOTAL_STEPS" "$TOTAL_STEPS" "Complete" "$STARTED_AT" done

echo ""
echo "======================================================="
echo "  ✅ Pre-market run complete — $(date '+%H:%M:%S')"
echo "======================================================="
