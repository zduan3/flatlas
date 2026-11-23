"""Read-only filesystem index and live-view queries."""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from flatlas.duplicate_filters import DuplicateFilters
from flatlas.errors import FlatlasError
from flatlas.lazystat import uses_lazy_metadata
from flatlas.mounts import mount_entry
from flatlas.paths import (
    _scope_row,
    discover_namespace,
    display_path,
    get_root,
    path_key,
    scope_path,
)
from flatlas.scanning import Observation, _observation


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
            "id": row["id"],
            "root_path_display": row["root_path_display"],
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
        "filesystem": str(mount_point) if entry is None else entry.source,
        "type": None if entry is None else entry.fstype,
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


def list_scans(
    connection: sqlite3.Connection,
    *,
    scope: Path | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    parameters: list[Any] = []
    where = ""
    if scope is not None:
        selected_path = scope.expanduser().resolve()
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
    parameters.append(limit)
    rows = connection.execute(
        f"""SELECT s.id AS scan_id, s.root_id, r.root_path_display AS root,
                   s.kind, s.status, s.started_at_ns, s.finished_at_ns,
                   CASE WHEN s.finished_at_ns IS NULL THEN NULL
                        ELSE s.finished_at_ns-s.started_at_ns END AS duration_ns,
                   s.files_seen, s.dirs_seen, s.errors_seen,
                   s.refresh_before_ns, s.scanner_version, s.note,
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
                  s.files_seen, s.dirs_seen, s.errors_seen,
                  s.refresh_before_ns, s.scanner_version, s.note
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
    return {
        **dict(scan),
        "scopes": [dict(row) for row in scopes],
        "errors": [dict(row) for row in errors],
    }


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
            actual_paths = [Path(entry.path) for entry in entries]
    except OSError as exc:
        raise FlatlasError(f"cannot list directory {selected_path}: {exc}") from exc

    parent = connection.execute(
        "SELECT id FROM path WHERE root_id=? AND path_key=?",
        (root["id"], path_key(namespace, selected_path)),
    ).fetchone()
    indexed_rows = [] if parent is None else connection.execute(
        """SELECT id, path_key, path_display, entry_kind, state, logical_size, allocated_size,
                  last_seen_scan_id
        FROM path WHERE parent_path_id=? AND state='present'""",
        (parent["id"],),
    ).fetchall()
    indexed_by_path = {_normalized_display(row["path_display"]): row for row in indexed_rows}

    usage_rows = [] if parent is None else connection.execute(
        """SELECT p.id, ds.subtree_files AS files,
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
                  s.started_at_ns, s.refresh_before_ns
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
            raise FlatlasError(f"cannot inspect directory entry {actual_path}: {exc}") from exc
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
        result.append(_live_child_status_row(observed, status, usage))

    for indexed in indexed_rows:
        if _normalized_display(indexed["path_display"]) not in actual_keys:
            result.append(_gone_child_status_row(indexed, usage_by_id.get(indexed["id"])))
    return sorted(result, key=lambda row: os.path.normcase(str(row["name"])))


def _normalized_display(path_display: str) -> str:
    return os.path.normcase(os.path.normpath(path_display))


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
        return "unscanned"
    covering = [scope for scope in scopes if entry_key.startswith(scope["scope_path_key"])]
    if not covering:
        return "unscanned"
    latest = covering[0]
    if latest["status"] == "completed":
        return "scanned"

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
        return "scanned"
    if int(indexed["last_seen_scan_id"]) == int(latest["scan_id"]):
        return "incomplete"

    previous = covering[1] if len(covering) > 1 else None
    if previous is None:
        return "unscanned"
    return "scanned" if previous["status"] == "completed" else "incomplete"


def _subtree_completed_in_scan(connection: sqlite3.Connection, path_id: int, scan_id: int) -> bool:
    incomplete = connection.execute(
        """WITH RECURSIVE directories(id) AS (
            SELECT id FROM path WHERE id=? AND state='present' AND entry_kind IN ('root', 'directory')
            UNION ALL
            SELECT child.id FROM path child JOIN directories parent ON child.parent_path_id=parent.id
            WHERE child.state='present' AND child.entry_kind='directory'
        )
        SELECT 1 FROM directories d LEFT JOIN directory_summary ds ON ds.path_id=d.id
        WHERE ds.last_completed_scan_id IS NOT ? LIMIT 1""",
        (path_id, scan_id),
    ).fetchone()
    return incomplete is None


def _directory_status_row(
    path: Path,
    status: str,
    usage: sqlite3.Row | None,
) -> dict[str, Any]:
    return {
        "entry_kind": "directory",
        "status": status,
        "files": None if usage is None else usage["files"],
        "logical_size": None if usage is None else usage["logical_size"],
        "allocated_size": None if usage is None else usage["allocated_size"],
        "subtree_complete_through_ns": None if usage is None else usage["subtree_complete_through_ns"],
        "name": path.name,
    }


def _live_child_status_row(
    observed: Observation,
    status: str,
    usage: sqlite3.Row | None,
) -> dict[str, Any]:
    if observed.kind == "directory":
        return _directory_status_row(observed.path, status, usage)
    if observed.kind == "file":
        return {
            "entry_kind": "file", "status": status, "files": 1,
            "logical_size": observed.logical_size, "allocated_size": observed.allocated_size,
            "subtree_complete_through_ns": None,
            "name": observed.path.name,
        }
    return {
        "entry_kind": observed.kind, "status": status, "files": None,
        "logical_size": None, "allocated_size": None, "name": observed.path.name,
        "subtree_complete_through_ns": None,
    }


def _gone_child_status_row(indexed: sqlite3.Row, usage: sqlite3.Row | None) -> dict[str, Any]:
    kind = str(indexed["entry_kind"])
    if kind == "directory":
        return _directory_status_row(Path(indexed["path_display"]), "missing", usage)
    if kind == "file":
        return {
            "entry_kind": "file", "status": "missing", "files": 1,
            "logical_size": indexed["logical_size"], "allocated_size": indexed["allocated_size"],
            "subtree_complete_through_ns": None,
            "name": Path(indexed["path_display"]).name,
        }
    return {
        "entry_kind": kind, "status": "missing", "files": None,
        "logical_size": None, "allocated_size": None, "name": Path(indexed["path_display"]).name,
        "subtree_complete_through_ns": None,
    }


_FREED_FIELDS = (
    "unique_logical_size", "unique_allocated_size",
    "freed_logical_size", "freed_allocated_size", "freed_files", "unknown_link_files",
)


def hardlink_usage(connection: sqlite3.Connection, path_ids: list[int]) -> dict[str, int | None]:
    """Inode-aware usage of the union of indexed subtrees.

    ``unique_*`` counts each ``(device, inode)`` once, as GNU ``du`` does.
    ``freed_*`` counts only inodes whose every link (``nlink``) is a present
    path inside the union, i.e. what deleting the union would release. Files
    without a recorded ``nlink`` are never counted as freed.
    """
    if not path_ids:
        return dict.fromkeys(_FREED_FIELDS)
    placeholders = ", ".join("?" for _ in path_ids)
    row = connection.execute(
        f"""WITH RECURSIVE descendants(id) AS (
            SELECT id FROM path WHERE id IN ({placeholders})
            UNION SELECT p.id FROM path p JOIN descendants d ON p.parent_path_id=d.id
        ), inodes AS (
            SELECT count(*) AS links_in_scope, max(p.nlink) AS nlink,
                   max(p.logical_size) AS logical_size, max(p.allocated_size) AS allocated_size
            FROM path p JOIN descendants d ON p.id=d.id
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
    return {key: row[key] for key in _FREED_FIELDS}


def disk_usage(
    connection: sqlite3.Connection,
    *,
    scope: Path | str | None = None,
    relative_to: Path | None = None,
    follow_symlink: bool = False,
    freed: bool = False,
) -> list[dict[str, Any]]:
    display_base = Path.cwd() if relative_to is None else relative_to
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
        return [{
            "files": None,
            "logical_size": None,
            "allocated_size": None,
            "status": "unscanned",
            **(dict.fromkeys(_FREED_FIELDS) if freed else {}),
            "path": _relative_display(str(selected_path), display_base, str(namespace.path)),
        }]
    row: dict[str, Any]
    if selected["entry_kind"] == "file":
        row = {
            "files": 1,
            "logical_size": selected["logical_size"],
            "allocated_size": selected["allocated_size"],
        }
        usage = None
    elif selected["entry_kind"] in {"root", "directory"}:
        usage = connection.execute(
            """SELECT subtree_files AS files, subtree_logical_size AS logical_size,
                      subtree_allocated_size AS allocated_size, self_completed_at_ns,
                      subtree_complete_through_ns, last_completed_scan_id, path_id AS id
               FROM directory_summary WHERE path_id=?""",
            (selected["id"],),
        ).fetchone()
        if usage is None:
            raise FlatlasError(f"directory summary is missing: {selected['path_display']}")
        row = {
            "files": usage["files"],
            "logical_size": usage["logical_size"],
            "allocated_size": usage["allocated_size"],
        }
    else:
        # Sizes only count regular-file content and special entries record none;
        # report unknown rather than 0, matching ls.
        row = {"files": None, "logical_size": None, "allocated_size": None}
        usage = None
    parent_summary = connection.execute(
        """SELECT self_completed_at_ns, last_completed_scan_id
           FROM directory_summary WHERE path_id=?""",
        (selected["parent_path_id"],),
    ).fetchone() if selected["parent_path_id"] is not None else None
    scopes = connection.execute(
        """SELECT ss.scope_path_key, ss.status, ss.scan_id,
                  s.started_at_ns, s.refresh_before_ns
           FROM scan_scope ss JOIN scan s ON s.id=ss.scan_id
           WHERE ss.root_id=? ORDER BY s.id DESC, ss.id DESC""",
        (selected["root_id"],),
    ).fetchall()
    status = _entry_scan_status(
        connection, selected, str(selected["entry_kind"]),
        bytes(selected["path_key"]), scopes, usage, parent_summary,
    )
    if freed:
        indexed_kind = selected["entry_kind"] in {"file", "root", "directory"}
        row.update(hardlink_usage(connection, [selected["id"]]) if indexed_kind else dict.fromkeys(_FREED_FIELDS))
    return [
        {
            **dict(row),
            "status": status,
            "path": _relative_display(selected["path_display"], display_base, selected["root_path_display"]),
        }
    ]


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
                SELECT p.id FROM path p JOIN descendants d ON p.parent_path_id=d.id
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
