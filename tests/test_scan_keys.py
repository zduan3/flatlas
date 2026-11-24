"""Scanner-derived path keys."""

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

