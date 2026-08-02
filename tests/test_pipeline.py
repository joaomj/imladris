"""End-to-end tests for the local pipeline using real SQLite and files."""

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from x_digest.bronze import BronzeWriter, BronzeWriteRequest
from x_digest.config import DEFAULT_X_SCOPE, Settings
from x_digest.db import Database, utc_now
from x_digest.gold import GoldStore
from x_digest.markdown import MarkdownWriter
from x_digest.media import MediaDownloader
from x_digest.pipeline import Pipeline
from x_digest.silver import SilverNormalizer

POST_COUNT = 2
OBSERVATION_COUNT = 4
VERSION_COUNT = 2
BRONZE_OBJECT_COUNT = 2
MEDIA_ATTEMPT_COUNT = 2
USAGE_ERROR = 2
FOLDER_POST_COUNT = 2
INCREMENTAL_FIRST_PAGES = 2
INCREMENTAL_SECOND_PAGES = 1
FULL_SYNC_PAGES = 2
TOTAL_PAGE_CALLS = 3
BASE_PAGE_CALLS = 2
NEW_PAGE_CALL = 3
MARKDOWN_INCREMENTAL_POSTS = 4
MARKDOWN_SEED_POSTS = 2


class FolderContentApi:
    """Fake external X service for the complete folder-sync user flow."""

    def current_user(self) -> dict[str, object]:
        return {"data": {"id": "owner"}}

    def bookmark_page(
        self, _user_id: str, _cursor: str | None, _max_results: int | None = None
    ) -> dict[str, object]:
        return {"data": [], "meta": {"result_count": 0}}

    def folders(self, _user_id: str) -> object:
        yield {"data": [{"id": "agents", "name": "agents"}]}

    def folder_posts(self, _user_id: str, _folder_id: str) -> dict[str, object]:
        return {"data": [{"id": "501"}, {"id": "502"}]}

    def posts(self, post_ids: list[str]) -> dict[str, object]:
        assert post_ids == ["501", "502"]
        return {
            "data": [
                {"id": "501", "author_id": "601", "text": "Agent memory patterns"},
                {"id": "502", "author_id": "602", "text": "Reliable tool loops"},
            ],
            "includes": {
                "users": [
                    {"id": "601", "username": "alice", "name": "Alice"},
                    {"id": "602", "username": "bob", "name": "Bob"},
                ]
            },
        }


class FolderIgnoreApi(FolderContentApi):
    """Fake external X service with one folder that must not be fetched."""

    def folders(self, _user_id: str) -> object:
        yield {"data": [{"id": "spam", "name": "spam"}, {"id": "agents", "name": "agents"}]}

    def folder_posts(self, _user_id: str, folder_id: str) -> dict[str, object]:
        assert folder_id != "spam"
        return super().folder_posts(_user_id, folder_id)

    def posts(self, post_ids: list[str]) -> dict[str, object]:
        assert post_ids != ["spam-post"]
        return super().posts(post_ids)


class IncrementalApi(FolderContentApi):
    """Fake X service with two bookmark pages and per-page call counting."""

    def __init__(self) -> None:
        self.page_calls = 0

    def bookmark_page(
        self, _user_id: str, _cursor: str | None, _max_results: int | None = None
    ) -> dict[str, object]:
        self.page_calls += 1
        pages = [
            {
                "data": [{"id": "301", "author_id": "601", "text": "Newest post"}],
                "meta": {"next_token": "t1"},
            },
            {"data": [{"id": "302", "author_id": "601", "text": "Older post"}], "meta": {}},
        ]
        return pages[(self.page_calls - 1) % len(pages)]


class GrowingApi(IncrementalApi):
    """Fake X service where one new post appears at the front of a later run."""

    def bookmark_page(
        self, _user_id: str, _cursor: str | None, _max_results: int | None = None
    ) -> dict[str, object]:
        self.page_calls += 1
        if self.page_calls <= BASE_PAGE_CALLS:
            return super().bookmark_page(_user_id, _cursor, _max_results)
        if self.page_calls == NEW_PAGE_CALL:
            return {
                "data": [
                    {"id": "999", "author_id": "601", "text": "Brand new post"},
                    {"id": "301", "author_id": "601", "text": "Newest post"},
                ],
                "meta": {"next_token": "t1"},
            }
        return {"data": [{"id": "302", "author_id": "601", "text": "Older post"}], "meta": {}}


