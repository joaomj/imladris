"""Focused tests for Brave saved-content ingestion with fake I/O."""

import hashlib
from pathlib import Path

import pytest

from x_digest import cli, saved_fetch, saved_pipeline
from x_digest.brave import BraveEntry
from x_digest.config import Settings
from x_digest.db import Database
from x_digest.paths import resolve_stored_path
from x_digest.saved_fetch import FetchOutcome, SavedFetcher, parse_donsetch_response
from x_digest.saved_store import rebuild_saved_from_bronze
from x_digest.saved_urls import SavedUrlError

FETCHED_PAIR = 2
RETRY_ATTEMPTS = 2


def _entry(url: str, title: str = "Title", status: int = 0) -> BraveEntry:
    return BraveEntry(
        key=f"reading_list-dt-{url}",
        entry_id=f"id-{url}",
        title=title,
        url=url,
        creation_us=100,
        update_us=200,
        status=status,
        status_text="unread",
        profile="Default",
    )


def _page_outcome(url: str, text: str = "page text") -> FetchOutcome:
    return FetchOutcome(
        kind="page",
        original_url=url,
        final_url=url,
        content_type="text/markdown",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        content_text=text,
        pdf_bytes=None,
        truncated=False,
        error=None,
        stage="donsetch",
        raw={"ok": True, "meta": {"url": url, "content_ok": True}, "content": text},
    )


def _pdf_outcome(url: str, body: bytes = b"%PDF-1.4 fake-bytes") -> FetchOutcome:
    return FetchOutcome(
        kind="pdf",
        original_url=url,
        final_url=url,
        content_type="application/pdf",
        content_hash=hashlib.sha256(body).hexdigest(),
        content_text=None,
        pdf_bytes=body,
        truncated=False,
        error=None,
        stage="http",
        raw=None,
    )


def _failure_outcome(url: str, error: str = "boom") -> FetchOutcome:
    return FetchOutcome(
        kind="failure",
        original_url=url,
        final_url=None,
        content_type=None,
        content_hash=None,
        content_text=None,
        pdf_bytes=None,
        truncated=False,
        error=error,
        stage="donsetch",
        raw={"ok": False, "meta": {"url": url}},
    )


