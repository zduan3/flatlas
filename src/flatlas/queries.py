"""Read-only filesystem index and live-view queries."""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from flatlas.errors import FlatlasError
from flatlas.paths import _scope_row, discover_namespace, get_root, path_key
from flatlas.scanning import Observation, _observation


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
            (str(selected_path),),
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
        normalized = _normalized_display(str(actual_path))
        actual_keys.add(normalized)
        indexed = indexed_by_path.get(normalized)
        try:
            observed = _observation(namespace, actual_path)
        except OSError as exc:
            raise FlatlasError(f"cannot inspect directory entry {actual_path}: {exc}") from exc
        usage = None if indexed is None else usage_by_id.get(indexed["id"])
        status = _entry_scan_status(
            indexed,
            observed.kind,
            path_key(namespace, actual_path),
            scopes,
            usage,
            parent_summary,
        )
        result.append(_live_child_status_row(observed, status, usage))

    for indexed in indexed_rows:
        if _normalized_display(indexed["path_display"]) not in actual_keys:
            result.append(_gone_child_status_row(indexed, usage_by_id.get(indexed["id"])))
    return sorted(result, key=lambda row: os.path.normcase(str(row["name"])))


def _normalized_display(path_display: str) -> str:
    return os.path.normcase(os.path.normpath(path_display))


def _entry_scan_status(
    indexed: sqlite3.Row | None,
    entry_kind: str,
    entry_key: bytes,
    scopes: list[sqlite3.Row],
    usage: sqlite3.Row | None,
    parent_summary: sqlite3.Row | None,
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
    cutoff = int(latest["started_at_ns"] if refresh_before is None else refresh_before)
    if entry_kind == "directory":
        complete_through = None if usage is None else usage["subtree_complete_through_ns"]
    else:
        complete_through = None if parent_summary is None else parent_summary["self_completed_at_ns"]
    if complete_through is not None and int(complete_through) >= cutoff:
        return "scanned"
    if int(indexed["last_seen_scan_id"]) == int(latest["scan_id"]):
        return "incomplete"

    previous = covering[1] if len(covering) > 1 else None
    if previous is None:
        return "unscanned"
    return "scanned" if previous["status"] == "completed" else "incomplete"


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
            "name": observed.path.name,
        }
    return {
        "entry_kind": observed.kind, "status": status, "files": None,
        "logical_size": None, "allocated_size": None, "name": observed.path.name,
    }


def _gone_child_status_row(indexed: sqlite3.Row, usage: sqlite3.Row | None) -> dict[str, Any]:
    kind = str(indexed["entry_kind"])
    if kind == "directory":
        return _directory_status_row(Path(indexed["path_display"]), "missing", usage)
    if kind == "file":
        return {
            "entry_kind": "file", "status": "missing", "files": 1,
            "logical_size": indexed["logical_size"], "allocated_size": indexed["allocated_size"],
            "name": Path(indexed["path_display"]).name,
        }
    return {
        "entry_kind": kind, "status": "missing", "files": None,
        "logical_size": None, "allocated_size": None, "name": Path(indexed["path_display"]).name,
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
            """SELECT r.id AS root_id, r.root_path_display,
                      CASE WHEN p.id IS NULL THEN 0 ELSE ds.subtree_files END AS files,
                      CASE WHEN p.id IS NULL THEN 0 ELSE ds.subtree_logical_size END AS logical_size,
                      CASE WHEN p.id IS NULL THEN 0 ELSE ds.subtree_allocated_size END AS allocated_size
               FROM root r
               LEFT JOIN path p ON p.root_id=r.id AND p.path_key=x'' AND p.state='present'
               LEFT JOIN directory_summary ds ON ds.path_id=p.id
               ORDER BY r.root_path_display"""
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
    if selected["entry_kind"] == "file":
        row = {
            "files": 1,
            "logical_size": selected["logical_size"],
            "allocated_size": selected["allocated_size"],
        }
    elif selected["entry_kind"] in {"root", "directory"}:
        summary = connection.execute(
            """SELECT subtree_files AS files, subtree_logical_size AS logical_size,
                      subtree_allocated_size AS allocated_size
               FROM directory_summary WHERE path_id=?""",
            (selected["id"],),
        ).fetchone()
        if summary is None:
            raise FlatlasError(f"directory summary is missing: {selected['path_display']}")
        row = dict(summary)
    else:
        row = {"files": 0, "logical_size": 0, "allocated_size": 0}
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
