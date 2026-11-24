"""Native messaging host that receives the reading list from the Brave extension.

Brave starts this process for each message, writes one length-prefixed JSON
message to stdin, and reads one length-prefixed reply from stdout.
"""

import json
import struct
import sys
from typing import BinaryIO

from .brave import BraveExportError, write_export
from .config import load_settings

LENGTH = struct.Struct("<I")


def _read_message(stream: BinaryIO) -> object:
    header = stream.read(LENGTH.size)
    if len(header) != LENGTH.size:
        raise BraveExportError("native message has no length header")
    (size,) = LENGTH.unpack(header)
    body = stream.read(size)
    if len(body) != size:
        raise BraveExportError("native message is shorter than its header")
    return json.loads(body)


def _reply(stream: BinaryIO, reply: dict[str, object]) -> None:
    body = json.dumps(reply).encode("utf-8")
    stream.write(LENGTH.pack(len(body)) + body)
    stream.flush()


def main() -> int:
    """Write one export and tell the extension whether it worked."""
    stdout = sys.stdout.buffer
    try:
        message = _read_message(sys.stdin.buffer)
        if not isinstance(message, dict):
            raise BraveExportError("native message is not a JSON object")
        count = write_export(load_settings().brave_export_path, message.get("entries"))
    except (BraveExportError, OSError, ValueError) as error:
        _reply(stdout, {"ok": False, "error": str(error)})
        return 1
    _reply(stdout, {"ok": True, "entries": count})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
