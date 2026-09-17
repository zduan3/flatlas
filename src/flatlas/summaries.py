"""Persistent directory usage summaries and scan freshness."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping


def _directory_rows(
    connection: sqlite3.Connection,
    root_id: int,
    scope_path_id: int,
) -> list[sqlite3.Row]:
    return connection.execute(
        """WITH RECURSIVE
        descendants(id) AS (
            SELECT id FROM path WHERE id=?
            UNION ALL
            SELECT p.id FROM path p JOIN descendants d ON p.parent_path_id=d.id
            WHERE p.state='present' AND p.entry_kind='directory'
        ),
        ancestors(id) AS (
            SELECT id FROM path WHERE id=?
            UNION ALL
            SELECT p.parent_path_id FROM path p JOIN ancestors a ON p.id=a.id
            WHERE p.parent_path_id IS NOT NULL
        )
        SELECT DISTINCT p.id, p.parent_path_id, p.path_key
        FROM path p
        WHERE p.root_id=? AND p.state='present'
          AND p.entry_kind IN ('root', 'directory')
          AND (p.id IN descendants OR p.id IN ancestors)""",
        (scope_path_id, scope_path_id, root_id),
    ).fetchall()


def refresh_directory_summaries(
    connection: sqlite3.Connection,
    root_id: int,
    scope_path_id: int,
    *,
    completed_directories: Mapping[int, tuple[int, int]] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    """Refresh cached totals for one subtree and every affected ancestor.

    ``completed_directories`` maps path id to ``(completed_at_ns, scan_id)``.
    A directory's completion timestamp advances only after all of its direct
    entries were enumerated and inspected successfully. Failed attempts leave
    the previous successful timestamp unchanged and remain visible through the
    scan audit tables.
    """

    completed = {} if completed_directories is None else completed_directories
    rows = sorted(
        _directory_rows(connection, root_id, scope_path_id),
        key=lambda row: bytes(row["path_key"]).count(b"\0"),
        reverse=True,
    )
    total = len(rows)
    if on_progress is not None:
        on_progress(0, total)
    updated_at = time.time_ns()
    for completed_count, directory in enumerate(rows, start=1):
        path_id = int(directory["id"])
        direct = connection.execute(
            """SELECT count(*) AS files,
                      COALESCE(sum(logical_size), 0) AS logical_size,
                      CASE WHEN count(*)=count(allocated_size)
                           THEN COALESCE(sum(allocated_size), 0)
                           ELSE NULL END AS allocated_size
               FROM path
               WHERE parent_path_id=? AND state='present' AND entry_kind='file'""",
            (path_id,),
        ).fetchone()
        children = connection.execute(
            """SELECT ds.subtree_files, ds.subtree_logical_size,
                      ds.subtree_allocated_size, ds.subtree_complete_through_ns
               FROM path p LEFT JOIN directory_summary ds ON ds.path_id=p.id
               WHERE p.parent_path_id=? AND p.state='present' AND p.entry_kind='directory'""",
            (path_id,),
        ).fetchall()
        existing = connection.execute(
            """SELECT self_completed_at_ns, last_completed_scan_id
               FROM directory_summary WHERE path_id=?""",
            (path_id,),
        ).fetchone()

        if path_id in completed:
            self_completed_at, last_completed_scan_id = completed[path_id]
        elif existing is None:
            self_completed_at = last_completed_scan_id = None
        else:
            self_completed_at = existing["self_completed_at_ns"]
            last_completed_scan_id = existing["last_completed_scan_id"]

        direct_files = int(direct["files"])
        direct_logical = int(direct["logical_size"])
        direct_allocated = direct["allocated_size"]
        subtree_files = direct_files + sum(int(child["subtree_files"] or 0) for child in children)
        subtree_logical = direct_logical + sum(int(child["subtree_logical_size"] or 0) for child in children)

        child_allocated = [child["subtree_allocated_size"] for child in children]
        if direct_allocated is None or any(value is None for value in child_allocated):
            subtree_allocated = None
        else:
            subtree_allocated = int(direct_allocated) + sum(int(value) for value in child_allocated)

        child_completed = [child["subtree_complete_through_ns"] for child in children]
        if self_completed_at is None or any(value is None for value in child_completed):
            subtree_complete_through = None
        else:
            subtree_complete_through = min(
                [int(self_completed_at), *(int(value) for value in child_completed)]
            )

        connection.execute(
            """INSERT INTO directory_summary(
                   path_id, direct_files, direct_logical_size, direct_allocated_size,
                   subtree_files, subtree_logical_size, subtree_allocated_size,
                   self_completed_at_ns, subtree_complete_through_ns,
                   last_completed_scan_id, updated_at_ns
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(path_id) DO UPDATE SET
                   direct_files=excluded.direct_files,
                   direct_logical_size=excluded.direct_logical_size,
                   direct_allocated_size=excluded.direct_allocated_size,
                   subtree_files=excluded.subtree_files,
                   subtree_logical_size=excluded.subtree_logical_size,
                   subtree_allocated_size=excluded.subtree_allocated_size,
                   self_completed_at_ns=excluded.self_completed_at_ns,
                   subtree_complete_through_ns=excluded.subtree_complete_through_ns,
                   last_completed_scan_id=excluded.last_completed_scan_id,
                   updated_at_ns=excluded.updated_at_ns""",
            (
                path_id,
                direct_files,
                direct_logical,
                direct_allocated,
                subtree_files,
                subtree_logical,
                subtree_allocated,
                self_completed_at,
                subtree_complete_through,
                last_completed_scan_id,
                updated_at,
            ),
        )
        if on_progress is not None:
            on_progress(completed_count, total)


def backfill_directory_summaries(
    connection: sqlite3.Connection,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    """Populate summaries and conservative historical completion timestamps."""

    completed_by_directory: dict[int, tuple[int, int]] = {}
    directories = connection.execute(
        """SELECT p.id, p.root_id, p.path_key, first_scan.started_at_ns AS first_seen_at_ns
           FROM path p JOIN scan first_scan ON first_scan.id=p.first_seen_scan_id
           WHERE p.state='present' AND p.entry_kind IN ('root', 'directory')"""
    ).fetchall()
    scopes = connection.execute(
        """SELECT ss.root_id, ss.scope_path_key, ss.finished_at_ns, ss.scan_id
           FROM scan_scope ss
           WHERE ss.status='completed' AND ss.finished_at_ns IS NOT NULL
           ORDER BY ss.finished_at_ns"""
    ).fetchall()
    for scope in scopes:
        scope_key = bytes(scope["scope_path_key"])
        completion = (int(scope["finished_at_ns"]), int(scope["scan_id"]))
        for directory in directories:
            if (
                directory["root_id"] == scope["root_id"]
                and bytes(directory["path_key"]).startswith(scope_key)
                and scope["finished_at_ns"] >= directory["first_seen_at_ns"]
            ):
                completed_by_directory[int(directory["id"])] = completion

    roots = connection.execute(
        """SELECT id, root_id FROM path
           WHERE state='present' AND entry_kind='root' AND parent_path_id IS NULL"""
    ).fetchall()
    completed_count = 0
    total = len(directories)
    if on_progress is not None and not roots:
        on_progress(0, total)
    for root in roots:
        def report_root_progress(
            root_completed: int,
            _root_total: int,
            offset: int = completed_count,
        ) -> None:
            if on_progress is not None:
                on_progress(offset + root_completed, total)

        refresh_directory_summaries(
            connection,
            int(root["root_id"]),
            int(root["id"]),
            completed_directories=completed_by_directory,
            on_progress=report_root_progress,
        )
        root_directory_count = connection.execute(
            """SELECT count(*) FROM path
               WHERE root_id=? AND state='present' AND entry_kind IN ('root', 'directory')""",
            (root["root_id"],),
        ).fetchone()[0]
        completed_count += int(root_directory_count)
