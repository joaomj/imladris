"""Isolate tests from local dotenv credentials."""

import keyring
import pytest


@pytest.fixture(autouse=True)
def _isolate_digest_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override dotenv-provided secrets with blank values.

    Pydantic-settings prioritizes environment variables over the `.env`
    file, and blank credentials normalize to missing. This keeps the
    developer's local `.env` from enabling Telegram/LLM delivery inside
    pipeline tests that construct bare Settings objects.
    """
    monkeypatch.setenv("XDIGEST_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("XDIGEST_TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_USER_ID", "")
    monkeypatch.setenv("XDIGEST_LLM_API_KEY", "")
    # Env vars outrank dotenv values, but keyring-backed secrets bypass
    # both. Neutralize the fallback so tests never read developer keys.
    # delete_mock=True keeps module-level `import keyring` bindings intact.
    monkeypatch.setattr(keyring, "get_password", lambda _service, _account: None)
    for module in ("x_digest.llm", "x_digest.telegram"):
        monkeypatch.setattr(
            f"{module}.keyring.get_password", lambda _service, _account: None
        )
