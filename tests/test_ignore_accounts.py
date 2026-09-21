"""Tests for account-level ignore (usernames and author IDs)."""

from pathlib import Path

from x_digest.cli import build_parser
from x_digest.config import Settings, account_is_ignored, filter_ignored_posts
from x_digest.db import Database, utc_now
from x_digest.digest import DigestStore
from x_digest.gold import GoldStore
from x_digest.pipeline import Pipeline


class AccountFilterApi:
    """Fake X service with posts from a kept and an ignored account."""

    def current_user(self) -> dict[str, object]:
        return {"data": {"id": "owner"}}

    def bookmark_page(
        self, _user_id: str, _cursor: str | None, _max_results: int | None = None
    ) -> dict[str, object]:
        return {
            "data": [
                {"id": "701", "author_id": "801", "text": "Kept post"},
                {"id": "702", "author_id": "802", "text": "Skipped post"},
            ],
            "includes": {
                "users": [
                    {"id": "801", "username": "friend", "name": "Friend"},
                    {"id": "802", "username": "Spammer", "name": "Spammer"},
                ]
            },
            "meta": {},
        }

    def folders(self, _user_id: str) -> object:
        yield {"data": []}


def _payload() -> dict[str, object]:
    return {
        "data": [
            {"id": "701", "author_id": "801", "text": "Kept post"},
            {"id": "702", "author_id": "802", "text": "Skipped post"},
        ],
        "includes": {
            "users": [
                {"id": "801", "username": "friend", "name": "Friend"},
                {"id": "802", "username": "Spammer", "name": "Spammer"},
            ]
        },
    }


def test_account_matching_rules() -> None:
    assert account_is_ignored("Spammer", "802", ["spammer"])
    assert account_is_ignored("spammer", "802", ["@Spammer"])
    assert account_is_ignored("anyone", "802", ["802"])
    assert not account_is_ignored("friend", "801", ["spammer"])
    assert not account_is_ignored("friend", "801", [])
    assert not account_is_ignored(None, None, ["spammer"])


def test_env_parses_ignore_accounts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDIGEST_IGNORE_ACCOUNTS", "Alice, @bob , 802")
    assert Settings().ignore_accounts == ["Alice", "@bob", "802"]


def test_filter_keeps_raw_payload_intact() -> None:
    payload = _payload()
    filtered, ignored = filter_ignored_posts(payload, ["spammer"])
    assert ignored == 1
    assert len(payload["data"]) == ignored + len(filtered["data"])  # type: ignore[arg-type]
    assert [item["id"] for item in filtered["data"]] == ["701"]  # type: ignore[union-attr]


def test_sync_skips_ignored_accounts(tmp_path: Path) -> None:
    settings = Settings(vault_path=tmp_path, ignore_accounts=["spammer"])
    result = Pipeline(settings, api=AccountFilterApi()).sync()
    assert result["posts"] == 1
    assert result["posts_ignored"] == 1
    store = GoldStore(Database(settings.database_path))
    assert store.show("701")["username"] == "friend"
    assert store.show("702") is None
    # A second run still stops early: the ignored post must not defeat
    # the incremental stop check. The stop happens before archiving, so
    # nothing is counted as ignored on that run.
    second = Pipeline(settings, api=AccountFilterApi()).sync()
    assert second["stopped_early"] == 1
    assert second["posts"] == 0
    assert second["posts_ignored"] == 0


