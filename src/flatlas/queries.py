"""Read-only filesystem index and live-view queries."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections import OrderedDict, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flatlas.duplicate_filters import DuplicateFilters
from flatlas.errors import FlatlasError, PathError
from flatlas.findexpr import FindContext, FindEntry, FindExpression
from flatlas.lazystat import uses_lazy_metadata
from flatlas.mounts import mount_entry
from flatlas.paths import (
    _scope_row,
    discover_namespace,
    display_path,
    get_root,
    indexed_key,
    parent_key,
    path_key,
    scope_path,
)
from flatlas.records import FREED_FIELDS, GONE, NEW, OK, PART, entry_record
from flatlas.scanning import _observation


def list_roots(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Registered namespaces as recorded in the index; never touches the filesystem.

    ``metadata`` is the mode the scanner will use for the root (``lazy`` for
    Lustre, otherwise ``exact``). Indexed totals come from the namespace root's
    directory summary and are ``None`` until something under it was scanned.
    """
    rows = connection.execute(
        """SELECT r.id, r.root_path_display, r.platform, r.namespace_kind, r.filesystem_type,
                  r.enabled, r.created_at_ns, r.updated_at_ns,
                  ds.subtree_files, ds.subtree_logical_size, ds.subtree_allocated_size,
                  s.id AS last_scan_id, s.status AS last_scan_status,
                  s.started_at_ns AS last_scan_started_at_ns,
                  s.finished_at_ns AS last_scan_finished_at_ns
           FROM root r
           LEFT JOIN path p ON p.root_id=r.id AND p.parent_path_id IS NULL
                AND p.entry_kind='root' AND p.state='present'
           LEFT JOIN directory_summary ds ON ds.path_id=p.id
           LEFT JOIN scan s ON s.id=(SELECT max(id) FROM scan WHERE root_id=r.id)
           ORDER BY r.id"""
    ).fetchall()
    return [
        {
            "type": "root",
            "id": row["id"],
            "path": row["root_path_display"],
            "platform": row["platform"],
            "namespace_kind": row["namespace_kind"],
            "filesystem_type": row["filesystem_type"],
            "metadata": "lazy" if uses_lazy_metadata(row["filesystem_type"]) else "exact",
            "enabled": bool(row["enabled"]),
            "created_at_ns": row["created_at_ns"],
            "updated_at_ns": row["updated_at_ns"],
            "files": row["subtree_files"],
            "logical_size": row["subtree_logical_size"],
            "allocated_size": row["subtree_allocated_size"],
            "last_scan_id": row["last_scan_id"],
            "last_scan_status": row["last_scan_status"],
            "last_scan_started_at_ns": row["last_scan_started_at_ns"],
            "last_scan_finished_at_ns": row["last_scan_finished_at_ns"],
        }
        for row in rows
    ]


