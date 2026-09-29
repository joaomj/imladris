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

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [sync-all] $*"
}

log "stage x-sync start"
if "$UV_BIN" run --project "$PROJECT_DIR" --extra brave x-digest sync; then
    X_STATUS=0
else
    X_STATUS=$?
fi
log "stage x-sync end exit=$X_STATUS"

log "stage brave-sync start"
if "$UV_BIN" run --project "$PROJECT_DIR" --extra brave x-digest brave-sync; then
    BRAVE_STATUS=0
else
    BRAVE_STATUS=$?
fi
log "stage brave-sync end exit=$BRAVE_STATUS"
log "complete x_exit=$X_STATUS brave_exit=$BRAVE_STATUS"
if [[ "$X_STATUS" -ne 0 || "$BRAVE_STATUS" -ne 0 ]]; then
    exit 1
fi
