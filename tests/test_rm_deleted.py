"""Explicit tombstone removal preserves the current index and scan audit."""

import json
import os
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
    indexed_key,
    register_namespace,
    remove_deleted_descendants,
    remove_indexed_subtree,
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
            "SELECT path_display FROM path_with_display WHERE state='deleted'"
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
            connection.execute("UPDATE path SET state='deleted' WHERE id=(SELECT id FROM path_with_display WHERE path_display=?)", (str(branch),))
        before = connection.execute("SELECT * FROM path ORDER BY id").fetchall()
        with pytest.raises(FlatlasError, match="retained paths reference"):
            remove_deleted_descendants(connection, source)
        assert connection.execute("SELECT * FROM path ORDER BY id").fetchall() == before
        assert not connection.in_transaction


def test_indexed_key_skips_non_utf8_roots_and_respects_case_sensitivity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep.bin").touch()
    (source / "gone.bin").touch()
    with closing(open_database(tmp_path / "index.sqlite")) as connection:
        connection.execute(
            """INSERT INTO root(root_path_raw, root_path_display, platform, namespace_kind,
               namespace_id_raw, filesystem_type, case_sensitive, created_at_ns, updated_at_ns)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                b"/mnt/root\xff",
                "/mnt/root\\xff",
                "linux",
                "posix_mount",
                b"/mnt/root\xff",
                None,
                1,
                1,
                1,
            ),
        )
        connection.commit()
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        (source / "gone.bin").unlink()
        scan_directory(connection, source)

        assert indexed_key(connection, source) is not None
        sensitive_id = connection.execute(
            """INSERT INTO root(root_path_raw, root_path_display, platform, namespace_kind,
               namespace_id_raw, filesystem_type, case_sensitive, created_at_ns, updated_at_ns)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                b"/mnt/CaseRoot",
                "/mnt/CaseRoot",
                "linux",
                "posix_mount",
                b"/mnt/CaseRoot",
                None,
                1,
                1,
                1,
            ),
        ).lastrowid
        assert indexed_key(connection, Path("/mnt/CaseRoot/file")) == (sensitive_id, b"file\x00")
        mismatched = indexed_key(connection, Path("/mnt/caseroot/file"))
        assert mismatched is None or mismatched[0] != sensitive_id
        insensitive_id = connection.execute(
            """INSERT INTO root(root_path_raw, root_path_display, platform, namespace_kind,
               namespace_id_raw, filesystem_type, case_sensitive, created_at_ns, updated_at_ns)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                b"/mnt/InsensitiveRoot",
                "/mnt/InsensitiveRoot",
                "linux",
                "posix_mount",
                b"/mnt/InsensitiveRoot",
                None,
                0,
                1,
                1,
            ),
        ).lastrowid
        assert indexed_key(connection, Path("/mnt/insensitiveroot/file")) == (insensitive_id, b"file\x00")
        if os.name == "nt":
            assert source.drive and source.drive[0].isupper()
            lower_source = Path(str(source).replace(source.drive, source.drive.lower(), 1))
            lower_keep = Path(str(source / "keep.bin").replace(source.drive, source.drive.lower(), 1))
            assert indexed_key(connection, lower_source) == indexed_key(connection, source)
            assert remove_deleted_descendants(connection, lower_source)["paths_removed"] == 1
            assert remove_indexed_subtree(connection, lower_keep)["paths_removed"] == 1
        else:
            assert remove_deleted_descendants(connection, source)["paths_removed"] == 1
            assert remove_indexed_subtree(connection, source / "keep.bin")["paths_removed"] == 1


@pytest.mark.skipif(os.name != "nt", reason="needs Windows drive letter case-folding")
def test_windows_lowercase_drive_matches_registered_root_for_rm_and_purge(tmp_path: Path) -> None:
    source = tmp_path / "source"
    sub = source / "branch"
    sub.mkdir(parents=True)
    (sub / "keep.bin").touch()
    (sub / "gone.bin").touch()
    with closing(open_database(tmp_path / "index.sqlite")) as connection:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        (sub / "gone.bin").unlink()
        scan_directory(connection, source)

        assert source.drive and source.drive[0].isupper()
        lower_sub = Path(str(sub).replace(source.drive, source.drive.lower(), 1))
        assert remove_deleted_descendants(connection, lower_sub)["paths_removed"] == 1
        assert remove_indexed_subtree(connection, lower_sub)["paths_removed"] == 2
