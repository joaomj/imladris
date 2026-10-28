"""Read-only checks that surface silent failures between weekly runs."""

import html
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database

BACKUP_STAMP_NAME = "backup.last-success"
DIGEST_EVENTS = ("digest_sent", "digest_failed", "digest_partial")
SNAPSHOT_EVENTS = ("snapshot_archived", "snapshot_deduped")
DETAIL_CHARS = 300


def backup_stamp_path(settings: Settings) -> Path:
    """Return the file the backup script stamps after a successful copy."""
    return settings.vault_path / "state" / BACKUP_STAMP_NAME


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a stored timestamp, treating a missing offset as UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


@dataclass(frozen=True)
class Finding:
    """One problem that the owner must read."""

    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        """Return the finding as a plain mapping for printing."""
        return {"code": self.code, "detail": self.detail}


class HealthCheck:
    """Report stalled runs, missing digests, an aging backup, and a stale reading list."""

    def __init__(self, settings: Settings, database: Database | None = None) -> None:
        self.settings = settings
        self.database = database or Database(settings.database_path)

    def run(self, now: datetime | None = None) -> dict[str, Any]:
        """Return every current finding with its evidence."""
        current = now or datetime.now(UTC)
        findings = [
            *self._stalled_runs(current),
            *self._latest_run(current),
            *self._digest(current),
            *self._backup(current),
            *self._saved(current),
        ]
        return {
            "status": "attention" if findings else "ok",
            "checked_at": current.isoformat(),
            "findings": [finding.as_dict() for finding in findings],
        }

    def _stalled_runs(self, now: datetime) -> list[Finding]:
        """Flag runs that started too long ago and never finished."""
        cutoff = now - timedelta(minutes=self.settings.health_stuck_run_minutes)
        self.database.initialize()
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT run_id, started_at FROM runs WHERE status = 'running'"
            ).fetchall()
        findings: list[Finding] = []
        for row in rows:
            started = _parse_timestamp(row["started_at"])
            if started is not None and started > cutoff:
                continue
            findings.append(
                Finding(
                    "run_stalled",
                    f"run {row['run_id']} started {row['started_at']} is still marked running",
                )
            )
        return findings

    def _latest_run(self, now: datetime) -> list[Finding]:
        """Flag a failed or missing latest run inside the expected cadence."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT run_id, status, started_at FROM runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return []
        started = _parse_timestamp(row["started_at"])
        if started is None:
            return [Finding("run_timestamp_invalid", f"run {row['run_id']} has no parseable start")]
        age = now - started
        if age > timedelta(days=self.settings.health_max_age_days):
            return [
                Finding(
                    "run_missing",
                    f"latest run started {row['started_at']}, more than "
                    f"{self.settings.health_max_age_days} days ago",
                )
            ]
        if row["status"] == "failed":
            return [
                Finding("run_failed", f"latest run {row['run_id']} failed at {row['started_at']}")
            ]
        return []

    def _digest(self, now: datetime) -> list[Finding]:
        """Flag the newest digest event when it is not a successful send."""
        cutoff = (now - timedelta(days=self.settings.health_max_age_days)).isoformat()
        placeholders = ",".join("?" for _ in DIGEST_EVENTS)
        with self.database.connect() as connection:
            row = connection.execute(
                f"""SELECT occurred_at, event, details_json FROM run_events
                    WHERE event IN ({placeholders}) AND occurred_at >= ?
                    ORDER BY occurred_at DESC LIMIT 1""",
                (*DIGEST_EVENTS, cutoff),
            ).fetchone()
        from_table = (
            {
                "event": row["event"],
                "occurred_at": row["occurred_at"],
                "details": json.loads(row["details_json"] or "{}"),
            }
            if row is not None
            else None
        )
        candidates = [item for item in (from_table, self._logged_digest_event(cutoff)) if item]
        if not candidates:
            return []
        newest = max(candidates, key=lambda item: item["occurred_at"])
        if newest["event"] == "digest_sent":
            return []
        reported = newest["details"].get("reason") or newest["details"].get("error") or ""
        detail = f"{newest['event']} at {newest['occurred_at']}"
        if reported:
            detail = f"{detail}: {str(reported)[:DETAIL_CHARS]}"
        return [Finding("digest_incomplete", detail)]

    def _logged_digest_event(self, cutoff: str) -> dict[str, Any] | None:
        """Return the newest digest event that only the application log holds.

        A manual `digest --send` has no run row, so its events never reach
        `run_events`.
        """
        newest: dict[str, Any] | None = None
        for path in self._log_files():
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict) or entry.get("event") not in DIGEST_EVENTS:
                    continue
                occurred_at = entry.get("timestamp")
                if not isinstance(occurred_at, str) or occurred_at < cutoff:
                    continue
                candidate = {
                    "event": entry["event"],
                    "occurred_at": occurred_at,
                    "details": entry,
                }
                if newest is None or occurred_at > newest["occurred_at"]:
                    newest = candidate
                break
        return newest

    def _log_files(self) -> list[Path]:
        """Return the current application log and its rotated files."""
        path = self.settings.log_path
        return [path, *(path.with_name(f"{path.name}.{index}") for index in range(1, 6))]

    def _backup(self, now: datetime) -> list[Finding]:
        """Flag a backup that never succeeded or has aged past the cadence."""
        stamp = backup_stamp_path(self.settings)
        try:
            value = stamp.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            value = ""
        finished = _parse_timestamp(value) or self._logged_backup_end()
        if finished is None:
            if not (self.settings.vault_path / "logs" / "backup.log").exists():
                return []
            return [Finding("backup_missing", f"no successful backup recorded in {stamp}")]
        if value and _parse_timestamp(value) is None:
            return [Finding("backup_timestamp_invalid", f"{stamp} holds no timestamp: {value!r}")]
        if now - finished > timedelta(days=self.settings.health_max_age_days):
            return [
                Finding(
                    "backup_stale",
                    f"latest successful backup finished {finished.isoformat()}, more than "
                    f"{self.settings.health_max_age_days} days ago",
                )
            ]
        return []

    def _saved(self, now: datetime) -> list[Finding]:
        """Flag a Brave reading list that has not been read for the whole cadence."""
        placeholders = ",".join("?" for _ in SNAPSHOT_EVENTS)
        with self.database.connect() as connection:
            row = connection.execute(
                f"""SELECT occurred_at FROM run_events
                    WHERE event IN ({placeholders})
                    ORDER BY occurred_at DESC LIMIT 1""",
                SNAPSHOT_EVENTS,
            ).fetchone()
        if row is None:
            return []
        read_at = _parse_timestamp(row["occurred_at"])
        if read_at is None:
            return []
        age = now - read_at
        if age > timedelta(days=self.settings.health_max_age_days):
            return [
                Finding(
                    "saved_stale",
                    f"latest Brave reading-list read finished {row['occurred_at']}, more than "
                    f"{self.settings.health_max_age_days} days ago",
                )
            ]
        return []

    def _logged_backup_end(self) -> datetime | None:
        """Return the newest completion line the backup log recorded."""
        log_path = self.settings.vault_path / "logs" / "backup.log"
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return None
        for line in reversed(lines):
            if not line.endswith(" backup end"):
                continue
            parsed = _parse_timestamp(line.split(" backup end")[0].replace(" ", "T", 1))
            if parsed is not None:
                return parsed
        return None


def render_alert(report: dict[str, Any]) -> str:
    """Render findings as one Telegram HTML message."""
    lines = ["<b>Imladris: the weekly check needs attention</b>"]
    for finding in report["findings"]:
        lines.append(
            f"\n• <code>{html.escape(finding['code'])}</code>: {html.escape(finding['detail'])}"
        )
    return "\n".join(lines)
