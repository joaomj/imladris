"""Select active LevelDB files before using the external binary-format reader."""

from collections.abc import Iterable
from pathlib import Path


def live_records(  # noqa: PLR0912
    snapshot: Path,
    manifest_name: str,
) -> Iterable[tuple[bytes, bytes]]:
    """Resolve CURRENT's manifest and last-sequence-wins, including tombstones.

    chromium-reader's RawLevelDb is a forensic iterator over all files. Use
    its individual file parsers instead, restricted to the active manifest.
    """
    # The dependency is optional; X-only commands must not require it.
    from chromium_reader.leveldb import KeyState, LdbFile, LogFile, ManifestFile  # noqa: PLC0415

    manifest = ManifestFile(snapshot / manifest_name)
    active: dict[int, int] = {}
    log_number = None
    previous_log = 0
    try:
        for edit in manifest:
            if edit.comparator not in (None, "leveldb.BytewiseComparator"):
                raise ValueError(f"unsupported LevelDB comparator: {edit.comparator}")
            for _, number in edit.deleted_files:
                active.pop(number, None)
            for entry in edit.new_files:
                active[entry.file_no] = entry.file_size
            if edit.log_number is not None:
                log_number = edit.log_number
            if edit.prev_log_number is not None:
                previous_log = edit.prev_log_number
    finally:
        manifest.close()
    if log_number is None:
        raise ValueError("active LevelDB manifest has no log number")

    paths = []
    for number, size in active.items():
        candidates = [snapshot / f"{number:06d}.{ext}" for ext in ("ldb", "sst")]
        matches = [path for path in candidates if path.exists()]
        if len(matches) != 1 or matches[0].stat().st_size != size:
            raise ValueError(f"active LevelDB table {number} is missing, ambiguous, or incomplete")
        paths.append((matches[0], LdbFile))
    for path in snapshot.glob("*.log"):
        if not path.stem.isdecimal():
            continue
        number = int(path.stem, 10)
        if number >= log_number or number == previous_log:
            paths.append((path, LogFile))

    latest = {}
    for path, parser in paths:
        reader = parser(path)
        try:
            for record in reader:
                key = bytes(record.user_key)
                if record.state not in (KeyState.LIVE, KeyState.DELETED):
                    raise ValueError(f"unknown LevelDB record state in {path.name}")
                previous = latest.get(key)
                if previous is None or record.seq > previous.seq:
                    latest[key] = record
                elif record.seq == previous.seq and (
                    record.state != previous.state or record.value != previous.value
                ):
                    raise ValueError(f"conflicting LevelDB sequence in {path.name}")
        finally:
            reader.close()
    for key, record in latest.items():
        if record.state == KeyState.LIVE:
            yield key, bytes(record.value)
