"""Archive full paper bytes apart from saved abstracts."""

import hashlib
import json
import sqlite3
from typing import TYPE_CHECKING, Any

from .bronze import BronzeWriter, BronzeWriteRequest
from .db import Database, utc_now
from .paths import stored_path

if TYPE_CHECKING:
    from .fulltext import FullTextResult

FULLTEXT_KIND = "saved-fulltext"

_STATES = frozenset({"fetched", "unavailable", "failed"})
_EXTENSIONS = {"application/pdf": "pdf", "application/xml": "xml"}
_NULLABLE_FIELDS = (
    "provider",
    "source_url",
    "final_url",
    "content_type",
    "license",
    "version",
    "error",
)


def _base_content_type(value: str | None) -> str:
    """Return the MIME type without parameters in lower case."""
    return (value or "").split(";")[0].strip().lower()


def _extension_for_content_type(content_type: str | None, url: str) -> str:
    """Return the archive extension for a supported paper type."""
    try:
        return _EXTENSIONS[_base_content_type(content_type)]
    except KeyError:
        raise ValueError(
            f"fetched full text has unsupported content type for {url}: {content_type}"
        ) from None


def _optional_str(payload: dict[str, Any], name: str, url: str) -> str | None:
    """Return one nullable string field or fail on a wrong type."""
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"full-text payload field {name} must be a string or null for {url}")
    return value


def _check_result_strings(result: "FullTextResult", url: str) -> None:
    """Fail when a result field has a wrong type."""
    if result.state not in _STATES:
        raise ValueError(f"unknown full-text state for {url}: {result.state}")
    if not isinstance(result.identifiers, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in result.identifiers.items()
    ):
        raise ValueError(f"full-text identifiers must be string pairs for {url}")
    if not isinstance(result.provenance, list):
        raise ValueError(f"full-text provenance must be a list for {url}")
    for name in _NULLABLE_FIELDS:
        value = getattr(result, name, None)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"full-text field {name} must be a string or null for {url}")


def _prior_attempts(database: Database, url: str) -> tuple[int, str | None]:
    """Return the stored attempt count and state for one URL."""
    try:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT state, attempts FROM saved_fulltext WHERE url = ?", (url,)
            ).fetchone()
            if row is None:
                return 0, None
            raw_attempts: Any = row["attempts"]
            raw_state: Any = row["state"]
    except sqlite3.Error as error:
        raise RuntimeError(f"failed to read prior full-text state for {url}: {error}") from error
    if not isinstance(raw_attempts, int) or isinstance(raw_attempts, bool) or raw_attempts < 0:
        raise ValueError(f"stored full-text attempts are corrupt for {url}")
    if not isinstance(raw_state, str) or not raw_state:
        raise ValueError(f"stored full-text state is corrupt for {url}")
    return raw_attempts, raw_state


def archive_fulltext(  # noqa: PLR0913, PLR0917
    database: Database,
    bronze: BronzeWriter,
    run_id: str,
    url: str,
    result: "FullTextResult",
    sequence: int,
) -> dict[str, Any]:
    """Store one paper result in Bronze, then apply it to SQLite."""
    if not isinstance(url, str) or not url:
        raise ValueError("full-text url must be a non-empty string")
    _check_result_strings(result, url)
    prior_attempts, _ = _prior_attempts(database, url)
    attempts = prior_attempts + 1
    archive_path: str | None = None
    content_hash: str | None = None
    if result.state == "fetched":
        body = result.body
        if not isinstance(body, bytes) or not body:
            raise ValueError(f"fetched full text has no body for {url}")
        extension = _extension_for_content_type(result.content_type, url)
        media_key = f"saved-fulltext-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:32]}"
        try:
            stored, content_hash = bronze.write_media(run_id, media_key, body, extension)
        except OSError as error:
            raise RuntimeError(f"failed to store full-text bytes for {url}: {error}") from error
        try:
            archive_path = stored_path(bronze.vault_path, stored)
        except ValueError as error:
            raise ValueError(
                f"full-text archive path is outside the vault for {url}: {error}"
            ) from error
    checked_at = utc_now()
    payload: dict[str, Any] = {
        "url": url,
        "state": result.state,
        "attempts": attempts,
        "identifiers": dict(result.identifiers),
        "provider": result.provider,
        "source_url": result.source_url,
        "final_url": result.final_url,
        "content_type": result.content_type,
        "license": result.license,
        "version": result.version,
        "archive_path": archive_path,
        "content_hash": content_hash,
        "error": result.error,
        "provenance": list(result.provenance),
        "checked_at": checked_at,
    }
    try:
        record = bronze.write_json(
            BronzeWriteRequest(
                run_id,
                FULLTEXT_KIND,
                payload,
                "saved://fulltext",
                None,
                [url],
                sequence,
                {"url": url, "state": result.state, "provider": result.provider},
            )
        )
    except (sqlite3.Error, OSError) as error:
        raise RuntimeError(f"failed to write full-text Bronze object for {url}: {error}") from error
    except ValueError as error:
        raise ValueError(f"failed to write full-text Bronze object for {url}: {error}") from error
    replay_fulltext(database, payload, record.object_id)
    return payload


