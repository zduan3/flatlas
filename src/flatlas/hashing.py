"""Conservative lazy hashing for duplicate candidates."""

from __future__ import annotations

import os
import sqlite3
import stat as stat_module
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blake3 import blake3

from flatlas.duplicate_filters import DuplicateFilters
from flatlas.errors import FlatlasError
from flatlas.lazystat import uses_lazy_metadata
from flatlas.paths import _scope_row, _sqlite_integer, display_text, path_from_key

QUICK_SAMPLE_BYTES = 64 * 1024


@dataclass(frozen=True)
class HashProgress:
    phase: str
    completed_files: int
    total_files: int
    completed_bytes: int
    total_bytes: int


@dataclass(frozen=True)
class HashIssue:
    path: str
    stage: str
    status: str
    error_code: int | None
    message: str


@dataclass(frozen=True)
class HashSummary:
    candidate_files: int
    candidate_bytes: int
    content_files_read: int
    changed_files: int
    errors: int
    cache_hits: int
    content_bytes_read: int
    issues: tuple[HashIssue, ...]

    @property
    def complete(self) -> bool:
        return self.changed_files == 0 and self.errors == 0

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "complete": self.complete,
            "candidate_files": self.candidate_files,
            "candidate_bytes": self.candidate_bytes,
            "content_files_read": self.content_files_read,
            "changed_files": self.changed_files,
            "errors": self.errors,
            "cache_hits": self.cache_hits,
            "content_bytes_read": self.content_bytes_read,
        }


class _HashBasisChanged(Exception):
    pass


def _stat_hash_basis(file_stat: os.stat_result) -> tuple[int | None, ...]:
    return (
        _sqlite_integer(getattr(file_stat, "st_dev", None)),
        _sqlite_integer(getattr(file_stat, "st_ino", None)),
        file_stat.st_size,
        getattr(file_stat, "st_mtime_ns", None),
        None if sys.platform == "win32" else getattr(file_stat, "st_ctime_ns", None),
        getattr(file_stat, "st_birthtime_ns", None),
    )


def _candidate_hash_basis(candidate: dict[str, Any]) -> tuple[int | None, ...]:
    return (
        candidate["device"],
        candidate["inode"],
        candidate["logical_size"],
        candidate["mtime_ns"],
        None if sys.platform == "win32" else candidate["change_time_ns"],
        candidate["birth_time_ns"],
    )


def _reconcile_lazy_times(candidate: dict[str, Any], actual: tuple[int | None, ...]) -> bool:
    """Accept exact times from ``fstat`` for a root scanned with lazy metadata.

    Lazy Lustre metadata can carry a modification/change time that trails the
    object storage targets (see flatlas.lazystat), which would mark the file as
    changed forever. Identity and size must still match; only the times are
    replaced, and the caller persists them on the path row.
    """
    expected = _candidate_hash_basis(candidate)
    if not candidate.get("lazy_metadata") or actual[:3] != expected[:3]:
        return False
    candidate["mtime_ns"], candidate["change_time_ns"], candidate["birth_time_ns"] = actual[3:]
    candidate["times_reconciled"] = True
    return True


def _persist_reconciled_times(connection: sqlite3.Connection, candidate: dict[str, Any]) -> None:
    if candidate.pop("times_reconciled", False):
        connection.execute(
            "UPDATE path SET mtime_ns=?, change_time_ns=?, birth_time_ns=? WHERE id=?",
            (candidate["mtime_ns"], candidate["change_time_ns"], candidate["birth_time_ns"], candidate["id"]),
        )


def _validated_digest(candidate: dict[str, Any], *, full: bool) -> bytes:
    expected = _candidate_hash_basis(candidate)
    size = int(candidate["logical_size"])
    digest = blake3()

    def consume(chunk: bytes) -> None:
        candidate["content_bytes_read"] += len(chunk)
        digest.update(chunk)

    with path_from_key(candidate["root_path_raw"], candidate["path_key"]).open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat_module.S_ISREG(before.st_mode):
            raise _HashBasisChanged
        actual = _stat_hash_basis(before)
        if actual != expected:
            if not _reconcile_lazy_times(candidate, actual):
                raise _HashBasisChanged
            expected = actual
        if full or size <= QUICK_SAMPLE_BYTES * 2:
            while chunk := stream.read(1024 * 1024):
                consume(chunk)
        else:
            consume(stream.read(QUICK_SAMPLE_BYTES))
            stream.seek(-QUICK_SAMPLE_BYTES, os.SEEK_END)
            consume(stream.read(QUICK_SAMPLE_BYTES))
        after = os.fstat(stream.fileno())
        if _stat_hash_basis(after) != expected:
            raise _HashBasisChanged
    return digest.digest()


