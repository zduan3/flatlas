"""SQLite connection and schema migration support."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 3

DDL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version INTEGER PRIMARY KEY, applied_at_ns INTEGER NOT NULL, description TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS root (
    id INTEGER PRIMARY KEY,
    root_path_raw BLOB NOT NULL UNIQUE,
    root_path_display TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('windows', 'linux', 'macos')),
    namespace_kind TEXT NOT NULL CHECK (namespace_kind IN ('windows_volume', 'posix_mount', 'path_anchor')),
    namespace_id_raw BLOB,
    filesystem_type TEXT,
    case_sensitive INTEGER CHECK (case_sensitive IN (0, 1)),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at_ns INTEGER NOT NULL,
    updated_at_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scan (
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES root(id),
    kind TEXT NOT NULL CHECK (kind IN ('full', 'subtree')),
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'partial', 'failed', 'cancelled')),
    started_at_ns INTEGER NOT NULL, finished_at_ns INTEGER,
    scanner_version TEXT NOT NULL,
    files_seen INTEGER NOT NULL DEFAULT 0, dirs_seen INTEGER NOT NULL DEFAULT 0,
    errors_seen INTEGER NOT NULL DEFAULT 0, note TEXT,
    refresh_before_ns INTEGER
);
CREATE TABLE IF NOT EXISTS scan_scope (
    id INTEGER PRIMARY KEY,
    scan_id INTEGER NOT NULL REFERENCES scan(id) ON DELETE CASCADE,
    root_id INTEGER NOT NULL REFERENCES root(id),
    scope_path_key BLOB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'partial', 'failed', 'cancelled')),
    started_at_ns INTEGER NOT NULL, finished_at_ns INTEGER,
    paths_seen INTEGER NOT NULL DEFAULT 0, error_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(scan_id, scope_path_key)
);
CREATE TABLE IF NOT EXISTS path (
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES root(id),
    parent_path_id INTEGER REFERENCES path(id),
    path_key BLOB NOT NULL, name_raw BLOB NOT NULL, path_display TEXT NOT NULL,
    entry_kind TEXT NOT NULL CHECK (entry_kind IN ('root', 'directory', 'file', 'symlink', 'reparse', 'other')),
    state TEXT NOT NULL CHECK (state IN ('present', 'deleted')),
    device INTEGER, inode INTEGER, object_id_raw BLOB, mode INTEGER, uid INTEGER, gid INTEGER,
    nlink INTEGER, logical_size INTEGER, allocated_size INTEGER, mtime_ns INTEGER,
    change_time_ns INTEGER, birth_time_ns INTEGER, symlink_target_raw BLOB, reparse_tag INTEGER,
    first_seen_scan_id INTEGER NOT NULL REFERENCES scan(id),
    last_seen_scan_id INTEGER NOT NULL REFERENCES scan(id),
    deleted_by_scope_id INTEGER REFERENCES scan_scope(id), deleted_at_ns INTEGER,
    UNIQUE(root_id, path_key),
    CHECK ((entry_kind = 'file' AND logical_size IS NOT NULL) OR entry_kind <> 'file')
);
CREATE TABLE IF NOT EXISTS scan_error (
    id INTEGER PRIMARY KEY,
    scan_scope_id INTEGER NOT NULL REFERENCES scan_scope(id) ON DELETE CASCADE,
    path_key BLOB NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('enumerate', 'stat', 'readlink', 'hash_quick', 'hash_full')),
    error_code TEXT, message TEXT NOT NULL, occurred_at_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS file_hash (
    path_id INTEGER PRIMARY KEY REFERENCES path(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('missing', 'quick_ready', 'full_ready', 'failed', 'stale')),
    quick_algorithm TEXT, quick_digest BLOB, quick_sample_bytes INTEGER,
    full_algorithm TEXT, full_digest BLOB,
    full_verification TEXT CHECK (full_verification IN ('hash_only', 'byte_compare')),
    basis_device INTEGER, basis_inode INTEGER, basis_size INTEGER, basis_mtime_ns INTEGER,
    basis_change_time_ns INTEGER, basis_birth_time_ns INTEGER,
    last_hashed_scan_id INTEGER REFERENCES scan(id), error_message TEXT,
    CHECK ((state = 'missing' AND quick_digest IS NULL AND full_digest IS NULL) OR state <> 'missing'),
    CHECK (full_digest IS NULL OR (full_algorithm IS NOT NULL AND basis_size IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS plan (
    id TEXT PRIMARY KEY, root_id INTEGER NOT NULL REFERENCES root(id),
    source_scan_id INTEGER REFERENCES scan(id),
    status TEXT NOT NULL CHECK (status IN ('draft', 'dry_run', 'superseded', 'applied', 'partially_applied', 'failed')),
    policy_json TEXT NOT NULL, created_at_ns INTEGER NOT NULL, immutable_at_ns INTEGER,
    exported_at_ns INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS plan_operation (
    id INTEGER PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('hardlink', 'reflink', 'delete')),
    canonical_path_id INTEGER NOT NULL REFERENCES path(id),
    replacement_path_id INTEGER NOT NULL REFERENCES path(id),
    expected_size INTEGER NOT NULL, expected_full_digest BLOB NOT NULL,
    expected_algorithm TEXT NOT NULL, preconditions_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('planned', 'skipped', 'applied', 'failed')),
    result_json TEXT, UNIQUE(plan_id, ordinal), UNIQUE(plan_id, replacement_path_id),
    CHECK(canonical_path_id <> replacement_path_id)
);
CREATE INDEX IF NOT EXISTS idx_scan_root_started ON scan(root_id, started_at_ns DESC);
CREATE INDEX IF NOT EXISTS idx_scope_scan_status ON scan_scope(scan_id, status);
CREATE INDEX IF NOT EXISTS idx_path_root_parent_live ON path(root_id, parent_path_id, state);
CREATE INDEX IF NOT EXISTS idx_path_root_kind_size_live ON path(root_id, entry_kind, logical_size)
    WHERE state = 'present' AND entry_kind = 'file';
CREATE INDEX IF NOT EXISTS idx_hash_quick ON file_hash(quick_algorithm, quick_digest)
    WHERE state IN ('quick_ready', 'full_ready');
CREATE INDEX IF NOT EXISTS idx_hash_full ON file_hash(full_algorithm, full_digest)
    WHERE state = 'full_ready';
CREATE INDEX IF NOT EXISTS idx_plan_root_created ON plan(root_id, created_at_ns DESC);
"""

