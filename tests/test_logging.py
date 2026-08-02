"""Tests for structured logging, rotation, and usage tracking."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from x_digest.logging_setup import JsonlLogger
from x_digest.x_api import UsageTracker

LOW_RATE_REMAINING = 30
LOW_APP_REMAINING = 880
RETRY_COUNT = 1
REQUEST_COUNT = 2


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_level_filtering_and_correlation_id(tmp_path: Path) -> None:
    log = JsonlLogger(tmp_path / "logs" / "application.jsonl", level="info")
    log.begin_run("corr-1")
    log.emit("corr-1", "debug_event", "debug", detail="x")
    log.emit("corr-1", "info_event", "info", detail="y")
    log.emit("corr-1", "warn_event", "warning", detail="z")
    log.end_run()
    aggregate = _read_jsonl(tmp_path / "logs" / "application.jsonl")
    assert [line["event"] for line in aggregate] == ["info_event", "warn_event"]
    assert all(line["correlation_id"] == "corr-1" for line in aggregate)
    per_run = _read_jsonl(tmp_path / "logs" / "runs" / "corr-1.jsonl")
    assert [line["event"] for line in per_run] == ["info_event", "warn_event"]


def test_rotation_creates_backups(tmp_path: Path) -> None:
    log = JsonlLogger(
        tmp_path / "logs" / "application.jsonl", level="info", max_bytes=200, backups=2
    )
    for index in range(60):
        log.emit("corr", "event", "info", index=index)
    rotated = tmp_path / "logs" / "application.jsonl.1"
    assert rotated.exists()
    assert _read_jsonl(rotated)


def test_usage_tracker_records_min_rate_limits() -> None:
    tracker = UsageTracker()
    tracker.record(
        "/2/users/1/bookmarks",
        SimpleNamespace(headers={"x-rate-limit-remaining": "50", "x-app-limit-remaining": "900"}),
    )
    tracker.record(
        "/2/users/1/bookmarks",
        SimpleNamespace(headers={"x-rate-limit-remaining": "30", "x-app-limit-remaining": "880"}),
    )
    tracker.record_retry("users/bookmarks")
    entry = tracker.summary()["/2/users/1/bookmarks"]
    assert entry["requests"] == REQUEST_COUNT
    assert entry["min_rate_limit_remaining"] == LOW_RATE_REMAINING
    assert entry["min_app_limit_remaining"] == LOW_APP_REMAINING
    assert tracker.retries() == {"users/bookmarks": RETRY_COUNT}


def test_cli_gold_command_logs_with_correlation_id(tmp_path: Path) -> None:
    environment = {**os.environ, "XDIGEST_VAULT_PATH": str(tmp_path)}
    subprocess.run(
        ["uv", "run", "x-digest", "status"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    lines = _read_jsonl(tmp_path / "logs" / "application.jsonl")
    completed = [line for line in lines if line["event"] == "command_completed"]
    assert completed
    assert completed[-1]["command"] == "status"
    assert completed[-1]["correlation_id"]
