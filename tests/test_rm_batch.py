"""rm with several PATHs or --files0-from: one transaction, key ranges, local summary refresh."""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas.cli import app
from flatlas.core import (
    discover_namespace,
    disk_usage,
    open_database,
    register_namespace,
    remove_indexed_subtrees,
    scan_directory,
)

pytestmark = [pytest.mark.plan, pytest.mark.cli, pytest.mark.safety]

FILES = {
    "a/x/deep.bin": 10, "a/y.bin": 20, "a b/z.bin": 40, "a-b/w.bin": 80, "a.b": 160,
    "keep/big/one.bin": 320, "keep/big/two.bin": 640, "keep/small.bin": 1280, "c/d/e/f.bin": 2560,
}


def _make(source: Path) -> None:
    for relative, size in FILES.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)


def _index(db: Path, source: Path) -> None:
    connection = open_database(db)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
    finally:
        connection.close()


def _usage(db: Path, source: Path) -> dict[str, tuple]:
    connection = open_database(db)
    try:
        rows = disk_usage(connection, scope=source, max_depth=3, relative_to=source)
    finally:
        connection.close()
    return {row["path"]: (row["files"], row["entries"], row["logical_size"]) for row in rows}


def test_batch_rm_matches_a_fresh_scan_of_what_remains(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make(source)
    db = tmp_path / "index.sqlite"
    _index(db, source)
    targets = [source / "a", source / "a" / "x", source / "keep" / "big", source / "c" / "d" / "e", source / "nope"]
    result = CliRunner().invoke(app, ["rm", *map(str, targets), "--db", str(db)])
    assert result.exit_code == 1
    assert "path is not indexed" in result.stderr and "nope" in result.stderr
    # "a/x" is inside "a"; the siblings "a b", "a-b", "a.b" sort right after "a" and stay.
    assert json.loads(result.stdout) == {"scopes": 3, "paths_removed": 4 + 3 + 2, "plans_removed": 0}

    for target in targets[:4]:
        shutil.rmtree(target, ignore_errors=True)
    fresh = tmp_path / "fresh.sqlite"
    _index(fresh, source)
    assert _usage(db, source) == _usage(fresh, source)


def test_rm_reads_paths_from_files0_from(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make(source)
    db = tmp_path / "index.sqlite"
    _index(db, source)
    found = CliRunner().invoke(app, ["find", "--db", str(db), str(source), "-name", "big", "-o", "-name", "d", "-prune"])
    listing = "\0".join(found.stdout.splitlines()) + "\0"
    result = CliRunner().invoke(app, ["rm", "--files0-from=-", "--db", str(db)], input=listing.encode())
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["scopes"] == 2
    assert _usage(db, source)["."][2] == sum(FILES.values()) - 320 - 640 - 2560

    assert CliRunner().invoke(app, ["rm", "--db", str(db)]).exit_code != 0
    both = CliRunner().invoke(app, ["rm", str(source), "--files0-from=-", "--db", str(db)], input=b"x\0")
    assert both.exit_code != 0 and "cannot be combined" in str(both.exception)


def test_rm_statements_use_the_path_key_range(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make(source)
    db = tmp_path / "index.sqlite"
    _index(db, source)
    connection = open_database(db)
    statements: list[str] = []
    try:
        connection.set_trace_callback(statements.append)
        remove_indexed_subtrees(connection, [source / "a", source / "keep" / "big"])
        connection.set_trace_callback(None)
        ranged = [sql for sql in statements if "path_key>=" in sql]
        assert any(sql.lstrip().startswith("DELETE FROM path") for sql in ranged)
        for sql in ranged:
            # The trace shows statements with their values bound.
            plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql)]
            # A bare (root_id=?) search would scan the whole root for each subtree.
            assert any("path_key>? AND path_key<?" in step for step in plan), plan
    finally:
        connection.close()
