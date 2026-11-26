"""A full scan purges index rows only in its completed scope transaction."""

import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas import cli, scanning
from flatlas.db import open_database, open_database_readonly
from flatlas.errors import FlatlasError
from flatlas.hashing import ensure_duplicate_hashes
from flatlas.paths import Namespace, discover_namespace, register_namespace
from flatlas.planning import create_dry_run_plan
from flatlas.queries import disk_usage, scan_info
from flatlas.scanning import scan_directory

pytestmark = [pytest.mark.scan, pytest.mark.safety]


@pytest.mark.cli
def test_purge_removes_missing_rows_and_tombstones_but_keeps_observations(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    target = source / "a"
    sibling = source / "ab"
    gone = target / "gone"
    for directory in (gone, sibling):
        directory.mkdir(parents=True)
    for file in (target / "keep1", target / "keep2"):
        file.write_bytes(b"x" * 16384)

    with closing(open_database(db)) as connection:
        root_id = register_namespace(connection, discover_namespace(source))
        first_id = scan_directory(connection, source)["scan_id"]
        ensure_duplicate_hashes(connection, source)
        kept_plan = create_dry_run_plan(connection, root_id)
        kept_hashes = connection.execute("SELECT * FROM file_hash ORDER BY path_id").fetchall()
        assert len(kept_hashes) == 2

        for file in (gone / "copy1", gone / "copy2"):
            file.write_bytes(b"y" * 32768)
        old_missing = gone / "old"
        outside = sibling / "old"
        reappeared = target / "back"
        for file in (old_missing, outside, reappeared):
            file.touch()
        scan_directory(connection, source)
        ensure_duplicate_hashes(connection, source)
        removed_plan = create_dry_run_plan(connection, root_id)
        for file in (old_missing, outside, reappeared):
            file.unlink()
        scan_directory(connection, source)
        shutil.rmtree(gone)
        reappeared.write_bytes(b"back")

        with connection:
            connection.execute(
                """CREATE TRIGGER reject_tombstone BEFORE UPDATE OF state ON path
                   WHEN NEW.state='deleted'
                   BEGIN SELECT RAISE(ABORT, 'purge must directly delete rows'); END"""
            )
        monkeypatch.setattr(cli, "_command_default", lambda *_args: "7d")
        result = CliRunner().invoke(cli.app, ["scan", str(target), "-p", "--format", "json", "--db", str(db)])
        assert result.exit_code == 0, (result.output, result.exception)
        output = json.loads(result.stdout)
        assert output["status"] == "completed" and output["refresh_before_ns"] is None
        assert (output["paths_deleted"], output["paths_purged"]) == (3, 4)
        info = scan_info(connection, output["scan_id"])
        assert info["stats"] == output["stats"]
        assert "purge_missing" in info["stats"]["phases"]
        assert info["scopes"][0]["status"] == "completed"
        assert scan_info(connection, first_id)["status"] == "completed"
        assert [row["path_display"] for row in connection.execute(
            "SELECT path_display FROM path_with_display WHERE state='deleted'"
        )] == [str(outside)]
        assert connection.execute("SELECT count(*) FROM path WHERE parent_path_id IN ("
                                  "SELECT id FROM path_with_display WHERE path_display=?)", (str(target),)).fetchone()[0] == 3
        assert connection.execute("SELECT * FROM file_hash ORDER BY path_id").fetchall() == kept_hashes
        assert {row["id"] for row in connection.execute("SELECT id FROM plan")} == {kept_plan}
        assert kept_plan != removed_plan
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        [usage] = disk_usage(connection, scope=source)
        assert (usage["files"], usage["logical_size"]) == (3, 32768 + 4)
        assert reappeared.read_bytes() == b"back"
        retry = scan_directory(connection, target, purge_deleted=True)
        assert retry["paths_deleted"] == retry["paths_purged"] == 0


@pytest.fixture
def missing_tree(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    db = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    old = source / "old"
    missing = source / "missing"
    for file in (old, missing):
        file.touch()
    with closing(open_database(db)) as connection:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        old.unlink()
        scan_directory(connection, source)
        missing.unlink()
    return db, source, old, missing


def _states(connection: sqlite3.Connection, *paths: Path) -> list[str]:
    return [
        connection.execute("SELECT state FROM path_with_display WHERE path_display=?", (str(path),)).fetchone()[0]
        for path in paths
    ]


def test_partial_purge_keeps_missing_paths_and_tombstones(missing_tree, monkeypatch) -> None:
    db, source, old, missing = missing_tree
    scandir = scanning.os.scandir

    def deny_nested(path):
        if Path(path) == source / "nested":
            raise PermissionError("injected")
        return scandir(path)

    monkeypatch.setattr(scanning.os, "scandir", deny_nested)
    with closing(open_database(db)) as connection:
        result = scan_directory(connection, source, purge_deleted=True)
        assert result["status"] == "partial"
        assert result["paths_deleted"] == result["paths_purged"] == 0
        assert _states(connection, old, missing) == ["deleted", "present"]


@pytest.mark.parametrize("error", [KeyboardInterrupt, sqlite3.OperationalError])
def test_purge_rolls_back_deleted_rows_when_finalizing_fails(missing_tree, monkeypatch, error) -> None:
    db, source, old, missing = missing_tree
    observed = source / "observed"
    observed.touch()

    def stop_after_delete(connection, *_args, **_kwargs):
        assert connection.execute(
            "SELECT count(*) FROM path_with_display WHERE path_display IN (?, ?)", (str(old), str(missing)),
        ).fetchone()[0] == 0
        with closing(open_database_readonly(db)) as reader:
            assert _states(reader, old, missing, observed) == ["deleted", "present", "present"]
        raise error("injected")

    monkeypatch.setattr(scanning, "refresh_directory_summaries", stop_after_delete)
    with closing(open_database(db)) as connection:
        with pytest.raises(error, match="injected"):
            scan_directory(connection, source, purge_deleted=True)
        assert _states(connection, old, missing, observed) == ["deleted", "present", "present"]
        scan_id = connection.execute("SELECT max(id) FROM scan").fetchone()[0]
        info = scan_info(connection, scan_id)
        assert info["status"] == ("cancelled" if error is KeyboardInterrupt else "failed")
        assert info["scopes"][0]["status"] == info["status"]
        assert info["stats"]["paths_deleted"] == info["stats"]["paths_purged"] == 0


@pytest.mark.cli
@pytest.mark.parametrize("namespace_root", [False, True])
def test_purge_reports_overlapping_scan_in_a_terminal(missing_tree, monkeypatch, capsys, namespace_root) -> None:
    db, source, old, missing = missing_tree
    monkeypatch.setattr(scanning, "CHECKPOINT_INTERVAL_S", 0)
    with closing(open_database(db)) as connection:
        other_scope = source / "nested"
        if namespace_root:
            namespace = Namespace(source, discover_namespace(source).platform, "path_anchor", os.fsencode(source))
            register_namespace(connection, namespace)
            monkeypatch.setattr(scanning, "discover_namespace", lambda _path: namespace)
            other_scope = source

        update = cli._ScanProgressDisplay.update

        def scan_nested(display: cli._ScanProgressDisplay, progress: scanning.ScanProgress) -> None:
            update(display, progress)
            if progress.phase == "scanning" and progress.current_directory == "nested":
                with closing(open_database(db)) as other:
                    assert scan_directory(other, other_scope)["status"] == "completed"

        monkeypatch.setattr(cli._ScanProgressDisplay, "update", scan_nested)
        monkeypatch.setattr(cli, "_stderr_is_tty", lambda: True)
        monkeypatch.setattr(cli, "_terminal_columns", lambda: 100)
        monkeypatch.setattr(cli, "_route_termination_signals", lambda: None)
        monkeypatch.setattr(sys, "argv", ["flatlas", "scan", "-p", str(source), "--db", str(db)])

        assert cli.main() == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Failed:" in captured.err
        assert "error: cannot purge deleted paths: scan 4 overlapped this scope" in captured.err
        assert "Traceback" not in captured.err
        assert _states(connection, old, missing) == ["deleted", "present"]
        assert scan_info(connection, 3)["status"] == "failed"


def test_purge_rejects_an_already_running_overlap_with_the_same_clock_tick(missing_tree, monkeypatch) -> None:
    db, source, _old, _missing = missing_tree
    tick = scanning.now_ns()
    monkeypatch.setattr(scanning, "now_ns", lambda: tick)
    monkeypatch.setattr(scanning, "CHECKPOINT_INTERVAL_S", 0)
    attempts = []

    def try_purge(progress: scanning.ScanProgress) -> None:
        if progress.phase == "scanning" and progress.current_directory == ".":
            with closing(open_database(db)) as other:
                assert other.execute("SELECT heartbeat_at_ns FROM scan WHERE id=3").fetchone()[0] == tick
                with pytest.raises(FlatlasError, match="overlapping scan 3 is still running"):
                    scan_directory(other, source / "nested", purge_deleted=True, on_progress=attempts.append)
                assert other.execute("SELECT max(id) FROM scan").fetchone()[0] == 3

    with closing(open_database(db)) as connection:
        assert scan_directory(connection, source, on_progress=try_purge)["status"] == "completed"
    assert not attempts


def test_purge_can_retry_after_an_abandoned_overlap(missing_tree, monkeypatch) -> None:
    db, source, old, missing = missing_tree

    class SimulatedCrash(BaseException):
        pass

    def crash_on_start(_progress: scanning.ScanProgress) -> None:
        raise SimulatedCrash

    with closing(open_database(db)) as connection:
        with pytest.raises(SimulatedCrash):
            scan_directory(connection, source, purge_deleted=True, on_progress=crash_on_start)
        assert scan_info(connection, 3)["status"] == "running"
        monkeypatch.setattr(scanning, "owner_is_alive", lambda *_args: False)
        retry = scan_directory(connection, source, purge_deleted=True)
        assert retry["status"] == "completed"
        assert (retry["paths_deleted"], retry["paths_purged"]) == (1, 2)
        assert scan_info(connection, 3)["scopes"][0]["status"] == "cancelled"
        assert connection.execute("SELECT count(*) FROM path_with_display WHERE path_display IN (?, ?)", (str(old), str(missing))).fetchone()[0] == 0


@pytest.mark.parametrize("ancestor_scan", [True, False])
def test_purge_scope_overlap_preserves_parent_totals(tmp_path: Path, monkeypatch, ancestor_scan: bool) -> None:
    db = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    target = source / "a"
    sibling = source / "ab"
    nested = target / "nested"
    nested.mkdir(parents=True)
    sibling.mkdir()
    (target / "existing").write_bytes(b"keep")
    other_scope = source if ancestor_scan else sibling
    monkeypatch.setattr(scanning, "CHECKPOINT_INTERVAL_S", 0)
    observed = nested / "new"

    def scan_other_scope(progress: scanning.ScanProgress) -> None:
        if progress.phase == "scanning" and progress.current_directory == "nested" and not observed.exists():
            with closing(open_database(db)) as other:
                assert scan_directory(other, other_scope)["status"] == "completed"
            observed.write_bytes(b"new file")

    with closing(open_database(db)) as connection:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        if ancestor_scan:
            with pytest.raises(FlatlasError, match="scan 3 overlapped this scope"):
                scan_directory(connection, target, purge_deleted=True, on_progress=scan_other_scope)
            info = scan_info(connection, 2)
            assert info["status"] == info["scopes"][0]["status"] == "failed"
            assert info["stats"]["paths_purged"] == 0
            assert "scan 3 overlapped" in info["note"]
        else:
            result = scan_directory(connection, target, purge_deleted=True, on_progress=scan_other_scope)
            assert result["status"] == "completed"
            assert result["paths_purged"] == 0

        for scope in (target, source):
            [usage] = disk_usage(connection, scope=scope)
            assert (usage["files"], usage["logical_size"]) == (2, 12)


@pytest.mark.cli
def test_first_scan_allows_full_with_short_purge_option(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").touch()
    with closing(open_database(db)) as connection:
        register_namespace(connection, discover_namespace(source))
    monkeypatch.setattr(scanning, "_purge_missing", lambda *_args: pytest.fail("no earlier descendants to purge"))
    result = CliRunner().invoke(cli.app, ["scan", str(source), "--full", "-p", "--format", "json", "--db", str(db)])
    assert result.exit_code == 0, (result.output, result.exception)
    output = json.loads(result.stdout)
    assert output["status"] == "completed" and output["files_seen"] == 1
    assert output["paths_deleted"] == output["paths_purged"] == 0


@pytest.mark.cli
@pytest.mark.parametrize("options", [("--shallow",), ("--resume",), ("--resume-from", "1"), ("--refresh-before", "1d")])
def test_purge_rejects_incomplete_scan_modes(tmp_path: Path, options: tuple[str, ...]) -> None:
    db = tmp_path / "index.sqlite"
    result = CliRunner().invoke(cli.app, ["scan", str(tmp_path), "--purge-deleted", *options, "--db", str(db)])
    assert result.exit_code == 2
    assert "--purge-deleted requires a full scan" in result.stderr
    assert not db.exists()
