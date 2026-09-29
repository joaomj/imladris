"""Read-only ingest of Brave's native Reading List from its LevelDB store.

The reader never opens the live browser directory. It copies the LevelDB
files into a temporary snapshot, checks that the source stayed stable
during the copy with bounded retries, and only then reads from the copy.
The pure-Python reader parses only the copy. Browser cookies and credential
files are never read.
"""

import hashlib
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .brave_leveldb import live_records

VARINT_BITS = 64
WIRE_LENGTH_DELIMITED = 2
WIRE_FIXED32 = 5
READING_LIST_PREFIX = "reading_list-dt-"
READING_LIST_FAMILY = "reading_list"

STATUS_TEXT = {0: "unread", 1: "read", 2: "unseen"}

COPY_SUFFIXES = (".log", ".ldb", ".sst")
COPY_EXACT = {"CURRENT", "LOG", "LOG.old"}


class BraveReaderError(RuntimeError):
    """Raised when the Brave snapshot cannot be taken or read."""


class BraveDecodeError(ValueError):
    """Raised when one Reading List value does not follow the known shape."""


@dataclass(frozen=True)
class BraveEntry:
    """One decoded native Reading List entry."""

    key: str
    entry_id: str
    title: str
    url: str
    creation_us: int | None
    update_us: int | None
    status: int
    status_text: str
    profile: str


def _read_varint(data: bytes, position: int) -> tuple[int, int]:
    """Read one protobuf varint; fail explicitly on truncation or overflow."""
    result = 0
    shift = 0
    while True:
        if position >= len(data):
            raise BraveDecodeError("truncated varint")
        byte = data[position]
        position += 1
        if shift == VARINT_BITS - 1 and byte > 1:
            raise BraveDecodeError("varint overflow")
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7
        if shift >= VARINT_BITS:
            raise BraveDecodeError("varint overflow")


def decode_reading_list_value(data: bytes) -> dict[str, object]:  # noqa: PLR0912, PLR0915
    """Decode one ReadingListLocal value with strict top-level parsing.

    Known fields are entry_id (1), title (2), url (3), creation time in
    microseconds (4), update time in microseconds (5), and status (6).
    Unknown fields are skipped by wire type. Nested messages are never
    descended into: wire types for groups are rejected instead of guessed.
    """
    fields: dict[int, list[object]] = {}
    position = 0
    while position < len(data):
        key, position = _read_varint(data, position)
        field_number, wire_type = key >> 3, key & 0x07
        if not field_number:
            raise BraveDecodeError("invalid protobuf field zero")
        expected_wire = {1: 2, 2: 2, 3: 2, 4: 0, 5: 0, 6: 0}.get(field_number)
        if expected_wire is not None and wire_type != expected_wire:
            raise BraveDecodeError(f"wrong wire type for field {field_number}")
        if wire_type == 0:
            value, position = _read_varint(data, position)
            fields.setdefault(field_number, []).append(value)
        elif wire_type == WIRE_LENGTH_DELIMITED:
            length, position = _read_varint(data, position)
            end = position + length
            if end > len(data):
                raise BraveDecodeError("truncated length-delimited field")
            fields.setdefault(field_number, []).append(data[position:end])
            position = end
        elif wire_type == 1:
            if position + 8 > len(data):
                raise BraveDecodeError("truncated 64-bit field")
            fields.setdefault(field_number, []).append(data[position : position + 8])
            position += 8
        elif wire_type == WIRE_FIXED32:
            if position + 4 > len(data):
                raise BraveDecodeError("truncated 32-bit field")
            fields.setdefault(field_number, []).append(data[position : position + 4])
            position += 4
        else:
            raise BraveDecodeError(f"unsupported wire type {wire_type}: refusing to guess")

    def _text(field_number: int) -> str | None:
        values = fields.get(field_number, [])
        raw = values[-1] if values else None
        if raw is None:
            return None
        if not isinstance(raw, bytes):
            raise BraveDecodeError(f"field {field_number} is not a string field")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise BraveDecodeError(f"field {field_number} is not valid UTF-8") from error

    def _number(field_number: int) -> int | None:
        values = fields.get(field_number, [])
        raw = values[-1] if values else None
        if raw is None:
            return None
        if not isinstance(raw, int):
            raise BraveDecodeError(f"field {field_number} is not a varint field")
        return raw

    entry_id = _text(1)
    title = _text(2)
    url = _text(3)
    status = _number(6)
    if not entry_id:
        raise BraveDecodeError("missing entry_id (field 1)")
    if not url:
        raise BraveDecodeError("missing url (field 3)")
    if entry_id != url:
        raise BraveDecodeError("entry_id must equal url")
    if status is None:
        status = 2
    if status not in STATUS_TEXT:
        raise BraveDecodeError(f"unknown status value: {status}")
    return {
        "entry_id": entry_id,
        "title": title or "",
        "url": url,
        "creation_us": _number(4),
        "update_us": _number(5),
        "status": status,
    }


