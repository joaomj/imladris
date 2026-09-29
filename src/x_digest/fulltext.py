"""Retrieve full text through the official PMC API without hosted services."""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlencode

import requests

from .fulltext_ids import pubmed_article_ids
from .fulltext_pmc import PmcUnavailable, parse_pmc_record
from .pubmed import parse_pubmed_record
from .saved_fetch import SavedFetchError, SavedUrlSkipped, probe_http

if TYPE_CHECKING:
    from .config import Settings

PMC_EFETCH_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
TOOL_NAME = "x-digest"
ERROR_SNIPPET_CHARS = 300
_PMCID_DIGITS_RE = re.compile(r"[1-9][0-9]*\Z")


@dataclass(frozen=True)
class FullTextResult:
    """Outcome of one open-access full-text resolution attempt."""

    state: Literal["fetched", "unavailable", "failed"]
    identifiers: dict[str, str]
    provider: str | None = None
    source_url: str | None = None
    final_url: str | None = None
    content_type: str | None = None
    license: str | None = None
    version: str | None = None
    body: bytes | None = None
    error: str | None = None
    provenance: list[dict[str, Any]] = field(default_factory=list)


def _snippet(body: bytes) -> str:
    return " ".join(body[:ERROR_SNIPPET_CHARS].decode("utf-8", "replace").split())


def _join_errors(errors: list[str], limit: int = 3000) -> str:
    message = "; ".join(errors)
    return message if len(message) <= limit else message[:limit] + "..."


