"""Identify the exact scholarly record behind supported saved URLs."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import unquote, urlparse

from .pubmed import parse_pubmed_record, pubmed_pmid

PMC_HOSTS = {"pmc.ncbi.nlm.nih.gov"}
PMC_LEGACY_HOSTS = {"www.ncbi.nlm.nih.gov"}
DOI_HOSTS = {"doi.org", "dx.doi.org"}
_PMID_RE = re.compile(r"[1-9][0-9]*\Z")
_PMCID_DIGITS_RE = re.compile(r"[1-9][0-9]*\Z")
_DOI_RE = re.compile(r"10\.\d{4,9}/\S+\Z")
_NEW_PMC_PATH_RE = re.compile(r"/articles/[Pp][Mm][Cc]([1-9][0-9]*)(?:/|\Z)")
_LEGACY_PMC_PATH_RE = re.compile(r"/pmc/articles/[Pp][Mm][Cc]([1-9][0-9]*)(?:/|\Z)")


def _location_ok(url: str, hosts: set[str]):
    """Return the parsed URL when scheme/host/credentials/port are strict."""
    try:
        parsed = urlparse(url.strip())
        port = parsed.port
    except ValueError:
        return None
    default = 80 if parsed.scheme == "http" else 443
    if (
        parsed.scheme in ("http", "https")
        and (parsed.hostname or "").lower().rstrip(".") in hosts
        and not parsed.username
        and not parsed.password
        and (port is None or port == default)
    ):
        return parsed
    return None


def pmcid_from_url(url: str) -> str | None:
    """Return the normalized PMCID for exact PMC article URLs, else None.

    Covers the current ``pmc.ncbi.nlm.nih.gov/articles/PMC...`` form
    (including saved ``.../pdf/...`` paths) and the legacy
    ``www.ncbi.nlm.nih.gov/pmc/articles/PMC...`` form. Query strings and
    fragments address the same record.
    """
    if not isinstance(url, str):
        return None
    if _location_ok(url, PMC_HOSTS) is not None:
        match = _NEW_PMC_PATH_RE.match(urlparse(url.strip()).path)
    elif _location_ok(url, PMC_LEGACY_HOSTS) is not None:
        match = _LEGACY_PMC_PATH_RE.match(urlparse(url.strip()).path)
    else:
        return None
    if not match:
        return None
    return f"PMC{match.group(1)}"


def _doi_from_url(url: str) -> str | None:
    """Return the DOI for canonical doi.org/dx.doi.org URLs, else None."""
    if not isinstance(url, str):
        return None
    parsed = _location_ok(url, DOI_HOSTS)
    if parsed is None:
        return None
    candidate = unquote(parsed.path.lstrip("/"))
    candidate = candidate.strip()
    if _DOI_RE.match(candidate) is None:
        return None
    return candidate


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def reject_entity_xml(body: bytes, label: str) -> None:
    """Reject XML with custom ENTITY declarations, incl. null-byte encodings."""
    if b"<!ENTITY" in body.replace(b"\x00", b""):
        raise ValueError(
            f"refused {label} XML with custom ENTITY declaration (possible XXE attack)"
        )


def _normalize_pmcid(value: str) -> str | None:
    """Normalize a PMCID value to ``PMC<digits>`` form, else None."""
    text = (value or "").strip().upper()
    digits = text[3:] if text.startswith("PMC") else text
    if _PMCID_DIGITS_RE.match(digits) is None:
        return None
    return f"PMC{digits}"


def _collect_article_id(entry: ET.Element, pmid: str, found: dict[str, str]) -> None:
    """Merge one ArticleIdList entry (DOI or PMC) into the identifier map."""
    kind = (entry.get("IdType") or "").strip().lower()
    text = _clean_text(entry.text)
    if not text:
        return
    if kind == "doi" and "doi" not in found:
        if _DOI_RE.match(text) is None:
            raise ValueError(f"invalid DOI {text!r} in PubMed record for PMID {pmid}")
        found["doi"] = text
    elif kind == "pmc" and "pmcid" not in found:
        normalized = _normalize_pmcid(text)
        if normalized is None:
            raise ValueError(f"invalid PMCID {text!r} in PubMed record for PMID {pmid}")
        found["pmcid"] = normalized


def _fallback_article_doi(article: ET.Element, pmid: str, found: dict[str, str]) -> None:
    """Fill a missing DOI from the article's own ELocationID element."""
    if "doi" in found:
        return
    fallback = _clean_text(article.findtext("MedlineCitation/Article/ELocationID[@EIdType='doi']"))
    if not fallback:
        return
    if _DOI_RE.match(fallback) is None:
        raise ValueError(f"invalid DOI {fallback!r} in PubMed record for PMID {pmid}")
    found["doi"] = fallback


