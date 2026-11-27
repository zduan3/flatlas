"""Persistent directory usage summaries and scan freshness."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping

_SUMMARY_UPSERT = """INSERT INTO directory_summary(
       path_id, direct_files, direct_logical_size, direct_allocated_size,
       subtree_files, subtree_logical_size, subtree_allocated_size,
       self_completed_at_ns, subtree_complete_through_ns,
       last_completed_scan_id, updated_at_ns, direct_entries, subtree_entries
   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
       updated_at_ns=excluded.updated_at_ns,
       direct_entries=excluded.direct_entries,
       subtree_entries=excluded.subtree_entries"""


def _id_batches(path_ids: list[int], size: int = 800) -> list[list[int]]:
    return [path_ids[offset:offset + size] for offset in range(0, len(path_ids), size)]


def _directory_rows(
    connection: sqlite3.Connection,
    root_id: int,
    scope_path_id: int,
    max_depth: int | None = None,
) -> list[sqlite3.Row]:
    return connection.execute(
        """WITH RECURSIVE
        descendants(id, depth) AS (
            SELECT id, 0 FROM path WHERE id=?
            UNION ALL
            SELECT p.id, d.depth + 1 FROM descendants d CROSS JOIN path p ON p.parent_path_id=d.id
            WHERE p.state='present' AND p.entry_kind='directory' AND (? IS NULL OR d.depth < ?)
        ),
        ancestors(id) AS (
            SELECT id FROM path WHERE id=?
            UNION ALL
            SELECT p.parent_path_id FROM ancestors a CROSS JOIN path p ON p.id=a.id
            WHERE p.parent_path_id IS NOT NULL
        )
        SELECT p.id, p.parent_path_id, p.path_key,
               ds.self_completed_at_ns, ds.last_completed_scan_id
        FROM (SELECT id FROM descendants UNION SELECT id FROM ancestors) s
        CROSS JOIN path p ON p.id=s.id
        LEFT JOIN directory_summary ds ON ds.path_id=p.id
        WHERE p.root_id=? AND p.state='present'
          AND p.entry_kind IN ('root', 'directory')""",
        (scope_path_id, max_depth, max_depth, scope_path_id, root_id),
    ).fetchall()


def refresh_directory_summaries(
    connection: sqlite3.Connection,
    root_id: int,
    scope_path_id: int,
    *,
    completed_directories: Mapping[int, tuple[int, int]] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    max_depth: int | None = None,
) -> None:
    """Refresh cached totals for one subtree and every affected ancestor.

    ``max_depth`` limits the recomputed subtree (1 after a shallow scan: the
    scope and its direct subdirectories); deeper directories keep their cached
    totals, which their parents read.

    ``completed_directories`` maps path id to ``(completed_at_ns, scan_id)``.
    A directory's completion timestamp advances only after all of its direct
    entries were enumerated and inspected successfully. Failed attempts leave
    the previous successful timestamp unchanged and remain visible through the
    scan audit tables.
    """

    _refresh_rows(
        connection,
        _directory_rows(connection, root_id, scope_path_id, max_depth),
        {} if completed_directories is None else completed_directories,
        on_progress,
    )


def refresh_finished_directories(
    connection: sqlite3.Connection,
    path_ids: list[int],
    completed_directories: Mapping[int, tuple[int, int]],
) -> None:
    """Refresh the selected directory summaries in post-order.

    A scan checkpoint calls this for the directories it finished since the
    previous checkpoint. Traversal is post-order, so every visited child of a
    finished directory was finished earlier: in this batch (computed here) or
    in an earlier checkpoint (read from its committed summary). Ancestors that
    are still being traversed keep their previous totals until a later batch
    or the final scope refresh. Selective refresh and index removal also use
    this routine with the affected directories and their ancestors.
    """
    rows: list[sqlite3.Row] = []
    for batch in _id_batches(path_ids):
        placeholders = ", ".join("?" for _ in batch)
        rows.extend(connection.execute(
            f"""SELECT p.id, p.parent_path_id, p.path_key,
                       ds.self_completed_at_ns, ds.last_completed_scan_id
                FROM path p LEFT JOIN directory_summary ds ON ds.path_id=p.id
                WHERE p.id IN ({placeholders}) AND p.state='present'
                  AND p.entry_kind IN ('root', 'directory')""",
            batch,
        ).fetchall())
    _refresh_rows(connection, rows, completed_directories, None)


def _refresh_rows(
    connection: sqlite3.Connection,
    directory_rows: list[sqlite3.Row],
    completed: Mapping[int, tuple[int, int]],
    on_progress: Callable[[int, int], None] | None,
) -> None:
    rows = sorted(directory_rows, key=lambda row: bytes(row["path_key"]).count(b"\0"), reverse=True)
    total = len(rows)
    if on_progress is not None:
        on_progress(0, total)
    if not rows:
        return

    path_ids = [int(row["id"]) for row in rows]
    direct_by_parent: dict[int, tuple[int, int, int | None]] = {}
    entries_by_parent: dict[int, int] = {}
    children_by_parent: dict[int, list[tuple[int, int | None, int | None, int | None, int | None, int | None]]] = {}
    for batch in _id_batches(path_ids):
        placeholders = ", ".join("?" for _ in batch)
        direct_rows = connection.execute(
            f"""SELECT parent_path_id, count(*) AS entries,
                       count(CASE WHEN entry_kind='file' THEN 1 END) AS files,
                       COALESCE(sum(CASE WHEN entry_kind='file' THEN logical_size END), 0) AS logical_size,
                       CASE WHEN count(CASE WHEN entry_kind='file' THEN 1 END)
                                 = count(CASE WHEN entry_kind='file' THEN allocated_size END)
                            THEN COALESCE(sum(CASE WHEN entry_kind='file' THEN allocated_size END), 0)
                            ELSE NULL END AS allocated_size
                FROM path
                WHERE parent_path_id IN ({placeholders}) AND state='present'
                GROUP BY parent_path_id""",
            batch,
        ).fetchall()
        for direct in direct_rows:
            entries_by_parent[int(direct["parent_path_id"])] = int(direct["entries"])
            direct_by_parent[int(direct["parent_path_id"])] = (
                int(direct["files"]),
                int(direct["logical_size"]),
                direct["allocated_size"],
            )

        child_rows = connection.execute(
            f"""SELECT p.parent_path_id, p.id, ds.subtree_files,
                       ds.subtree_logical_size, ds.subtree_allocated_size,
                       ds.subtree_complete_through_ns, ds.subtree_entries
                FROM path p LEFT JOIN directory_summary ds ON ds.path_id=p.id
                WHERE p.parent_path_id IN ({placeholders})
                  AND p.state='present' AND p.entry_kind='directory'""",
            batch,
        ).fetchall()
        for child in child_rows:
            children_by_parent.setdefault(int(child["parent_path_id"]), []).append(
                (
                    int(child["id"]),
                    child["subtree_files"],
                    child["subtree_logical_size"],
                    child["subtree_allocated_size"],
                    child["subtree_complete_through_ns"],
                    child["subtree_entries"],
                )
            )

    updated_at = time.time_ns()
    computed: dict[int, tuple[int, int, int | None, int | None, int]] = {}
    upserts: list[tuple[int | None, ...]] = []
    for completed_count, directory in enumerate(rows, start=1):
        path_id = int(directory["id"])
        if path_id in completed:
            self_completed_at, last_completed_scan_id = completed[path_id]
        else:
            self_completed_at = directory["self_completed_at_ns"]
            last_completed_scan_id = directory["last_completed_scan_id"]

        direct_files, direct_logical, direct_allocated = direct_by_parent.get(path_id, (0, 0, 0))
        children = []
        for child in children_by_parent.get(path_id, []):
            cached = computed.get(int(child[0]))
            children.append(child[1:] if cached is None else cached)

        subtree_files = direct_files + sum(int(child[0] or 0) for child in children)
        subtree_logical = direct_logical + sum(int(child[1] or 0) for child in children)
        child_allocated = [child[2] for child in children]
        if direct_allocated is None or any(value is None for value in child_allocated):
            subtree_allocated = None
        else:
            subtree_allocated = int(direct_allocated) + sum(int(value) for value in child_allocated)

        child_completed = [child[3] for child in children]
        if self_completed_at is None or any(value is None for value in child_completed):
            subtree_complete_through = None
        else:
            subtree_complete_through = min(
                [int(self_completed_at), *(int(value) for value in child_completed)]
            )

        # Entries count every present descendant of any kind plus the directory
        # itself, like GNU ``du --inodes``.
        direct_entries = entries_by_parent.get(path_id, 0)
        subtree_entries = 1 + direct_entries - len(children) + sum(int(child[4] or 1) for child in children)

        computed[path_id] = (
            subtree_files,
            subtree_logical,
            subtree_allocated,
            subtree_complete_through,
            subtree_entries,
        )
        upserts.append(
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
                direct_entries,
                subtree_entries,
            )
        )
        if on_progress is not None:
            on_progress(completed_count, total)
    connection.executemany(_SUMMARY_UPSERT, upserts)


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


def refresh_all_directory_summaries(
    connection: sqlite3.Connection,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    """Recompute every root's totals, keeping stored completion timestamps."""
    roots = connection.execute(
        """SELECT id, root_id FROM path
           WHERE state='present' AND entry_kind='root' AND parent_path_id IS NULL"""
    ).fetchall()
    total = int(connection.execute(
        "SELECT count(*) FROM path WHERE state='present' AND entry_kind IN ('root', 'directory')"
    ).fetchone()[0])
    offset = 0
    for root in roots:
        def report(done: int, _root_total: int, base: int = offset) -> None:
            if on_progress is not None:
                on_progress(base + done, total)

        refresh_directory_summaries(connection, int(root["root_id"]), int(root["id"]), on_progress=report)
        offset += int(connection.execute(
            "SELECT count(*) FROM path WHERE root_id=? AND state='present' AND entry_kind IN ('root', 'directory')",
            (root["root_id"],),
        ).fetchone()[0])
