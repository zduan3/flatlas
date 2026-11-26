"""Hash validation on roots scanned with lazy metadata.

Lazy Lustre metadata can carry a modification time that trails the object
storage targets by about a second. Hashing compares the indexed basis against
``fstat``; on lazy roots a time-only difference is reconciled (exact times are
written back) instead of marking the file changed forever. Identity and size
must still match, and exact roots stay strict.
"""

from pathlib import Path

import pytest

from flatlas import hashing
from flatlas.db import open_database
from flatlas.paths import (
    discover_namespace,
    register_namespace,
)
from flatlas.scanning import scan_directory

pytestmark = [pytest.mark.hash, pytest.mark.safety]

LAG_NS = 1_000_000_000


def _index_pair(tmp_path: Path, fs_type: str):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("a.bin", "b.bin"):
        (source / name).write_bytes(b"same content " * 1000)
    connection = open_database(tmp_path / "index.sqlite")
    root_id = register_namespace(connection, discover_namespace(source))
    with connection:
        connection.execute("UPDATE root SET filesystem_type=? WHERE id=?", (fs_type, root_id))
    assert scan_directory(connection, source)["status"] == "completed"
    exact = {name: (source / name).stat().st_mtime_ns for name in ("a.bin", "b.bin")}
    with connection:
        connection.execute(f"UPDATE path SET mtime_ns = mtime_ns - {LAG_NS} WHERE entry_kind='file'")
    return connection, source, exact


def _indexed_mtimes(connection) -> dict[str, int]:
    rows = connection.execute("SELECT path_display, mtime_ns FROM path_with_display WHERE entry_kind='file'")
    return {Path(row["path_display"]).name: int(row["mtime_ns"]) for row in rows}


def test_lazy_root_reconciles_trailing_mtime_and_finds_the_group(tmp_path: Path) -> None:
    connection, source, exact = _index_pair(tmp_path, "lustre")
    try:
        summary = hashing.ensure_duplicate_hashes(connection, source)
        assert summary.complete
        assert summary.changed_files == 0
        assert _indexed_mtimes(connection) == exact
        states = {row[0] for row in connection.execute("SELECT state FROM file_hash")}
        assert states == {"full_ready"}
        # A second run reuses the reconciled basis without reading content again.
        assert hashing.ensure_duplicate_hashes(connection, source).content_files_read == 0
    finally:
        connection.close()


def test_exact_root_still_treats_a_time_difference_as_a_change(tmp_path: Path) -> None:
    connection, source, _exact = _index_pair(tmp_path, "ext4")
    try:
        summary = hashing.ensure_duplicate_hashes(connection, source)
        assert summary.changed_files == 2
        assert not summary.complete
    finally:
        connection.close()


def test_lazy_root_still_rejects_a_size_change(tmp_path: Path) -> None:
    connection, source, _exact = _index_pair(tmp_path, "lustre")
    try:
        (source / "a.bin").write_bytes(b"different and longer content " * 1000)
        summary = hashing.ensure_duplicate_hashes(connection, source)
        assert summary.changed_files == 1
    finally:
        connection.close()
