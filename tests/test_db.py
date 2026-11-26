import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from typer.testing import CliRunner

from flatlas import db
from flatlas.cli import app
from flatlas.errors import FlatlasError
from flatlas.paths import (
    discover_namespace,
    register_namespace,
)
from flatlas.scanning import scan_directory

pytestmark = pytest.mark.db


@pytest.mark.scan
@pytest.mark.query
def test_ls_other_directory_during_scan(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite"
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (first / "new.bin").write_bytes(b"new")
    (second / "kept.bin").write_bytes(b"kept")
    writer = db.open_database(database)
    queried = False

    def query_during_scan(progress) -> None:
        nonlocal queried
        if queried or progress.phase != "scanning" or progress.files_seen == 0:
            return
        queried = True
        assert writer.in_transaction
        result = CliRunner().invoke(app, ["ls", str(second), "--db", str(database), "--format", "jsonl"])
        assert result.exit_code == 0, result.output
        assert "kept.bin" in result.stdout
        assert "migration" not in result.stderr

    try:
        register_namespace(writer, discover_namespace(first))
        scan_directory(writer, second)
        scan_directory(writer, first, on_progress=query_during_scan)
        assert queried
        assert writer.execute(
            "SELECT state FROM path_with_display WHERE path_display=?", (str(second / "kept.bin"),)
        ).fetchone()[0] == "present"
    finally:
        writer.close()


def test_current_schema_migration_is_read_only(tmp_path: Path) -> None:
    connection = db.open_database(tmp_path / "index.sqlite")
    try:
        connection.execute("PRAGMA query_only = ON")
        progress = []
        db.migrate(connection, on_migration=progress.append)
        assert not connection.in_transaction
        assert progress == []
    finally:
        connection.close()


def test_pending_migration_requires_write_lock(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite"
    writer = db.open_database(database)
    reader = sqlite3.connect(database, timeout=0)
    reader.row_factory = sqlite3.Row
    try:
        writer.execute("DELETE FROM schema_migration WHERE version=3")
        writer.commit()
        writer.execute("UPDATE schema_migration SET description=description")
        progress = []
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            db.migrate(reader, on_migration=progress.append)
        assert progress == []
        assert not reader.in_transaction
        writer.rollback()
        db.migrate(reader, on_migration=progress.append)
        assert [(event.version, event.phase) for event in progress] == [(3, "started"), (3, "completed")]
    finally:
        reader.close()
        writer.close()


def test_concurrent_openers_recheck_versions_after_lock(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "index.sqlite"
    connection = db.open_database(database)
    connection.execute("DELETE FROM schema_migration WHERE version=3")
    connection.commit()
    connection.close()
    barrier = Barrier(2)
    real_versions = db._migration_versions

    def synchronized_versions(connection):
        versions = real_versions(connection)
        if not connection.in_transaction:
            barrier.wait(timeout=10)
        return versions

    monkeypatch.setattr(db, "_migration_versions", synchronized_versions)

    def open_index():
        progress = []
        connection = db.open_database(database, on_migration=progress.append)
        try:
            assert connection.execute("SELECT count(*) FROM schema_migration").fetchone()[0] == db.SCHEMA_VERSION
            return [(event.version, event.phase) for event in progress]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: open_index(), range(2)))
    assert sorted(results) == [[], [(3, "started"), (3, "completed")]]


def test_failed_migration_rolls_back_schema_and_preserves_data(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "index.sqlite"
    connection = db.open_database(database)
    register_namespace(connection, discover_namespace(tmp_path))
    connection.execute("DROP TABLE directory_summary")
    connection.execute("DELETE FROM schema_migration WHERE version IN (2, 3, 4, 5, 6, 7)")
    connection.commit()
    connection.close()

    def interrupt_backfill(*args, **kwargs):
        raise RuntimeError("injected migration failure")

    with monkeypatch.context() as patch:
        patch.setattr("flatlas.summaries.backfill_directory_summaries", interrupt_backfill)
        progress = []
        with pytest.raises(RuntimeError, match="injected migration failure"):
            db.open_database(database, on_migration=progress.append)
        assert not any(event.phase == "completed" for event in progress)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT count(*) FROM root").fetchone()[0] == 1
        assert connection.execute("SELECT version FROM schema_migration").fetchall() == [(1,), (8,)]
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='directory_summary'"
        ).fetchone() is None
    finally:
        connection.close()
    connection = db.open_database(database)
    try:
        assert connection.execute("SELECT count(*) FROM schema_migration").fetchone()[0] == db.SCHEMA_VERSION
        assert connection.execute("SELECT count(*) FROM root").fetchone()[0] == 1
    finally:
        connection.close()


def test_v8_migrates_populated_index_in_place(tmp_path: Path, monkeypatch) -> None:
    from flatlas.hashing import ensure_duplicate_hashes
    from flatlas.planning import create_dry_run_plan

    database = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    source.mkdir()
    for name in ("a", "b"):
        (source / name).write_bytes(b"x" * 16384)
    connection = db.open_database(database)
    root_id = register_namespace(connection, discover_namespace(source))
    scan_directory(connection, source)
    ensure_duplicate_hashes(connection, source)
    create_dry_run_plan(connection, root_id)
    with connection:
        connection.execute("DROP VIEW path_with_display")
        connection.execute("ALTER TABLE path ADD COLUMN path_display TEXT NOT NULL DEFAULT ''")
        connection.execute(
            """UPDATE path SET path_display=flatlas_display_path(
                (SELECT root_path_raw FROM root WHERE id=root_id), path_key,
                (SELECT platform FROM root WHERE id=root_id))"""
        )
        connection.execute("DROP INDEX idx_plan_operation_canonical_path")
        connection.execute("DROP INDEX idx_plan_operation_replacement_path")
        connection.execute("ALTER TABLE scan DROP COLUMN refresh_changed")
        connection.execute("ALTER TABLE directory_summary DROP COLUMN enumerated_mtime_ns")
        connection.execute("ALTER TABLE directory_summary DROP COLUMN enumerated_change_time_ns")
        connection.execute("DELETE FROM schema_migration WHERE version=8")
    tables = ("root", "scan", "scan_scope", "scan_error", "file_hash", "plan", "plan_operation", "directory_summary")
    columns = {
        table: ", ".join(row["name"] for row in connection.execute(f"PRAGMA table_info({table})"))
        for table in tables
    }
    before = {table: connection.execute(f"SELECT {cols} FROM {table}").fetchall() for table, cols in columns.items()}
    paths = connection.execute("SELECT id, path_key, path_display FROM path ORDER BY id").fetchall()
    connection.close()

    migrate_v8 = db._migrate_v8

    def fail_after_ddl(connection: sqlite3.Connection) -> None:
        migrate_v8(connection)
        raise RuntimeError("injected v8 migration failure")

    with monkeypatch.context() as patch:
        patch.setattr(db, "_migrate_v8", fail_after_ddl)
        with pytest.raises(RuntimeError, match="injected v8 migration failure"):
            db.open_database(database)
    with sqlite3.connect(database) as raw:
        assert raw.execute("SELECT max(version) FROM schema_migration").fetchone()[0] == 7
        assert raw.execute("SELECT id, path_key, path_display FROM path ORDER BY id").fetchall() == [
            tuple(row) for row in paths
        ]
    connection = db.open_database(database)
    try:
        assert connection.execute("SELECT max(version) FROM schema_migration").fetchone()[0] == 8
        assert connection.execute("SELECT id, path_key, path_display FROM path_with_display ORDER BY id").fetchall() == paths
        assert "path_display" not in {row["name"] for row in connection.execute("PRAGMA table_info(path)")}
        for table, cols in columns.items():
            assert connection.execute(f"SELECT {cols} FROM {table}").fetchall() == before[table]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN DELETE FROM path WHERE id=1")]
        assert any("idx_plan_operation_canonical_path" in step for step in plan)
        assert any("idx_plan_operation_replacement_path" in step for step in plan)
    finally:
        connection.close()


@pytest.mark.safety
def test_open_database_readonly_wraps_sqlite_errors(tmp_path: Path, monkeypatch) -> None:
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(FlatlasError, match="cannot open index database for reading"):
        db.open_database_readonly(corrupt)

    database = tmp_path / "index.sqlite"
    db.open_database(database).close()
    Path(f"{database}-wal").write_bytes(b"active-wal")

    def fail_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated read error")

    monkeypatch.setattr(db, "_connect_readonly", fail_connect)
    with pytest.raises(FlatlasError, match="cannot open index database for reading"):
        db.open_database_readonly(database)

    Path(f"{database}-wal").unlink()
    with pytest.raises(FlatlasError, match="cannot open index database for reading"):
        db.open_database_readonly(database)

    # A symlinked index checks <resolved>-wal rather than <symlink>-wal before falling back.
    alias = tmp_path / "alias.sqlite"
    alias.write_bytes(b"placeholder")
    Path(f"{database}-wal").write_bytes(b"active-wal")
    original_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self: database if self == alias else original_resolve(self))
    calls: list[str] = []

    def record_and_fail(uri: str):
        calls.append(uri)
        raise sqlite3.OperationalError("simulated read error")

    monkeypatch.setattr(db, "_connect_readonly", record_and_fail)
    with pytest.raises(FlatlasError, match="cannot open index database for reading"):
        db.open_database_readonly(alias)
    assert len(calls) == 1 and "immutable=1" not in calls[0]
