"""SQLite persistence, filesystem scanning, hashing, queries, and dry-run plans."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat as stat_module
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blake3 import blake3

from flatlas.errors import FlatlasError

SCHEMA_VERSION = 1
SCANNER_VERSION = "flatlas-python-0.1"
QUICK_SAMPLE_BYTES = 64 * 1024

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
    errors_seen INTEGER NOT NULL DEFAULT 0, note TEXT
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


@dataclass(frozen=True)
class Namespace:
    path: Path
    platform: str
    kind: str
    identity: bytes


@dataclass(frozen=True)
class Observation:
    path: Path
    key: bytes
    name: bytes
    kind: str
    device: int | None
    inode: int | None
    mode: int | None
    uid: int | None
    gid: int | None
    nlink: int | None
    logical_size: int | None
    allocated_size: int | None
    mtime_ns: int | None
    change_time_ns: int | None
    birth_time_ns: int | None
    symlink_target: bytes | None


@dataclass(frozen=True)
class ScanProgress:
    phase: str
    files_seen: int
    dirs_seen: int
    logical_bytes_seen: int


@dataclass(frozen=True)
class HashProgress:
    phase: str
    completed_files: int
    total_files: int
    completed_bytes: int
    total_bytes: int


@dataclass(frozen=True)
class HashSummary:
    candidate_files: int
    candidate_bytes: int
    content_files_read: int
    changed_files: int
    errors: int

    @property
    def complete(self) -> bool:
        return self.changed_files == 0 and self.errors == 0

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "complete": self.complete,
            "candidate_files": self.candidate_files,
            "candidate_bytes": self.candidate_bytes,
            "content_files_read": self.content_files_read,
            "changed_files": self.changed_files,
            "errors": self.errors,
        }


class _HashBasisChanged(Exception):
    pass


def _last_insert_id(cursor: sqlite3.Cursor) -> int:
    value = cursor.lastrowid
    if value is None:
        raise RuntimeError("SQLite insert did not return a row id")
    return int(value)

def now_ns() -> int:
    return time.time_ns()


def open_database(location: Path) -> sqlite3.Connection:
    location.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(location)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    migrate(connection)
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA application_id = 1179402580")
    connection.executescript(DDL)
    exists = connection.execute("SELECT 1 FROM schema_migration WHERE version = ?", (SCHEMA_VERSION,)).fetchone()
    if exists is None:
        connection.execute(
            "INSERT INTO schema_migration(version, applied_at_ns, description) VALUES (?, ?, ?)",
            (SCHEMA_VERSION, now_ns(), "initial persistent index schema"),
        )
    connection.commit()


def discover_namespace(value: Path) -> Namespace:
    path = value.expanduser().resolve()
    if not path.exists():
        raise FlatlasError(f"path does not exist: {path}")
    if os.name == "nt":
        anchor = Path(path.anchor)
        return Namespace(anchor, "windows", "windows_volume", os.fsencode(str(anchor)))
    current = path if path.is_dir() else path.parent
    while current.parent != current and not os.path.ismount(current):
        current = current.parent
    platform = "macos" if sys.platform == "darwin" else "linux"
    return Namespace(current, platform, "posix_mount", os.fsencode(str(current)))


def path_key(namespace: Namespace, value: Path) -> bytes:
    relative = os.path.relpath(str(value), str(namespace.path))
    if relative in (".", ""):
        return b""
    return b"".join(os.fsencode(part) + b"\0" for part in Path(relative).parts)


def parent_key(key: bytes) -> bytes | None:
    if not key:
        return None
    parts = key.rstrip(b"\0").split(b"\0")
    return b"" if len(parts) == 1 else b"".join(part + b"\0" for part in parts[:-1])


def get_root(connection: sqlite3.Connection, namespace: Namespace) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM root WHERE root_path_raw = ?", (namespace.identity,)).fetchone()


def register_namespace(connection: sqlite3.Connection, namespace: Namespace) -> int:
    existing = get_root(connection, namespace)
    if existing is not None:
        return int(existing["id"])
    timestamp = now_ns()
    cursor = connection.execute(
        """INSERT INTO root(root_path_raw, root_path_display, platform, namespace_kind,
           namespace_id_raw, case_sensitive, created_at_ns, updated_at_ns)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            namespace.identity, str(namespace.path), namespace.platform, namespace.kind,
            namespace.identity, 0 if os.name == "nt" else 1, timestamp, timestamp,
        ),
    )
    connection.commit()
    return _last_insert_id(cursor)


def list_roots(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute("SELECT id, root_path_display, platform, namespace_kind, enabled FROM root ORDER BY id")]


