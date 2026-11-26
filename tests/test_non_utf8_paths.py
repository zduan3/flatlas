"""Names that are not valid UTF-8 must not abort a scan.

POSIX names are bytes; Python decodes undecodable bytes to lone surrogates,
which SQLite TEXT cannot store. Before the fix, one such name raised
UnicodeEncodeError mid-transaction and rolled back the entire scan.
"""

import os
import sys
from contextlib import closing
from pathlib import Path

import pytest

from flatlas.db import open_database
from flatlas.paths import (
    discover_namespace,
    display_path,
    path_key,
    register_namespace,
    remove_deleted_descendants,
)
from flatlas.scanning import scan_directory

pytestmark = [
    pytest.mark.scan,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a filesystem that accepts arbitrary name bytes"),
]


def _make(parent: Path, raw_name: bytes, *, directory: bool = False) -> Path:
    target = os.path.join(os.fsencode(parent), raw_name)
    if directory:
        os.mkdir(target)
    else:
        with open(target, "wb") as stream:
            stream.write(b"payload")
    return Path(os.fsdecode(target))


def test_display_path_escapes_only_undecodable_bytes() -> None:
    assert display_path(Path(os.fsdecode(b"/a/caf\xc3\xa9/bad\xffname"))) == "/a/café/bad\\xffname"


def test_scan_indexes_non_utf8_names_and_completes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    bad_file = _make(source, b"bad\xffname")
    bad_dir = _make(source, b"dir\xfe", directory=True)
    _make(bad_dir, b"inner\xfd.bin")
    connection = open_database(tmp_path / "index.sqlite")
    namespace = discover_namespace(source)
    try:
        register_namespace(connection, namespace)
        result = scan_directory(connection, source)
        assert result["status"] == "completed"
        assert result["files_seen"] == 2
        row = connection.execute(
            "SELECT path_display, state FROM path_with_display WHERE path_key=?", (path_key(namespace, bad_file),)
        ).fetchone()
        assert row["state"] == "present"
        assert row["path_display"].endswith("bad\\xffname")

        # A rescan keeps the same identities and, after deletion, confirms it by key.
        os.unlink(os.fsencode(bad_file))
        assert scan_directory(connection, source)["status"] == "completed"
        assert connection.execute(
            "SELECT state FROM path WHERE path_key=?", (path_key(namespace, bad_file),)
        ).fetchone()["state"] == "deleted"
    finally:
        connection.close()


@pytest.mark.safety
def test_purge_uses_raw_namespace_identity_when_display_paths_collide(tmp_path: Path, monkeypatch) -> None:
    raw = _make(tmp_path, b"root\xff", directory=True)
    literal = _make(tmp_path, b"root\\xff", directory=True)
    assert display_path(raw) == display_path(literal)
    original_ismount = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda path: path in (raw, literal) or original_ismount(path))
    with closing(open_database(tmp_path / "index.sqlite")) as connection:
        raw_id = register_namespace(connection, discover_namespace(raw))
        register_namespace(connection, discover_namespace(literal))
        for root in (raw, literal):
            file = _make(root, b"file")
            scan_directory(connection, root)
            file.unlink()
            scan_directory(connection, root)

        assert remove_deleted_descendants(connection, literal)["paths_removed"] == 1
        tombstones = connection.execute("SELECT root_id FROM path WHERE state='deleted'").fetchall()
        assert [row["root_id"] for row in tombstones] == [raw_id]
        assert remove_deleted_descendants(connection, raw)["paths_removed"] == 1


@pytest.mark.skipif(sys.platform == "linux" and os.geteuid() == 0, reason="root can enumerate a mode-000 directory")
def test_error_message_with_non_utf8_name_is_recorded(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    locked = _make(source, b"locked\xfe", directory=True)
    os.chmod(os.fsencode(locked), 0)
    connection = open_database(tmp_path / "index.sqlite")
    try:
        register_namespace(connection, discover_namespace(source))
        result = scan_directory(connection, source)
        assert result["status"] == "partial"
        message = connection.execute("SELECT message FROM scan_error").fetchone()["message"]
        assert "locked\\udcfe" in message
    finally:
        os.chmod(os.fsencode(locked), 0o700)
        connection.close()
