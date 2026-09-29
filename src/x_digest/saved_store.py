"""Persist Brave saved items and rebuild them from Bronze objects.

Bronze kinds for saved content:

- ``saved-snapshot``: the Brave URL-list snapshot for one profile. Payload is
  ``{"profile", "snapshot_sha256", "entries": [...], "counts": {...}}``.
- ``saved-page``: one Donsetch webpage extraction. Payload is
  ``{"url", "final_url", "content_text", "content_hash", "response"}`` where
  ``response`` is the complete Donsetch JSON envelope.
- ``saved-api-record``: one approved API record, with the same rendered-text
  fields as ``saved-page``. The response preserves provider metadata and
  the raw response bytes as base64; it is not a full paper.
- ``saved-pdf-meta``: one PDF download. Payload is
  ``{"url", "final_url", "content_type", "content_hash", "archive_path",
  "size"}``. The original bytes live in the Bronze media file at
  ``archive_path`` and are never text-extracted.
- ``saved-fetch-failure``: one failed attempt. Payload is
  ``{"url", "final_url", "stage", "error", "attempts", "response"}``.

Replay API for Gold integration (planned ``rebuild-saved``):

- :func:`replay_saved_object` applies one Bronze object to ``saved_items``.
- :func:`rebuild_saved_from_bronze` replays every ``saved-*`` Bronze object
  in ``created_at`` order without any network access.
"""

import gzip
import hashlib
import json
from typing import Any

from .brave import BraveEntry
from .db import Database, utc_now
from .fulltext_store import FULLTEXT_KIND, replay_fulltext
from .paths import resolve_stored_path
from .saved_fetch import FetchOutcome
from .saved_urls import SAVED_URL_EXCLUSION_REASON, is_excluded_saved_url

SNAPSHOT_KIND = "saved-snapshot"
PAGE_KIND = "saved-page"
API_RECORD_KIND = "saved-api-record"
PDF_META_KIND = "saved-pdf-meta"
FAILURE_KIND = "saved-fetch-failure"

SKIPPED_KIND = "saved-fetch-skipped"

SAVED_KINDS = (
    SNAPSHOT_KIND,
    PAGE_KIND,
    API_RECORD_KIND,
    PDF_META_KIND,
    FAILURE_KIND,
    SKIPPED_KIND,
    FULLTEXT_KIND,
)


def checkpoint_key(profile: str) -> str:
    """Return the snapshot-hash checkpoint key for one profile."""
    return f"saved:snapshot:{profile}"


def entry_dict(entry: BraveEntry) -> dict[str, Any]:
    """Return the replay-friendly dict for one Brave entry."""
    return {
        "key": entry.key,
        "entry_id": entry.entry_id,
        "title": entry.title,
        "url": entry.url,
        "creation_us": entry.creation_us,
        "update_us": entry.update_us,
        "status": entry.status,
        "status_text": entry.status_text,
        "profile": entry.profile,
    }


