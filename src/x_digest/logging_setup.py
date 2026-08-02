"""Persistent structured logging with rotation and per-run correlation files."""

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


class JsonFormatter(logging.Formatter):
    """Pass through one pre-serialized JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


class JsonlLogger:
    """Write structured events as JSONL with rotation and level filtering."""

    def __init__(
        self,
        path: Path,
        level: str = "info",
        max_bytes: int = 5_000_000,
        backups: int = 5,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.runs_dir = self.path.parent / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger("x_digest")
        self._logger.setLevel(LEVELS.get(level, logging.INFO))
        if not any(
            isinstance(handler, RotatingFileHandler)
            and getattr(handler, "baseFilename", None) == str(self.path)
            for handler in self._logger.handlers
        ):
            handler = RotatingFileHandler(
                self.path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
            )
            handler.setLevel(self._logger.level)
            handler.setFormatter(JsonFormatter())
            self._logger.addHandler(handler)
        self._run_handler: logging.Handler | None = None

    def begin_run(self, correlation_id: str) -> None:
        """Attach one immutable per-run log file."""
        self.end_run()
        handler = logging.FileHandler(
            self.runs_dir / f"{correlation_id}.jsonl", encoding="utf-8"
        )
        handler.setLevel(self._logger.level)
        handler.setFormatter(JsonFormatter())
        self._logger.addHandler(handler)
        self._run_handler = handler

    def end_run(self) -> None:
        """Detach and close the per-run log file."""
        if self._run_handler is not None:
            self._logger.removeHandler(self._run_handler)
            self._run_handler.close()
            self._run_handler = None

    def emit(self, correlation_id: str, event: str, level: str = "info", **details: Any) -> None:
        """Write one event without secrets."""
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "correlation_id": correlation_id,
            "event": event,
            "level": level,
            **details,
        }
        self._logger.log(
            LEVELS.get(level, logging.INFO),
            json.dumps(record, sort_keys=True, default=str),
        )
