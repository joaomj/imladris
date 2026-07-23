"""SQLite schema and connection helpers."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    counts_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);
CREATE TABLE IF NOT EXISTS run_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    occurred_at TEXT NOT NULL,
    stage TEXT NOT NULL,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bronze_objects (
    object_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    kind TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    manifest_path TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    pagination_cursor TEXT,
    payload_sha256 TEXT NOT NULL,
    source_ids_json TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authors (
    author_id TEXT PRIMARY KEY,
    username TEXT,
    name TEXT,
    raw_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    post_id TEXT PRIMARY KEY,
    author_id TEXT,
    username TEXT,
    created_at TEXT,
    url TEXT NOT NULL,
    text TEXT NOT NULL,
    note_text TEXT,
    article_body TEXT,
    article_json TEXT,
    content_state TEXT NOT NULL,
    language TEXT,
    public_metrics_json TEXT,
    attachments_json TEXT,
    current_content_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS post_versions (
    post_id TEXT NOT NULL REFERENCES posts(post_id),
    content_hash TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (post_id, content_hash)
);
CREATE TABLE IF NOT EXISTS post_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL REFERENCES posts(post_id),
    bronze_object_id TEXT NOT NULL REFERENCES bronze_objects(object_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    observed_at TEXT NOT NULL,
    UNIQUE(post_id, bronze_object_id)
);
CREATE TABLE IF NOT EXISTS folders (
    folder_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookmark_memberships (
    post_id TEXT NOT NULL REFERENCES posts(post_id),
    folder_id TEXT NOT NULL REFERENCES folders(folder_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (post_id, folder_id, run_id)
);
CREATE TABLE IF NOT EXISTS media (
    media_key TEXT PRIMARY KEY,
    post_id TEXT NOT NULL REFERENCES posts(post_id),
    media_type TEXT,
    source_url TEXT,
    archive_path TEXT,
    sha256 TEXT,
    status TEXT NOT NULL,
    error TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS references_to_posts (
    post_id TEXT NOT NULL REFERENCES posts(post_id),
    referenced_post_id TEXT NOT NULL,
    reference_type TEXT,
    PRIMARY KEY (post_id, referenced_post_id, reference_type)
);
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    post_id UNINDEXED,
    username,
    url,
    content
);
CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_post_observations_post ON post_observations(post_id);
CREATE INDEX IF NOT EXISTS idx_memberships_folder ON bookmark_memberships(folder_id);
"""


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(UTC).isoformat()


class Database:
    """Manage the local SQLite database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """Open a connection with the required SQLite settings."""
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        """Create the schema if needed."""
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            bronze_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(bronze_objects)")
            }
            if "context_json" not in bronze_columns:
                connection.execute(
                    "ALTER TABLE bronze_objects ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'"
                )
            post_columns = {row["name"] for row in connection.execute("PRAGMA table_info(posts)")}
            if "content_state" not in post_columns:
                connection.execute(
                    "ALTER TABLE posts ADD COLUMN content_state TEXT NOT NULL DEFAULT 'complete'"
                )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                ("schema_version", SCHEMA_VERSION),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield a transactional connection."""
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def set_checkpoint(self, key: str, value: Any) -> None:
        """Store a JSON checkpoint atomically."""
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO checkpoints(checkpoint_key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(checkpoint_key) DO UPDATE SET value=excluded.value,
                   updated_at=excluded.updated_at""",
                (key, json.dumps(value, sort_keys=True), utc_now()),
            )

    def get_checkpoint(self, key: str) -> Any | None:
        """Read a JSON checkpoint."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM checkpoints WHERE checkpoint_key = ?", (key,)
            ).fetchone()
        return json.loads(row["value"]) if row else None