def snapshot_hash(entries: list[BraveEntry]) -> str:
    """Hash the canonical snapshot so unchanged lists dedup exactly."""
    canonical = sorted(
        (
            {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "url": entry.url,
                "creation_us": entry.creation_us,
                "update_us": entry.update_us,
                "status": entry.status,
                "profile": entry.profile,
            }
            for entry in entries
        ),
        key=lambda item: (item["url"], item["entry_id"]),
    )
    serialized = json.dumps(canonical, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(serialized).hexdigest()


def snapshot_payload(
    entries: list[BraveEntry], profile: str, digest: str, counts: dict[str, Any]
) -> dict[str, Any]:
    """Build the ``saved-snapshot`` Bronze payload."""
    return {
        "profile": profile,
        "snapshot_sha256": digest,
        "entries": [entry_dict(entry) for entry in entries],
        "counts": counts,
    }


def page_payload(outcome: FetchOutcome) -> dict[str, Any]:
    """Build a rendered page or API record payload with provider provenance."""
    return {
        "url": outcome.original_url,
        "final_url": outcome.final_url,
        "content_text": outcome.content_text or "",
        "content_kind": outcome.kind,
        "provider": outcome.stage,
        "content_hash": outcome.content_hash,
        "response": outcome.raw,
    }


def pdf_meta_payload(outcome: FetchOutcome, archive_path: str, size: int) -> dict[str, Any]:
    """Build the ``saved-pdf-meta`` Bronze payload from a PDF outcome."""
    return {
        "url": outcome.original_url,
        "final_url": outcome.final_url,
        "content_type": outcome.content_type,
        "content_hash": outcome.content_hash,
        "archive_path": archive_path,
        "size": size,
    }


def failure_payload(outcome: FetchOutcome, attempts: int) -> dict[str, Any]:
    """Build the ``saved-fetch-failure`` Bronze payload."""
    return {
        "url": outcome.original_url,
        "final_url": outcome.final_url,
        "stage": outcome.stage,
        "error": outcome.error,
        "attempts": attempts,
        "response": outcome.raw,
    }


def upsert_entries(
    database: Database, entries: list[BraveEntry], now: str | None = None
) -> dict[str, int]:
    """Insert new saved URLs and refresh Brave metadata on known ones.

    Fetch state, attempts, content, hashes, errors, and timestamps from
    previous fetch work are never cleared here. All Brave statuses are
    stored; no entry is filtered.
    """
    timestamp = now or utc_now()
    with database.connect() as connection:
        known = {str(row["url"]) for row in connection.execute("SELECT url FROM saved_items")}
    counts = {"upserted": 0, "new": 0}
    with database.transaction() as connection:
        for entry in entries:
            is_new = entry.url not in known
            known.add(entry.url)
            connection.execute(
                """INSERT INTO saved_items(
                    url, entry_id, title, brave_creation_us, brave_update_us,
                    brave_status, profile, source, fetch_state, attempts,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'brave_reading_list', 'pending', 0, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    entry_id=excluded.entry_id,
                    title=excluded.title,
                    brave_creation_us=excluded.brave_creation_us,
                    brave_update_us=excluded.brave_update_us,
                    brave_status=excluded.brave_status,
                    profile=excluded.profile,
                    last_seen_at=excluded.last_seen_at""",
                (
                    entry.url,
                    entry.entry_id,
                    entry.title,
                    entry.creation_us,
                    entry.update_us,
                    entry.status,
                    entry.profile,
                    timestamp,
                    timestamp,
                ),
            )
            if is_excluded_saved_url(entry.url):
                connection.execute(
                    "UPDATE saved_items SET fetch_state='skipped', fetch_error=? "
                    "WHERE url=? AND fetch_state != 'fetched'",
                    (SAVED_URL_EXCLUSION_REASON, entry.url),
                )
            content = connection.execute(
                "SELECT content_text FROM saved_items WHERE url=?", (entry.url,)
            ).fetchone()["content_text"]
            _sync_fts(connection, entry.url, entry.title, content or "")
            counts["upserted"] += 1
            if is_new:
                counts["new"] += 1
    return counts


def get_due_items(database: Database, max_attempts: int) -> list[dict[str, Any]]:
    """Return pending items plus failed items below the retry bound."""
    with database.connect() as connection:
        rows = connection.execute(
            """SELECT url, entry_id, title, attempts, fetch_state FROM saved_items
               WHERE fetch_state = 'pending'
                  OR (fetch_state = 'failed' AND attempts < ?)
               ORDER BY first_seen_at, url""",
            (max_attempts,),
        ).fetchall()
    return [dict(row) for row in rows]


def _sync_fts(connection: Any, url: str, title: str, content: str) -> None:
    """Replace the FTS row for one saved URL."""
    connection.execute("DELETE FROM saved_items_fts WHERE url = ?", (url,))
    connection.execute(
        "INSERT INTO saved_items_fts(url, title, content) VALUES (?, ?, ?)",
        (url, title, content),
    )


def _next_attempt(connection: Any, url: str) -> tuple[int, str]:
    """Return the incremented attempt count and current title for one URL."""
    row = connection.execute(
        "SELECT attempts, title FROM saved_items WHERE url = ?", (url,)
    ).fetchone()
    if row is None:
        return 1, ""
    return int(row["attempts"] or 0) + 1, str(row["title"] or "")


def mark_page_success(database: Database, outcome: FetchOutcome, bronze_object_id: str) -> None:
    """Record successfully rendered text from a webpage or an API record."""
    now = utc_now()
    text = outcome.content_text or ""
    with database.transaction() as connection:
        attempts, title = _next_attempt(connection, outcome.original_url)
        connection.execute(
            """UPDATE saved_items SET fetch_state='fetched', fetch_error=NULL,
               attempts=?, content_type='text/markdown', final_url=?,
               content_hash=?, bronze_object_id=?, archive_path=NULL,
               content_text=?, truncated=0, last_seen_at=?, fetched_at=?
               WHERE url=?""",
            (
                attempts,
                outcome.final_url,
                outcome.content_hash,
                bronze_object_id,
                text,
                now,
                now,
                outcome.original_url,
            ),
        )
        _sync_fts(connection, outcome.original_url, title, text)


def mark_pdf_success(
    database: Database, outcome: FetchOutcome, bronze_object_id: str, archive_path: str
) -> None:
    """Record a successful PDF download; bytes stay unchanged on disk."""
    now = utc_now()
    with database.transaction() as connection:
        attempts, title = _next_attempt(connection, outcome.original_url)
        connection.execute(
            """UPDATE saved_items SET fetch_state='fetched', fetch_error=NULL,
               attempts=?, content_type=?, final_url=?,
               content_hash=?, bronze_object_id=?, archive_path=?,
               content_text=NULL, truncated=0, last_seen_at=?, fetched_at=?
               WHERE url=?""",
            (
                attempts,
                outcome.content_type,
                outcome.final_url,
                outcome.content_hash,
                bronze_object_id,
                archive_path,
                now,
                now,
                outcome.original_url,
            ),
        )
        _sync_fts(connection, outcome.original_url, title, "")


def mark_failure(
    database: Database, url: str, error: str, bronze_object_id: str | None = None
) -> int:
    """Record a failed attempt while preserving prior content and metadata.

    Returns the new attempt count. Content columns, hashes, archive paths,
    and previously fetched text are left untouched.
    """
    now = utc_now()
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT attempts FROM saved_items WHERE url = ?", (url,)
        ).fetchone()
        attempts = int(row["attempts"] or 0) + 1 if row else 1
        if row is None:
            connection.execute(
                """INSERT INTO saved_items(url, fetch_state, fetch_error, attempts,
                   first_seen_at, last_seen_at, bronze_object_id)
                   VALUES (?, 'failed', ?, ?, ?, ?, ?)""",
                (url, error[:2000], attempts, now, now, bronze_object_id),
            )
        elif bronze_object_id is None:
            connection.execute(
                """UPDATE saved_items SET fetch_state='failed', fetch_error=?,
                   attempts=?, last_seen_at=? WHERE url=?""",
                (error[:2000], attempts, now, url),
            )
        else:
            connection.execute(
                """UPDATE saved_items SET fetch_state='failed', fetch_error=?,
                   attempts=?, last_seen_at=?, bronze_object_id=? WHERE url=?""",
                (error[:2000], attempts, now, bronze_object_id, url),
            )
    return attempts


