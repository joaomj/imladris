"""Authentication recovery through the CLI with isolated external services."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

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
    """Simulate HTTP boundaries and the on-disk token state."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.token_path = settings.state_dir / "oauth2.json"
        self.pkce_path = settings.state_dir / "pkce.json"
        self.requests: list[requests.PreparedRequest] = []
        self.messages: list[str] = []
        self.reject_refresh = False
        self.reject_user = False
        self.alert_timeout = False
        self.alert_attempts = 0
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(
            json.dumps(
                {
                    "access_token": OLD_ACCESS,
                    "refresh_token": OLD_REFRESH,
                    "token_type": "bearer",
                    "expires_at": 0,
                    "scope": DEFAULT_X_SCOPE,
                }
            ),
            encoding="utf-8",
        )

    def saved_token(self) -> dict[str, Any]:
        return json.loads(self.token_path.read_text(encoding="utf-8"))

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
        state_dir=tmp_path / "state",
        secrets_vault=None,
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
    saved = services.saved_token()
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
    services.pkce_path.write_text(
        json.dumps({"state": "login-state", "verifier": PKCE_VERIFIER}), encoding="utf-8"
    )
    before = services.saved_token()
    callback = f"http://localhost:8080/callback?state=login-state&code={LOGIN_CODE}"
    with ProcessLock(services.settings.lock_path):
        assert main(["auth", "--callback-url", callback]) == CLI_FAILURE
        assert services.saved_token() == before
        assert services.requests == []
    assert main(["auth", "--callback-url", callback]) == 0
    saved = services.saved_token()
    assert saved["access_token"] == NEW_ACCESS
    assert saved["refresh_token"] == NEW_REFRESH
    assert not services.pkce_path.exists()
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
