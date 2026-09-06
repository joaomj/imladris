"""Tests for the OpenRouter client without live network calls."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from x_digest.config import Settings
from x_digest.llm import LlmClient
from x_digest.logging_setup import JsonlLogger

DUMMY_KEY = "dummy-openrouter-key"
EXPECTED_TIMEOUT = 30.0
EXPECTED_RETRY_CALLS = 2


def _settings(vault: Path) -> Settings:
    return Settings(vault_path=vault, llm_api_key=DUMMY_KEY)


def _response(
    status: int, payload: dict[str, object] | None = None, text: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status,
        text=text or json.dumps(payload or {}),
        json=lambda: payload or {},
    )


def test_llm_request_uses_zdr_cheapest_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_post(
        url: str, headers: dict[str, str], json: dict[str, object], timeout: float
    ) -> SimpleNamespace:
        captured.update(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return _response(
            200, {"choices": [{"message": {"content": "  Summary  "}}]}
        )

    monkeypatch.setattr("x_digest.llm.requests.post", fake_post)
    result = LlmClient(_settings(tmp_path)).complete("prompt", "system")
    assert result == "Summary"
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["zdr"] is True
    assert body["provider"] == {"sort": "price", "zdr": True, "data_collection": "deny"}
    assert captured["headers"]["Authorization"] == f"Bearer {DUMMY_KEY}"
    assert captured["timeout"] == EXPECTED_TIMEOUT


def test_llm_rate_limit_retries_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"count": 0}

    def fake_post(*args: Any, **kwargs: Any) -> SimpleNamespace:  # noqa: ARG001
        calls["count"] += 1
        if calls["count"] == 1:
            return _response(429, text="rate limited")
        return _response(200, {"choices": [{"message": {"content": "Recovered"}}]})

    monkeypatch.setattr("x_digest.llm.requests.post", fake_post)
    monkeypatch.setattr("x_digest.llm.time.sleep", lambda _seconds: None)
    result = LlmClient(_settings(tmp_path)).complete("prompt", "system")
    assert result == "Recovered"
    assert calls["count"] == EXPECTED_RETRY_CALLS


def test_llm_never_logs_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(*args: Any, **kwargs: Any) -> SimpleNamespace:  # noqa: ARG001
        raise requests.ConnectionError("network down")

    monkeypatch.setattr("x_digest.llm.requests.post", fake_post)
    monkeypatch.setattr("x_digest.llm.time.sleep", lambda _seconds: None)
    log = JsonlLogger(tmp_path / "logs" / "application.jsonl", level="debug")
    try:
        LlmClient(_settings(tmp_path), log, "llm-run").complete("prompt", "system")
    except RuntimeError:
        pass
    else:
        raise AssertionError("connection failure must raise")
    log.end_run()
    content = (tmp_path / "logs" / "application.jsonl").read_text(encoding="utf-8")
    assert DUMMY_KEY not in content
    assert "llm_failed" in content


def test_llm_missing_key_fails_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_post(*args: Any, **kwargs: Any) -> SimpleNamespace:  # noqa: ARG001
        raise AssertionError("network must not be used")

    monkeypatch.setattr("x_digest.llm.requests.post", fail_post)
    monkeypatch.setattr("x_digest.llm.keyring.get_password", lambda _s, _a: None)
    # delenv restores the dotenv value (env outranks .env only while set);
    # blank it instead so Settings sees an explicit missing credential.
    monkeypatch.setenv("XDIGEST_LLM_API_KEY", "")
    try:
        LlmClient(Settings(vault_path=tmp_path)).complete("prompt", "system")
    except ValueError as error:
        assert "XDIGEST_LLM_API_KEY" in str(error)
    else:
        raise AssertionError("missing key must fail fast")