def _usage_row(mount_point: Path, *, statfs_path: Path | None = None, status_when_ok: str = "ok") -> dict[str, Any]:
    entry = mount_entry(mount_point)
    try:
        # Like GNU df, query the given FILE itself rather than its mount point:
        # filesystems with directory quotas (Lustre project quotas, for one)
        # answer statfs with the quota that applies to that directory.
        usage = shutil.disk_usage(statfs_path or mount_point)
    except OSError:
        total = used = available = use_percent = None
        status = "unavailable"
    else:
        # shutil reports used = f_blocks - f_bfree and free = f_bavail, the same
        # quantities GNU df prints as Used and Available. GNU df computes Use%
        # against used + available (space usable by unprivileged users),
        # rounded up, not against the total.
        total = _blocks_1k(usage.total)
        used = _blocks_1k(usage.used)
        available = _blocks_1k(usage.free)
        usable = usage.used + usage.free
        use_percent = None if usable == 0 else -(-usage.used * 100 // usable)
        status = status_when_ok
    return {
        "type": "filesystem",
        "filesystem": str(mount_point) if entry is None else entry.source,
        "fstype": None if entry is None else entry.fstype,
        "blocks_1k": total,
        "used_1k": used,
        "available_1k": available,
        "use_percent": use_percent,
        "mounted_on": str(mount_point),
        "status": status,
    }


def filesystem_usage(
    connection: sqlite3.Connection | None = None,
    files: list[Path] | None = None,
) -> list[dict[str, Any]]:
    """Live capacity in the shape of GNU ``df``.

    With ``files``, report the filesystem containing each one (registration is
    not required). Without, report every registered namespace from the index.
    """
    if files:
        return [_usage_row(discover_namespace(value).path, statfs_path=value) for value in files]
    if connection is None:
        raise ValueError("a connection is required when no files are given")
    rows = connection.execute("SELECT root_path_display, enabled FROM root ORDER BY root_path_display")
    return [
        _usage_row(Path(row["root_path_display"]), status_when_ok="ok" if row["enabled"] else "disabled")
        for row in rows
    ]


def _blocks_1k(byte_count: int) -> int:
    return (byte_count + 1023) // 1024


def list_scans(
    connection: sqlite3.Connection,
    *,
    scope: Path | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    parameters: list[Any] = []
    where = ""
    if scope is not None:
        selected_path = scope.expanduser().resolve()
        key = indexed_key(connection, selected_path)
        indexed = None if key is None else connection.execute(
            "SELECT root_id, path_key FROM path WHERE root_id=? AND path_key=? AND path_display=?",
            (*key, display_path(selected_path)),
        ).fetchone()
        if indexed is None:
            indexed = connection.execute(
                """SELECT root_id, path_key FROM path
                   WHERE path_display=? ORDER BY state='present' DESC LIMIT 1""",
                (display_path(selected_path),),
            ).fetchone()
        if indexed is None:
            namespace = discover_namespace(selected_path)
            root = get_root(connection, namespace)
            if root is None:
                raise FlatlasError(f"filesystem is not registered; run 'flatlas init {scope}' first")
            root_id = int(root["id"])
            selected_key = path_key(namespace, selected_path)
        else:
            root_id = int(indexed["root_id"])
            selected_key = bytes(indexed["path_key"])
        where = """WHERE ss.root_id=? AND (
            substr(ss.scope_path_key, 1, length(?))=?
            OR substr(?, 1, length(ss.scope_path_key))=ss.scope_path_key
        )"""
        parameters.extend((root_id, selected_key, selected_key, selected_key))
    parameters.append(-1 if limit is None else limit)  # SQLite: a negative LIMIT means none
    rows = connection.execute(
        f"""SELECT 'scan' AS type, s.id AS scan_id, s.root_id, r.root_path_display AS root,
                   s.kind, s.status, s.started_at_ns, s.finished_at_ns,
                   CASE WHEN s.finished_at_ns IS NULL THEN NULL
                        ELSE s.finished_at_ns-s.started_at_ns END AS duration_ns,
                   s.files_seen, s.dirs_seen, s.errors_seen,
                   s.refresh_before_ns, s.max_depth, s.scanner_version, s.note,
                   ss.id AS scope_id, ss.status AS scope_status,
                   ss.paths_seen, ss.error_count,
                   p.path_display AS scope,
                   hex(ss.scope_path_key) AS scope_path_key_hex
            FROM scan s
            JOIN root r ON r.id=s.root_id
            JOIN scan_scope ss ON ss.scan_id=s.id
            LEFT JOIN path p
              ON p.root_id=ss.root_id AND p.path_key=ss.scope_path_key
            {where}
            ORDER BY s.id DESC, ss.id
            LIMIT ?""",
        parameters,
    ).fetchall()
    return [dict(row) for row in rows]


def scan_info(connection: sqlite3.Connection, scan_id: int) -> dict[str, Any]:
    scan = connection.execute(
        """SELECT s.id AS scan_id, s.root_id, r.root_path_display AS root,
                  s.kind, s.status, s.started_at_ns, s.finished_at_ns,
                  CASE WHEN s.finished_at_ns IS NULL THEN NULL
                       ELSE s.finished_at_ns-s.started_at_ns END AS duration_ns,
                  s.files_seen, s.dirs_seen, s.errors_seen, s.logical_bytes_seen,
                  s.refresh_before_ns, s.max_depth, s.scanner_version, s.note,
                  s.heartbeat_at_ns, s.owner_json, s.stats_json
           FROM scan s JOIN root r ON r.id=s.root_id
           WHERE s.id=?""",
        (scan_id,),
    ).fetchone()
    if scan is None:
        raise FlatlasError(f"scan not found: {scan_id}")
    scopes = connection.execute(
        """SELECT ss.id AS scope_id, ss.status, ss.started_at_ns,
                  ss.finished_at_ns, ss.paths_seen, ss.error_count,
                  p.path_display AS scope,
                  hex(ss.scope_path_key) AS scope_path_key_hex
           FROM scan_scope ss
           LEFT JOIN path p
             ON p.root_id=ss.root_id AND p.path_key=ss.scope_path_key
           WHERE ss.scan_id=? ORDER BY ss.id""",
        (scan_id,),
    ).fetchall()
    errors = connection.execute(
        """SELECT e.id AS error_id, e.scan_scope_id,
                  p.path_display AS path, hex(e.path_key) AS path_key_hex,
                  e.operation, e.error_code, e.message, e.occurred_at_ns
           FROM scan_error e
           JOIN scan_scope ss ON ss.id=e.scan_scope_id
           LEFT JOIN path p
             ON p.root_id=ss.root_id AND p.path_key=e.path_key
           WHERE ss.scan_id=? ORDER BY e.id""",
        (scan_id,),
    ).fetchall()
    details = dict(scan)
    stats_json = details.pop("stats_json")
    owner_json = details.pop("owner_json")
    return {
        **details,
        "owner": None if owner_json is None else json.loads(owner_json),
        "stats": None if stats_json is None else json.loads(stats_json),
        "scopes": [dict(row) for row in scopes],
        "errors": [dict(row) for row in errors],
    }


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
            actual_paths = [Path(entry.path) for entry in entries]
    except OSError as exc:
        raise PathError(f"cannot list directory {selected_path}: {exc}") from exc

    parent = connection.execute(
        "SELECT id FROM path WHERE root_id=? AND path_key=?",
        (root["id"], path_key(namespace, selected_path)),
    ).fetchone()
    indexed_rows = [] if parent is None else connection.execute(
        """SELECT id, path_key, path_display, entry_kind, state, logical_size, allocated_size,
                  mtime_ns, last_seen_scan_id, first_seen_scan_id
        FROM path WHERE parent_path_id=? AND state='present'""",
        (parent["id"],),
    ).fetchall()
    indexed_by_path = {_normalized_display(row["path_display"]): row for row in indexed_rows}

    usage_rows = [] if parent is None else connection.execute(
        """SELECT p.id, ds.subtree_files AS files, ds.subtree_entries AS entries,
                  ds.subtree_logical_size AS logical_size,
                  ds.subtree_allocated_size AS allocated_size,
                  ds.self_completed_at_ns, ds.subtree_complete_through_ns,
                  ds.last_completed_scan_id
           FROM path p JOIN directory_summary ds ON ds.path_id=p.id
           WHERE p.parent_path_id=? AND p.state='present' AND p.entry_kind='directory'""",
        (parent["id"],),
    ).fetchall()
    usage_by_id = {row["id"]: row for row in usage_rows}
    parent_summary = None if parent is None else connection.execute(
        """SELECT self_completed_at_ns, last_completed_scan_id
           FROM directory_summary WHERE path_id=?""",
        (parent["id"],),
    ).fetchone()
    scopes = connection.execute(
        """SELECT ss.scope_path_key, ss.status, ss.scan_id,
                  s.started_at_ns, s.refresh_before_ns, s.max_depth
           FROM scan_scope ss
        JOIN scan s ON s.id=ss.scan_id
        WHERE ss.root_id=?
        ORDER BY s.id DESC, ss.id DESC""",
        (root["id"],),
    ).fetchall()

    result: list[dict[str, Any]] = []
    actual_keys: set[str] = set()
    for actual_path in actual_paths:
        normalized = _normalized_display(display_path(actual_path))
        actual_keys.add(normalized)
        indexed = indexed_by_path.get(normalized)
        try:
            observed = _observation(namespace, actual_path, lazy=uses_lazy_metadata(root["filesystem_type"]))
        except OSError as exc:
            raise PathError(f"cannot inspect directory entry {actual_path}: {exc}") from exc
        usage = None if indexed is None else usage_by_id.get(indexed["id"])
        status = _entry_scan_status(
            connection,
            indexed,
            observed.kind,
            path_key(namespace, actual_path),
            scopes,
            usage,
            parent_summary,
            check_subtree=False,
        )
        result.append(_child_record(
            display_path(actual_path), observed.kind, status, observed.logical_size, observed.allocated_size,
            observed.mtime_ns, usage, parent_summary,
        ))

    for indexed in indexed_rows:
        if _normalized_display(indexed["path_display"]) not in actual_keys:
            result.append(_child_record(
                str(indexed["path_display"]), str(indexed["entry_kind"]), GONE, indexed["logical_size"],
                indexed["allocated_size"], indexed["mtime_ns"], usage_by_id.get(indexed["id"]), parent_summary,
            ))
    return sorted(result, key=lambda row: os.path.normcase(os.path.basename(str(row["path"]))))


def _normalized_display(path_display: str) -> str:
    return os.path.normcase(os.path.normpath(path_display))


def _scope_covers(scope: sqlite3.Row, entry_key: bytes, entry_kind: str) -> bool:
    """Whether a scan scope observed ``entry`` (and, for directories, its subtree).

    A shallow scope (``max_depth`` 0) enumerated only its direct entries, so it
    covers direct non-directory children; directory status describes a subtree.
    """
    scope_key = bytes(scope["scope_path_key"])
    if not entry_key.startswith(scope_key):
        return False
    if scope["max_depth"] is None:
        return True
    return entry_kind not in {"root", "directory"} and parent_key(entry_key) == scope_key


def _entry_scan_status(
    connection: sqlite3.Connection,
    indexed: sqlite3.Row | None,
    entry_kind: str,
    entry_key: bytes,
    scopes: list[sqlite3.Row],
    usage: sqlite3.Row | None,
    parent_summary: sqlite3.Row | None,
    *,
    check_subtree: bool = True,
) -> str:
    if (
        indexed is None
        or indexed["state"] != "present"
        or indexed["entry_kind"] != entry_kind
    ):
        return NEW
    covering = [scope for scope in scopes if _scope_covers(scope, entry_key, entry_kind)]
    if not covering:
        return NEW
    latest = covering[0]
    if entry_kind in {"root", "directory"} and int(indexed["first_seen_scan_id"]) > int(latest["scan_id"]):
        # First seen by a later shallow scan that did not enumerate it.
        return NEW
    if latest["status"] == "completed":
        return OK

    refresh_before = latest["refresh_before_ns"]
    if refresh_before is None:
        if entry_kind in {"root", "directory"}:
            completed = (
                usage is not None
                and usage["last_completed_scan_id"] == latest["scan_id"]
                and (
                    not check_subtree
                    or _subtree_completed_in_scan(connection, int(usage["id"]), int(latest["scan_id"]))
                )
            )
        else:
            completed = (
                parent_summary is not None
                and parent_summary["last_completed_scan_id"] == latest["scan_id"]
            )
    else:
        if entry_kind in {"root", "directory"}:
            complete_through = None if usage is None else usage[
                "subtree_complete_through_ns" if check_subtree else "self_completed_at_ns"
            ]
        else:
            complete_through = None if parent_summary is None else parent_summary["self_completed_at_ns"]
        completed = complete_through is not None and int(complete_through) >= int(refresh_before)
    if completed:
        return OK
    if int(indexed["last_seen_scan_id"]) == int(latest["scan_id"]):
        return PART

    previous = covering[1] if len(covering) > 1 else None
    if previous is None:
        return NEW
    return OK if previous["status"] == "completed" else PART


def _subtree_completed_in_scan(connection: sqlite3.Connection, path_id: int, scan_id: int) -> bool:
    incomplete = connection.execute(
        """WITH RECURSIVE directories(id) AS (
            SELECT id FROM path WHERE id=? AND state='present' AND entry_kind IN ('root', 'directory')
            UNION ALL
            SELECT child.id FROM directories parent CROSS JOIN path child ON child.parent_path_id=parent.id
            WHERE child.state='present' AND child.entry_kind='directory'
        )
        SELECT 1 FROM directories d LEFT JOIN directory_summary ds ON ds.path_id=d.id
        WHERE ds.last_completed_scan_id IS NOT ? LIMIT 1""",
        (path_id, scan_id),
    ).fetchone()
    return incomplete is None


def _child_record(
    path: str,
    kind: str,
    status: str,
    logical_size: int | None,
    allocated_size: int | None,
    mtime_ns: int | None,
    usage: sqlite3.Row | None,
    parent_summary: sqlite3.Row | None,
) -> dict[str, Any]:
    """One ``ls`` entry: a directory carries its indexed subtree totals."""
    if kind in {"root", "directory"}:
        return entry_record(
            path=path, kind=kind, status=status,
            logical_size=None if usage is None else usage["logical_size"],
            allocated_size=None if usage is None else usage["allocated_size"],
            files=None if usage is None else usage["files"],
            entries=None if usage is None else usage["entries"],
            mtime_ns=mtime_ns,
            complete_through_ns=None if usage is None else usage["subtree_complete_through_ns"],
            depth=1,
        )
    is_file = kind == "file"
    return entry_record(
        path=path, kind=kind, status=status,
        logical_size=logical_size if is_file else None,
        allocated_size=allocated_size if is_file else None,
        files=1 if is_file else None, entries=1, mtime_ns=mtime_ns,
        complete_through_ns=None if parent_summary is None else parent_summary["self_completed_at_ns"],
        depth=1,
    )


def hardlink_usage(connection: sqlite3.Connection, path_ids: list[int]) -> dict[str, int | None]:
    """Inode-aware usage of the union of indexed subtrees.

    ``unique_*`` counts each ``(device, inode)`` once, as GNU ``du`` does.
    ``freed_*`` counts only inodes whose every link (``nlink``) is a present
    path inside the union, i.e. what deleting the union would release. Files
    without a recorded ``nlink`` are never counted as freed.
    """
    if not path_ids:
        return dict.fromkeys(FREED_FIELDS)
    placeholders = ", ".join("?" for _ in path_ids)
    row = connection.execute(
        f"""WITH RECURSIVE descendants(id) AS (
            SELECT id FROM path WHERE id IN ({placeholders})
            UNION SELECT p.id FROM descendants d CROSS JOIN path p ON p.parent_path_id=d.id
        ), inodes AS (
            SELECT count(*) AS links_in_scope, max(p.nlink) AS nlink,
                   max(p.logical_size) AS logical_size, max(p.allocated_size) AS allocated_size
            FROM descendants d CROSS JOIN path p ON p.id=d.id
            WHERE p.state='present' AND p.entry_kind='file'
            GROUP BY p.root_id, coalesce(p.device, -p.id), coalesce(p.inode, -p.id)
        )
        SELECT coalesce(sum(logical_size), 0) AS unique_logical_size,
               CASE WHEN count(allocated_size) = count(*) THEN coalesce(sum(allocated_size), 0) END
                   AS unique_allocated_size,
               coalesce(sum(CASE WHEN links_in_scope >= nlink THEN logical_size END), 0) AS freed_logical_size,
               CASE WHEN count(allocated_size) = count(*)
                    THEN coalesce(sum(CASE WHEN links_in_scope >= nlink THEN allocated_size END), 0) END
                   AS freed_allocated_size,
               count(CASE WHEN links_in_scope >= nlink THEN 1 END) AS freed_files,
               count(CASE WHEN nlink IS NULL THEN 1 END) AS unknown_link_files
        FROM inodes""",
        path_ids,
    ).fetchone()
    return {key: row[key] for key in FREED_FIELDS}


_SCOPE_COLUMNS = """p.id, p.root_id, p.path_key, p.parent_path_id, p.state,
                  p.last_seen_scan_id, p.first_seen_scan_id, p.path_display, p.entry_kind,
                  p.logical_size, p.allocated_size, p.mtime_ns, r.root_path_display"""


def _descendant_directories(
    connection: sqlite3.Connection, selected: sqlite3.Row, max_depth: int
) -> list[tuple[int, sqlite3.Row]]:
    """Present subdirectories of ``selected`` down to ``max_depth``, in path order."""
    # Drive the final lookup from the subtree even when SQLite can scan path_key in order.
    rows = connection.execute(
        f"""WITH RECURSIVE tree(id, depth) AS (
            SELECT ?, 0
            UNION ALL
            SELECT p.id, t.depth + 1 FROM tree t CROSS JOIN path p ON p.parent_path_id=t.id
            WHERE p.state='present' AND p.entry_kind='directory' AND t.depth < ?
        )
        SELECT t.depth, {_SCOPE_COLUMNS}
        FROM tree t CROSS JOIN path p ON p.id=t.id JOIN root r ON r.id=p.root_id
        WHERE t.depth > 0 ORDER BY p.path_key""",
        (selected["id"], max_depth),
    ).fetchall()
    return [(int(row["depth"]), row) for row in rows]


def _scan_scopes(connection: sqlite3.Connection, root_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT ss.scope_path_key, ss.status, ss.scan_id,
                  s.started_at_ns, s.refresh_before_ns, s.max_depth
           FROM scan_scope ss JOIN scan s ON s.id=ss.scan_id
           WHERE ss.root_id=? ORDER BY s.id DESC, ss.id DESC""",
        (root_id,),
    ).fetchall()


def _indexed_entry(
    connection: sqlite3.Connection,
    selected: sqlite3.Row,
    scopes: list[sqlite3.Row],
    *,
    depth: int,
    freed: bool = False,
) -> dict[str, Any]:
    """The entry record of one indexed path; directories use their cached subtree totals."""
    kind = str(selected["entry_kind"])
    usage = None
    if kind in {"root", "directory"}:
        usage = connection.execute(
            """SELECT subtree_files AS files, subtree_entries AS entries,
                      subtree_logical_size AS logical_size,
                      subtree_allocated_size AS allocated_size, self_completed_at_ns,
                      subtree_complete_through_ns, last_completed_scan_id, path_id AS id
               FROM directory_summary WHERE path_id=?""",
            (selected["id"],),
        ).fetchone()
        if usage is None:
            raise FlatlasError(f"directory summary is missing: {selected['path_display']}")
    parent_summary = connection.execute(
        """SELECT self_completed_at_ns, last_completed_scan_id
           FROM directory_summary WHERE path_id=?""",
        (selected["parent_path_id"],),
    ).fetchone() if selected["parent_path_id"] is not None else None
    status = _entry_scan_status(connection, selected, kind, bytes(selected["path_key"]), scopes, usage, parent_summary)
    if usage is not None:
        files, entries = usage["files"], usage["entries"]
        logical, allocated = usage["logical_size"], usage["allocated_size"]
        complete_through = usage["subtree_complete_through_ns"]
    else:
        # Sizes only count regular-file content and special entries record none;
        # report unknown rather than 0. The entry itself is one inode.
        is_file = kind == "file"
        files, entries = (1 if is_file else None), 1
        logical = selected["logical_size"] if is_file else None
        allocated = selected["allocated_size"] if is_file else None
        complete_through = None if parent_summary is None else parent_summary["self_completed_at_ns"]
    record = entry_record(
        path=str(selected["path_display"]), kind=kind, status=status,
        logical_size=logical, allocated_size=allocated, files=files, entries=entries,
        mtime_ns=selected["mtime_ns"], complete_through_ns=complete_through, depth=depth,
    )
    if freed:
        indexed_kind = kind in {"file", "root", "directory"}
        record.update(hardlink_usage(connection, [selected["id"]]) if indexed_kind else dict.fromkeys(FREED_FIELDS))
    return record


def disk_usage(
    connection: sqlite3.Connection,
    *,
    scope: Path | str | None = None,
    follow_symlink: bool = False,
    freed: bool = False,
    max_depth: int = 0,
) -> list[dict[str, Any]]:
    """Entry records of one scope, then its subdirectories down to ``max_depth``."""
    scope = Path(".") if scope is None else scope
    try:
        selected = _scope_row(connection, scope, follow=follow_symlink)
    except FlatlasError:
        selected_path = scope_path(scope, follow=follow_symlink)
        if not selected_path.is_dir() or selected_path.is_symlink() or os.path.isjunction(selected_path):
            raise
        namespace = discover_namespace(selected_path)
        root = get_root(connection, namespace)
        if root is None:
            raise FlatlasError(f"filesystem is not registered; run 'flatlas init {scope}' first") from None
        record = entry_record(
            path=display_path(selected_path), kind="directory", status=NEW, logical_size=None,
            allocated_size=None, files=None, entries=None, mtime_ns=None, complete_through_ns=None, depth=0,
        )
        return [{**record, **(dict.fromkeys(FREED_FIELDS) if freed else {})}]
    scopes = _scan_scopes(connection, int(selected["root_id"]))
    targets = [(0, selected)]
    if max_depth > 0 and selected["entry_kind"] in {"root", "directory"}:
        targets.extend(_descendant_directories(connection, selected, max_depth))
    return [_indexed_entry(connection, target, scopes, depth=depth, freed=freed) for depth, target in targets]


def indexed_scope_ids(
    connection: sqlite3.Connection, scopes: list[Path | str], *, follow_symlink: bool = False
) -> list[int]:
    """Path ids of the scopes that are currently indexed; others are skipped."""
    ids = []
    for scope in scopes:
        try:
            ids.append(int(_scope_row(connection, scope, follow=follow_symlink)["id"]))
        except FlatlasError:
            continue
    return ids


def largest_files(
    connection: sqlite3.Connection,
    *,
    scope: Path = Path("."),
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Entry records of the largest present files under ``scope``, largest first."""
    selected = _scope_row(connection, scope)
    key = bytes(selected["path_key"])
    upper = key[:-1] + b"\x01" if key else None
    rows = connection.execute(
        f"""SELECT {_SCOPE_COLUMNS}
        FROM path p JOIN root r ON r.id=p.root_id
        WHERE p.root_id=? AND p.path_key>=? {'' if upper is None else 'AND p.path_key<?'}
          AND p.state='present' AND p.entry_kind='file'
        ORDER BY p.logical_size DESC, p.path_display LIMIT ?""",
        (selected["root_id"], key, *(() if upper is None else (upper,)), -1 if limit is None else limit),
    ).fetchall()
    scopes = _scan_scopes(connection, int(selected["root_id"]))
    base = key.count(b"\0")
    return [
        _indexed_entry(connection, row, scopes, depth=bytes(row["path_key"]).count(b"\0") - base)
        for row in rows
    ]


def duplicate_groups(
    connection: sqlite3.Connection,
    *,
    root_id: int | None = None,
    scope: Path | None = None,
    filters: DuplicateFilters | None = None,
) -> list[dict[str, Any]]:
    filters = filters or DuplicateFilters()
    if root_id is not None and scope is not None:
        raise ValueError("root_id and scope cannot be combined")
    if scope is not None:
        selected = _scope_row(connection, scope)
        if selected["entry_kind"] not in {"root", "directory"}:
            raise FlatlasError(f"duplicate scope is not a directory: {selected['path_display']}")
        rows = connection.execute(
            """WITH RECURSIVE descendants(id) AS (
                SELECT id FROM path WHERE id=? UNION ALL
                SELECT p.id FROM descendants d CROSS JOIN path p ON p.parent_path_id=d.id
            ) SELECT h.full_algorithm, h.full_digest, p.logical_size, p.id, p.root_id, p.path_display,
                   p.device, p.inode, p.nlink
            FROM file_hash h JOIN path p ON p.id=h.path_id
            WHERE h.state='full_ready' AND p.state='present' AND p.entry_kind='file'
              AND p.logical_size > 0
              AND (p.allocated_size IS NULL OR p.allocated_size > 0)
              AND p.id IN descendants
              AND p.logical_size BETWEEN ? AND ?
            ORDER BY h.full_algorithm, h.full_digest, p.path_display""",
            (selected["id"], *filters.size_bounds),
        ).fetchall()
    else:
        where = "AND p.root_id=?" if root_id is not None else ""
        rows = connection.execute(
            f"""SELECT h.full_algorithm, h.full_digest, p.logical_size, p.id, p.root_id, p.path_display,
                   p.device, p.inode, p.nlink
            FROM file_hash h JOIN path p ON p.id=h.path_id
            WHERE h.state='full_ready' AND p.state='present' AND p.entry_kind='file'
              AND p.logical_size > 0 {where}
              AND (p.allocated_size IS NULL OR p.allocated_size > 0)
              AND p.logical_size BETWEEN ? AND ?
            ORDER BY h.full_algorithm, h.full_digest, p.path_display""",
            (*(() if root_id is None else (root_id,)), *filters.size_bounds),
        ).fetchall()
    grouped: dict[tuple[str, bytes, int], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[(row["full_algorithm"], row["full_digest"], row["logical_size"])].append(row)
    result = []
    for (algorithm, digest, size), members in grouped.items():
        # Hardlink aliases are one copy: count distinct (device, inode) objects.
        inodes: dict[tuple[Any, ...], list[sqlite3.Row]] = defaultdict(list)
        for row in members:
            identity = (row["root_id"], row["device"], row["inode"]) if row["inode"] is not None else ("path", row["id"])
            inodes[identity].append(row)
        copies = len(inodes)
        if copies >= filters.min_count and size * (copies - 1) >= filters.min_savings:
            result.append({
                "algorithm": algorithm,
                "digest": digest.hex(),
                "logical_size": size,
                "count": len(members),
                "inodes": copies,
                "theoretical_savings": size * (copies - 1),
                # Some inode has links the group does not list (elsewhere or outside the index).
                "external_links": any(
                    (aliases[0]["nlink"] or 1) > len(aliases) for aliases in inodes.values()
                ),
                "paths": [
                    {"id": row["id"], "root_id": row["root_id"], "path_display": row["path_display"],
                     "device": row["device"], "inode": row["inode"]}
                    for row in members
                ],
            })
    return sorted(
        result,
        key=lambda group: (
            -group["theoretical_savings"],
            -group["logical_size"],
            group["algorithm"],
            group["digest"],
        ),
    )[:filters.limit]


@dataclass(frozen=True)
class FindHit:
    """One print action of a ``find`` expression."""

    path: str  # as GNU find prints it: the start point as given plus relative components
    terminator: str
    record: dict[str, Any] | None = None  # the entry record, when requested


_FIND_COLUMNS = "p.id, p.path_key, p.name_raw, p.entry_kind, p.mode, p.state"
_FIND_METADATA_COLUMNS = f"""{_FIND_COLUMNS}, p.parent_path_id, p.path_display, p.logical_size,
    p.allocated_size, p.mtime_ns, p.first_seen_scan_id, p.last_seen_scan_id,
    ds.path_id AS summary_id, ds.subtree_files, ds.subtree_entries, ds.subtree_logical_size,
    ds.subtree_allocated_size, ds.self_completed_at_ns, ds.subtree_complete_through_ns,
    ds.last_completed_scan_id"""
_PARENT_CACHE_SIZE = 4096


def find_paths(
    connection: sqlite3.Connection,
    start: str,
    expression: FindExpression,
    *,
    follow_symlink: bool = False,
    records: bool = False,
) -> Iterator[FindHit]:
    """Evaluate a GNU ``find`` expression over the present indexed tree under ``start``.

    Yields a :class:`FindHit` for each print action, in GNU order: a directory
    before its entries. Printed paths are ``start`` as given plus the relative
    components, like GNU find. Only the index is read; ``start`` must be
    indexed and present. A final symlink in ``start`` is examined itself unless
    ``follow_symlink`` or a trailing ``/``. With ``records`` each hit carries
    the entry record (see :mod:`flatlas.records`); a directory's status, as in
    ``ls``, describes whether the directory itself was completely enumerated.
    """
    selected = _scope_row(connection, start, follow=follow_symlink)
    root_id = int(selected["root_id"])
    start_key = bytes(selected["path_key"])
    start_parts = start_key.count(b"\0")
    prefix = start if start.endswith("/") else start + "/"
    max_depth = expression.max_depth
    metadata = records or expression.needs_metadata
    columns = _FIND_METADATA_COLUMNS if metadata else _FIND_COLUMNS
    source = "path p LEFT JOIN directory_summary ds ON ds.path_id=p.id" if metadata else "path p"
    scopes = _scan_scopes(connection, root_id) if metadata else []
    parents: OrderedDict[int, sqlite3.Row | None] = OrderedDict()

    def parent_summary(path_id: int | None) -> sqlite3.Row | None:
        # Rows arrive in path order, so a parent was usually just seen; the
        # cache keeps the recently used ones and falls back to a lookup.
        if path_id is None:
            return None
        if path_id in parents:
            parents.move_to_end(path_id)
            return parents[path_id]
        found = connection.execute(
            "SELECT self_completed_at_ns, last_completed_scan_id FROM directory_summary WHERE path_id=?", (path_id,)
        ).fetchone()
        remember(path_id, found)
        return found

    def remember(path_id: int, summary: sqlite3.Row | None) -> None:
        parents[path_id] = summary
        if len(parents) > _PARENT_CACHE_SIZE:
            parents.popitem(last=False)

    def visit(row: sqlite3.Row, path: str, name: str, depth: int) -> tuple[FindContext, list[FindHit]]:
        kind = str(row["entry_kind"])
        context = FindContext()
        if not metadata:
            expression.evaluate(FindEntry(path, name, kind, row["mode"], depth), context)
            return context, [FindHit(printed, terminator) for printed, terminator in context.output]
        directory = kind in {"root", "directory"}
        summary = row if row["summary_id"] is not None else None
        if directory:
            remember(int(row["id"]), summary)
        status: list[str] = []

        def status_of() -> str:
            if not status:
                status.append(_entry_scan_status(
                    connection, row, kind, bytes(row["path_key"]), scopes, summary,
                    parent_summary(row["parent_path_id"]), check_subtree=False,
                ))
            return status[0]

        is_file = kind == "file"
        if directory:
            size = None if summary is None else row["subtree_logical_size"]
            entries = None if summary is None else row["subtree_entries"]
        else:
            size, entries = (row["logical_size"] if is_file else None), 1
        expression.evaluate(
            FindEntry(path, name, kind, row["mode"], depth, size=size, entries=entries,
                      mtime_ns=row["mtime_ns"], status=status_of),
            context,
        )
        if not context.output:
            return context, []
        record = None
        if records:
            if directory:
                files = None if summary is None else row["subtree_files"]
                allocated = None if summary is None else row["subtree_allocated_size"]
                complete = None if summary is None else row["subtree_complete_through_ns"]
            else:
                files = 1 if is_file else None
                allocated = row["allocated_size"] if is_file else None
                parent = parent_summary(row["parent_path_id"])
                complete = None if parent is None else parent["self_completed_at_ns"]
            record = entry_record(
                path=str(row["path_display"]), kind=kind, status=status_of(), logical_size=size,
                allocated_size=allocated, files=files, entries=entries, mtime_ns=row["mtime_ns"],
                complete_through_ns=complete, depth=depth,
            )
        return context, [FindHit(printed, terminator, record) for printed, terminator in context.output]

    start_row = connection.execute(
        f"SELECT {columns} FROM {source} WHERE p.id=?", (int(selected["id"]),)
    ).fetchone()
    context = FindContext()
    if expression.min_depth == 0:
        context, hits = visit(start_row, start, os.path.basename(start.rstrip("/")) or start, 0)
        yield from hits
    if context.prune or max_depth == 0:
        return

    # Path keys are NUL-terminated components, so a subtree is one contiguous
    # range of the (root_id, path_key) index, already in GNU preorder. Walking
    # that range needs no recursive CTE or sort, and a pruned subtree, one past
    # -maxdepth, or one under a deleted directory is skipped by restarting the
    # range after it instead of reading its rows.
    upper = _subtree_upper_key(start_key)
    lower = start_key
    while True:
        rows = connection.execute(
            f"""SELECT {columns} FROM {source}
                WHERE p.root_id=? AND p.path_key>? {'' if upper is None else 'AND p.path_key<?'}
                ORDER BY p.path_key""",
            (root_id, lower) if upper is None else (root_id, lower, upper),
        )
        skip_to: bytes | None = None
        leaf_skip: bytes | None = None
        for row in rows:
            key = bytes(row["path_key"])
            if leaf_skip is not None and key.startswith(leaf_skip):
                continue
            leaf_skip = None
            is_directory = row["entry_kind"] in {"root", "directory"}
            if row["state"] != "present":
                # The former walk descended through present rows only.
                if is_directory:
                    skip_to = key
                    break
                leaf_skip = key
                continue
            depth = key.count(b"\0") - start_parts
            context = FindContext()
            if depth >= expression.min_depth:
                # Names never contain "/" or NUL, so one decode of the joined
                # suffix equals decoding and joining each component.
                path = prefix + os.fsdecode(key[len(start_key):-1].replace(b"\0", b"/"))
                context, hits = visit(row, path, os.fsdecode(bytes(row["name_raw"])), depth)
                yield from hits
            if context.prune or (max_depth is not None and depth >= max_depth):
                if is_directory:
                    skip_to = key
                    break
                leaf_skip = key
        rows.close()
        if skip_to is None:
            return
        # A key ending in 0x01 is never stored (keys end in NUL), so ">" resumes right after the subtree.
        lower = skip_to[:-1] + b"\x01"


def _subtree_upper_key(key: bytes) -> bytes | None:
    """Exclusive upper bound of the keys strictly under ``key`` (None for a namespace root)."""
    return key[:-1] + b"\x01" if key else None
