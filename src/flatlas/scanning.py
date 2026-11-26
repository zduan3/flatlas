"""Filesystem metadata scanning and coverage-safe index updates."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import stat as stat_module
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flatlas.allocation import allocated_size
from flatlas.db import _last_insert_id, now_ns
from flatlas.errors import FlatlasError
from flatlas.lazystat import ExactFallbacks, lazy_lstat, uses_lazy_metadata
from flatlas.paths import (
    Namespace,
    _delete_paths_and_plans,
    _key_range,
    _sqlite_integer,
    discover_namespace,
    display_path,
    display_text,
    get_root,
    parent_key,
    path_from_key,
    path_key,
)
from flatlas.summaries import refresh_directory_summaries, refresh_finished_directories

SCANNER_VERSION = "flatlas-python-0.2"
LAZY_SCANNER_SUFFIX = "+lazy-statx"
# A running scan commits its observations at least this often.
CHECKPOINT_INTERVAL_S = 5.0
# A running scan whose heartbeat is older than this is treated as abandoned.
ABANDONED_AFTER_NS = 30 * 60 * 1_000_000_000


class ScanTakenOverError(FlatlasError):
    """Another process finalized this running scan as abandoned."""


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
    refresh_before_ns: int | None
    scope: Path
    refresh_changed: bool


def _observation(
    namespace: Namespace, value: Path, *, root: bool = False, lazy: bool = False, key: bytes | None = None,
) -> Observation:
    # Classify from this single lstat. Extra islink/is_dir/is_file probes would
    # repeat the metadata request per entry, which is the dominant scan cost on
    # network filesystems such as Lustre. lstat never follows the final link, so
    # S_ISDIR/S_ISREG here match the former is_dir()/is_file() after the symlink
    # check. ``lazy`` (Lustre roots) reads metadata without forcing a size
    # resynchronisation; see flatlas.lazystat. ``key`` is the path key when the
    # caller already has it (the scanner extends its parent's key): path_key()
    # re-splits the whole relative path, which cost ~80 us per deep entry.
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
        path=value, key=path_key(namespace, value) if key is None else key,
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
        """SELECT id, state, entry_kind, device, inode, birth_time_ns
           FROM path WHERE root_id=? AND path_key=?""",
        (root_id, item.key),
    ).fetchone()
    values = (
        parent_id, item.name, item.kind, item.device, item.inode, item.mode,
        item.uid, item.gid, item.nlink, item.logical_size, item.allocated_size, item.mtime_ns,
        item.change_time_ns, item.birth_time_ns, item.symlink_target, scan_id, root_id, item.key,
    )
    if old is None:
        cursor = connection.execute(
            """INSERT INTO path(parent_path_id, name_raw, entry_kind, state,
            device, inode, mode, uid, gid, nlink, logical_size, allocated_size, mtime_ns,
            change_time_ns, birth_time_ns, symlink_target_raw, first_seen_scan_id, last_seen_scan_id,
            root_id, path_key) VALUES (?, ?, ?, 'present', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        or old["birth_time_ns"] != item.birth_time_ns
    ):
        # A successful scan timestamp belongs to the observed directory object,
        # not merely to a path that may later be reused.
        connection.execute("DELETE FROM directory_summary WHERE path_id=?", (path_id,))
    connection.execute(
        """UPDATE path SET parent_path_id=?, name_raw=?, entry_kind=?, state='present',
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
) -> tuple[int, Observation]:
    values = [namespace.path]
    relative = scope.relative_to(namespace.path)
    current = namespace.path
    for part in relative.parts:
        current = current / part
        values.append(current)
    parent_id: int | None = None
    for index, value in enumerate(values[:-1]):
        observed = _observation(namespace, value, root=index == 0, lazy=lazy)
        parent_id = upsert_path(connection, root_id, scan_id, observed, parent_id=parent_id)
    observed = _observation(namespace, scope, root=scope == namespace.path, lazy=lazy)
    return upsert_path(connection, root_id, scan_id, observed, parent_id=parent_id), observed


def _mark_missing(connection: sqlite3.Connection, root_id: int, scan_id: int, scope_id: int, scope_key: bytes) -> int:
    """Mark every present descendant of the scope not seen by this scan as deleted.

    A path key is its parent's key plus one NUL-terminated name, so the scope's
    descendants are exactly the keys in one range of the (root_id, path_key)
    index; walking that range is about twice as fast as a recursive CTE over
    parent links. The scope itself was observed by this scan.
    """
    bounds, keys = _key_range(scope_key, descendants_only=True)
    cursor = connection.execute(
        f"""UPDATE path SET state='deleted', deleted_by_scope_id=?, deleted_at_ns=?
            WHERE root_id=? AND {bounds} AND state='present' AND last_seen_scan_id <> ?""",
        (scope_id, now_ns(), root_id, *keys, scan_id),
    )
    return cursor.rowcount


def _overlapping_scans(
    connection: sqlite3.Connection, root_id: int, scope_key: bytes, scan_id: int, *,
    starting: bool = False, since_ns: int | None = None,
) -> sqlite3.Cursor:
    condition = "s.id<? AND s.status='running'" if starting else "s.id>?"
    parameters = (scan_id,)
    if since_ns is not None:
        condition = "(s.id>? OR (s.id<? AND (s.finished_at_ns IS NULL OR s.finished_at_ns>?)))"
        parameters = (scan_id, scan_id, since_ns)
    return connection.execute(
        f"""SELECT s.id, s.started_at_ns, s.heartbeat_at_ns, s.owner_json
           FROM scan s JOIN scan_scope ss ON ss.scan_id=s.id
           WHERE s.root_id=? AND {condition}
             AND (ss.scope_path_key=? OR substr(ss.scope_path_key, 1, length(?))=?
                  OR substr(?, 1, length(ss.scope_path_key))=ss.scope_path_key)""",
        (root_id, *parameters, scope_key, scope_key, scope_key, scope_key),
    )


def _purge_missing(
    connection: sqlite3.Connection, root_id: int, scan_id: int, scope_key: bytes,
) -> tuple[int, int]:
    """Remove missing rows and tombstones together in the completed scope's transaction."""
    bounds, keys = _key_range(scope_key, descendants_only=True, column="p.path_key")
    condition = f"p.root_id=? AND {bounds} AND (p.state='deleted' OR p.last_seen_scan_id<>?)"
    parameters = (root_id, *keys, scan_id)
    newly_missing, matched = connection.execute(
        f"SELECT count(CASE WHEN p.state='present' THEN 1 END), count(*) FROM path p WHERE {condition}",
        parameters,
    ).fetchone()
    if matched == 0:
        return 0, 0
    # Delete both states in one statement: a historical tombstone can still
    # reference a directory that this scan just confirmed missing.
    purged, _ = _delete_paths_and_plans(connection, condition, parameters)
    return int(newly_missing), purged


def _mark_missing_direct_children(
    connection: sqlite3.Connection,
    root_id: int,
    scan_id: int,
    scope_id: int,
    directory_id: int,
) -> int:
    """Confirm missing direct entries and their descendants for one complete directory."""

    connection.execute(
        """WITH RECURSIVE missing(id) AS (
             SELECT id FROM path
             WHERE root_id=? AND parent_path_id=? AND state='present'
               AND last_seen_scan_id <> ?
             UNION ALL
             SELECT child.id
             FROM path direct CROSS JOIN path child ON child.parent_path_id=direct.id
             WHERE direct.root_id=? AND direct.parent_path_id=? AND direct.state='present'
               AND direct.last_seen_scan_id=? AND direct.entry_kind NOT IN ('root', 'directory')
               AND child.state='present'
             UNION ALL
             SELECT p.id FROM missing m CROSS JOIN path p ON p.parent_path_id=m.id
             WHERE p.root_id=? AND p.state='present'
           )
           UPDATE path SET state='deleted', deleted_by_scope_id=?, deleted_at_ns=?
           WHERE id IN missing""",
        (root_id, directory_id, scan_id, root_id, directory_id, scan_id, root_id, scope_id, now_ns()),
    )
    # sqlite3.Cursor.rowcount is -1 for an UPDATE prefixed by WITH.
    return int(connection.execute("SELECT changes()").fetchone()[0])


def _directory_refresh_state(
    connection: sqlite3.Connection,
    root_id: int,
    directory_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT ds.self_completed_at_ns, ds.subtree_complete_through_ns,
                  ds.enumerated_mtime_ns, ds.enumerated_change_time_ns
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
        """SELECT p.id, p.path_key,
                  ds.subtree_complete_through_ns
           FROM path p LEFT JOIN directory_summary ds ON ds.path_id=p.id
           WHERE p.root_id=? AND p.parent_path_id=? AND p.state='present'
             AND p.entry_kind='directory'
           ORDER BY p.path_key""",
        (root_id, parent_id),
    ).fetchall()


def _pid_namespace() -> str:
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return ""


def scan_owner() -> dict[str, Any]:
    """Identity of this process as recorded on the scans it runs."""
    return {"host": socket.gethostname(), "pid_ns": _pid_namespace(), "pid": os.getpid()}


def owner_is_alive(owner_json: str | None, heartbeat_at_ns: int, *, now: int | None = None) -> bool:
    """Whether the process running a scan may still be writing it.

    A heartbeat older than ``ABANDONED_AFTER_NS`` means abandoned. Otherwise
    the process is probed only when it ran on this host in this PID namespace
    (containers and sandboxes on one host do not see each other's PIDs) and
    the platform exposes namespaces; anything else is presumed alive.
    """
    current_time = now_ns() if now is None else now
    if current_time - heartbeat_at_ns >= ABANDONED_AFTER_NS:
        return False
    owner = json.loads(owner_json) if owner_json else {}
    current = scan_owner()
    if not current["pid_ns"] or owner.get("host") != current["host"] or owner.get("pid_ns") != current["pid_ns"]:
        return True
    try:
        os.kill(int(owner["pid"]), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _claim_running_scan(connection: sqlite3.Connection, scan_id: int, assignments: str, values: tuple[Any, ...]) -> None:
    """Update this process's running scan, failing if another process finalized it."""
    cursor = connection.execute(f"UPDATE scan SET {assignments} WHERE id=? AND status='running'", (*values, scan_id))
    if cursor.rowcount != 1:
        raise ScanTakenOverError(f"scan {scan_id} was finalized by another process; stopping without saving")


def _finalize_abandoned(connection: sqlite3.Connection, row: sqlite3.Row, *, reason: str = "resume") -> None:
    """Cancel an abandoned scan in the caller's transaction, keeping its checkpoints."""
    finished = int(row["heartbeat_at_ns"] or row["started_at_ns"])
    note = (
        f"abandoned by {row['owner_json'] or 'unknown owner'}; "
        f"finalized on {reason} by {json.dumps(scan_owner(), sort_keys=True)}"
    )
    connection.execute(
        "UPDATE scan_scope SET status='cancelled', finished_at_ns=? WHERE scan_id=? AND status='running'",
        (finished, row["id"]),
    )
    connection.execute(
        """UPDATE scan SET status='cancelled', finished_at_ns=?, note=?, stats_json=?,
                  errors_seen=(SELECT coalesce(sum(error_count), 0) FROM scan_scope WHERE scan_id=?)
           WHERE id=? AND status='running'""",
        (finished, note, json.dumps({"paths_deleted": 0}), row["id"], row["id"]),
    )


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
        """SELECT s.id, s.status, s.started_at_ns, s.refresh_before_ns, s.refresh_changed,
                  s.heartbeat_at_ns, s.owner_json
           FROM scan s JOIN scan_scope ss ON ss.scan_id=s.id
           WHERE ss.root_id=? AND ss.scope_path_key=? AND s.max_depth IS NULL
           ORDER BY s.id DESC LIMIT 1""",
        (int(root["id"]), scope_key),
    ).fetchone()
    if row is None:
        raise FlatlasError(f"no previous scan found for {scope}; run without --resume")
    return _resume_selection_from_row(connection, row, scope)


def resume_selection_from_scan(connection: sqlite3.Connection, scan_id: int) -> ResumeSelection:
    """Resolve one incomplete scan ID and infer its unique scope path."""

    rows = connection.execute(
        """SELECT s.id, s.status, s.started_at_ns, s.refresh_before_ns, s.max_depth, s.refresh_changed,
                  s.heartbeat_at_ns, s.owner_json, p.path_key, r.root_path_raw
           FROM scan s JOIN scan_scope ss ON ss.scan_id=s.id
           JOIN root r ON r.id=ss.root_id
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
    if row["max_depth"] is not None:
        raise FlatlasError(f"scan {scan_id} is a shallow scan; rerun it instead of resuming")
    if row["path_key"] is None:
        raise FlatlasError(f"scan {scan_id} scope path is unavailable")
    return _resume_selection_from_row(connection, row, path_from_key(row["root_path_raw"], row["path_key"]))


def _resume_selection_from_row(connection: sqlite3.Connection, row: sqlite3.Row, scope: Path) -> ResumeSelection:
    status = str(row["status"])
    if status == "completed":
        raise FlatlasError(f"scan {row['id']} for {scope} is completed; run without resume options")
    if status == "running":
        heartbeat = int(row["heartbeat_at_ns"] or row["started_at_ns"])
        if owner_is_alive(row["owner_json"], heartbeat):
            age = max(now_ns() - heartbeat, 0) // 1_000_000_000
            raise FlatlasError(
                f"scan {row['id']} for {scope} is still running "
                f"(owner {row['owner_json'] or 'unknown'}, last checkpoint {age}s ago); cannot resume it"
            )
        # The owner crashed or was killed: keep its checkpoints and resume from them.
        with connection:
            _finalize_abandoned(connection, row)
    refresh_before = row["refresh_before_ns"]
    refresh_changed = bool(row["refresh_changed"])
    return ResumeSelection(
        scan_id=int(row["id"]),
        refresh_before_ns=None if refresh_changed else int(row["started_at_ns"] if refresh_before is None else refresh_before),
        scope=scope,
        refresh_changed=refresh_changed,
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
    refresh_changed: bool = False,
    on_progress: Callable[[ScanProgress], None] | None = None,
    shallow: bool = False,
    purge_deleted: bool = False,
) -> dict[str, Any]:
    """Scan ``value``; ``shallow`` re-lists only its direct entries.

    A complete shallow scan confirms missing direct entries (with their indexed
    descendants) but neither enters nor refreshes subdirectories.
    A complete purge scan physically removes missing rows and prior tombstones.

    The running scan is committed first and its observations are checkpointed
    every ``CHECKPOINT_INTERVAL_S``; only the final transaction confirms
    deletions, so an interrupted or killed scan keeps its checkpoints but never
    deletes anything.
    """
    if shallow and refresh_before_ns is not None:
        raise ValueError("a shallow scan cannot use refresh_before_ns")
    if refresh_changed and (shallow or purge_deleted or refresh_before_ns is not None):
        raise ValueError("refresh_changed requires a recursive scan without an age cutoff or purge")
    if purge_deleted and (shallow or refresh_before_ns is not None):
        raise ValueError("purge_deleted requires a full scan")
    empty_deletion_counts = {"paths_deleted": 0}
    if purge_deleted:
        empty_deletion_counts["paths_purged"] = 0
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
            """INSERT INTO scan(root_id, kind, status, started_at_ns, scanner_version, refresh_before_ns, max_depth, refresh_changed,
                                heartbeat_at_ns, owner_json)
               VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)""",
            (
                root_id, kind, started, SCANNER_VERSION + (LAZY_SCANNER_SUFFIX if lazy else ""),
                refresh_before_ns, 0 if shallow else None, refresh_changed, started, json.dumps(scan_owner(), sort_keys=True),
            ),
        ))
        scope_id = _last_insert_id(connection.execute(
            "INSERT INTO scan_scope(scan_id, root_id, scope_path_key, status, started_at_ns) VALUES (?, ?, ?, 'running', ?)",
            (scan_id, root_id, scope_key, started),
        ))
        if purge_deleted:
            # Hold the write lock while excluding active overlapping scans.
            # Finalizing an abandoned owner fences off all its future checkpoints.
            for overlap in _overlapping_scans(connection, root_id, scope_key, scan_id, starting=True).fetchall():
                heartbeat = int(overlap["heartbeat_at_ns"] or overlap["started_at_ns"])
                if owner_is_alive(overlap["owner_json"], heartbeat):
                    raise FlatlasError(f"cannot purge deleted paths: overlapping scan {overlap['id']} is still running")
                _finalize_abandoned(connection, overlap, reason="purge")
        try:
            scope_path_id, scope_observed = _ensure_ancestors(connection, namespace, root_id, scan_id, scope, lazy=lazy)
            scope_first_seen = int(connection.execute(
                "SELECT first_seen_scan_id FROM path WHERE id=?", (scope_path_id,)
            ).fetchone()[0])
        except OSError as exc:
            _record_error(connection, scope_id, scope_key, "stat", exc)
            connection.execute("UPDATE scan_scope SET status='failed', finished_at_ns=? WHERE id=?", (now_ns(), scope_id))
            stats = timer.as_dict(0) | {"checkpoints": 0} | empty_deletion_counts
            connection.execute(
                "UPDATE scan SET status='failed', finished_at_ns=?, errors_seen=1, stats_json=? WHERE id=?",
                (now_ns(), json.dumps(stats), scan_id),
            )
            return {
                "scan_id": scan_id,
                "status": "failed",
                "files_seen": 0,
                "dirs_seen": 0,
                "logical_bytes_seen": 0,
                "refresh_before_ns": refresh_before_ns,
                "refresh_changed": refresh_changed,
                "errors_seen": 1,
                **empty_deletion_counts,
                "stats": stats,
            }
    files_seen = 0
    dirs_seen = 1
    logical_bytes_seen = 0
    failed = False
    # Directories whose traversal finished and the subset that completed,
    # split into committed state and the batch since the last checkpoint.
    completed_directories: dict[int, tuple[int, int]] = {}
    pending_completed: dict[int, tuple[int, int]] = {}
    pending_finished: list[int] = []
    enumeration_bases: dict[int, tuple[int | None, int | None]] = {}
    directories_skipped = 0
    committed_counts = (files_seen, dirs_seen, logical_bytes_seen)
    checkpoints = 0
    next_checkpoint = time.monotonic() + CHECKPOINT_INTERVAL_S

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

    def checkpoint() -> None:
        """Commit the batch: paths, errors, finished directory summaries and counters."""
        nonlocal committed_counts, checkpoints, next_checkpoint
        with timer.phase("checkpoint"):
            if pending_finished:
                refresh_finished_directories(connection, pending_finished, pending_completed)
            _claim_running_scan(
                connection, scan_id,
                "files_seen=?, dirs_seen=?, logical_bytes_seen=?, heartbeat_at_ns=?",
                (files_seen, dirs_seen, logical_bytes_seen, now_ns()),
            )
            connection.execute(
                "UPDATE scan_scope SET paths_seen=? WHERE id=?", (files_seen + dirs_seen, scope_id)
            )
        with timer.phase("commit"):
            connection.commit()
        completed_directories.update(pending_completed)
        pending_completed.clear()
        pending_finished.clear()
        committed_counts = (files_seen, dirs_seen, logical_bytes_seen)
        checkpoints += 1
        next_checkpoint = time.monotonic() + CHECKPOINT_INTERVAL_S

    def maybe_checkpoint() -> None:
        if time.monotonic() >= next_checkpoint:
            checkpoint()

    def visit(
        directory: Path,
        directory_id: int,
        observed_directory: Observation,
    ) -> None:
        nonlocal files_seen, dirs_seen, logical_bytes_seen, failed, directories_skipped
        maybe_checkpoint()
        directory_key = observed_directory.key
        basis = (observed_directory.mtime_ns, observed_directory.change_time_ns)
        if refresh_before_ns is not None or refresh_changed:
            report("scanning", directory)
            with timer.phase("refresh_query"):
                refresh_state = _directory_refresh_state(connection, root_id, directory_id)
            if refresh_changed:
                skip_enumeration = (
                    refresh_state is not None
                    and basis[0] is not None
                    and observed_directory.inode not in (None, 0)
                    and observed_directory.device is not None
                    and basis == (refresh_state["enumerated_mtime_ns"], refresh_state["enumerated_change_time_ns"])
                )
            else:
                assert refresh_before_ns is not None
                skip_enumeration = (
                    refresh_state is not None
                    and refresh_state["self_completed_at_ns"] is not None
                    and int(refresh_state["self_completed_at_ns"]) >= refresh_before_ns
                )
            if skip_enumeration:
                assert refresh_state is not None
                subtree_time = refresh_state["subtree_complete_through_ns"]
                if refresh_before_ns is not None and subtree_time is not None and int(subtree_time) >= refresh_before_ns:
                    return
                with timer.phase("refresh_query"):
                    indexed_children = _indexed_directories(connection, root_id, directory_id)
                observed_children: list[tuple[Path, Observation]] = []
                refresh_parent = False
                for child in indexed_children:
                    child_subtree_time = child["subtree_complete_through_ns"]
                    if (refresh_before_ns is not None and child_subtree_time is not None
                            and int(child_subtree_time) >= refresh_before_ns):
                        continue
                    child_path = path_from_key(os.fsencode(namespace.path), child["path_key"])
                    try:
                        with timer.phase("observe"):
                            child_observed = _observation(
                                namespace, child_path, lazy=lazy, key=bytes(child["path_key"])
                            )
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
                    if refresh_changed:
                        directories_skipped += 1
                    for child_path, child_observed in observed_children:
                        with timer.phase("db_write"):
                            child_id = upsert_path(
                                connection, root_id, scan_id, child_observed, parent_id=directory_id
                            )
                        dirs_seen += 1
                        visit(child_path, child_id, child_observed)
                    pending_finished.append(directory_id)
                    return
        report("scanning", directory)
        try:
            with timer.phase("readdir"):
                entries = list(os.scandir(directory))
        except OSError as exc:
            _record_error(connection, scope_id, directory_key, "enumerate", exc)
            failed = True
            return
        directory_complete = True
        for entry in entries:
            maybe_checkpoint()
            entry_path = Path(entry.path)
            entry_key = directory_key + os.fsencode(entry.name) + b"\0"
            try:
                with timer.phase("observe"):
                    observed = _observation(namespace, entry_path, lazy=lazy, key=entry_key)
            except OSError as exc:
                _record_error(connection, scope_id, entry_key, "stat", exc)
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
                if not shallow:
                    visit(entry_path, path_id, observed)
            elif observed.kind == "file":
                files_seen += 1
                logical_bytes_seen += observed.logical_size or 0
                report("scanning", directory)
        if directory_complete:
            pending_completed[directory_id] = (now_ns(), scan_id)
            enumeration_bases[directory_id] = basis
        pending_finished.append(directory_id)

    def abandon(status: str) -> None:
        """Discard the uncommitted batch and end the scan with its checkpointed counts."""
        connection.rollback()
        files, dirs, logical_bytes = committed_counts
        stats = timer.as_dict(files + dirs) | {"checkpoints": checkpoints} | empty_deletion_counts
        with connection:
            connection.execute(
                "UPDATE scan_scope SET status=?, finished_at_ns=? WHERE id=? AND status='running'",
                (status, now_ns(), scope_id),
            )
            connection.execute(
                """UPDATE scan SET status=?, finished_at_ns=?, files_seen=?, dirs_seen=?, logical_bytes_seen=?,
                          errors_seen=(SELECT error_count FROM scan_scope WHERE id=?), stats_json=?
                   WHERE id=? AND status='running'""",
                (status, now_ns(), files, dirs, logical_bytes, scope_id, json.dumps(stats), scan_id),
            )

    cancelled = False
    try:
        visit(scope, scope_path_id, scope_observed)
    except KeyboardInterrupt:
        cancelled = True
    except ScanTakenOverError:
        connection.rollback()
        raise
    except Exception:
        with suppress(sqlite3.Error):
            abandon("failed")
        raise

    scope_status = "cancelled" if cancelled else "partial" if failed else "completed"
    deletion_counts = empty_deletion_counts.copy()
    overlap_error = None
    try:
        report("saving")
        # The first interrupt keeps everything observed so far, as before checkpoints.
        checkpoint()
        with connection:
            if scope_status == "completed" and (purge_deleted or refresh_changed):
                # Last-seen IDs may be overwritten while the write lock is
                # released between checkpoints. Check under the final write lock.
                connection.execute("BEGIN IMMEDIATE")
                overlap = _overlapping_scans(
                    connection, root_id, scope_key, scan_id, since_ns=started if refresh_changed else None,
                ).fetchone()
                if overlap is not None:
                    overlap_error = (
                        f"cannot confirm deleted paths: scan {overlap['id']} overlapped this scope; rerun the scan"
                        if refresh_changed else
                        f"cannot purge deleted paths: scan {overlap['id']} overlapped this scope; rerun the full scan"
                    )
                    scope_status = "failed"
                    connection.execute("UPDATE scan SET note=? WHERE id=?", (overlap_error, scan_id))
            connection.execute(
                "UPDATE scan_scope SET status=?, finished_at_ns=?, paths_seen=? WHERE id=?",
                (scope_status, now_ns(), files_seen + dirs_seen, scope_id),
            )
            if scope_status == "completed":
                with timer.phase("purge_missing" if purge_deleted else "mark_missing"):
                    if purge_deleted:
                        if scope_first_seen != scan_id:
                            newly_missing, purged = _purge_missing(connection, root_id, scan_id, scope_key)
                            deletion_counts.update(paths_deleted=newly_missing, paths_purged=purged)
                    elif shallow:
                        deletion_counts["paths_deleted"] = _mark_missing_direct_children(
                            connection, root_id, scan_id, scope_id, scope_path_id,
                        )
                    elif refresh_before_ns is None and not refresh_changed:
                        if scope_first_seen != scan_id:
                            # A scope first recorded by this scan had no indexed
                            # descendants (a child row needs its parent row), so
                            # nothing can be missing; skip the whole-subtree pass.
                            deletion_counts["paths_deleted"] = _mark_missing(connection, root_id, scan_id, scope_id, scope_key)
                    else:
                        for directory_id in completed_directories:
                            deletion_counts["paths_deleted"] += _mark_missing_direct_children(
                                connection, root_id, scan_id, scope_id, directory_id,
                            )
            with timer.phase("summaries"):
                refresh_directory_summaries(
                    connection,
                    root_id,
                    scope_path_id,
                    completed_directories=completed_directories if overlap_error is None else None,
                    max_depth=1 if shallow else None,
                )
                if scope_status == "completed" and (
                    refresh_changed or not _overlapping_scans(
                        connection, root_id, scope_key, scan_id, since_ns=started,
                    ).fetchone()
                ):
                    connection.executemany(
                        """UPDATE directory_summary SET enumerated_mtime_ns=?, enumerated_change_time_ns=?
                           WHERE path_id=? AND last_completed_scan_id=?
                             AND EXISTS (SELECT 1 FROM path p WHERE p.id=path_id AND p.last_seen_scan_id=?)
                             AND (enumerated_mtime_ns IS NOT ? OR enumerated_change_time_ns IS NOT ?)""",
                        ((mtime, ctime, path_id, scan_id, scan_id, mtime, ctime)
                         for path_id, (mtime, ctime) in enumeration_bases.items()),
                    )
            errors = int(
                connection.execute("SELECT error_count FROM scan_scope WHERE id=?", (scope_id,)).fetchone()["error_count"]
            )
            stats = timer.as_dict(files_seen + dirs_seen) | {"checkpoints": checkpoints} | deletion_counts
            if refresh_changed:
                stats["directories_skipped"] = directories_skipped
            _claim_running_scan(
                connection, scan_id,
                "status=?, finished_at_ns=?, files_seen=?, dirs_seen=?, logical_bytes_seen=?, errors_seen=?, "
                "stats_json=?, heartbeat_at_ns=?",
                (scope_status, now_ns(), files_seen, dirs_seen, logical_bytes_seen, errors, json.dumps(stats), now_ns()),
            )
    except KeyboardInterrupt:
        # Interrupted while saving: keep the checkpoints and end the scan cheaply.
        abandon("cancelled")
        report("cancelled")
        raise
    except ScanTakenOverError:
        connection.rollback()
        raise
    except Exception:
        with suppress(sqlite3.Error):
            abandon("failed")
        raise
    report(scope_status)
    if overlap_error is not None:
        raise FlatlasError(overlap_error)
    if cancelled:
        raise KeyboardInterrupt
    return {
        "scan_id": scan_id,
        "status": scope_status,
        "files_seen": files_seen,
        "dirs_seen": dirs_seen,
        "logical_bytes_seen": logical_bytes_seen,
        "refresh_before_ns": refresh_before_ns,
        "refresh_changed": refresh_changed,
        "errors_seen": errors,
        **deletion_counts,
        "stats": stats,
    }