def _insert_account_post(
    database: Database, post_id: str, username: str, author_id: str
) -> None:
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO posts(post_id, author_id, username, created_at, url, text,
               content_state, current_content_hash, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, '2026-08-01T00:00:00Z',
               'https://x.com/i/status/' || ?, ?, 'complete', ?, ?, ?)""",
            (post_id, author_id, username, post_id, f"Body {post_id}", post_id,
             utc_now(), utc_now()),
        )


def test_digest_excludes_ignored_accounts(tmp_path: Path) -> None:
    database = Database(tmp_path / "silver.sqlite")
    database.initialize()
    _insert_account_post(database, "701", "friend", "801")
    _insert_account_post(database, "702", "Spammer", "802")
    store = DigestStore(database, Settings(vault_path=tmp_path, ignore_accounts=["spammer"]))
    state = store.load_state()
    assert store.pending_count(state["cursor"]) == 1
    posts, _, _ = store.select_batch(state["cursor"], 20)
    assert [row["post_id"] for row in posts] == ["701"]
    assert [row["post_id"] for row in store.get_posts_by_ids(["701", "702"])] == ["701"]


def test_rebuild_silver_skips_ignored_accounts(tmp_path: Path) -> None:
    settings = Settings(vault_path=tmp_path)
    Pipeline(settings, api=AccountFilterApi()).sync()
    store = GoldStore(Database(settings.database_path))
    store.rebuild_silver(tmp_path / "bronze", None, ["@Spammer"])
    with Database(settings.database_path).connect() as connection:
        remaining = {
            row["post_id"]
            for row in connection.execute("SELECT post_id FROM posts").fetchall()
        }
    assert remaining == {"701"}


def test_cli_accepts_ignore_account_flag() -> None:
    args = build_parser().parse_args(["sync", "--ignore-account", "spammer"])
    assert args.ignore_account == ["spammer"]
    args = build_parser().parse_args(["rebuild-silver", "--ignore-account", "802"])
    assert args.ignore_account == ["802"]


class IgnoredFolderApi:
    """Fake X service where the ignored folder drives the blocklist."""

    def current_user(self) -> dict[str, object]:
        return {"data": {"id": "owner"}}

    def bookmark_page(
        self, _user_id: str, _cursor: str | None, _max_results: int | None = None
    ) -> dict[str, object]:
        return {
            "data": [
                {"id": "903", "author_id": "9013", "text": "Straggler post"},
                {"id": "904", "author_id": "801", "text": "Kept post"},
            ],
            "includes": {
                "users": [
                    {"id": "9013", "username": "excluded_user", "name": "ExcludedUser"},
                    {"id": "801", "username": "friend", "name": "Friend"},
                ]
            },
            "meta": {},
        }

    def folders(self, _user_id: str) -> object:
        yield {"data": [{"id": "excluded", "name": "Excluded"}]}

    def folder_posts(self, _user_id: str, folder_id: str) -> dict[str, object]:
        assert folder_id == "excluded"
        return {"data": [{"id": "903"}, {"id": "905"}]}

    def posts(self, post_ids: list[str]) -> dict[str, object]:
        assert post_ids == ["905"]
        return {
            "data": [{"id": "905", "author_id": "9014", "text": "New Excluded post"}],
            "includes": {
                "users": [{"id": "9014", "username": "newbie", "name": "Newbie"}]
            },
        }


def test_ignored_folder_authors_are_auto_blocked(tmp_path: Path) -> None:
    settings = Settings(vault_path=tmp_path, ignore_folders=["excluded"])
    result = Pipeline(settings, api=IgnoredFolderApi()).sync()
    assert result["folders_ignored"] == 1
    assert result["posts_purged"] == 1
    assert result["folder_content_batches"] == 0
    database = Database(settings.database_path)
    auto = database.get_checkpoint("ignore-accounts:auto")
    assert sorted(auto["author_ids"]) == ["9013", "9014"]
    assert result["accounts_auto_blocked"] == len(auto["author_ids"])
    store = GoldStore(database)
    assert store.show("903") is None
    assert store.show("904")["username"] == "friend"
    # A rebuild honors the learned checkpoint without env configuration.
    plain = Settings(vault_path=tmp_path)
    GoldStore(Database(plain.database_path)).rebuild_silver(tmp_path / "bronze")
    with Database(settings.database_path).connect() as connection:
        remaining = {
            row["post_id"]
            for row in connection.execute("SELECT post_id FROM posts").fetchall()
        }
    assert "903" not in remaining
    assert "904" in remaining


def test_digest_uses_auto_blocked_ids(tmp_path: Path) -> None:
    database = Database(tmp_path / "silver.sqlite")
    database.initialize()
    _insert_account_post(database, "701", "friend", "801")
    _insert_account_post(database, "702", "Spammer", "802")
    database.set_checkpoint("ignore-accounts:auto", {"author_ids": ["802"]})
    store = DigestStore(database, Settings(vault_path=tmp_path))
    state = store.load_state()
    assert store.pending_count(state["cursor"]) == 1
    posts, _, _ = store.select_batch(state["cursor"], 20)
    assert [row["post_id"] for row in posts] == ["701"]
