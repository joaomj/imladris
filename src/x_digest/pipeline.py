"""ETL orchestration for bookmarks and one-post probes."""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .auth import authenticated_client
from .bronze import BronzeWriter, BronzeWriteRequest
from .config import (
    AUTO_IGNORE_ACCOUNTS_KEY,
    Settings,
    effective_ignore_accounts,
    filter_ignored_posts,
    folder_is_ignored,
)
from .db import Database, utc_now
from .digest_service import DigestContext, DigestDependencies, DigestService
from .lock import ProcessLock
from .logging_setup import JsonlLogger
from .markdown import MarkdownWriter
from .media import MediaDownloader
from .paths import resolve_stored_path
from .silver import SilverNormalizer
from .x_api import MAX_POST_IDS_PER_REQUEST, XApi

POST_URL = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/(\d+)(?:[/?#].*)?$")


@dataclass(frozen=True)
class FolderSyncOptions:
    """Options controlling folder content hydration."""

    ignore_folders: list[str]
    ignore_accounts: list[str]
    full: bool


@dataclass
class SyncRequest:
    """Inputs controlling one bookmark synchronization."""

    max_pages: int | None = None
    dry_run: bool = False
    ignore_folders: list[str] | None = None
    full: bool = False
    ignore_accounts: list[str] | None = None


@dataclass
class SyncRun:
    """Live objects needed while a synchronization is running."""

    api: Any
    user_id: str
    run_id: str
    counts: dict[str, int]
    request: SyncRequest


def _source_ids(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data") or []
    if isinstance(data, dict):
        data = [data]
    return [str(item["id"]) for item in data if isinstance(item, dict) and item.get("id")]


def _next_token(payload: dict[str, Any]) -> str | None:
    meta = payload.get("meta")
    return str(meta["next_token"]) if isinstance(meta, dict) and meta.get("next_token") else None


class Pipeline:
    """Run the Bronze-to-Silver pipeline."""

    def __init__(
        self,
        settings: Settings,
        api: Any | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.api = api
        self.correlation_id = correlation_id
        self.database = Database(settings.database_path)
        self.database.initialize()
        self.bronze = BronzeWriter(settings.vault_path, self.database)
        self.silver = SilverNormalizer(self.database)
        self.log = JsonlLogger(
            settings.log_path, settings.log_level, settings.log_max_bytes, settings.log_backups
        )

    def _start_run(self) -> str:
        run_id = self.correlation_id or str(uuid.uuid4())
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO runs(run_id, started_at, status) VALUES (?, ?, 'running')",
                (run_id, utc_now()),
            )
        self.log.begin_run(run_id)
        return run_id

    def _event(
        self, run_id: str, stage: str, event: str, level: str = "info", **details: Any
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO run_events(run_id, occurred_at, stage, level, event, details_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, utc_now(), stage, level, event, json.dumps(details)),
            )
        self.log.emit(run_id, event, level, stage=stage, **details)

    def _finish(
        self, run_id: str, status: str, counts: dict[str, int], error: str | None = None
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET completed_at=?, status=?, counts_json=?, error=? WHERE run_id=?",
                (utc_now(), status, json.dumps(counts), error, run_id),
            )
        self.log.end_run()

    def _record_usage(self, run_id: str, api: Any, counts: dict[str, Any]) -> None:
        """Record measured X API usage for one run."""
        tracker = getattr(api, "tracker", None)
        if tracker is None:
            return
        usage = {"requests": tracker.summary(), "retries": tracker.retries()}
        counts["api_requests"] = usage["requests"]
        counts["api_retries"] = usage["retries"]
        self._event(run_id, "pipeline", "api_usage", level="info", **usage)

    def _finalize_unbounded_sync(self, sync_run: SyncRun) -> None:
        """Run folder sync, media, Markdown, and digest for a full sync."""
        active_accounts = list(sync_run.request.ignore_accounts or [])
        options = FolderSyncOptions(
            sync_run.request.ignore_folders or [],
            active_accounts,
            sync_run.request.full,
        )
        self._sync_folders_if_due(
            sync_run.api,
            sync_run.user_id,
            sync_run.run_id,
            sync_run.counts,
            options,
        )
        self._purge_blocked_posts(sync_run.run_id, active_accounts, sync_run.counts)
        self._archive_media_and_markdown(sync_run.run_id, sync_run.counts)
        self._send_digest(sync_run.run_id, sync_run.counts)

    def _send_digest(self, run_id: str, counts: dict[str, int]) -> None:
        """Deliver one pending digest batch without failing the archive run."""
        context = DigestContext(
            settings=self.settings,
            database=self.database,
            log=self.log,
            correlation_id=run_id,
            dependencies=DigestDependencies(
                emit=lambda event, level="info", **details: self._event(
                    run_id, "digest", event, level, **details
                )
            ),
        )
        DigestService(context).deliver(run_id, counts)

    def _archive_media_and_markdown(self, run_id: str, counts: dict[str, int]) -> None:
        """Download pending media, then write Markdown files for new posts."""
        media_counts = MediaDownloader(
            self.settings, self.database, self.bronze, self.log, run_id
        ).download_pending(run_id)
        counts.update({f"media_{key}": value for key, value in media_counts.items()})
        markdown_counts = MarkdownWriter(self.settings, self.database, self.log, run_id).write_new(
            run_id
        )
        counts.update({f"markdown_{key}": value for key, value in markdown_counts.items()})

    def _sync_folders(
        self,
        api: Any,
        user_id: str,
        run_id: str,
        counts: dict[str, int],
        options: FolderSyncOptions,
    ) -> None:
        """Archive folders and hydrate their post IDs in batches."""
        ignore_folders = options.ignore_folders
        active_accounts = options.ignore_accounts
        full = options.full
        for folder_payload in api.folders(user_id):
            counts["folder_pages"] += 1
            self.bronze.write_json(
                BronzeWriteRequest(
                    run_id,
                    "folders",
                    folder_payload,
                    "/2/users/{id}/bookmark_folders",
                    None,
                    _source_ids(folder_payload),
                    counts["folder_pages"],
                )
            )
            self.silver.apply_folders(run_id, folder_payload)
            for folder in folder_payload.get("data") or []:
                if not isinstance(folder, dict) or not folder.get("id"):
                    continue
                folder_id = str(folder["id"])
                if self._is_ignored_folder(folder, ignore_folders):
                    counts["folders_ignored"] += 1
                    self._event(
                        run_id,
                        "extract",
                        "folder_ignored",
                        folder_id=folder_id,
                        folder_name=str(folder.get("name", "")),
                    )
                    counts["accounts_auto_blocked"] += self._learn_folder_authors(
                        api, user_id, run_id, folder_id, active_accounts
                    )
                    continue
                folder_posts = api.folder_posts(user_id, folder_id)
                folder_post_ids = _source_ids(folder_posts)
                counts["folder_posts"] += len(folder_post_ids)
                folder_record = self.bronze.write_json(
                    BronzeWriteRequest(
                        run_id,
                        "folder-posts",
                        folder_posts,
                        "/2/users/{id}/bookmarks/folders/{folder_id}",
                        None,
                        folder_post_ids,
                        counts["folder_posts"],
                        {"folder_id": folder_id},
                    )
                )
                self.silver.apply_posts(run_id, folder_record.object_id, folder_posts, folder_id)
                if not full:
                    complete_ids = self.database.complete_post_ids(folder_post_ids)
                    to_fetch = [
                        post_id for post_id in folder_post_ids if post_id not in complete_ids
                    ]
                    if not to_fetch:
                        self._event(
                            run_id,
                            "extract",
                            "folder_hydration_skipped",
                            "debug",
                            folder_id=folder_id,
                            posts=len(folder_post_ids),
                        )
                else:
                    to_fetch = folder_post_ids
                for offset in range(0, len(to_fetch), MAX_POST_IDS_PER_REQUEST):
                    batch_ids = to_fetch[offset : offset + MAX_POST_IDS_PER_REQUEST]
                    content_payload = api.posts(batch_ids)
                    counts["folder_content_batches"] += 1
                    visible_payload, ignored = filter_ignored_posts(
                        content_payload, active_accounts
                    )
                    counts["posts_ignored"] += ignored
                    content_record = self.bronze.write_json(
                        BronzeWriteRequest(
                            run_id,
                            "folder-post-contents",
                            content_payload,
                            "/2/tweets",
                            None,
                            _source_ids(content_payload),
                            counts["folder_content_batches"],
                            {"folder_id": folder_id},
                        )
                    )
                    self.silver.apply_posts(
                        run_id, content_record.object_id, visible_payload, folder_id
                    )

    def _auto_blocked_author_ids(self) -> list[str]:
        """Return author IDs previously learned from ignored folders."""
        value = self.database.get_checkpoint(AUTO_IGNORE_ACCOUNTS_KEY)
        if isinstance(value, dict) and isinstance(value.get("author_ids"), list):
            return [str(entry) for entry in value["author_ids"] if str(entry).strip()]
        return []

    def _authors_of_posts(self, post_ids: list[str]) -> dict[str, str]:
        """Map known post IDs to their author IDs."""
        if not post_ids:
            return {}
        placeholders = ",".join("?" * len(post_ids))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT post_id, author_id FROM posts WHERE post_id IN ({placeholders})"
                " AND author_id IS NOT NULL",
                post_ids,
            ).fetchall()
        return {str(row["post_id"]): str(row["author_id"]) for row in rows}

    def _learn_folder_authors(
        self,
        api: Any,
        user_id: str,
        run_id: str,
        folder_id: str,
        active_accounts: list[str],
    ) -> int:
        """Block every author currently listed in an ignored folder.

        Membership is the signal: any author with a post in the folder joins
        the run's ignore set and is persisted for future runs. The lookup only
        attributes authors, it never archives content, and a lookup failure
        keeps the configured list working. Returns newly blocked authors.
        """
        try:
            folder_post_ids = _source_ids(api.folder_posts(user_id, folder_id))
        except Exception as error:
            self._event(
                run_id,
                "extract",
                "ignored_folder_lookup_failed",
                level="warning",
                folder_id=folder_id,
                error=type(error).__name__,
            )
            return 0
        if not folder_post_ids:
            return 0
        authors = self._authors_of_posts(folder_post_ids)
        unknown = [post_id for post_id in folder_post_ids if post_id not in authors]
        for offset in range(0, len(unknown), MAX_POST_IDS_PER_REQUEST):
            batch = unknown[offset : offset + MAX_POST_IDS_PER_REQUEST]
            try:
                content = api.posts(batch)
            except Exception as error:
                self._event(
                    run_id,
                    "extract",
                    "ignored_folder_lookup_failed",
                    level="warning",
                    folder_id=folder_id,
                    error=type(error).__name__,
                )
                continue
            data = content.get("data")
            if isinstance(data, dict):
                items: list[Any] = [data]
            elif isinstance(data, list):
                items = data
            else:
                items = []
            for item in items:
                if isinstance(item, dict) and item.get("id") and item.get("author_id"):
                    authors[str(item["id"])] = str(item["author_id"])
        blocked = {author_id for author_id in authors.values() if author_id}
        fresh = sorted(author_id for author_id in blocked if author_id not in active_accounts)
        if not fresh:
            return 0
        active_accounts.extend(fresh)
        self.database.set_checkpoint(
            AUTO_IGNORE_ACCOUNTS_KEY,
            {"author_ids": sorted(set(self._auto_blocked_author_ids()) | set(fresh))},
        )
        self._event(
            run_id,
            "extract",
            "ignored_folder_authors_blocked",
            folder_id=folder_id,
            authors=len(fresh),
        )
        return len(fresh)

    def _purge_blocked_posts(
        self, run_id: str, active_accounts: list[str], counts: dict[str, int]
    ) -> None:
        """Remove this run's Silver rows for blocked authors before delivery."""
        blocked = {entry for entry in active_accounts if entry.isdigit()}
        if not blocked:
            return
        placeholders = ",".join("?" * len(blocked))
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT po.post_id AS post_id FROM post_observations po"
                " JOIN posts p ON p.post_id = po.post_id WHERE po.run_id = ?"
                f" AND p.author_id IN ({placeholders})",
                [run_id, *blocked],
            ).fetchall()
        post_ids = [str(row["post_id"]) for row in rows]
        if not post_ids:
            return
        purged = self._delete_posts(run_id, post_ids)
        counts["posts_purged"] += purged
        self._event(run_id, "extract", "blocked_posts_purged", posts=purged)

    def _delete_posts(self, run_id: str, post_ids: list[str]) -> int:
        """Delete Silver rows and local files for posts, returning the count."""
        if not post_ids:
            return 0
        placeholders = ",".join("?" * len(post_ids))
        with self.database.connect() as connection:
            media_paths = [
                str(row["archive_path"])
                for row in connection.execute(
                    f"SELECT archive_path FROM media WHERE post_id IN ({placeholders})"
                    " AND archive_path IS NOT NULL",
                    post_ids,
                ).fetchall()
            ]
        with self.database.transaction() as connection:
            for table in (
                "bookmark_memberships",
                "media",
                "post_observations",
                "post_versions",
                "references_to_posts",
                "posts_fts",
                "posts",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE post_id IN ({placeholders})", post_ids
                )
        failures = 0
        vault = self.settings.vault_path
        for relative in media_paths:
            try:
                resolve_stored_path(vault, relative).unlink(missing_ok=True)
            except OSError:
                failures += 1
        posts_dir = vault / "markdown" / "posts"
        folders_dir = vault / "markdown" / "folders"
        for post_id in post_ids:
            try:
                (posts_dir / f"{post_id}.md").unlink(missing_ok=True)
                if folders_dir.exists():
                    for target in folders_dir.glob(f"*/{post_id}.md"):
                        target.unlink(missing_ok=True)
            except OSError:
                failures += 1
        if failures:
            self._event(
                run_id,
                "extract",
                "blocked_post_file_cleanup_failed",
                level="warning",
                files=failures,
            )
        return len(post_ids)

    @staticmethod
    def _is_ignored_folder(folder: dict[str, Any], ignore_folders: list[str]) -> bool:
        """Return True when a folder matches an ignored name or ID."""
        return folder_is_ignored(str(folder["id"]), str(folder.get("name", "")), ignore_folders)

    def _folders_due(self, user_id: str, full: bool) -> bool:
        """Return True when folder content must be re-read for this run."""
        if full or self.settings.folder_sync_days == 0:
            return True
        checkpoint = self.database.get_checkpoint(f"folders:{user_id}")
        synced_at = checkpoint.get("synced_at") if isinstance(checkpoint, dict) else None
        if not synced_at:
            return True
        try:
            last_sync = datetime.fromisoformat(synced_at)
        except ValueError:
            return True
        if last_sync.tzinfo is None:
            last_sync = last_sync.replace(tzinfo=UTC)
        return datetime.now(UTC) - last_sync >= timedelta(days=self.settings.folder_sync_days)

    def _sync_folders_if_due(
        self,
        api: Any,
        user_id: str,
        run_id: str,
        counts: dict[str, int],
        options: FolderSyncOptions,
    ) -> None:
        """Read folder content when the configured interval has elapsed."""
        if self._folders_due(user_id, options.full):
            self._sync_folders(api, user_id, run_id, counts, options)
            self.database.set_checkpoint(f"folders:{user_id}", {"synced_at": utc_now()})
        else:
            counts["folders_skipped"] = 1
            self._event(run_id, "pipeline", "folders_skipped", reason="weekly_interval")

    def sync(
        self,
        max_pages: int | None = None,
        dry_run: bool = False,
        ignore_folders: list[str] | None = None,
        full: bool = False,
        ignore_accounts: list[str] | None = None,
    ) -> dict[str, int | str]:
        """Fetch bookmarks and folders, with an optional page bound."""
        if ignore_folders is None:
            ignore_folders = self.settings.ignore_folders
        if ignore_accounts is None:
            ignore_accounts = self.settings.ignore_accounts
        ignore_accounts = effective_ignore_accounts(
            ignore_accounts, self._auto_blocked_author_ids()
        )
        run_id = self._start_run()
        counts = {
            "bookmark_pages": 0,
            "posts": 0,
            "posts_ignored": 0,
            "posts_purged": 0,
            "accounts_auto_blocked": 0,
            "folder_pages": 0,
            "folder_posts": 0,
            "folder_content_batches": 0,
            "folders_ignored": 0,
            "folders_skipped": 0,
            "stopped_early": 0,
        }
        api: Any = None
        try:
            with ProcessLock(self.settings.lock_path):
                api = self.api or XApi(
                    authenticated_client(self.settings), self.settings, self.log, run_id
                )
                user_payload = api.current_user()
                user_data = user_payload.get("data", {})
                user_id = str(user_data["id"])
                self._event(run_id, "extract", "authenticated", user_id=user_id)
                cursor_value = self.database.get_checkpoint(f"bookmarks:{user_id}")
                cursor = cursor_value.get("next_token") if isinstance(cursor_value, dict) else None
                request = SyncRequest(max_pages, dry_run, ignore_folders, full, ignore_accounts)
                sync_run = SyncRun(api, user_id, run_id, counts, request)
                self._read_bookmark_pages(sync_run, cursor)
                self._finish_sync_stages(sync_run)
                return {"run_id": run_id, **counts}
        except Exception as error:
            if api is not None:
                self._record_usage(run_id, api, counts)
            self._finish(run_id, "failed", counts, str(error))
            self._event(run_id, "pipeline", "failed", "error", error=str(error))
            raise

    def _read_bookmark_pages(self, sync_run: SyncRun, cursor: str | None) -> None:
        """Fetch bookmark pages and archive new posts."""
        request = sync_run.request
        counts = sync_run.counts
        while request.max_pages is None or counts["bookmark_pages"] < request.max_pages:
            payload = sync_run.api.bookmark_page(sync_run.user_id, cursor)
            counts["bookmark_pages"] += 1
            if not request.dry_run:
                # Ignored accounts never reach Silver, so they must not count
                # as known posts for the incremental stop check.
                visible, _ = filter_ignored_posts(
                    payload, request.ignore_accounts or []
                )
                post_ids = _source_ids(visible)
                if not request.full and post_ids and self._all_posts_known(post_ids):
                    counts["stopped_early"] = 1
                    self._event(
                        sync_run.run_id,
                        "extract",
                        "stopped_early",
                        page=counts["bookmark_pages"],
                        archived_posts=len(post_ids),
                    )
                    break
                counts["posts"] += self._archive_bookmark_page(sync_run, cursor, payload)
            cursor = _next_token(payload)
            if not cursor:
                break

    def _all_posts_known(self, post_ids: list[str]) -> bool:
        """Return True when every post ID is already archived."""
        return self.database.known_post_ids(post_ids) == set(post_ids)

    def _archive_bookmark_page(
        self, sync_run: SyncRun, cursor: str | None, payload: dict[str, Any]
    ) -> int:
        """Archive one bookmark page and persist its pagination cursor."""
        post_ids = _source_ids(payload)
        record = self.bronze.write_json(
            BronzeWriteRequest(
                sync_run.run_id,
                "bookmarks-page",
                payload,
                "/2/users/{id}/bookmarks",
                cursor,
                post_ids,
                sync_run.counts["bookmark_pages"],
            )
        )
        visible, ignored = filter_ignored_posts(
            payload, sync_run.request.ignore_accounts or []
        )
        sync_run.counts["posts_ignored"] += ignored
        archived = self.silver.apply_posts(sync_run.run_id, record.object_id, visible)
        self.database.set_checkpoint(
            f"bookmarks:{sync_run.user_id}", {"next_token": _next_token(payload)}
        )
        return archived

    def _finish_sync_stages(self, sync_run: SyncRun) -> None:
        """Run post-page stages and close out the successful sync."""
        request = sync_run.request
        if not request.dry_run and request.max_pages is None:
            self._finalize_unbounded_sync(sync_run)
        elif not request.dry_run:
            self._purge_blocked_posts(
                sync_run.run_id, request.ignore_accounts or [], sync_run.counts
            )
            self._event(
                sync_run.run_id, "pipeline", "folders_skipped", reason="bounded_sync"
            )
            self._event(sync_run.run_id, "digest", "digest_skipped", reason="bounded_sync")
        if not request.dry_run:
            self.bronze.write_run_manifest(sync_run.run_id)
        else:
            self._event(sync_run.run_id, "digest", "digest_skipped", reason="dry_run")
        self._record_usage(sync_run.run_id, sync_run.api, sync_run.counts)
        self._finish(sync_run.run_id, "success", sync_run.counts)
        self._event(sync_run.run_id, "pipeline", "completed", counts=sync_run.counts)

    def probe_post(self, url: str) -> dict[str, str | int]:
        """Fetch and archive exactly one canonical post URL."""
        match = POST_URL.match(url)
        if not match:
            raise ValueError("URL must match https://x.com/<username>/status/<id>")
        run_id = self._start_run()
        counts: dict[str, Any] = {"posts": 0}
        try:
            with ProcessLock(self.settings.lock_path):
                api = XApi(authenticated_client(self.settings), self.settings, self.log, run_id)
                payload = api.post(match.group(1))
                record = self.bronze.write_json(
                    BronzeWriteRequest(
                        run_id,
                        "probe-post",
                        payload,
                        "/2/tweets/{id}",
                        None,
                        _source_ids(payload),
                        1,
                    )
                )
                counts["posts"] = self.silver.apply_posts(run_id, record.object_id, payload)
                self._archive_media_and_markdown(run_id, counts)
                self.bronze.write_run_manifest(run_id)
                self._record_usage(run_id, api, counts)
                self._finish(run_id, "success", counts)
                self._event(
                    run_id, "probe", "completed", post_id=match.group(1), posts=counts["posts"]
                )
                return {"run_id": run_id, "post_id": match.group(1), "posts": counts["posts"]}
        except Exception as error:
            self._finish(run_id, "failed", counts, str(error))
            self._event(run_id, "probe", "failed", "error", error=str(error))
            raise

    def probe_bookmarks(self, max_results: int = 20) -> dict[str, str | int]:
        """Fetch one bookmark page and exactly one ordinary post and Article."""
        if not 1 <= max_results <= self.settings.max_results_per_page:
            raise ValueError(
                f"max_results must be between 1 and {self.settings.max_results_per_page}"
            )
        run_id = self._start_run()
        counts: dict[str, Any] = {"posts": 0}
        try:
            with ProcessLock(self.settings.lock_path):
                api = XApi(authenticated_client(self.settings), self.settings, self.log, run_id)
                user_payload = api.current_user()
                user_id = str(user_payload["data"]["id"])
                bookmark_payload = api.bookmark_page(user_id, None, max_results)
                page_record = self.bronze.write_json(
                    BronzeWriteRequest(
                        run_id,
                        "bookmark-probe-page",
                        bookmark_payload,
                        "/2/users/{id}/bookmarks",
                        None,
                        _source_ids(bookmark_payload),
                        1,
                    )
                )
                selected = self._select_probe_posts(bookmark_payload)
                result: dict[str, str | int] = {
                    "run_id": run_id,
                    "bookmark_page_object_id": page_record.object_id,
                    "bookmark_page_items": len(_source_ids(bookmark_payload)),
                }
                for sequence, (kind, post_id) in enumerate(selected.items(), start=1):
                    payload = api.post(post_id)
                    record = self.bronze.write_json(
                        BronzeWriteRequest(
                            run_id,
                            "bookmark-probe-post",
                            payload,
                            "/2/tweets/{id}",
                            None,
                            _source_ids(payload),
                            sequence + 1,
                            {"selection": kind},
                        )
                    )
                    counts["posts"] += self.silver.apply_posts(run_id, record.object_id, payload)
                    result[f"{kind}_post_id"] = post_id
                self._archive_media_and_markdown(run_id, counts)
                self.bronze.write_run_manifest(run_id)
                self._record_usage(run_id, api, counts)
                self._finish(run_id, "success", counts)
                self._event(
                    run_id,
                    "probe",
                    "completed",
                    **{key: value for key, value in result.items() if key != "run_id"},
                )
                return result
        except Exception as error:
            self._finish(run_id, "failed", counts, str(error))
            self._event(run_id, "probe", "failed", "error", error=str(error))
            raise

    @staticmethod
    def _select_probe_posts(payload: dict[str, Any]) -> dict[str, str]:
        """Select the first ordinary post and first Article from one page."""
        data = payload.get("data", [])
        if not isinstance(data, list):
            raise ValueError("bookmark response has no data list")
        ordinary: str | None = None
        article: str | None = None
        for item in data:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            post_id = str(item["id"])
            has_article = isinstance(item.get("article"), dict) and bool(item["article"])
            if has_article and article is None:
                article = post_id
            elif not has_article and ordinary is None:
                ordinary = post_id
        if ordinary is None or article is None:
            raise ValueError(
                "the bounded bookmark page did not contain both an ordinary post and an Article; "
                "rerun with a larger --max-results value"
            )
        return {"ordinary": ordinary, "article": article}
