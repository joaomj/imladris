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

RCLONE_BIN="$(command -v rclone || true)"
if [[ -z "$RCLONE_BIN" ]]; then
    RCLONE_BIN="$HOME/homebrew/bin/rclone"
fi
if [[ ! -x "$RCLONE_BIN" ]]; then
    echo "rclone was not found" >&2
    exit 1
fi

# Prefer macOS Keychain for GCS credentials. If the service account JSON is
# stored in the Keychain under service "x-digest" and account
# "gcs-backup-credentials", export it for rclone via the env var.
# Fallback is rclone.conf with service_account_file.
if [[ -z "${RCLONE_GCS_SERVICE_ACCOUNT_CREDENTIALS:-}" ]]; then
    if CREDENTIALS="$(uv run --project "$PROJECT_DIR" python -c "import keyring; v=keyring.get_password('x-digest','gcs-backup-credentials'); print(v or '')" 2>/dev/null)" && [[ -n "$CREDENTIALS" ]]; then
        export RCLONE_GCS_SERVICE_ACCOUNT_CREDENTIALS="$CREDENTIALS"
    fi
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
