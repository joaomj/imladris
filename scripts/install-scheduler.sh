#!/usr/bin/env bash
# Install or remove the weekly X Digest sync as a launchd LaunchAgent.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.x-digest.sync"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
PASS_CLI_BIN="$(command -v pass-cli || true)"
# launchd starts agents with a minimal PATH, so the directory that holds
# pass-cli is added here; Proton Pass lookups fail without it.
JOB_PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [[ -n "$PASS_CLI_BIN" ]]; then
    JOB_PATH="$(dirname "$PASS_CLI_BIN"):${JOB_PATH}"
fi

usage() {
    echo "usage: install-scheduler.sh [--remove]"
}

remove() {
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed ${LABEL}"
}

if [[ "${1:-}" == "--remove" ]]; then
    remove
    exit 0
fi
if [[ -n "${1:-}" ]]; then
    usage
    exit 2
fi
# The agent runs the project interpreter directly, so Full Disk Access can be
# granted to that one binary instead of the whole shell.
if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "python interpreter not found: $VENV_PYTHON" >&2
    echo "Run 'uv sync --extra brave' in $PROJECT_DIR first." >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/data/logs"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_PYTHON}</string>
        <string>-m</string>
        <string>x_digest</string>
        <string>sync</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>0</integer>
        <key>Hour</key>
        <integer>6</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>${PROJECT_DIR}/data/logs/scheduler.out.log</string>
    <key>StandardErrorPath</key>
    <string>${PROJECT_DIR}/data/logs/scheduler.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${JOB_PATH}</string>
        <key>PROTON_PASS_KEY_PROVIDER</key>
        <string>fs</string>
    </dict>
    <key>ProcessType</key>
    <string>Background</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed ${LABEL}: X collection every Sunday at 06:00"