def _save_hash(
    connection: sqlite3.Connection,
    candidate: dict[str, Any],
    *,
    quick: bytes,
    full: bytes | None = None,
) -> None:
    state = "full_ready" if full is not None else "quick_ready"
    connection.execute(
        """INSERT INTO file_hash(path_id, state, quick_algorithm, quick_digest, quick_sample_bytes,
        full_algorithm, full_digest, full_verification, basis_device, basis_inode, basis_size,
        basis_mtime_ns, basis_change_time_ns, basis_birth_time_ns, last_hashed_scan_id, error_message)
        VALUES (?, ?, 'blake3', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(path_id) DO UPDATE SET state=excluded.state, quick_algorithm=excluded.quick_algorithm,
        quick_digest=excluded.quick_digest, quick_sample_bytes=excluded.quick_sample_bytes,
        full_algorithm=excluded.full_algorithm, full_digest=excluded.full_digest,
        full_verification=excluded.full_verification,
        basis_device=excluded.basis_device, basis_inode=excluded.basis_inode, basis_size=excluded.basis_size,
        basis_mtime_ns=excluded.basis_mtime_ns, basis_change_time_ns=excluded.basis_change_time_ns,
        basis_birth_time_ns=excluded.basis_birth_time_ns, last_hashed_scan_id=excluded.last_hashed_scan_id,
        error_message=NULL""",
        (
            candidate["id"],
            state,
            quick,
            QUICK_SAMPLE_BYTES,
            "blake3" if full else None,
            full,
            "hash_only" if full else None,
            *_candidate_hash_basis(candidate),
            candidate["last_seen_scan_id"],
        ),
    )


def _save_hash_problem(connection: sqlite3.Connection, candidate: dict[str, Any], state: str, message: str) -> None:
    connection.execute(
        """INSERT INTO file_hash(path_id, state, basis_device, basis_inode, basis_size,
        basis_mtime_ns, basis_change_time_ns, basis_birth_time_ns, last_hashed_scan_id, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path_id) DO UPDATE SET state=excluded.state,
        quick_algorithm=NULL, quick_digest=NULL, quick_sample_bytes=NULL,
        full_algorithm=NULL, full_digest=NULL, full_verification=NULL,
        basis_device=excluded.basis_device, basis_inode=excluded.basis_inode,
        basis_size=excluded.basis_size, basis_mtime_ns=excluded.basis_mtime_ns,
        basis_change_time_ns=excluded.basis_change_time_ns,
        basis_birth_time_ns=excluded.basis_birth_time_ns,
        last_hashed_scan_id=excluded.last_hashed_scan_id,
        error_message=excluded.error_message""",
        (
            candidate["id"],
            state,
            *_candidate_hash_basis(candidate),
            candidate["last_seen_scan_id"],
            message,
        ),
    )


def _duplicate_hash_candidates(
    connection: sqlite3.Connection, scope: Path, filters: DuplicateFilters,
) -> list[dict[str, Any]]:
    selected = _scope_row(connection, scope)
    if selected["entry_kind"] not in {"root", "directory"}:
        raise FlatlasError(f"duplicate scope is not a directory: {selected['path_display']}")
    rows = connection.execute(
        """WITH RECURSIVE descendants(id) AS (
            SELECT id FROM path WHERE id=? UNION ALL
            SELECT p.id FROM descendants d CROSS JOIN path p ON p.parent_path_id=d.id
        ), duplicate_sizes(logical_size) AS (
            SELECT p.logical_size FROM descendants d CROSS JOIN path p ON d.id=p.id
            WHERE p.state='present' AND p.entry_kind='file' AND p.logical_size > 0
              AND (p.allocated_size IS NULL OR p.allocated_size > 0)
              AND p.logical_size BETWEEN ? AND ?
            GROUP BY p.logical_size
            HAVING count(DISTINCT coalesce(p.device, '') || ':' || coalesce(p.inode, -p.id)) > 1
        )
        SELECT p.id, p.path_display, p.path_key, r.root_path_raw, p.logical_size, p.device, p.inode, p.mtime_ns,
               p.change_time_ns, p.birth_time_ns, p.last_seen_scan_id,
               r.filesystem_type,
               h.state AS hash_state, h.quick_algorithm, h.quick_digest, h.quick_sample_bytes,
               h.full_algorithm, h.full_digest,
               h.basis_device AS hash_device, h.basis_inode AS hash_inode,
               h.basis_size AS hash_size, h.basis_mtime_ns AS hash_mtime_ns,
               h.basis_change_time_ns AS hash_change_time_ns,
               h.basis_birth_time_ns AS hash_birth_time_ns
        FROM descendants d CROSS JOIN path_with_display p ON d.id=p.id
        JOIN duplicate_sizes s ON s.logical_size=p.logical_size
        JOIN root r ON r.id=p.root_id
        LEFT JOIN file_hash h ON h.path_id=p.id
        WHERE p.state='present' AND p.entry_kind='file' AND p.logical_size > 0
          AND (p.allocated_size IS NULL OR p.allocated_size > 0)
        ORDER BY p.logical_size, p.path_display""",
        (selected["id"], *filters.size_bounds),
    ).fetchall()
    candidates = [dict(row) for row in rows]
    for candidate in candidates:
        candidate["lazy_metadata"] = uses_lazy_metadata(candidate.pop("filesystem_type"))
    return candidates


