"""Resumable media retrieval from URLs returned by X."""

import mimetypes
import urllib.error
import urllib.request
from pathlib import Path

from .bronze import BronzeWriter
from .config import Settings
from .db import Database, utc_now
from .logging_setup import JsonlLogger
from .paths import stored_path


class MediaDownloader:
    """Download pending media with independent database status."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        bronze: BronzeWriter,
        log: JsonlLogger | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.bronze = bronze
        self.log = log
        self.correlation_id = correlation_id

    def download_pending(self, run_id: str) -> dict[str, int]:
        """Attempt each pending media item and record every result."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT media_key, source_url FROM media
                   WHERE status = 'pending' AND source_url IS NOT NULL"""
            ).fetchall()
        counts = {"attempted": 0, "downloaded": 0, "failed": 0, "bytes_downloaded": 0}
        for row in rows:
            counts["attempted"] += 1
            media_key = str(row["media_key"])
            correlation_id = self.correlation_id or run_id
            if self.log:
                self.log.emit(correlation_id, "media_attempted", "debug", media_key=media_key)
            try:
                content, extension = self._fetch(str(row["source_url"]))
                path, digest = self.bronze.write_media(
                    run_id, media_key, content, extension
                )
                self._mark(
                    media_key,
                    "downloaded",
                    stored_path(self.bronze.vault_path, path),
                    digest,
                    None,
                )
                counts["downloaded"] += 1
                counts["bytes_downloaded"] += len(content)
                if self.log:
                    self.log.emit(
                        correlation_id,
                        "media_downloaded",
                        "info",
                        media_key=media_key,
                        bytes=len(content),
                    )
            except (OSError, urllib.error.URLError, ValueError) as error:
                self._mark(media_key, "failed", None, None, str(error))
                counts["failed"] += 1
                if self.log:
                    self.log.emit(
                        correlation_id,
                        "media_failed",
                        "warning",
                        media_key=media_key,
                        error=str(error),
                    )
        return counts

    def _fetch(self, url: str) -> tuple[bytes, str]:
        request = urllib.request.Request(url, headers={"User-Agent": "x-digest/0.1"})
        with urllib.request.urlopen(
            request, timeout=self.settings.media_timeout_seconds
        ) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > self.settings.media_max_bytes:
                raise ValueError("media exceeds configured size limit")
            content = response.read(self.settings.media_max_bytes + 1)
            if len(content) > self.settings.media_max_bytes:
                raise ValueError("media exceeds configured size limit")
            content_type = response.headers.get_content_type()
        extension = mimetypes.guess_extension(content_type) or Path(url).suffix or ".bin"
        return content, extension

    def _mark(
        self,
        media_key: str,
        status: str,
        path: str | None,
        digest: str | None,
        error: str | None,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE media SET status=?, archive_path=?, sha256=?, error=?, last_seen_at=?
                   WHERE media_key=?""",
                (status, path, digest, error, utc_now(), media_key),
            )
