#!/usr/bin/env bash
# Register the native messaging host that receives the Brave reading list
# from the extension in extension/. Safe to run again; --remove undoes it.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_NAME="com.imladris.reading_list"
HOST_PATH="$PROJECT_DIR/scripts/brave-reading-list-host"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
# On macOS, Brave reads user-level hosts from Chrome's directory, not its own
# (brave-core app/brave_main_delegate.cc overrides DIR_USER_NATIVE_MESSAGING).
HOSTS_DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
MANIFEST="$HOSTS_DIR/${HOST_NAME}.json"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "install-brave-extension.sh supports macOS only" >&2
    exit 1
fi
if [[ "${1:-}" == "--remove" ]]; then
    rm -f "$MANIFEST"
    echo "removed ${HOST_NAME}"
    exit 0
fi
if [[ -n "${1:-}" ]]; then
    echo "usage: install-brave-extension.sh [--remove]" >&2
    exit 2
fi
if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "python interpreter not found: $VENV_PYTHON; run 'uv sync' in $PROJECT_DIR first" >&2
    exit 1
fi

# An unpacked extension's ID is derived from the public key in its manifest.
EXTENSION_ID="$("$VENV_PYTHON" - "$PROJECT_DIR/extension/manifest.json" <<'PY'
import base64, hashlib, json, sys
key = json.load(open(sys.argv[1]))["key"]
digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
print("".join(chr(ord("a") + int(c, 16)) for c in digest))
PY
)"

mkdir -p "$HOSTS_DIR"
"$VENV_PYTHON" - "$MANIFEST" "$HOST_NAME" "$HOST_PATH" "$EXTENSION_ID" <<'PY'
import json, sys
path, name, host, extension_id = sys.argv[1:]
manifest = {
    "name": name,
    "description": "Imladris reading list export",
    "path": host,
    "type": "stdio",
    "allowed_origins": [f"chrome-extension://{extension_id}/"],
}
with open(path, "w") as stream:
    json.dump(manifest, stream, indent=2)
PY
echo "registered ${HOST_NAME} for extension ${EXTENSION_ID}"
echo "Next, once: open brave://extensions, turn on Developer mode, choose"
echo "Load unpacked, and select $PROJECT_DIR/extension"
