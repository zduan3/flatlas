"""Explicit tombstone removal preserves the current index and scan audit."""

import json
import shutil
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas.cli import app
from flatlas.db import open_database
from flatlas.errors import FlatlasError, PathError
from flatlas.hashing import ensure_duplicate_hashes
from flatlas.paths import (
    discover_namespace,
    register_namespace,
    remove_deleted_descendants,
)
from flatlas.planning import create_dry_run_plan
from flatlas.scanning import scan_directory

pytestmark = [pytest.mark.cli, pytest.mark.plan, pytest.mark.safety]


def test_rm_deleted_purges_only_tombstones_in_the_selected_directory(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    target = source / "a"
    neighbor = source / "ab"
    nested = target / "missing" / "nested"
    target.mkdir(parents=True)
    live_files = (target / "a.bin", target / "b.bin")
    for file in live_files:
        file.write_bytes(b"x" * 16384)
    with closing(open_database(db)) as connection:
        root_id = register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        ensure_duplicate_hashes(connection, source)
        kept_plan = create_dry_run_plan(connection, root_id)

        nested.mkdir(parents=True)
        neighbor.mkdir()
        for file in (nested / "one.bin", nested / "two.bin"):
            file.write_bytes(b"y" * 32768)
        (neighbor / "stale.bin").write_bytes(b"z")
        scan_directory(connection, source)
        ensure_duplicate_hashes(connection, source)
        purged_plan = create_dry_run_plan(connection, root_id)
        assert connection.execute(
            "SELECT count(*) FROM plan_operation WHERE plan_id=?", (purged_plan,)
        ).fetchone()[0] == 2

        shutil.rmtree(nested.parent)
        (neighbor / "stale.bin").unlink()
        scan_directory(connection, source)

        def retained_rows():
            queries = (
                "SELECT * FROM path WHERE state='present' ORDER BY id",
                (
                    "SELECT ds.* FROM directory_summary ds JOIN path p ON p.id=ds.path_id "
                    "WHERE p.state='present' ORDER BY p.id"
                ),
                "SELECT h.* FROM file_hash h JOIN path p ON p.id=h.path_id WHERE p.state='present' ORDER BY p.id",
                "SELECT * FROM scan ORDER BY id",
                "SELECT * FROM scan_scope ORDER BY id",
            )
            return [connection.execute(query).fetchall() for query in queries]

        before = retained_rows()
        offline = target.rename(source / "offline")
        runner = CliRunner()
        args = ["rm", "--deleted", str(target), "--format", "json", "--db", str(db)]
        result = runner.invoke(app, args)
        assert result.exit_code == 0, (result.output, result.exception)
        assert json.loads(result.stdout) == {"path": str(target), "paths_removed": 4, "plans_removed": 1}
        assert [row["id"] for row in connection.execute("SELECT id FROM plan")] == [kept_plan]
        assert [row["path_display"] for row in connection.execute(
            "SELECT path_display FROM path WHERE state='deleted'"
        )] == [str(neighbor / "stale.bin")]
        assert connection.execute("SELECT count(*) FROM file_hash").fetchone()[0] == 2
        assert retained_rows() == before
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert [file.read_bytes() for file in sorted(offline.iterdir())] == [b"x" * 16384] * 2

        retry = runner.invoke(app, args)
        assert retry.exit_code == 0, retry.output
        assert json.loads(retry.stdout) == {"path": str(target), "paths_removed": 0, "plans_removed": 0}


def test_rm_deleted_requires_one_present_indexed_directory(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    (source / "gone").mkdir(parents=True)
    file = source / "live.bin"
    file.touch()
    with closing(open_database(db)) as connection:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        (source / "gone").rmdir()
        scan_directory(connection, source)

    runner = CliRunner()
    for args in ([], [str(source), str(file)], ["--files0-from=-", str(source)]):
        result = runner.invoke(app, ["rm", "--deleted", *args, "--db", str(db)])
        assert isinstance(result.exception, FlatlasError) and "exactly one PATH" in str(result.exception)
    for invalid in (file, source / "gone", source / "never-indexed"):
        result = runner.invoke(app, ["rm", "--deleted", str(invalid), "--db", str(db)])
        assert isinstance(result.exception, PathError) and "present indexed directory" in str(result.exception)

    help_result = runner.invoke(app, ["rm", "--help"])
    assert help_result.exit_code == 0
    assert "immediately removes" in help_result.stdout and "flatlas du PATH" in help_result.stdout


def test_purge_rolls_back_if_a_retained_path_references_a_tombstone(tmp_path: Path) -> None:
    source = tmp_path / "source"
    branch = source / "branch"
    branch.mkdir(parents=True)
    (branch / "file").touch()
    with closing(open_database(tmp_path / "index.sqlite")) as connection:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        with connection:
            connection.execute("UPDATE path SET state='deleted' WHERE path_display=?", (str(branch),))
        before = connection.execute("SELECT * FROM path ORDER BY id").fetchall()
        with pytest.raises(FlatlasError, match="retained paths reference"):
            remove_deleted_descendants(connection, source)
        assert connection.execute("SELECT * FROM path ORDER BY id").fetchall() == before
        assert not connection.in_transaction
