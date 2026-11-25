"""The public Python API shares CLI records and opens the index read-only."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas import api
from flatlas.cli import app
from flatlas.db import open_database
from flatlas.errors import FlatlasError
from flatlas.paths import discover_namespace, register_namespace
from flatlas.queries import FindHit
from flatlas.scanning import scan_directory

pytestmark = [pytest.mark.query, pytest.mark.safety]


def test_api_matches_cli_machine_output_for_an_offline_tree(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    for index in range(12):
        (source / "nested" / f"测试{index}.bin").write_bytes(b"x" * (index + 1))
    db = tmp_path / "index.sqlite"
    with closing(open_database(db)) as connection:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        scan_id = scan_directory(connection, source / "nested")["scan_id"]
    source.rename(tmp_path / "offline")
    monkeypatch.setenv("FLATLAS_DB", str(db))
    runner = CliRunner()

    def cli_records(command: str, *args: str, fmt: str = "jsonl"):
        result = runner.invoke(app, [command, "--format", fmt, *args])
        assert result.exit_code == 0, (result.output, result.exception)
        if fmt == "json":
            return json.loads(result.stdout)
        return [json.loads(line) for line in result.stdout.splitlines()]

    with api.open_index() as idx:
        expression = ("-type", "f", "-name", "*.bin")
        assert list(idx.find(source, expression)) == cli_records("find", str(source), *expression)
        assert idx.du(source, max_depth=1, freed=True) == cli_records("du", str(source), "-d", "1", "--freed")
        largest = idx.largest(source)
        assert len(largest) == 12 and largest == cli_records("largest", str(source))
        assert idx.largest(source, limit=2) == largest[:2]
        assert idx.roots() == cli_records("roots")
        assert idx.scans(source, limit=1) == cli_records("scans", str(source), "--limit", "1")
        assert idx.scan_info(scan_id) == cli_records("scan-info", str(scan_id), fmt="json")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            idx._connection.execute("DELETE FROM scan")

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        idx.roots()


def test_find_is_lazy(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "index.sqlite"
    with closing(open_database(db)):
        pass
    fetched = []

    def hits(*_args, **_kwargs):
        for path in ("one", "two"):
            fetched.append(path)
            yield FindHit(path, "\n", {"path": path})

    monkeypatch.setattr(api, "find_paths", hits)
    with api.open_index(db) as idx:
        records = idx.find()
        assert fetched == []
        assert next(records) == {"path": "one"}
        assert fetched == ["one"]
        assert list(records) == [{"path": "two"}]


def test_api_rejects_missing_or_outdated_indexes_without_migrating(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    with pytest.raises(FlatlasError, match="does not exist"), api.open_index(db):
        pytest.fail("opened a missing database")
    assert not db.exists()
    with closing(open_database(db)) as connection:
        with connection:
            connection.execute("DELETE FROM schema_migration WHERE version=(SELECT max(version) FROM schema_migration)")
        before = connection.execute("SELECT count(*) FROM schema_migration").fetchone()[0]
        with pytest.raises(FlatlasError, match="needs migration"), api.open_index(db):
            pytest.fail("opened a database with an outdated schema")
        assert connection.execute("SELECT count(*) FROM schema_migration").fetchone()[0] == before
