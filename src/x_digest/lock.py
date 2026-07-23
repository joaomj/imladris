"""Small exclusive process lock for sync jobs."""

import os
from pathlib import Path


class LockAlreadyHeld(RuntimeError):
    """Raised when another pipeline run holds the lock."""


class ProcessLock:
    """Create an exclusive lock file and remove it on release."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file_descriptor: int | None = None

    def acquire(self) -> None:
        """Acquire the lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._file_descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self._file_descriptor, str(os.getpid()).encode())
        except FileExistsError as error:
            raise LockAlreadyHeld(f"pipeline lock already exists: {self.path}") from error

    def release(self) -> None:
        """Release the lock."""
        if self._file_descriptor is not None:
            os.close(self._file_descriptor)
            self._file_descriptor = None
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
