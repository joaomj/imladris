"""Persistent structured logging."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JsonlLogger:
    """Write structured events to a JSONL file and the process logger."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger("x_digest")

    def emit(self, run_id: str, event: str, level: str = "info", **details: Any) -> None:
        """Write one event without secrets."""
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "event": event,
            "level": level,
            **details,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        log_method = getattr(self._logger, level, self._logger.info)
        log_method("%s %s", event, details)
