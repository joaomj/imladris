"""Tests for the digest CLI boundary without live network calls."""

import json
import os
import subprocess
from pathlib import Path

from x_digest.db import Database, utc_now

MISSING_CONFIG_EXIT = 2


def _insert_post(database: Database) -> None:
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO posts(post_id, username, created_at, url, text,
               content_state, current_content_hash, first_seen_at, last_seen_at)
               VALUES ('cli-1', 'reader', '2026-08-01T00:00:00Z',
               'https://x.com/reader/status/cli-1', 'CLI preview body',
               'complete', 'hash-cli-1', ?, ?)""",
            (utc_now(), utc_now()),
        )


def _run(
    args: list[str], vault: Path, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "XDIGEST_VAULT_PATH": str(vault),
        # Blank values outrank dotenv entries; validators normalize them
        # to missing so the child process never sees developer credentials.
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_USER_ID": "",
        "XDIGEST_TELEGRAM_BOT_TOKEN": "",
        "XDIGEST_TELEGRAM_CHAT_ID": "",
        "XDIGEST_LLM_API_KEY": "",
    }
    if extra:
        environment.update(extra)
    return subprocess.run(
        ["uv", "run", "x-digest", *args],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_digest_dry_run_prints_preview_without_network(tmp_path: Path) -> None:
    database = Database(tmp_path / "silver.sqlite")
    database.initialize()
    _insert_post(database)
    result = _run(["digest", "--dry-run"], tmp_path)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "dry-run"
    assert payload["post_ids"] == ["cli-1"]
    assert "Weekly digest preview" in payload["text"]
    assert "AI takeaway will appear here" in payload["text"]
    assert "AI summary" not in payload["text"].replace(
        "Run with --send to generate the AI summary.", ""
    )


def test_digest_send_without_config_exits_two(tmp_path: Path) -> None:
    database = Database(tmp_path / "silver.sqlite")
    database.initialize()
    result = _run(["digest", "--send"], tmp_path)
    assert result.returncode == MISSING_CONFIG_EXIT
    assert "not configured" in result.stderr


def test_digest_dry_run_rejects_limit_below_one(tmp_path: Path) -> None:
    result = _run(["digest", "--dry-run", "--limit", "0"], tmp_path)
    assert result.returncode == MISSING_CONFIG_EXIT
    assert "at least 1" in result.stderr
