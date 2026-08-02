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
    assert media_result == {"attempted": MEDIA_ATTEMPT_COUNT, "downloaded": 1, "failed": 1}
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
