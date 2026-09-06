#!/bin/sh
# Trigger the weekly x-digest sync plus backup from first shell use.
# Runs detached so shell startup stays fast. Skips when this ISO week
# already ran. Delay keeps boot and other first-shell agents responsive.
# Usage: weekly-shell-trigger.sh [delay_seconds]

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="$PROJECT_DIR/data/logs"
STAMP_FILE="$STATE_DIR/weekly-shell-trigger.stamp"
DELAY_SECONDS="${1:-1800}"
SYNC_PLIST="$HOME/Library/LaunchAgents/com.x-digest.sync.plist"
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
    if command -v launchctl >/dev/null 2>&1; then
        if [ -f "$SYNC_PLIST" ]; then
            launchctl start com.x-digest.sync >> "$PROJECT_DIR/data/logs/weekly-trigger.log" 2>&1 || log "sync start failed"
        fi
        # Wait for sync (up to 30 min) before backup so the archive is fresh.
        # The sync agent exits after dispatching, so poll for completion by
        # comparing the scheduler log instead of the process table.
        waited=0
        sync_marker_before="$(wc -c < "$PROJECT_DIR/data/logs/scheduler.out.log" 2>/dev/null || echo 0)"
        while [ "$waited" -lt 1800 ]; do
            sleep 60
            waited=$((waited + 60))
            sync_marker_after="$(wc -c < "$PROJECT_DIR/data/logs/scheduler.out.log" 2>/dev/null || echo 0)"
            if [ "$sync_marker_after" != "$sync_marker_before" ]; then
                break
            fi
            if pgrep -f "x-digest sync" >/dev/null 2>&1; then
                continue
            fi
            # No process and no new output after 3 min: assume sync finished fast.
            if [ "$waited" -ge 180 ]; then
                break
            fi
        done
        if [ -f "$BACKUP_PLIST" ]; then
            launchctl start com.x-digest.backup >> "$PROJECT_DIR/data/logs/weekly-trigger.log" 2>&1 || log "backup start failed"
        fi
    else
        log "launchctl not available"
    fi
    log "dispatched week $(current_week)"
) >/dev/null 2>&1 &
