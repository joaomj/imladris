"""Cover the native messaging exchange between the Brave extension and the host."""

import json
import os
import struct
import subprocess
from pathlib import Path

from x_digest.brave import read_export

HOST = Path(__file__).resolve().parents[1] / "scripts" / "brave-reading-list-host"
ENTRY = {
    "url": "https://example.com/a",
    "title": "A",
    "hasBeenRead": True,
    "creationTime": 1795543302117,
    "lastUpdateTime": 1795543302118,
}


def _send(vault: Path, message: object) -> dict[str, object]:
    """Run the host as Brave does and return its decoded reply."""
    body = json.dumps(message).encode()
    result = subprocess.run(
        [str(HOST)],
        input=struct.pack("<I", len(body)) + body,
        capture_output=True,
        env={**os.environ, "XDIGEST_VAULT_PATH": str(vault)},
        check=False,
        timeout=30,
    )
    (size,) = struct.unpack("<I", result.stdout[:4])
    return json.loads(result.stdout[4 : 4 + size])


def test_host_writes_the_reading_list_the_extension_sends(tmp_path: Path) -> None:
    assert _send(tmp_path, {"entries": [ENTRY]}) == {"ok": True, "entries": 1}
    entries, _exported_at = read_export(tmp_path / "state" / "brave-reading-list.json", "Default")
    assert [(e.url, e.title, e.status, e.creation_us) for e in entries] == [
        ("https://example.com/a", "A", 1, 1795543302117000)
    ]


def test_host_rejects_a_malformed_list_and_keeps_the_last_good_export(tmp_path: Path) -> None:
    _send(tmp_path, {"entries": [ENTRY]})
    reply = _send(tmp_path, {"entries": [{"title": "no url"}]})
    assert reply["ok"] is False
    assert "no url" in str(reply["error"])
    entries, _exported_at = read_export(tmp_path / "state" / "brave-reading-list.json", "Default")
    assert [entry.url for entry in entries] == ["https://example.com/a"]
