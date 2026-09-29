"""Read a browser-store copy without changing the original files."""

import hashlib
import shutil
import struct
from pathlib import Path

import pytest

from x_digest.brave import (
    BraveReaderError,
    current_manifest_name,
    read_entries,
    snapshot_leveldb,
)

ONE_BYTE_VARINT_LIMIT = 128


def entry_bytes(url: str, title: str = "Saved article") -> bytes:
    def text(number: int, value: str) -> bytes:
        encoded = value.encode()
        assert len(encoded) < ONE_BYTE_VARINT_LIMIT
        return bytes([number * 8 + 2, len(encoded)]) + encoded

    return text(1, url) + text(2, title) + text(3, url)


def fixture_store(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "CURRENT").write_text("MANIFEST-000001\n")
    (source / "MANIFEST-000001").write_bytes(b"fixture manifest")
    return source


def test_snapshot_and_decode_preserve_source(tmp_path: Path) -> None:
    source = fixture_store(tmp_path)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    snapshot = snapshot_leveldb(source, retries=0)
    url = "https://example.org/article"
    try:
        entries, counts = read_entries(
            snapshot, "Default", lambda _: [(b"reading_list-dt-" + url.encode(), entry_bytes(url))]
        )
        assert counts["entries"] == 1
        assert entries[0].url == url
        assert entries[0].title == "Saved article"
        assert entries[0].status_text == "unseen"
        assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    finally:
        shutil.rmtree(snapshot)


def test_invalid_reading_list_entry_is_not_silently_skipped(tmp_path: Path) -> None:
    source = fixture_store(tmp_path)
    with pytest.raises(BraveReaderError, match="invalid reading-list record"):
        read_entries(source, "Default", lambda _: [(b"reading_list-dt-https://example.org", b"\0")])
    with pytest.raises(BraveReaderError, match="does not match"):
        read_entries(
            source,
            "Default",
            lambda _: [
                (b"reading_list-dt-https://example.org", entry_bytes("https://example.net"))
            ],
        )


def test_manifest_cannot_escape_snapshot(tmp_path: Path) -> None:
    source = fixture_store(tmp_path)
    (source / "CURRENT").write_text("../MANIFEST-000001\n")
    with pytest.raises(BraveReaderError, match="basename"):
        current_manifest_name(source)


def test_mutating_store_fails_bounded_snapshot(tmp_path: Path, monkeypatch) -> None:
    source = fixture_store(tmp_path)
    original = shutil.copyfile

    def changing_copy(src, dst):
        result = original(src, dst)
        (source / "000099.log").write_bytes(b"new write")
        return result

    monkeypatch.setattr(shutil, "copyfile", changing_copy)
    with pytest.raises(BraveReaderError, match="stable snapshot"):
        snapshot_leveldb(source, retries=0)


def test_reader_excludes_deleted_and_obsolete_records(tmp_path: Path) -> None:
    pytest.importorskip("chromium_reader")
    source = fixture_store(tmp_path)

    def log_record(body: bytes) -> bytes:
        return struct.pack("<IHB", 0, len(body), 1) + body

    def batch(seq: int, url: str, deleted: bool = False) -> bytes:
        key = b"reading_list-dt-" + url.encode()
        operation = bytes([0 if deleted else 1, len(key)]) + key
        if not deleted:
            value = entry_bytes(url)
            assert len(value) < ONE_BYTE_VARINT_LIMIT
            operation += bytes([len(value)]) + value
        return log_record(struct.pack("<QI", seq, 1) + operation)

    # Manifest tag 2 is log_number. CURRENT selects this manifest, not the newest filename.
    (source / "MANIFEST-000001").write_bytes(log_record(bytes([2, 2])))
    (source / "MANIFEST-000099").write_bytes(log_record(bytes([2, 1])))
    (source / "000001.log").write_bytes(batch(99, "https://example.org/obsolete"))
    url = "https://example.org/live"
    (source / "000002.log").write_bytes(
        batch(1, url)
        + batch(2, "https://example.org/deleted")
        + batch(3, "https://example.org/deleted", deleted=True)
    )
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
    snapshot = snapshot_leveldb(source, retries=0)
    try:
        entries, _ = read_entries(snapshot, "Default")
        assert [entry.url for entry in entries] == [url]
        assert {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()
        } == before
    finally:
        shutil.rmtree(snapshot)