def _payload() -> dict[str, object]:
    return {
        "data": [
            {
                "id": "100",
                "author_id": "200",
                "created_at": "2026-07-22T12:00:00Z",
                "lang": "en",
                "text": "A local archive keeps bookmarks searchable.",
                "attachments": {"media_keys": ["3_1"]},
            },
            {
                "id": "101",
                "author_id": "200",
                "created_at": "2026-07-22T13:00:00Z",
                "article": {"title": "An Article without body"},
                "text": "Article preview",
            },
        ],
        "includes": {
            "users": [{"id": "200", "username": "reader", "name": "Reader"}],
            "media": [{"media_key": "3_1", "type": "photo", "url": "https://example.test/a.jpg"}],
        },
        "meta": {"result_count": 2},
    }


def _seed_vault(root: Path) -> tuple[Database, str]:
    database = Database(root / "silver.sqlite")
    database.initialize()
    run_id = str(uuid.uuid4())
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO runs(run_id, started_at, status) VALUES (?, ?, 'running')",
            (run_id, utc_now()),
        )
    writer = BronzeWriter(root, database)
    normalizer = SilverNormalizer(database)
    payload = _payload()
    first = writer.write_json(
        BronzeWriteRequest(
            run_id,
            "bookmarks-page",
            payload,
            "/2/users/{id}/bookmarks",
            None,
            ["100", "101"],
            1,
        )
    )
    normalizer.apply_posts(run_id, first.object_id, payload)
    second = writer.write_json(
        BronzeWriteRequest(
            run_id,
            "bookmarks-page",
            payload,
            "/2/users/{id}/bookmarks",
            "next",
            ["100", "101"],
            2,
        )
    )
    normalizer.apply_posts(run_id, second.object_id, payload)
    writer.write_run_manifest(run_id)
    return database, run_id


def test_append_only_observations_search_export_and_rebuild(tmp_path: Path) -> None:
    database, _ = _seed_vault(tmp_path)
    store = GoldStore(database)

    with database.connect() as connection:
        paths = connection.execute("SELECT path, manifest_path FROM bronze_objects").fetchall()
    assert all(not Path(row["path"]).is_absolute() for row in paths)
    assert all(not Path(row["manifest_path"]).is_absolute() for row in paths)

    assert store.status()["counts"] == {
        "bronze_objects": BRONZE_OBJECT_COUNT,
        "posts": POST_COUNT,
        "media": 1,
        "folders": 0,
        "runs": 1,
    }
    assert store.search("searchable")[0]["post_id"] == "100"
    assert store.show("101")["content_state"] == "article_metadata_only"
    media_source = tmp_path / "source.jpg"
    media_source.write_bytes(b"image bytes")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE media SET source_url=? WHERE media_key='3_1'",
            (media_source.as_uri(),),
        )
        connection.execute(
            """INSERT INTO media(media_key, post_id, source_url, status,
               first_seen_at, last_seen_at)
               VALUES ('missing', '100', 'file:///does-not-exist', 'pending', ?, ?)""",
            (utc_now(), utc_now()),
        )
    media_result = MediaDownloader(
        Settings(vault_path=tmp_path), database, BronzeWriter(tmp_path, database)
    ).download_pending("media-run")
    assert media_result == {
        "attempted": MEDIA_ATTEMPT_COUNT,
        "downloaded": 1,
        "failed": 1,
        "bytes_downloaded": len(b"image bytes"),
    }
    assert store.show("100")["media"][0]["status"] == "downloaded"
    with database.connect() as connection:
        observations = connection.execute("SELECT COUNT(*) FROM post_observations").fetchone()[0]
        versions = connection.execute("SELECT COUNT(*) FROM post_versions").fetchone()[0]
    assert observations == OBSERVATION_COUNT
    assert versions == VERSION_COUNT
    assert store.verify(full=True) == {"checked": 3, "failed": 0}

    output = tmp_path / "export.json"
    assert store.export(output, "json") == POST_COUNT
    assert len(json.loads(output.read_text(encoding="utf-8"))) == POST_COUNT

    rebuilt = store.rebuild_silver(tmp_path / "bronze")
    assert rebuilt == {"objects": BRONZE_OBJECT_COUNT, "posts": OBSERVATION_COUNT, "folders": 0}
    with database.connect() as connection:
        media_rows = connection.execute(
            "SELECT media_key, status, archive_path FROM media ORDER BY media_key"
        ).fetchall()
    assert {row["media_key"]: row["status"] for row in media_rows} == {
        "3_1": "downloaded",
    }
    assert all(row["archive_path"] for row in media_rows if row["status"] == "downloaded")


