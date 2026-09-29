"""Brave saved-content pipeline: snapshot the reading list, then fetch.

Runs without X authorization. Takes a stable LevelDB snapshot, decodes all
entries regardless of read status, persists the snapshot to Bronze, upserts
``saved_items`` metadata, and fetches only pending or retryable URLs.
Fetched entries are never refetched; failures retry up to the configured
attempt bound while preserving prior metadata and content.
"""

import hashlib
import json
import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import brave
from .bronze import BronzeWriter, BronzeWriteRequest
from .config import Settings
from .db import Database, utc_now
from .fulltext_pipeline import collect_fulltext
from .lock import ProcessLock
from .logging_setup import JsonlLogger
from .paths import stored_path
from .saved_fetch import FetchOutcome, SavedFetcher
from .saved_store import (
    API_RECORD_KIND,
    FAILURE_KIND,
    PAGE_KIND,
    PDF_META_KIND,
    SKIPPED_KIND,
    SNAPSHOT_KIND,
    checkpoint_key,
    failure_payload,
    get_due_items,
    mark_failure,
    mark_page_success,
    mark_pdf_success,
    mark_skipped,
    page_payload,
    pdf_meta_payload,
    snapshot_hash,
    snapshot_payload,
    upsert_entries,
)

KIND_ENDPOINTS = {
    SNAPSHOT_KIND: "brave://reading-list",
    PAGE_KIND: "donsetch://fetch",
    API_RECORD_KIND: "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
    PDF_META_KIND: "https://saved-pdf",
    FAILURE_KIND: "saved://fetch-failure",
    SKIPPED_KIND: "saved://fetch-skipped",
}


