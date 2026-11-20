"""du on indexed special entries reports unknown, not zero."""

import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas.cli import app
from flatlas.core import (
    discover_namespace,
    disk_usage,
    open_database,
    register_namespace,
    scan_directory,
)

pytestmark = [pytest.mark.query, pytest.mark.safety]


@pytest.mark.skipif(sys.platform == "win32" or not hasattr(os, "mkfifo"), reason="needs a POSIX FIFO")
def test_du_reports_special_entries_as_unknown(tmp_path: Path) -> None:
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

    result = CliRunner().invoke(app, ["du", "--db", str(db), "--format", "json", str(fifo)])
    assert result.exit_code == 0, result.output
    [payload] = json.loads(result.output)
    assert payload["files"] is None and payload["logical_size"] is None
    table = CliRunner().invoke(app, ["du", "--db", str(db), str(fifo)])
    assert table.exit_code == 0, table.output
    assert table.output.splitlines()[-1].split()[1:3] == ["-", "-"]  # S, SIZE(B), N


def test_du_follows_a_symlink_argument_to_its_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "real").mkdir(parents=True)
    (source / "real" / "file.bin").write_bytes(b"data")
    link = source / "alias"
    try:
        link.symlink_to(source / "real", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    connection = open_database(tmp_path / "index.sqlite")
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        [row] = disk_usage(connection, scope=link)
        assert (row["files"], row["logical_size"]) == (1, 4)
    finally:
        connection.close()
