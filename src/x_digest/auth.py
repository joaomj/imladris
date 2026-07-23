"""OAuth 2.0 PKCE and macOS Keychain token storage."""

import json
import secrets
import urllib.parse
from typing import Any

import keyring
from xdk import Client

from .config import Settings


class AuthError(RuntimeError):
    """Raised when X authentication cannot proceed."""


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


def build_client(settings: Settings, token: dict[str, Any] | None = None) -> Client:
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
        refreshed = client.refresh_token()
        TokenStore(settings).save(refreshed)
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


def exchange_callback(settings: Settings, callback_url: str) -> None:
    """Exchange a copied callback URL and store the resulting token."""
    client = build_client(settings)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query)
    code = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    exchange = TokenStore(settings).load_pkce()
    if not code or not state or not exchange or state != exchange.get("state"):
        raise AuthError("OAuth callback state is missing or does not match")
    token = client.exchange_code(code, exchange["verifier"])
    if not token:
        raise AuthError("X returned no OAuth token")
    store = TokenStore(settings)
    store.save(token)
    store.clear_pkce()


def authenticated_client(settings: Settings) -> Client:
    """Build a client from the stored token."""
    token = TokenStore(settings).load()
    if not token:
        raise AuthError("No X token found; run the auth command first")
    return build_client(settings, token)
