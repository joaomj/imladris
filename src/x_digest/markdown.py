"""One Markdown file per archived post, generated once and never rewritten."""

import os
import re
from pathlib import Path

from .config import Settings
from .db import Database
from .logging_setup import JsonlLogger

IMAGE_TYPES = {"photo"}
UNSAFE_NAME = re.compile(r'[/\\:*?"<>|\x00]')


class MarkdownWriter:
    """Write a Markdown file for each new post, following the bookmark folder layout."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        log: JsonlLogger | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.log = log
        self.correlation_id = correlation_id
        self.posts_dir = settings.vault_path / "markdown" / "posts"
        self.folders_dir = settings.vault_path / "markdown" / "folders"

    def write_new(self, run_id: str) -> dict[str, int]:
        """Create Markdown files for posts that do not have one yet."""
        counts = {"written": 0, "skipped": 0, "no_content": 0}
        self.posts_dir.mkdir(parents=True, exist_ok=True)
        with self.database.connect() as connection:
            post_rows = connection.execute(
                """SELECT post_id, username, created_at, url, content_state,
                          text, note_text, article_body
                   FROM posts ORDER BY created_at, post_id"""
            ).fetchall()
            media_rows = connection.execute(
                """SELECT post_id, media_key, media_type, archive_path FROM media
                   WHERE archive_path IS NOT NULL"""
            ).fetchall()
            membership_rows = connection.execute(
                """SELECT m.post_id, m.folder_id, f.name
                   FROM bookmark_memberships m
                   JOIN folders f ON f.folder_id = m.folder_id"""
            ).fetchall()
        media_by_post: dict[str, list[dict[str, object]]] = {}
        for row in media_rows:
            media_by_post.setdefault(str(row["post_id"]), []).append(dict(row))
        folder_dirs = self._folder_dirs(membership_rows)
        memberships: dict[str, list[dict[str, object]]] = {}
        for row in membership_rows:
            memberships.setdefault(str(row["post_id"]), []).append(dict(row))
        for row in post_rows:
            post = dict(row)
            post_id = str(post["post_id"])
            media = media_by_post.get(post_id, [])
            body = post["article_body"] or post["note_text"] or post["text"]
            if not body and not media:
                counts["no_content"] += 1
                continue
            targets = self._targets(post_id, memberships.get(post_id, []), folder_dirs)
            for target in targets:
                if target.exists():
                    counts["skipped"] += 1
                    continue
                self._write(target, post, media)
                counts["written"] += 1
                if self.log:
                    self.log.emit(
                        self.correlation_id or run_id,
                        "markdown_written",
                        "debug",
                        post_id=post_id,
                    )
        if self.log:
            self.log.emit(
                self.correlation_id or run_id,
                "markdown_phase",
                "info",
                **counts,
            )
        return counts

    def _folder_dirs(self, membership_rows: list) -> dict[str, Path]:
        """Map each folder ID to its output directory, disambiguating names."""
        folder_names: dict[str, str] = {}
        for row in membership_rows:
            folder_names.setdefault(str(row["folder_id"]), str(row["name"] or ""))
        dirs_by_id = {
            folder_id: self._sanitize(name)
            for folder_id, name in folder_names.items()
        }
        duplicates: set[str] = set()
        for name in dirs_by_id.values():
            if list(dirs_by_id.values()).count(name) > 1:
                duplicates.add(name)
        result: dict[str, Path] = {}
        for folder_id, name in dirs_by_id.items():
            directory = f"{name}-{folder_id}" if name in duplicates else name
            result[folder_id] = self.folders_dir / directory
        return result

    def _sanitize(self, name: str) -> str:
        cleaned = UNSAFE_NAME.sub("_", name).strip()
        return cleaned if cleaned not in {"", ".", ".."} else "folder"

    def _targets(
        self,
        post_id: str,
        post_memberships: list[dict[str, object]],
        folder_dirs: dict[str, Path],
    ) -> list[Path]:
        if not post_memberships:
            return [self.posts_dir / f"{post_id}.md"]
        targets: list[Path] = []
        for row in post_memberships:
            target = folder_dirs[str(row["folder_id"])] / f"{post_id}.md"
            if target not in targets:
                targets.append(target)
        return targets

    def _write(
        self,
        target: Path,
        post: dict[str, object],
        media: list[dict[str, object]],
    ) -> None:
        username = str(post.get("username") or "unknown")
        body = str(post["article_body"] or post["note_text"] or post["text"])
        url = str(post.get("url") or "")
        blocks = [
            "---",
            f"post_id: {post['post_id']}",
            f"author: {username}",
            f"created_at: {post.get('created_at') or ''}",
            f"url: {url}",
            f"content_state: {post.get('content_state') or ''}",
            "---",
            "",
            f"# @{username}",
            "",
        ]
        if url:
            blocks.extend([f"Source: {url}", ""])
        blocks.append(body)
        if media:
            blocks.extend(["", "## Media", ""])
            for item in media:
                blocks.append(self._media_line(item, target))
        tmp = target.with_suffix(".md.tmp")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text("\n".join(blocks) + "\n", encoding="utf-8")
        os.replace(tmp, target)

    def _media_line(self, item: dict[str, object], target: Path) -> str:
        media_key = str(item["media_key"])
        media_type = str(item.get("media_type") or "")
        media_path = str(item["archive_path"] or "")
        stored = Path(media_path)
        if not stored.is_absolute():
            stored = self.settings.vault_path / stored
        reference = os.path.relpath(stored, target.parent)
        if media_type in IMAGE_TYPES:
            return f"![{media_key}]({reference})"
        return f"[{media_type or 'media'}]({reference})"
