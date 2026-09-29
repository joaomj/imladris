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
            if launchctl start com.x-digest.sync >> "$PROJECT_DIR/data/logs/weekly-trigger.log" 2>&1; then
                # Allow launchd a moment to transition to running after
                # `start` before inspecting, so a fast check does not
                # mistake "not yet started" for "already finished".
                sleep 10
                SYNC_RESULT="timeout"
                waited=0
                while [ "$waited" -lt 1800 ]; do
                    if print_out="$(launchctl print "gui/$(id -u)/com.x-digest.sync" 2>&1)"; then
                        case "$print_out" in
                            *"state = running"*)
                                sleep 30
                                waited=$((waited + 30))
                                ;;
                            *)
                                SYNC_RESULT="done"
                                break
                                ;;
                        esac
                    else
                        log "sync status check failed; skipping backup"
                        SYNC_RESULT="status-failed"
                        break
                    fi
                done
                if [ "$SYNC_RESULT" = "done" ]; then
                    if [ -f "$BACKUP_PLIST" ]; then
                        launchctl start com.x-digest.backup >> "$PROJECT_DIR/data/logs/weekly-trigger.log" 2>&1 || log "backup start failed"
                    fi
                elif [ "$SYNC_RESULT" = "timeout" ]; then
                    log "sync did not finish within 1800s; skipping backup"
                fi
            else
                log "sync start failed; skipping backup"
            fi
        else
            log "sync agent not installed; skipping sync"
            if [ -f "$BACKUP_PLIST" ]; then
                launchctl start com.x-digest.backup >> "$PROJECT_DIR/data/logs/weekly-trigger.log" 2>&1 || log "backup start failed"
            fi
        fi
    else
        log "launchctl not available"
    fi
    log "dispatched week $(current_week)"
) >/dev/null 2>&1 &
