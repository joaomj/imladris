"""OpenRouter-compatible LLM client with retries and secret redaction."""

import time
from typing import Any

import keyring
import requests

from .config import OPENROUTER_API_KEY_ACCOUNT, Settings
from .logging_setup import JsonlLogger

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)
MAX_ATTEMPTS = 4
ERROR_PREVIEW_CHARS = 500
LLM_TEMPERATURE = 0.3
HTTP_OK = 200


def _error_category(status: int | None, error: Exception) -> str:
    """Classify an LLM failure without preserving secrets or bodies."""
    if isinstance(error, requests.Timeout):
        return "timeout"
    if isinstance(error, requests.ConnectionError):
        return "connection_error"
    if status is not None:
        return f"http_{status}"
    return "request_error"


class LlmClient:
    """Call an OpenAI-compatible chat endpoint with explicit retry behavior."""

    def __init__(
        self,
        settings: Settings,
        log: JsonlLogger | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.log = log
        self.correlation_id = correlation_id or "unknown"

    def resolve_api_key(self) -> str | None:
        """Return the configured key, falling back to Keychain."""
        if self.settings.llm_api_key:
            return self.settings.llm_api_key
        try:
            return keyring.get_password(
                self.settings.keychain_service, OPENROUTER_API_KEY_ACCOUNT
            )
        except Exception:
            return None

    def complete(self, prompt: str, system: str) -> str:
        """Request one completion and return the stripped response text."""
        api_key = self.resolve_api_key()
        if not api_key:
            raise ValueError("XDIGEST_LLM_API_KEY is required")
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/x-digest",
            "X-Title": "x-digest",
        }
        body = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": self.settings.llm_max_tokens,
            "temperature": LLM_TEMPERATURE,
            "zdr": True,
            "provider": {"sort": "price", "zdr": True, "data_collection": "deny"},
        }
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            if self.log:
                self.log.emit(
                    self.correlation_id,
                    "llm_attempt",
                    "debug",
                    attempt=attempt,
                    model=self.settings.llm_model,
                )
            try:
                response = requests.post(
                    url, headers=headers, json=body, timeout=self.settings.llm_timeout_seconds
                )
                outcome = self._response_text(response)
                if isinstance(outcome, str):
                    return outcome
                self._log_retry(attempt, outcome)
            except requests.RequestException as error:
                last_error = error
                if not self._handle_transport_error(error, attempt):
                    raise RuntimeError(
                        f"LLM request failed: {_error_category(self._status_of(error), error)}"
                    ) from error
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(RETRY_DELAYS_SECONDS[min(attempt, len(RETRY_DELAYS_SECONDS) - 1)])
        raise RuntimeError("LLM request failed: retry_exhausted") from last_error

    def _response_text(self, response: requests.Response) -> str | int:
        """Return response text for success or status for a retryable failure."""
        status = response.status_code
        if status == HTTP_OK:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("LLM response contained no text")
            return content.strip()
        if status not in RETRYABLE_STATUSES:
            raise RuntimeError(
                f"LLM request failed with HTTP {status}: "
                f"{response.text[:ERROR_PREVIEW_CHARS]}"
            )
        return status

    def _log_retry(self, attempt: int, status: int) -> None:
        """Log one retryable HTTP status."""
        if self.log:
            self.log.emit(
                self.correlation_id,
                "llm_retry",
                "warning",
                attempt=attempt,
                status=status,
                category=f"http_{status}",
            )

    @staticmethod
    def _status_of(error: requests.RequestException) -> int | None:
        """Return the HTTP status attached to a transport error, if any."""
        if error.response is not None:
            return int(error.response.status_code)
        return None

    def _handle_transport_error(self, error: requests.RequestException, attempt: int) -> bool:
        """Log a transport error and return True when another attempt is allowed."""
        status = self._status_of(error)
        retryable = status is None or status in RETRYABLE_STATUSES
        if not retryable or attempt >= MAX_ATTEMPTS - 1:
            if self.log:
                self.log.emit(
                    self.correlation_id,
                    "llm_failed",
                    "warning",
                    attempt=attempt,
                    status=status,
                    category=_error_category(status, error),
                )
            return False
        if self.log:
            self.log.emit(
                self.correlation_id,
                "llm_retry",
                "warning",
                attempt=attempt,
                status=status,
                category=_error_category(status, error),
            )
        return True

    def request_body(self) -> dict[str, Any]:
        """Return the request body shape for tests without network access."""
        return {
            "model": self.settings.llm_model,
            "max_tokens": self.settings.llm_max_tokens,
            "temperature": LLM_TEMPERATURE,
            "zdr": True,
            "provider": {"sort": "price", "zdr": True, "data_collection": "deny"},
        }
