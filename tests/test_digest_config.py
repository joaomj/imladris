"""Tests for digest configuration aliases and credential normalization."""

import os
import subprocess
from pathlib import Path

DUMMY_TOKEN = "dummy-bot-token"
DUMMY_CHAT = "123456"
DUMMY_KEY = "dummy-openrouter-key"


def _settings_source(*, telegram_token: str, chat_id: str, llm_key: str) -> str:
    return (
        "from x_digest.config import load_settings\n"
        "settings = load_settings()\n"
        f"print(repr(settings.telegram_bot_token == {telegram_token!r}))\n"
        f"print(repr(settings.telegram_chat_id == {chat_id!r}))\n"
        f"print(repr(settings.llm_api_key == {llm_key!r}))\n"
        "print(repr(settings.telegram_enabled()))\n"
        "print(repr(settings.llm_enabled()))\n"
        "print(settings.llm_base_url)\n"
    )


def _run(source: str, environment: dict[str, str]) -> list[str]:
    result = subprocess.run(
        ["uv", "run", "python", "-c", source],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.splitlines()


def test_legacy_telegram_names_configure_delivery() -> None:
    environment = {
        **os.environ,
        "TELEGRAM_BOT_TOKEN": DUMMY_TOKEN,
        "TELEGRAM_USER_ID": DUMMY_CHAT,
        "XDIGEST_LLM_API_KEY": DUMMY_KEY,
    }
    environment.pop("XDIGEST_TELEGRAM_BOT_TOKEN", None)
    environment.pop("XDIGEST_TELEGRAM_CHAT_ID", None)
    lines = _run(
        _settings_source(
            telegram_token=DUMMY_TOKEN, chat_id=DUMMY_CHAT, llm_key=DUMMY_KEY
        ),
        environment,
    )
    assert lines == ["True", "True", "True", "True", "True", "https://openrouter.ai/api/v1"]


def test_prefixed_names_take_precedence_over_legacy_names() -> None:
    environment = {
        **os.environ,
        "XDIGEST_TELEGRAM_BOT_TOKEN": "prefixed-token",
        "XDIGEST_TELEGRAM_CHAT_ID": "prefixed-chat",
        "TELEGRAM_BOT_TOKEN": DUMMY_TOKEN,
        "TELEGRAM_USER_ID": DUMMY_CHAT,
    }
    lines = _run(
        _settings_source(
            telegram_token="prefixed-token", chat_id="prefixed-chat", llm_key=""
        ),
        environment,
    )
    assert lines[0] == "True"
    assert lines[1] == "True"


def test_blank_credentials_become_missing(tmp_path: Path) -> None:
    environment = {
        **os.environ,
        "XDIGEST_VAULT_PATH": str(tmp_path),
        "XDIGEST_TELEGRAM_BOT_TOKEN": "   ",
        "XDIGEST_TELEGRAM_CHAT_ID": "",
        "XDIGEST_LLM_API_KEY": "  ",
        "TELEGRAM_BOT_TOKEN": "unused",
        "TELEGRAM_USER_ID": "unused",
    }
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "-c",
            "from x_digest.config import load_settings\n"
            "settings = load_settings()\n"
            "print(repr(settings.telegram_bot_token))\n"
            "print(repr(settings.telegram_chat_id))\n"
            "print(repr(settings.llm_api_key))\n"
            "print(repr(settings.telegram_enabled()))\n"
            "print(repr(settings.llm_enabled()))\n",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.stdout.splitlines() == ["None", "None", "None", "False", "False"]


def test_settings_construct_directly_without_keyring() -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "-c",
            "from x_digest.config import Settings\n"
            "settings = Settings(\n"
            "    telegram_bot_token='direct-token',\n"
            "    telegram_chat_id='direct-chat',\n"
            "    llm_api_key='direct-key',\n"
            ")\n"
            "print(settings.telegram_enabled(), settings.llm_enabled())\n",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "True True"
