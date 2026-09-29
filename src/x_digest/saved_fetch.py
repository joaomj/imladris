"""Fetch saved URLs through approved deterministic routes.

PDF bytes are stored unchanged. PDF text is never extracted. Webpages use
the Donsetch CLI with a fixed flag set; any ok/content_ok failure or
truncation marker is an explicit failure, never a silent success.
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urljoin, urlparse

import requests

from .pubmed import parse_pubmed_record, pubmed_api_url, pubmed_pmid
from .saved_urls import (
    SAVED_URL_EXCLUSION_REASON,
    SavedUrlError,
    assert_public_url,
    is_excluded_saved_url,
    validate_saved_url,
)

if TYPE_CHECKING:
    from .config import Settings

MAX_REDIRECTS = 5
PROBE_CHUNK_BYTES = 65536

FetchKind = Literal["pdf", "page", "api_record", "failure", "skipped"]


class SavedUrlSkipped(Exception):
    """The URL belongs to a source excluded from webpage collection."""


class SavedFetchError(RuntimeError):
    """Raised when a saved URL cannot be fetched honestly."""

    def __init__(self, message: str, stage: str = "fetch", raw: dict[str, Any] | None = None):
        super().__init__(message)
        self.stage = stage
        self.raw = raw


@dataclass(frozen=True)
class DonsetchConfig:
    """Validated Donsetch invocation settings."""

    binary: str
    max_chars: int
    deadline_ms: int
    timeout_seconds: float


@dataclass(frozen=True)
class FetchOutcome:
    """Result of routing one saved URL, without any persistence."""

    kind: FetchKind
    original_url: str
    final_url: str | None
    content_type: str | None
    content_hash: str | None
    content_text: str | None
    pdf_bytes: bytes | None
    truncated: bool
    error: str | None
    stage: str
    raw: dict[str, Any] | None


def failure_outcome(
    url: str,
    error: str,
    stage: str,
    raw: dict[str, Any] | None = None,
    truncated: bool = False,
) -> FetchOutcome:
    """Build an explicit failure outcome that pretends nothing."""
    return FetchOutcome(
        kind="failure",
        original_url=url,
        final_url=None,
        content_type=None,
        content_hash=None,
        content_text=None,
        pdf_bytes=None,
        truncated=truncated,
        error=error,
        stage=stage,
        raw=raw,
    )


def is_pdf(content_type: str | None, head: bytes) -> bool:
    """Return True for PDF Content-Type headers or %PDF magic bytes."""
    if content_type and "application/pdf" in content_type.lower():
        return True
    return head[:4] == b"%PDF"


def _check_content_length(value: str | None, max_bytes: int) -> None:
    """Fail explicitly when the declared body already exceeds the bound."""
    if not value:
        return
    try:
        declared = int(value)
    except ValueError:
        return
    if declared > max_bytes:
        raise SavedFetchError(f"content exceeds size bound ({declared} bytes)", stage="http")


def _read_bounded_body(response: requests.Response, max_bytes: int) -> bytes:
    """Read a streamed body up to the size bound."""
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=PROBE_CHUNK_BYTES):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise SavedFetchError("content exceeds size bound", stage="http")
            chunks.append(chunk)
    except SavedFetchError:
        raise
    except requests.RequestException as error:
        raise SavedFetchError(f"http read failed: {error}", stage="http") from error
    return b"".join(chunks)


def probe_http(
    url: str,
    timeout_seconds: float,
    max_bytes: int,
    session_factory: Callable[[], requests.Session] | None = None,
) -> tuple[str, str | None, bytes]:
    """GET one URL with streaming, size, timeout, and redirect bounds.

    Every observed hop (history plus final URL) must resolve to a public
    address. Donsetch redirect handling has its own guards; this covers
    only the requests probe path used for PDF routing.
    """
    session = session_factory() if session_factory else requests.Session()
    session.trust_env = False  # Do not import netrc credentials or proxy settings.
    target = url
    try:
        for hop in range(MAX_REDIRECTS + 1):
            try:
                if is_excluded_saved_url(target):
                    raise SavedUrlSkipped(SAVED_URL_EXCLUSION_REASON)
                assert_public_url(target)
                response = session.get(
                    target,
                    stream=True,
                    timeout=timeout_seconds,
                    allow_redirects=False,
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
                )
            except (SavedUrlError, requests.RequestException) as error:
                raise SavedFetchError(f"http request failed: {error}", stage="http") from error
            try:
                if response.is_redirect:
                    if hop == MAX_REDIRECTS:
                        raise SavedFetchError("too many redirects", stage="http")
                    target = urljoin(target, response.headers["Location"])
                    continue
                response.raise_for_status()
                _check_content_length(response.headers.get("Content-Length"), max_bytes)
                body = _read_bounded_body(response, max_bytes)
                return response.url, response.headers.get("Content-Type"), body
            except requests.RequestException as error:
                raise SavedFetchError(f"http response failed: {error}", stage="http") from error
            finally:
                response.close()
        raise SavedFetchError("redirect limit exceeded", stage="http")
    finally:
        session.close()


def resolve_donsetch_bin(configured: str) -> str:
    """Return a validated Donsetch executable path or fail explicitly."""
    text = (configured or "").strip()
    if not text:
        raise SavedFetchError(
            "donsetch binary is not configured; set XDIGEST_DONSETCH_BIN", stage="donsetch"
        )
    if "/" in text:
        candidate = Path(text).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise SavedFetchError(
            f"donsetch binary is not executable: {text}; set XDIGEST_DONSETCH_BIN",
            stage="donsetch",
        )
    found = shutil.which(text)
    if found:
        return str(Path(found).absolute())
    # User-local installs remain available with launchd's minimal PATH.
    user_binary = Path.home() / ".local/bin/donsetch"
    if text == "donsetch" and user_binary.is_file() and os.access(user_binary, os.X_OK):
        return str(user_binary)
    raise SavedFetchError(
        f"donsetch binary not found: {text}; install donsetch or set XDIGEST_DONSETCH_BIN",
        stage="donsetch",
    )


def run_donsetch(
    url: str,
    config: DonsetchConfig,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Run the Donsetch CLI once and return the parsed JSON envelope."""
    binary = resolve_donsetch_bin(config.binary)
    args = [
        binary,
        "fetch",
        url,
        "--json",
        "--max-chars",
        str(config.max_chars),
        "--deadline-ms",
        str(config.deadline_ms),
        "--archive",
        "off",
        "--links",
        "--media",
    ]
    env = dict(os.environ)
    env["DONSETCH_MCP__URL_HANDLES"] = "false"
    env["DONSETCH_BYPASS__ENABLED"] = "false"
    call = runner or subprocess.run
    try:
        completed = call(
            args, capture_output=True, text=True, timeout=config.timeout_seconds, env=env
        )
    except subprocess.TimeoutExpired as error:
        raise SavedFetchError(f"donsetch timed out for {url[:300]}", stage="donsetch") from error
    except OSError as error:
        raise SavedFetchError(f"donsetch failed to start: {error}", stage="donsetch") from error
    stdout = completed.stdout if isinstance(completed.stdout, str) else ""
    if not stdout.strip():
        stderr = completed.stderr if isinstance(completed.stderr, str) else ""
        raise SavedFetchError(
            f"donsetch returned no output for {url[:300]}: {stderr[:300]}", stage="donsetch"
        )
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise SavedFetchError(
            f"donsetch returned invalid JSON for {url[:300]}: {error}", stage="donsetch"
        ) from error
    if completed.returncode != 0:
        detail = parsed.get("error") if isinstance(parsed, dict) else None
        message = detail.get("message") if isinstance(detail, dict) else detail
        description = f"donsetch exited with status {completed.returncode}"
        if isinstance(message, str) and message.strip():
            description += f": {message[:1200]}"
        raise SavedFetchError(
            description,
            stage="donsetch",
            raw=parsed if isinstance(parsed, dict) else None,
        )
    if not isinstance(parsed, dict):
        raise SavedFetchError(
            f"donsetch returned a non-object response for {url[:300]}", stage="donsetch"
        )
    return parsed


