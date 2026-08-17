#!/usr/bin/env bash
# Back up the local data/ directory to Google Drive with rclone.
# Copy semantics: files are never deleted on Drive.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="xdigest"
DEST="${REMOTE}:x-digest-backup"
LOG_DIR="$PROJECT_DIR/data/logs"
LOG_FILE="$LOG_DIR/backup.log"

RCLONE_BIN="$(command -v rclone || true)"
if [[ -z "$RCLONE_BIN" ]]; then
    RCLONE_BIN="$HOME/homebrew/bin/rclone"
fi
if [[ ! -x "$RCLONE_BIN" ]]; then
    echo "rclone was not found" >&2
    exit 1
fi

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

mkdir -p "$LOG_DIR"
log "backup start"
sqlite3 "$PROJECT_DIR/data/silver.sqlite" ".backup '$STAGING/silver.sqlite'"
"$RCLONE_BIN" copy "$PROJECT_DIR/data/" "$DEST/" \
    --exclude "silver.sqlite*" \
    --fast-list \
    --log-file "$LOG_FILE"
"$RCLONE_BIN" copyto "$STAGING/silver.sqlite" "$DEST/silver.sqlite" \
    --log-file "$LOG_FILE"
"$RCLONE_BIN" check "$PROJECT_DIR/data/" "$DEST/" \
    --exclude "silver.sqlite*" \
    --one-way \
    --fast-list \
    --log-file "$LOG_FILE"
log "backup end"
