"""Filesystem namespace and persistent path identity helpers."""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flatlas.db import _last_insert_id, now_ns
from flatlas.errors import FlatlasError
from flatlas.lazystat import filesystem_type


@dataclass(frozen=True)
class Namespace:
    path: Path
    platform: str
    kind: str
    identity: bytes


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
    """Register a namespace, recording its filesystem type.

    Re-registering refreshes ``filesystem_type`` so roots created before the
    type was recorded (or whose mount changed) pick up the current value; the
    scanner reads it to decide whether to use lazy metadata.
    """
    fs_type = filesystem_type(namespace.path) if namespace.kind == "posix_mount" else None
    existing = get_root(connection, namespace)
    if existing is not None:
        if fs_type is not None and existing["filesystem_type"] != fs_type:
            connection.execute(
                "UPDATE root SET filesystem_type=?, updated_at_ns=? WHERE id=?",
                (fs_type, now_ns(), existing["id"]),
            )
            connection.commit()
        return int(existing["id"])
    timestamp = now_ns()
    cursor = connection.execute(
        """INSERT INTO root(root_path_raw, root_path_display, platform, namespace_kind,
           namespace_id_raw, filesystem_type, case_sensitive, created_at_ns, updated_at_ns)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            namespace.identity, str(namespace.path), namespace.platform, namespace.kind,
            namespace.identity, fs_type, 0 if os.name == "nt" else 1, timestamp, timestamp,
        ),
    )
    connection.commit()
    return _last_insert_id(cursor)


def _sqlite_integer(value: int | None) -> int | None:
    """Map platform unsigned identifiers into SQLite's signed 64-bit range."""
    if value is None:
        return None
    if value > 2**63 - 1:
        return value - 2**64
    return value


def _scope_row(connection: sqlite3.Connection, value: Path) -> sqlite3.Row:
    display = str(value.expanduser().resolve())
    row = connection.execute(
        """SELECT p.id, p.root_id, p.path_key, p.parent_path_id, p.state,
                  p.last_seen_scan_id, p.path_display, p.entry_kind,
                  p.logical_size, p.allocated_size,
                  r.root_path_display
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
        "SELECT id, root_id, parent_path_id FROM path WHERE path_display=?",
        (display,),
    ).fetchone()
    if selected is None:
        raise FlatlasError(f"path is not indexed: {display}")

    path_id = int(selected["id"])
    root_id = int(selected["root_id"])
    parent_path_id = selected["parent_path_id"]
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
        if parent_path_id is not None:
            from flatlas.summaries import refresh_directory_summaries

            refresh_directory_summaries(connection, root_id, int(parent_path_id))
    return {
        "path": display,
        "paths_removed": removed_paths,
        "plans_removed": len(plan_ids),
    }
