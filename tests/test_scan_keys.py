"""Scanner-derived path keys and the key-range missing pass."""

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from flatlas.core import (
    discover_namespace,
    open_database,
    register_namespace,
    scan_directory,
)
from flatlas.paths import path_key

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
