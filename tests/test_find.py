"""flatlas find: GNU find expressions over the index, and du --files0-from."""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas.cli import app
from flatlas.db import open_database
from flatlas.findexpr import parse_find_expression
from flatlas.paths import (
    discover_namespace,
    register_namespace,
)
from flatlas.queries import (
    disk_usage,
    find_paths,
)
from flatlas.scanning import scan_directory

pytestmark = [pytest.mark.query, pytest.mark.cli]


@pytest.fixture
def tree(tmp_path: Path):
    """source/{README.md, a/{build/x.o, src/{m.py, __pycache__/m.pyc}}, b/build/y.o, link -> a}."""
    source = tmp_path / "source"
    for relative, size in (
        ("README.md", 1), ("a/build/x.o", 100), ("a/src/m.py", 10),
        ("a/src/__pycache__/m.pyc", 20), ("b/build/y.o", 1000),
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    try:
        (source / "link").symlink_to(source / "a", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    db = tmp_path / "index.sqlite"
    connection = open_database(db)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
    finally:
        connection.close()
    return db, source


def _find(db: Path, *args: str, exit_code: int = 0) -> list[str]:
    result = CliRunner().invoke(app, ["find", "--db", str(db), *args])
    assert result.exit_code == exit_code, result.output
    return result.stdout.splitlines()


def _relative(lines: list[str], source: Path) -> list[str]:
    return [os.path.relpath(line, source) for line in lines]


def test_default_print_lists_the_tree_in_preorder(tree) -> None:
    db, source = tree
    assert _relative(_find(db, str(source)), source) == [
        ".", "README.md", "a", "a/build", "a/build/x.o", "a/src", "a/src/__pycache__",
        "a/src/__pycache__/m.pyc", "a/src/m.py", "b", "b/build", "b/build/y.o", "link",
    ]


def test_prune_keeps_outermost_matches(tree) -> None:
    db, source = tree
    found = _find(db, str(source), "-type", "d", "(", "-name", "build", "-o", "-name", "__pycache__", ")", "-prune")
    assert _relative(found, source) == ["a/build", "a/src/__pycache__", "b/build"]


def test_prune_or_print_idiom_skips_pruned_subtrees(tree) -> None:
    db, source = tree
    found = _find(db, str(source), "-name", "build", "-prune", "-o", "-type", "f", "-print")
    assert _relative(found, source) == ["README.md", "a/src/__pycache__/m.pyc", "a/src/m.py"]


def test_name_path_type_and_negation(tree) -> None:
    db, source = tree
    assert _relative(_find(db, str(source), "-iname", "*.PY*"), source) == ["a/src/__pycache__/m.pyc", "a/src/m.py"]
    assert _relative(_find(db, str(source), "-path", "*/a/*", "-type", "f"), source) == [
        "a/build/x.o", "a/src/__pycache__/m.pyc", "a/src/m.py",
    ]
    assert _relative(_find(db, str(source), "-type", "l"), source) == ["link"]
    assert _relative(_find(db, str(source), "!", "-type", "d", "-not", "-name", "*.o"), source) == [
        "README.md", "a/src/__pycache__/m.pyc", "a/src/m.py", "link",
    ]


def test_depth_limits(tree) -> None:
    db, source = tree
    assert _relative(_find(db, str(source), "-maxdepth", "1"), source) == [".", "README.md", "a", "b", "link"]
    assert _relative(_find(db, str(source), "-mindepth", "2", "-maxdepth", "2", "-type", "d"), source) == [
        "a/build", "a/src", "b/build",
    ]


def test_paths_are_printed_as_given(tree, monkeypatch) -> None:
    db, source = tree
    monkeypatch.chdir(source)
    assert _find(db, "-maxdepth", "1", "-name", "a") == ["./a"]
    assert _find(db, "a/", "-maxdepth", "1") == ["a/", "a/build", "a/src"]
    # A symlink start point is examined itself unless -H or a trailing slash.
    assert _find(db, "link") == ["link"]
    assert _find(db, "-H", "link", "-maxdepth", "1") == ["link", "link/build", "link/src"]


def test_print0_feeds_du_files0_from(tree, monkeypatch) -> None:
    db, source = tree
    monkeypatch.chdir(source)
    found = CliRunner().invoke(app, ["find", "--db", str(db), ".", "-name", "build", "-prune", "-print0"])
    assert found.exit_code == 0
    assert found.stdout_bytes == b"./a/build\0./b/build\0"
    listing = source.parent / "builds.lst"
    listing.write_bytes(found.stdout_bytes)
    usage = CliRunner().invoke(
        app, ["du", "--db", str(db), "--files0-from", str(listing), "-c", "--format", "json"]
    )
    assert usage.exit_code == 0, usage.output
    rows = json.loads(usage.stdout)
    assert [(row["path"], row["logical_size"]) for row in rows] == [
        ("a/build", 100), ("b/build", 1000), ("total", 1100),
    ]
    piped = CliRunner().invoke(app, ["du", "--db", str(db), "--files0-from=-", "-c"], input=found.stdout_bytes)
    assert piped.exit_code == 0 and "total" in piped.stdout


def test_files0_from_reports_bad_names_and_keeps_going(tree, tmp_path: Path) -> None:
    db, source = tree
    listing = tmp_path / "bad.lst"
    a, b = str(source / "a"), str(source / "b")
    listing.write_bytes(f"{a}\0\0{b}".encode())  # an empty second name, no final NUL
    result = CliRunner().invoke(app, ["du", "--db", str(db), "--files0-from", str(listing), "-c", "--format", "json"])
    assert result.exit_code == 1
    assert f"{listing}:2: invalid zero-length file name" in result.stderr
    rows = json.loads(result.stdout)
    assert [Path(row["path"]).name for row in rows] == ["a", "b", "total"]
    assert rows[-1]["logical_size"] == 130 + 1000

    piped = CliRunner().invoke(app, ["du", "--db", str(db), "--files0-from=-"], input=f"-\0{b}\0".encode())
    assert piped.exit_code == 1 and "no file name of '-' allowed" in piped.stderr
    assert "build" not in piped.stderr and piped.stdout.strip()

    combined = CliRunner().invoke(app, ["du", "--db", str(db), "--files0-from", str(listing), str(source)])
    assert combined.exit_code != 0 and "cannot be combined" in str(combined.exception)


def test_errors_are_reported_and_other_start_points_still_print(tree) -> None:
    db, source = tree
    result = CliRunner().invoke(app, ["find", "--db", str(db), str(source / "nope"), str(source / "b"), "-type", "f"])
    assert result.exit_code == 1
    assert result.stdout.splitlines() == [str(source / "b" / "build" / "y.o")]
    assert "not currently indexed" in result.stderr
    for bad in (["-bogus"], ["-name"], ["(", "-name", "x"], ["-type", "z"], ["-o", "-name", "x"]):
        rejected = CliRunner().invoke(app, ["find", "--db", str(db), str(source), *bad])
        assert rejected.exit_code != 0, bad


def test_scope_lookup_uses_the_path_key_index(tree) -> None:
    db, source = tree
    connection = open_database(db)
    statements: list[str] = []
    try:
        connection.set_trace_callback(statements.append)
        disk_usage(connection, scope=source / "a" / "src")
    finally:
        connection.close()
    # The fallback filters on path_display alone, which scans every path row.
    assert not [sql for sql in statements if "path_display=" in sql and "path_key=" not in sql]


def test_skipped_subtrees_keep_siblings_that_share_a_prefix(tmp_path: Path) -> None:
    # "a b" (0x20), "a-b" (0x2d) and "a.b" sort after "a" but before "a/..." would as text;
    # skipping the key range under "a" must not swallow them.
    source = tmp_path / "source"
    for relative in ("a/x/deep", "a b/y", "a-b/z", "a.b", "b/w"):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    db = tmp_path / "index.sqlite"
    connection = open_database(db)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
    finally:
        connection.close()
    assert _relative(_find(db, str(source), "-name", "a", "-prune", "-o", "-print"), source) == [
        ".", "a b", "a b/y", "a-b", "a-b/z", "a.b", "b", "b/w",
    ]
    assert _relative(_find(db, str(source), "-mindepth", "1", "-maxdepth", "1"), source) == [
        "a", "a b", "a-b", "a.b", "b",
    ]
    assert _relative(_find(db, str(source / "a"), "-maxdepth", "1"), source) == ["a", "a/x"]


def test_deleted_entries_and_their_subtrees_are_not_listed(tree) -> None:
    db, source = tree
    for path in sorted((source / "a" / "src").rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    (source / "a" / "src").rmdir()
    (source / "README.md").unlink()
    connection = open_database(db)
    try:
        scan_directory(connection, source)
    finally:
        connection.close()
    assert _relative(_find(db, str(source)), source) == [
        ".", "a", "a/build", "a/build/x.o", "b", "b/build", "b/build/y.o", "link",
    ]


def test_find_walks_the_path_key_range_without_sorting(tree) -> None:
    db, source = tree
    connection = open_database(db)
    statements: list[str] = []
    try:
        connection.set_trace_callback(statements.append)
        assert len(list(find_paths(connection, str(source), parse_find_expression(["-name", "build", "-prune"])))) == 2
        connection.set_trace_callback(None)
        walks = [sql for sql in statements if "path_key>" in sql]
        assert walks
        for sql in walks:
            plan = " | ".join(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, (1, b"", b"\x01")[:sql.count("?")]))
            assert "TEMP B-TREE" not in plan and "path_key>" in plan, plan
    finally:
        connection.close()


def test_query_commands_run_in_one_read_transaction(tree, monkeypatch) -> None:
    from flatlas import cli

    db, source = tree
    seen: list[bool] = []
    original = cli.disk_usage

    def checked(connection, **kwargs):
        seen.append(connection.in_transaction)
        return original(connection, **kwargs)

    monkeypatch.setattr(cli, "disk_usage", checked)
    result = CliRunner().invoke(app, ["du", "--db", str(db), str(source / "a"), str(source / "b")])
    assert result.exit_code == 0, result.output
    assert seen == [True, True]
