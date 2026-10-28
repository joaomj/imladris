#!/bin/sh
# Trigger the weekly Brave collection plus backup from first shell use.
# Runs detached so shell startup stays fast. Skips when this ISO week
# already ran. Delay keeps boot and other first-shell agents responsive.
# Brave's LevelDB sits behind Full Disk Access, which the terminal app
# holds and a launchd job does not, so this stage runs here.
# Usage: weekly-shell-trigger.sh [delay_seconds]

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="$PROJECT_DIR/data/logs"
STAMP_FILE="$STATE_DIR/weekly-shell-trigger.stamp"
DELAY_SECONDS="${1:-1800}"
BACKUP_PLIST="$HOME/Library/LaunchAgents/com.x-digest.backup.plist"

current_week() {
    date +%G-W%V
}

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') weekly-trigger: $*" >> "$PROJECT_DIR/data/logs/weekly-trigger.log"
}

mkdir -p "$STATE_DIR" "$PROJECT_DIR/data/logs" 2>/dev/null || exit 0
if [ -f "$STAMP_FILE" ] && [ "$(cat "$STAMP_FILE" 2>/dev/null)" = "$(current_week)" ]; then
    exit 0
fi

(
    sleep "$DELAY_SECONDS"
    # Re-check after the delay so concurrent shells run only once.
    if [ -f "$STAMP_FILE" ] && [ "$(cat "$STAMP_FILE" 2>/dev/null)" = "$(current_week)" ]; then
        exit 0
    fi
    current_week > "$STAMP_FILE"
    log "start week $(current_week)"
    PYTHON="$PROJECT_DIR/.venv/bin/python"
    if [ ! -x "$PYTHON" ]; then
        log "interpreter not found: $PYTHON"
    else
        capture="$(mktemp)"
        ( cd "$PROJECT_DIR" && "$PYTHON" -m x_digest brave-sync ) > "$capture" 2>&1
        BRAVE_STATUS=$?
        while IFS= read -r line; do
            echo "$(date '+%Y-%m-%d %H:%M:%S') brave-sync: $line" >> "$PROJECT_DIR/data/logs/weekly-trigger.log"
        done < "$capture"
        rm -f "$capture"
        log "brave-sync end exit=$BRAVE_STATUS"
    fi
    if [ -f "$BACKUP_PLIST" ] && command -v launchctl >/dev/null 2>&1; then
        launchctl start com.x-digest.backup >> "$PROJECT_DIR/data/logs/weekly-trigger.log" 2>&1 \
            || log "backup start failed"
    fi
    log "dispatched week $(current_week)"
) >/dev/null 2>&1 &
