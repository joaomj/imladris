"""Cover the findings the daily problem report must not miss."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from x_digest.config import Settings
from x_digest.db import Database
from x_digest.health import HealthCheck


def _record_snapshot(database: Database, run_id: str, read_at: datetime) -> None:
    """Store one finished reading-list run with its snapshot event."""
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO runs(run_id, started_at, completed_at, status) VALUES (?, ?, ?, ?)",
            (run_id, read_at.isoformat(), read_at.isoformat(), "success"),
        )
        connection.execute(
            """INSERT INTO run_events(run_id, occurred_at, stage, level, event)
               VALUES (?, ?, ?, ?, ?)""",
            (run_id, read_at.isoformat(), "extract", "info", "snapshot_archived"),
        )


def _codes(settings: Settings, database: Database, now: datetime) -> list[str]:
    report = HealthCheck(settings, database).run(now)
    return [finding["code"] for finding in report["findings"]]


def test_stale_reading_list_read_is_reported(tmp_path: Path) -> None:
    settings = Settings(vault_path=tmp_path)  # type: ignore[arg-type]
    database = Database(settings.database_path)
    database.initialize()
    now = datetime.now(UTC)
    _record_snapshot(database, "old", now - timedelta(days=settings.health_max_age_days + 1))
    assert "saved_stale" in _codes(settings, database, now)
    _record_snapshot(database, "new", now)
    assert "saved_stale" not in _codes(settings, database, now)