def parse_donsetch_response(original_url: str, raw: dict[str, Any]) -> FetchOutcome:
    """Interpret one Donsetch envelope; failures and truncation are explicit."""
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    final_url = str(meta.get("url") or original_url)
    if raw.get("ok") is not True:
        error_obj = raw.get("error")
        message = "donsetch reported failure"
        if isinstance(error_obj, dict) and error_obj.get("message"):
            message = f"donsetch reported failure: {error_obj['message']}"[:800]
        return failure_outcome(original_url, message, "donsetch", raw)
    if meta.get("content_ok") is not True or meta.get("thin") is True:
        return failure_outcome(original_url, "donsetch reported content_ok=false", "donsetch", raw)
    if meta.get("pdf") is not None:
        return failure_outcome(
            original_url,
            "URL changed to a PDF during extraction; refusing converted PDF",
            "donsetch",
            raw,
        )
    if (
        meta.get("next_offset") is not None
        or meta.get("truncated")
        or raw.get("next_offset") is not None
    ):
        return failure_outcome(
            original_url,
            f"donsetch response truncated at offset {meta.get('next_offset')}",
            "donsetch",
            raw,
            truncated=True,
        )
    content = raw.get("content")
    text = content if isinstance(content, str) else ""
    if not text.strip():
        return failure_outcome(original_url, "donsetch returned no content", "donsetch", raw)
    return FetchOutcome(
        kind="page",
        original_url=original_url,
        final_url=final_url,
        content_type="text/markdown",
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        content_text=text,
        pdf_bytes=None,
        truncated=False,
        error=None,
        stage="donsetch",
        raw=raw,
    )


