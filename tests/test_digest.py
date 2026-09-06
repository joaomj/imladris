"""Tests for digest selection, prompt budget, and safe HTML rendering."""

import json
from pathlib import Path

from x_digest.config import Settings
from x_digest.db import Database, utc_now
from x_digest.digest import DigestBuilder, DigestStore

POST_IDS = ["10", "11", "12"]
BATCH_LIMIT = 20
LARGE_BATCH_COUNT = 25
PROMPT_BUDGET = 12000


def _insert_post(database: Database, post_id: str, seen: str, body: str = "Body") -> None:
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO posts(post_id, username, created_at, url, text,
               content_state, current_content_hash, first_seen_at, last_seen_at)
               VALUES (?, 'reader', '2026-08-01T00:00:00Z',
               'https://x.com/reader/status/' || ?, ?, 'complete', 'hash'
               || ?, ?, ?)""",
            (post_id, post_id, f"{body} {post_id}", post_id, seen, seen),
        )


def _settings(vault: Path, **overrides: object) -> Settings:
    return Settings(vault_path=vault, **overrides)  # type: ignore[arg-type]


def test_failed_delivery_keeps_pending_batch(tmp_path: Path) -> None:
    database = Database(tmp_path / "silver.sqlite")
    database.initialize()
    _insert_post(database, "10", utc_now())
    store = DigestStore(database, _settings(tmp_path))
    state = store.load_state()
    posts, _, _ = store.select_batch(state["cursor"], 20)
    assert [row["post_id"] for row in posts] == ["10"]
    # Simulate a delivery failure: state is not advanced.
    assert store.pending_count(state["cursor"]) == 1
    assert [row["post_id"] for row in store.select_batch(state["cursor"], 20)[0]] == [
        "10"
    ]


def test_successful_delivery_advances_cursor(tmp_path: Path) -> None:
    database = Database(tmp_path / "silver.sqlite")
    database.initialize()
    _insert_post(database, "10", "2026-08-01T00:00:00+00:00")
    _insert_post(database, "11", "2026-08-02T00:00:00+00:00")
    store = DigestStore(database, _settings(tmp_path))
    state = store.load_state()
    posts, _, _ = store.select_batch(state["cursor"], 20)
    cursor = DigestStore.cursor_for(posts)
    assert cursor is not None
    store.clear_batch(cursor)
    assert store.pending_count(store.load_state()["cursor"]) == 0


def test_probe_run_does_not_consume_digest_cursor(tmp_path: Path) -> None:
    database = Database(tmp_path / "silver.sqlite")
    database.initialize()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO runs(run_id, started_at, status) VALUES ('probe', ?, 'success')",
            (utc_now(),),
        )
    _insert_post(database, "10", utc_now())
    store = DigestStore(database, _settings(tmp_path))
    posts, _, total = store.select_batch(store.load_state()["cursor"], 20)
    assert total == 1
    assert [row["post_id"] for row in posts] == ["10"]


def test_more_than_limit_retains_excess(tmp_path: Path) -> None:
    database = Database(tmp_path / "silver.sqlite")
    database.initialize()
    for index in range(LARGE_BATCH_COUNT):
        _insert_post(database, f"{100 + index}", "2026-08-01T00:00:00+00:00")
    store = DigestStore(database, _settings(tmp_path))
    posts, has_more, total = store.select_batch(None, BATCH_LIMIT)
    assert len(posts) == BATCH_LIMIT
    assert has_more is True
    assert total == LARGE_BATCH_COUNT


def test_timestamp_ties_use_post_id_order(tmp_path: Path) -> None:
    database = Database(tmp_path / "silver.sqlite")
    database.initialize()
    for post_id in ("b", "a", "c"):
        _insert_post(database, post_id, "2026-08-01T00:00:00+00:00")
    store = DigestStore(database, _settings(tmp_path))
    posts, _, _ = store.select_batch(None, 20)
    assert [row["post_id"] for row in posts] == ["a", "b", "c"]


def test_prompt_stays_within_total_budget(tmp_path: Path) -> None:
    settings = _settings(tmp_path, digest_max_chars_per_post=800)
    long_posts = [
        {
            "post_id": str(index),
            "username": "reader",
            "url": f"https://x.com/reader/status/{index}",
            "text": "x" * 5000,
            "created_at": "2026-08-01",
            "first_seen_at": "2026-08-01",
        }
        for index in range(20)
    ]
    _, user, prompt_chars, _ = DigestBuilder(settings).build_prompt(long_posts)
    assert prompt_chars < PROMPT_BUDGET
    assert len(user) < PROMPT_BUDGET


def test_build_prompt_cites_only_input_ids(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    posts = [
        {
            "post_id": post_id,
            "username": "reader",
            "url": f"https://x.com/reader/status/{post_id}",
            "text": "Body",
            "created_at": "2026-08-01",
            "first_seen_at": "2026-08-01",
        }
        for post_id in POST_IDS
    ]
    _, user, _, _ = DigestBuilder(settings).build_prompt(posts)
    for post_id in POST_IDS:
        assert post_id in user
    assert "9999" not in user


def test_parse_rejects_unknown_post_ids(tmp_path: Path) -> None:
    builder = DigestBuilder(_settings(tmp_path))
    payload = json.dumps(
        {
            "takeaway": "One. Two.",
            "themes": [{"title": "Theme", "points": [{"text": "Point", "post_ids": ["9999"]}]}],
        }
    )
    try:
        builder.parse_response(payload, {"10"})
    except ValueError as error:
        assert "unknown post IDs" in str(error)
    else:
        raise AssertionError("unknown post IDs must be rejected")


def test_rendered_html_escapes_untrusted_text(tmp_path: Path) -> None:
    builder = DigestBuilder(_settings(tmp_path))
    parsed = {
        "takeaway": "Takeaway with <b>tags</b> & symbols.",
        "themes": [
            {
                "title": "Theme <script>",
                "points": [{"text": "Point <i>here</i>", "post_ids": ["10"]}],
            }
        ],
    }
    posts = {
        "10": {
            "post_id": "10",
            "username": "evil<script>",
            "url": "https://x.com/reader/status/10",
        }
    }
    rendered = builder.render_html(parsed, posts)
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert '<a href="https://x.com/reader/status/10">' in rendered