def mark_skipped(database: Database, url: str, reason: str, object_id: str) -> None:
    """Retain excluded saved URLs without consuming fetch attempts."""
    with database.transaction() as connection:
        connection.execute(
            "UPDATE saved_items SET fetch_state='skipped', fetch_error=?, bronze_object_id=? "
            "WHERE url=? AND fetch_state != 'fetched'",
            (reason, object_id, url),
        )


def _replay_snapshot(database: Database, payload: dict[str, Any]) -> None:
    """Upsert URL metadata from a ``saved-snapshot`` payload."""
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("saved-snapshot payload has no entries list")
    now = utc_now()
    with database.transaction() as connection:
        for item in entries:
            if not isinstance(item, dict) or not item.get("url"):
                raise ValueError("saved snapshot contains an invalid entry")
            connection.execute(
                """INSERT INTO saved_items(
                    url, entry_id, title, brave_creation_us, brave_update_us,
                    brave_status, profile, source, fetch_state, attempts,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'brave_reading_list', 'pending', 0, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    entry_id=excluded.entry_id,
                    title=excluded.title,
                    brave_creation_us=excluded.brave_creation_us,
                    brave_update_us=excluded.brave_update_us,
                    brave_status=excluded.brave_status,
                    profile=excluded.profile,
                    last_seen_at=excluded.last_seen_at""",
                (
                    str(item["url"]),
                    str(item.get("entry_id") or "") or None,
                    str(item.get("title") or "") or None,
                    item.get("creation_us"),
                    item.get("update_us"),
                    item.get("status"),
                    str(item.get("profile") or ""),
                    now,
                    now,
                ),
            )


def _replay_page(database: Database, payload: dict[str, Any], bronze_object_id: str) -> None:
    """Apply a ``saved-page`` payload as a fetched page."""
    url = str(payload.get("url") or "")
    if not url:
        raise ValueError("saved-page payload has no url")
    text = str(payload.get("content_text") or "")
    now = utc_now()
    with database.transaction() as connection:
        attempts, title = _next_attempt(connection, url)
        if attempts == 1 and not title:
            connection.execute(
                "INSERT OR IGNORE INTO saved_items(url, first_seen_at, last_seen_at)"
                " VALUES (?, ?, ?)",
                (url, now, now),
            )
        connection.execute(
            """UPDATE saved_items SET fetch_state='fetched', fetch_error=NULL,
               attempts=?, content_type='text/markdown', final_url=?,
               content_hash=?, bronze_object_id=?, archive_path=NULL,
               content_text=?, truncated=0, last_seen_at=?, fetched_at=?
               WHERE url=?""",
            (
                attempts,
                payload.get("final_url"),
                payload.get("content_hash"),
                bronze_object_id,
                text,
                now,
                now,
                url,
            ),
        )
        _sync_fts(connection, url, title, text)


