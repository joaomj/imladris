"""Tests for the digest CLI boundary without live network calls."""

import json
from pathlib import Path

import pytest

from x_digest import cli
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


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at an empty vault; the shared fixture blanks credentials."""
    monkeypatch.setenv("XDIGEST_VAULT_PATH", str(tmp_path))
    return tmp_path


def test_digest_dry_run_prints_preview_without_network(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = Database(vault / "silver.sqlite")
    database.initialize()
    _insert_post(database)
    assert cli.main(["digest", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["post_ids"] == ["cli-1"]
    assert "Weekly digest preview" in payload["text"]
    assert "AI takeaway will appear here" in payload["text"]
    assert "AI summary" not in payload["text"].replace(
        "Run with --send to generate the AI summary.", ""
    )


def test_digest_send_without_config_exits_two(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = Database(vault / "silver.sqlite")
    database.initialize()
    assert cli.main(["digest", "--send"]) == MISSING_CONFIG_EXIT
    assert "not configured" in capsys.readouterr().err


def test_digest_dry_run_rejects_limit_below_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["digest", "--dry-run", "--limit", "0"])
    assert exit_info.value.code == MISSING_CONFIG_EXIT
    assert "at least 1" in capsys.readouterr().err
