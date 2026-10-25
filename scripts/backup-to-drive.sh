#!/usr/bin/env bash
# Back up the local data/ directory to Google Cloud Storage with rclone.
# Copy semantics: files are never deleted on the bucket.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Load .env if present so launchd gets XDIGEST_BACKUP_BUCKET without manual export.
if [[ -f "$PROJECT_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_DIR/.env"
    set +a
fi
REMOTE="${XDIGEST_BACKUP_REMOTE:-gcs}"
BUCKET="${XDIGEST_BACKUP_BUCKET:-}"
if [[ -z "$BUCKET" ]]; then
    echo "XDIGEST_BACKUP_BUCKET is not set. Set it in .env or environment to your GCS bucket name." >&2
    exit 1
fi
DEST="${REMOTE}:${BUCKET}"
LOG_DIR="$PROJECT_DIR/data/logs"
LOG_FILE="$LOG_DIR/backup.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

# One Sunday attempt wins; later attempts exit before reading Proton Pass.
STATE_DIR="$PROJECT_DIR/data/state"
SUCCESS_STAMP="$STATE_DIR/backup.last-success"

mkdir -p "$LOG_DIR" "$STATE_DIR"
log "backup start"
# The agent fires several times on Sunday. One successful copy is enough.
if [[ -f "$SUCCESS_STAMP" && "$(cut -c1-10 "$SUCCESS_STAMP")" == "$(date '+%Y-%m-%d')" ]]; then
    log "backup already succeeded today; skipping"
    exit 0
fi

RCLONE_BIN="$(command -v rclone || true)"
if [[ -z "$RCLONE_BIN" ]]; then
    RCLONE_BIN="$HOME/homebrew/bin/rclone"
fi
if [[ ! -x "$RCLONE_BIN" ]]; then
    echo "rclone was not found" >&2
    exit 1
fi

# The GCS service-account JSON lives in Proton Pass. pass-cli needs the Proton
# Pass environment the shell uses; the LaunchAgent sets PROTON_PASS_KEY_PROVIDER.
GCS_CREDENTIAL_REFERENCE="pass://your-vault/x-digest-gcs-backup/service-account-json"
if [[ -z "${RCLONE_GCS_SERVICE_ACCOUNT_CREDENTIALS:-}" ]]; then
    if ! PASS_CLI="$(command -v pass-cli)"; then
        echo "pass-cli was not found; it is required to read the backup credentials" >&2
        exit 1
    fi
    if ! CREDENTIALS="$("$PASS_CLI" item view "$GCS_CREDENTIAL_REFERENCE")" || [[ -z "$CREDENTIALS" ]]; then
        echo "pass-cli could not read $GCS_CREDENTIAL_REFERENCE" >&2
        exit 1
    fi
    export RCLONE_GCS_SERVICE_ACCOUNT_CREDENTIALS="$CREDENTIALS"
fi

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

# The calendar backup must also wait for both collection stages to finish.
if [[ -f "$HOME/Library/LaunchAgents/com.x-digest.sync.plist" ]]; then
    waited=0
    while true; do
        if ! sync_state="$(launchctl print "gui/$(id -u)/com.x-digest.sync" 2>&1)"; then
            log "cannot inspect collection job; backup postponed"
            echo "Cannot inspect com.x-digest.sync; backup postponed." >&2
            exit 1
        fi
        if ! printf '%s\n' "$sync_state" | grep -Eq 'state = running|^[[:space:]]*pid = [0-9]+'; then
            break
        fi
        if [[ "$waited" -ge 1800 ]]; then
            log "collection still running after 30 minutes; backup postponed"
            echo "Collection still running; backup postponed." >&2
            exit 1
        fi
        sleep 30
        waited=$((waited + 30))
    done
fi
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
printf '%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" > "$SUCCESS_STAMP.tmp"
mv "$SUCCESS_STAMP.tmp" "$SUCCESS_STAMP"
