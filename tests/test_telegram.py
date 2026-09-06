"""Tests for Telegram chunking and redacted retries without live sends."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from x_digest.config import Settings
from x_digest.digest import TELEGRAM_CHUNK_LIMIT
from x_digest.logging_setup import JsonlLogger
from x_digest.telegram import TelegramSender

DUMMY_TOKEN = "dummy-bot-token"
DUMMY_CHAT = "123456"
EXPECTED_SINGLE_CHUNK = 1
EXPECTED_TWO_CHUNKS = 2
EXPECTED_ATTEMPTS_AFTER_RETRY = 2


def _settings(vault: Path) -> Settings:
    return Settings(
        vault_path=vault, telegram_bot_token=DUMMY_TOKEN, telegram_chat_id=DUMMY_CHAT
    )


def _ok_response() -> SimpleNamespace:
    return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {}})


def test_telegram_chunks_long_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> SimpleNamespace:  # noqa: ARG001
        sent.append(kwargs["json"])
        return _ok_response()

    monkeypatch.setattr("x_digest.telegram.requests.post", fake_post)
    block_one = "<b>Theme one</b>\n" + ("x" * 2500)
    block_two = "<b>Theme two</b>\n" + ("y" * 2500)
    sender = TelegramSender(_settings(tmp_path))
    result = sender.send(f"{block_one}\n\n{block_two}")
    assert result["chunks"] == EXPECTED_TWO_CHUNKS
    assert result["sent"] == EXPECTED_TWO_CHUNKS
    assert len(sent) == EXPECTED_TWO_CHUNKS
    assert all(len(item["text"]) <= TELEGRAM_CHUNK_LIMIT for item in sent)
    assert all(item["parse_mode"] == "HTML" for item in sent)
    assert sent[0]["text"].startswith("<b>Theme one</b>")


def test_telegram_retries_on_429(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"count": 0}

    def fake_post(*args: Any, **kwargs: Any) -> SimpleNamespace:  # noqa: ARG001
        calls["count"] += 1
        if calls["count"] == 1:
            response = SimpleNamespace(
                status_code=429,
                json=lambda: {"ok": False, "parameters": {"retry_after": 0}},
            )
            error = requests.RequestException("rate limited")
            error.response = response
            raise error
        return _ok_response()

    monkeypatch.setattr("x_digest.telegram.requests.post", fake_post)
    monkeypatch.setattr("x_digest.telegram.time.sleep", lambda _seconds: None)
    result = TelegramSender(_settings(tmp_path)).send("<b>Digest</b>")
    assert result["chunks"] == EXPECTED_SINGLE_CHUNK
    assert calls["count"] == EXPECTED_ATTEMPTS_AFTER_RETRY


def test_telegram_never_logs_token_or_full_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_post(*args: Any, **kwargs: Any) -> SimpleNamespace:  # noqa: ARG001
        raise requests.ConnectionError("network down")

    monkeypatch.setattr("x_digest.telegram.requests.post", fake_post)
    monkeypatch.setattr("x_digest.telegram.time.sleep", lambda _seconds: None)
    log = JsonlLogger(tmp_path / "logs" / "application.jsonl", level="debug")
    try:
        TelegramSender(_settings(tmp_path), log, "telegram-run").send("<b>Digest</b>")
    except RuntimeError:
        pass
    else:
        raise AssertionError("connection failure must raise")
    log.end_run()
    content = (tmp_path / "logs" / "application.jsonl").read_text(encoding="utf-8")
    assert DUMMY_TOKEN not in content
    assert DUMMY_CHAT not in content
    assert "telegram_failed" in content
    assert json.loads(content.splitlines()[-1])["category"] == "connection_error"


def test_telegram_rejects_unexpected_ok_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_post(*args: Any, **kwargs: Any) -> SimpleNamespace:  # noqa: ARG001
        return SimpleNamespace(status_code=200, json=lambda: {"ok": False})

    monkeypatch.setattr("x_digest.telegram.requests.post", fake_post)
    monkeypatch.setattr("x_digest.telegram.time.sleep", lambda _seconds: None)
    try:
        TelegramSender(_settings(tmp_path)).send("<b>Digest</b>")
    except RuntimeError as error:
        assert "invalid_response" in str(error)
    else:
        raise AssertionError("ok:false must fail")


def test_telegram_resumes_after_first_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    def fake_post(*args: Any, **kwargs: Any) -> SimpleNamespace:  # noqa: ARG001
        sent.append(kwargs["json"]["text"])
        return _ok_response()

    monkeypatch.setattr("x_digest.telegram.requests.post", fake_post)
    sender = TelegramSender(_settings(tmp_path))
    chunks = ["<b>One</b>", "<b>Two</b>"]
    assert sender.send_chunks(chunks, start=1) == 1
    assert sent == ["<b>Two</b>"]
