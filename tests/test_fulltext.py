"""Exercise full-text collection and replay without network or browser access."""

import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from x_digest import cli, saved_pipeline
from x_digest.brave import BraveEntry
from x_digest.config import Settings
from x_digest.gold import GoldStore
from x_digest.paths import resolve_stored_path
from x_digest.saved_fetch import SavedFetcher, SavedFetchError
from x_digest.saved_store import rebuild_saved_from_bronze

URL = "https://pubmed.ncbi.nlm.nih.gov/12345678/"
DOI = "10.1234/fulltext-test"
SOCIAL_URLS = ("https://x.com/user/status/123", "https://www.reddit.com/r/test/")
PMC_XML = b"""<?xml version="1.0"?>
<pmc-articleset><article><front><article-meta>
<article-id pub-id-type="pmcid">PMC12345</article-id>
<article-id pub-id-type="pmid">12345678</article-id>
<article-id pub-id-type="doi">10.1234/fulltext-test</article-id>
<permissions><license xmlns:xlink="http://www.w3.org/1999/xlink"
xlink:href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</license></permissions>
</article-meta></front><body><sec><p>The full article body.</p></sec></body>
</article></pmc-articleset>"""


def _entry(url: str) -> BraveEntry:
    return BraveEntry(url, url, "Saved article", url, 100, 200, 0, "unread", "Default")


def _scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str):
    settings = Settings(
        _env_file=None,
        vault_path=tmp_path,
        saved_fulltext_enabled=True,
        saved_max_attempts=2,
    )
    calls: list[str] = []
    pmc_id = '<ArticleId IdType="pmc">PMC12345</ArticleId>' if mode != "unavailable" else ""
    pubmed_xml = (
        "<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>12345678</PMID>"
        "<Article><ArticleTitle>Saved article</ArticleTitle>"
        "<Abstract><AbstractText>The original abstract.</AbstractText></Abstract>"
        "</Article></MedlineCitation><PubmedData><ArticleIdList>"
        f'<ArticleId IdType="doi">{DOI}</ArticleId>{pmc_id}'
        "</ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>"
    ).encode()

    def probe(url: str, _timeout: float, _limit: int):
        calls.append(url)
        parsed = urlparse(url)
        if parsed.hostname == "eutils.ncbi.nlm.nih.gov":
            database = parse_qs(parsed.query)["db"][0]
            if database == "pubmed":
                return url, "application/xml", pubmed_xml
            if mode == "request_failed":
                raise SavedFetchError("PMC request failed", stage="http")
            if mode == "malformed":
                return url, "text/html", b"<html>Checking your browser</html>"
            if mode == "no_body":
                return (
                    url,
                    "application/xml",
                    PMC_XML.replace(b"<body>", b"<absent>").replace(b"</body>", b"</absent>"),
                )
            body = PMC_XML.replace(b"PMC12345", b"PMC99999") if mode == "wrong_pmc" else PMC_XML
            return url, "application/xml", body
        pytest.fail(f"Unexpected request outside the approved routes: {url}")

    def no_donsetch(*_args):
        pytest.fail("Scholarly API routing must not invoke webpage extraction")

    monkeypatch.setattr(saved_pipeline.brave, "find_brave_db", lambda *_args: tmp_path)
    pipeline = saved_pipeline.SavedPipeline(settings)

    def snapshot(*_args):
        directory = tmp_path / "browser-snapshot"
        directory.mkdir()
        return directory

    pipeline.snapshot_fn = snapshot
    pipeline.read_fn = lambda *_args: (
        [
            _entry(URL),
            _entry("https://x.com/user/status/123"),
            _entry("https://www.reddit.com/r/test/"),
        ],
        {"entries": 3},
    )
    pipeline.fetcher = SavedFetcher(settings, probe_fn=probe, donsetch_fn=no_donsetch)
    return pipeline, calls


def _state(pipeline):
    with pipeline.database.connect() as connection:
        source = dict(
            connection.execute("SELECT * FROM saved_items WHERE url=?", (URL,)).fetchone()
        )
        paper = dict(
            connection.execute("SELECT * FROM saved_fulltext WHERE url=?", (URL,)).fetchone()
        )
    return source, paper


def test_full_paper_is_separate_unchanged_and_rebuildable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    original, mime = PMC_XML, "application/xml"
    pipeline, calls = _scenario(tmp_path, monkeypatch, "xml")
    first = pipeline.sync()
    source, paper = _state(pipeline)
    assert first["fulltext_downloaded"] == 1
    assert source["fetch_state"] == "fetched"
    assert "The original abstract." in source["content_text"]
    assert source["archive_path"] is None
    assert paper["state"] == "fetched"
    assert paper["content_type"] == mime
    path = resolve_stored_path(tmp_path, paper["archive_path"])
    assert path.read_bytes() == original
    assert paper["content_hash"] == hashlib.sha256(original).hexdigest()
    before = list(calls)
    assert pipeline.sync()["fulltext_downloaded"] == 0
    assert calls == before
    with pipeline.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM saved_items WHERE fetch_state='skipped'"
        ).fetchone()[0] == len(SOCIAL_URLS)
    assert GoldStore(pipeline.database).verify(full=True)["failed"] == 0
    assert rebuild_saved_from_bronze(pipeline.database)["fulltext"] == 1
    assert _state(pipeline)[1] == paper
    assert GoldStore(pipeline.database).verify(full=True)["failed"] == 0
    monkeypatch.setattr(cli, "load_settings", lambda: pipeline.settings)
    assert cli.main(["saved-show", URL]) == 0
    assert json.loads(capsys.readouterr().out)["fulltext"]["content_type"] == mime


@pytest.mark.parametrize(
    "mode,state",
    [
        ("unavailable", "unavailable"),
        ("no_body", "unavailable"),
        ("malformed", "failed"),
        ("request_failed", "failed"),
        ("wrong_pmc", "failed"),
    ],
)
def test_abstract_survives_missing_or_invalid_full_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode,
    state,
) -> None:
    pipeline, calls = _scenario(tmp_path, monkeypatch, mode)
    first = pipeline.sync()
    source, paper = _state(pipeline)
    assert source["fetch_state"] == "fetched"
    assert "The original abstract." in source["content_text"]
    assert paper["state"] == state
    assert paper["archive_path"] is None
    assert paper["error"]
    assert first["fulltext_downloaded"] == 0
    before = list(calls)
    second = pipeline.sync()
    if state == "failed":
        assert second["fulltext_exhausted"] == 1
        after_retry = list(calls)
        assert len(after_retry) > len(before)
        pipeline.sync()
        assert calls == after_retry
    else:
        assert calls == before
    assert rebuild_saved_from_bronze(pipeline.database)["fulltext"] >= 1
    assert _state(pipeline)[0]["content_text"] == source["content_text"]