def ensure_duplicate_hashes(
    connection: sqlite3.Connection,
    scope: Path,
    *,
    on_progress: Callable[[HashProgress], None] | None = None,
    commit_batch: int = 100,
    filters: DuplicateFilters | None = None,
) -> HashSummary:
    if commit_batch < 1:
        raise ValueError("commit_batch must be at least 1")
    candidates = _duplicate_hash_candidates(connection, scope, filters or DuplicateFilters())
    candidate_bytes = sum(int(candidate["logical_size"]) for candidate in candidates)
    if on_progress is not None:
        on_progress(HashProgress("candidates", 0, len(candidates), 0, candidate_bytes))

    changed_files = 0
    errors = 0
    writes = 0
    content_bytes_read = 0
    content_path_ids: set[int] = set()
    issues: list[HashIssue] = []
    # Hardlink aliases share content: (device, inode, full) -> (indexed basis, validated basis, digest).
    alias_digests: dict[tuple[Any, ...], tuple[tuple[int | None, ...], tuple[int | None, ...], bytes]] = {}

    def digest_once(candidate: dict[str, Any], *, full: bool) -> bytes:
        """Read each inode at most once per stage while its basis matches."""
        nonlocal content_bytes_read
        indexed = _candidate_hash_basis(candidate)
        key = (candidate["device"], candidate["inode"], full) if candidate["inode"] is not None else None
        cached = alias_digests.get(key) if key is not None else None
        if cached is not None and indexed in cached[:2]:
            validated = cached[1]
            if indexed != validated:
                candidate["mtime_ns"], candidate["change_time_ns"], candidate["birth_time_ns"] = validated[3:]
                candidate["times_reconciled"] = True
            return cached[2]
        candidate["content_bytes_read"] = 0
        try:
            digest = _validated_digest(candidate, full=full)
        finally:
            read_bytes = candidate.pop("content_bytes_read")
            content_bytes_read += read_bytes
            if read_bytes:
                content_path_ids.add(int(candidate["id"]))
        if key is not None:
            alias_digests[key] = (indexed, _candidate_hash_basis(candidate), digest)
        return digest

    def save_problem(
        candidate: dict[str, Any], state: str, stage: str, message: str, error_code: int | None = None,
    ) -> None:
        nonlocal writes
        message = display_text(message)
        issues.append(HashIssue(
            candidate["path_display"], stage, "changed" if state == "stale" else "failed", error_code, message,
        ))
        _save_hash_problem(connection, candidate, state, message)
        candidate["hash_state"] = state
        candidate["quick_digest"] = None
        candidate["full_digest"] = None
        writes += 1
        if writes % commit_batch == 0:
            connection.commit()

    def has_current_basis(candidate: dict[str, Any]) -> bool:
        return (
            candidate["hash_device"],
            candidate["hash_inode"],
            candidate["hash_size"],
            candidate["hash_mtime_ns"],
            candidate["hash_change_time_ns"],
            candidate["hash_birth_time_ns"],
        ) == _candidate_hash_basis(candidate)

    def has_quick(candidate: dict[str, Any]) -> bool:
        return bool(
            candidate["hash_state"] in {"quick_ready", "full_ready"}
            and candidate["quick_algorithm"] == "blake3"
            and candidate["quick_digest"] is not None
            and candidate["quick_sample_bytes"] == QUICK_SAMPLE_BYTES
            and has_current_basis(candidate)
        )

    def has_full(candidate: dict[str, Any]) -> bool:
        return bool(
            has_quick(candidate)
            and candidate["hash_state"] == "full_ready"
            and candidate["full_algorithm"] == "blake3"
            and candidate["full_digest"] is not None
        )

    def remember_current_basis(candidate: dict[str, Any]) -> None:
        (
            candidate["hash_device"],
            candidate["hash_inode"],
            candidate["hash_size"],
            candidate["hash_mtime_ns"],
            candidate["hash_change_time_ns"],
            candidate["hash_birth_time_ns"],
        ) = _candidate_hash_basis(candidate)

    quick_work = [
        candidate
        for candidate in candidates
        if not has_quick(candidate)
    ]
    cache_hits = len(candidates) - len(quick_work)
    quick_total_bytes = sum(int(candidate["logical_size"]) for candidate in quick_work)
    if on_progress is not None and quick_work:
        on_progress(HashProgress("quick", 0, len(quick_work), 0, quick_total_bytes))
    quick_done_bytes = 0
    try:
        for index, candidate in enumerate(quick_work, start=1):
            size = int(candidate["logical_size"])
            try:
                quick = digest_once(candidate, full=False)
                _persist_reconciled_times(connection, candidate)
                is_full = size <= QUICK_SAMPLE_BYTES * 2
                _save_hash(connection, candidate, quick=quick, full=quick if is_full else None)
                candidate["hash_state"] = "full_ready" if is_full else "quick_ready"
                candidate["quick_algorithm"] = "blake3"
                candidate["quick_digest"] = quick
                candidate["quick_sample_bytes"] = QUICK_SAMPLE_BYTES
                candidate["full_algorithm"] = "blake3" if is_full else None
                candidate["full_digest"] = quick if is_full else None
                remember_current_basis(candidate)
                writes += 1
                if writes % commit_batch == 0:
                    connection.commit()
            except _HashBasisChanged:
                changed_files += 1
                save_problem(candidate, "stale", "quick", "metadata changed since scan")
            except OSError as exc:
                errors += 1
                save_problem(candidate, "failed", "quick", str(exc), exc.errno)
            quick_done_bytes += size
            if on_progress is not None:
                on_progress(HashProgress("quick", index, len(quick_work), quick_done_bytes, quick_total_bytes))

        # Older quick hashes of small files already cover the entire file and can be
        # promoted without reading the content a second time.
        for candidate in candidates:
            if (
                int(candidate["logical_size"]) <= QUICK_SAMPLE_BYTES * 2
                and has_quick(candidate)
                and not has_full(candidate)
            ):
                _save_hash(
                    connection,
                    candidate,
                    quick=candidate["quick_digest"],
                    full=candidate["quick_digest"],
                )
                candidate["hash_state"] = "full_ready"
                candidate["full_algorithm"] = "blake3"
                candidate["full_digest"] = candidate["quick_digest"]
                writes += 1
                if writes % commit_batch == 0:
                    connection.commit()

        quick_groups: dict[tuple[int, bytes], list[dict[str, Any]]] = defaultdict(list)
        for candidate in candidates:
            if has_quick(candidate):
                quick_groups[(int(candidate["logical_size"]), candidate["quick_digest"])].append(candidate)

        full_work = [
            candidate
            for group in quick_groups.values()
            if len(group) > 1
            for candidate in group
            if not has_full(candidate)
        ]
        full_total_bytes = sum(int(candidate["logical_size"]) for candidate in full_work)
        if on_progress is not None and full_work:
            on_progress(HashProgress("full", 0, len(full_work), 0, full_total_bytes))
        full_done_bytes = 0
        for index, candidate in enumerate(full_work, start=1):
            size = int(candidate["logical_size"])
            try:
                full = digest_once(candidate, full=True)
                _persist_reconciled_times(connection, candidate)
                _save_hash(connection, candidate, quick=candidate["quick_digest"], full=full)
                candidate["hash_state"] = "full_ready"
                candidate["full_algorithm"] = "blake3"
                candidate["full_digest"] = full
                remember_current_basis(candidate)
                writes += 1
                if writes % commit_batch == 0:
                    connection.commit()
            except _HashBasisChanged:
                changed_files += 1
                save_problem(candidate, "stale", "full", "metadata changed during hash")
            except OSError as exc:
                errors += 1
                save_problem(candidate, "failed", "full", str(exc), exc.errno)
            full_done_bytes += size
            if on_progress is not None:
                on_progress(HashProgress("full", index, len(full_work), full_done_bytes, full_total_bytes))
        connection.commit()
    except KeyboardInterrupt:
        connection.commit()
        raise

    summary = HashSummary(
        candidate_files=len(candidates),
        candidate_bytes=candidate_bytes,
        content_files_read=len(content_path_ids),
        changed_files=changed_files,
        errors=errors,
        cache_hits=cache_hits,
        content_bytes_read=content_bytes_read,
        issues=tuple(issues),
    )
    if on_progress is not None:
        phase = "incomplete" if not summary.complete else "cached" if not content_path_ids else "completed"
        on_progress(HashProgress(phase, 0, len(candidates), 0, candidate_bytes))
    return summary
