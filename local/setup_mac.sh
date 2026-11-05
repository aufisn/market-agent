#!/bin/bash

# ============================================================
# Market Agent V4.5 — Laptop (macOS) setup, manual-run mode
# Run from the work folder:
#
#   bash local/setup_mac.sh
#
# Installs into ~/market-agent (where the pipeline scripts expect
# to live). Safe to re-run: never overwrites .env, .trigger_token,
# watchlist.csv, logs/ or data/.
#
# Installs NO schedule — scheduler.sh is kept for later but nothing
# starts it. Runs happen only from the dashboard's Run buttons
# (or by hand). Start the dashboard with: bash ~/market-agent/local/start.sh
# ============================================================

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$HOME/market-agent"

GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; BOLD="\033[1m"; RESET="\033[0m"
ok()   { echo -e "${GREEN}✓ $1${RESET}"; }
warn() { echo -e "${YELLOW}⚠ $1${RESET}"; }
die()  { echo -e "${RED}✗ $1${RESET}"; exit 1; }

[ -f "$SRC_DIR/agent_brain.py" ] || die "Can't find the code next to local/ (looked in $SRC_DIR)"
command -v python3 >/dev/null || die "python3 not found — install it with: brew install python"

echo ""
echo -e "${BOLD}=======================================================${RESET}"
echo -e "${BOLD}  MARKET AGENT V4.5 — Laptop Setup (manual runs)${RESET}"
echo -e "${BOLD}  Source: $SRC_DIR${RESET}"
echo -e "${BOLD}  Target: $APP_DIR${RESET}"
echo -e "${BOLD}=======================================================${RESET}"
echo ""

# ── Code ─────────────────────────────────────────────────────
bash "$SRC_DIR/local/sync_code.sh" "$SRC_DIR" "$APP_DIR"
mkdir -p "$APP_DIR/logs" "$APP_DIR/data"
echo "$SRC_DIR" > "$APP_DIR/.source_dir"   # start.sh re-syncs code from here
ok "Code copied to $APP_DIR"

# ── Python venv ──────────────────────────────────────────────
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
ok "Python venv ready ($("$APP_DIR/venv/bin/python" --version))"

# ── Secrets + seed files ─────────────────────────────────────
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/local/env.example" "$APP_DIR/.env"
    warn ".env created from template — fill in your keys: open -e $APP_DIR/.env"
else
    ok ".env already present (left untouched)"
fi
chmod 600 "$APP_DIR/.env"

if [ ! -s "$APP_DIR/.trigger_token" ]; then
    openssl rand -hex 32 > "$APP_DIR/.trigger_token"
    ok "Generated new .trigger_token"
else
    ok ".trigger_token already present (left untouched)"
fi
chmod 600 "$APP_DIR/.trigger_token"

if [ ! -f "$APP_DIR/watchlist.csv" ]; then
    cp "$APP_DIR/local/watchlist.example.csv" "$APP_DIR/watchlist.csv"
    warn "watchlist.csv seeded with EXAMPLE rows — replace with your real holdings"
else
    ok "watchlist.csv already present (left untouched)"
fi

echo ""
echo -e "${GREEN}${BOLD}  ✅ Laptop setup complete${RESET}"
echo ""
echo "  Start the dashboard:   bash ~/market-agent/local/start.sh"
echo "  Access token (paste when the dashboard asks):"
echo "                         pbcopy < ~/market-agent/.trigger_token"
echo ""
