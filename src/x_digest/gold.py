"""Search, display, export, rebuild, and integrity verification."""

import gzip
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .db import Database
from .silver import SilverNormalizer


class GoldStore:
    """Provide local read and maintenance operations."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def status(self) -> dict[str, Any]:
        """Return local record counts and the latest run."""
        tables = ("bronze_objects", "posts", "media", "folders", "runs")
        with self.database.connect() as connection:
            counts = {
                table: connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()[
                    "count"
                ]
                for table in tables
            }
            latest = connection.execute(
                """SELECT run_id, status, started_at, completed_at, counts_json
                   FROM runs ORDER BY started_at DESC LIMIT 1"""
            ).fetchone()
        return {"counts": counts, "latest_run": dict(latest) if latest else None}

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search normalized text through SQLite FTS5."""
        with self.database.connect() as connection:
            try:
                rows = connection.execute(
                    """SELECT p.post_id, p.username, p.created_at, p.url, p.text,
                       p.article_body FROM posts_fts f JOIN posts p ON p.post_id=f.post_id
                       WHERE posts_fts MATCH ? ORDER BY p.created_at DESC LIMIT ?""",
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError as error:
                if "fts5" not in str(error).lower() and "syntax" not in str(error).lower():
                    raise
                rows = connection.execute(
                    """SELECT post_id, username, created_at, url, text, article_body FROM posts
                       WHERE text LIKE ? OR article_body LIKE ? ORDER BY created_at DESC LIMIT ?""",
                    (f"%{query}%", f"%{query}%", limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def show(self, post_id: str) -> dict[str, Any] | None:
        """Return one post and its local relationships."""
        with self.database.connect() as connection:
            post = connection.execute(
                "SELECT * FROM posts WHERE post_id = ?", (post_id,)
            ).fetchone()
            if not post:
                return None
            media = connection.execute(
                "SELECT * FROM media WHERE post_id = ?", (post_id,)
            ).fetchall()
            folders = connection.execute(
                """SELECT f.* FROM folders f JOIN bookmark_memberships m ON m.folder_id=f.folder_id
                   WHERE m.post_id=? ORDER BY f.name""",
                (post_id,),
            ).fetchall()
        result = dict(post)
        result["media"] = [dict(row) for row in media]
        result["folders"] = [dict(row) for row in folders]
        return result

    def export(self, output: Path, format_name: str) -> int:
        """Export all normalized posts as Markdown or JSON."""
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM posts ORDER BY created_at, post_id").fetchall()
        records = [dict(row) for row in rows]
        output.parent.mkdir(parents=True, exist_ok=True)
        if format_name == "json":
            output.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            blocks = []
            for record in records:
                body = record["article_body"] or record["note_text"] or record["text"]
                blocks.append(
                    f"# @{record['username'] or 'unknown'}\n\nSource: {record['url']}\n\n{body}\n"
                )
            output.write_text("\n---\n\n".join(blocks), encoding="utf-8")
        return len(records)

    def export_post(self, post_id: str, output: Path) -> None:
        """Export one normalized post as Markdown."""
        record = self.show(post_id)
        if record is None:
            raise ValueError(f"post not found: {post_id}")
        body = record["article_body"] or record["note_text"] or record["text"]
        markdown = (
            "---\n"
            f"post_id: {record['post_id']}\n"
            f"author: {record['username'] or 'unknown'}\n"
            f"created_at: {record['created_at'] or ''}\n"
            f"url: {record['url']}\n"
            f"content_state: {record['content_state']}\n"
            "---\n\n"
            f"# @{record['username'] or 'unknown'}\n\n"
            f"Source: {record['url']}\n\n"
            f"{body}\n"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")

    def verify(self, full: bool = False) -> dict[str, int]:
        """Verify Bronze payload hashes and, optionally, media files."""
        checked = 0
        failed = 0
        with self.database.connect() as connection:
            objects = connection.execute(
                "SELECT path, payload_sha256, manifest_path FROM bronze_objects"
            ).fetchall()
            media = connection.execute(
                "SELECT archive_path, sha256 FROM media WHERE archive_path IS NOT NULL"
            ).fetchall()
        for row in objects:
            checked += 1
            try:
                compressed = Path(row["path"]).read_bytes()
                payload = gzip.decompress(compressed)
                digest = hashlib.sha256(payload).hexdigest()
                if digest != row["payload_sha256"] or not Path(row["manifest_path"]).exists():
                    failed += 1
            except (OSError, gzip.BadGzipFile):
                failed += 1
        if full:
            for row in media:
                checked += 1
                try:
                    digest = hashlib.sha256(Path(row["archive_path"]).read_bytes()).hexdigest()
                    if digest != row["sha256"]:
                        failed += 1
                except OSError:
                    failed += 1
        return {"checked": checked, "failed": failed}

    def rebuild_silver(self, bronze_root: Path) -> dict[str, int]:
        """Rebuild normalized tables from immutable Bronze objects."""
        with self.database.transaction() as connection:
            for table in (
                "posts_fts",
                "references_to_posts",
                "media",
                "bookmark_memberships",
                "post_observations",
                "post_versions",
                "posts",
                "authors",
                "folders",
            ):
                connection.execute(f"DELETE FROM {table}")
        normalizer = SilverNormalizer(self.database)
        with self.database.connect() as connection:
            objects = connection.execute(
                """SELECT object_id, run_id, kind, path, context_json FROM bronze_objects
                   ORDER BY created_at, object_id"""
            ).fetchall()
        counts = {"objects": 0, "posts": 0, "folders": 0}
        for row in objects:
            path = Path(row["path"])
            if not path.resolve().is_relative_to(bronze_root.resolve()):
                continue
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                payload = json.load(stream)
            counts["objects"] += 1
            if row["kind"] == "folders":
                counts["folders"] += normalizer.apply_folders(row["run_id"], payload)
            else:
                context = json.loads(row["context_json"])
                counts["posts"] += normalizer.apply_posts(
                    row["run_id"], row["object_id"], payload, context.get("folder_id")
                )
        return counts
