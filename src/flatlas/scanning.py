"""Filesystem metadata scanning and coverage-safe index updates."""

from __future__ import annotations

import json
import os
import sqlite3
import stat as stat_module
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flatlas.allocation import allocated_size
from flatlas.db import _last_insert_id, now_ns
from flatlas.errors import FlatlasError
from flatlas.lazystat import ExactFallbacks, lazy_lstat, uses_lazy_metadata
from flatlas.paths import (
    Namespace,
    _sqlite_integer,
    discover_namespace,
    display_path,
    display_text,
    get_root,
    parent_key,
    path_key,
)
from flatlas.summaries import refresh_directory_summaries

SCANNER_VERSION = "flatlas-python-0.2"
LAZY_SCANNER_SUFFIX = "+lazy-statx"


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
    allocation_error: OSError | None = None


@dataclass(frozen=True)
class ScanProgress:
    phase: str
    files_seen: int
    dirs_seen: int
    logical_bytes_seen: int
    current_directory: str | None = None


@dataclass(frozen=True)
class ResumeSelection:
    scan_id: int
    refresh_before_ns: int
    scope: Path


def _observation(namespace: Namespace, value: Path, *, root: bool = False, lazy: bool = False) -> Observation:
    # Classify from this single lstat. Extra islink/is_dir/is_file probes would
    # repeat the metadata request per entry, which is the dominant scan cost on
    # network filesystems such as Lustre. lstat never follows the final link, so
    # S_ISDIR/S_ISREG here match the former is_dir()/is_file() after the symlink
    # check. ``lazy`` (Lustre roots) reads metadata without forcing a size
    # resynchronisation; see flatlas.lazystat.
    stat = lazy_lstat(value) if lazy else value.lstat()
    mode = stat.st_mode
    reparse = bool(getattr(stat, "st_file_attributes", 0) & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if root:
        kind = "root"
    elif stat_module.S_ISLNK(mode):
        kind = "symlink"
    elif reparse:
        kind = "reparse"
    elif stat_module.S_ISDIR(mode):
        kind = "directory"
    elif stat_module.S_ISREG(mode):
        kind = "file"
    else:
        kind = "other"
    target: bytes | None = None
    if kind == "symlink":
        try:
            target = os.fsencode(os.readlink(value))
        except OSError:
            target = None
    allocation = None
    allocation_error = None
    if kind == "file":
        try:
            allocation = allocated_size(value, stat)
        except OSError as exc:
            allocation_error = exc
    return Observation(
        path=value, key=path_key(namespace, value),
        name=b"" if root else os.fsencode(value.name), kind=kind,
        device=_sqlite_integer(getattr(stat, "st_dev", None)), inode=_sqlite_integer(getattr(stat, "st_ino", None)),
        mode=mode, uid=getattr(stat, "st_uid", None), gid=getattr(stat, "st_gid", None),
        nlink=getattr(stat, "st_nlink", None),
        logical_size=stat.st_size if kind == "file" else None,
        allocated_size=allocation,
        mtime_ns=getattr(stat, "st_mtime_ns", None),
        # Python's Windows st_ctime is not a stable POSIX change time (and is
        # deprecated as a creation-time alias), so it is not a reliable hash basis.
        change_time_ns=None if sys.platform == "win32" else getattr(stat, "st_ctime_ns", None),
        birth_time_ns=getattr(stat, "st_birthtime_ns", None), symlink_target=target,
        allocation_error=allocation_error,
    )


def _parent_id(connection: sqlite3.Connection, root_id: int, key: bytes) -> int | None:
    parent = parent_key(key)
    if parent is None:
        return None
    found = connection.execute("SELECT id FROM path WHERE root_id = ? AND path_key = ?", (root_id, parent)).fetchone()
    if found is None:
        raise FlatlasError("scanner invariant failed: parent was not recorded")
    return int(found["id"])


def upsert_path(
    connection: sqlite3.Connection,
    root_id: int,
    scan_id: int,
    item: Observation,
    *,
    parent_id: int | None = None,
) -> int:
    """Insert or refresh one observed path.

    Callers that already hold the parent's row id (the scanner always does)
    pass ``parent_id`` to skip a per-entry lookup by ``path_key``; ``None``
    falls back to that lookup, which also covers the namespace root.
    """
    if parent_id is None:
        parent_id = _parent_id(connection, root_id, item.key)
    old = connection.execute(
        """SELECT id, state, entry_kind, device, inode
           FROM path WHERE root_id=? AND path_key=?""",
        (root_id, item.key),
    ).fetchone()
    values = (
        parent_id, item.name, display_path(item.path), item.kind, item.device, item.inode, item.mode,
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
    # Summary rows only ever exist for directory paths, so a path that was not
    # a directory has nothing to delete; skipping the no-op saves a statement
    # per file on every rescan.
    if old["entry_kind"] in {"root", "directory"} and (
        item.kind not in {"root", "directory"}
        or old["state"] == "deleted"
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
    # Hash rows are only created for regular files; directories and other kinds
    # cannot carry one unless the path used to be a file.
    if item.kind == "file" or old["entry_kind"] == "file":
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
        (scope_id, key, operation, getattr(exc, "errno", None), display_text(exc), now_ns()),
    )
    connection.execute("UPDATE scan_scope SET error_count = error_count + 1 WHERE id = ?", (scope_id,))


def _ensure_ancestors(
    connection: sqlite3.Connection,
    namespace: Namespace,
    root_id: int,
    scan_id: int,
    scope: Path,
    *,
    lazy: bool = False,
) -> int:
    values = [namespace.path]
    relative = scope.relative_to(namespace.path)
    current = namespace.path
    for part in relative.parts:
        current = current / part
        values.append(current)
    scope_id = -1
    parent_id: int | None = None
    for index, value in enumerate(values):
        observed = _observation(namespace, value, root=index == 0, lazy=lazy)
        scope_id = upsert_path(connection, root_id, scan_id, observed, parent_id=parent_id)
        parent_id = scope_id
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


def _mark_missing_direct_children(
    connection: sqlite3.Connection,
    root_id: int,
    scan_id: int,
    scope_id: int,
    directory_id: int,
) -> None:
    """Confirm missing direct entries and their descendants for one complete directory."""

    connection.execute(
        """WITH RECURSIVE missing(id) AS (
             SELECT id FROM path
             WHERE root_id=? AND parent_path_id=? AND state='present'
               AND last_seen_scan_id <> ?
             UNION ALL
             SELECT p.id FROM path p JOIN missing m ON p.parent_path_id=m.id
             WHERE p.root_id=? AND p.state='present'
           )
           UPDATE path SET state='deleted', deleted_by_scope_id=?, deleted_at_ns=?
           WHERE id IN missing""",
        (root_id, directory_id, scan_id, root_id, scope_id, now_ns()),
    )


def _directory_refresh_state(
    connection: sqlite3.Connection,
    root_id: int,
    directory_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT ds.self_completed_at_ns, ds.subtree_complete_through_ns
           FROM path p JOIN directory_summary ds ON ds.path_id=p.id
           WHERE p.root_id=? AND p.id=? AND p.state='present'
             AND p.entry_kind IN ('root', 'directory')""",
        (root_id, directory_id),
    ).fetchone()


def _indexed_directories(
    connection: sqlite3.Connection,
    root_id: int,
    parent_id: int,
) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT p.id, p.path_key, p.path_display,
                  ds.subtree_complete_through_ns
           FROM path p LEFT JOIN directory_summary ds ON ds.path_id=p.id
           WHERE p.root_id=? AND p.parent_path_id=? AND p.state='present'
             AND p.entry_kind='directory'
           ORDER BY p.path_key""",
        (root_id, parent_id),
    ).fetchall()


def resume_selection(
    connection: sqlite3.Connection,
    value: Path,
) -> ResumeSelection:
    """Resolve the latest incomplete attempt for one exact directory scope."""

    namespace = discover_namespace(value)
    root = get_root(connection, namespace)
    if root is None:
        raise FlatlasError(f"filesystem is not registered; run 'flatlas init {value}' first")
    scope = value.expanduser().resolve()
    if not scope.is_dir():
        raise FlatlasError(f"scan path must be a directory: {scope}")
    scope_key = path_key(namespace, scope)
    row = connection.execute(
        """SELECT s.id, s.status, s.started_at_ns, s.refresh_before_ns
           FROM scan s JOIN scan_scope ss ON ss.scan_id=s.id
           WHERE ss.root_id=? AND ss.scope_path_key=?
           ORDER BY s.id DESC LIMIT 1""",
        (int(root["id"]), scope_key),
    ).fetchone()
    if row is None:
        raise FlatlasError(f"no previous scan found for {scope}; run without --resume")
    return _resume_selection_from_row(row, scope)


def resume_selection_from_scan(connection: sqlite3.Connection, scan_id: int) -> ResumeSelection:
    """Resolve one incomplete scan ID and infer its unique scope path."""

    rows = connection.execute(
        """SELECT s.id, s.status, s.started_at_ns, s.refresh_before_ns,
                  p.path_display AS scope
           FROM scan s JOIN scan_scope ss ON ss.scan_id=s.id
           LEFT JOIN path p
             ON p.root_id=ss.root_id AND p.path_key=ss.scope_path_key
           WHERE s.id=? ORDER BY ss.id""",
        (scan_id,),
    ).fetchall()
    if not rows:
        raise FlatlasError(f"scan not found: {scan_id}")
    if len(rows) != 1:
        raise FlatlasError(f"scan {scan_id} has multiple scopes; --resume-from cannot infer a unique PATH")
    row = rows[0]
    if row["scope"] is None:
        raise FlatlasError(f"scan {scan_id} scope path is unavailable")
    return _resume_selection_from_row(row, Path(str(row["scope"])))


def _resume_selection_from_row(row: sqlite3.Row, scope: Path) -> ResumeSelection:
    status = str(row["status"])
    if status == "completed":
        raise FlatlasError(f"scan {row['id']} for {scope} is completed; run without resume options")
    if status == "running":
        raise FlatlasError(f"scan {row['id']} for {scope} is still running; cannot resume it")
    refresh_before = row["refresh_before_ns"]
    return ResumeSelection(
        scan_id=int(row["id"]),
        refresh_before_ns=int(row["started_at_ns"] if refresh_before is None else refresh_before),
        scope=scope,
    )


class ScanTimer:
    """Wall time and call counts per scan phase, persisted as ``scan.stats_json``.

    Phases are measured around individual operations, so the recursion in
    ``visit`` does not double count. ``observe`` includes the exact-``lstat``
    fallbacks, reported separately as ``exact_stat_fallbacks``.
    """

    def __init__(self) -> None:
        self.ns: defaultdict[str, int] = defaultdict(int)
        self.calls: defaultdict[str, int] = defaultdict(int)
        self.started = time.perf_counter_ns()
        self._fallbacks = (ExactFallbacks.count, ExactFallbacks.ns)

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        started = time.perf_counter_ns()
        try:
            yield
        finally:
            self.ns[name] += time.perf_counter_ns() - started
            self.calls[name] += 1

    def as_dict(self, entries: int) -> dict[str, Any]:
        wall_ns = time.perf_counter_ns() - self.started
        wall_s = wall_ns / 1e9
        return {
            "wall_s": round(wall_s, 3),
            "entries_per_s": round(entries / wall_s, 1) if wall_s > 0 else None,
            "phases": {
                name: {"s": round(self.ns[name] / 1e9, 3), "calls": self.calls[name]}
                for name in sorted(self.ns, key=lambda key: -self.ns[key])
            },
            "unaccounted_s": round((wall_ns - sum(self.ns.values())) / 1e9, 3),
            "exact_stat_fallbacks": ExactFallbacks.count - self._fallbacks[0],
            "exact_stat_fallback_s": round((ExactFallbacks.ns - self._fallbacks[1]) / 1e9, 3),
        }


def scan_directory(
    connection: sqlite3.Connection,
    value: Path,
    *,
    refresh_before_ns: int | None = None,
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
    lazy = uses_lazy_metadata(root["filesystem_type"])
    timer = ScanTimer()
    kind = "full" if scope == namespace.path else "subtree"
    started = now_ns()
    if refresh_before_ns is not None and refresh_before_ns > started:
        raise FlatlasError("refresh time must not be in the future")
    scope_key = path_key(namespace, scope)
    with connection:
        scan_id = _last_insert_id(connection.execute(
            """INSERT INTO scan(root_id, kind, status, started_at_ns, scanner_version, refresh_before_ns)
               VALUES (?, ?, 'running', ?, ?, ?)""",
            (root_id, kind, started, SCANNER_VERSION + (LAZY_SCANNER_SUFFIX if lazy else ""), refresh_before_ns),
        ))
        scope_id = _last_insert_id(connection.execute(
            "INSERT INTO scan_scope(scan_id, root_id, scope_path_key, status, started_at_ns) VALUES (?, ?, ?, 'running', ?)",
            (scan_id, root_id, scope_key, started),
        ))
        try:
            scope_path_id = _ensure_ancestors(connection, namespace, root_id, scan_id, scope, lazy=lazy)
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
                "refresh_before_ns": refresh_before_ns,
                "errors_seen": 1,
            }
        files_seen = 0
        dirs_seen = 1
        logical_bytes_seen = 0
        failed = False
        completed_directories: dict[int, tuple[int, int]] = {}

        def report(phase: str, directory: Path | None = None) -> None:
            if on_progress is not None:
                with timer.phase("progress"):
                    _report(phase, directory)

        # Files of one directory report it repeatedly; formatting its relative
        # path once per directory keeps progress cheap (~80 us per entry before).
        displayed_directory: Path | None = None
        displayed_text: str | None = None

        def _report(phase: str, directory: Path | None) -> None:
            nonlocal displayed_directory, displayed_text
            if on_progress is not None:
                current_directory = None
                if directory is not None:
                    if displayed_directory != directory:
                        relative_directory = directory.relative_to(scope)
                        displayed_directory = directory
                        displayed_text = display_path(relative_directory) if relative_directory.parts else "."
                    current_directory = displayed_text
                on_progress(
                    ScanProgress(phase, files_seen, dirs_seen, logical_bytes_seen, current_directory)
                )

        def visit(
            directory: Path,
            directory_id: int,
        ) -> None:
            nonlocal files_seen, dirs_seen, logical_bytes_seen, failed
            scan_self = refresh_before_ns is None
            if refresh_before_ns is not None:
                report("scanning", directory)
                with timer.phase("refresh_query"):
                    refresh_state = _directory_refresh_state(connection, root_id, directory_id)
                scan_self = (
                    refresh_state is None
                    or refresh_state["self_completed_at_ns"] is None
                    or int(refresh_state["self_completed_at_ns"]) < refresh_before_ns
                )
                if not scan_self:
                    assert refresh_state is not None
                    subtree_time = refresh_state["subtree_complete_through_ns"]
                    if subtree_time is not None and int(subtree_time) >= refresh_before_ns:
                        return
                    with timer.phase("refresh_query"):
                        indexed_children = _indexed_directories(connection, root_id, directory_id)
                    observed_children: list[tuple[Path, Observation]] = []
                    refresh_parent = False
                    for child in indexed_children:
                        child_subtree_time = child["subtree_complete_through_ns"]
                        if child_subtree_time is not None and int(child_subtree_time) >= refresh_before_ns:
                            continue
                        child_path = Path(str(child["path_display"]))
                        try:
                            with timer.phase("observe"):
                                child_observed = _observation(namespace, child_path, lazy=lazy)
                        except FileNotFoundError:
                            # The child was known when the parent was last
                            # enumerated. Re-enumerate the parent before
                            # confirming that the child was deleted.
                            refresh_parent = True
                            break
                        except OSError as exc:
                            _record_error(connection, scope_id, bytes(child["path_key"]), "stat", exc)
                            failed = True
                            continue
                        if child_observed.kind != "directory":
                            refresh_parent = True
                            break
                        observed_children.append((child_path, child_observed))
                    if not refresh_parent:
                        for child_path, child_observed in observed_children:
                            with timer.phase("db_write"):
                                child_id = upsert_path(
                                    connection, root_id, scan_id, child_observed, parent_id=directory_id
                                )
                            dirs_seen += 1
                            visit(child_path, child_id)
                        return
            report("scanning", directory)
            try:
                with timer.phase("readdir"):
                    entries = list(os.scandir(directory))
            except OSError as exc:
                _record_error(connection, scope_id, path_key(namespace, directory), "enumerate", exc)
                failed = True
                return
            directory_complete = True
            for entry in entries:
                entry_path = Path(entry.path)
                try:
                    with timer.phase("observe"):
                        observed = _observation(namespace, entry_path, lazy=lazy)
                except OSError as exc:
                    _record_error(connection, scope_id, path_key(namespace, entry_path), "stat", exc)
                    failed = True
                    directory_complete = False
                    continue
                with timer.phase("db_write"):
                    path_id = upsert_path(connection, root_id, scan_id, observed, parent_id=directory_id)
                if observed.allocation_error is not None:
                    _record_error(connection, scope_id, observed.key, "stat", observed.allocation_error)
                    failed = True
                    directory_complete = False
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
            with timer.phase("mark_missing"):
                if refresh_before_ns is None:
                    _mark_missing(connection, root_id, scan_id, scope_id, scope_path_id)
                else:
                    for directory_id in completed_directories:
                        _mark_missing_direct_children(connection, root_id, scan_id, scope_id, directory_id)
        with timer.phase("summaries"):
            refresh_directory_summaries(
                connection,
                root_id,
                scope_path_id,
                completed_directories=completed_directories,
            )
        errors = int(connection.execute("SELECT error_count FROM scan_scope WHERE id=?", (scope_id,)).fetchone()["error_count"])
        stats = timer.as_dict(files_seen + dirs_seen)
        connection.execute(
            """UPDATE scan SET status=?, finished_at_ns=?, files_seen=?, dirs_seen=?, errors_seen=?, stats_json=?
               WHERE id=?""",
            (scope_status, now_ns(), files_seen, dirs_seen, errors, json.dumps(stats), scan_id),
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
        "refresh_before_ns": refresh_before_ns,
        "errors_seen": int(
            connection.execute("SELECT error_count FROM scan_scope WHERE id=?", (scope_id,)).fetchone()["error_count"]
        ),
        "stats": stats,
    }
