"""Machine-readable record shapes shared by every query command.

One vocabulary for every command and format: ``path`` is always the absolute
display path, sizes are bytes, times are ``*_ns`` integers, unknown is
``None``. Tables only abbreviate and relativize these records for display.
"""

from __future__ import annotations

from typing import Any

# Entry status, the same words in tables and machine output.
OK = "ok"  # every covering scan observed it completely
NEW = "new"  # not covered by any scan yet
PART = "part"  # covered only by incomplete scans
GONE = "gone"  # indexed, but missing from the live listing (ls only)

KINDS = {
    "root": "dir", "directory": "dir", "file": "file",
    "symlink": "symlink", "reparse": "reparse", "other": "other",
}

ENTRY_FIELDS = (
    "type", "path", "kind", "status", "logical_size", "allocated_size",
    "files", "entries", "mtime_ns", "complete_through_ns", "depth",
)
FREED_FIELDS = (
    "unique_logical_size", "unique_allocated_size",
    "freed_logical_size", "freed_allocated_size", "freed_files", "unknown_link_files",
)
GROUP_FIELDS = (
    "type", "algorithm", "digest", "logical_size", "count", "inodes",
    "theoretical_savings", "external_links", "paths",
)
HASH_FIELDS = (
    "type", "complete", "candidate_files", "candidate_bytes",
    "content_files_read", "changed_files", "errors", "cache_hits", "content_bytes_read",
)


def entry_record(
    *,
    path: str,
    kind: str,
    status: str,
    logical_size: int | None,
    allocated_size: int | None,
    files: int | None,
    entries: int | None,
    mtime_ns: int | None,
    complete_through_ns: int | None,
    depth: int | None,
) -> dict[str, Any]:
    """One entry in the shared shape; ``kind`` takes an index ``entry_kind``."""
    return {
        "type": "entry",
        "path": path,
        "kind": KINDS.get(kind, kind),
        "status": status,
        "logical_size": logical_size,
        "allocated_size": allocated_size,
        "files": files,
        "entries": entries,
        "mtime_ns": mtime_ns,
        "complete_through_ns": complete_through_ns,
        "depth": depth,
    }


ROOT_FIELDS = (
    "type", "id", "path", "platform", "namespace_kind", "filesystem_type", "metadata", "enabled",
    "created_at_ns", "updated_at_ns", "files", "logical_size", "allocated_size",
    "last_scan_id", "last_scan_status", "last_scan_started_at_ns", "last_scan_finished_at_ns",
)
FILESYSTEM_FIELDS = (
    "type", "filesystem", "fstype", "blocks_1k", "used_1k", "available_1k", "use_percent", "mounted_on", "status",
)
SCAN_FIELDS = (
    "type", "scan_id", "root_id", "root", "kind", "status", "started_at_ns", "finished_at_ns", "duration_ns",
    "files_seen", "dirs_seen", "errors_seen", "refresh_before_ns", "max_depth", "refresh_changed", "scanner_version", "note",
    "scope_id", "scope_status", "paths_seen", "error_count", "scope", "scope_path_key_hex",
)
PLAN_FIELDS = (
    "type", "id", "root", "source_scan_id", "status", "created_at_ns", "immutable_at_ns", "exported_at_ns",
    "note", "operation_count", "theoretical_savings",
)
OPERATION_FIELDS = (
    "type", "ordinal", "action", "canonical_path", "replacement_path", "expected_size", "expected_digest",
    "expected_algorithm", "preconditions_json", "status",
)
