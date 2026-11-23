"""du on indexed special entries reports unknown, not zero."""

import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas.cli import app
from flatlas.db import open_database
from flatlas.paths import (
    discover_namespace,
    register_namespace,
)
from flatlas.queries import disk_usage
from flatlas.scanning import scan_directory

pytestmark = [pytest.mark.query, pytest.mark.safety]


def test_du_reports_special_entries_as_unknown(tmp_path: Path) -> None:
    if sys.platform == "win32" or not hasattr(os, "mkfifo"):
        pytest.skip("needs a POSIX FIFO")
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.bin").write_bytes(b"data")
    fifo = source / "pipe"
    os.mkfifo(fifo)
    db = tmp_path / "index.sqlite"
    connection = open_database(db)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        [row] = disk_usage(connection, scope=fifo)
        assert (row["files"], row["logical_size"], row["allocated_size"]) == (None, None, None)
        # The directory total still counts only the regular file.
        [total] = disk_usage(connection, scope=source)
        assert (total["files"], total["logical_size"]) == (1, 4)
    finally:
        connection.close()

    result = CliRunner().invoke(app, ["du", "--db", str(db), "--format", "jsonl", str(fifo)])
    assert result.exit_code == 0, result.output
    [payload] = [json.loads(line) for line in result.output.splitlines()]
    assert payload["files"] is None and payload["logical_size"] is None
    table = CliRunner().invoke(app, ["du", "--db", str(db), str(fifo)])
    assert table.exit_code == 0, table.output
    assert table.output.splitlines()[-1].split()[1:3] == ["-", "-"]  # S, SIZE(B), N


@pytest.fixture
def linked_index(tmp_path: Path):
    source = tmp_path / "source"
    (source / "real").mkdir(parents=True)
    (source / "real" / "file.bin").write_bytes(b"data")
    link = source / "alias"
    try:
        link.symlink_to(source / "real", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    db = tmp_path / "index.sqlite"
    connection = open_database(db)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
    finally:
        connection.close()
    return db, source, link


def test_du_does_not_follow_a_symlink_argument_by_default(linked_index) -> None:
    db, _source, link = linked_index
    connection = open_database(db)
    try:
        [row] = disk_usage(connection, scope=link)
        assert row["path"].endswith("alias")
        assert (row["files"], row["logical_size"]) == (None, None)
        # A trailing slash dereferences, as in GNU du.
        [slash] = disk_usage(connection, scope=f"{link}/")
        assert (slash["files"], slash["logical_size"]) == (1, 4)
    finally:
        connection.close()


def test_du_dereference_args_follows_the_symlink(linked_index) -> None:
    db, _source, link = linked_index
    for flag in ("-D", "-H", "--dereference-args"):
        result = CliRunner().invoke(app, ["du", flag, "--db", str(db), "--format", "jsonl", str(link)])
        assert result.exit_code == 0, result.output
        [payload] = [json.loads(line) for line in result.stdout.splitlines()]
        assert (payload["files"], payload["logical_size"]) == (1, 4)


def test_du_reports_a_failing_argument_and_continues(linked_index, tmp_path: Path) -> None:
    db, source, _link = linked_index
    outside = tmp_path / "missing"
    result = CliRunner().invoke(
        app, ["du", "--db", str(db), "--format", "jsonl", str(outside), str(source / "real")]
    )
    assert result.exit_code == 1
    assert "missing" in result.stderr
    [payload] = [json.loads(line) for line in result.stdout.splitlines()]
    assert (payload["files"], payload["logical_size"]) == (1, 4)
