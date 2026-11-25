"""Recursive CTEs walk the tree from the CTE side instead of rescanning ``path`` per row.

Without ``sqlite_stat1`` the planner's choice depends only on SQL and indexes, so a tiny
database reproduces the plan used on a million-entry index.
"""

import re
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from flatlas.db import open_database
from flatlas.findexpr import parse_find_expression
from flatlas.hashing import ensure_duplicate_hashes
from flatlas.paths import (
    discover_namespace,
    register_namespace,
    remove_deleted_descendants,
    remove_indexed_subtree,
)
from flatlas.planning import create_dry_run_plan
from flatlas.queries import (
    disk_usage,
    duplicate_groups,
    find_paths,
    largest_files,
)
from flatlas.scanning import scan_directory

pytestmark = [pytest.mark.scan, pytest.mark.query]

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "flatlas"
_NUMBERED = re.compile(r"X'[0-9A-Fa-f]*'|'(?:[^']|'')*'|-?\b\d+\b")
_FULL_PATH_SCAN = re.compile(r"^SCAN (path|p|child)\b|USING (COVERING )?INDEX \w+ \(root_id=\?\)$")


def _normalize(sql: str) -> str:
    text = _NUMBERED.sub("?", " ".join(sql.split()))
    return re.sub(r"\?(, \?)+", "?", text)


def _plan(connection: sqlite3.Connection, sql: str) -> list[tuple[int, int, str]]:
    return [(row[0], row[1], row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + sql)]


def _bad_steps(plan: list[tuple[int, int, str]]) -> list[str]:
    problems = [detail for _id, _parent, detail in plan if _FULL_PATH_SCAN.search(detail)]
    for step_id, _parent, detail in plan:
        if detail == "RECURSIVE STEP":
            children = [child for child in plan if child[1] == step_id]
            if children and not re.fullmatch(r"SCAN \w+", children[0][2]):
                problems.append(f"recursive step starts with {children[0][2]!r}")
    return problems


def test_every_recursive_cte_iterates_from_the_cte(tmp_path: Path) -> None:
    source = tmp_path / "source"
    for relative in ("keep/a", "keep/deep/b", "gone/c", "gone/deeper/d", "forget/e", "stale/f"):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"same bytes")
    connection = open_database(tmp_path / "index.sqlite")
    statements: dict[str, str] = {}

    def record(sql: str) -> None:
        if "RECURSIVE" in sql and not sql.lstrip().upper().startswith("EXPLAIN"):
            statements.setdefault(_normalize(sql), sql)

    try:
        root_id = register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        connection.set_trace_callback(record)
        ensure_duplicate_hashes(connection, source)
        duplicate_groups(connection, scope=source)
        create_dry_run_plan(connection, root_id)
        disk_usage(connection, scope=source, freed=True, max_depth=2)
        largest_files(connection, scope=source)
        list(find_paths(connection, str(source), parse_find_expression(["-maxdepth", "3", "-name", "*"])))
        shutil.rmtree(source / "gone")
        scan_directory(connection, source, shallow=True)
        (source / "keep" / "deep" / "b").unlink()
        scan_directory(connection, source)
        (source / "stale" / "f").unlink()
        scan_directory(connection, source, refresh_before_ns=time.time_ns())
        remove_indexed_subtree(connection, source / "forget")
        connection.set_trace_callback(None)

        expected = sum(path.read_text(encoding="utf-8").count("WITH RECURSIVE") for path in SOURCE_ROOT.glob("*.py"))
        assert len(statements) >= expected, sorted(statements)
        failures = {
            normalized: problems
            for normalized, sql in statements.items()
            if (problems := _bad_steps(_plan(connection, sql)))
        }
        assert not failures, failures
    finally:
        connection.close()


@pytest.mark.plan
@pytest.mark.parametrize("mode", ["scan", "rm"])
@pytest.mark.parametrize("missing", ["a", "b"])
def test_purge_checks_plan_paths_by_primary_key(tmp_path: Path, mode: str, missing: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for file in ("a", "b"):
        (source / file).write_bytes(b"x" * 16384)
    with closing(open_database(tmp_path / "index.sqlite")) as connection:
        root_id = register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        ensure_duplicate_hashes(connection, source)
        plan_id = create_dry_run_plan(connection, root_id)
        assert connection.execute("SELECT count(*) FROM plan_operation WHERE plan_id=?", (plan_id,)).fetchone()[0] == 1
        (source / missing).unlink()
        if mode == "rm":
            scan_directory(connection, source)

        traced: list[str] = []
        connection.set_trace_callback(traced.append)
        if mode == "rm":
            removed = remove_deleted_descendants(connection, source)["paths_removed"]
        else:
            removed = scan_directory(connection, source, purge_deleted=True)["paths_purged"]
        connection.set_trace_callback(None)

        statements = {sql for sql in traced if sql.startswith("DELETE FROM plan WHERE id IN")}
        [statement] = statements
        plan = _plan(connection, statement)
        path_steps = [detail for _, _, detail in plan if detail.startswith(("SCAN p ", "SEARCH p "))]
        assert path_steps == ["SEARCH p USING INTEGER PRIMARY KEY (rowid=?)"] * 2, plan
        assert removed == 1
        assert connection.execute("SELECT count(*) FROM plan WHERE id=?", (plan_id,)).fetchone()[0] == 0
