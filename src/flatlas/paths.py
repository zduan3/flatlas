"""Filesystem namespace and persistent path identity helpers."""

from __future__ import annotations

import errno
import os
import sqlite3
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flatlas.db import _last_insert_id, now_ns
from flatlas.errors import FlatlasError, PathError
from flatlas.mounts import filesystem_type


@dataclass(frozen=True)
class Namespace:
    path: Path
    platform: str
    kind: str
    identity: bytes


def discover_namespace(value: Path) -> Namespace:
    path = value.expanduser().resolve()
    if not path.exists():
        raise PathError(f"path does not exist: {path}")
    if os.name == "nt":
        anchor = Path(path.anchor)
        return Namespace(anchor, "windows", "windows_volume", os.fsencode(str(anchor)))
    current = path if path.is_dir() else path.parent
    while current.parent != current and not os.path.ismount(current):
        current = current.parent
    platform = "macos" if sys.platform == "darwin" else "linux"
    return Namespace(current, platform, "posix_mount", os.fsencode(str(current)))


def display_path(value: Path | str) -> str:
    """Text form of a path for ``*_display`` columns, messages and lookups.

    Identity lives in the raw-byte ``path_key``; SQLite TEXT must be valid
    UTF-8, so bytes that are not (POSIX names decoded with surrogateescape)
    are shown as ``\\xNN`` escapes instead of aborting the transaction.
    """
    return os.fsencode(str(value)).decode("utf-8", "backslashreplace")


def display_text(value: object) -> str:
    """``str(value)`` made storable as SQLite TEXT (lone surrogates escaped)."""
    return str(value).encode("utf-8", "backslashreplace").decode("utf-8")


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
            namespace.identity, display_path(namespace.path), namespace.platform, namespace.kind,
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


def scope_path(value: Path | str, *, follow: bool = True) -> Path:
    """Absolute scope path; with ``follow=False`` a final symlink is not resolved.

    Matches GNU ``du`` without ``-D``: parent components are resolved, the last
    one is kept unless the argument ends in ``/`` or is ``.``/``..``. Pass a
    ``str`` to keep a trailing slash; ``Path`` drops it.
    """
    raw = os.path.expanduser(os.fspath(value))
    if os.altsep is not None:
        raw = raw.replace(os.altsep, os.sep)
    if follow or raw.endswith(os.sep) or Path(raw).name in {"", ".", ".."}:
        if ".." in raw.split(os.sep):
            # ".." after a symlink means the target's parent; only resolve() knows.
            return Path(raw).resolve()
        return _resolve_directory(Path(os.path.abspath(raw)))
    absolute = Path(os.path.abspath(raw))
    return _resolve_directory(absolute.parent) / absolute.name


@dataclass
class _RealpathMemo:
    prefixes: dict[str, str]
    resolved: dict[str, Path]


_REALPATH_MEMO: ContextVar[_RealpathMemo | None] = ContextVar("flatlas_realpath_memo", default=None)


@contextmanager
def shared_path_resolution() -> Iterator[None]:
    """Resolve each directory prefix once for all paths looked up in this block.

    ``realpath`` stats every component of every path; a command given thousands
    of paths under one tree (``du --files0-from``) repeats the same ancestors,
    which costs a metadata round trip each on network filesystems. Inside the
    block a resolved prefix is reused, so symlinks changed meanwhile are not
    noticed: use it for one command, not for a long-lived process.
    """
    token = _REALPATH_MEMO.set(_RealpathMemo({}, {}))
    try:
        yield
    finally:
        _REALPATH_MEMO.reset(token)


def _resolve_directory(path: Path) -> Path:
    """``path.resolve()`` for an absolute, lexically normal path, memoized per prefix when enabled."""
    memo = _REALPATH_MEMO.get()
    if memo is None or os.name == "nt":
        return path.resolve()
    key = os.fspath(path)
    if (done := memo.resolved.get(key)) is not None:
        return done
    result = Path(_memo_realpath(key, memo.prefixes))
    # Like Path.resolve(): non-strict realpath hides symlink loops, stat reveals them.
    try:
        result.stat()
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RuntimeError(f"Symlink loop from {exc.filename!r}") from None
    memo.resolved[key] = result
    return result


def _memo_realpath(path: str, memo: dict[str, str]) -> str:
    # realpath(P/name) == realpath(realpath(P)/name): resolve the parent once,
    # then only this component needs an lstat; links defer to realpath itself.
    cached = memo.get(path)
    if cached is not None:
        return cached
    parent, name = os.path.split(path)
    if not name or parent == path:
        result = os.path.realpath(path)
    else:
        candidate = os.path.join(_memo_realpath(parent, memo), name)
        result = os.path.realpath(candidate) if os.path.islink(candidate) else candidate
    memo[path] = result
    return result


