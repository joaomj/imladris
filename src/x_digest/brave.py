"""Brave Reading List, as exported by the bundled Brave extension.

The extension in ``extension/`` reads the list through Chromium's
``chrome.readingList`` API and sends it to the native messaging host in
``brave_host``, which writes it to ``Settings.brave_export_path``. Brave's
profile directory is never read, so no macOS privacy permission is needed.
"""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

STATUS_UNREAD = 0
STATUS_READ = 1


class BraveExportError(RuntimeError):
    """Raised when the reading-list export is missing or malformed."""


@dataclass(frozen=True)
class BraveEntry:
    """One Reading List entry."""

    title: str
    url: str
    creation_us: int
    update_us: int
    status: int
    profile: str


def _millis_to_micros(item: dict[str, object], field: str) -> int:
    value = item.get(field)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise BraveExportError(f"reading-list entry has an invalid {field}: {value!r}")
    return round(value * 1000)


def parse_entries(raw: object, profile: str) -> list[BraveEntry]:
    """Validate ``chrome.readingList.query`` results and convert them to entries."""
    if not isinstance(raw, list):
        raise BraveExportError("reading-list export has no entries list")
    entries: list[BraveEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            raise BraveExportError(f"reading-list entry is not an object: {item!r}")
        url = item.get("url")
        title = item.get("title")
        read = item.get("hasBeenRead")
        if not isinstance(url, str) or not url.strip():
            raise BraveExportError(f"reading-list entry has no url: {item!r}")
        if not isinstance(title, str) or not isinstance(read, bool):
            raise BraveExportError(f"reading-list entry has an invalid title or status: {item!r}")
        entries.append(
            BraveEntry(
                title=title,
                url=url.strip(),
                creation_us=_millis_to_micros(item, "creationTime"),
                update_us=_millis_to_micros(item, "lastUpdateTime"),
                status=STATUS_READ if read else STATUS_UNREAD,
                profile=profile,
            )
        )
    entries.sort(key=lambda entry: entry.url)
    return entries


def write_export(path: Path, entries: object, now: datetime | None = None) -> int:
    """Validate the extension's entries and replace the export file atomically."""
    count = len(parse_entries(entries, ""))
    exported_at = (now or datetime.now(UTC)).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"exported_at": exported_at, "entries": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return count


def read_export_time(path: Path) -> datetime:
    """Return when the extension last exported the list."""
    payload = _load(path)
    try:
        exported_at = datetime.fromisoformat(str(payload.get("exported_at")))
    except ValueError as error:
        raise BraveExportError(f"{path} has no valid exported_at") from error
    if exported_at.tzinfo is None:
        raise BraveExportError(f"{path} has an exported_at without a time zone")
    return exported_at


def read_export(path: Path, profile: str) -> tuple[list[BraveEntry], datetime]:
    """Return the exported entries and the time of the export."""
    return parse_entries(_load(path).get("entries"), profile), read_export_time(path)


def _load(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise BraveExportError(
            f"no reading-list export at {path}; run ./scripts/install-brave-extension.sh "
            "and load the extension in Brave"
        ) from error
    except json.JSONDecodeError as error:
        raise BraveExportError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise BraveExportError(f"{path} does not hold a JSON object")
    return payload
