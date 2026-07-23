"""Resumable media retrieval from URLs returned by X."""

import mimetypes
import urllib.error
import urllib.request
from pathlib import Path

from .bronze import BronzeWriter
from .config import Settings
from .db import Database, utc_now


class MediaDownloader:
    """Download pending media with independent database status."""

    def __init__(self, settings: Settings, database: Database, bronze: BronzeWriter) -> None:
        self.settings = settings
        self.database = database
        self.bronze = bronze

    def download_pending(self, run_id: str) -> dict[str, int]:
        """Attempt each pending media item and record every result."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT media_key, source_url FROM media
                   WHERE status = 'pending' AND source_url IS NOT NULL"""
            ).fetchall()
        counts = {"attempted": 0, "downloaded": 0, "failed": 0}
        for row in rows:
            counts["attempted"] += 1
            try:
                content, extension = self._fetch(str(row["source_url"]))
                path, digest = self.bronze.write_media(
                    run_id, str(row["media_key"]), content, extension
                )
                self._mark(str(row["media_key"]), "downloaded", str(path), digest, None)
                counts["downloaded"] += 1
            except (OSError, urllib.error.URLError, ValueError) as error:
                self._mark(str(row["media_key"]), "failed", None, None, str(error))
                counts["failed"] += 1
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