def indexed_key(connection: sqlite3.Connection, value: Path) -> tuple[int, bytes] | None:
    """``(root_id, path_key)`` of an absolute path under the deepest registered root.

    Computed from the root table without touching the filesystem, so lookups
    use the ``(root_id, path_key)`` unique index; ``path_display`` has no index
    and filtering on it scans every path row. Returns ``None`` if no registered
    root contains ``value``.
    """
    best: tuple[int, int] | None = None
    for root in connection.execute("SELECT id, root_path_raw, platform, case_sensitive FROM root"):
        try:
            root_text = os.fsdecode(root["root_path_raw"])
        except UnicodeDecodeError:
            continue
        root_parts = Path(root_text).parts
        if len(value.parts) < len(root_parts):
            continue
        prefix = value.parts[: len(root_parts)]
        case_sensitive = root["case_sensitive"]
        insensitive = (case_sensitive == 0) if case_sensitive is not None else (root["platform"] == "windows")
        if insensitive:
            matches = tuple(part.casefold() for part in prefix) == tuple(
                part.casefold() for part in root_parts
            )
        else:
            matches = prefix == root_parts
        if matches and (best is None or len(root_parts) > best[1]):
            best = (int(root["id"]), len(root_parts))
    if best is None:
        return None
    return best[0], b"".join(os.fsencode(part) + b"\0" for part in value.parts[best[1]:])


_SCOPE_ROW_SELECT = """SELECT p.id, p.root_id, p.path_key, p.parent_path_id, p.state,
                  p.last_seen_scan_id, p.first_seen_scan_id, p.path_display, p.entry_kind,
                  p.logical_size, p.allocated_size, p.mtime_ns,
                  r.root_path_display
        FROM path p JOIN root r ON r.id=p.root_id"""


def _scope_row(connection: sqlite3.Connection, value: Path | str, *, follow: bool = True) -> sqlite3.Row:
    selected = scope_path(value, follow=follow)
    display = display_path(selected)
    key = indexed_key(connection, selected)
    row = None
    if key is not None:
        row = connection.execute(
            f"{_SCOPE_ROW_SELECT} WHERE p.root_id=? AND p.path_key=? AND p.state='present' AND p.path_display=?",
            (*key, display),
        ).fetchone()
    if row is None:
        # Slow path (full scan) for names whose display text differs from the raw key.
        row = connection.execute(
            f"{_SCOPE_ROW_SELECT} WHERE p.path_display=? AND p.state='present'", (display,)
        ).fetchone()
    if row is None:
        raise PathError(f"path is not currently indexed: {display}")
    return row


def _lexical_absolute_path(value: Path) -> Path:
    """Make an absolute path without resolving or inspecting the filesystem."""
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def remove_indexed_subtree(connection: sqlite3.Connection, value: Path) -> dict[str, Any]:
    """Remove one indexed path and all indexed descendants without touching the filesystem."""
    selected = _removal_row(connection, value)
    summary = _remove_rows(connection, [selected])
    return {
        "path": display_path(_lexical_absolute_path(value)),
        "paths_removed": summary["paths_removed"],
        "plans_removed": summary["plans_removed"],
    }


def remove_indexed_subtrees(
    connection: sqlite3.Connection,
    values: Sequence[Path],
    *,
    on_error: Callable[[Path, FlatlasError], None] | None = None,
) -> dict[str, Any]:
    """Remove several indexed subtrees in one transaction, like ``rm -r`` on the index.

    A path that is not indexed raises, or with ``on_error`` is reported there
    and skipped. Paths nested in another requested path are removed with it.
    Only the surviving parents and their ancestors get their summaries
    recomputed, once for the whole batch.
    """
    rows = []
    for value in values:
        try:
            rows.append(_removal_row(connection, value))
        except FlatlasError as exc:
            if on_error is None:
                raise
            on_error(value, exc)
    return _remove_rows(connection, rows)


def remove_deleted_descendants(connection: sqlite3.Connection, value: Path) -> dict[str, Any]:
    """Purge tombstones beneath a present indexed directory in one transaction."""
    absolute = _lexical_absolute_path(value)
    display = display_path(absolute)
    try:
        with connection:
            # Reserve the write lock before checking the scope and its plan references.
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            key = indexed_key(connection, absolute)
            selected = None if key is None else connection.execute(
                """SELECT 1 FROM path WHERE root_id=? AND path_key=? AND state='present'
                   AND entry_kind IN ('root', 'directory')""",
                key,
            ).fetchone()
            if key is None or selected is None:
                raise PathError(f"path is not a present indexed directory: {display}")
            root_id, scope_key = key
            condition, keys = _key_range(scope_key, column="p.path_key")
            paths_removed, plans_removed = _delete_paths_and_plans(
                connection, f"p.root_id=? AND {condition} AND p.state='deleted'", (root_id, *keys)
            )
    except sqlite3.IntegrityError as exc:
        if exc.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY:
            raise FlatlasError(f"cannot purge tombstones under {display}: retained paths reference them") from exc
        raise
    except sqlite3.OperationalError as exc:
        raise FlatlasError(f"cannot purge tombstones under {display}: {exc}") from exc
    return {"path": display, "paths_removed": paths_removed, "plans_removed": plans_removed}


