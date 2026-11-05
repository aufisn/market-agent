#!/bin/bash

# ============================================================
# Market Agent — copy code from the work folder into the install
# folder, never touching secrets or data there.
#   bash local/sync_code.sh <source_dir> <install_dir>
# ============================================================

SRC="$1"
DST="$2"
[ -d "$SRC" ] && [ -n "$DST" ] || { echo "usage: sync_code.sh <source_dir> <install_dir>"; exit 1; }

mkdir -p "$DST"
rsync -a \
    --exclude "Old files" --exclude ".DS_Store" --exclude ".git" --exclude ".gitignore" --exclude ".github" --exclude "venv" --exclude "__pycache__" \
    --exclude ".env" --exclude ".trigger_token" --exclude ".source_dir" --exclude "watchlist.csv" \
    --exclude "logs" --exclude "data" --exclude "old-schedule-backup" --exclude "review" --exclude "backups" \
    "$SRC/" "$DST/"
chmod +x "$DST"/*.sh "$DST"/local/*.sh