def filesystem_usage(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT id, root_path_display, platform, namespace_kind, filesystem_type, enabled
        FROM root ORDER BY root_path_display"""
    )
    result = []
    for row in rows:
        try:
            usage = shutil.disk_usage(row["root_path_display"])
        except OSError:
            total = used = available = use_percent = None
            status = "unavailable"
        else:
            total = _blocks_1k(usage.total)
            used = _blocks_1k(usage.used)
            available = _blocks_1k(usage.free)
            use_percent = 0 if usage.total == 0 else min(100, (usage.used * 100 + usage.total - 1) // usage.total)
            status = "ok" if row["enabled"] else "disabled"
        result.append(
            {
                "id": row["id"],
                "root_path_display": row["root_path_display"],
                "platform": row["platform"],
                "namespace_kind": row["namespace_kind"],
                "enabled": bool(row["enabled"]),
                "filesystem": row["root_path_display"],
                "filesystem_type": row["filesystem_type"],
                "blocks_1k": total,
                "used_1k": used,
                "available_1k": available,
                "use_percent": use_percent,
                "mounted_on": row["root_path_display"],
                "status": status,
            }
        )
    return result


def _blocks_1k(byte_count: int) -> int:
    return (byte_count + 1023) // 1024


def _sqlite_integer(value: int | None) -> int | None:
    """Map platform unsigned identifiers into SQLite's signed 64-bit range."""
    if value is None:
        return None
    if value > 2**63 - 1:
        return value - 2**64
    return value

def _observation(namespace: Namespace, value: Path, *, root: bool = False) -> Observation:
    stat = value.lstat()
    mode = stat.st_mode
    reparse = bool(getattr(stat, "st_file_attributes", 0) & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if root:
        kind = "root"
    elif os.path.islink(value):
        kind = "symlink"
    elif reparse:
        kind = "reparse"
    elif value.is_dir():
        kind = "directory"
    elif value.is_file():
        kind = "file"
    else:
        kind = "other"
    target: bytes | None = None
    if kind == "symlink":
        try:
            target = os.fsencode(os.readlink(value))
        except OSError:
            target = None
    return Observation(
        path=value, key=path_key(namespace, value),
        name=b"" if root else os.fsencode(value.name), kind=kind,
        device=_sqlite_integer(getattr(stat, "st_dev", None)), inode=_sqlite_integer(getattr(stat, "st_ino", None)),
        mode=mode, uid=getattr(stat, "st_uid", None), gid=getattr(stat, "st_gid", None),
        nlink=getattr(stat, "st_nlink", None),
        logical_size=stat.st_size if kind == "file" else None,
        allocated_size=(getattr(stat, "st_blocks", 0) * 512) if hasattr(stat, "st_blocks") else None,
        mtime_ns=getattr(stat, "st_mtime_ns", None),
        # Python's Windows st_ctime is not a stable POSIX change time (and is
        # deprecated as a creation-time alias), so it is not a reliable hash basis.
        change_time_ns=None if sys.platform == "win32" else getattr(stat, "st_ctime_ns", None),
        birth_time_ns=getattr(stat, "st_birthtime_ns", None), symlink_target=target,
    )


def _parent_id(connection: sqlite3.Connection, root_id: int, key: bytes) -> int | None:
    parent = parent_key(key)
    if parent is None:
        return None
    found = connection.execute("SELECT id FROM path WHERE root_id = ? AND path_key = ?", (root_id, parent)).fetchone()
    if found is None:
        raise FlatlasError("scanner invariant failed: parent was not recorded")
    return int(found["id"])


def upsert_path(connection: sqlite3.Connection, root_id: int, scan_id: int, item: Observation) -> int:
    parent_id = _parent_id(connection, root_id, item.key)
    old = connection.execute("SELECT id FROM path WHERE root_id = ? AND path_key = ?", (root_id, item.key)).fetchone()
    values = (
        parent_id, item.name, str(item.path), item.kind, item.device, item.inode, item.mode,
        item.uid, item.gid, item.nlink, item.logical_size, item.allocated_size, item.mtime_ns,
        item.change_time_ns, item.birth_time_ns, item.symlink_target, scan_id, root_id, item.key,
    )
    if old is None:
        cursor = connection.execute(
            """INSERT INTO path(parent_path_id, name_raw, path_display, entry_kind, state,
            device, inode, mode, uid, gid, nlink, logical_size, allocated_size, mtime_ns,
            change_time_ns, birth_time_ns, symlink_target_raw, first_seen_scan_id, last_seen_scan_id,
            root_id, path_key) VALUES (?, ?, ?, ?, 'present', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values[:-2] + (scan_id, root_id, item.key),
        )
        return _last_insert_id(cursor)
    path_id = int(old["id"])
    connection.execute(
        """UPDATE path SET parent_path_id=?, name_raw=?, path_display=?, entry_kind=?, state='present',
        device=?, inode=?, mode=?, uid=?, gid=?, nlink=?, logical_size=?, allocated_size=?, mtime_ns=?,
        change_time_ns=?, birth_time_ns=?, symlink_target_raw=?, last_seen_scan_id=?,
        deleted_by_scope_id=NULL, deleted_at_ns=NULL WHERE root_id=? AND path_key=?""",
        values,
    )
    connection.execute(
        """UPDATE file_hash SET state='stale', error_message=NULL
           WHERE path_id=? AND state IN ('quick_ready', 'full_ready') AND
           (basis_device IS NOT ? OR basis_inode IS NOT ? OR basis_size IS NOT ? OR
            basis_mtime_ns IS NOT ? OR basis_change_time_ns IS NOT ? OR basis_birth_time_ns IS NOT ?)""",
        (path_id, item.device, item.inode, item.logical_size, item.mtime_ns, item.change_time_ns, item.birth_time_ns),
    )
    return path_id


def _record_error(connection: sqlite3.Connection, scope_id: int, key: bytes, operation: str, exc: OSError | Exception) -> None:
    connection.execute(
        "INSERT INTO scan_error(scan_scope_id, path_key, operation, error_code, message, occurred_at_ns) VALUES (?, ?, ?, ?, ?, ?)",
        (scope_id, key, operation, getattr(exc, "errno", None), str(exc), now_ns()),
    )
    connection.execute("UPDATE scan_scope SET error_count = error_count + 1 WHERE id = ?", (scope_id,))


def _ensure_ancestors(connection: sqlite3.Connection, namespace: Namespace, root_id: int, scan_id: int, scope: Path) -> int:
    values = [namespace.path]
    relative = scope.relative_to(namespace.path)
    current = namespace.path
    for part in relative.parts:
        current = current / part
        values.append(current)
    scope_id = -1
    for index, value in enumerate(values):
        observed = _observation(namespace, value, root=index == 0)
        scope_id = upsert_path(connection, root_id, scan_id, observed)
    return scope_id


def _stat_hash_basis(file_stat: os.stat_result) -> tuple[int | None, ...]:
    return (
        _sqlite_integer(getattr(file_stat, "st_dev", None)),
        _sqlite_integer(getattr(file_stat, "st_ino", None)),
        file_stat.st_size,
        getattr(file_stat, "st_mtime_ns", None),
        None if sys.platform == "win32" else getattr(file_stat, "st_ctime_ns", None),
        getattr(file_stat, "st_birthtime_ns", None),
    )


def _candidate_hash_basis(candidate: dict[str, Any]) -> tuple[int | None, ...]:
    return (
        candidate["device"],
        candidate["inode"],
        candidate["logical_size"],
        candidate["mtime_ns"],
        None if sys.platform == "win32" else candidate["change_time_ns"],
        candidate["birth_time_ns"],
    )


def _validated_digest(candidate: dict[str, Any], *, full: bool) -> bytes:
    expected = _candidate_hash_basis(candidate)
    size = int(candidate["logical_size"])
    digest = blake3()
    with Path(candidate["path_display"]).open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat_module.S_ISREG(before.st_mode) or _stat_hash_basis(before) != expected:
            raise _HashBasisChanged
        if full or size <= QUICK_SAMPLE_BYTES * 2:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        else:
            digest.update(stream.read(QUICK_SAMPLE_BYTES))
            stream.seek(-QUICK_SAMPLE_BYTES, os.SEEK_END)
            digest.update(stream.read(QUICK_SAMPLE_BYTES))
        after = os.fstat(stream.fileno())
        if _stat_hash_basis(after) != expected:
            raise _HashBasisChanged
    return digest.digest()


def _save_hash(
    connection: sqlite3.Connection,
    candidate: dict[str, Any],
    *,
    quick: bytes,
    full: bytes | None = None,
) -> None:
    state = "full_ready" if full is not None else "quick_ready"
    connection.execute(
        """INSERT INTO file_hash(path_id, state, quick_algorithm, quick_digest, quick_sample_bytes,
        full_algorithm, full_digest, full_verification, basis_device, basis_inode, basis_size,
        basis_mtime_ns, basis_change_time_ns, basis_birth_time_ns, last_hashed_scan_id, error_message)
        VALUES (?, ?, 'blake3', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(path_id) DO UPDATE SET state=excluded.state, quick_algorithm=excluded.quick_algorithm,
        quick_digest=excluded.quick_digest, quick_sample_bytes=excluded.quick_sample_bytes,
        full_algorithm=excluded.full_algorithm, full_digest=excluded.full_digest,
        full_verification=excluded.full_verification,
        basis_device=excluded.basis_device, basis_inode=excluded.basis_inode, basis_size=excluded.basis_size,
        basis_mtime_ns=excluded.basis_mtime_ns, basis_change_time_ns=excluded.basis_change_time_ns,
        basis_birth_time_ns=excluded.basis_birth_time_ns, last_hashed_scan_id=excluded.last_hashed_scan_id,
        error_message=NULL""",
        (
            candidate["id"],
            state,
            quick,
            QUICK_SAMPLE_BYTES,
            "blake3" if full else None,
            full,
            "hash_only" if full else None,
            *_candidate_hash_basis(candidate),
            candidate["last_seen_scan_id"],
        ),
    )


def _save_hash_problem(connection: sqlite3.Connection, candidate: dict[str, Any], state: str, message: str) -> None:
    connection.execute(
        """INSERT INTO file_hash(path_id, state, basis_device, basis_inode, basis_size,
        basis_mtime_ns, basis_change_time_ns, basis_birth_time_ns, last_hashed_scan_id, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path_id) DO UPDATE SET state=excluded.state,
        quick_algorithm=NULL, quick_digest=NULL, quick_sample_bytes=NULL,
        full_algorithm=NULL, full_digest=NULL, full_verification=NULL,
        basis_device=excluded.basis_device, basis_inode=excluded.basis_inode,
        basis_size=excluded.basis_size, basis_mtime_ns=excluded.basis_mtime_ns,
        basis_change_time_ns=excluded.basis_change_time_ns,
        basis_birth_time_ns=excluded.basis_birth_time_ns,
        last_hashed_scan_id=excluded.last_hashed_scan_id,
        error_message=excluded.error_message""",
        (
            candidate["id"],
            state,
            *_candidate_hash_basis(candidate),
            candidate["last_seen_scan_id"],
            message,
        ),
    )


def _duplicate_hash_candidates(connection: sqlite3.Connection, scope: Path) -> list[dict[str, Any]]:
    selected = _scope_row(connection, scope)
    if selected["entry_kind"] not in {"root", "directory"}:
        raise FlatlasError(f"duplicate scope is not a directory: {selected['path_display']}")
    rows = connection.execute(
        """WITH RECURSIVE descendants(id) AS (
            SELECT id FROM path WHERE id=? UNION ALL
            SELECT p.id FROM path p JOIN descendants d ON p.parent_path_id=d.id
        ), duplicate_sizes(logical_size) AS (
            SELECT p.logical_size FROM path p JOIN descendants d ON d.id=p.id
            WHERE p.state='present' AND p.entry_kind='file' AND p.logical_size > 0
            GROUP BY p.logical_size HAVING count(*) > 1
        )
        SELECT p.id, p.path_display, p.logical_size, p.device, p.inode, p.mtime_ns,
               p.change_time_ns, p.birth_time_ns, p.last_seen_scan_id,
               h.state AS hash_state, h.quick_algorithm, h.quick_digest, h.quick_sample_bytes,
               h.full_algorithm, h.full_digest,
               h.basis_device AS hash_device, h.basis_inode AS hash_inode,
               h.basis_size AS hash_size, h.basis_mtime_ns AS hash_mtime_ns,
               h.basis_change_time_ns AS hash_change_time_ns,
               h.basis_birth_time_ns AS hash_birth_time_ns
        FROM path p JOIN descendants d ON d.id=p.id
        JOIN duplicate_sizes s ON s.logical_size=p.logical_size
        LEFT JOIN file_hash h ON h.path_id=p.id
        WHERE p.state='present' AND p.entry_kind='file' AND p.logical_size > 0
        ORDER BY p.logical_size, p.path_display""",
        (selected["id"],),
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_duplicate_hashes(
    connection: sqlite3.Connection,
    scope: Path,
    *,
    on_progress: Callable[[HashProgress], None] | None = None,
    commit_batch: int = 100,
) -> HashSummary:
    if commit_batch < 1:
        raise ValueError("commit_batch must be at least 1")
    candidates = _duplicate_hash_candidates(connection, scope)
    candidate_bytes = sum(int(candidate["logical_size"]) for candidate in candidates)
    if on_progress is not None:
        on_progress(HashProgress("candidates", 0, len(candidates), 0, candidate_bytes))

    changed_files = 0
    errors = 0
    writes = 0
    content_path_ids: set[int] = set()

    def save_problem(candidate: dict[str, Any], state: str, message: str) -> None:
        nonlocal writes
        _save_hash_problem(connection, candidate, state, message)
        candidate["hash_state"] = state
        candidate["quick_digest"] = None
        candidate["full_digest"] = None
        writes += 1
        if writes % commit_batch == 0:
            connection.commit()

    def has_current_basis(candidate: dict[str, Any]) -> bool:
        return (
            candidate["hash_device"],
            candidate["hash_inode"],
            candidate["hash_size"],
            candidate["hash_mtime_ns"],
            candidate["hash_change_time_ns"],
            candidate["hash_birth_time_ns"],
        ) == _candidate_hash_basis(candidate)

    def has_quick(candidate: dict[str, Any]) -> bool:
        return bool(
            candidate["hash_state"] in {"quick_ready", "full_ready"}
            and candidate["quick_algorithm"] == "blake3"
            and candidate["quick_digest"] is not None
            and candidate["quick_sample_bytes"] == QUICK_SAMPLE_BYTES
            and has_current_basis(candidate)
        )

    def has_full(candidate: dict[str, Any]) -> bool:
        return bool(
            has_quick(candidate)
            and candidate["hash_state"] == "full_ready"
            and candidate["full_algorithm"] == "blake3"
            and candidate["full_digest"] is not None
        )

    def remember_current_basis(candidate: dict[str, Any]) -> None:
        (
            candidate["hash_device"],
            candidate["hash_inode"],
            candidate["hash_size"],
            candidate["hash_mtime_ns"],
            candidate["hash_change_time_ns"],
            candidate["hash_birth_time_ns"],
        ) = _candidate_hash_basis(candidate)

    quick_work = [
        candidate
        for candidate in candidates
        if not has_quick(candidate)
    ]
    quick_total_bytes = sum(int(candidate["logical_size"]) for candidate in quick_work)
    if on_progress is not None and quick_work:
        on_progress(HashProgress("quick", 0, len(quick_work), 0, quick_total_bytes))
    quick_done_bytes = 0
    try:
        for index, candidate in enumerate(quick_work, start=1):
            size = int(candidate["logical_size"])
            try:
                quick = _validated_digest(candidate, full=False)
                is_full = size <= QUICK_SAMPLE_BYTES * 2
                _save_hash(connection, candidate, quick=quick, full=quick if is_full else None)
                candidate["hash_state"] = "full_ready" if is_full else "quick_ready"
                candidate["quick_algorithm"] = "blake3"
                candidate["quick_digest"] = quick
                candidate["quick_sample_bytes"] = QUICK_SAMPLE_BYTES
                candidate["full_algorithm"] = "blake3" if is_full else None
                candidate["full_digest"] = quick if is_full else None
                remember_current_basis(candidate)
                content_path_ids.add(int(candidate["id"]))
                writes += 1
                if writes % commit_batch == 0:
                    connection.commit()
            except _HashBasisChanged:
                changed_files += 1
                save_problem(candidate, "stale", "metadata changed since scan")
            except OSError as exc:
                errors += 1
                save_problem(candidate, "failed", str(exc))
            quick_done_bytes += size
            if on_progress is not None:
                on_progress(HashProgress("quick", index, len(quick_work), quick_done_bytes, quick_total_bytes))

        # Older quick hashes of small files already cover the entire file and can be
        # promoted without reading the content a second time.
        for candidate in candidates:
            if (
                int(candidate["logical_size"]) <= QUICK_SAMPLE_BYTES * 2
                and has_quick(candidate)
                and not has_full(candidate)
            ):
                _save_hash(
                    connection,
                    candidate,
                    quick=candidate["quick_digest"],
                    full=candidate["quick_digest"],
                )
                candidate["hash_state"] = "full_ready"
                candidate["full_algorithm"] = "blake3"
                candidate["full_digest"] = candidate["quick_digest"]
                writes += 1
                if writes % commit_batch == 0:
                    connection.commit()

        quick_groups: dict[tuple[int, bytes], list[dict[str, Any]]] = defaultdict(list)
        for candidate in candidates:
            if has_quick(candidate):
                quick_groups[(int(candidate["logical_size"]), candidate["quick_digest"])].append(candidate)

        full_work = [
            candidate
            for group in quick_groups.values()
            if len(group) > 1
            for candidate in group
            if not has_full(candidate)
        ]
        full_total_bytes = sum(int(candidate["logical_size"]) for candidate in full_work)
        if on_progress is not None and full_work:
            on_progress(HashProgress("full", 0, len(full_work), 0, full_total_bytes))
        full_done_bytes = 0
        for index, candidate in enumerate(full_work, start=1):
            size = int(candidate["logical_size"])
            try:
                full = _validated_digest(candidate, full=True)
                _save_hash(connection, candidate, quick=candidate["quick_digest"], full=full)
                candidate["hash_state"] = "full_ready"
                candidate["full_algorithm"] = "blake3"
                candidate["full_digest"] = full
                remember_current_basis(candidate)
                content_path_ids.add(int(candidate["id"]))
                writes += 1
                if writes % commit_batch == 0:
                    connection.commit()
            except _HashBasisChanged:
                changed_files += 1
                save_problem(candidate, "stale", "metadata changed during hash")
            except OSError as exc:
                errors += 1
                save_problem(candidate, "failed", str(exc))
            full_done_bytes += size
            if on_progress is not None:
                on_progress(HashProgress("full", index, len(full_work), full_done_bytes, full_total_bytes))
        connection.commit()
    except KeyboardInterrupt:
        connection.commit()
        raise

    summary = HashSummary(
        candidate_files=len(candidates),
        candidate_bytes=candidate_bytes,
        content_files_read=len(content_path_ids),
        changed_files=changed_files,
        errors=errors,
    )
    if on_progress is not None:
        phase = "incomplete" if not summary.complete else "cached" if not content_path_ids else "completed"
        on_progress(HashProgress(phase, 0, len(candidates), 0, candidate_bytes))
    return summary

def _mark_missing(connection: sqlite3.Connection, root_id: int, scan_id: int, scope_id: int, scope_path_id: int) -> None:
    connection.execute(
        """WITH RECURSIVE descendants(id) AS (
             SELECT id FROM path WHERE id=?
             UNION ALL
             SELECT p.id FROM path p JOIN descendants d ON p.parent_path_id=d.id
           )
           UPDATE path SET state='deleted', deleted_by_scope_id=?, deleted_at_ns=?
           WHERE root_id=? AND state='present' AND last_seen_scan_id <> ? AND id IN descendants""",
        (scope_path_id, scope_id, now_ns(), root_id, scan_id),
    )


def scan_directory(
    connection: sqlite3.Connection,
    value: Path,
    *,
    on_progress: Callable[[ScanProgress], None] | None = None,
) -> dict[str, Any]:
    namespace = discover_namespace(value)
    root = get_root(connection, namespace)
    if root is None:
        raise FlatlasError(f"filesystem is not registered; run 'flatlas init {value}' first")
    scope = value.expanduser().resolve()
    if not scope.is_dir():
        raise FlatlasError(f"scan path must be a directory: {scope}")
    root_id = int(root["id"])
    kind = "full" if scope == namespace.path else "subtree"
    started = now_ns()
    with connection:
        scan_id = _last_insert_id(connection.execute(
            "INSERT INTO scan(root_id, kind, status, started_at_ns, scanner_version) VALUES (?, ?, 'running', ?, ?)",
            (root_id, kind, started, SCANNER_VERSION),
        ))
        scope_key = path_key(namespace, scope)
        scope_id = _last_insert_id(connection.execute(
            "INSERT INTO scan_scope(scan_id, root_id, scope_path_key, status, started_at_ns) VALUES (?, ?, ?, 'running', ?)",
            (scan_id, root_id, scope_key, started),
        ))
        try:
            scope_path_id = _ensure_ancestors(connection, namespace, root_id, scan_id, scope)
        except OSError as exc:
            _record_error(connection, scope_id, scope_key, "stat", exc)
            connection.execute("UPDATE scan_scope SET status='failed', finished_at_ns=? WHERE id=?", (now_ns(), scope_id))
            connection.execute("UPDATE scan SET status='failed', finished_at_ns=?, errors_seen=1 WHERE id=?", (now_ns(), scan_id))
            return {
                "scan_id": scan_id,
                "status": "failed",
                "files_seen": 0,
                "dirs_seen": 0,
                "logical_bytes_seen": 0,
                "errors_seen": 1,
            }
        files_seen = 0
        dirs_seen = 1
        logical_bytes_seen = 0
        failed = False

        def report(phase: str) -> None:
            if on_progress is not None:
                on_progress(ScanProgress(phase, files_seen, dirs_seen, logical_bytes_seen))

        def visit(directory: Path) -> None:
            nonlocal files_seen, dirs_seen, logical_bytes_seen, failed
            try:
                entries = list(os.scandir(directory))
            except OSError as exc:
                _record_error(connection, scope_id, path_key(namespace, directory), "enumerate", exc)
                failed = True
                return
            for entry in entries:
                entry_path = Path(entry.path)
                try:
                    observed = _observation(namespace, entry_path)
                except OSError as exc:
                    _record_error(connection, scope_id, path_key(namespace, entry_path), "stat", exc)
                    failed = True
                    continue
                upsert_path(connection, root_id, scan_id, observed)
                if observed.kind == "directory":
                    dirs_seen += 1
                    report("scanning")
                    visit(entry_path)
                elif observed.kind == "file":
                    files_seen += 1
                    logical_bytes_seen += observed.logical_size or 0
                    report("scanning")

        try:
            report("scanning")
            visit(scope)
            scope_status = "partial" if failed else "completed"
            connection.execute(
                "UPDATE scan_scope SET status=?, finished_at_ns=?, paths_seen=? WHERE id=?",
                (scope_status, now_ns(), files_seen + dirs_seen, scope_id),
            )
            if scope_status == "completed":
                _mark_missing(connection, root_id, scan_id, scope_id, scope_path_id)
            errors = int(connection.execute("SELECT error_count FROM scan_scope WHERE id=?", (scope_id,)).fetchone()["error_count"])
            connection.execute(
                "UPDATE scan SET status=?, finished_at_ns=?, files_seen=?, dirs_seen=?, errors_seen=? WHERE id=?",
                (scope_status, now_ns(), files_seen, dirs_seen, errors, scan_id),
            )
        except KeyboardInterrupt:
            connection.execute("UPDATE scan_scope SET status='cancelled', finished_at_ns=? WHERE id=?", (now_ns(), scope_id))
            connection.execute("UPDATE scan SET status='cancelled', finished_at_ns=? WHERE id=?", (now_ns(), scan_id))
            raise
    report("partial" if failed else "completed")
    return {
        "scan_id": scan_id,
        "status": "partial" if failed else "completed",
        "files_seen": files_seen,
        "dirs_seen": dirs_seen,
        "logical_bytes_seen": logical_bytes_seen,
        "errors_seen": int(
            connection.execute("SELECT error_count FROM scan_scope WHERE id=?", (scope_id,)).fetchone()["error_count"]
        ),
    }


def _scope_row(connection: sqlite3.Connection, value: Path) -> sqlite3.Row:
    display = str(value.expanduser().resolve())
    row = connection.execute(
        """SELECT p.id, p.path_display, p.entry_kind, r.root_path_display
        FROM path p JOIN root r ON r.id=p.root_id
        WHERE p.path_display=? AND p.state='present'""",
        (display,),
    ).fetchone()
    if row is None:
        raise FlatlasError(f"path is not currently indexed: {display}")
    return row


def _lexical_absolute_path(value: Path) -> Path:
    """Make an absolute path without resolving or inspecting the filesystem."""
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def remove_indexed_subtree(connection: sqlite3.Connection, value: Path) -> dict[str, Any]:
    """Remove one indexed path and all indexed descendants without touching the filesystem."""
    display = str(_lexical_absolute_path(value))
    selected = connection.execute(
        "SELECT id FROM path WHERE path_display=?",
        (display,),
    ).fetchone()
    if selected is None:
        raise FlatlasError(f"path is not indexed: {display}")

    path_id = int(selected["id"])
    plan_ids = [
        str(row["plan_id"])
        for row in connection.execute(
            """WITH RECURSIVE descendants(id) AS (
                SELECT id FROM path WHERE id=?
                UNION ALL
                SELECT p.id FROM path p JOIN descendants d ON p.parent_path_id=d.id
            )
            SELECT DISTINCT po.plan_id FROM plan_operation po
            WHERE po.canonical_path_id IN descendants OR po.replacement_path_id IN descendants""",
            (path_id,),
        )
    ]
    with connection:
        if plan_ids:
            placeholders = ", ".join("?" for _ in plan_ids)
            connection.execute(f"DELETE FROM plan WHERE id IN ({placeholders})", plan_ids)
        connection.execute(
            """WITH RECURSIVE descendants(id) AS (
                SELECT id FROM path WHERE id=?
                UNION ALL
                SELECT p.id FROM path p JOIN descendants d ON p.parent_path_id=d.id
            )
            DELETE FROM path WHERE id IN descendants""",
            (path_id,),
        )
        removed_paths = int(connection.execute("SELECT changes()").fetchone()[0])
    return {
        "path": display,
        "paths_removed": removed_paths,
        "plans_removed": len(plan_ids),
    }

def query_paths(connection: sqlite3.Connection, *, scope: Path | None = None, limit: int = 500) -> list[dict[str, Any]]:
    if scope is None:
        rows = connection.execute(
            "SELECT root_id, path_display, entry_kind, logical_size, allocated_size, mtime_ns FROM path WHERE state='present' ORDER BY path_display LIMIT ?", (limit,)
        )
    else:
        selected = _scope_row(connection, scope)
        rows = connection.execute(
            """WITH RECURSIVE descendants(id) AS (
                SELECT id FROM path WHERE id=? UNION ALL
                SELECT p.id FROM path p JOIN descendants d ON p.parent_path_id=d.id
            ) SELECT root_id, path_display, entry_kind, logical_size, allocated_size, mtime_ns
            FROM path WHERE state='present' AND id IN descendants ORDER BY path_display LIMIT ?""",
            (selected["id"], limit),
        )
    return [dict(row) for row in rows]


def _relative_display(path_display: str, relative_to: Path, namespace_path: str) -> str:
    try:
        return os.path.relpath(path_display, relative_to)
    except ValueError:
        # Windows cannot express a cwd-relative path across drive letters, but
        # every indexed path can still be shown relative to its scan namespace.
        return os.path.relpath(path_display, namespace_path)


def list_child_directories(
    connection: sqlite3.Connection,
    *,
    scope: Path = Path("."),
) -> list[dict[str, Any]]:
    selected_path = scope.expanduser().resolve()
    namespace = discover_namespace(selected_path)
    root = get_root(connection, namespace)
    if root is None:
        raise FlatlasError(f"filesystem is not registered; run 'flatlas init {scope}' first")
    try:
        with os.scandir(selected_path) as entries:
            actual_paths = [Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False)]
    except OSError as exc:
        raise FlatlasError(f"cannot list directory {selected_path}: {exc}") from exc

    parent = connection.execute(
        "SELECT id FROM path WHERE root_id=? AND path_key=?",
        (root["id"], path_key(namespace, selected_path)),
    ).fetchone()
    indexed_rows = [] if parent is None else connection.execute(
        """SELECT id, path_key, path_display, state FROM path
        WHERE parent_path_id=? AND entry_kind='directory'""",
        (parent["id"],),
    ).fetchall()
    indexed_by_path = {_normalized_display(row["path_display"]): row for row in indexed_rows}

    usage_rows = [] if parent is None else connection.execute(
        """WITH RECURSIVE descendants(top_id, id) AS (
            SELECT p.id, p.id FROM path p
            WHERE p.parent_path_id=? AND p.state='present'
              AND p.entry_kind='directory'
            UNION ALL
            SELECT d.top_id, p.id FROM path p
            JOIN descendants d ON p.parent_path_id=d.id
            WHERE p.state='present'
        )
        SELECT top.id,
               sum(CASE WHEN item.entry_kind='file' THEN 1 ELSE 0 END) AS files,
               COALESCE(sum(CASE WHEN item.entry_kind='file' THEN item.logical_size END), 0) AS logical_size,
               CASE
                   WHEN sum(CASE WHEN item.entry_kind='file' THEN 1 ELSE 0 END)
                      = sum(CASE WHEN item.entry_kind='file' AND item.allocated_size IS NOT NULL THEN 1 ELSE 0 END)
                   THEN COALESCE(sum(CASE WHEN item.entry_kind='file' THEN item.allocated_size END), 0)
                   ELSE NULL
               END AS allocated_size
        FROM descendants d
        JOIN path top ON top.id=d.top_id
        JOIN path item ON item.id=d.id
        GROUP BY top.id
        """,
        (parent["id"],),
    ).fetchall()
    usage_by_id = {row["id"]: row for row in usage_rows}
    scopes = connection.execute(
        """SELECT ss.scope_path_key, ss.status FROM scan_scope ss
        JOIN scan s ON s.id=ss.scan_id
        WHERE ss.root_id=?
        ORDER BY s.started_at_ns DESC, ss.id DESC""",
        (root["id"],),
    ).fetchall()

    result: list[dict[str, Any]] = []
    actual_keys: set[str] = set()
    for actual_path in actual_paths:
        normalized = _normalized_display(str(actual_path))
        actual_keys.add(normalized)
        indexed = indexed_by_path.get(normalized)
        status = _directory_scan_status(indexed, path_key(namespace, actual_path), scopes)
        usage = None if indexed is None else usage_by_id.get(indexed["id"])
        result.append(_directory_status_row(actual_path, status, usage))

    for indexed in indexed_rows:
        if _normalized_display(indexed["path_display"]) not in actual_keys:
            result.append(
                _directory_status_row(
                    Path(indexed["path_display"]),
                    "missing",
                    None,
                )
            )
    return sorted(result, key=lambda row: os.path.normcase(str(row["name"])))


def _normalized_display(path_display: str) -> str:
    return os.path.normcase(os.path.normpath(path_display))


def _directory_scan_status(
    indexed: sqlite3.Row | None,
    directory_key: bytes,
    scopes: list[sqlite3.Row],
) -> str:
    if indexed is None or indexed["state"] != "present":
        return "unscanned"
    latest = next((scope for scope in scopes if directory_key.startswith(scope["scope_path_key"])), None)
    if latest is None:
        return "unscanned"
    return "scanned" if latest["status"] == "completed" else "incomplete"


def _directory_status_row(
    path: Path,
    status: str,
    usage: sqlite3.Row | None,
) -> dict[str, Any]:
    return {
        "status": status,
        "files": None if usage is None else usage["files"],
        "logical_size": None if usage is None else usage["logical_size"],
        "allocated_size": None if usage is None else usage["allocated_size"],
        "name": path.name,
    }


def disk_usage(
    connection: sqlite3.Connection,
    *,
    scope: Path | None = None,
    relative_to: Path | None = None,
) -> list[dict[str, Any]]:
    display_base = Path.cwd() if relative_to is None else relative_to
    if scope is None:
        rows = connection.execute(
            """SELECT r.id AS root_id, r.root_path_display, count(p.id) AS files,
               COALESCE(sum(p.logical_size), 0) AS logical_size,
               CASE WHEN count(p.id) = count(p.allocated_size)
                    THEN COALESCE(sum(p.allocated_size), 0)
                    ELSE NULL END AS allocated_size
               FROM root r LEFT JOIN path p ON p.root_id=r.id AND p.state='present' AND p.entry_kind='file'
               GROUP BY r.id ORDER BY r.root_path_display"""
        )
        return [
            {
                "root_id": row["root_id"],
                "files": row["files"],
                "logical_size": row["logical_size"],
                "allocated_size": row["allocated_size"],
                "path": _relative_display(row["root_path_display"], display_base, row["root_path_display"]),
            }
            for row in rows
        ]
    selected = _scope_row(connection, scope)
    row = connection.execute(
        """WITH RECURSIVE descendants(id) AS (
            SELECT id FROM path WHERE id=? UNION ALL
            SELECT p.id FROM path p JOIN descendants d ON p.parent_path_id=d.id
        ) SELECT count(p.id) AS files, COALESCE(sum(p.logical_size), 0) AS logical_size,
        CASE WHEN count(p.id) = count(p.allocated_size)
             THEN COALESCE(sum(p.allocated_size), 0)
             ELSE NULL END AS allocated_size FROM path p
        WHERE p.state='present' AND p.entry_kind='file' AND p.id IN descendants""",
        (selected["id"],),
    ).fetchone()
    return [
        {
            **dict(row),
            "path": _relative_display(selected["path_display"], display_base, selected["root_path_display"]),
        }
    ]

def largest_files(
    connection: sqlite3.Connection,
    *,
    scope: Path = Path("."),
    limit: int = 10,
) -> list[dict[str, Any]]:
    selected = _scope_row(connection, scope)
    rows = connection.execute(
        """WITH RECURSIVE descendants(id) AS (
            SELECT id FROM path WHERE id=? UNION ALL
            SELECT p.id FROM path p JOIN descendants d ON p.parent_path_id=d.id
        )
        SELECT p.root_id, p.path_display, p.logical_size, p.allocated_size
        FROM path p
        WHERE p.state='present' AND p.entry_kind='file' AND p.id IN descendants
        ORDER BY p.logical_size DESC, p.path_display LIMIT ?""",
        (selected["id"], limit),
    )
    display_base = Path(selected["path_display"])
    return [
        {
            "root_id": row["root_id"],
            "logical_size": row["logical_size"],
            "allocated_size": row["allocated_size"],
            "path": _relative_display(row["path_display"], display_base, selected["root_path_display"]),
        }
        for row in rows
    ]



def duplicate_groups(
    connection: sqlite3.Connection,
    *,
    root_id: int | None = None,
    scope: Path | None = None,
) -> list[dict[str, Any]]:
    if root_id is not None and scope is not None:
        raise ValueError("root_id and scope cannot be combined")
    if scope is not None:
        selected = _scope_row(connection, scope)
        if selected["entry_kind"] not in {"root", "directory"}:
            raise FlatlasError(f"duplicate scope is not a directory: {selected['path_display']}")
        rows = connection.execute(
            """WITH RECURSIVE descendants(id) AS (
                SELECT id FROM path WHERE id=? UNION ALL
                SELECT p.id FROM path p JOIN descendants d ON p.parent_path_id=d.id
            ) SELECT h.full_algorithm, h.full_digest, p.logical_size, p.id, p.root_id, p.path_display
            FROM file_hash h JOIN path p ON p.id=h.path_id
            WHERE h.state='full_ready' AND p.state='present' AND p.entry_kind='file'
              AND p.logical_size > 0
              AND p.id IN descendants
            ORDER BY h.full_algorithm, h.full_digest, p.path_display""",
            (selected["id"],),
        ).fetchall()
    else:
        where = "AND p.root_id=?" if root_id is not None else ""
        rows = connection.execute(
            f"""SELECT h.full_algorithm, h.full_digest, p.logical_size, p.id, p.root_id, p.path_display
            FROM file_hash h JOIN path p ON p.id=h.path_id
            WHERE h.state='full_ready' AND p.state='present' AND p.entry_kind='file'
              AND p.logical_size > 0 {where}
            ORDER BY h.full_algorithm, h.full_digest, p.path_display""",
            (() if root_id is None else (root_id,)),
        ).fetchall()
    grouped: dict[tuple[str, bytes, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["full_algorithm"], row["full_digest"], row["logical_size"])].append({"id": row["id"], "root_id": row["root_id"], "path_display": row["path_display"]})
    result = []
    for (algorithm, digest, size), paths in grouped.items():
        if len(paths) > 1:
            result.append({
                "algorithm": algorithm,
                "digest": digest.hex(),
                "logical_size": size,
                "count": len(paths),
                "theoretical_savings": size * (len(paths) - 1),
                "paths": paths,
            })
    return sorted(
        result,
        key=lambda group: (
            -group["theoretical_savings"],
            -group["logical_size"],
            group["algorithm"],
            group["digest"],
        ),
    )


def create_dry_run_plan(connection: sqlite3.Connection, root_id: int) -> str:
    groups = duplicate_groups(connection, root_id=root_id)
    plan_id = str(uuid.uuid4())
    source = connection.execute("SELECT id FROM scan WHERE root_id=? ORDER BY id DESC LIMIT 1", (root_id,)).fetchone()
    policy = {"version": 1, "canonical_selection": "lexical_path", "execution": "dry_run_only"}
    timestamp = now_ns()
    with connection:
        connection.execute(
            "INSERT INTO plan(id, root_id, source_scan_id, status, policy_json, created_at_ns, immutable_at_ns) VALUES (?, ?, ?, 'dry_run', ?, ?, ?)",
            (plan_id, root_id, source["id"] if source else None, json.dumps(policy, sort_keys=True), timestamp, timestamp),
        )
        ordinal = 0
        for group in groups:
            ordered = sorted(group["paths"], key=lambda item: item["path_display"])
            canonical = ordered[0]
            for replacement in ordered[1:]:
                ordinal += 1
                preconditions = {
                    "same_registered_filesystem": True,
                    "expected_canonical_path": canonical["path_display"],
                    "expected_replacement_path": replacement["path_display"],
                    "execution_not_implemented": True,
                }
                connection.execute(
                    """INSERT INTO plan_operation(plan_id, ordinal, action, canonical_path_id, replacement_path_id,
                    expected_size, expected_full_digest, expected_algorithm, preconditions_json, status)
                    VALUES (?, ?, 'hardlink', ?, ?, ?, ?, ?, ?, 'planned')""",
                    (plan_id, ordinal, canonical["id"], replacement["id"], group["logical_size"], bytes.fromhex(group["digest"]), group["algorithm"], json.dumps(preconditions, sort_keys=True)),
                )
    return plan_id


def plan_operations(connection: sqlite3.Connection, plan_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT po.ordinal, po.action, c.path_display AS canonical_path, r.path_display AS replacement_path,
        po.expected_size, hex(po.expected_full_digest) AS expected_digest, po.expected_algorithm,
        po.preconditions_json, po.status FROM plan_operation po
        JOIN path c ON c.id=po.canonical_path_id JOIN path r ON r.id=po.replacement_path_id
        WHERE po.plan_id=? ORDER BY po.ordinal""", (plan_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def export_rows(rows: Iterable[dict[str, Any]], fmt: str, output: Path | None = None) -> str:
    materialized = list(rows)
    if fmt == "json":
        rendered = json.dumps(materialized, ensure_ascii=False, indent=2, default=str) + "\n"
    elif fmt == "csv":
        import csv
        import io
        buffer = io.StringIO(newline="")
        keys = list(materialized[0].keys()) if materialized else []
        writer = csv.DictWriter(buffer, fieldnames=keys)
        if keys:
            writer.writeheader()
            writer.writerows(materialized)
        rendered = buffer.getvalue()
    else:
        raise FlatlasError("format must be json or csv")
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8", newline="")
    return rendered
















