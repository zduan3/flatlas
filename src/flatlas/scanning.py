"""Filesystem metadata scanning and coverage-safe index updates."""

from __future__ import annotations

import os
import sqlite3
import stat as stat_module
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flatlas.db import _last_insert_id, now_ns
from flatlas.errors import FlatlasError
from flatlas.paths import (
    Namespace,
    _sqlite_integer,
    discover_namespace,
    get_root,
    parent_key,
    path_key,
)
from flatlas.summaries import refresh_directory_summaries

SCANNER_VERSION = "flatlas-python-0.2"


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
    current_directory: str | None = None


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
    old = connection.execute(
        """SELECT id, state, entry_kind, device, inode
           FROM path WHERE root_id=? AND path_key=?""",
        (root_id, item.key),
    ).fetchone()
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
    if item.kind not in {"root", "directory"}:
        connection.execute("DELETE FROM directory_summary WHERE path_id=?", (path_id,))
    elif (
        old["state"] == "deleted"
        or old["entry_kind"] not in {"root", "directory"}
        or old["device"] != item.device
        or old["inode"] != item.inode
    ):
        # A successful scan timestamp belongs to the observed directory object,
        # not merely to a path that may later be reused.
        connection.execute("DELETE FROM directory_summary WHERE path_id=?", (path_id,))
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
        completed_directories: dict[int, tuple[int, int]] = {}

        def report(phase: str, directory: Path | None = None) -> None:
            if on_progress is not None:
                current_directory = None
                if directory is not None:
                    relative_directory = directory.relative_to(scope)
                    current_directory = str(relative_directory) if relative_directory.parts else "."
                on_progress(
                    ScanProgress(phase, files_seen, dirs_seen, logical_bytes_seen, current_directory)
                )

        def visit(directory: Path, directory_id: int) -> None:
            nonlocal files_seen, dirs_seen, logical_bytes_seen, failed
            report("scanning", directory)
            try:
                entries = list(os.scandir(directory))
            except OSError as exc:
                _record_error(connection, scope_id, path_key(namespace, directory), "enumerate", exc)
                failed = True
                return
            directory_complete = True
            for entry in entries:
                entry_path = Path(entry.path)
                try:
                    observed = _observation(namespace, entry_path)
                except OSError as exc:
                    _record_error(connection, scope_id, path_key(namespace, entry_path), "stat", exc)
                    failed = True
                    directory_complete = False
                    continue
                path_id = upsert_path(connection, root_id, scan_id, observed)
                if observed.kind == "directory":
                    dirs_seen += 1
                    visit(entry_path, path_id)
                elif observed.kind == "file":
                    files_seen += 1
                    logical_bytes_seen += observed.logical_size or 0
                    report("scanning", directory)
            if directory_complete:
                completed_directories[directory_id] = (now_ns(), scan_id)

        cancelled = False
        try:
            visit(scope, scope_path_id)
        except KeyboardInterrupt:
            cancelled = True

        scope_status = "cancelled" if cancelled else "partial" if failed else "completed"
        report("saving")
        connection.execute(
            "UPDATE scan_scope SET status=?, finished_at_ns=?, paths_seen=? WHERE id=?",
            (scope_status, now_ns(), files_seen + dirs_seen, scope_id),
        )
        if scope_status == "completed":
            _mark_missing(connection, root_id, scan_id, scope_id, scope_path_id)
        refresh_directory_summaries(
            connection,
            root_id,
            scope_path_id,
            completed_directories=completed_directories,
        )
        errors = int(connection.execute("SELECT error_count FROM scan_scope WHERE id=?", (scope_id,)).fetchone()["error_count"])
        connection.execute(
            "UPDATE scan SET status=?, finished_at_ns=?, files_seen=?, dirs_seen=?, errors_seen=? WHERE id=?",
            (scope_status, now_ns(), files_seen, dirs_seen, errors, scan_id),
        )
    report(scope_status)
    if cancelled:
        raise KeyboardInterrupt
    return {
        "scan_id": scan_id,
        "status": scope_status,
        "files_seen": files_seen,
        "dirs_seen": dirs_seen,
        "logical_bytes_seen": logical_bytes_seen,
        "errors_seen": int(
            connection.execute("SELECT error_count FROM scan_scope WHERE id=?", (scope_id,)).fetchone()["error_count"]
        ),
    }
