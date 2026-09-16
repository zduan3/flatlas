"""Backward-compatible facade for the decomposed core modules."""

# Re-exports and module objects in this facade are intentional compatibility APIs.
# ruff: noqa: F401

from __future__ import annotations

# Keep these module objects available for existing fault-injection callers.
import os
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flatlas import hashing as _hashing
from flatlas import scanning as _scanning
from flatlas.db import DDL, SCHEMA_VERSION, migrate, now_ns, open_database
from flatlas.errors import FlatlasError
from flatlas.hashing import QUICK_SAMPLE_BYTES, HashProgress, HashSummary
from flatlas.output import export_rows
from flatlas.paths import (
    Namespace,
    discover_namespace,
    get_root,
    parent_key,
    path_key,
    register_namespace,
    remove_indexed_subtree,
)
from flatlas.planning import create_dry_run_plan, list_plans, plan_operations
from flatlas.queries import (
    disk_usage,
    duplicate_groups,
    filesystem_usage,
    largest_files,
    list_child_directories,
    list_roots,
    query_paths,
)
from flatlas.scanning import SCANNER_VERSION, Observation, ScanProgress, upsert_path

_observation = _scanning._observation
_validated_digest = _hashing._validated_digest


def ensure_duplicate_hashes(
    connection: sqlite3.Connection,
    scope: Path,
    *,
    on_progress: Callable[[HashProgress], None] | None = None,
    commit_batch: int = 100,
) -> HashSummary:
    """Delegate hashing while preserving the legacy fault-injection seam."""
    _hashing._validated_digest = _validated_digest
    return _hashing.ensure_duplicate_hashes(
        connection,
        scope,
        on_progress=on_progress,
        commit_batch=commit_batch,
    )


def scan_directory(
    connection: sqlite3.Connection,
    value: Path,
    *,
    on_progress: Callable[[ScanProgress], None] | None = None,
) -> dict[str, Any]:
    """Delegate scanning while preserving the legacy fault-injection seam."""
    _scanning._observation = _observation
    return _scanning.scan_directory(connection, value, on_progress=on_progress)
