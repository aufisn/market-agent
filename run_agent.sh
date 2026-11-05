#!/bin/bash

# ============================================================
# Market Agent V4.5 — Full Pipeline Runner
# ============================================================

BASE_DIR="${MA_BASE_DIR:-$HOME/market-agent}"   # GitHub Actions sets MA_BASE_DIR to the checkout
VENV="$BASE_DIR/venv/bin/activate"

# Write Python output to the log as it happens, not in buffered chunks —
# otherwise the log looks frozen for minutes during step 7.
export PYTHONUNBUFFERED=1

echo ""
echo "======================================================="
echo "  MARKET AGENT V4.5 — Starting"
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
echo "agent" > "$LOCK_DIR/what"
trap 'rm -rf "$LOCK_DIR"' EXIT
export RUN_ID="agent_$(date -u +%Y%m%dT%H%M%S)"

STATUS_FILE="$BASE_DIR/logs/agent_run_status.json"
STARTED_AT=$(python3 -c "from datetime import datetime,timezone; print(datetime.now(timezone.utc).isoformat())")
TOTAL_STEPS=10
progress() { python3 "$BASE_DIR/write_progress.py" "$STATUS_FILE" "$1" "$TOTAL_STEPS" "$2" "$STARTED_AT"; }

progress 1 "Fetching live market data"

# ── FAILURE ALERT ─────────────────────────────────────────────
send_failure_alert() {
    local step="$1"
    local reason="$2"
    if [ -f "$BASE_DIR/.env" ]; then
        export $(grep -v '^#' "$BASE_DIR/.env" | xargs) 2>/dev/null
    fi
    local token="${TELEGRAM_TOKEN}"
    local chat="${TELEGRAM_CHAT_ID}"
    local msg="🚨 Market Agent FAILED%0AStep: ${step}%0AReason: ${reason}%0ATime: $(date '+%H:%M') UTC%0A%0ANo analysis saved. Previous data unchanged."
    if [ -n "$token" ] && [ -n "$chat" ]; then
        curl -s "https://api.telegram.org/bot${token}/sendMessage?chat_id=${chat}&text=${msg}" > /dev/null
        echo "✓ Failure alert sent to Telegram"
    fi
    python3 "$BASE_DIR/write_progress.py" "$STATUS_FILE" 0 "$TOTAL_STEPS" "Failed: ${step}" "$STARTED_AT" failed
}

# Step 1 — Fetch live market data (includes SPY + sector ETFs)
echo "📡 Step 1: Fetching live market data + context prices..."
python3 fetch_data.py
FETCH_EXIT=$?
if [ $FETCH_EXIT -eq 2 ]; then
    echo "✗ Market data failed validation (missing/zero prices) — stopping, previous analysis kept"
    send_failure_alert "Step 1 — Data Fetch" "market data incomplete (see logs) — previous analysis kept"
    exit 1
elif [ $FETCH_EXIT -ne 0 ]; then
    echo "✗ fetch_data.py failed — stopping"
    send_failure_alert "Step 1 — Data Fetch" "fetch_data.py exited with error"
    exit 1
fi

# Step 1b — Drawdown monitor (runs immediately after fetch)
progress 2 "Checking drawdown protection"
echo ""
echo "📉 Step 1b: Checking drawdown protection..."
python3 drawdown_monitor.py

# Step 1c — Stop tracker: intelligent position monitor
progress 3 "Running position monitor"
echo ""
echo "🛡️  Step 1c: Running position monitor (stop_tracker)..."
python3 stop_tracker.py

# Step 1d — Regime detector: classify market conditions
progress 4 "Detecting market regime"
echo ""
echo "🌡️  Step 1d: Detecting market regime..."
python3 regime_detector.py

# Step 1e — Earnings brief: pre-earnings alerts for holdings
progress 5 "Checking earnings calendar"
echo ""
echo "📅 Step 1e: Checking earnings calendar..."
python3 earnings_brief.py

# Step 2a — Fetch market context
progress 6 "Fetching market context"
echo ""
echo "🌍 Step 2a: Fetching market context..."
python3 market_context.py

# Step 2b — Run Claude analysis (stop status injected into prompt)
progress 7 "Analysis — starting"
echo ""
echo "🧠 Step 2b: Running Claude analysis..."
# agent_brain.py reports its own sub-stages ("Analysis 2/8 · Screening
# candidates 120/301") into the same status file via write_progress.substep
PROGRESS_FILE="$STATUS_FILE" PROGRESS_STEP=7 PROGRESS_TOTAL="$TOTAL_STEPS" \
PROGRESS_STARTED="$STARTED_AT" PROGRESS_LABEL="Analysis" \
    python3 agent_brain.py
BRAIN_EXIT=$?
if [ $BRAIN_EXIT -ne 0 ]; then
    echo "✗ agent_brain.py failed (exit $BRAIN_EXIT) — stopping pipeline"
    send_failure_alert "Step 2b — Claude Analysis" "agent_brain.py failed — check API credits or quota"
    exit 1
fi

# Step 2c — Portfolio Analysis
progress 8 "Running portfolio analysis"
echo ""
echo "📐 Step 2c: Running portfolio analysis..."
python3 portfolio_analysis.py
PORTFOLIO_EXIT=$?
if [ $PORTFOLIO_EXIT -ne 0 ]; then
    echo "⚠ portfolio_analysis.py failed (exit $PORTFOLIO_EXIT) — Analytics tab will be stale/empty until this is fixed"
    send_failure_alert "Step 2c — Portfolio Analysis" "portfolio_analysis.py failed (exit $PORTFOLIO_EXIT) — Analytics tab affected, rest of pipeline continuing"
    # NOT exiting — Analytics is a supplementary tab, not core to the main
    # analysis/alerts. The rest of the pipeline (Telegram alerts, performance
    # snapshot) should still run even if this one step fails.
fi

# Step 2d — Record performance snapshot
progress 9 "Recording performance snapshot"
echo ""
echo "📊 Step 2d: Recording performance snapshot..."
python3 -c "
import sys
sys.path.insert(0, '.')
from monthly_report import record_snapshot
record_snapshot()
"

# Step 3 — Send Telegram alerts
progress 10 "Sending Telegram alerts"
echo ""
echo "📱 Step 3: Sending Telegram alerts..."
HOUR=$((10#$(date -u +%H)))
if [ "$HOUR" == "14" ]; then
    python3 send_alerts.py full
else
    python3 send_alerts.py alerts
fi

# Step 3b — Weekly Sunday summary (runs every Sunday morning)
DAY_OF_WEEK=$(date +%u)   # 1=Mon ... 7=Sun
if [ "$DAY_OF_WEEK" == "7" ]; then
    echo ""
    echo "📋 Step 3b: Sending weekly Sunday summary..."
    python3 weekly_report.py
fi

# NOTE: Monthly PDF report is NOT triggered here. scheduler.sh already fires
# monthly_report.py exactly once, at 04:00 UTC on the 1st of each month.
# This step used to ALSO fire it on every run_agent.sh invocation on the 1st
# (3x/day from scheduled runs, plus any manual triggers) — causing duplicate
# monthly reports. Removed to leave scheduler.sh as the single source of truth.

progress 10 "Complete"
python3 write_progress.py "$STATUS_FILE" 10 "$TOTAL_STEPS" "Complete" "$STARTED_AT" done

echo ""
echo "🧪 Step 4: Updating recommendation simulator..."
python3 "$BASE_DIR/simulator.py"

echo ""
echo "======================================================="
echo "  ✅ Agent run complete — $(date '+%H:%M:%S')"
echo "======================================================="