class FullTextResolver:
    """Retrieve licensed PMC full text, otherwise retain the saved record."""

    def __init__(
        self,
        settings: Settings,
        *,
        probe_fn: Callable[..., tuple[str, str | None, bytes]] | None = None,
        ncbi_probe: Callable[[str], tuple[str, str | None, bytes]] | None = None,
        pubmed_fetch: Callable[[str], Any] | None = None,
    ) -> None:
        self.settings = settings
        self._probe_fn = probe_fn or probe_http
        self._ncbi_probe = ncbi_probe
        self._pubmed_fetch = pubmed_fetch
        self._next_ncbi_request = 0.0

    def _timeout(self) -> float:
        return self.settings.saved_fetch_timeout_seconds

    def _max_bytes(self) -> int:
        return self.settings.saved_max_bytes

    def resolve(self, url: str, identifiers: dict[str, str]) -> FullTextResult:
        """Use known PMC identifiers without searching for additional providers."""
        wanted = dict(identifiers)
        provenance: list[dict[str, Any]] = []
        errors: list[str] = []
        unavailable: list[str] = []
        if wanted.get("pmid") and not wanted.get("doi") and not wanted.get("pmcid"):
            wanted.update(self._enrich_from_pubmed(url, wanted["pmid"], provenance, errors))
        if wanted.get("pmcid"):
            result = self._try_pmc(wanted["pmcid"], wanted, provenance, errors, unavailable)
            if result is not None:
                return result
        if errors:
            return FullTextResult(
                state="failed",
                identifiers=wanted,
                error=_join_errors(errors),
                provenance=provenance,
            )
        reason = "; ".join(unavailable) or "No PMC identifier is available for this saved record"
        return FullTextResult(
            state="unavailable",
            identifiers=wanted,
            error=f"Full text was not obtained through PMC: {reason}",
            provenance=provenance,
        )

    def _pubmed_raw_body(
        self,
        url: str,
        provenance: list[dict[str, Any]],
        errors: list[str],
    ) -> bytes | None:
        """Return the raw PubMed record bytes, or None with an error recorded."""
        if self._pubmed_fetch is None:
            errors.append("PubMed metadata fetch is not configured for PMID-only lookup")
            return None
        try:
            outcome = self._pubmed_fetch(url)
        except (SavedFetchError, SavedUrlSkipped, requests.RequestException) as exc:
            errors.append(f"PubMed metadata fetch failed: {exc}")
            return None
        raw = getattr(outcome, "raw", None)
        if getattr(outcome, "kind", None) != "api_record" or not isinstance(raw, dict) or not raw:
            errors.append(
                "PubMed metadata fetch did not return a usable API record: "
                f"{getattr(outcome, 'error', None) or getattr(outcome, 'kind', None)}"
            )
            return None
        provenance.append({"provider": "pubmed_enrichment", "api_record": raw})
        encoded = raw.get("body_base64")
        if not isinstance(encoded, str) or not encoded:
            errors.append("PubMed metadata response is missing the raw record body")
            return None
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            errors.append(f"PubMed metadata record body is not valid base64: {exc}")
            return None

    def _enrich_from_pubmed(
        self,
        url: str,
        pmid: str,
        provenance: list[dict[str, Any]],
        errors: list[str],
    ) -> dict[str, str]:
        """Expand PMID-only identifiers from the official PubMed record."""
        body = self._pubmed_raw_body(url, provenance, errors)
        if body is None:
            return {}
        try:
            parse_pubmed_record(body, pmid)
            article_ids = pubmed_article_ids(body)
        except ValueError as exc:
            errors.append(f"PubMed metadata response rejected: {exc}")
            return {}
        if article_ids.get("pmid") != pmid:
            errors.append(f"PubMed metadata PMID mismatch for PMID {pmid}")
            return {}
        return {key: article_ids[key] for key in ("doi", "pmcid") if key in article_ids}

    def _ncbi_probe_fn(self) -> Callable[[str], tuple[str, str | None, bytes]]:
        if self._ncbi_probe is not None:
            return self._ncbi_probe

        def _probe(target: str) -> tuple[str, str | None, bytes]:
            delay = self._next_ncbi_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next_ncbi_request = time.monotonic() + 0.4
            return self._probe_fn(target, self._timeout(), self._max_bytes())

        return _probe

    def _try_pmc(
        self,
        pmcid: str,
        wanted: dict[str, str],
        provenance: list[dict[str, Any]],
        errors: list[str],
        clean: list[str],
    ) -> FullTextResult | None:
        """Return the preserved PMC EFetch record when openly licensed, else None."""
        digits = pmcid.strip().upper()
        digits = digits[3:] if digits.startswith("PMC") else digits
        if _PMCID_DIGITS_RE.match(digits) is None:
            errors.append(f"invalid PMCID {pmcid!r}: expected 'PMC' followed by digits")
            return None
        request_url = (
            PMC_EFETCH_BASE
            + "?"
            + urlencode({"db": "pmc", "id": digits, "retmode": "xml", "tool": TOOL_NAME})
        )
        try:
            final_url, content_type, body = self._ncbi_probe_fn()(request_url)
        except (SavedFetchError, SavedUrlSkipped, requests.RequestException) as exc:
            errors.append(f"PMC full-text request failed: {exc}")
            provenance.append(
                {
                    "provider": "pmc_eutils",
                    "endpoint": request_url,
                    "outcome": "request_failed",
                    "error": str(exc),
                }
            )
            return None
        entry: dict[str, Any] = {
            "provider": "pmc_eutils",
            "endpoint": request_url,
            "final_url": final_url,
            "content_type": content_type,
        }
        try:
            license_text, pmc_identifiers = parse_pmc_record(body, digits)
            if (
                wanted.get("doi")
                and pmc_identifiers.get("doi")
                and wanted["doi"].casefold() != pmc_identifiers["doi"].casefold()
            ):
                raise ValueError("PMC DOI does not match the saved paper")
            if (
                wanted.get("pmid")
                and pmc_identifiers.get("pmid")
                and wanted["pmid"] != pmc_identifiers["pmid"]
            ):
                raise ValueError("PMC PMID does not match the saved paper")
        except PmcUnavailable as exc:
            clean.append(str(exc))
            entry["outcome"] = "unavailable"
            entry["error"] = str(exc)
            provenance.append(entry)
            return None
        except ValueError as exc:
            errors.append(f"PMC EFetch response rejected: {exc}")
            entry["outcome"] = "rejected"
            entry["error"] = str(exc)
            entry["response_snippet"] = _snippet(body)
            provenance.append(entry)
            return None
        wanted.update(pmc_identifiers)
        entry["outcome"] = "fetched"
        entry["license"] = license_text
        provenance.append(entry)
        return FullTextResult(
            state="fetched",
            identifiers=dict(wanted),
            provider="pmc_eutils",
            source_url=request_url,
            final_url=final_url,
            content_type="application/xml",
            license=license_text,
            version=None,
            body=body,
            error=None,
            provenance=provenance,
        )