def test_cli_status_uses_configured_vault(tmp_path: Path) -> None:
    _seed_vault(tmp_path)
    environment = {**os.environ, "XDIGEST_VAULT_PATH": str(tmp_path)}
    result = subprocess.run(
        ["uv", "run", "x-digest", "status"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert '"posts": 2' in result.stdout


def test_cli_dry_run_requires_a_page_bound() -> None:
    result = subprocess.run(
        ["uv", "run", "x-digest", "sync", "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == USAGE_ERROR
    assert "requires --max-pages" in result.stderr


def test_cli_has_no_scheduler_commands() -> None:
    result = subprocess.run(
        ["uv", "run", "x-digest", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "install-scheduler" not in result.stdout
    assert "scheduled-sync" not in result.stdout


def test_sync_ignores_matching_folder_completely(tmp_path: Path) -> None:
    settings = Settings(vault_path=tmp_path)
    result = Pipeline(settings, api=FolderIgnoreApi()).sync(ignore_folders=["spam"])
    store = GoldStore(Database(settings.database_path))

    assert result["folders_ignored"] == 1
    assert result["folder_posts"] == FOLDER_POST_COUNT
    assert store.show("501")["username"] == "alice"
    with Database(settings.database_path).connect() as connection:
        memberships = connection.execute(
            "SELECT COUNT(*) FROM bookmark_memberships WHERE folder_id='spam'"
        ).fetchone()[0]
        ignored_events = connection.execute(
            """SELECT COUNT(*) FROM run_events
               WHERE event='folder_ignored' AND details_json LIKE '%spam%'"""
        ).fetchone()[0]
    assert memberships == 0
    assert ignored_events == 1


def test_incremental_sync_stops_after_archived_pages(tmp_path: Path) -> None:
    settings = Settings(vault_path=tmp_path)
    api = IncrementalApi()
    first = Pipeline(settings, api=api).sync()
    assert first["bookmark_pages"] == INCREMENTAL_FIRST_PAGES
    assert first["markdown_written"] == MARKDOWN_INCREMENTAL_POSTS
    second = Pipeline(settings, api=api).sync()
    assert second["bookmark_pages"] == INCREMENTAL_SECOND_PAGES
    assert second["stopped_early"] == 1
    assert second["folder_content_batches"] == 0
    assert second["markdown_written"] == 0
    assert second["markdown_skipped"] == MARKDOWN_INCREMENTAL_POSTS
    assert api.page_calls == TOTAL_PAGE_CALLS
    with Database(settings.database_path).connect() as connection:
        stopped = connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE event='stopped_early'"
        ).fetchone()[0]
    assert stopped == 1
    assert (tmp_path / "markdown" / "posts" / "301.md").exists()
    assert (tmp_path / "markdown" / "folders" / "agents" / "501.md").exists()


def test_markdown_follows_folder_organization(tmp_path: Path) -> None:
    settings = Settings(vault_path=tmp_path)
    Pipeline(settings, api=FolderContentApi()).sync()
    database = Database(settings.database_path)
    writer = MarkdownWriter(settings, database)

    first = (tmp_path / "markdown" / "folders" / "agents" / "501.md").read_text(
        encoding="utf-8"
    )
    assert "Agent memory patterns" in first
    assert (tmp_path / "markdown" / "folders" / "agents" / "502.md").exists()
    assert not (tmp_path / "markdown" / "posts" / "501.md").exists()

    with database.connect() as connection:
        latest_run = connection.execute(
            "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    sync_run_id = str(latest_run["run_id"])
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO folders(folder_id, name, raw_json, first_seen_at, last_seen_at)
               VALUES ('tools', 'tools', '{}', ?, ?)""",
            (utc_now(), utc_now()),
        )
        connection.execute(
            """INSERT INTO bookmark_memberships(post_id, folder_id, run_id, observed_at)
               VALUES ('501', 'tools', ?, ?)""",
            (sync_run_id, utc_now()),
        )
    counts = writer.write_new("md-run")
    assert counts == {"written": 1, "skipped": 2, "no_content": 0}
    second = (tmp_path / "markdown" / "folders" / "tools" / "501.md").read_text(
        encoding="utf-8"
    )
    assert "Agent memory patterns" in second


def test_rebuild_silver_skips_ignored_folders(tmp_path: Path) -> None:
    settings = Settings(vault_path=tmp_path)
    Pipeline(settings, api=FolderContentApi()).sync()
    store = GoldStore(Database(settings.database_path))
    store.rebuild_silver(tmp_path / "bronze", ["agents"])
    with Database(settings.database_path).connect() as connection:
        folder_count = connection.execute(
            "SELECT COUNT(*) FROM folders WHERE name='agents'"
        ).fetchone()[0]
        membership_count = connection.execute(
            "SELECT COUNT(*) FROM bookmark_memberships WHERE folder_id='agents'"
        ).fetchone()[0]
        post_count = connection.execute(
            "SELECT COUNT(*) FROM posts WHERE post_id IN ('501', '502')"
        ).fetchone()[0]
    assert folder_count == 0
    assert membership_count == 0
    assert post_count == 0


def test_rebuild_silver_keeps_folders_without_ignore_list(tmp_path: Path) -> None:
    settings = Settings(vault_path=tmp_path)
    Pipeline(settings, api=FolderContentApi()).sync()
    store = GoldStore(Database(settings.database_path))
    store.rebuild_silver(tmp_path / "bronze")
    with Database(settings.database_path).connect() as connection:
        folder_count = connection.execute(
            "SELECT COUNT(*) FROM folders WHERE name='agents'"
        ).fetchone()[0]
        membership_count = connection.execute(
            "SELECT COUNT(*) FROM bookmark_memberships WHERE folder_id='agents'"
        ).fetchone()[0]
    assert folder_count == 1
    assert membership_count == FOLDER_POST_COUNT


def test_cli_markdown_command_writes_missing_files(tmp_path: Path) -> None:
    _seed_vault(tmp_path)
    environment = {**os.environ, "XDIGEST_VAULT_PATH": str(tmp_path)}
    first = subprocess.run(
        ["uv", "run", "x-digest", "markdown"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert '"written": 2' in first.stdout
    assert (tmp_path / "markdown" / "posts" / "100.md").exists()
    second = subprocess.run(
        ["uv", "run", "x-digest", "markdown"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert '"written": 0' in second.stdout
    assert '"skipped": 2' in second.stdout


def test_markdown_writer_creates_complete_files_with_media(tmp_path: Path) -> None:
    database, _ = _seed_vault(tmp_path)
    media_source = tmp_path / "source.jpg"
    media_source.write_bytes(b"image bytes")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE media SET source_url=? WHERE media_key='3_1'",
            (media_source.as_uri(),),
        )
        connection.execute(
            """INSERT INTO media(media_key, post_id, media_type, source_url, archive_path,
               status, first_seen_at, last_seen_at)
               VALUES ('13_1', '100', 'video', 'file:///does-not-matter',
               'bronze/media/13_1.mp4', 'downloaded', ?, ?)""",
            (utc_now(), utc_now()),
        )
    MediaDownloader(
        Settings(vault_path=tmp_path), database, BronzeWriter(tmp_path, database)
    ).download_pending("media-run")
    counts = MarkdownWriter(Settings(vault_path=tmp_path), database).write_new("md-run")
    assert counts == {"written": 2, "skipped": 0, "no_content": 0}

    first = (tmp_path / "markdown" / "posts" / "100.md").read_text(encoding="utf-8")
    assert "post_id: 100" in first
    assert "A local archive keeps bookmarks searchable." in first
    assert "![3_1](" in first
    assert "[video](../../bronze/media/13_1.mp4)" in first
    image_reference = first.split("![3_1](")[1].split(")")[0]
    assert (tmp_path / "markdown" / "posts" / image_reference).exists()
    second = (tmp_path / "markdown" / "posts" / "101.md").read_text(encoding="utf-8")
    assert "Article preview" in second
    assert "## Media" not in second


def test_markdown_never_overwrites_existing_files(tmp_path: Path) -> None:
    database, _ = _seed_vault(tmp_path)
    writer = MarkdownWriter(Settings(vault_path=tmp_path), database)
    assert writer.write_new("md-run")["written"] == MARKDOWN_SEED_POSTS
    target = tmp_path / "markdown" / "posts" / "100.md"
    target.write_text("hand edited content\n", encoding="utf-8")
    counts = writer.write_new("md-run")
    assert counts == {"written": 0, "skipped": 2, "no_content": 0}
    assert target.read_text(encoding="utf-8") == "hand edited content\n"


def test_incremental_sync_captures_new_posts(tmp_path: Path) -> None:
    settings = Settings(vault_path=tmp_path)
    api = GrowingApi()
    Pipeline(settings, api=api).sync()
    result = Pipeline(settings, api=api).sync()
    assert result["bookmark_pages"] == FULL_SYNC_PAGES
    store = GoldStore(Database(settings.database_path))
    assert store.show("999")["text"] == "Brand new post"
    assert store.show("301")["content_state"] == "complete"


def test_full_sync_rereads_everything(tmp_path: Path) -> None:
    settings = Settings(vault_path=tmp_path)
    api = IncrementalApi()
    Pipeline(settings, api=api).sync()
    result = Pipeline(settings, api=api).sync(full=True)
    assert result["bookmark_pages"] == FULL_SYNC_PAGES
    assert result["stopped_early"] == 0
    assert result["folder_content_batches"] == 1


def test_sync_downloads_searchable_content_for_folder_bookmarks(tmp_path: Path) -> None:
    settings = Settings(vault_path=tmp_path)
    result = Pipeline(settings, api=FolderContentApi()).sync()
    store = GoldStore(Database(settings.database_path))

    assert result["folder_posts"] == FOLDER_POST_COUNT
    assert [post["post_id"] for post in store.search("Agent OR Reliable")] == ["501", "502"]
    assert store.show("501")["username"] == "alice"
    with Database(settings.database_path).connect() as connection:
        memberships = connection.execute(
            "SELECT COUNT(*) FROM bookmark_memberships WHERE folder_id='agents'"
        ).fetchone()[0]
    assert memberships == FOLDER_POST_COUNT


def test_moved_legacy_vault_remains_verifiable_and_rebuildable(tmp_path: Path) -> None:
    original = tmp_path / "original"
    database, _ = _seed_vault(original)
    with database.transaction() as connection:
        rows = connection.execute(
            "SELECT object_id, path, manifest_path FROM bronze_objects"
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE bronze_objects SET path=?, manifest_path=? WHERE object_id=?",
                (
                    str(original / row["path"]),
                    str(original / row["manifest_path"]),
                    row["object_id"],
                ),
            )

    moved = tmp_path / "moved"
    shutil.move(str(original), str(moved))
    moved_database = Database(moved / "silver.sqlite")
    moved_database.initialize()
    store = GoldStore(moved_database)

    assert store.verify(full=True) == {"checked": 2, "failed": 0}
    assert store.rebuild_silver(moved / "bronze") == {
        "objects": BRONZE_OBJECT_COUNT,
        "posts": OBSERVATION_COUNT,
        "folders": 0,
    }


def test_empty_scope_configuration_uses_read_only_defaults() -> None:
    assert Settings(x_scope="").x_scope == DEFAULT_X_SCOPE


def test_normalizer_handles_null_includes_media(tmp_path: Path) -> None:
    database, run_id = _seed_vault(tmp_path)
    writer = BronzeWriter(tmp_path, database)
    normalizer = SilverNormalizer(database)
    payload = {
        "data": [{"id": "777", "author_id": "200", "text": "Null includes body"}],
        "includes": {"users": None, "media": None},
    }
    record = writer.write_json(
        BronzeWriteRequest(
            run_id,
            "folder-post-contents",
            payload,
            "/2/tweets",
            None,
            ["777"],
            5,
        )
    )
    assert normalizer.apply_posts(run_id, record.object_id, payload) == 1
    assert GoldStore(database).show("777")["content_state"] == "complete"


def test_normalizer_accepts_single_post_response_shape(tmp_path: Path) -> None:
    database, run_id = _seed_vault(tmp_path)
    writer = BronzeWriter(tmp_path, database)
    normalizer = SilverNormalizer(database)
    payload = _payload()
    payload["data"] = payload["data"][0]
    payload["data"]["referenced_tweets"] = None
    record = writer.write_json(
        BronzeWriteRequest(
            run_id,
            "probe-post",
            payload,
            "/2/tweets/{id}",
            None,
            ["100"],
            3,
        )
    )
    assert normalizer.apply_posts(run_id, record.object_id, payload) == 1

    article_payload = _payload()
    article_payload["data"] = article_payload["data"][1]
    article_payload["data"]["article"]["plain_text"] = "Complete Article body"
    article_record = writer.write_json(
        BronzeWriteRequest(
            run_id,
            "probe-article",
            article_payload,
            "/2/tweets/{id}",
            None,
            ["101"],
            4,
        )
    )
    assert normalizer.apply_posts(run_id, article_record.object_id, article_payload) == 1
    assert GoldStore(database).show("101")["content_state"] == "complete"