def _leveldb_files(directory: Path) -> list[Path]:
    """List the LevelDB files that make up one database snapshot."""
    if not directory.is_dir():
        raise BraveReaderError(f"not a directory: {directory}")
    files = [
        path
        for path in sorted(directory.iterdir())
        if path.is_file()
        and (
            path.name in COPY_EXACT
            or path.name.startswith("MANIFEST-")
            or path.suffix in COPY_SUFFIXES
        )
    ]
    return files


def _hash_files(files: Iterable[Path]) -> dict[str, str]:
    """Hash files in chunks; missing files hash as absent."""
    digests: dict[str, str] = {}
    for path in files:
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            digests[path.name] = digest.hexdigest()
        except FileNotFoundError:
            digests[path.name] = "<absent>"
    return digests


def current_manifest_name(directory: Path) -> str:
    """Return the active MANIFEST name from CURRENT, failing when unclear."""
    current = directory / "CURRENT"
    if not current.is_file():
        raise BraveReaderError(
            f"no CURRENT file in {directory}: not a LevelDB directory, "
            "pass --db-path pointing at the Reading List LevelDB"
        )
    lines = [line.strip() for line in current.read_text(encoding="utf-8").splitlines()]
    lines = [line for line in lines if line]
    if len(lines) != 1 or not re.fullmatch(r"MANIFEST-[0-9]+", lines[0]):
        raise BraveReaderError(f"CURRENT must name one MANIFEST basename in {directory}")
    return lines[0]


def snapshot_leveldb(source: Path, retries: int = 5) -> Path:
    """Copy LevelDB files into a temp dir with bounded stability retries.

    The source is only read, never opened as a database. After copying, the
    source hashes are compared before and after the copy; a changing source
    (a running browser writing) retries with backoff, then fails explicitly
    instead of ingesting a torn snapshot. The caller owns the returned
    directory and must remove it.
    """
    if retries < 0:
        raise BraveReaderError("snapshot retries must not be negative")
    last_error = "unknown"
    for attempt in range(retries + 1):
        manifest = current_manifest_name(source)
        files = _leveldb_files(source)
        if not (source / manifest).is_file():
            raise BraveReaderError(f"active {manifest} is missing in {source}")
        if not any(path.name.startswith("MANIFEST-") for path in files):
            raise BraveReaderError(f"no MANIFEST file found in {source}")
        before = _hash_files(files)
        destination = Path(tempfile.mkdtemp(prefix="brave-leveldb-snapshot-"))
        try:
            for path in files:
                if path.is_symlink():
                    raise BraveReaderError(f"refusing symlink in LevelDB: {path}")
                shutil.copyfile(path, destination / path.name)
            after = _hash_files(_leveldb_files(source))
            copied = _hash_files(_leveldb_files(destination))
            stable = before == after and current_manifest_name(source) == manifest
            intact = before == copied
            if stable and intact:
                return destination
            last_error = "source changed during copy" if not stable else "copy mismatch"
        except FileNotFoundError as error:
            last_error = str(error)
        except Exception:
            shutil.rmtree(destination)
            raise
        shutil.rmtree(destination)
        if attempt < retries:
            time.sleep(0.2 * (2**attempt))
    raise BraveReaderError(
        f"could not take a stable snapshot of {source} after {retries + 1} attempts "
        f"({last_error}); the browser may be writing heavily, retry later"
    )


KvRecord = tuple[bytes, bytes | None]
KvReader = Callable[[Path], Iterable[KvRecord]]


def default_kv_reader(snapshot: Path) -> Iterable[KvRecord]:
    """Use the optional pure-Python parser on active snapshot files only."""
    try:
        yield from live_records(snapshot, current_manifest_name(snapshot))
    except ImportError as error:
        raise BraveReaderError(
            "Brave ingestion requires chromium-reader; run uv sync --extra brave"
        ) from error