class SavedPipeline:
    """Coordinate Brave snapshot, Bronze writes, and content fetching."""

    def __init__(
        self,
        settings: Settings,
        database: Database | None = None,
        bronze: BronzeWriter | None = None,
        log: JsonlLogger | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.correlation_id = correlation_id
        self.database = database or Database(settings.database_path)
        self.database.initialize()
        self.bronze = bronze or BronzeWriter(settings.vault_path, self.database)
        self.log = log or JsonlLogger(
            settings.log_path, settings.log_level, settings.log_max_bytes, settings.log_backups
        )
        # Test hooks: tests assign snapshot_fn, read_fn, and fetcher directly.
        self.snapshot_fn = brave.snapshot_leveldb
        self.read_fn = brave.read_entries
        self.fetcher: SavedFetcher | None = None
        self._sequence: dict[str, int] = {}

    def _fetcher_for(self) -> SavedFetcher:
        if self.fetcher is not None:
            return self.fetcher
        fetcher = SavedFetcher(self.settings)
        fetcher.validate()
        return fetcher

    def _start_run(self) -> str:
        run_id = self.correlation_id or str(uuid.uuid4())
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO runs(run_id, started_at, status) VALUES (?, ?, 'running')",
                (run_id, utc_now()),
            )
        self.log.begin_run(run_id)
        return run_id

    def _event(
        self, run_id: str, stage: str, event: str, level: str = "info", **details: Any
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO run_events(run_id, occurred_at, stage, level, event, details_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, utc_now(), stage, level, event, json.dumps(details)),
            )
        self.log.emit(run_id, event, level, stage=stage, **details)

    def _finish(
        self, run_id: str, status: str, counts: dict[str, int], error: str | None = None
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET completed_at=?, status=?, counts_json=?, error=? WHERE run_id=?",
                (utc_now(), status, json.dumps(counts), error, run_id),
            )
        self.log.end_run()

    def _write_json(
        self,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        source_ids: list[str],
        context: dict[str, Any],
    ) -> str:
        sequence = self._sequence.get(kind, 0) + 1
        self._sequence[kind] = sequence
        record = self.bronze.write_json(
            BronzeWriteRequest(
                run_id,
                kind,
                payload,
                KIND_ENDPOINTS[kind],
                None,
                source_ids,
                sequence,
                context,
            )
        )
        return record.object_id

    @staticmethod
    def _media_key(url: str) -> str:
        return f"saved-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:32]}"

    def sync(self, profile: str | None = None, db_path: Path | str | None = None) -> dict[str, Any]:
        """Snapshot the reading list and fetch due content once."""
        fetcher = self._fetcher_for()
        active_profile = profile or self.settings.brave_profile or "Default"
        explicit = Path(db_path).expanduser() if db_path else self.settings.brave_db_path
        run_id = self._start_run()
        counts: dict[str, int] = {
            "entries": 0,
            "snapshot_deduped": 0,
            "saved_new": 0,
            "pages": 0,
            "api_records": 0,
            "pdfs": 0,
            "fetched": 0,
            "failed": 0,
            "skipped": 0,
        }
        try:
            with ProcessLock(self.settings.lock_path):
                source = brave.find_brave_db(explicit, active_profile)
                self._event(run_id, "extract", "brave_db_resolved", profile=active_profile)
                snapshot_dir: Path | None = None
                try:
                    snapshot_dir = self.snapshot_fn(source, self.settings.brave_snapshot_retries)
                    entries, read_counts = self.read_fn(snapshot_dir, active_profile)
                finally:
                    if snapshot_dir is not None:
                        shutil.rmtree(snapshot_dir)
                counts["entries"] = len(entries)
                digest = snapshot_hash(entries)
                stored = self.database.get_checkpoint(checkpoint_key(active_profile))
                stored_hash = stored.get("sha256") if isinstance(stored, dict) else None
                if stored_hash == digest:
                    counts["snapshot_deduped"] = 1
                    self._event(
                        run_id,
                        "extract",
                        "snapshot_deduped",
                        profile=active_profile,
                        entries=len(entries),
                    )
                else:
                    payload = snapshot_payload(entries, active_profile, digest, read_counts)
                    self._write_json(
                        run_id,
                        SNAPSHOT_KIND,
                        payload,
                        [entry.url for entry in entries],
                        {"profile": active_profile, "snapshot_sha256": digest},
                    )
                    self.database.set_checkpoint(
                        checkpoint_key(active_profile),
                        {"sha256": digest, "entries": len(entries)},
                    )
                    self._event(
                        run_id,
                        "extract",
                        "snapshot_archived",
                        profile=active_profile,
                        entries=len(entries),
                    )
                upserted = upsert_entries(self.database, entries)
                counts["saved_new"] = upserted["new"]
                due = get_due_items(self.database, self.settings.saved_max_attempts)
                for item in due:
                    self._fetch_one(run_id, active_profile, str(item["url"]), counts, fetcher)
                with self.database.connect() as connection:
                    counts["skipped"] = connection.execute(
                        "SELECT COUNT(*) AS count FROM saved_items WHERE fetch_state = 'fetched'"
                    ).fetchone()["count"]
                    counts["excluded"] = connection.execute(
                        "SELECT COUNT(*) FROM saved_items WHERE fetch_state='skipped'"
                    ).fetchone()[0]
                    counts["exhausted"] = connection.execute(
                        "SELECT COUNT(*) FROM saved_items "
                        "WHERE fetch_state='failed' AND attempts>=?",
                        (self.settings.saved_max_attempts,),
                    ).fetchone()[0]
                fulltext_counts = collect_fulltext(
                    self.settings, self.database, self.bronze, run_id, fetcher
                )
                counts.update(fulltext_counts)
                self._event(run_id, "fulltext", "fulltext_completed", counts=fulltext_counts)
                incomplete = counts["failed"] or counts["exhausted"] or counts["fulltext_failed"]
                self.bronze.write_run_manifest(run_id)
                self._finish(
                    run_id,
                    "failed" if incomplete else "success",
                    counts,
                    "Some saved URLs or full-text lookups are incomplete" if incomplete else None,
                )
                self._event(run_id, "pipeline", "completed", counts=counts)
                return {"run_id": run_id, **counts}
        except Exception as error:
            self._finish(run_id, "failed", counts, str(error))
            self._event(run_id, "pipeline", "failed", "error", error=str(error))
            raise

    def _fetch_one(
        self, run_id: str, profile: str, url: str, counts: dict[str, int], fetcher: SavedFetcher
    ) -> None:
        outcome = fetcher.fetch(url)
        if outcome.kind == "skipped":
            reason = outcome.error or "URL excluded by source policy"
            object_id = self._write_json(
                run_id, SKIPPED_KIND, {"url": url, "reason": reason}, [url], {"profile": profile}
            )
            mark_skipped(self.database, url, reason, object_id)
            self._event(run_id, "fetch", "url_skipped", url=url, reason=reason)
        elif outcome.kind == "pdf":
            self._store_pdf(run_id, profile, outcome, counts)
        elif outcome.kind in ("page", "api_record"):
            self._store_page(run_id, profile, outcome, counts)
        else:
            self._store_failure(run_id, profile, outcome, counts)

    def _store_page(
        self, run_id: str, profile: str, outcome: FetchOutcome, counts: dict[str, int]
    ) -> None:
        object_id = self._write_json(
            run_id,
            API_RECORD_KIND if outcome.kind == "api_record" else PAGE_KIND,
            page_payload(outcome),
            [outcome.original_url],
            {"url": outcome.original_url, "final_url": outcome.final_url, "profile": profile},
        )
        mark_page_success(self.database, outcome, object_id)
        count_key = "api_records" if outcome.kind == "api_record" else "pages"
        counts[count_key] += 1
        counts["fetched"] += 1
        self._event(
            run_id,
            "fetch",
            f"{outcome.kind}_fetched",
            url=outcome.original_url,
            provider=outcome.stage,
            final_url=outcome.final_url,
        )

    def _store_pdf(
        self, run_id: str, profile: str, outcome: FetchOutcome, counts: dict[str, int]
    ) -> None:
        body = outcome.pdf_bytes or b""
        path, digest = self.bronze.write_media(
            run_id, self._media_key(outcome.original_url), body, "pdf"
        )
        verified = replace(outcome, content_hash=digest)
        relative = stored_path(self.bronze.vault_path, path)
        object_id = self._write_json(
            run_id,
            PDF_META_KIND,
            pdf_meta_payload(verified, relative, len(body)),
            [outcome.original_url],
            {"url": outcome.original_url, "final_url": outcome.final_url, "profile": profile},
        )
        mark_pdf_success(self.database, verified, object_id, relative)
        counts["pdfs"] += 1
        counts["fetched"] += 1
        self._event(run_id, "fetch", "pdf_fetched", url=outcome.original_url, bytes=len(body))

    def _store_failure(
        self, run_id: str, profile: str, outcome: FetchOutcome, counts: dict[str, int]
    ) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM saved_items WHERE url = ?", (outcome.original_url,)
            ).fetchone()
        attempts = int(row["attempts"] or 0) + 1 if row else 1
        object_id = self._write_json(
            run_id,
            FAILURE_KIND,
            failure_payload(outcome, attempts),
            [outcome.original_url],
            {"url": outcome.original_url, "profile": profile, "stage": outcome.stage},
        )
        mark_failure(
            self.database, outcome.original_url, outcome.error or "fetch failed", object_id
        )
        counts["failed"] += 1
        self._event(
            run_id,
            "fetch",
            "fetch_failed",
            level="warning",
            url=outcome.original_url,
            error=outcome.error or "fetch failed",
        )
