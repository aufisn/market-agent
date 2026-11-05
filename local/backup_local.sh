#!/bin/bash
# ============================================================
# Market Agent — back up the laptop's data (no GitHub involved)
#   bash local/backup_local.sh [folder]
# Saves watchlist.csv, logs/ and data/ from ~/market-agent as a plain,
# dated market-agent-backup-YYYY-MM-DD.tar.gz in the folder (default
# ~/Documents/Market Agent Backups). Same format as the GitHub backup, so
# either restores with local/restore_local.sh or ci/state_store.py restore.
# The file is NOT encrypted: keep it somewhere private.
# ============================================================
set -e
BASE_DIR="${MA_BASE_DIR:-$HOME/market-agent}"
DEST="${1:-$HOME/Documents/Market Agent Backups}"

[ -f "$BASE_DIR/watchlist.csv" ] || { echo "✗ No watchlist.csv in $BASE_DIR — nothing to back up"; exit 1; }
mkdir -p "$DEST"
FILE="$DEST/market-agent-backup-$(date +%Y-%m-%d)${MA_BACKUP_SUFFIX:-}.tar.gz"
[ -e "$FILE" ] && FILE="$DEST/market-agent-backup-$(date +%Y-%m-%d_%H%M%S)${MA_BACKUP_SUFFIX:-}.tar.gz"

cd "$BASE_DIR"
PARTS="watchlist.csv"
[ -d logs ] && PARTS="$PARTS logs"
[ -d data ] && PARTS="$PARTS data"
# Same exclusions as the GitHub state: lock files, *.log, caches
# COPYFILE_DISABLE: no hidden macOS "._" metadata files in the archive
COPYFILE_DISABLE=1 tar -czf "$FILE" --no-xattrs --exclude "._*" --exclude "run.lock" --exclude "*.log" --exclude "*.tmp" \
    --exclude "__pycache__" --exclude ".DS_Store" $PARTS
chmod 600 "$FILE"
COUNT=$(tar -tzf "$FILE" | grep -vc '/$')
echo "✓ Backup saved: $FILE ($COUNT files, plain — keep it private)"
