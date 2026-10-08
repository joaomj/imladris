"""OAuth 2.0 PKCE and macOS Keychain token storage."""

import json
import secrets
import urllib.parse
from typing import Any

import keyring
from oauthlib.oauth2 import OAuth2Error
from xdk import Client

from .config import Settings
from .lock import ProcessLock
from .logging_setup import JsonlLogger


class AuthError(RuntimeError):
    """Raised when X authentication cannot proceed."""


class XAuthenticationError(AuthError):
    """Raised when X access requires authorization again."""


class TokenStore:
    """Store one OAuth token in the system credential store."""

    def __init__(self, settings: Settings) -> None:
        self.service = settings.keychain_service
        self.account = "oauth2"
        self.pkce_account = "pkce"

    def load(self) -> dict[str, Any] | None:
        """Load the token, if it exists."""
        value = keyring.get_password(self.service, self.account)
        return json.loads(value) if value else None

    def save(self, token: dict[str, Any]) -> None:
        """Save the token to the system credential store."""
        keyring.set_password(self.service, self.account, json.dumps(token))

    def save_pkce(self, verifier: str, state: str) -> None:
        """Store the short-lived PKCE exchange state."""
        keyring.set_password(
            self.service,
            self.pkce_account,
            json.dumps({"verifier": verifier, "state": state}),
        )

    def load_pkce(self) -> dict[str, str] | None:
        """Load the short-lived PKCE exchange state."""
        value = keyring.get_password(self.service, self.pkce_account)
        return json.loads(value) if value else None

    def clear_pkce(self) -> None:
        """Remove the short-lived PKCE exchange state."""
        keyring.delete_password(self.service, self.pkce_account)


def build_client(
    settings: Settings,
    token: dict[str, Any] | None = None,
    *,
    log: JsonlLogger | None = None,
    correlation_id: str = "unknown",
) -> Client:
    """Build an official XDK client for user-context reads."""
    if not settings.x_client_id:
        raise AuthError("XDIGEST_X_CLIENT_ID is required")
    client = Client(
        client_id=settings.x_client_id,
        client_secret=settings.x_client_secret,
        redirect_uri=settings.x_redirect_uri,
        token=token,
        scope=settings.x_scope.split(),
    )
    if token and client.is_token_expired():
        log = log or JsonlLogger(
            settings.log_path, settings.log_level, settings.log_max_bytes, settings.log_backups
        )
        log.emit(correlation_id, "oauth_refresh_started", expires_at=token.get("expires_at"))
        try:
            refreshed = client.refresh_token()
        except Exception as error:
            category = error.error if isinstance(error, OAuth2Error) else type(error).__name__
            log.emit(correlation_id, "oauth_refresh_failed", "error", category=category)
            if isinstance(error, OAuth2Error) and error.error in {
                "invalid_grant",
                "invalid_request",
                "invalid_client",
                "unauthorized_client",
                "access_denied",
            }:
                raise XAuthenticationError(
                    f"X rejected credential refresh ({category}); run uv run x-digest auth"
                ) from error
            raise
        try:
            TokenStore(settings).save(refreshed)
        except Exception as error:
            log.emit(
                correlation_id,
                "oauth_token_save_failed",
                "error",
                reason="refresh",
                category=type(error).__name__,
            )
            raise XAuthenticationError(
                "X refreshed access, but saving credentials to Keychain failed; "
                "fix Keychain access and run uv run x-digest auth"
            ) from error
        log.emit(
            correlation_id,
            "oauth_token_saved",
            reason="refresh",
            expires_at=refreshed.get("expires_at"),
            refresh_token_rotated=refreshed.get("refresh_token") != token.get("refresh_token"),
        )
    return client


def authorization_url(settings: Settings) -> str:
    """Create an OAuth authorization URL and persist the PKCE verifier in XDK."""
    client = build_client(settings)
    state = secrets.token_urlsafe(32)
    url = client.get_authorization_url(state=state)
    verifier = client.oauth2_auth.code_verifier if client.oauth2_auth else None
    if not verifier:
        raise AuthError("XDK did not create a PKCE verifier")
    TokenStore(settings).save_pkce(verifier, state)
    return url


def exchange_callback(
    settings: Settings, callback_url: str, log: JsonlLogger, correlation_id: str
) -> None:
    """Exchange and save login credentials while holding the pipeline lock."""
    with ProcessLock(settings.lock_path):
        client = build_client(settings)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query)
        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]
        exchange = TokenStore(settings).load_pkce()
        if not code or not state or not exchange or state != exchange.get("state"):
            raise AuthError("OAuth callback state is missing or does not match")
        log.emit(correlation_id, "oauth_authorization_exchange_started")
        credentials = client.exchange_code(code, exchange["verifier"])
        if not credentials:
            raise AuthError("X returned no OAuth token")
        store = TokenStore(settings)
        try:
            store.save(credentials)
        except Exception as error:
            log.emit(
                correlation_id,
                "oauth_token_save_failed",
                "error",
                reason="authorization",
                category=type(error).__name__,
            )
            raise AuthError(
                "Saving X credentials to Keychain failed; fix Keychain access and authorize again"
            ) from error
        log.emit(
            correlation_id,
            "oauth_token_saved",
            reason="authorization",
            expires_at=credentials.get("expires_at"),
        )
        store.clear_pkce()


def authenticated_client(settings: Settings, log: JsonlLogger, correlation_id: str) -> Client:
    """Build an authenticated client while the caller holds the pipeline lock."""
    credentials = TokenStore(settings).load()
    if not credentials:
        raise XAuthenticationError("No X token found; run uv run x-digest auth")
    return build_client(settings, credentials, log=log, correlation_id=correlation_id)