def pubmed_article_ids(body: bytes) -> dict[str, str]:
    """Extract pmid/doi/pmcid from one strict PubMed article record.

    Only the article's own ``PubmedData/ArticleIdList`` is consulted, with
    a fallback to the article's ``ELocationID`` DOI. Reference lists are
    never inspected.
    """
    reject_entity_xml(body, "PubMed")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f"malformed PubMed XML: {exc}") from exc
    if root.tag != "PubmedArticleSet":
        raise ValueError(f"unexpected PubMed root {root.tag!r} (expected 'PubmedArticleSet')")
    articles = root.findall("PubmedArticle")
    if len(articles) != 1:
        raise ValueError(f"expected exactly one PubmedArticle, found {len(articles)}")
    article = articles[0]
    pmid = _clean_text(article.findtext("MedlineCitation/PMID"))
    if _PMID_RE.match(pmid) is None:
        raise ValueError(f"invalid PMID {pmid!r} in PubMed record")
    found: dict[str, str] = {"pmid": pmid}
    for entry in article.findall("PubmedData/ArticleIdList/ArticleId"):
        _collect_article_id(entry, pmid, found)
    _fallback_article_doi(article, pmid, found)
    return found


def identifiers_for(url: str, pubmed_xml: bytes | None = None) -> dict[str, str]:
    """Extract known identifiers from an exact article URL.

    Only PubMed article pages, PMC article URLs (current and legacy,
    including saved PDF paths), and canonical DOI URLs are recognized;
    anything else returns ``{}``. When ``pubmed_xml`` is supplied it must
    be the efetch body for the URL's PMID (verified with
    :func:`parse_pubmed_record`); DOI/PMC come only from that article's
    own identifiers.
    """
    found: dict[str, str] = {}
    pmid = pubmed_pmid(url) if isinstance(url, str) else None
    if pmid is not None:
        found["pmid"] = pmid
    pmcid = pmcid_from_url(url)
    if pmcid is not None:
        found["pmcid"] = pmcid
    doi = _doi_from_url(url)
    if doi is not None:
        found["doi"] = doi
    if pubmed_xml is not None:
        if not isinstance(pubmed_xml, bytes):
            raise ValueError("pubmed_xml must be efetch XML bytes")
        if pmid is not None:
            parse_pubmed_record(pubmed_xml, pmid)
            article_ids = pubmed_article_ids(pubmed_xml)
            if article_ids.get("pmid") != pmid:
                raise ValueError(f"PMID mismatch: XML record is {article_ids.get('pmid')!r}")
            found.update({key: article_ids[key] for key in ("doi", "pmcid") if key in article_ids})
        else:
            article_ids = pubmed_article_ids(pubmed_xml)
            parse_pubmed_record(pubmed_xml, article_ids["pmid"])
            for key, value in found.items():
                if key in article_ids and article_ids[key].casefold() != value.casefold():
                    raise ValueError(f"PubMed metadata does not match the saved {key}")
            found.update(article_ids)
    return found