def _replay_fields(  # noqa: PLR0912
    payload: dict[str, Any],
) -> tuple[str, str, int, str, str | None, str | None, dict[str, str | None], str]:
    """Check one Bronze payload and return its stored columns."""
    if not isinstance(payload, dict):
        raise ValueError("full-text payload must be an object")
    url = payload.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("full-text payload has no url")
    state = payload.get("state")
    if state not in _STATES:
        raise ValueError(f"full-text payload has invalid state for {url}")
    attempts = payload.get("attempts")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise ValueError(f"full-text payload has invalid attempts for {url}")
    identifiers = payload.get("identifiers")
    if not isinstance(identifiers, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in identifiers.items()
    ):
        raise ValueError(f"full-text payload has invalid identifiers for {url}")
    if not isinstance(payload.get("provenance"), list):
        raise ValueError(f"full-text payload has invalid provenance for {url}")
    checked_at = payload.get("checked_at")
    if not isinstance(checked_at, str) or not checked_at:
        raise ValueError(f"full-text payload has no checked_at for {url}")
    fields = {name: _optional_str(payload, name, url) for name in _NULLABLE_FIELDS}
    archive_path = payload.get("archive_path")
    content_hash = payload.get("content_hash")
    if state == "fetched":
        if not isinstance(archive_path, str) or not archive_path:
            raise ValueError(f"fetched full-text payload has no archive path for {url}")
        if not isinstance(content_hash, str) or not content_hash:
            raise ValueError(f"fetched full-text payload has no content hash for {url}")
        _extension_for_content_type(fields["content_type"], url)
    else:
        if archive_path is not None and not isinstance(archive_path, str):
            raise ValueError(f"full-text payload has invalid archive path for {url}")
        if content_hash is not None and not isinstance(content_hash, str):
            raise ValueError(f"full-text payload has invalid content hash for {url}")
    identifiers_json = json.dumps(identifiers, sort_keys=True, ensure_ascii=False)
    return url, state, attempts, identifiers_json, archive_path, content_hash, fields, checked_at


def replay_fulltext(database: Database, payload: dict[str, Any], bronze_object_id: str) -> None:
    """Apply one Bronze payload to saved_fulltext without network access."""
    if not isinstance(bronze_object_id, str) or not bronze_object_id:
        raise ValueError("full-text bronze object id must be a non-empty string")
    url, state, attempts, identifiers_json, archive_path, content_hash, fields, checked_at = (
        _replay_fields(payload)
    )
    try:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT state FROM saved_fulltext WHERE url = ?", (url,)
            ).fetchone()
            current = row["state"] if row is not None else None
    except sqlite3.Error as error:
        raise RuntimeError(f"failed to read prior full-text state for {url}: {error}") from error
    if current == "fetched" and state != "fetched":
        return
    try:
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO saved_fulltext(
                    url, state, attempts, identifiers_json, provider, source_url,
                    final_url, content_type, license, version, archive_path,
                    content_hash, error, bronze_object_id, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    state=excluded.state,
                    attempts=excluded.attempts,
                    identifiers_json=excluded.identifiers_json,
                    provider=excluded.provider,
                    source_url=excluded.source_url,
                    final_url=excluded.final_url,
                    content_type=excluded.content_type,
                    license=excluded.license,
                    version=excluded.version,
                    archive_path=excluded.archive_path,
                    content_hash=excluded.content_hash,
                    error=excluded.error,
                    bronze_object_id=excluded.bronze_object_id,
                    checked_at=excluded.checked_at""",
                (
                    url,
                    state,
                    attempts,
                    identifiers_json,
                    fields["provider"],
                    fields["source_url"],
                    fields["final_url"],
                    fields["content_type"],
                    fields["license"],
                    fields["version"],
                    archive_path,
                    content_hash,
                    fields["error"],
                    bronze_object_id,
                    checked_at,
                ),
            )
    except sqlite3.Error as error:
        raise RuntimeError(f"failed to replay full-text state for {url}: {error}") from error
