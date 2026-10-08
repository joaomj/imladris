"""Authentication recovery through the CLI with isolated external services."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import keyring
import pytest
import requests

from x_digest.cli import main
from x_digest.config import DEFAULT_X_SCOPE, Settings
from x_digest.db import Database, utc_now
from x_digest.lock import ProcessLock

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
CLI_FAILURE = 1
OLD_ACCESS = "old-access-marker"
OLD_REFRESH = "old-refresh-marker"
NEW_ACCESS = "new-access-marker"
NEW_REFRESH = "new-refresh-marker"
CLIENT_SECRET = "client-secret-marker"
BOT_CREDENTIAL = "bot-credential-marker"
LLM_CREDENTIAL = "llm-credential-marker"
LOGIN_CODE = "login-code-marker"
PKCE_VERIFIER = "pkce-verifier-marker"


class ExternalServices:
    """Simulate HTTP and Keychain boundaries, not application collaborators."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.keychain: dict[tuple[str, str], str] = {}
        self.requests: list[requests.PreparedRequest] = []
        self.messages: list[str] = []
        self.reject_refresh = False
        self.reject_user = False
        self.alert_timeout = False
        self.alert_attempts = 0
        self.keychain[(settings.keychain_service, "oauth2")] = json.dumps(
            {
                "access_token": OLD_ACCESS,
                "refresh_token": OLD_REFRESH,
                "token_type": "bearer",
                "expires_at": 0,
                "scope": DEFAULT_X_SCOPE,
            }
        )

    def get_password(self, service: str, account: str) -> str | None:
        return self.keychain.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.keychain[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        del self.keychain[(service, account)]

    def send(self, request: requests.PreparedRequest) -> requests.Response:
        self.requests.append(request)
        path = urlparse(str(request.url)).path
        status = HTTP_OK
        if path == "/2/oauth2/token":
            fields = parse_qs(request.body or "")
            if fields["grant_type"] == ["refresh_token"]:
                assert fields["refresh_token"] == [OLD_REFRESH]
                if self.reject_refresh:
                    status = HTTP_BAD_REQUEST
                    payload = {"error": "invalid_request", "error_description": OLD_REFRESH}
                else:
                    payload = self._credentials()
            else:
                assert fields["code"] == [LOGIN_CODE]
                assert fields["code_verifier"] == [PKCE_VERIFIER]
                payload = self._credentials()
        elif path == "/2/users/me":
            status = HTTP_UNAUTHORIZED if self.reject_user else HTTP_OK
            payload = (
                {"title": "Unauthorized"}
                if self.reject_user
                else {"data": {"id": "owner", "name": "Owner", "username": "owner"}}
            )
        elif path == "/2/users/owner/bookmarks":
            payload = {"data": [], "meta": {}}
        elif path == "/api/v1/chat/completions":
            summary = {
                "takeaway": "The archived post is still available.",
                "themes": [
                    {
                        "title": "Archive",
                        "points": [{"text": "An archived finding", "post_ids": ["901"]}],
                    }
                ],
            }
            payload = {"choices": [{"message": {"content": json.dumps(summary)}}]}
        elif path.endswith("/sendMessage"):
            text = json.loads(request.body)["text"]
            if "authorization needs attention" in text:
                self.alert_attempts += 1
                if self.alert_timeout:
                    raise requests.Timeout("Delivery acknowledgment was lost")
            self.messages.append(text)
            payload = {"ok": True, "result": {"message_id": len(self.messages)}}
        else:
            raise AssertionError(f"Unexpected HTTP request: {path}")
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode()
        response.headers["Content-Type"] = "application/json"
        response.request = request
        response.url = request.url
        return response

    @staticmethod
    def _credentials() -> dict[str, Any]:
        return {
            "access_token": NEW_ACCESS,
            "refresh_token": NEW_REFRESH,
            "token_type": "bearer",
            "expires_in": 7200,
            "scope": DEFAULT_X_SCOPE,
        }

    def log_events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.settings.log_path.read_text().splitlines()]


@pytest.fixture
def services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ExternalServices]:
    settings = Settings(
        _env_file=None,
        vault_path=tmp_path,
        keychain_service="isolated-auth-regression",
        x_client_id="isolated-client",
        **{
            "x_client_secret": CLIENT_SECRET,
            "telegram_bot_token": BOT_CREDENTIAL,
            "telegram_chat_id": "123456",
            "llm_api_key": LLM_CREDENTIAL,
        },
    )
    boundary = ExternalServices(settings)
    monkeypatch.setattr("x_digest.cli.load_settings", lambda: settings)
    monkeypatch.setattr(keyring, "get_password", boundary.get_password)
    monkeypatch.setattr(keyring, "set_password", boundary.set_password)
    monkeypatch.setattr(keyring, "delete_password", boundary.delete_password)

    def send(
        _session: requests.Session, request: requests.PreparedRequest, **_kwargs: Any
    ) -> requests.Response:
        return boundary.send(request)

    monkeypatch.setattr(requests.Session, "send", send)
    logger = logging.getLogger("x_digest")
    original_handlers = logger.handlers[:]
    original_level = logger.level
    try:
        yield boundary
    finally:
        for handler in logger.handlers[:]:
            if handler not in original_handlers:
                logger.removeHandler(handler)
                handler.close()
        logger.setLevel(original_level)


