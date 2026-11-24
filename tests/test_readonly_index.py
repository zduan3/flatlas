"""Query commands work on an index the caller cannot write; writers fail cleanly.

Reproduces an agent sandbox: the index directory is read-only, so SQLite can
neither take write locks nor create the WAL ``-shm`` file.
"""

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas.cli import app
from flatlas.db import (
    open_database,
    open_database_readonly,
)
from flatlas.errors import FlatlasError
from flatlas.paths import (
    discover_namespace,
    register_namespace,
)
from flatlas.scanning import scan_directory

pytestmark = [
    pytest.mark.cli,
    pytest.mark.db,
    pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions enforced"),
]


@pytest.fixture
def readonly_index(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.bin").write_bytes(b"data")
    store = tmp_path / "store"
    store.mkdir()
    database = store / "index.sqlite"
    connection = open_database(database)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
    finally:
        connection.close()
    assert not Path(f"{database}-wal").exists()
    store.chmod(0o555)
    database.chmod(0o444)
    try:
        yield database, source
    finally:
        store.chmod(0o755)
        database.chmod(0o644)


def test_query_commands_fall_back_to_read_only(readonly_index) -> None:
    database, source = readonly_index
    runner = CliRunner()
    for command in (["roots"], ["scans"], ["du", str(source)], ["largest", str(source)]):
        result = runner.invoke(app, [*command, "--db", str(database)])
        assert result.exit_code == 0, (command, result.output, result.exception)
    du = runner.invoke(app, ["du", str(source), "--format", "json", "--db", str(database)])
    assert '"logical_size": 4' in du.stdout
    assert sorted(path.name for path in database.parent.iterdir()) == ["index.sqlite"]


def test_write_commands_report_a_domain_error(readonly_index) -> None:
    database, source = readonly_index
    result = CliRunner().invoke(app, ["scan", str(source), "--db", str(database)])
    assert isinstance(result.exception, FlatlasError)
    assert "cannot open index database for writing" in str(result.exception)


def test_read_only_open_refuses_an_unmigrated_schema(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite"
    connection = open_database(database)
    with connection:
        connection.execute("DELETE FROM schema_migration WHERE version = (SELECT max(version) FROM schema_migration)")
    connection.close()
    with pytest.raises(FlatlasError, match="needs migration"):
        open_database_readonly(database)
    with pytest.raises(FlatlasError, match="does not exist"):
        open_database_readonly(tmp_path / "missing.sqlite")