class SavedFetcher:
    """Route saved URLs to raw PDFs, PubMed records, or Donsetch pages."""

    def __init__(
        self,
        settings: Settings,
        probe_fn: Callable[..., tuple[str, str | None, bytes]] | None = None,
        donsetch_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.settings = settings
        self._probe_fn = probe_fn or probe_http
        self._donsetch_fn = donsetch_fn or run_donsetch
        self._next_ncbi_request = 0.0

    def validate(self) -> None:
        """Fail before ingestion if the required webpage extractor is unavailable."""
        self._binary = resolve_donsetch_bin(self.settings.donsetch_bin)

    def _donsetch_config(self) -> DonsetchConfig:
        return DonsetchConfig(
            binary=getattr(self, "_binary", self.settings.donsetch_bin),
            max_chars=self.settings.donsetch_max_chars,
            deadline_ms=self.settings.donsetch_deadline_ms,
            timeout_seconds=self.settings.donsetch_timeout_seconds,
        )

    def probe_ncbi(self, url: str) -> tuple[str, str | None, bytes]:
        """Share the keyless NCBI request limit across metadata and full text."""
        if urlparse(url).hostname != "eutils.ncbi.nlm.nih.gov":
            raise ValueError("NCBI requests must use the approved E-Utilities host")
        delay = self._next_ncbi_request - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._next_ncbi_request = time.monotonic() + 0.4
        return self._probe_fn(
            url, self.settings.saved_fetch_timeout_seconds, self.settings.saved_max_bytes
        )

    def _fetch_pubmed(self, original_url: str, pmid: str) -> FetchOutcome:
        """Fetch one exact PubMed record and retain the unmodified XML bytes."""
        endpoint = pubmed_api_url(pmid)
        raw: dict[str, Any] = {
            "provider": "ncbi_eutils",
            "record_type": "pubmed",
            "pmid": pmid,
            "request_url": endpoint,
        }
        try:
            final_url, content_type, body = self.probe_ncbi(endpoint)
        except (SavedFetchError, SavedUrlSkipped, requests.RequestException) as error:
            return failure_outcome(
                original_url, f"PubMed API request failed: {error}", "ncbi_eutils", raw
            )
        raw.update(
            {
                "final_url": final_url,
                "content_type": content_type,
                "body_base64": base64.b64encode(body).decode("ascii"),
            }
        )
        try:
            text = parse_pubmed_record(body, pmid)
        except ValueError as error:
            return failure_outcome(
                original_url, f"PubMed API response rejected: {error}", "ncbi_eutils", raw
            )
        return FetchOutcome(
            kind="api_record",
            original_url=original_url,
            final_url=final_url,
            content_type="text/markdown",
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            content_text=text,
            pdf_bytes=None,
            truncated=False,
            error=None,
            stage="ncbi_eutils",
            raw=raw,
        )

    def fetch(self, url: str) -> FetchOutcome:  # noqa: PLR0911
        """Exclude social URLs, route PubMed records, then probe other URLs."""
        if is_excluded_saved_url(url):
            return replace(
                failure_outcome(url, SAVED_URL_EXCLUSION_REASON, "policy"),
                kind="skipped",
            )
        try:
            validate_saved_url(url)
            pmid = pubmed_pmid(url)
            if pmid is not None:
                return self._fetch_pubmed(url, pmid)
            assert_public_url(url)
        except SavedUrlError as error:
            return failure_outcome(url, str(error), "validate")
        try:
            final_url, content_type, body = self._probe_fn(
                url, self.settings.saved_fetch_timeout_seconds, self.settings.saved_max_bytes
            )
        except SavedUrlSkipped as error:
            return replace(failure_outcome(url, str(error), "policy"), kind="skipped")
        except SavedFetchError as error:
            return failure_outcome(url, str(error), error.stage, error.raw)
        except requests.RequestException as error:
            return failure_outcome(url, f"http request failed: {error}", "http")
        if is_excluded_saved_url(final_url):
            return replace(
                failure_outcome(url, SAVED_URL_EXCLUSION_REASON, "policy"),
                kind="skipped",
            )
        if is_pdf(content_type, body[:4]):
            return FetchOutcome(
                kind="pdf",
                original_url=url,
                final_url=final_url,
                content_type=content_type or "application/pdf",
                content_hash=hashlib.sha256(body).hexdigest(),
                content_text=None,
                pdf_bytes=body,
                truncated=False,
                error=None,
                stage="http",
                raw=None,
            )
        redirected_pmid = pubmed_pmid(final_url)
        if redirected_pmid is not None:
            return self._fetch_pubmed(url, redirected_pmid)
        try:
            raw = self._donsetch_fn(url, self._donsetch_config())
        except SavedFetchError as error:
            return failure_outcome(url, str(error), error.stage, error.raw)
        outcome = parse_donsetch_response(url, raw)
        if outcome.final_url and is_excluded_saved_url(outcome.final_url):
            return replace(
                failure_outcome(url, SAVED_URL_EXCLUSION_REASON, "policy"), kind="skipped"
            )
        return outcome