def read_entries(
    snapshot: Path,
    profile: str,
    kv_reader: KvReader | None = None,
) -> tuple[list[BraveEntry], dict[str, object]]:
    """Decode live Reading List entries from a snapshot directory.

    CURRENT/MANIFEST presence is validated first so a torn copy fails here
    even when a backend would silently iterate stale files. Keys equal to a
    tombstone (None value) are skipped; when a key appears more than once
    the last occurrence wins and the duplicate is counted. Only keys under
    `reading_list-dt-` are decoded; other `reading_list*` namespaces are
    reported so sync-account variants stay visible instead of guessed.
    """
    manifest = current_manifest_name(snapshot)
    if not (snapshot / manifest).is_file():
        raise BraveReaderError(f"active {manifest} from CURRENT is missing in snapshot {snapshot}")
    reader = kv_reader or default_kv_reader
    merged: dict[bytes, bytes | None] = {}
    duplicates = 0
    for key, value in reader(snapshot):
        if key in merged:
            duplicates += 1
        merged[key] = value
    entries: list[BraveEntry] = []
    decode_failures = 0
    tombstones = 0
    other_namespaces: set[str] = set()
    for raw_key, raw_value in merged.items():
        if not raw_key.startswith(READING_LIST_FAMILY.encode()):
            continue
        key = raw_key.decode("utf-8")
        if not key.startswith(READING_LIST_PREFIX):
            other_namespaces.add(key.split("-", 2)[0] if "-" in key else key)
            continue
        if raw_value is None:
            tombstones += 1
            continue
        try:
            decoded = decode_reading_list_value(bytes(raw_value))
        except BraveDecodeError as error:
            raise BraveReaderError(f"invalid reading-list record {key!r}: {error}") from error
        if key.removeprefix(READING_LIST_PREFIX) != decoded["url"]:
            raise BraveReaderError(f"reading-list key does not match URL: {key!r}")
        status = int(decoded["status"])  # type: ignore[arg-type]
        entries.append(
            BraveEntry(
                key=key,
                entry_id=str(decoded["entry_id"]),
                title=str(decoded["title"]),
                url=str(decoded["url"]).strip(),
                creation_us=decoded["creation_us"],  # type: ignore[arg-type]
                update_us=decoded["update_us"],  # type: ignore[arg-type]
                status=status,
                status_text=STATUS_TEXT[status],
                profile=profile,
            )
        )
    entries.sort(key=lambda entry: entry.url)
    counts: dict[str, object] = {
        "entries": len(entries),
        "duplicates": duplicates,
        "tombstones": tombstones,
        "decode_failures": decode_failures,
        "other_namespaces": sorted(other_namespaces),
        "manifest": manifest,
    }
    return entries, counts


def find_brave_db(explicit: Path | None, profile: str) -> Path:
    """Resolve the Brave Reading List LevelDB directory.

    An explicit --db-path always wins. Otherwise well-known profile
    locations are probed in order. Storage layouts vary across Brave and
    Chromium releases, so a miss fails with guidance instead of guessing.
    """
    if explicit is not None:
        candidate = explicit.expanduser()
        if (candidate / "CURRENT").is_file():
            return candidate.resolve()
        raise BraveReaderError(
            f"no CURRENT file in explicit database path {candidate}; "
            "point --db-path at the LevelDB directory holding CURRENT, "
            "MANIFEST-*, *.log and *.ldb files"
        )
    home = Path.home()
    candidates = [
        home
        / "Library/Application Support/BraveSoftware/Brave-Browser"
        / profile
        / "Sync Data/LevelDB",
        home / "Library/Application Support/BraveSoftware/Brave-Browser" / profile,
        home / ".config/BraveSoftware/Brave-Browser" / profile / "Sync Data/LevelDB",
        home / ".config/BraveSoftware/Brave-Browser" / profile,
    ]
    for candidate in candidates:
        if (candidate / "CURRENT").is_file():
            return candidate.resolve()
    searched = "\n".join(f"  - {candidate}" for candidate in candidates)
    raise BraveReaderError(
        "could not find the Brave Reading List LevelDB; searched:\n"
        f"{searched}\n"
        "Pass --db-path pointing at the directory that holds CURRENT, "
        "MANIFEST-*, *.log and *.ldb for the profile with the reading list."
    )
