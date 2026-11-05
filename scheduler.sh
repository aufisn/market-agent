#!/bin/bash

# ============================================================
# MARKET AGENT V4.5 — Scheduler
# Agent run is pinned to 10:30 AM US Eastern (not a fixed UTC
# time) — this shows up as 6:30pm Oman during EDT (summer) and
# 7:30pm Oman during EST (winter). Using the server's real IANA
# timezone database (TZ='America/New_York') to read the actual
# NY clock means the EDT/EST transition is handled automatically
# by the OS every year — this part never needs a manual update.
# Other runs (Pre Market, Day Insight, extra agent runs) are
# manual-only, triggered from the dashboard Run tab.
# ============================================================

BASE_DIR="$HOME/market-agent"
LOG="$BASE_DIR/logs/scheduler.log"
VENV="$BASE_DIR/venv/bin/activate"

mkdir -p "$BASE_DIR/logs"
source "$VENV"

# ── US MARKET CALENDAR ───────────────────────────────────────
# Holidays come from market_calendar.py (NYSE rules, L7), the same source
# as premarket_scanner.py and the dashboard, so there's no list to update.
is_market_day() {
    local today=$(TZ='America/New_York' date +%Y-%m-%d)
    python3 "$BASE_DIR/market_calendar.py" --is-trading-day "$today"
}

echo "$(date): Scheduler V4.5 started" >> "$LOG"

while true; do
    HOUR=$((10#$(date -u +%H)))
    MIN=$((10#$(date -u +%M)))
    TIME=$(printf "%d:%02d" $HOUR $MIN)
    DAY=$(date +%u)   # 1=Mon 7=Sun
    DOM=$(date +%d)   # Day of month 01-31
    NY_TIME=$(TZ='America/New_York' date +%H:%M)
    NY_HOUR=$((10#$(TZ='America/New_York' date +%H)))
    NY_MIN=$((10#$(TZ='America/New_York' date +%M)))

    # ── MAIN AGENT RUN — checked FIRST to prevent overlap ────
    # Fires at 10:30 AM NY time — an hour after market open, so there's
    # actually something to react to, not just the opening print.
    # Only the 6:30pm Oman run is automatic now. The 9:00pm and
    # 12:30am runs were removed from the schedule per Salim's request —
    # still available anytime as a manual "Run Agent" from the dashboard.
    # Skipped entirely on weekends/market holidays — no point running a
    # full analysis when there's nothing new to react to.
    if [[ "$NY_TIME" == "10:30" ]]; then
        if is_market_day; then
            echo "$(date): Running full agent at 10:30 AM NY time ($NY_TIME)" >> "$LOG"
            cd "$BASE_DIR" && bash run_agent.sh >> "$LOG" 2>&1
        else
            echo "$(date): Skipping scheduled agent run — market closed today (weekend/holiday)" >> "$LOG"
        fi
        sleep 61
        continue
    fi

    # ── MONTHLY REPORT (1st of month, 04:00 UTC = 08:00 Oman) ─
    if [[ "$DOM" == "01" && "$TIME" == "4:00" ]]; then
        echo "$(date): Sending monthly performance report" >> "$LOG"
        cd "$BASE_DIR" && python3 monthly_report.py >> "$LOG" 2>&1
        sleep 61
        continue
    fi

    # ── WEEKLY REPORT (Sunday 04:00 UTC = 08:00 Oman) ────────
    # Skip on 1st of month — monthly report takes priority
    if [[ "$DAY" == "7" && "$TIME" == "4:00" && "$DOM" != "01" ]]; then
        echo "$(date): Sending weekly report" >> "$LOG"
        cd "$BASE_DIR" && python3 weekly_report.py >> "$LOG" 2>&1
        sleep 61
        continue
    fi

    # ── PRICE ALERTS (every 15min during US market hours) ────
    # Now checked against the real NY clock (9:30 AM - 4:00 PM ET) instead
    # of a fixed UTC window — the old 13:00-20:00 UTC window was only
    # accurate during EDT; during EST it drifted to 8:00 AM-3:00 PM ET,
    # cutting off the last hour of trading every winter. Also skipped on
    # weekends/holidays now, same reasoning as the agent run above.
    NY_MINUTES=$((NY_HOUR * 60 + NY_MIN))
    if [[ "$MIN" == "00" || "$MIN" == "15" || "$MIN" == "30" || "$MIN" == "45" ]]; then
        if [[ $NY_MINUTES -ge $((9*60+30)) && $NY_MINUTES -lt $((16*60)) ]] && is_market_day; then
            echo "$(date): Price alert check (NY time $NY_TIME)" >> "$LOG"
            cd "$BASE_DIR" && python3 price_alerts.py >> "$LOG" 2>&1
            sleep 61
            continue
        fi
    fi

    sleep 29
done