def _replay_pdf(database: Database, payload: dict[str, Any], bronze_object_id: str) -> None:
    """Apply a ``saved-pdf-meta`` payload as a fetched PDF."""
    url = str(payload.get("url") or "")
    if not url:
        raise ValueError("saved-pdf-meta payload has no url")
    now = utc_now()
    with database.transaction() as connection:
        attempts, title = _next_attempt(connection, url)
        if attempts == 1 and not title:
            connection.execute(
                "INSERT OR IGNORE INTO saved_items(url, first_seen_at, last_seen_at)"
                " VALUES (?, ?, ?)",
                (url, now, now),
            )
        connection.execute(
            """UPDATE saved_items SET fetch_state='fetched', fetch_error=NULL,
               attempts=?, content_type=?, final_url=?, content_hash=?,
               bronze_object_id=?, archive_path=?, content_text=NULL,
               truncated=0, last_seen_at=?, fetched_at=? WHERE url=?""",
            (
                attempts,
                payload.get("content_type"),
                payload.get("final_url"),
                payload.get("content_hash"),
                bronze_object_id,
                payload.get("archive_path"),
                now,
                now,
                url,
            ),
        )
        _sync_fts(connection, url, title, "")


def replay_saved_object(
    database: Database, kind: str, payload: dict[str, Any], bronze_object_id: str
) -> str:
    """Apply one Bronze payload to ``saved_items`` without network access."""
    if kind == FULLTEXT_KIND:
        replay_fulltext(database, payload, bronze_object_id)
        return "fulltext"
    if kind == SNAPSHOT_KIND:
        _replay_snapshot(database, payload)
        return "snapshot"
    if kind in (PAGE_KIND, API_RECORD_KIND):
        _replay_page(database, payload, bronze_object_id)
        return "api_record" if kind == API_RECORD_KIND else "page"
    if kind == PDF_META_KIND:
        _replay_pdf(database, payload, bronze_object_id)
        return "pdf"
    if kind == SKIPPED_KIND:
        mark_skipped(database, payload["url"], payload["reason"], bronze_object_id)
        return "skipped"
    if kind == FAILURE_KIND:
        url = str(payload.get("url") or "")
        if not url:
            raise ValueError("saved-fetch-failure payload has no url")
        mark_failure(database, url, str(payload.get("error") or "fetch failed"), bronze_object_id)
        return "failure"
    raise ValueError(f"not a saved bronze kind: {kind}")


def rebuild_saved_from_bronze(database: Database) -> dict[str, int]:
    """Rebuild ``saved_items`` and its FTS from ``saved-*`` Bronze objects.

    Reads Bronze JSON payloads in ``created_at`` order and applies them with
    :func:`replay_saved_object`. Media bytes are not re-downloaded; PDF
    verification compares the archived file hash separately.
    """
    with database.connect() as connection:
        objects = connection.execute(
            """SELECT object_id, kind, path FROM bronze_objects
               WHERE kind IN ('saved-snapshot', 'saved-page', 'saved-api-record', 'saved-pdf-meta',
                              'saved-fetch-failure', 'saved-fetch-skipped', 'saved-fulltext')
               ORDER BY created_at, object_id"""
        ).fetchall()
    counts = {
        "objects": 0,
        "fulltext": 0,
        "snapshot": 0,
        "page": 0,
        "api_record": 0,
        "pdf": 0,
        "failure": 0,
        "skipped": 0,
    }
    vault_root = database.path.parent
    payloads = []
    for row in objects:
        compressed = resolve_stored_path(vault_root, row["path"]).read_bytes()
        payload = json.loads(gzip.decompress(compressed).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"invalid saved Bronze payload: {row['path']}")
        payloads.append((row, payload))
    with database.transaction() as connection:
        connection.execute("DELETE FROM saved_items_fts")
        connection.execute("DELETE FROM saved_items")
    for row, payload in payloads:
        applied = replay_saved_object(database, row["kind"], payload, row["object_id"])
        counts["objects"] += 1
        counts[applied] += 1
    with database.transaction() as connection:
        for row in connection.execute(
            "SELECT url FROM saved_items WHERE fetch_state != 'fetched'"
        ).fetchall():
            if is_excluded_saved_url(row["url"]):
                connection.execute(
                    "UPDATE saved_items SET fetch_state='skipped', fetch_error=? WHERE url=?",
                    (SAVED_URL_EXCLUSION_REASON, row["url"]),
                )
        connection.execute("DELETE FROM saved_items_fts")
        connection.execute(
            "INSERT INTO saved_items_fts(url,title,content) "
            "SELECT url, COALESCE(title,''), COALESCE(content_text,'') FROM saved_items"
        )
    return counts
