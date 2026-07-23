"""Silver-layer normalization and idempotent SQLite writes."""

import hashlib
import json
from typing import Any

from .db import Database, utc_now


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _text_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "plain_text", "body", "content", "value"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return None


def _post_url(username: str | None, post_id: str) -> str:
    return (
        f"https://x.com/{username}/status/{post_id}"
        if username
        else f"https://x.com/i/status/{post_id}"
    )


def _media_url(media: dict[str, Any]) -> str | None:
    direct_url = media.get("url")
    if isinstance(direct_url, str):
        return direct_url
    variants = media.get("variants", [])
    if isinstance(variants, list):
        urls = [
            item.get("url")
            for item in variants
            if isinstance(item, dict) and isinstance(item.get("url"), str)
        ]
        if urls:
            return urls[-1]
    preview_url = media.get("preview_image_url")
    return preview_url if isinstance(preview_url, str) else None


def _created_at(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value


class SilverNormalizer:
    """Normalize API payloads into canonical records."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def apply_folders(self, _run_id: str, payload: dict[str, Any]) -> int:
        """Upsert bookmark folders from an API response."""
        folders = payload.get("data", [])
        if not isinstance(folders, list):
            return 0
        now = utc_now()
        with self.database.transaction() as connection:
            for folder in folders:
                if not isinstance(folder, dict) or not folder.get("id"):
                    continue
                connection.execute(
                    """INSERT INTO folders(folder_id, name, raw_json, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(folder_id) DO UPDATE SET name=excluded.name,
                       raw_json=excluded.raw_json, last_seen_at=excluded.last_seen_at""",
                    (str(folder["id"]), str(folder.get("name", "")), _json(folder), now, now),
                )
        return len(folders)

    def apply_posts(
        self,
        run_id: str,
        bronze_object_id: str,
        payload: dict[str, Any],
        folder_id: str | None = None,
    ) -> int:
        """Normalize all posts in one Bronze response."""
        posts = payload.get("data", [])
        if isinstance(posts, dict):
            posts = [posts]
        includes = payload.get("includes", {})
        users = includes.get("users", []) if isinstance(includes, dict) else []
        authors = {
            str(user.get("id")): user for user in users if isinstance(user, dict) and user.get("id")
        }
        now = utc_now()
        count = 0
        if not isinstance(posts, list):
            return count
        with self.database.transaction() as connection:
            for raw_post in posts:
                if not isinstance(raw_post, dict) or not raw_post.get("id"):
                    continue
                media_by_key = {
                    str(item.get("media_key")): item
                    for item in (includes.get("media", []) if isinstance(includes, dict) else [])
                    if isinstance(item, dict) and item.get("media_key")
                }
                normalized = self._normalize_post(raw_post, authors, media_by_key)
                post_id = normalized["post_id"]
                author = normalized["author"]
                if author:
                    connection.execute(
                        """INSERT INTO authors(author_id, username, name, raw_json,
                           first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT(author_id) DO UPDATE SET username=excluded.username,
                           name=excluded.name, raw_json=excluded.raw_json,
                           last_seen_at=excluded.last_seen_at""",
                        (
                            author["id"],
                            author.get("username"),
                            author.get("name"),
                            _json(author),
                            now,
                            now,
                        ),
                    )
                connection.execute(
                    """INSERT INTO posts(post_id, author_id, username, created_at, url, text,
                       note_text, article_body, article_json, content_state, language,
                       public_metrics_json,
                       attachments_json, current_content_hash, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(post_id) DO UPDATE SET author_id=excluded.author_id,
                       username=excluded.username, created_at=excluded.created_at,
                       url=excluded.url, text=excluded.text, note_text=excluded.note_text,
                       article_body=excluded.article_body, article_json=excluded.article_json,
                       content_state=excluded.content_state,
                       language=excluded.language, public_metrics_json=excluded.public_metrics_json,
                       attachments_json=excluded.attachments_json,
                       current_content_hash=excluded.current_content_hash,
                       last_seen_at=excluded.last_seen_at""",
                    (
                        post_id,
                        normalized["author_id"],
                        normalized["username"],
                        normalized["created_at"],
                        normalized["url"],
                        normalized["text"],
                        normalized["note_text"],
                        normalized["article_body"],
                        normalized["article_json"],
                        normalized["content_state"],
                        normalized["language"],
                        normalized["public_metrics_json"],
                        normalized["attachments_json"],
                        normalized["content_hash"],
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """INSERT OR IGNORE INTO post_versions(post_id, content_hash,
                       normalized_json, first_seen_at) VALUES (?, ?, ?, ?)""",
                    (post_id, normalized["content_hash"], _json(normalized), now),
                )
                connection.execute(
                    """INSERT OR IGNORE INTO post_observations(post_id, bronze_object_id,
                       run_id, observed_at) VALUES (?, ?, ?, ?)""",
                    (post_id, bronze_object_id, run_id, now),
                )
                if folder_id:
                    connection.execute(
                        """INSERT OR IGNORE INTO bookmark_memberships(post_id, folder_id,
                           run_id, observed_at) VALUES (?, ?, ?, ?)""",
                        (post_id, folder_id, run_id, now),
                    )
                for reference in normalized["references"]:
                    connection.execute(
                        """INSERT OR IGNORE INTO references_to_posts(post_id,
                           referenced_post_id, reference_type) VALUES (?, ?, ?)""",
                        (post_id, reference["id"], reference.get("type")),
                    )
                for media in normalized["media"]:
                    connection.execute(
                        """INSERT INTO media(media_key, post_id, media_type, source_url,
                           archive_path, sha256, status, error, first_seen_at, last_seen_at)
                           VALUES (?, ?, ?, ?, NULL, NULL, 'pending', NULL, ?, ?)
                           ON CONFLICT(media_key) DO UPDATE SET source_url=excluded.source_url,
                           media_type=excluded.media_type, last_seen_at=excluded.last_seen_at""",
                        (
                            media["media_key"],
                            post_id,
                            media.get("type"),
                            _media_url(media),
                            now,
                            now,
                        ),
                    )
                self._refresh_fts(connection, normalized)
                count += 1
        return count

    def _normalize_post(
        self,
        raw: dict[str, Any],
        authors: dict[str, dict[str, Any]],
        media_by_key: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        post_id = str(raw["id"])
        author_id = str(raw["author_id"]) if raw.get("author_id") else None
        author = authors.get(author_id or "")
        username = str(author["username"]) if author and author.get("username") else None
        note_text = _text_value(raw.get("note_tweet"))
        article = raw.get("article") if isinstance(raw.get("article"), dict) else None
        article_body = _text_value(article)
        content_state = "article_metadata_only" if article and not article_body else "complete"
        text = str(raw.get("text", ""))
        references = [
            item
            for item in (raw.get("referenced_tweets") or [])
            if isinstance(item, dict) and item.get("id")
        ]
        attachments = raw.get("attachments") if isinstance(raw.get("attachments"), dict) else None
        media = (
            [
                media_by_key[key]
                for key in attachments.get("media_keys", [])
                if isinstance(key, str) and key in media_by_key
            ]
            if attachments
            else []
        )
        normalized = {
            "post_id": post_id,
            "author": author,
            "author_id": author_id,
            "username": username,
            "created_at": _created_at(raw.get("created_at")),
            "url": _post_url(username, post_id),
            "text": text,
            "note_text": note_text,
            "article_body": article_body,
            "article_json": _json(article) if article else None,
            "content_state": content_state,
            "language": raw.get("lang"),
            "public_metrics_json": (
                _json(raw.get("public_metrics")) if raw.get("public_metrics") else None
            ),
            "attachments_json": _json(attachments) if attachments else None,
            "references": references,
            "media": media,
        }
        normalized["content_hash"] = hashlib.sha256(_json(normalized).encode()).hexdigest()
        return normalized

    @staticmethod
    def _refresh_fts(connection: Any, normalized: dict[str, Any]) -> None:
        content = "\n".join(
            value
            for value in (
                normalized["text"],
                normalized["note_text"],
                normalized["article_body"],
            )
            if value
        )
        connection.execute("DELETE FROM posts_fts WHERE post_id = ?", (normalized["post_id"],))
        connection.execute(
            "INSERT INTO posts_fts(post_id, username, url, content) VALUES (?, ?, ?, ?)",
            (normalized["post_id"], normalized["username"], normalized["url"], content),
        )