DIRECTORY_SUMMARY_MIGRATION = """
CREATE TABLE IF NOT EXISTS directory_summary (
    path_id INTEGER PRIMARY KEY REFERENCES path(id) ON DELETE CASCADE,
    direct_files INTEGER NOT NULL DEFAULT 0 CHECK (direct_files >= 0),
    direct_logical_size INTEGER NOT NULL DEFAULT 0 CHECK (direct_logical_size >= 0),
    direct_allocated_size INTEGER CHECK (direct_allocated_size IS NULL OR direct_allocated_size >= 0),
    subtree_files INTEGER NOT NULL DEFAULT 0 CHECK (subtree_files >= 0),
    subtree_logical_size INTEGER NOT NULL DEFAULT 0 CHECK (subtree_logical_size >= 0),
    subtree_allocated_size INTEGER CHECK (subtree_allocated_size IS NULL OR subtree_allocated_size >= 0),
    self_completed_at_ns INTEGER,
    subtree_complete_through_ns INTEGER,
    last_completed_scan_id INTEGER REFERENCES scan(id),
    updated_at_ns INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_directory_summary_freshness
    ON directory_summary(self_completed_at_ns, subtree_complete_through_ns);
"""

REFRESH_SCAN_MIGRATION_DESCRIPTION = "record directory refresh cutoff"


@dataclass(frozen=True)
class MigrationProgress:
    version: int
    description: str
    phase: str
    completed: int = 0
    total: int = 0


