"""ETL orchestration for bookmarks and one-post probes."""

import json
import re
import uuid
from typing import Any

from .auth import authenticated_client
from .bronze import BronzeWriter, BronzeWriteRequest
from .config import Settings
from .db import Database, utc_now
from .lock import ProcessLock
from .logging_setup import JsonlLogger
from .media import MediaDownloader
from .silver import SilverNormalizer
from .x_api import MAX_POST_IDS_PER_REQUEST, XApi

POST_URL = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/(\d+)(?:[/?#].*)?$")


def _source_ids(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data", [])
    if isinstance(data, dict):
        data = [data]
    return [str(item["id"]) for item in data if isinstance(item, dict) and item.get("id")]


def _next_token(payload: dict[str, Any]) -> str | None:
    meta = payload.get("meta")
    return str(meta["next_token"]) if isinstance(meta, dict) and meta.get("next_token") else None


class Pipeline:
    """Run the Bronze-to-Silver pipeline."""

    def __init__(self, settings: Settings, api: Any | None = None) -> None:
        self.settings = settings
        self.api = api
        self.database = Database(settings.database_path)
        self.database.initialize()
        self.bronze = BronzeWriter(settings.vault_path, self.database)
        self.silver = SilverNormalizer(self.database)
        self.log = JsonlLogger(settings.log_path)

    def _start_run(self) -> str:
        run_id = str(uuid.uuid4())
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO runs(run_id, started_at, status) VALUES (?, ?, 'running')",
                (run_id, utc_now()),
            )
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

    def _sync_folders(self, api: Any, user_id: str, run_id: str, counts: dict[str, int]) -> None:
        """Archive folders and hydrate their post IDs in batches."""
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
            for folder in folder_payload.get("data", []):
                if not isinstance(folder, dict) or not folder.get("id"):
                    continue
                folder_id = str(folder["id"])
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
                for offset in range(0, len(folder_post_ids), MAX_POST_IDS_PER_REQUEST):
                    batch_ids = folder_post_ids[offset : offset + MAX_POST_IDS_PER_REQUEST]
                    content_payload = api.posts(batch_ids)
                    counts["folder_content_batches"] += 1
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
                        run_id, content_record.object_id, content_payload, folder_id
                    )

    def sync(self, max_pages: int | None = None, dry_run: bool = False) -> dict[str, int | str]:
        """Fetch bookmarks and folders, with an optional page bound."""
        run_id = self._start_run()
        counts = {
            "bookmark_pages": 0,
            "posts": 0,
            "folder_pages": 0,
            "folder_posts": 0,
            "folder_content_batches": 0,
        }
        try:
            with ProcessLock(self.settings.lock_path):
                api = self.api or XApi(authenticated_client(self.settings), self.settings)
                user_payload = api.current_user()
                user_data = user_payload.get("data", {})
                user_id = str(user_data["id"])
                self._event(run_id, "extract", "authenticated", user_id=user_id)
                cursor_value = self.database.get_checkpoint(f"bookmarks:{user_id}")
                cursor = cursor_value.get("next_token") if isinstance(cursor_value, dict) else None
                while max_pages is None or counts["bookmark_pages"] < max_pages:
                    payload = api.bookmark_page(user_id, cursor)
                    counts["bookmark_pages"] += 1
                    if not dry_run:
                        record = self.bronze.write_json(
                            BronzeWriteRequest(
                                run_id,
                                "bookmarks-page",
                                payload,
                                "/2/users/{id}/bookmarks",
                                cursor,
                                _source_ids(payload),
                                counts["bookmark_pages"],
                            )
                        )
                        counts["posts"] += self.silver.apply_posts(
                            run_id, record.object_id, payload
                        )
                        next_cursor = _next_token(payload)
                        self.database.set_checkpoint(
                            f"bookmarks:{user_id}", {"next_token": next_cursor}
                        )
                    else:
                        next_cursor = _next_token(payload)
                    if not next_cursor:
                        break
                    cursor = next_cursor
                if not dry_run and max_pages is None:
                    self._sync_folders(api, user_id, run_id, counts)
                    media_counts = MediaDownloader(
                        self.settings, self.database, self.bronze
                    ).download_pending(run_id)
                    counts.update({f"media_{key}": value for key, value in media_counts.items()})
                elif not dry_run:
                    self._event(run_id, "pipeline", "folders_skipped", reason="bounded_sync")
                if not dry_run:
                    self.bronze.write_run_manifest(run_id)
                self._finish(run_id, "success", counts)
                self._event(run_id, "pipeline", "completed", counts=counts)
                return {"run_id": run_id, **counts}
        except Exception as error:
            self._finish(run_id, "failed", counts, str(error))
            self._event(run_id, "pipeline", "failed", "error", error=str(error))
            raise

    def probe_post(self, url: str) -> dict[str, str | int]:
        """Fetch and archive exactly one canonical post URL."""
        match = POST_URL.match(url)
        if not match:
            raise ValueError("URL must match https://x.com/<username>/status/<id>")
        run_id = self._start_run()
        try:
            with ProcessLock(self.settings.lock_path):
                api = XApi(authenticated_client(self.settings), self.settings)
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
                posts = self.silver.apply_posts(run_id, record.object_id, payload)
                MediaDownloader(self.settings, self.database, self.bronze).download_pending(run_id)
                self.bronze.write_run_manifest(run_id)
                self._finish(run_id, "success", {"posts": posts})
                self._event(run_id, "probe", "completed", post_id=match.group(1), posts=posts)
                return {"run_id": run_id, "post_id": match.group(1), "posts": posts}
        except Exception as error:
            self._finish(run_id, "failed", {"posts": 0}, str(error))
            self._event(run_id, "probe", "failed", "error", error=str(error))
            raise

    def probe_bookmarks(self, max_results: int = 20) -> dict[str, str | int]:
        """Fetch one bookmark page and exactly one ordinary post and Article."""
        if not 1 <= max_results <= self.settings.max_results_per_page:
            raise ValueError(
                f"max_results must be between 1 and {self.settings.max_results_per_page}"
            )
        run_id = self._start_run()
        try:
            with ProcessLock(self.settings.lock_path):
                api = XApi(authenticated_client(self.settings), self.settings)
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
                    self.silver.apply_posts(run_id, record.object_id, payload)
                    result[f"{kind}_post_id"] = post_id
                MediaDownloader(self.settings, self.database, self.bronze).download_pending(run_id)
                self.bronze.write_run_manifest(run_id)
                self._finish(run_id, "success", {"posts": len(selected)})
                self._event(
                    run_id,
                    "probe",
                    "completed",
                    **{key: value for key, value in result.items() if key != "run_id"},
                )
                return result
        except Exception as error:
            self._finish(run_id, "failed", {"posts": 0}, str(error))
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