class _ScriptFetcher:
    """Return scripted outcomes per URL, recording every fetch call."""

    def __init__(self, outcomes: dict[str, list[FetchOutcome]]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def fetch(self, url: str) -> FetchOutcome:
        self.calls.append(url)
        script = self.outcomes.get(url)
        assert script, f"unexpected fetch: {url}"
        if len(script) > 1:
            return script.pop(0)
        return script[0]


def _pipeline(
    tmp_path: Path,
    entries: list[BraveEntry],
    fetcher: _ScriptFetcher,
    monkeypatch: pytest.MonkeyPatch,
    **settings_overrides: object,
) -> saved_pipeline.SavedPipeline:
    settings = Settings(vault_path=tmp_path, **settings_overrides)  # type: ignore[arg-type]
    state = {"entries": entries}
    pipeline = saved_pipeline.SavedPipeline(settings)

    def fake_snapshot(_source: Path, _retries: int) -> Path:
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir(parents=True, exist_ok=True)
        return snapshot

    def fake_read(_snapshot: Path, _profile: str) -> tuple[list[BraveEntry], dict[str, object]]:
        return list(state["entries"]), {"entries": len(state["entries"])}

    monkeypatch.setattr(saved_pipeline.brave, "find_brave_db", lambda _explicit, _profile: tmp_path)
    pipeline.snapshot_fn = fake_snapshot  # type: ignore[method-assign]
    pipeline.read_fn = fake_read  # type: ignore[method-assign]
    pipeline.fetcher = fetcher  # type: ignore[assignment]
    pipeline.state = state  # type: ignore[attr-defined]
    return pipeline


def _bronze_kind_count(database: Database, kind: str) -> int:
    with database.connect() as connection:
        return connection.execute(
            "SELECT COUNT(*) AS count FROM bronze_objects WHERE kind = ?", (kind,)
        ).fetchone()["count"]


def _row(database: Database, url: str) -> dict:
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM saved_items WHERE url = ?", (url,)).fetchone()
    assert row is not None
    return dict(row)


def _fetcher_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(vault_path=tmp_path, **overrides)  # type: ignore[arg-type]


def test_fetcher_routes_pdf_by_content_type(tmp_path: Path) -> None:
    def no_donsetch(_url: str, _config: object) -> dict:
        raise AssertionError("must not call donsetch")

    fetcher = SavedFetcher(
        _fetcher_settings(tmp_path),
        probe_fn=lambda url, _timeout, _limit: (url, "application/pdf", b"%PDF-1.7 body"),
        donsetch_fn=no_donsetch,  # type: ignore[arg-type]
    )
    outcome = fetcher.fetch("https://example.com/doc.pdf")
    assert outcome.kind == "pdf"
    assert outcome.pdf_bytes == b"%PDF-1.7 body"


def test_fetcher_routes_pdf_by_magic_bytes(tmp_path: Path) -> None:
    def no_donsetch(_url: str, _config: object) -> dict:
        raise AssertionError("must not call donsetch")

    fetcher = SavedFetcher(
        _fetcher_settings(tmp_path),
        probe_fn=lambda url, _timeout, _limit: (url, "application/octet-stream", b"%PDF-x"),
        donsetch_fn=no_donsetch,  # type: ignore[arg-type]
    )
    assert fetcher.fetch("https://example.com/file").kind == "pdf"


def test_fetcher_treats_truncation_as_failure(tmp_path: Path) -> None:
    raw = {
        "ok": True,
        "meta": {"url": "https://example.com/a", "content_ok": True, "next_offset": 1000},
        "content": "x",
    }
    fetcher = SavedFetcher(
        _fetcher_settings(tmp_path, donsetch_max_chars=1000),
        probe_fn=lambda url, _timeout, _limit: (url, "text/html", b"<html>"),
        donsetch_fn=lambda _url, _config: raw,
    )
    outcome = fetcher.fetch("https://example.com/a")
    assert outcome.kind == "failure"
    assert outcome.truncated is True
    assert "truncated" in (outcome.error or "")


def test_fetcher_treats_not_ok_as_failure(tmp_path: Path) -> None:
    raw: dict = {
        "ok": False,
        "meta": {"url": "https://example.com/a"},
        "error": {"message": "nope"},
    }
    fetcher = SavedFetcher(
        _fetcher_settings(tmp_path),
        probe_fn=lambda url, _timeout, _limit: (url, "text/html", b"<html>"),
        donsetch_fn=lambda _url, _config: raw,
    )
    outcome = fetcher.fetch("https://example.com/a")
    assert outcome.kind == "failure"
    assert "nope" in (outcome.error or "")


def test_fetcher_rejects_private_url_without_network(tmp_path: Path) -> None:
    def no_probe(*_args: object) -> tuple:
        raise AssertionError("must not probe")

    def no_donsetch(*_args: object) -> dict:
        raise AssertionError("must not call donsetch")

    fetcher = SavedFetcher(
        _fetcher_settings(tmp_path),
        probe_fn=no_probe,  # type: ignore[arg-type]
        donsetch_fn=no_donsetch,  # type: ignore[arg-type]
    )
    outcome = fetcher.fetch("http://127.0.0.1/private")
    assert outcome.kind == "failure"
    assert outcome.stage == "validate"


def test_two_consecutive_syncs_do_not_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page_url = "https://example.com/a"
    pdf_url = "https://example.com/b.pdf"
    pdf_body = b"%PDF-1.4 fake-bytes"
    fetcher = _ScriptFetcher(
        {page_url: [_page_outcome(page_url)], pdf_url: [_pdf_outcome(pdf_url, pdf_body)]}
    )
    pipeline = _pipeline(tmp_path, [_entry(page_url), _entry(pdf_url)], fetcher, monkeypatch)
    first = pipeline.sync()
    assert first["fetched"] == FETCHED_PAIR
    assert first["failed"] == 0
    assert first["snapshot_deduped"] == 0
    assert sorted(fetcher.calls) == sorted([page_url, pdf_url])
    assert _bronze_kind_count(pipeline.database, "saved-page") == 1
    assert _bronze_kind_count(pipeline.database, "saved-pdf-meta") == 1

    fetcher.calls.clear()
    second = pipeline.sync()
    assert second["fetched"] == 0
    assert second["failed"] == 0
    assert second["snapshot_deduped"] == 1
    assert second["skipped"] == FETCHED_PAIR
    assert fetcher.calls == []
    assert _bronze_kind_count(pipeline.database, "saved-page") == 1
    assert _bronze_kind_count(pipeline.database, "saved-pdf-meta") == 1


def test_failure_retries_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.com/flaky"
    fetcher = _ScriptFetcher({url: [_failure_outcome(url, "first"), _page_outcome(url, "later")]})
    pipeline = _pipeline(tmp_path, [_entry(url)], fetcher, monkeypatch)
    first = pipeline.sync()
    assert first["failed"] == 1
    row = _row(pipeline.database, url)
    assert row["fetch_state"] == "failed"
    assert row["attempts"] == 1
    assert row["fetch_error"] == "first"

    second = pipeline.sync()
    assert second["failed"] == 0
    assert second["fetched"] == 1
    row = _row(pipeline.database, url)
    assert row["fetch_state"] == "fetched"
    assert row["attempts"] == RETRY_ATTEMPTS
    assert row["content_text"] == "later"


def test_failures_stop_at_max_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.com/always-down"
    fetcher = _ScriptFetcher({url: [_failure_outcome(url)]})
    pipeline = _pipeline(tmp_path, [_entry(url)], fetcher, monkeypatch, saved_max_attempts=1)
    assert pipeline.sync()["failed"] == 1
    fetcher.calls.clear()
    second = pipeline.sync()
    assert second["failed"] == 0
    assert second["fetched"] == 0
    assert fetcher.calls == []
    assert _row(pipeline.database, url)["attempts"] == 1


def test_pdf_bytes_unchanged_and_metadata_retained_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_url = "https://example.com/doc.pdf"
    bad_url = "https://example.com/bad"
    pdf_body = b"%PDF-1.4 unchanged-bytes"
    fetcher = _ScriptFetcher(
        {pdf_url: [_pdf_outcome(pdf_url, pdf_body)], bad_url: [_failure_outcome(bad_url, "denied")]}
    )
    pipeline = _pipeline(
        tmp_path, [_entry(pdf_url, "Doc"), _entry(bad_url, "Old title")], fetcher, monkeypatch
    )
    result = pipeline.sync()
    assert result["failed"] == 1
    assert result["pdfs"] == 1

    pdf_row = _row(pipeline.database, pdf_url)
    assert pdf_row["fetch_state"] == "fetched"
    assert pdf_row["content_text"] is None
    assert pdf_row["content_hash"] == hashlib.sha256(pdf_body).hexdigest()
    stored = resolve_stored_path(pipeline.settings.vault_path, pdf_row["archive_path"])
    assert stored.read_bytes() == pdf_body

    bad_row = _row(pipeline.database, bad_url)
    assert bad_row["fetch_state"] == "failed"
    assert bad_row["title"] == "Old title"
    assert bad_row["fetch_error"] == "denied"


def test_fetched_entry_keeps_content_when_brave_metadata_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://example.com/known"
    fetcher = _ScriptFetcher({url: [_page_outcome(url, "original")]})
    pipeline = _pipeline(tmp_path, [_entry(url, "Old title")], fetcher, monkeypatch)
    assert pipeline.sync()["fetched"] == 1
    pipeline.state["entries"] = [_entry(url, "New title")]  # type: ignore[attr-defined]
    fetcher.calls.clear()
    second = pipeline.sync()
    assert second["fetched"] == 0
    assert fetcher.calls == []
    row = _row(pipeline.database, url)
    assert row["title"] == "New title"
    assert row["content_text"] == "original"
    assert row["attempts"] == 1


def test_bronze_replay_rebuilds_saved_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://example.com/replay"
    fetcher = _ScriptFetcher({url: [_page_outcome(url, "replay text")]})
    pipeline = _pipeline(tmp_path, [_entry(url, "Replay title")], fetcher, monkeypatch)
    assert pipeline.sync()["fetched"] == 1
    database = Database(pipeline.settings.database_path)
    with database.transaction() as connection:
        connection.execute("DELETE FROM saved_items_fts")
        connection.execute("DELETE FROM saved_items")
    counts = rebuild_saved_from_bronze(database)
    assert counts["page"] == 1
    assert counts["snapshot"] == 1
    row = _row(database, url)
    assert row["fetch_state"] == "fetched"
    assert row["title"] == "Replay title"
    rebuild_saved_from_bronze(database)
    assert _row(database, url)["attempts"] == 1


def test_brave_sync_cli_exit_code_reflects_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDIGEST_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(
        cli.SavedPipeline, "sync", lambda _self, **_kwargs: {"run_id": "r", "failed": 2}
    )
    assert cli.main(["brave-sync", "--profile", "Default"]) == 1
    monkeypatch.setattr(
        cli.SavedPipeline, "sync", lambda _self, **_kwargs: {"run_id": "r", "failed": 0}
    )
    assert cli.main(["brave-sync", "--profile", "Default"]) == 0
    parser = cli.build_parser()
    args = parser.parse_args(["brave-sync", "--profile", "P", "--db-path", "/tmp/db"])
    assert args.profile == "P"
    assert str(args.db_path) == "/tmp/db"


def test_http_redirect_is_checked_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def guard(url):
        if "127.0.0.1" in url:
            raise SavedUrlError("private destination")

    class Response:
        is_redirect = True

        def __init__(self):
            self.headers = {"Location": "http://127.0.0.1/private"}

        def close(self):
            pass

    class Session:
        def get(self, url, **kwargs):
            calls.append(url)
            assert kwargs["allow_redirects"] is False
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(saved_fetch, "assert_public_url", guard)
    with pytest.raises(saved_fetch.SavedFetchError, match="private destination"):
        saved_fetch.probe_http("https://example.com/a", 10, 10000, Session)
    assert calls == ["https://example.com/a"]


@pytest.mark.parametrize(
    "meta,content",
    [
        ({"content_ok": True}, ""),
        ({"content_ok": True, "pdf": {"pages": 1}}, "converted PDF"),
        ({"content_ok": False}, "blocked page"),
    ],
)
def test_invalid_extraction_is_not_archived_as_success(meta, content) -> None:
    result = parse_donsetch_response(
        "https://example.com/a", {"ok": True, "meta": meta, "content": content}
    )
    assert result.kind == "failure"
