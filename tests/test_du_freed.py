"""du --freed counts hardlinked inodes once and only frees fully contained inodes."""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas.cli import app
from flatlas.core import (
    discover_namespace,
    hardlink_usage,
    indexed_scope_ids,
    open_database,
    register_namespace,
    scan_directory,
)

pytestmark = [
    pytest.mark.query,
    pytest.mark.skipif(not hasattr(os, "link") or os.name != "posix", reason="needs POSIX hardlinks"),
]


@pytest.fixture
def linked_tree(tmp_path: Path):
    """``x/a`` and ``x/b`` share an inode with ``y/c``; ``x/d`` and ``y/e`` are single."""
    source = tmp_path / "source"
    (source / "x").mkdir(parents=True)
    (source / "y").mkdir()
    (source / "x" / "a").write_bytes(b"A" * 100)
    os.link(source / "x" / "a", source / "x" / "b")
    os.link(source / "x" / "a", source / "y" / "c")
    (source / "x" / "d").write_bytes(b"D" * 10)
    (source / "y" / "e").write_bytes(b"E" * 1)
    db = tmp_path / "index.sqlite"
    connection = open_database(db)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
    finally:
        connection.close()
    return db, source


def _usage(db: Path, *scopes: Path) -> dict:
    connection = open_database(db)
    try:
        return hardlink_usage(connection, indexed_scope_ids(connection, list(scopes)))
    finally:
        connection.close()


def test_partial_alias_set_is_not_freed(linked_tree) -> None:
    db, source = linked_tree
    usage = _usage(db, source / "x")
    assert usage["unique_logical_size"] == 110  # a/b counted once
    assert (usage["freed_logical_size"], usage["freed_files"]) == (10, 1)  # c keeps the shared inode
    assert usage["unknown_link_files"] == 0


def test_union_frees_an_inode_whose_links_are_split_across_scopes(linked_tree) -> None:
    db, source = linked_tree
    usage = _usage(db, source / "x", source / "y")
    assert (usage["unique_logical_size"], usage["freed_logical_size"], usage["freed_files"]) == (111, 111, 3)
    # Nested scopes are not double counted.
    assert _usage(db, source, source / "x") == usage


def test_cli_freed_columns_and_total(linked_tree) -> None:
    db, source = linked_tree
    result = CliRunner().invoke(
        app, ["du", "--freed", "-c", "--format", "json", "--db", str(db), str(source / "x"), str(source / "y")]
    )
    assert result.exit_code == 0, result.output
    x, y, total = json.loads(result.stdout)
    assert (x["logical_size"], x["freed_logical_size"]) == (210, 10)
    assert (y["logical_size"], y["freed_logical_size"]) == (101, 1)
    assert total["path"] == "total"
    assert (total["files"], total["logical_size"], total["freed_logical_size"]) == (5, 311, 111)

    table = CliRunner().invoke(app, ["du", "--freed", "--db", str(db), str(source / "x")])
    assert table.exit_code == 0, table.output
    assert table.output.splitlines()[0].split()[-3:] == ["UNIQ(B)", "FREED(B)", "PATH"]


def test_total_without_freed_sums_rows(linked_tree) -> None:
    db, source = linked_tree
    result = CliRunner().invoke(app, ["du", "-c", "--format", "json", "--db", str(db), str(source / "x")])
    assert result.exit_code == 0, result.output
    x, total = json.loads(result.stdout)
    assert "freed_logical_size" not in total
    assert (total["files"], total["logical_size"]) == (x["files"], x["logical_size"])