def test_refresh_persists_replacement_and_logs_without_credentials(
    services: ExternalServices, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["sync", "--max-pages", "1"]) == 0
    saved = json.loads(services.keychain[(services.settings.keychain_service, "oauth2")])
    assert saved["access_token"] == NEW_ACCESS
    assert saved["refresh_token"] == NEW_REFRESH
    events = services.log_events()
    started = next(event for event in events if event["event"] == "oauth_refresh_started")
    completed = next(event for event in events if event["event"] == "oauth_token_saved")
    assert started["correlation_id"] == completed["correlation_id"]
    assert completed["reason"] == "refresh"
    assert completed["refresh_token_rotated"] is True
    assert completed["expires_at"] == saved["expires_at"]
    output = capsys.readouterr()
    logs = services.settings.log_path.read_text() + output.out + output.err
    for credential in (OLD_ACCESS, OLD_REFRESH, NEW_ACCESS, NEW_REFRESH, CLIENT_SECRET):
        assert credential not in logs


def test_login_callback_cannot_overwrite_credentials_during_sync(
    services: ExternalServices, capsys: pytest.CaptureFixture[str]
) -> None:
    service = services.settings.keychain_service
    services.keychain[(service, "pkce")] = json.dumps(
        {"state": "login-state", "verifier": PKCE_VERIFIER}
    )
    before = dict(services.keychain)
    callback = f"http://localhost:8080/callback?state=login-state&code={LOGIN_CODE}"
    with ProcessLock(services.settings.lock_path):
        assert main(["auth", "--callback-url", callback]) == CLI_FAILURE
        assert services.keychain == before
        assert services.requests == []
    assert main(["auth", "--callback-url", callback]) == 0
    saved = json.loads(services.keychain[(service, "oauth2")])
    assert saved["access_token"] == NEW_ACCESS
    assert saved["refresh_token"] == NEW_REFRESH
    assert (service, "pkce") not in services.keychain
    output = capsys.readouterr()
    logs = services.settings.log_path.read_text() + output.out + output.err
    for credential in (LOGIN_CODE, PKCE_VERIFIER, NEW_ACCESS, NEW_REFRESH):
        assert credential not in logs


@pytest.mark.parametrize("failure", ["refresh-rejected", "api-unauthorized", "alert-timeout"])
def test_authentication_failure_reports_failure_and_delivers_archive(
    services: ExternalServices, failure: str, capsys: pytest.CaptureFixture[str]
) -> None:
    services.reject_refresh = failure != "api-unauthorized"
    services.reject_user = failure == "api-unauthorized"
    services.alert_timeout = failure == "alert-timeout"
    database = Database(services.settings.database_path)
    database.initialize()
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO posts(post_id, username, created_at, url, text,
               content_state, current_content_hash, first_seen_at, last_seen_at)
               VALUES ('901', 'reader', '2026-08-01T00:00:00Z',
               'https://x.com/reader/status/901', 'Archived finding',
               'complete', 'hash-901', ?, ?)""",
            (utc_now(), utc_now()),
        )
    assert main(["sync"]) == CLI_FAILURE
    assert services.alert_attempts == 1
    digests = [text for text in services.messages if "Weekly digest" in text]
    assert len(digests) == 1
    assert "https://x.com/reader/status/901" in digests[0]
    if failure != "alert-timeout":
        alerts = [text for text in services.messages if "authorization needs attention" in text]
        assert len(alerts) == 1
        assert "uv run x-digest auth" in alerts[0]
    with database.connect() as connection:
        run = connection.execute("SELECT status, counts_json FROM runs").fetchone()
    assert run["status"] == "failed"
    counts = json.loads(run["counts_json"])
    assert counts["digest_posts"] == 1
    assert counts["digest_backlog"] == 0
    if failure == "alert-timeout":
        assert counts["auth_alert_failed"] == 1
    else:
        assert counts["auth_alert_sent"] == 1
    assert main(["digest", "--dry-run"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["pending_total"] == 0
    logs = services.settings.log_path.read_text() + output.err
    for credential in (OLD_REFRESH, NEW_REFRESH, CLIENT_SECRET, BOT_CREDENTIAL, LLM_CREDENTIAL):
        assert credential not in logs
