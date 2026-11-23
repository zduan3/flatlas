"""du entry counts, depth expansion, sorting and top-N."""

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas.cli import app
from flatlas.db import open_database
from flatlas.paths import (
    discover_namespace,
    register_namespace,
)
from flatlas.queries import disk_usage
from flatlas.scanning import scan_directory

pytestmark = [pytest.mark.query]


@pytest.fixture
def tree(tmp_path: Path):
    """source/{big/{x,y/z}, many/{0..4}, link -> big}."""
    source = tmp_path / "source"
    (source / "big" / "y").mkdir(parents=True)
    (source / "big" / "x").write_bytes(b"x" * 1000)
    (source / "big" / "y" / "z").write_bytes(b"z" * 10)
    (source / "many").mkdir()
    for index in range(5):
        (source / "many" / str(index)).write_bytes(b"m")
    try:
        (source / "link").symlink_to(source / "big", target_is_directory=True)
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


def _du(db: Path, *args: str) -> list[dict]:
    result = CliRunner().invoke(app, ["du", "--format", "jsonl", "--db", str(db), *args])
    assert result.exit_code == 0, result.output
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_entries_count_every_kind_including_the_directory(tree) -> None:
    db, source = tree
    [row] = _du(db, str(source))
    # source, big, big/x, big/y, big/y/z, many, many/0..4, link
    assert (row["files"], row["entries"]) == (7, 12)
    [big] = _du(db, str(source / "big"))
    assert big["entries"] == 4
    [file_row] = _du(db, str(source / "big" / "x"))
    assert file_row["entries"] == 1


def test_max_depth_lists_subdirectories_in_path_order(tree) -> None:
    db, source = tree
    rows = _du(db, "-d", "1", str(source))
    assert [(Path(row["path"]).name, row["depth"]) for row in rows] == [
        ("source", 0), ("big", 1), ("many", 1),
    ]
    deeper = _du(db, "-d", "2", str(source))
    assert [Path(row["path"]).name for row in deeper] == ["source", "big", "y", "many"]


def test_sort_and_limit(tree) -> None:
    db, source = tree
    by_entries = _du(db, "-d", "1", "--sort", "entries", "-n", "2", str(source))
    assert [Path(row["path"]).name for row in by_entries] == ["source", "many"]
    by_size = _du(db, "-d", "2", "--sort", "size", str(source))
    assert [Path(row["path"]).name for row in by_size] == ["source", "big", "y", "many"]


def test_total_counts_only_the_arguments(tree) -> None:
    db, source = tree
    rows = _du(db, "-d", "1", "-c", str(source / "big"), str(source / "many"))
    total = rows[-1]
    assert (total["type"], total["path"]) == ("total", None)
    assert (total["files"], total["entries"]) == (7, 4 + 6)


def test_invalid_sort_is_rejected(tree) -> None:
    db, source = tree
    for value in ("bogus", "freed"):
        result = CliRunner().invoke(app, ["du", "--sort", value, "--db", str(db), str(source)])
        assert result.exit_code != 0


def test_migration_backfills_entries_and_keeps_completion(tree) -> None:
    db, source = tree
    connection = sqlite3.connect(db)
    before = connection.execute(
        "SELECT path_id, self_completed_at_ns, subtree_complete_through_ns FROM directory_summary ORDER BY path_id"
    ).fetchall()
    connection.execute("ALTER TABLE directory_summary DROP COLUMN direct_entries")
    connection.execute("ALTER TABLE directory_summary DROP COLUMN subtree_entries")
    connection.execute("DELETE FROM schema_migration WHERE version=4")
    connection.commit()
    connection.close()

    events = []
    connection = open_database(db, on_migration=events.append)
    try:
        assert any(event.version == 4 and event.phase == "completed" for event in events)
        after = connection.execute(
            "SELECT path_id, self_completed_at_ns, subtree_complete_through_ns FROM directory_summary ORDER BY path_id"
        ).fetchall()
        assert [tuple(row) for row in after] == [tuple(row) for row in before]
        [row] = disk_usage(connection, scope=source)
        assert row["entries"] == 12
    finally:
        connection.close()


def test_rescan_updates_entries(tree) -> None:
    if sys.platform == "win32" or not hasattr(os, "mkfifo"):
        pytest.skip("needs a POSIX FIFO")
    db, source = tree
    os.mkfifo(source / "many" / "pipe")
    (source / "many" / "0").unlink()
    connection = open_database(db)
    try:
        scan_directory(connection, source / "many")
        [many] = disk_usage(connection, scope=source / "many")
        [root] = disk_usage(connection, scope=source)
        assert (many["files"], many["entries"]) == (4, 6)
        assert root["entries"] == 12
    finally:
        connection.close()
