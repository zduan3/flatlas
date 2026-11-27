"""Scanner-derived path keys and the key-range missing pass."""

import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas import scanning
from flatlas.cli import app
from flatlas.db import open_database
from flatlas.paths import (
    discover_namespace,
    filesystem_path,
    path_key,
    register_namespace,
)
from flatlas.scanning import scan_directory

pytestmark = [pytest.mark.scan, pytest.mark.safety]


def _tree(source: Path) -> None:
    for relative in ("a/x/deep.txt", "a b/y.txt", "a-b/z.txt", "a.b", "ünï code/w.txt", "b/c/d/e/f.txt"):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")


def _present(connection: sqlite3.Connection) -> dict[bytes, str]:
    return {
        bytes(row["path_key"]): str(row["entry_kind"])
        for row in connection.execute("SELECT path_key, entry_kind FROM path WHERE state='present'")
    }


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows path prefixes")
def test_windows_io_prefix_uses_utf16_length_and_preserves_special_namespaces() -> None:
    short = Path(r"C:\short")
    assert filesystem_path(short) is short
    long_drive = short / ("😀" * 70) / ("😀" * 70)
    assert str(filesystem_path(long_drive)) == "\\\\?\\" + str(long_drive)
    long_unc = Path(r"\\server\share") / ("a" * 140) / ("b" * 140)
    assert str(filesystem_path(long_unc)) == "\\\\?\\UNC\\" + str(long_unc)[2:]
    prefixed = Path("\\\\?\\" + str(long_drive))
    assert filesystem_path(prefixed) is prefixed


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows long paths and allocation")
def test_windows_scan_and_resume_keep_long_paths_under_existing_keys(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    deep = source / ("a" * 98) / ("b" * 98) / ("目录" * 30)
    extended = Path("\\\\?\\" + str(deep))
    assert len(str(deep)) > 260
    with closing(open_database(database)) as connection:
        try:
            extended.mkdir(parents=True)
            (extended / "payload.bin").write_bytes(b"x" * 5000)
            (extended / "missing.bin").touch()
            namespace = discover_namespace(source)
            root_id = register_namespace(connection, namespace)
            assert discover_namespace(deep) == discover_namespace(extended) == namespace
            indexed = scan_directory(connection, source)
            assert indexed["status"] == "completed"
            assert indexed["files_seen"] == 2
            row = connection.execute(
                "SELECT path_display, logical_size, allocated_size FROM path_with_display WHERE root_id=? AND path_key=?",
                (root_id, path_key(namespace, deep / "payload.bin")),
            ).fetchone()
            assert row["path_display"] == str(deep / "payload.bin")
            assert row["logical_size"] == 5000 and row["allocated_size"] is not None

            (extended / "missing.bin").unlink()
            with monkeypatch.context() as patch:
                def denied(_path):
                    raise PermissionError(13, "injected enumeration failure")

                patch.setattr(scanning.os, "scandir", denied)
                partial = scan_directory(connection, extended)
            assert partial["status"] == "partial"
            assert partial["paths_deleted"] == 0
            missing = path_key(namespace, deep / "missing.bin")
            assert missing in _present(connection)

            resumed = CliRunner().invoke(app, ["scan", str(deep), "--resume", "--db", str(database)])
            assert resumed.exit_code == 0, resumed.output
            record = connection.execute("SELECT id, status FROM scan ORDER BY id DESC LIMIT 1").fetchone()
            assert tuple(record) == (partial["scan_id"] + 1, "completed")
            assert missing not in _present(connection)
            keys = connection.execute("SELECT DISTINCT scope_path_key FROM scan_scope WHERE scan_id >= ?", (partial["scan_id"],))
            assert [row[0] for row in keys] == [path_key(namespace, deep)]
        finally:
            shutil.rmtree(Path("\\\\?\\" + str(source)), ignore_errors=True)


def test_scanned_keys_equal_path_key_of_each_path(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _tree(source)
    if sys.platform != "win32":
        raw = os.fsencode(source) + b"/bad-\xff-name"
        try:
            os.mkdir(raw)
            with open(raw + b"/inner", "wb") as handle:
                handle.write(b"x")
        except OSError:
            pass
    connection = open_database(tmp_path / "index.sqlite")
    try:
        namespace = discover_namespace(source)
        register_namespace(connection, namespace)
        scan_directory(connection, source)
        expected = {path_key(namespace, source)}
        for directory, dirnames, filenames in os.walk(source):
            for name in dirnames + filenames:
                expected.add(path_key(namespace, Path(directory) / name))
        keys = set(_present(connection))
        assert expected <= keys
        assert {key for key in keys if key.startswith(path_key(namespace, source))} == expected
    finally:
        connection.close()


def test_subtree_rescan_keeps_siblings_that_share_a_prefix(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _tree(source)
    connection = open_database(tmp_path / "index.sqlite")
    try:
        namespace = discover_namespace(source)
        register_namespace(connection, namespace)
        scan_directory(connection, source)
        before = _present(connection)
        (source / "a" / "x" / "deep.txt").unlink()
        scan_directory(connection, source / "a")
        after = _present(connection)
        gone = path_key(namespace, source / "a" / "x" / "deep.txt")
        assert gone in before and gone not in after
        # "a b", "a-b" and "a.b" sort right after "a" but are not under it.
        assert {key for key in before if key != gone} == set(after)
    finally:
        connection.close()


def test_first_scan_skips_the_missing_pass_and_rescans_still_confirm(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _tree(source)
    connection = open_database(tmp_path / "index.sqlite")
    statements: list[str] = []
    try:
        namespace = discover_namespace(source)
        register_namespace(connection, namespace)
        connection.set_trace_callback(statements.append)
        scan_directory(connection, source)
        assert not [sql for sql in statements if "SET state='deleted'" in sql]
        statements.clear()
        (source / "b" / "c" / "d" / "e" / "f.txt").unlink()
        (source / "b" / "c" / "d" / "e").rmdir()
        scan_directory(connection, source)
        assert [sql for sql in statements if "SET state='deleted'" in sql]
        connection.set_trace_callback(None)
        present = _present(connection)
        assert path_key(namespace, source / "b" / "c" / "d" / "e") not in present
        assert path_key(namespace, source / "b" / "c" / "d" / "e" / "f.txt") not in present
        assert path_key(namespace, source / "b" / "c" / "d") in present
    finally:
        connection.close()
