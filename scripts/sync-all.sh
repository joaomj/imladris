#!/usr/bin/env bash
# Run both sources sequentially; one source failure must not block the other.
set -uo pipefail

if [[ $# -ne 1 || "${1:-}" != /* ]]; then
    echo "usage: sync-all.sh /absolute/path/to/uv" >&2
    exit 2
fi
UV_BIN="$1"
if [[ ! -x "$UV_BIN" ]]; then
    echo "uv executable not found or not executable: $UV_BIN" >&2
    exit 1
fi
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 1
cd "$PROJECT_DIR" || exit 1

LOG_DIR="$PROJECT_DIR/data/logs"
RUN_LOG="$LOG_DIR/scheduler-$(date '+%Y-%m-%d').log"
mkdir -p "$LOG_DIR"

stamp_lines() {
    while IFS= read -r line; do
        printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$line"
    done
}

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "[sync-all] $*" | tee -a "$RUN_LOG"
}

# Every stage line reaches the dated run log with its own timestamp, so an old
# traceback cannot be mistaken for the current run.
run_stage() {
    "$@" 2>&1 | stamp_lines | tee -a "$RUN_LOG"
    return "${PIPESTATUS[0]}"
}

log "run start"
log "stage x-sync start"
if run_stage "$UV_BIN" run --project "$PROJECT_DIR" --extra brave x-digest sync; then
    X_STATUS=0
else
    X_STATUS=$?
fi
log "stage x-sync end exit=$X_STATUS"

log "stage brave-sync start"
if run_stage "$UV_BIN" run --project "$PROJECT_DIR" --extra brave x-digest brave-sync; then
    BRAVE_STATUS=0
else
    BRAVE_STATUS=$?
fi
log "stage brave-sync end exit=$BRAVE_STATUS"
log "complete x_exit=$X_STATUS brave_exit=$BRAVE_STATUS"
if [[ "$X_STATUS" -ne 0 || "$BRAVE_STATUS" -ne 0 ]]; then
    exit 1
fi
