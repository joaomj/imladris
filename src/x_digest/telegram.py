"""Telegram delivery with safe chunking, retries, and secret redaction."""

import time
from typing import Any

import keyring
import requests

from .config import TELEGRAM_BOT_TOKEN_ACCOUNT, TELEGRAM_CHAT_ID_ACCOUNT, Settings
from .digest import MAX_TELEGRAM_CHUNKS, TELEGRAM_CHUNK_LIMIT
from .logging_setup import JsonlLogger

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)
MAX_ATTEMPTS = 4
LOG_PREVIEW_CHARS = 200
HTTP_OK = 200
HTTP_RATE_LIMITED = 429
MAX_RETRY_AFTER_SECONDS = 60.0


def _error_category(status: int | None, error: Exception) -> str:
    """Classify a Telegram failure without preserving secrets or bodies."""
    if isinstance(error, requests.Timeout):
        return "timeout"
    if isinstance(error, requests.ConnectionError):
        return "connection_error"
    if isinstance(error, ValueError):
        return "invalid_response"
    if status is not None:
        return f"http_{status}"
    return "request_error"


def _retry_after_seconds(error: Exception, default: float) -> float:
    """Honor Telegram retry hints within a bounded delay."""
    response = error.response if isinstance(error, requests.RequestException) else None
    if response is None or response.status_code != HTTP_RATE_LIMITED:
        return default
    try:
        payload = response.json()
    except ValueError:
        return default
    parameters = payload.get("parameters") if isinstance(payload, dict) else None
    retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
    if isinstance(retry_after, bool):
        return default
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
    return default


class TelegramSender:
    """Send rendered HTML digests through the Telegram Bot API."""

    def __init__(
        self,
        settings: Settings,
        log: JsonlLogger | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.log = log
        self.correlation_id = correlation_id or "unknown"

    def resolve_credentials(self) -> tuple[str | None, str | None]:
        """Return bot token and chat ID, falling back to Keychain."""
        token = self.settings.telegram_bot_token
        chat_id = self.settings.telegram_chat_id
        if token is None:
            try:
                token = keyring.get_password(
                    self.settings.keychain_service, TELEGRAM_BOT_TOKEN_ACCOUNT
                )
            except Exception:
                token = None
        if chat_id is None:
            try:
                chat_id = keyring.get_password(
                    self.settings.keychain_service, TELEGRAM_CHAT_ID_ACCOUNT
                )
            except Exception:
                chat_id = None
        return token, chat_id

    @staticmethod
    def split_chunks(text: str) -> list[str]:
        """Split rendered HTML into complete blocks without breaking markup."""
        blocks = [block for block in text.split("\n\n") if block.strip()]
        if not blocks:
            return [text] if text.strip() else []
        chunks: list[str] = []
        current = ""
        for block in blocks:
            candidate = f"{current}\n\n{block}" if current else block
            if len(candidate) <= TELEGRAM_CHUNK_LIMIT:
                current = candidate
                continue
            if current:
                chunks.append(current)
            # A single oversized block is split on line boundaries only
            # when every line is itself within the limit.
            if len(block) > TELEGRAM_CHUNK_LIMIT:
                lines = block.split("\n")
                if all(len(line) <= TELEGRAM_CHUNK_LIMIT for line in lines):
                    current = ""
                    for line in lines:
                        candidate = f"{current}\n{line}" if current else line
                        if len(candidate) <= TELEGRAM_CHUNK_LIMIT:
                            current = candidate
                        else:
                            chunks.append(current)
                            current = line
                    continue
                raise ValueError("digest block exceeds Telegram message limit")
            current = block
        if current:
            chunks.append(current)
        if len(chunks) > MAX_TELEGRAM_CHUNKS:
            raise ValueError("digest exceeds Telegram chunk limit")
        return chunks

    def send_chunks(self, chunks: list[str], start: int = 0) -> int:
        """Send chunks starting at an index and return the sent count."""
        token, chat_id = self.resolve_credentials()
        if not token or not chat_id:
            raise ValueError("Telegram bot token and chat ID are required")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        sent = 0
        for index in range(start, len(chunks)):
            self._send_one(url, chat_id, chunks[index], index)
            sent += 1
        return sent

    def send(self, text: str) -> dict[str, int | str]:
        """Chunk and send rendered HTML from the beginning."""
        chunks = self.split_chunks(text)
        token, chat_id = self.resolve_credentials()
        if not token or not chat_id:
            raise ValueError("Telegram bot token and chat ID are required")
        sent = self.send_chunks(chunks, 0)
        return {"chunks": len(chunks), "sent": sent, "chat_id": str(chat_id)}

    def _send_one(self, url: str, chat_id: str, chunk: str, index: int) -> None:
        """Send one chunk with explicit retries and sanitized logging."""
        body = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = requests.post(
                    url, json=body, timeout=self.settings.telegram_timeout_seconds
                )
                if response.status_code == HTTP_OK and self._accepted(response):
                    if self.log:
                        self.log.emit(
                            self.correlation_id,
                            "telegram_chunk_sent",
                            "info",
                            chunk=index,
                            chars=len(chunk),
                            preview=chunk[:LOG_PREVIEW_CHARS],
                        )
                    return
                if response.status_code == HTTP_OK:
                    raise ValueError("Telegram response reported ok:false")
                raise self._response_error(response)
            except (requests.RequestException, ValueError) as error:
                last_error = error
                status = (
                    error.response.status_code
                    if isinstance(error, requests.RequestException)
                    and error.response is not None
                    else None
                )
                if not self._should_retry(status, attempt):
                    self._log_failed(index, status, error)
                    raise RuntimeError(
                        f"Telegram send failed: {_error_category(status, error)}"
                    ) from error
                delay = RETRY_DELAYS_SECONDS[min(attempt, len(RETRY_DELAYS_SECONDS) - 1)]
                self._log_retry(index, attempt, status, error)
                time.sleep(_retry_after_seconds(error, delay))
        raise RuntimeError("Telegram send failed: retry_exhausted") from last_error

    @staticmethod
    def _accepted(response: requests.Response) -> bool:
        """Return True when Telegram reports a successful delivery."""
        try:
            return bool(response.json().get("ok", False))
        except ValueError:
            return False

    @staticmethod
    def _response_error(response: requests.Response) -> requests.RequestException:
        """Wrap an unsuccessful HTTP response for retry classification."""
        error = requests.RequestException(f"HTTP {response.status_code}")
        error.response = response
        return error

    def _should_retry(self, status: int | None, attempt: int) -> bool:
        """Return True when another attempt is allowed for this status."""
        if status is None:
            return attempt < MAX_ATTEMPTS - 1
        return status in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS - 1

    def _log_retry(
        self, index: int, attempt: int, status: int | None, error: Exception
    ) -> None:
        """Log one retryable chunk failure without secrets."""
        if self.log:
            self.log.emit(
                self.correlation_id,
                "telegram_retry",
                "warning",
                chunk=index,
                attempt=attempt,
                status=status,
                category=_error_category(status, error),
            )

    def _log_failed(self, index: int, status: int | None, error: Exception) -> None:
        """Log a terminal chunk failure without secrets."""
        if self.log:
            self.log.emit(
                self.correlation_id,
                "telegram_failed",
                "warning",
                chunk=index,
                status=status,
                category=_error_category(status, error),
            )

    def safe_details(self, error: Exception) -> dict[str, Any]:
        """Return sanitized details for a Telegram failure."""
        status = (
            error.response.status_code
            if isinstance(error, requests.RequestException) and error.response is not None
            else None
        )
        return {"status": status, "category": _error_category(status, error)}
