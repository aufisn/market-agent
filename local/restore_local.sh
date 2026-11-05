#!/bin/bash
# ============================================================
# Market Agent — restore a backup into the laptop install (no GitHub)
#   bash local/restore_local.sh <market-agent-backup-*.tar.gz>
# The safe-fail option: works from a GitHub backup or a laptop backup,
# with no GitHub account, token or connection. Steps:
#   1. checks the file only holds watchlist.csv, logs/ and data/
#   2. saves the laptop's current data as a "-before-restore" backup
#      next to the file
#   3. replaces watchlist.csv, logs/ and data/ in ~/market-agent with it
# Then start Market Agent as usual: bash ~/market-agent/local/start.sh
# ============================================================
set -e
BASE_DIR="${MA_BASE_DIR:-$HOME/market-agent}"
FILE="$1"
HERE="$(cd "$(dirname "$0")" && pwd)"

[ -n "$FILE" ] && [ -f "$FILE" ] || { echo "usage: restore_local.sh <market-agent-backup-*.tar.gz>"; exit 1; }
FILE="$(cd "$(dirname "$FILE")" && pwd)/$(basename "$FILE")"

# 1. Only the three known parts, no absolute paths or "..", and a watchlist
LIST=$(tar -tzf "$FILE" 2>/dev/null) || { echo "✗ Not a readable .tar.gz backup"; exit 1; }
BAD=$(printf '%s\n' "$LIST" | sed 's#^\./##' | grep -vE '(^|/)\._' | grep -vE '^(watchlist\.csv|logs(/.*)?|data(/.*)?)$' | grep -v '^$' || true)
TRAVERSE=$(printf '%s\n' "$LIST" | grep -E '(^/|(^|/)\.\.(/|$))' || true)
if [ -n "$BAD" ] || [ -n "$TRAVERSE" ]; then
    echo "✗ Not a Market Agent backup — unexpected entries:"; printf '%s\n' "$BAD" "$TRAVERSE" | grep -v '^$' | head -5
    exit 1
fi
printf '%s\n' "$LIST" | sed 's#^\./##' | grep -qx 'watchlist.csv' || { echo "✗ Not a Market Agent backup: no watchlist.csv inside"; exit 1; }
COUNT=$(printf '%s\n' "$LIST" | grep -vc '/$')

# A run in progress would keep writing over the restored files
if [ -d "$BASE_DIR/logs/run.lock" ] && kill -0 "$(cat "$BASE_DIR/logs/run.lock/pid" 2>/dev/null)" 2>/dev/null; then
    echo "✗ A Market Agent run is in progress — wait for it to finish"; exit 1
fi

# 2. Keep what's there now
if [ -f "$BASE_DIR/watchlist.csv" ]; then
    MA_BASE_DIR="$BASE_DIR" MA_BACKUP_SUFFIX="-before-restore" bash "$HERE/backup_local.sh" "$(dirname "$FILE")"
fi

read -r -p "Replace the laptop's data with $(basename "$FILE") ($COUNT files)? Type RESTORE: " ANSWER
[ "$ANSWER" = "RESTORE" ] || { echo "✗ Not restored"; exit 1; }

# 3. Swap the data in
mkdir -p "$BASE_DIR"
rm -rf "$BASE_DIR/logs" "$BASE_DIR/data" "$BASE_DIR/watchlist.csv"
tar -xzf "$FILE" -C "$BASE_DIR" --exclude "._*"
mkdir -p "$BASE_DIR/logs" "$BASE_DIR/data"
echo "✓ Restored $COUNT files into $BASE_DIR"
echo "  Start Market Agent with: bash $BASE_DIR/local/start.sh"
