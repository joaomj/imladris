"""Deterministic PubMed official API routing helpers (stdlib only).

This module has no network or persistence operations. It only:

- identifies PubMed article URLs (:func:`pubmed_pmid`),
- builds the official efetch API URL (:func:`pubmed_api_url`),
- renders an efetch XML body as markdown (:func:`parse_pubmed_record`).

No network access, no full-text fetching, no related-paper discovery.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import ParseResult, urlencode, urlparse

PUBMED_HOST = "pubmed.ncbi.nlm.nih.gov"
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
TOOL_NAME = "x-digest"

_POSITIVE_INT_RE = re.compile(r"[1-9][0-9]*\Z")
_PATH_RE = re.compile(r"/([1-9][0-9]*)/?\Z")


def _is_positive_pmid(value: str) -> bool:
    return isinstance(value, str) and _POSITIVE_INT_RE.match(value) is not None


def _require_pmid(value: str, label: str) -> None:
    if not _is_positive_pmid(value):
        raise ValueError(f"invalid {label} {value!r}: expected positive numeric digits")


def _location_ok(parsed: ParseResult) -> bool:
    if parsed.scheme not in ("http", "https"):
        return False
    if (parsed.hostname or "").removesuffix(".") != PUBMED_HOST:
        return False
    if parsed.username or parsed.password:
        return False
    port = parsed.port
    default = 80 if parsed.scheme == "http" else 443
    return port is None or port == default


def _path_pmid(path: str) -> str | None:
    # Strictly "/<pmid>" or "/<pmid>/"; query and fragment never reach here.
    match = _PATH_RE.match(path)
    return match.group(1) if match else None


def pubmed_pmid(url: str) -> str | None:
    """Return the PMID if *url* is a PubMed article page, else None.

    Only ``http``/``https`` URLs with hostname ``pubmed.ncbi.nlm.nih.gov``
    (case-insensitive, one trailing dot allowed), no credentials, no
    non-default port, and path exactly ``/<positive-numeric-PMID>`` with an
    optional trailing slash count. Query strings and fragments are ignored
    (same record). Everything else (search pages, PMC, spoofs) is None.
    """
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
        if not _location_ok(parsed):
            return None
    except ValueError:
        return None
    return _path_pmid(parsed.path)


def pubmed_api_url(pmid: str) -> str:
    """Build the official efetch URL for a positive numeric PMID."""
    _require_pmid(pmid, "PMID")
    query = urlencode({"db": "pubmed", "id": pmid, "retmode": "xml", "tool": TOOL_NAME})
    return f"{EUTILS_BASE}?{query}"


def _text(element: ET.Element | None) -> str:
    """Inline text of *element* with inner markup words preserved."""
    if element is None:
        return ""
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _parse_pubmed_root(body: bytes, expected_pmid: str) -> ET.Element:
    if not isinstance(body, bytes) or not body.strip():
        raise ValueError("empty PubMed response body: expected efetch XML bytes")
    # ElementTree never resolves remote DTDs; the official external PubMed
    # DOCTYPE stays allowed. Only custom ENTITY declarations are rejected.
    if b"<!ENTITY" in body.replace(b"\x00", b""):
        raise ValueError("refused PubMed XML with custom ENTITY declaration (possible XXE attack)")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f"malformed PubMed XML: {exc}") from exc
    snippet = re.sub(r"\s+", " ", body[:300].decode("utf-8", "replace")).strip()
    if root.tag in ("ERROR", "eFetchResult") or root.find(".//ERROR") is not None:
        detail = _text(root.find(".//ERROR")) or snippet
        raise ValueError(f"PubMed API error for PMID {expected_pmid}: {detail}")
    if root.tag == "PubmedArticleSet":
        return root
    if "PubmedBookArticle" in root.tag or root.find(".//PubmedBookArticle") is not None:
        raise ValueError(
            f"unsupported PubMed book record for PMID {expected_pmid}: "
            "only journal articles (PubmedArticle) are supported"
        )
    raise ValueError(
        f"unexpected PubMed root {root.tag!r} for PMID {expected_pmid} "
        f"(expected 'PubmedArticleSet'): {snippet}"
    )


def _only_article(root: ET.Element, expected_pmid: str) -> ET.Element:
    articles = root.findall("PubmedArticle")
    if len(articles) == 1:
        if root.find("PubmedBookArticle") is not None:
            raise ValueError(f"multiple PubMed records returned for PMID {expected_pmid}")
        return articles[0]
    if not articles and root.find(".//PubmedBookArticle") is not None:
        raise ValueError(
            f"unsupported PubMed book record for PMID {expected_pmid}: "
            "only journal articles (PubmedArticle) are supported"
        )
    if not articles:
        raise ValueError(f"no PubmedArticle record found for PMID {expected_pmid}")
    raise ValueError(
        f"multiple ({len(articles)}) PubmedArticle records returned for PMID {expected_pmid}: "
        "expected exactly one"
    )


def _journal_article(article: ET.Element, expected_pmid: str) -> ET.Element:
    citation = article.find("MedlineCitation")
    if citation is None:
        raise ValueError(f"missing MedlineCitation in PubMed record for PMID {expected_pmid}")
    found_pmid = _text(citation.find("PMID"))
    if found_pmid != expected_pmid:
        raise ValueError(
            f"PMID mismatch: response record is PMID {found_pmid!r}, "
            f"expected PMID {expected_pmid!r}"
        )
    journal_article = citation.find("Article")
    if journal_article is None:
        raise ValueError(f"missing Article in PubMed record for PMID {expected_pmid}")
    return journal_article


def _format_authors(article: ET.Element) -> str:
    names: list[str] = []
    for author in article.findall("./AuthorList/Author"):
        collective = _text(author.find("CollectiveName"))
        if collective:
            names.append(collective)
            continue
        last = _text(author.find("LastName"))
        given = _text(author.find("ForeName")) or _text(author.find("Initials"))
        if last and given:
            names.append(f"{given} {last}")
        elif last or given:
            names.append(last or given)
    return "; ".join(names)


def _pub_date(issue: ET.Element | None) -> str:
    if issue is None:
        return ""
    pub_date = issue.find("PubDate")
    if pub_date is None:
        return ""
    medline_date = _text(pub_date.find("MedlineDate"))
    if medline_date:
        return medline_date
    return " ".join(
        part
        for part in (
            _text(pub_date.find("Year")),
            _text(pub_date.find("Month")),
            _text(pub_date.find("Day")),
        )
        if part
    )


def _detail_lines(journal_article: ET.Element, article: ET.Element) -> list[str]:
    lines: list[str] = []
    authors = _format_authors(journal_article)
    if authors:
        lines.append(f"- Authors: {authors}")
    journal = _text(journal_article.find("Journal/Title")) or _text(
        journal_article.find("Journal/ISOAbbreviation")
    )
    if journal:
        lines.append(f"- Journal: {journal}")
    issue = journal_article.find("Journal/JournalIssue")
    if _pub_date(issue):
        lines.append(f"- Published: {_pub_date(issue)}")
    if issue is not None and _text(issue.find("Volume")):
        lines.append(f"- Volume: {_text(issue.find('Volume'))}")
    if issue is not None and _text(issue.find("Issue")):
        lines.append(f"- Issue: {_text(issue.find('Issue'))}")
    pages = _text(journal_article.find("Pagination/MedlinePgn"))
    if not pages:
        start = _text(journal_article.find("Pagination/StartPage"))
        end = _text(journal_article.find("Pagination/EndPage"))
        pages = f"{start}-{end}" if start and end else start
    if pages:
        lines.append(f"- Pages: {pages}")
    doi = _text(journal_article.find("ELocationID[@EIdType='doi']")) or _text(
        article.find("PubmedData/ArticleIdList/ArticleId[@IdType='doi']")
    )
    if doi:
        lines.append(f"- DOI: {doi}")
    return lines


def _abstract_lines(journal_article: ET.Element) -> list[str]:
    parts: list[str] = []
    for para in journal_article.findall("Abstract/AbstractText"):
        words = _text(para)
        if not words:
            continue
        label = (para.get("Label") or "").strip()
        parts.append(f"**{label}** {words}" if label else words)
    if not parts:
        return ["No abstract supplied by PubMed."]
    return parts


def parse_pubmed_record(body: bytes, expected_pmid: str) -> str:
    """Render an efetch XML *body* as a readable markdown record.

    Raises ValueError with an actionable reason for empty input, custom
    ENTITY declarations, malformed XML, API error documents, a wrong root,
    zero/multiple records, unsupported book records, PMID mismatch, or a
    missing title.
    """
    _require_pmid(expected_pmid, "expected PMID")
    root = _parse_pubmed_root(body, expected_pmid)
    article = _only_article(root, expected_pmid)
    journal_article = _journal_article(article, expected_pmid)
    title = _text(journal_article.find("ArticleTitle"))
    if not title:
        raise ValueError(f"missing ArticleTitle in PubMed record for PMID {expected_pmid}")
    lines = [
        f"# {title}",
        "",
        "PubMed record: metadata and abstract only, not full text.",
        "",
        f"- PMID: {expected_pmid} (https://pubmed.ncbi.nlm.nih.gov/{expected_pmid}/)",
        *_detail_lines(journal_article, article),
        "",
        "## Abstract",
        "",
        *_abstract_lines(journal_article),
        "",
    ]
    return "\n".join(lines)
