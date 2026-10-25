"""Isolate tests from local dotenv credentials."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_digest_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
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
    # Env vars outrank dotenv values, but a Proton Pass lookup bypasses both.
    # Blank the vault so tests never read developer secrets or call pass-cli.
    monkeypatch.setenv("XDIGEST_SECRETS_VAULT", "")
    monkeypatch.setenv(
        "XDG_STATE_HOME", str(tmp_path_factory.mktemp("state-home"))
    )