def _last_insert_id(cursor: sqlite3.Cursor) -> int:
    value = cursor.lastrowid
    if value is None:
        raise RuntimeError("SQLite insert did not return a row id")
    return int(value)


def now_ns() -> int:
    return time.time_ns()


def open_database(
    location: Path,
    *,
    on_migration: Callable[[MigrationProgress], None] | None = None,
) -> sqlite3.Connection:
    location.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(location)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            connection.execute("PRAGMA journal_mode = WAL")
        migrate(connection, on_migration=on_migration)
    except BaseException:
        connection.close()
        raise
    return connection


def migrate(
    connection: sqlite3.Connection,
    *,
    on_migration: Callable[[MigrationProgress], None] | None = None,
) -> None:
    required_versions = set(range(1, SCHEMA_VERSION + 1))
    if required_versions <= _migration_versions(connection):
        return
    completed: list[MigrationProgress] = []
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        # Another opener may have migrated while we waited for the write lock.
        if required_versions <= _migration_versions(connection):
            return
        _apply_migrations(connection, on_migration=on_migration, completed=completed)
    if on_migration is not None:
        for event in completed:
            on_migration(event)


def _migration_versions(connection: sqlite3.Connection) -> set[int]:
    if connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='schema_migration'"
    ).fetchone() is None:
        return set()
    return {int(row[0]) for row in connection.execute("SELECT version FROM schema_migration")}


def _execute_ddl(connection: sqlite3.Connection, script: str) -> None:
    # executescript implicitly commits, which would release the migration lock.
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def _apply_migrations(
    connection: sqlite3.Connection,
    *,
    on_migration: Callable[[MigrationProgress], None] | None,
    completed: list[MigrationProgress],
) -> None:
    connection.execute("PRAGMA application_id = 1179402580")
    _execute_ddl(connection, DDL)
    initial = connection.execute("SELECT 1 FROM schema_migration WHERE version = 1").fetchone()
    existing_database = initial is not None
    if initial is None:
        connection.execute(
            "INSERT INTO schema_migration(version, applied_at_ns, description) VALUES (?, ?, ?)",
            (1, now_ns(), "initial persistent index schema"),
        )
    description = "cache directory usage and scan freshness"
    directory_summary = connection.execute("SELECT 1 FROM schema_migration WHERE version = 2").fetchone()
    if directory_summary is None:
        if existing_database and on_migration is not None:
            on_migration(MigrationProgress(2, description, "started"))
        _execute_ddl(connection, DIRECTORY_SUMMARY_MIGRATION)
        from flatlas.summaries import backfill_directory_summaries

        progress_callback = None
        if existing_database and on_migration is not None:
            def report_backfill(completed: int, total: int) -> None:
                on_migration(MigrationProgress(2, description, "backfill", completed, total))

            progress_callback = report_backfill
        backfill_directory_summaries(connection, on_progress=progress_callback)
        connection.execute(
            "INSERT INTO schema_migration(version, applied_at_ns, description) VALUES (?, ?, ?)",
            (2, now_ns(), description),
        )
        if existing_database and on_migration is not None:
            completed.append(MigrationProgress(2, description, "completed"))
    refresh_scan = connection.execute("SELECT 1 FROM schema_migration WHERE version = 3").fetchone()
    if refresh_scan is None:
        if existing_database and on_migration is not None:
            on_migration(MigrationProgress(3, REFRESH_SCAN_MIGRATION_DESCRIPTION, "started"))
        scan_columns = {row["name"] for row in connection.execute("PRAGMA table_info('scan')")}
        if "refresh_before_ns" not in scan_columns:
            connection.execute("ALTER TABLE scan ADD COLUMN refresh_before_ns INTEGER")
        connection.execute(
            "INSERT INTO schema_migration(version, applied_at_ns, description) VALUES (?, ?, ?)",
            (3, now_ns(), REFRESH_SCAN_MIGRATION_DESCRIPTION),
        )
    if existing_database and refresh_scan is None and on_migration is not None:
        completed.append(MigrationProgress(3, REFRESH_SCAN_MIGRATION_DESCRIPTION, "completed"))