def _delete_paths_and_plans(
    connection: sqlite3.Connection, condition: str, parameters: tuple[object, ...],
) -> tuple[int, int]:
    """Delete matching paths (aliased as p) and their plans in the caller's transaction."""
    # Each reference is a primary-key lookup. Using the subtree index inside
    # these subqueries would rescan the range for every plan operation.
    plans_removed = connection.execute(
        f"""DELETE FROM plan WHERE id IN (
            SELECT po.plan_id FROM plan_operation po
            WHERE EXISTS (
                SELECT 1 FROM path p NOT INDEXED WHERE p.id=po.canonical_path_id AND {condition}
            ) OR EXISTS (
                SELECT 1 FROM path p NOT INDEXED WHERE p.id=po.replacement_path_id AND {condition}
            )
        )""",
        parameters * 2,
    ).rowcount
    paths_removed = connection.execute(f"DELETE FROM path AS p WHERE {condition}", parameters).rowcount
    return paths_removed, plans_removed


def _removal_row(connection: sqlite3.Connection, value: Path) -> sqlite3.Row:
    absolute = _lexical_absolute_path(value)
    display = display_path(absolute)
    selected = None
    columns = "id, root_id, parent_path_id, path_key"
    if (key := indexed_key(connection, absolute)) is not None:
        selected = connection.execute(
            f"SELECT {columns} FROM path WHERE root_id=? AND path_key=?",
            key,
        ).fetchone()
    if selected is None:
        selected = connection.execute(f"SELECT {columns} FROM path WHERE path_display=?", (display,)).fetchone()
    if selected is None:
        raise PathError(f"path is not indexed: {display}")
    return selected


def _key_range(
    key: bytes, *, descendants_only: bool = False, column: str = "path_key",
) -> tuple[str, tuple[bytes, ...]]:
    """SQL condition and parameters for the keys under ``key``.

    A key is its parent's key plus one NUL-terminated name, so a subtree is one
    range of the (root_id, path_key) index; 0x01 never ends a stored key.
    """
    # Keep it one plain range: with "path_key=? OR (...)" SQLite gave up the
    # range and scanned the whole root for every statement (0.65 s each on a
    # 4.8M-row index). The key itself is >= key, its descendants < the bound.
    lower = f"{column}>?" if descendants_only else f"{column}>=?"
    if not key:
        return lower, (key,)
    return f"{lower} AND {column}<?", (key, key[:-1] + b"\x01")


def _remove_rows(connection: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, Any]:
    outermost: list[sqlite3.Row] = []
    for row in sorted(rows, key=lambda item: (int(item["root_id"]), bytes(item["path_key"]))):
        kept = outermost[-1] if outermost else None
        if kept is not None and kept["root_id"] == row["root_id"] and bytes(row["path_key"]).startswith(
            bytes(kept["path_key"])
        ):
            continue  # inside an already selected subtree (or the same path again)
        outermost.append(row)

    plan_ids: set[str] = set()
    for row in outermost:
        condition, keys = _key_range(bytes(row["path_key"]), column="p.path_key")
        parameters = (row["root_id"], *keys)
        plan_ids.update(
            str(found["plan_id"])
            for found in connection.execute(
                f"""SELECT DISTINCT po.plan_id FROM plan_operation po
                    WHERE EXISTS (
                        SELECT 1 FROM path p NOT INDEXED
                        WHERE p.id=po.canonical_path_id AND p.root_id=? AND {condition}
                    ) OR EXISTS (
                        SELECT 1 FROM path p NOT INDEXED
                        WHERE p.id=po.replacement_path_id AND p.root_id=? AND {condition}
                    )""",
                parameters * 2,
            )
        )
    removed_paths = 0
    with connection:
        if plan_ids:
            ordered = sorted(plan_ids)
            placeholders = ", ".join("?" for _ in ordered)
            connection.execute(f"DELETE FROM plan WHERE id IN ({placeholders})", ordered)
        for row in outermost:
            condition, keys = _key_range(bytes(row["path_key"]))
            cursor = connection.execute(f"DELETE FROM path WHERE root_id=? AND {condition}", (row["root_id"], *keys))
            removed_paths += cursor.rowcount
        # Only the surviving parents and their ancestors change; every other
        # directory keeps its cached totals, which these are computed from.
        affected: set[int] = set()
        pending = [int(row["parent_path_id"]) for row in outermost if row["parent_path_id"] is not None]
        while pending:
            path_id = pending.pop()
            if path_id in affected:
                continue
            affected.add(path_id)
            parent = connection.execute("SELECT parent_path_id FROM path WHERE id=?", (path_id,)).fetchone()
            if parent is not None and parent["parent_path_id"] is not None:
                pending.append(int(parent["parent_path_id"]))
        if affected:
            from flatlas.summaries import refresh_finished_directories

            refresh_finished_directories(connection, sorted(affected), {})
    return {
        "scopes": len(outermost),
        "paths_removed": removed_paths,
        "plans_removed": len(plan_ids),
    }
