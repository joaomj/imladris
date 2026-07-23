"""Immutable Bronze archive writer."""

import gzip
import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import Database, utc_now

BRONZE_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class BronzeRecord:
    """Reference to an immutable Bronze object."""

    object_id: str
    path: Path
    manifest_path: Path
    payload_sha256: str


@dataclass(frozen=True)
class BronzeWriteRequest:
    """Input for one immutable JSON object."""

    run_id: str
    kind: str
    payload: Any
    endpoint: str
    pagination_cursor: str | None
    source_ids: list[str]
    sequence: int
    context: dict[str, Any] | None = None


def _atomic_write(path: Path, data: bytes) -> None:
    """Write bytes and replace the destination atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        os.unlink(temporary_name)
        raise


class BronzeWriter:
    """Write append-only JSON and media objects."""

    def __init__(self, vault_path: Path, database: Database) -> None:
        self.root = vault_path / "bronze"
        self.database = database

    def _run_directory(self, run_id: str) -> Path:
        date_path = datetime.now(UTC).strftime("%Y/%m/%d")
        directory = self.root / date_path / run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def write_json(self, request: BronzeWriteRequest) -> BronzeRecord:
        """Write one immutable compressed JSON response and manifest."""
        serialized = json.dumps(
            request.payload, sort_keys=True, ensure_ascii=False, default=str
        ).encode()
        payload_hash = hashlib.sha256(serialized).hexdigest()
        object_id = str(uuid.uuid4())
        directory = self._run_directory(request.run_id)
        object_path = directory / f"{request.kind}-{request.sequence:04d}.json.gz"
        manifest_path = directory / f"{request.kind}-{request.sequence:04d}.manifest.json"
        compressed = gzip.compress(serialized, mtime=0)
        _atomic_write(object_path, compressed)
        manifest = {
            "object_id": object_id,
            "run_id": request.run_id,
            "kind": request.kind,
            "endpoint": request.endpoint,
            "pagination_cursor": request.pagination_cursor,
            "source_ids": request.source_ids,
            "fetched_at": utc_now(),
            "schema_version": BRONZE_SCHEMA_VERSION,
            "payload_sha256": payload_hash,
            "context": request.context or {},
            "path": str(object_path),
        }
        _atomic_write(manifest_path, json.dumps(manifest, indent=2, sort_keys=True).encode())
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO bronze_objects(
                    object_id, run_id, kind, path, manifest_path, endpoint,
                    pagination_cursor, payload_sha256, source_ids_json, context_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    object_id,
                    request.run_id,
                    request.kind,
                    str(object_path),
                    str(manifest_path),
                    request.endpoint,
                    request.pagination_cursor,
                    payload_hash,
                    json.dumps(request.source_ids),
                    json.dumps(request.context or {}, sort_keys=True),
                    utc_now(),
                ),
            )
        return BronzeRecord(object_id, object_path, manifest_path, payload_hash)

    def write_media(
        self,
        run_id: str,
        media_key: str,
        content: bytes,
        extension: str,
    ) -> tuple[Path, str]:
        """Write one immutable media file and return its path and hash."""
        content_hash = hashlib.sha256(content).hexdigest()
        safe_extension = extension.lstrip(".") or "bin"
        directory = self._run_directory(run_id) / "media"
        path = directory / f"{media_key}.{safe_extension}"
        if path.exists():
            existing_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if existing_hash != content_hash:
                raise FileExistsError(f"immutable media collision: {path}")
            return path, content_hash
        _atomic_write(path, content)
        return path, content_hash

    def write_run_manifest(self, run_id: str) -> Path:
        """Write the final run manifest without replacing an existing one."""
        directory = self._run_directory(run_id)
        path = directory / "manifest.json"
        if path.exists():
            return path
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT object_id, kind, path, payload_sha256, source_ids_json, context_json
                   FROM bronze_objects WHERE run_id = ? ORDER BY object_id""",
                (run_id,),
            ).fetchall()
        payload = {
            "run_id": run_id,
            "schema_version": BRONZE_SCHEMA_VERSION,
            "objects": [
                {
                    "object_id": row["object_id"],
                    "kind": row["kind"],
                    "path": row["path"],
                    "payload_sha256": row["payload_sha256"],
                    "source_ids": json.loads(row["source_ids_json"]),
                    "context": json.loads(row["context_json"]),
                }
                for row in rows
            ],
        }
        _atomic_write(path, json.dumps(payload, indent=2, sort_keys=True).encode())
        return path
