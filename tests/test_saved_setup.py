"""Startup failures must not consume saved-item retry attempts."""

from pathlib import Path

import pytest

from x_digest import saved_fetch
from x_digest.config import Settings
from x_digest.saved_pipeline import SavedPipeline


def test_missing_extractor_stops_before_brave_read(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        vault_path=tmp_path,
        donsetch_bin=str(tmp_path / "missing-donsetch"),
    )
    pipeline = SavedPipeline(settings)
    with pipeline.database.transaction() as connection:
        connection.execute(
            "INSERT INTO saved_items(url,first_seen_at,last_seen_at,attempts) "
            "VALUES ('https://example.org/a','2026-01-01','2026-01-01',1)"
        )

    def unexpected_snapshot(*_args):
        pytest.fail("Brave must not be read when extractor setup is invalid")

    pipeline.snapshot_fn = unexpected_snapshot
    with pytest.raises(saved_fetch.SavedFetchError, match="XDIGEST_DONSETCH_BIN"):
        pipeline.sync()
    with pipeline.database.connect() as connection:
        assert connection.execute("SELECT attempts FROM saved_items").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM bronze_objects").fetchone()[0] == 0


def test_user_install_is_found_without_user_path(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / ".local/bin/donsetch"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert saved_fetch.resolve_donsetch_bin("donsetch") == str(binary)
    with pytest.raises(saved_fetch.SavedFetchError):
        saved_fetch.resolve_donsetch_bin("misspelled-extractor")
