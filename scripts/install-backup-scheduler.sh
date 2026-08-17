#!/usr/bin/env bash
# Install or remove the weekly Google Drive backup as a launchd LaunchAgent.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.x-digest.backup"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

usage() {
    echo "usage: install-backup-scheduler.sh [--remove]"
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
        <string>/bin/bash</string>
        <string>${PROJECT_DIR}/scripts/backup-to-drive.sh</string>
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
        <integer>15</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>${PROJECT_DIR}/data/logs/backup-scheduler.out.log</string>
    <key>StandardErrorPath</key>
    <string>${PROJECT_DIR}/data/logs/backup-scheduler.err.log</string>
    <key>ProcessType</key>
    <string>Background</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed ${LABEL}: runs every Sunday at 06:15"
