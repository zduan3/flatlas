"""dupes treats hardlink aliases as one copy and reads each inode once."""

import os
from pathlib import Path

import pytest

from flatlas.core import (
    create_dry_run_plan,
    discover_namespace,
    duplicate_groups,
    ensure_duplicate_hashes,
    open_database,
    plan_operations,
    register_namespace,
    scan_directory,
)

pytestmark = [
    pytest.mark.hash,
    pytest.mark.skipif(not hasattr(os, "link") or os.name != "posix", reason="needs POSIX hardlinks"),
]

PAYLOAD = b"same content " * 8


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """``scope/a`` = ``scope/b`` (one inode) and ``scope/c`` hold the same bytes; ``scope/d`` differs;
    ``scope/e`` = ``scope/f`` are aliases with no other copy; ``outside/g`` links to ``c``."""
    scope = tmp_path / "source" / "scope"
    scope.mkdir(parents=True)
    (tmp_path / "source" / "outside").mkdir()
    (scope / "a").write_bytes(PAYLOAD)
    os.link(scope / "a", scope / "b")
    (scope / "c").write_bytes(PAYLOAD)
    (scope / "d").write_bytes(b"x" * len(PAYLOAD))
    (scope / "e").write_bytes(b"only aliases" * 50)
    os.link(scope / "e", scope / "f")
    os.link(scope / "c", tmp_path / "source" / "outside" / "g")
    return scope


def _index(tmp_path: Path, source: Path):
    connection = open_database(tmp_path / "index.sqlite")
    register_namespace(connection, discover_namespace(source))
    scan_directory(connection, source)
    return connection


def test_aliases_count_as_one_copy_and_are_read_once(tmp_path: Path, tree: Path) -> None:
    connection = _index(tmp_path, tree.parent)
    try:
        summary = ensure_duplicate_hashes(connection, tree)
        # e/f have no other same-size inode, so they are not candidates; b reuses a's read.
        assert summary.candidate_files == 4
        assert summary.content_files_read == 3
        [group] = duplicate_groups(connection, scope=tree)
        assert (group["count"], group["inodes"]) == (3, 2)
        assert group["theoretical_savings"] == len(PAYLOAD)
        assert sorted(Path(path["path_display"]).name for path in group["paths"]) == ["a", "b", "c"]
    finally:
        connection.close()


def test_links_outside_the_scope_are_flagged(tmp_path: Path, tree: Path) -> None:
    connection = _index(tmp_path, tree.parent)
    try:
        ensure_duplicate_hashes(connection, tree.parent)
        [scoped] = duplicate_groups(connection, scope=tree)
        assert scoped["external_links"] is True  # c is also outside/g
        [whole] = duplicate_groups(connection, scope=tree.parent)
        assert (whole["count"], whole["inodes"], whole["external_links"]) == (4, 2, False)
    finally:
        connection.close()


def test_plan_skips_aliases_of_the_canonical_file(tmp_path: Path, tree: Path) -> None:
    connection = _index(tmp_path, tree.parent)
    try:
        ensure_duplicate_hashes(connection, tree.parent)
        root_id = connection.execute("SELECT id FROM root").fetchone()["id"]
        operations = plan_operations(connection, create_dry_run_plan(connection, root_id))
        # Lexical order makes outside/g canonical; its alias c is skipped, a and b need relinking.
        assert sorted(Path(op["replacement_path"]).name for op in operations) == ["a", "b"]
        assert {Path(op["canonical_path"]).name for op in operations} == {"g"}
    finally:
        connection.close()
