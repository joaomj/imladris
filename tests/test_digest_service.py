"""Tests for digest delivery through the sync pipeline."""

import json
from pathlib import Path
from typing import Any

import pytest

from x_digest.config import Settings
from x_digest.db import Database
from x_digest.digest_service import DigestContext, DigestDependencies, DigestService
from x_digest.pipeline import Pipeline

DUMMY_TOKEN = "dummy-bot-token"
DUMMY_CHAT = "123456"
DUMMY_KEY = "dummy-openrouter-key"


class DigestApi:
    """Fake X service that archives one post with stable content."""

    def current_user(self) -> dict[str, object]:
        return {"data": {"id": "owner"}}

    def bookmark_page(
        self, _user_id: str, _cursor: str | None, _max_results: int | None = None
    ) -> dict[str, object]:
        return {
            "data": [
                {
                    "id": "901",
                    "author_id": "701",
                    "created_at": "2026-08-01T00:00:00Z",
                    "text": "Digest pipeline post",
                }
            ],
            "includes": {
                "users": [{"id": "701", "username": "reader", "name": "Reader"}]
            },
            "meta": {},
        }

    def folders(self, _user_id: str) -> Any:
        yield {"data": []}


class FakeLlm:
    """Fake LLM that returns valid structured output without network."""

    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload = payload or {
            "takeaway": "First sentence. Second sentence.",
            "themes": [
                {
                    "title": "Theme",
                    "points": [{"text": "Key point", "post_ids": ["901"]}],
                }
            ],
        }
        self.calls = 0

    def complete(self, _prompt: str, _system: str) -> str:
        self.calls += 1
        return json.dumps(self.payload)


class FakeSender:
    """Fake Telegram sender that records chunks without network."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[str] = []
        self.fail = fail

    def send_chunks(self, chunks: list[str], start: int = 0) -> int:
        if self.fail is not None:
            raise self.fail
        self.sent.extend(chunks[start:])
        return len(chunks) - start

    def safe_details(self, error: Exception) -> dict[str, object]:
        return {"category": type(error).__name__}


def _settings(vault: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "vault_path": vault,
        "telegram_bot_token": DUMMY_TOKEN,
        "telegram_chat_id": DUMMY_CHAT,
        "llm_api_key": DUMMY_KEY,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_sync_sends_themed_digest(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pipeline = Pipeline(settings, api=DigestApi())
    sender = FakeSender()

    def deliver(run_id: str, counts: dict[str, int]) -> dict[str, object]:
        return DigestService(
            DigestContext(
                settings=settings,
                database=pipeline.database,
                log=pipeline.log,
                correlation_id=run_id,
                dependencies=DigestDependencies(
                    emit=lambda event, level="info", **details: pipeline._event(
                        run_id, "digest", event, level, **details
                    ),
                    llm=FakeLlm(),
                    sender=sender,
                ),
            )
        ).deliver(run_id, counts)

    pipeline._send_digest = deliver  # type: ignore[method-assign]
    result = pipeline.sync()
    assert result["digest_posts"] == 1
    assert result["digest_chunks"] == 1
    assert sender.sent
    assert "Weekly digest" in sender.sent[0]
    assert "https://x.com/reader/status/901" in sender.sent[0]
    with Database(settings.database_path).connect() as connection:
        events = connection.execute(
            "SELECT event FROM run_events WHERE stage='digest'"
        ).fetchall()
    assert "digest_sent" in {row["event"] for row in events}


def test_sync_without_digest_config_skips_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Blank values outrank dotenv entries and normalize to missing.
    # delenv would restore the developer's local .env credentials here.
    monkeypatch.setenv("XDIGEST_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("XDIGEST_TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("TELEGRAM_USER_ID", "")
    monkeypatch.setenv("XDIGEST_LLM_API_KEY", "")
    settings = Settings(vault_path=tmp_path)
    result = Pipeline(settings, api=DigestApi()).sync()
    assert result["digest_skipped"] == 1
    assert "digest_posts" not in result
    with Database(settings.database_path).connect() as connection:
        status = connection.execute(
            "SELECT status FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        skipped = connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE event='digest_skipped'"
        ).fetchone()[0]
    assert status["status"] == "success"
    assert skipped == 1


def test_sync_keeps_archive_success_when_digest_fails(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pipeline = Pipeline(settings, api=DigestApi())
    sender = FakeSender(fail=RuntimeError("Telegram send failed: connection_error"))

    def deliver(run_id: str, counts: dict[str, int]) -> dict[str, object]:
        return DigestService(
            DigestContext(
                settings=settings,
                database=pipeline.database,
                log=pipeline.log,
                correlation_id=run_id,
                dependencies=DigestDependencies(
                    emit=lambda event, level="info", **details: pipeline._event(
                        run_id, "digest", event, level, **details
                    ),
                    llm=FakeLlm(),
                    sender=sender,
                ),
            )
        ).deliver(run_id, counts)

    pipeline._send_digest = deliver  # type: ignore[method-assign]
    result = pipeline.sync()
    assert result["digest_failed"] == 1
    with Database(settings.database_path).connect() as connection:
        status = connection.execute(
            "SELECT status FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        failed = connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE event='digest_failed'"
        ).fetchone()[0]
    assert status["status"] == "success"
    assert failed == 1


def test_bounded_sync_never_sends_digest(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pipeline = Pipeline(settings, api=DigestApi())
    sender = FakeSender()

    def fail_send(_run_id: str, _counts: dict[str, int]) -> dict[str, object]:
        raise AssertionError("bounded sync must not send")

    pipeline._send_digest = fail_send  # type: ignore[method-assign]
    result = pipeline.sync(max_pages=1)
    assert "digest_posts" not in result
    assert sender.sent == []
