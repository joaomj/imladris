"""Collect separately archived full text for saved scholarly identifiers."""

import base64
import gzip
import json
from typing import TYPE_CHECKING, Any

from .fulltext import FullTextResolver
from .fulltext_ids import identifiers_for
from .fulltext_store import archive_fulltext
from .paths import resolve_stored_path
from .saved_urls import is_excluded_saved_url

if TYPE_CHECKING:
    from .bronze import BronzeWriter
    from .config import Settings
    from .db import Database
    from .saved_fetch import SavedFetcher


def _record_xml(database: Database, object_id: str | None) -> bytes | None:
    """Read an archived PubMed API response without refetching its metadata."""
    if object_id is None:
        return None
    with database.connect() as connection:
        row = connection.execute(
            "SELECT kind, path FROM bronze_objects WHERE object_id=?", (object_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"saved item references a missing Bronze object: {object_id}")
    if row["kind"] != "saved-api-record":
        return None
    path = resolve_stored_path(database.path.parent, row["path"])
    payload = json.loads(gzip.decompress(path.read_bytes()))
    raw = payload.get("response")
    if not isinstance(raw, dict) or raw.get("provider") != "ncbi_eutils":
        raise ValueError(f"invalid PubMed API response in {path}")
    encoded = raw.get("body_base64")
    if not isinstance(encoded, str):
        raise ValueError(f"PubMed API response has no preserved XML in {path}")
    return base64.b64decode(encoded, validate=True)


def _due(row: dict[str, Any], settings: Settings) -> bool:
    state = row["fulltext_state"]
    if state in ("fetched", "unavailable"):
        return False
    return not (state == "failed" and row["fulltext_attempts"] >= settings.saved_max_attempts)


def collect_fulltext(
    settings: Settings,
    database: Database,
    bronze: BronzeWriter,
    run_id: str,
    fetcher: SavedFetcher,
) -> dict[str, int]:
    """Resolve only known identifiers and skip terminal or exhausted outcomes."""
    counts = {
        "fulltext_downloaded": 0,
        "fulltext_unavailable": 0,
        "fulltext_failed": 0,
        "fulltext_exhausted": 0,
    }
    if not settings.saved_fulltext_enabled:
        return counts
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT s.*, f.state AS fulltext_state, f.attempts AS fulltext_attempts "
            "FROM saved_items s LEFT JOIN saved_fulltext f ON f.url=s.url "
            "ORDER BY s.first_seen_at, s.url"
        ).fetchall()
    resolver: FullTextResolver | None = None
    sequence = 0
    for source in rows:
        row = dict(source)
        url = row["url"]
        if is_excluded_saved_url(url) or not _due(row, settings):
            continue
        identifiers = identifiers_for(url)
        if not identifiers:
            continue
        # A saved PDF already has an unchanged full-text artifact.
        if row["archive_path"] and row["content_type"] == "application/pdf":
            continue
        # Do not duplicate a failed primary PubMed metadata request in the same run.
        if identifiers.get("pmid") and row["fetch_state"] != "fetched":
            continue
        xml = _record_xml(database, row["bronze_object_id"])
        if xml is not None:
            identifiers = identifiers_for(url, xml)
        if resolver is None:
            resolver = FullTextResolver(
                settings, ncbi_probe=fetcher.probe_ncbi, pubmed_fetch=fetcher.fetch
            )
        result = resolver.resolve(url, identifiers)
        sequence += 1
        archive_fulltext(database, bronze, run_id, url, result, sequence)
        if result.state == "fetched":
            counts["fulltext_downloaded"] += 1
    with database.connect() as connection:
        for row in connection.execute(
            "SELECT state, COUNT(*) AS count FROM saved_fulltext GROUP BY state"
        ):
            if row["state"] in ("unavailable", "failed"):
                counts[f"fulltext_{row['state']}"] = row["count"]
        counts["fulltext_exhausted"] = connection.execute(
            "SELECT COUNT(*) FROM saved_fulltext WHERE state='failed' AND attempts>=?",
            (settings.saved_max_attempts,),
        ).fetchone()[0]
    return counts
