"""find --format: entry records, and the -size/-entries/-mtime/-status tests."""

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas.cli import app
from flatlas.db import open_database
from flatlas.paths import discover_namespace, register_namespace
from flatlas.records import ENTRY_FIELDS
from flatlas.scanning import scan_directory

pytestmark = [pytest.mark.query, pytest.mark.cli]

SIZES = {"empty": 0, "one": 1, "block": 512, "block1": 513, "kib": 1024, "kib1": 1025, "big/x": 3000, "big/y/z": 100}
DAY = 86_400


@pytest.fixture
def tree(tmp_path: Path):
    source = tmp_path / "source"
    for relative, size in SIZES.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    old = time.time() - 10 * DAY
    os.utime(source / "big" / "x", (old, old))
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


def _names(db: Path, source: Path, *expression: str) -> set[str]:
    return {os.path.relpath(line, source) for line in _find(db, str(source), *expression)}


def test_records_carry_subtree_totals_and_absolute_paths(tree) -> None:
    db, source = tree
    records = [json.loads(line) for line in _find(db, "--format", "jsonl", str(source), "-name", "big*")]
    assert all(tuple(record) == ENTRY_FIELDS for record in records)
    by_name = {Path(record["path"]).name: record for record in records}
    big = by_name["big"]
    assert (big["kind"], big["status"], big["depth"]) == ("dir", "ok", 1)
    assert big["path"] == str(source / "big")
    assert (big["files"], big["entries"], big["logical_size"]) == (2, 4, 3100)  # big, x, y, y/z
    assert big["complete_through_ns"] is not None
    csv_lines = _find(db, "--format", "csv", str(source), "-type", "f", "-name", "one")
    assert csv_lines[0] == ",".join(ENTRY_FIELDS) and len(csv_lines) == 2


def test_records_follow_prune_and_print0(tree) -> None:
    db, source = tree
    expression = ["-type", "d", "-name", "big", "-prune", "-print0"]
    [record] = [json.loads(line) for line in _find(db, "--format", "jsonl", str(source), *expression)]
    assert record["path"] == str(source / "big") and record["entries"] == 4


def test_size_follows_gnu_units_and_rounding(tree) -> None:
    db, source = tree
    files = ["-type", "f"]
    assert _names(db, source, *files, "-size", "0") == {"empty"}  # 512-byte blocks by default
    assert _names(db, source, *files, "-size", "1") == {"one", "block", "big/y/z"}
    assert _names(db, source, *files, "-size", "-1") == {"empty"}  # rounded up: only 0 blocks
    assert _names(db, source, *files, "-size", "1k") == {"one", "block", "block1", "kib", "big/y/z"}
    assert _names(db, source, *files, "-size", "+1k") == {"kib1", "big/x"}
    assert _names(db, source, *files, "-size", "513c") == {"block1"}
    assert _names(db, source, *files, "-size", "-2c") == {"empty", "one"}
    # A directory is measured by its indexed subtree, not its inode.
    assert _names(db, source, "-type", "d", "-size", "+3k") == {".", "big"}
    assert _names(db, source, "-type", "d", "-size", "-3k") == {"big/y"}


def test_entries_mtime_and_combinations(tree) -> None:
    db, source = tree
    assert _names(db, source, "-type", "d", "-entries", "+3") == {".", "big"}
    assert _names(db, source, "-entries", "2") == {"big/y"}
    assert _names(db, source, "-type", "f", "-mtime", "+5") == {"big/x"}
    assert "big/x" not in _names(db, source, "-type", "f", "-mtime", "-1")
    assert _names(db, source, "-type", "f", "-mtime", "-1", "-size", "+1k") == {"kib1"}


def test_status_reports_partial_directories(tree, monkeypatch) -> None:
    db, source = tree
    real_scandir = os.scandir

    def fail_big(path):
        if Path(path) == source / "big" / "y":
            raise PermissionError("injected")
        return real_scandir(path)

    connection = open_database(db)
    try:
        with monkeypatch.context() as patched:
            patched.setattr("flatlas.scanning.os.scandir", fail_big)
            assert scan_directory(connection, source)["status"] == "partial"
    finally:
        connection.close()
    assert _names(db, source, "-status", "part") >= {"big/y"}
    assert "one" in _names(db, source, "-status", "ok,part")
    [record] = [json.loads(line) for line in _find(db, "--format", "jsonl", str(source / "big" / "y"), "-maxdepth", "0")]
    assert record["status"] == "part"


def test_table_output_and_output_file(tree, tmp_path: Path, monkeypatch) -> None:
    db, source = tree
    monkeypatch.chdir(source)
    lines = _find(db, "--format", "table", "--human-readable", ".", "-type", "d")
    assert lines[0].split() == ["T", "S", "SIZE", "N", "E", "ALLOC", "PATH"]
    assert lines[1].split()[0] == "d" and lines[1].endswith(" .")
    assert any(line.endswith(" ./big/y") for line in lines)
    target = tmp_path / "out" / "found.lst"
    assert _find(db, "--output", str(target), ".", "-name", "one", "-print0") == []
    assert target.read_bytes() == b"./one\0"


@pytest.mark.parametrize("expression", [
    ["-size"], ["-size", "1x"], ["-size", "k"], ["-entries", "+x"], ["-mtime", "1.5"], ["-status", "gone"],
])
def test_invalid_metadata_tests_are_rejected(tree, expression) -> None:
    db, source = tree
    result = CliRunner().invoke(app, ["find", "--db", str(db), str(source), *expression])
    assert result.exit_code != 0
    assert "find:" in str(result.exception) or "find:" in result.output
