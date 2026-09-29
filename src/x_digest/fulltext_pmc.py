"""Validate identity, full text, and license in original PMC XML."""

import re
import xml.etree.ElementTree as ET
from typing import Any

from .fulltext_ids import reject_entity_xml

CC_LICENSE_MARKER = "creativecommons.org/licenses/"


class PmcUnavailable(Exception):
    """A valid PMC response has no usable openly licensed full text."""


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _snippet(body: bytes) -> str:
    return _clean_text(body[:300].decode("utf-8", "replace"))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _pmc_article_matches(meta: ET.Element, digits: str) -> bool:
    """Return True when an article-id equals the requested numeric PMCID."""
    for article_id in _children(meta, "article-id"):
        if article_id.get("pub-id-type") not in ("pmc", "pmcid"):
            continue
        text = _clean_text(article_id.text).upper()
        normalized = text[3:] if text.startswith("PMC") else text
        if normalized == digits:
            return True
    return False


def _recognized_cc_license(meta: ET.Element) -> str | None:
    """Return the full license text when a Creative Commons license is present."""
    permissions = _child(meta, "permissions")
    licenses = _children(permissions, "license") if permissions is not None else []
    for license_element in licenses:
        text = _clean_text("".join(license_element.itertext()))
        attributes = " ".join(
            str(value) for element in license_element.iter() for value in element.attrib.values()
        )
        notice = f"{text} {attributes}".lower()
        if CC_LICENSE_MARKER in notice or "creativecommons.org/publicdomain/" in notice:
            return text or _clean_text(attributes)
    return None


def _article_identifiers(meta: ET.Element, digits: str) -> dict[str, str]:
    identifiers = {"pmcid": f"PMC{digits}"}
    for item in _children(meta, "article-id"):
        kind = item.get("pub-id-type")
        text = _clean_text(item.text)
        if kind == "doi" and text:
            if re.fullmatch(r"10\.\d{4,9}/\S+", text) is None:
                raise ValueError("PMC returned an invalid DOI")
            identifiers["doi"] = text
        elif kind == "pmid" and re.fullmatch(r"[1-9][0-9]*", text):
            identifiers["pmid"] = text
    return identifiers


def parse_pmc_record(body: bytes, digits: str) -> tuple[str, dict[str, str]]:
    """Return the license and identifiers of a strict PMC full-text record.

    Raises :class:`PmcUnavailable` for well-formed records that honestly
    carry no usable full text (empty articleset, no body, no recognized
    open-access license), and ``ValueError`` for malformed, mismatched, or
    error documents.
    """
    if not isinstance(body, bytes) or not body.strip():
        raise ValueError("empty PMC response body: expected efetch XML bytes")
    reject_entity_xml(body, "PMC")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f"malformed PMC XML: {exc}") from exc
    error_element = next(
        (element for element in root.iter() if _local_name(element.tag).lower() == "error"),
        None,
    )
    if error_element is not None:
        detail = _clean_text(error_element.text) or _snippet(body)
        raise ValueError(f"PMC API error for PMC{digits}: {detail}")
    if _local_name(root.tag) != "pmc-articleset":
        raise ValueError(
            f"unexpected PMC root {root.tag!r} for PMC{digits} "
            f"(expected 'pmc-articleset'): {_snippet(body)}"
        )
    articles = _children(root, "article")
    if not articles:
        raise PmcUnavailable(f"PMC{digits}: articleset contains no article")
    if len(articles) > 1:
        raise ValueError(f"multiple ({len(articles)}) articles returned for PMC{digits}")
    article = articles[0]
    front = _child(article, "front")
    meta = _child(front, "article-meta") if front is not None else None
    if meta is None:
        raise ValueError(f"missing front/article-meta in PMC record for PMC{digits}")
    if not _pmc_article_matches(meta, digits):
        raise ValueError(f"PMCID mismatch: no article-id matches PMC{digits}")
    body_element = _child(article, "body")
    body_has_content = body_element is not None and bool(
        _clean_text("".join(body_element.itertext()))
    )
    if not body_has_content:
        raise PmcUnavailable(f"PMC{digits}: article has no body")
    license_text = _recognized_cc_license(meta)
    if license_text is None:
        raise PmcUnavailable(f"PMC{digits}: no recognized Creative Commons license")
    return license_text, _article_identifiers(meta, digits)
