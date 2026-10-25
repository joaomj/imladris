"""Tests for digest configuration aliases and credential normalization."""

from pathlib import Path

import pytest

from x_digest.config import load_settings

DUMMY_TOKEN = "dummy-bot-token"
DUMMY_CHAT = "123456"
DUMMY_KEY = "dummy-openrouter-key"
DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"


def test_legacy_telegram_names_configure_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    # A blank prefixed value exists in the fixture environment and would win
    # the alias lookup, so remove it to prove the legacy names alone work.
    monkeypatch.delenv("XDIGEST_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("XDIGEST_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", DUMMY_TOKEN)
    monkeypatch.setenv("TELEGRAM_USER_ID", DUMMY_CHAT)
    monkeypatch.setenv("XDIGEST_LLM_API_KEY", DUMMY_KEY)
    settings = load_settings()
    assert settings.telegram_bot_token == DUMMY_TOKEN
    assert settings.telegram_chat_id == DUMMY_CHAT
    assert settings.llm_api_key == DUMMY_KEY
    assert settings.telegram_enabled() is True
    assert settings.llm_enabled() is True
    assert settings.llm_base_url == DEFAULT_LLM_BASE_URL


def test_prefixed_names_take_precedence_over_legacy_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDIGEST_TELEGRAM_BOT_TOKEN", "prefixed-token")
    monkeypatch.setenv("XDIGEST_TELEGRAM_CHAT_ID", "prefixed-chat")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", DUMMY_TOKEN)
    monkeypatch.setenv("TELEGRAM_USER_ID", DUMMY_CHAT)
    settings = load_settings()
    assert settings.telegram_bot_token == "prefixed-token"
    assert settings.telegram_chat_id == "prefixed-chat"


def test_blank_credentials_become_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDIGEST_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("XDIGEST_TELEGRAM_BOT_TOKEN", "   ")
    monkeypatch.setenv("XDIGEST_TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("XDIGEST_LLM_API_KEY", "  ")
    # A blank vault keeps llm_enabled() from reading developer secrets.
    monkeypatch.setenv("XDIGEST_SECRETS_VAULT", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unused")
    monkeypatch.setenv("TELEGRAM_USER_ID", "unused")
    settings = load_settings()
    assert settings.telegram_bot_token is None
    assert settings.telegram_chat_id is None
    assert settings.llm_api_key is None
    assert settings.telegram_enabled() is False
    assert settings.llm_enabled() is False
