import csv
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas import cli, errors, hashing, scanning
from flatlas.cli import app
from flatlas.db import open_database
from flatlas.findexpr import parse_find_expression
from flatlas.hashing import ensure_duplicate_hashes
from flatlas.paths import (
    discover_namespace,
    register_namespace,
    remove_indexed_subtree,
)
from flatlas.planning import (
    create_dry_run_plan,
    list_plans,
    plan_operations,
)
from flatlas.queries import (
    disk_usage,
    duplicate_groups,
    find_paths,
    largest_files,
    list_child_directories,
    list_scans,
    scan_info,
)
from flatlas.scanning import (
    resume_selection,
    resume_selection_from_scan,
    scan_directory,
)
from flatlas.summaries import refresh_directory_summaries
from flatlas.version import VersionInfo


def _records(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines()]


@pytest.fixture
def external_file_allocation(monkeypatch) -> None:
    """Keep hash/plan fixtures eligible regardless of NTFS resident storage."""
    monkeypatch.setattr(
        "flatlas.scanning.allocated_size", lambda _path, metadata, **_kwargs: 4096 if metadata.st_size else 0,
    )


def make_connection(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    connection = open_database(tmp_path / "index.sqlite")
    register_namespace(connection, discover_namespace(source))
    return connection, source


@pytest.mark.version
@pytest.mark.cli
def test_version_reports_commit_and_date_without_database(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "current_version",
        lambda: VersionInfo(version="0.2.0", commit="b1f9a4ff8e34", commit_date="2026-09-16", dirty=True),
    )
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == "flatlas 0.2.0 (b1f9a4ff8e34, 2026-09-16, dirty)\n"


@pytest.mark.db
@pytest.mark.query
@pytest.mark.cli
@pytest.mark.safety
def test_init_registration_does_not_scan_and_roots_has_df_alias(tmp_path: Path, monkeypatch) -> None:
    connection, _ = make_connection(tmp_path)
    try:
        assert connection.execute("SELECT count(*) FROM scan").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM path").fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.scan
@pytest.mark.hash
@pytest.mark.plan
@pytest.mark.safety
def test_full_scan_detects_duplicates_and_builds_immutable_plan(tmp_path: Path, external_file_allocation) -> None:
    connection, source = make_connection(tmp_path)
    try:
        (source / "left.bin").write_bytes(b"same payload")
        (source / "right.bin").write_bytes(b"same payload")
        result = scan_directory(connection, source)
        assert result["status"] == "completed"
        ensure_duplicate_hashes(connection, source)
        groups = duplicate_groups(connection)
        assert len(groups) == 1
        assert groups[0]["count"] == 2
        assert groups[0]["theoretical_savings"] == len(b"same payload")
        root_id = connection.execute("SELECT id FROM root").fetchone()[0]
        plan_id = create_dry_run_plan(connection, root_id)
        assert len(plan_operations(connection, plan_id)) == 1
        assert connection.execute("SELECT status, immutable_at_ns FROM plan WHERE id=?", (plan_id,)).fetchone()[0] == "dry_run"
    finally:
        connection.close()


@pytest.mark.plan
@pytest.mark.cli
def test_plan_show_without_id_lists_summaries_without_operations(tmp_path: Path, external_file_allocation) -> None:
    database = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    source.mkdir()
    (source / "left.bin").write_bytes(b"same payload")
    (source / "right.bin").write_bytes(b"same payload")
    connection = open_database(database)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        ensure_duplicate_hashes(connection, source)
        root_id = connection.execute("SELECT id FROM root").fetchone()["id"]
        plan_id = create_dry_run_plan(connection, root_id)
        summaries = list_plans(connection)
        assert len(summaries) == 1
        assert summaries[0]["id"] == plan_id
        assert summaries[0]["operation_count"] == 1
        assert summaries[0]["theoretical_savings"] == len(b"same payload")
        assert "canonical_path" not in summaries[0]
    finally:
        connection.close()

    result = CliRunner().invoke(app, ["plan", "show", "--format", "jsonl", "--db", str(database)])
    assert result.exit_code == 0
    payload = _records(result.stdout)
    assert payload[0]["type"] == "plan"
    assert payload[0]["id"] == plan_id
    assert payload[0]["operation_count"] == 1
    assert "canonical_path" not in payload[0]
    assert "replacement_path" not in payload[0]

@pytest.mark.scan
@pytest.mark.hash
@pytest.mark.safety
def test_subtree_scan_can_match_existing_index_and_complete_scope_marks_deletion(tmp_path: Path, external_file_allocation) -> None:
    connection, source = make_connection(tmp_path)
    try:
        (source / "old.bin").write_bytes(b"shared")
        scan_directory(connection, source)
        incoming = source / "incoming"
        incoming.mkdir()
        (incoming / "new.bin").write_bytes(b"shared")
        scan_directory(connection, incoming)
        ensure_duplicate_hashes(connection, source)
        assert duplicate_groups(connection)[0]["count"] == 2
        (incoming / "new.bin").unlink()
        scan_directory(connection, incoming)
        row = connection.execute("SELECT state FROM path_with_display WHERE path_display=?", (str(incoming / "new.bin"),)).fetchone()
        assert row["state"] == "deleted"
        old = connection.execute("SELECT state FROM path_with_display WHERE path_display=?", (str(source / "old.bin"),)).fetchone()
        assert old["state"] == "present"
        assert disk_usage(connection, scope=source)[0]["logical_size"] == len(b"shared")
    finally:
        connection.close()


@pytest.mark.db
@pytest.mark.plan
@pytest.mark.safety
def test_rm_recursively_removes_only_indexed_paths_without_touching_files(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    target = source / "target"
    source.mkdir()
    target.mkdir()
    nested = target / "nested"
    nested.mkdir()
    indexed_file = nested / "indexed.bin"
    indexed_file.write_bytes(b"payload")
    retained_file = source / "retained.bin"
    retained_file.write_bytes(b"retain")
    absent = source / "already-absent"
    absent.mkdir()
    connection = open_database(database)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
    finally:
        connection.close()

    result = CliRunner().invoke(app, ["rm", "--format", "json", str(target), "--db", str(database)])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "path": str(target),
        "paths_removed": 3,
        "plans_removed": 0,
    }
    assert target.is_dir()
    assert indexed_file.read_bytes() == b"payload"
    assert retained_file.read_bytes() == b"retain"
    absent.rmdir()
    absent_result = CliRunner().invoke(app, ["rm", "--format", "json", str(absent), "--db", str(database)])
    assert absent_result.exit_code == 0
    assert json.loads(absent_result.stdout)["paths_removed"] == 1

    connection = open_database(database)
    try:
        assert connection.execute("SELECT count(*) FROM path_with_display WHERE path_display LIKE ?", (f"{target}%",)).fetchone()[0] == 0
        assert connection.execute("SELECT state FROM path_with_display WHERE path_display=?", (str(retained_file),)).fetchone()["state"] == "present"
        assert disk_usage(connection, scope=source)[0]["logical_size"] == len(b"retain")
    finally:
        connection.close()


@pytest.mark.db
@pytest.mark.plan
@pytest.mark.safety
def test_rm_discards_dry_run_plans_that_reference_removed_paths(tmp_path: Path, external_file_allocation) -> None:
    connection, source = make_connection(tmp_path)
    target = source / "target"
    target.mkdir()
    try:
        (target / "a.bin").write_bytes(b"same")
        (source / "b.bin").write_bytes(b"same")
        scan_directory(connection, source)
        ensure_duplicate_hashes(connection, source)
        root_id = connection.execute("SELECT id FROM root").fetchone()["id"]
        plan_id = create_dry_run_plan(connection, root_id)
        summary = remove_indexed_subtree(connection, target)
        assert summary["plans_removed"] == 1
        assert connection.execute("SELECT count(*) FROM plan WHERE id=?", (plan_id,)).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM plan_operation WHERE plan_id=?", (plan_id,)).fetchone()[0] == 0
    finally:
        connection.close()

@pytest.mark.query
@pytest.mark.cli
@pytest.mark.output
def test_du_defaults_to_headered_summary_with_relative_path(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.bin").write_bytes(b"a")
    (source / "two.bin").write_bytes(b"bc")
    connection = open_database(database)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        rows = disk_usage(connection, scope=source)
        assert rows[0]["path"] == str(source)
        assert rows[0]["files"] == 2
        assert rows[0]["logical_size"] == 3
    finally:
        connection.close()

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["du", str(source), "--db", str(database)])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert "\t" not in result.stdout
    assert lines[0].split()[:3] == ["S", "SIZE(B)", "N"]
    assert lines[0].endswith("PATH")
    assert lines[1].split()[:3] == ["ok", "3", "2"]
    assert lines[1].endswith("  source")
    assert lines[0].index("PATH") == lines[1].index("source")

    json_result = runner.invoke(app, ["du", str(source), "--format", "jsonl", "--db", str(database)])
    assert json_result.exit_code == 0
    # Machine output carries the absolute path; only the table is relative.
    assert _records(json_result.stdout)[0]["path"] == str(source)
    assert _records(json_result.stdout)[0]["status"] == "ok"

    monkeypatch.chdir(source)
    default_result = runner.invoke(app, ["du", "--format", "jsonl", "--db", str(database)])
    assert default_result.exit_code == 0, default_result.output
    assert _records(default_result.stdout)[0]["path"] == str(source)
    assert _records(default_result.stdout)[0]["logical_size"] == 3
    monkeypatch.chdir(tmp_path)

    multiple_result = runner.invoke(
        app,
        ["du", "source/one.bin", "source/two.bin", "--format", "jsonl", "--db", str(database)],
    )
    assert multiple_result.exit_code == 0
    assert [row["logical_size"] for row in _records(multiple_result.stdout)] == [1, 2]

    help_result = runner.invoke(app, ["du", "--help"])
    assert help_result.exit_code == 0
    assert "SIZE(B)=logical bytes" in help_result.stdout
    assert "N=file count" in help_result.stdout
    assert "ALLOC(B)=allocated bytes" in help_result.stdout
    assert "ok=scanned" in help_result.stdout


@pytest.mark.query
@pytest.mark.cli
def test_du_reports_unscanned_directories_without_inventing_sizes(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    source.mkdir()
    new_directory = source / "new"
    new_directory.mkdir()
    (new_directory / "unknown.bin").write_bytes(b"unknown")
    connection = open_database(database)
    try:
        register_namespace(connection, discover_namespace(source))
        root_row = disk_usage(connection, scope=source)[0]
        assert root_row["status"] == "new"
        assert root_row["files"] is None
        assert root_row["logical_size"] is None
        initial = CliRunner().invoke(app, ["du", str(source), "--format", "jsonl", "--db", str(database)])
        assert initial.exit_code == 0, initial.output
        assert _records(initial.stdout)[0]["status"] == "new"
        scan_directory(connection, source)
        later = source / "later"
        later.mkdir()
        assert disk_usage(connection, scope=later)[0]["status"] == "new"
    finally:
        connection.close()

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["du", "source", "source/later", "--db", str(database)])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split()[:3] == ["S", "SIZE(B)", "N"]
    assert lines[1].split()[0] == "ok"
    assert lines[2].split()[:3] == ["new", "-", "-"]
    assert lines[2].endswith(str(Path("source/later")))

    json_result = runner.invoke(app, ["du", "source/later", "--format", "jsonl", "--db", str(database)])
    assert json_result.exit_code == 0, json_result.output
    assert _records(json_result.stdout) == [{
        "type": "entry", "path": str(tmp_path / "source" / "later"), "kind": "dir", "status": "new",
        "logical_size": None, "allocated_size": None, "files": None, "entries": None,
        "mtime_ns": None, "complete_through_ns": None, "depth": 0,
    }]

    default_result = runner.invoke(app, ["du", "--format", "jsonl", "--db", str(database)])
    assert default_result.exit_code == 0, default_result.output
    assert _records(default_result.stdout)[0]["path"] == str(tmp_path)
    assert _records(default_result.stdout)[0]["status"] == "new"


@pytest.mark.scan
@pytest.mark.query
def test_du_uses_ls_coverage_status_for_partial_directory(tmp_path: Path, monkeypatch) -> None:
    connection, source = make_connection(tmp_path)
    nested = source / "nested"
    nested.mkdir()
    (nested / "file.bin").write_bytes(b"known")
    try:
        scan_directory(connection, source)
        real_scandir = os.scandir

        def fail_nested(path):
            if Path(path) == nested:
                raise PermissionError("injected coverage failure")
            return real_scandir(path)

        with monkeypatch.context() as failed_scan:
            failed_scan.setattr("flatlas.scanning.os.scandir", fail_nested)
            assert scan_directory(connection, source)["status"] == "partial"

        ls_status = next(row["status"] for row in list_child_directories(connection, scope=source) if Path(row["path"]).name == "nested")
        du_row = disk_usage(connection, scope=nested)[0]
        assert ls_status == du_row["status"] == "part"
        assert du_row["logical_size"] == 5
    finally:
        connection.close()


@pytest.mark.query
@pytest.mark.cli
def test_largest_is_scoped_to_path_and_defaults_to_cwd(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    source.mkdir()
    inside = source / "inside"
    inside.mkdir()
    nested = inside / "nested"
    nested.mkdir()
    (source / "outside.bin").write_bytes(b"x" * 100)
    (inside / "small.bin").write_bytes(b"x")
    (nested / "large.bin").write_bytes(b"xxx")

    connection = open_database(database)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        assert [row["path"] for row in largest_files(connection, scope=inside)] == [
            str(nested / "large.bin"),
            str(inside / "small.bin"),
        ]
        assert [row["path"] for row in largest_files(connection, scope=inside, limit=1)] == [
            str(nested / "large.bin")
        ]
        assert [row["path"] for row in largest_files(connection, scope=inside / "small.bin")] == [
            str(inside / "small.bin")
        ]
    finally:
        connection.close()

    runner = CliRunner()
    explicit = runner.invoke(app, ["largest", str(inside), "--db", str(database)])
    assert explicit.exit_code == 0
    lines = explicit.stdout.splitlines()
    assert lines[0].lstrip().startswith("SIZE(B)")
    assert lines[0].endswith("PATH")
    assert lines[1].endswith(str(Path("nested") / "large.bin"))
    assert lines[2].endswith("small.bin")

    monkeypatch.chdir(inside)
    defaulted = runner.invoke(app, ["largest", "--limit", "1", "--format", "jsonl", "--db", str(database)])
    assert defaulted.exit_code == 0
    assert [row["path"] for row in _records(defaulted.stdout)] == [str(nested / "large.bin")]


@pytest.mark.query
@pytest.mark.cli
def test_largest_defaults_to_ten_and_accepts_short_limit_alias(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    source.mkdir()
    for size in range(1, 13):
        (source / f"{size:02d}.bin").write_bytes(b"x" * size)

    connection = open_database(database)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        # The query has no implicit limit; only the CLI table shows 10 by default.
        assert len(largest_files(connection, scope=source)) == 12
    finally:
        connection.close()

    runner = CliRunner()
    default_result = runner.invoke(app, ["largest", str(source), "--format", "jsonl", "--db", str(database)])
    assert default_result.exit_code == 0
    assert [row["logical_size"] for row in _records(default_result.stdout)] == list(range(12, 0, -1))
    table_result = runner.invoke(app, ["largest", str(source), "--db", str(database)])
    assert table_result.exit_code == 0
    assert len(table_result.stdout.splitlines()) == 1 + 10

    short_result = runner.invoke(app, ["largest", str(source), "-n", "2", "--format", "jsonl", "--db", str(database)])
    assert short_result.exit_code == 0
    assert [row["logical_size"] for row in _records(short_result.stdout)] == [12, 11]

    help_result = runner.invoke(app, ["largest", "--help"])
    assert help_result.exit_code == 0
    assert "-n" in help_result.stdout
    assert "--limit" in help_result.stdout

@pytest.mark.scan
@pytest.mark.query
def test_ls_marks_live_child_directory_scan_statuses(tmp_path: Path, monkeypatch) -> None:
    connection, source = make_connection(tmp_path)
    try:
        empty = source / "empty"
        empty.mkdir()
        complete = source / "complete"
        complete.mkdir()
        (complete / "file.bin").write_bytes(b"done")
        nested = source / "nested"
        nested.mkdir()
        (nested / "child.bin").write_bytes(b"ab")
        scan_directory(connection, source)

        real_scandir = __import__("os").scandir

        def fail_nested(path):
            if Path(path) == nested:
                raise PermissionError("injected ls coverage failure")
            return real_scandir(path)

        with monkeypatch.context() as partial_scan:
            partial_scan.setattr("flatlas.scanning.os.scandir", fail_nested)
            assert scan_directory(connection, nested)["status"] == "partial"

        empty.rmdir()
        (source / "new").mkdir()
        rows = list_child_directories(connection, scope=source)
        by_name = {Path(row["path"]).name: row for row in rows}
        assert list(by_name) == ["complete", "empty", "nested", "new"]
        assert by_name["complete"]["status"] == "ok"
        assert by_name["complete"]["logical_size"] == 4
        assert by_name["complete"]["complete_through_ns"] is not None
        assert by_name["empty"]["status"] == "gone"
        assert by_name["empty"]["logical_size"] == 0
        assert by_name["empty"]["complete_through_ns"] is not None
        assert by_name["nested"]["status"] == "part"
        assert by_name["nested"]["files"] == 1
        assert by_name["nested"]["complete_through_ns"] is not None
        assert by_name["new"]["status"] == "new"
        assert by_name["new"]["files"] is None
        assert by_name["new"]["complete_through_ns"] is None
        empty_state = connection.execute(
            "SELECT state FROM path_with_display WHERE path_display=?",
            (str(empty),),
        ).fetchone()["state"]
        assert empty_state == "present"
    finally:
        connection.close()


@pytest.mark.scan
@pytest.mark.query
def test_ls_does_not_treat_equal_scan_times_as_completed_coverage(tmp_path: Path, monkeypatch) -> None:
    connection, source = make_connection(tmp_path)
    parent = source / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    complete = source / "complete"
    complete.mkdir()
    (child / "old.bin").write_bytes(b"old")
    (complete / "done.bin").write_bytes(b"done")
    monkeypatch.setattr("flatlas.scanning.now_ns", lambda: 1_700_000_000_000_000_000)
    real_scandir = os.scandir

    def fail_child(path):
        if Path(path) == child:
            raise PermissionError("injected child coverage failure")
        return real_scandir(path)

    try:
        assert scan_directory(connection, source)["status"] == "completed"
        with monkeypatch.context() as partial_scan:
            partial_scan.setattr("flatlas.scanning.os.scandir", fail_child)
            assert scan_directory(connection, source)["status"] == "partial"

        scans = connection.execute("SELECT id, started_at_ns FROM scan ORDER BY id").fetchall()
        assert scans[0]["started_at_ns"] == scans[1]["started_at_ns"]
        statuses = {Path(row["path"]).name: row["status"] for row in list_child_directories(connection, scope=source)}
        assert statuses == {"complete": "ok", "parent": "ok"}
        assert disk_usage(connection, scope=parent)[0]["status"] == "part"
        child_rows = list_child_directories(connection, scope=parent)
        assert Path(child_rows[0]["path"]).name == "child"
        assert child_rows[0]["status"] == "part"
        assert connection.execute(
            "SELECT state FROM path_with_display WHERE path_display=?", (str(child / "old.bin"),)
        ).fetchone()["state"] == "present"
    finally:
        connection.close()


@pytest.mark.scan
@pytest.mark.query
def test_ls_refresh_status_uses_directory_itself_without_descendant_checks(tmp_path: Path, monkeypatch) -> None:
    connection, source = make_connection(tmp_path)
    parent = source / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    previous_time = 1_700_000_000_000_000_000
    real_scandir = os.scandir

    def fail_child(path):
        if Path(path) == child:
            raise PermissionError("injected refresh coverage failure")
        return real_scandir(path)

    try:
        monkeypatch.setattr("flatlas.scanning.now_ns", lambda: previous_time)
        scan_directory(connection, source)
        monkeypatch.setattr("flatlas.scanning.now_ns", lambda: previous_time + 10)
        with monkeypatch.context() as partial_scan:
            partial_scan.setattr("flatlas.scanning.os.scandir", fail_child)
            assert scan_directory(connection, source, refresh_before_ns=previous_time + 5)["status"] == "partial"
        assert list_child_directories(connection, scope=source)[0]["status"] == "ok"
        assert list_child_directories(connection, scope=parent)[0]["status"] == "part"
        assert disk_usage(connection, scope=parent)[0]["status"] == "part"
    finally:
        connection.close()


@pytest.mark.query
@pytest.mark.scan
@pytest.mark.cli
@pytest.mark.output
def test_scan_reports_file_count_and_logical_size_progress(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    source.mkdir()
    nested = source / "nested" / "deeper"
    nested.mkdir(parents=True)
    (source / "one.bin").write_bytes(b"a")
    (source / "two.bin").write_bytes(b"bc")
    (nested / "three.bin").write_bytes(b"def")
    connection = open_database(database)
    try:
        register_namespace(connection, discover_namespace(source))
    finally:
        connection.close()

    monkeypatch.setattr("flatlas.cli._stderr_is_tty", lambda: True)
    result = CliRunner().invoke(app, ["scan", "--format", "json", str(source), "--db", str(database)])
    assert result.exit_code == 0
    summary = json.loads(result.stdout)
    assert summary["files_seen"] == 3
    assert summary["dirs_seen"] == 3
    assert summary["logical_bytes_seen"] == 6
    assert "Scanning: 0 files · 1 dirs · 0 B · ." in result.stderr
    assert str(source.resolve()) not in result.stderr
    assert "Completed: 3 files · 3 dirs · 6 B" in result.stderr


@pytest.mark.scan
@pytest.mark.cli
def test_refresh_before_parser_accepts_absolute_time_and_duration() -> None:
    current = 2_000_000_000_000_000_000

    assert cli._parse_refresh_before("7d", current_ns=current) == current - 7 * 24 * 60 * 60 * 1_000_000_000
    assert cli._parse_refresh_before("1970-01-01T00:00:01Z", current_ns=current) == 1_000_000_000


@pytest.mark.scan
@pytest.mark.cli
@pytest.mark.output
def test_scan_progress_truncates_long_directory_prefix_by_display_width(monkeypatch) -> None:
    output: list[str] = []
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda fallback: os.terminal_size((40, 24)))
    monkeypatch.setattr(cli.typer, "echo", lambda message="", **_kwargs: output.append(message))

    display = cli._ScanProgressDisplay(enabled=True, interval=0)
    display.update(scanning.ScanProgress("scanning", 0, 1, 0, "目录" * 20))

    assert len(output) == 1
    assert output[0].startswith("\r\x1b[2KScanning: 0 files · 1 dirs · 0 B · 目")
    line = output[0].removeprefix("\r\x1b[2K")
    assert line.endswith("…")
    assert cli._display_width(line) <= 39


@pytest.mark.scan
@pytest.mark.cli
@pytest.mark.output
def test_scan_progress_uses_current_directory_on_timed_refresh(monkeypatch) -> None:
    output: list[str] = []
    times = iter((1.0, 1.01, 1.11))
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(cli.typer, "echo", lambda message="", **_kwargs: output.append(message))

    display = cli._ScanProgressDisplay(enabled=True, interval=0.1)
    display.update(scanning.ScanProgress("scanning", 0, 1, 0, "."))
    display.update(scanning.ScanProgress("scanning", 0, 2, 0, "first"))
    display.update(scanning.ScanProgress("scanning", 0, 3, 0, "second"))

    assert len(output) == 2
    assert output[0].endswith(" · .")
    assert "first" not in "".join(output)
    assert output[1].endswith(" · second")


@pytest.mark.hash
@pytest.mark.query
@pytest.mark.cli
@pytest.mark.output
def test_dupes_scopes_grouped_output_to_path_and_defaults_to_cwd(tmp_path: Path, monkeypatch, external_file_allocation) -> None:
    database = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    source.mkdir()
    inside = source / "inside"
    inside.mkdir()
    (source / "small-a.bin").write_bytes(b"small")
    (source / "small-b.bin").write_bytes(b"small")
    (inside / "large-a.bin").write_bytes(b"larger payload")
    (inside / "large-b.bin").write_bytes(b"larger payload")
    (inside / "empty-a.bin").touch()
    (inside / "empty-b.bin").touch()
    (source / "large-c.bin").write_bytes(b"larger payload")
    connection = open_database(database)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
    finally:
        connection.close()

    runner = CliRunner()
    monkeypatch.setattr("flatlas.cli._stderr_is_tty", lambda: True)
    result = runner.invoke(app, ["dupes", str(inside), "--db", str(database)])
    assert result.exit_code == 0
    assert "Candidates: 2 files · 28 B" in result.stderr
    assert "Hashing: complete · 2 candidates" in result.stderr
    assert "empty-a.bin" not in result.stdout
    assert "empty-b.bin" not in result.stdout
    lines = result.stdout.splitlines()
    assert lines[0] == "1 duplicate group · 1 redundant file · 14 B theoretical savings"
    assert lines[2] == "[1] 14 B × 2 files · 14 B theoretical savings"
    assert lines[3:7] == [
        "    large-a.bin",
        "    large-b.bin",
        "",
        "Hash: 2 candidates · 0 cache hits · 2 files read · 28 bytes read",
    ]
    assert lines[7].startswith("Timing: ")

    connection = open_database(database)
    try:
        assert connection.execute(
            "SELECT count(*) FROM file_hash h JOIN path p ON p.id=h.path_id WHERE p.logical_size=0"
        ).fetchone()[0] == 0
    finally:
        connection.close()

    absolute_result = runner.invoke(app, ["dupes", str(inside), "--absolute", "--db", str(database)])
    assert absolute_result.exit_code == 0
    assert f"    {inside / 'large-a.bin'}" in absolute_result.stdout
    assert f"    {inside / 'large-b.bin'}" in absolute_result.stdout

    monkeypatch.chdir(inside)
    json_result = runner.invoke(app, ["dupes", "--format", "jsonl", "--db", str(database)])
    assert json_result.exit_code == 0
    hash_record, *groups = _records(json_result.stdout)
    assert hash_record["type"] == "hash" and hash_record["complete"] is True
    assert [group["theoretical_savings"] for group in groups] == [14]
    assert groups[0]["paths"] == [str(inside / "large-a.bin"), str(inside / "large-b.bin")]

    old_command = runner.invoke(app, ["duplicates", "--db", str(database)])
    assert old_command.exit_code != 0


@pytest.mark.scan
@pytest.mark.hash
@pytest.mark.safety
def test_scan_is_metadata_only_and_has_no_hash_option(tmp_path: Path) -> None:
    connection, source = make_connection(tmp_path)
    try:
        (source / "a.bin").write_bytes(b"same")
        (source / "b.bin").write_bytes(b"same")
        scan_directory(connection, source)
        assert connection.execute("SELECT count(*) FROM file_hash").fetchone()[0] == 0
    finally:
        connection.close()

    help_result = CliRunner().invoke(app, ["scan", "--help"])
    assert help_result.exit_code == 0
    assert "--hash" not in help_result.stdout


@pytest.mark.hash
@pytest.mark.safety
def test_lazy_hashing_is_scoped_reads_small_files_once_and_reuses_cache(tmp_path: Path, monkeypatch, external_file_allocation) -> None:
    connection, source = make_connection(tmp_path)
    inside = source / "inside"
    outside = source / "outside"
    inside.mkdir()
    outside.mkdir()
    for directory in (inside, outside):
        (directory / "a.bin").write_bytes(b"same")
        (directory / "b.bin").write_bytes(b"same")
    try:
        scan_directory(connection, source)
        original = hashing._validated_digest
        reads: list[tuple[Path, bool]] = []

        def tracked(candidate, *, full, timer):
            reads.append((Path(candidate["path_display"]), full))
            return original(candidate, full=full, timer=timer)

        monkeypatch.setattr(hashing, "_validated_digest", tracked)
        summary = ensure_duplicate_hashes(connection, inside)
        assert summary.as_dict() == {
            "candidate_files": 2,
            "candidate_bytes": 8,
            "content_files_read": 2,
            "changed_files": 0,
            "errors": 0,
            "cache_hits": 0,
            "content_bytes_read": 8,
            "complete": True,
        }
        assert reads == [(inside / "a.bin", False), (inside / "b.bin", False)]
        assert duplicate_groups(connection, scope=inside)[0]["count"] == 2
        assert connection.execute(
            "SELECT count(*) FROM file_hash h JOIN path_with_display p ON p.id=h.path_id WHERE p.path_display LIKE ?",
            (f"{outside}%",),
        ).fetchone()[0] == 0

        reads.clear()
        cached = ensure_duplicate_hashes(connection, inside)
        assert cached.content_files_read == 0
        assert reads == []
    finally:
        connection.close()


@pytest.mark.hash
def test_quick_hash_avoids_full_reads_for_different_large_files(tmp_path: Path, monkeypatch, external_file_allocation) -> None:
    connection, source = make_connection(tmp_path)
    try:
        size = hashing.QUICK_SAMPLE_BYTES * 2 + 1
        (source / "a.bin").write_bytes(b"a" * size)
        (source / "b.bin").write_bytes(b"b" * size)
        (source / "c.bin").write_bytes(b"c" * size)
        (source / "d.bin").write_bytes(b"c" * size)
        scan_directory(connection, source)
        original = hashing._validated_digest
        phases: list[bool] = []

        def tracked(candidate, *, full, timer):
            phases.append(full)
            return original(candidate, full=full, timer=timer)

        monkeypatch.setattr(hashing, "_validated_digest", tracked)
        summary = ensure_duplicate_hashes(connection, source)
        assert summary.content_files_read == 4
        assert phases == [False, False, False, False, True, True]
        groups = duplicate_groups(connection, scope=source)
        assert len(groups) == 1
        assert [Path(path["path_display"]).name for path in groups[0]["paths"]] == ["c.bin", "d.bin"]
    finally:
        connection.close()


@pytest.mark.hash
@pytest.mark.safety
def test_lazy_hash_reports_file_changed_after_scan_as_incomplete(tmp_path: Path, external_file_allocation) -> None:
    connection, source = make_connection(tmp_path)
    changed = source / "changed.bin"
    try:
        changed.write_bytes(b"same")
        (source / "stable.bin").write_bytes(b"same")
        scan_directory(connection, source)
        scanned_mtime = changed.stat().st_mtime_ns
        changed.write_bytes(b"else")
        os.utime(changed, ns=(scanned_mtime + 1_000_000_000, scanned_mtime + 1_000_000_000))

        summary = ensure_duplicate_hashes(connection, source)
        assert summary.complete is False
        assert summary.changed_files == 1
        assert summary.errors == 0
        assert duplicate_groups(connection, scope=source) == []
        assert connection.execute(
            "SELECT h.state FROM file_hash h JOIN path_with_display p ON p.id=h.path_id WHERE p.path_display=?",
            (str(changed),),
        ).fetchone()["state"] == "stale"
    finally:
        connection.close()

@pytest.mark.query
@pytest.mark.safety
def test_ls_lists_direct_files_and_preserves_gone_file_metadata(tmp_path: Path) -> None:
    connection, source = make_connection(tmp_path)
    try:
        live = source / "live.bin"
        gone = source / "gone.bin"
        live.write_bytes(b"abc")
        gone.write_bytes(b"gone")
        scan_directory(connection, source)

        gone.unlink()
        fresh = source / "fresh.bin"
        fresh.write_bytes(b"fresh")

        by_name = {Path(row["path"]).name: row for row in list_child_directories(connection, scope=source)}
        assert by_name["live.bin"]["kind"] == "file"
        assert by_name["live.bin"]["status"] == "ok"
        assert by_name["live.bin"]["logical_size"] == 3
        assert by_name["live.bin"]["files"] == 1
        assert by_name["fresh.bin"]["status"] == "new"
        assert by_name["fresh.bin"]["logical_size"] == 5
        assert by_name["fresh.bin"]["files"] == 1
        assert by_name["gone.bin"]["kind"] == "file"
        assert by_name["gone.bin"]["status"] == "gone"
        assert by_name["gone.bin"]["logical_size"] == 4
        assert by_name["gone.bin"]["files"] == 1
    finally:
        connection.close()

    result = CliRunner().invoke(app, ["ls", str(source), "--db", str(tmp_path / "index.sqlite")])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].split()[:4] == ["T", "S", "LOGICAL(B)", "N"]
    assert {line.split()[1] for line in lines[1:]} == {"ok", "new", "gone"}


@pytest.mark.scan
@pytest.mark.query
@pytest.mark.output
def test_ls_shows_subtree_scan_time_in_table_json_and_csv(tmp_path: Path) -> None:
    connection, source = make_connection(tmp_path)
    indexed = source / "indexed"
    indexed.mkdir()
    (indexed / "child.bin").write_bytes(b"child")
    (source / "file.bin").write_bytes(b"file")
    scan_directory(connection, source)
    scanned_through = 1_700_000_000_000_000_000
    connection.execute(
        """UPDATE directory_summary SET subtree_complete_through_ns=?
           WHERE path_id=(SELECT id FROM path_with_display WHERE path_display=?)""",
        (scanned_through, str(indexed)),
    )
    connection.commit()
    (source / "new").mkdir()
    connection.close()

    runner = CliRunner()
    arguments = ["ls", str(source), "--db", str(tmp_path / "index.sqlite")]
    table = runner.invoke(app, arguments)
    assert table.exit_code == 0
    assert "SCANNED-THROUGH" in table.stdout.splitlines()[0]
    table_rows = {line.split()[-1]: line.split() for line in table.stdout.splitlines()[1:]}
    indexed_line = next(line for line in table.stdout.splitlines()[1:] if line.endswith("indexed"))
    assert cli._format_ls_timestamp_ns(scanned_through) in indexed_line
    assert table_rows["file.bin"][-2] == "-"
    assert table_rows["new"][-2] == "-"

    json_result = runner.invoke(app, [*arguments, "--format", "jsonl"])
    csv_result = runner.invoke(app, [*arguments, "--format", "csv"])
    assert json_result.exit_code == csv_result.exit_code == 0
    json_rows = {Path(row["path"]).name: row for row in _records(json_result.stdout)}
    csv_rows = {Path(row["path"]).name: row for row in csv.DictReader(csv_result.stdout.splitlines())}
    assert json_rows["indexed"]["complete_through_ns"] == scanned_through
    assert csv_rows["indexed"]["complete_through_ns"] == str(scanned_through)
    # Unscanned: unknown. A file's coverage is its parent's listing, which completed.
    assert json_rows["new"]["complete_through_ns"] is None
    assert csv_rows["new"]["complete_through_ns"] == ""
    assert json_rows["file.bin"]["complete_through_ns"] is not None


@pytest.mark.query
@pytest.mark.output
def test_ls_scan_time_uses_gnu_recent_and_old_styles() -> None:
    now_ns = int(datetime(2026, 9, 23, 12, tzinfo=UTC).timestamp() * 1_000_000_000)
    recent_ns = int(datetime(2026, 9, 3, 7, 5, tzinfo=UTC).timestamp() * 1_000_000_000)
    old_ns = int(datetime(2025, 9, 3, 7, 5, tzinfo=UTC).timestamp() * 1_000_000_000)

    recent = datetime.fromtimestamp(recent_ns / 1_000_000_000).astimezone()
    old = datetime.fromtimestamp(old_ns / 1_000_000_000).astimezone()
    assert cli._format_ls_timestamp_ns(recent_ns, now_ns=now_ns) == (
        f"{recent.strftime('%b')} {recent.day:2} {recent:%H:%M}"
    )
    assert cli._format_ls_timestamp_ns(old_ns, now_ns=now_ns) == (
        f"{old.strftime('%b')} {old.day:2}  {old.year}"
    )
    assert cli._format_ls_timestamp_ns(now_ns + 1, now_ns=now_ns).endswith("  2026")
    assert cli._format_ls_timestamp_ns(
        now_ns - (31_556_952 // 2) * 1_000_000_000, now_ns=now_ns
    ).endswith("  2026")
    assert cli._format_ls_timestamp_ns(None, now_ns=now_ns) == "-"


@pytest.mark.db
@pytest.mark.safety
def test_database_migration_is_idempotent_and_keeps_existing_data(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite"
    connection, source = make_connection(tmp_path)
    (source / "preserved.bin").write_bytes(b"data")
    scan_directory(connection, source)
    preserved_path_count = connection.execute("SELECT count(*) FROM path").fetchone()[0]
    # Remove the v2 table and marker to exercise the legacy backfill.
    connection.execute("DROP TABLE directory_summary")
    connection.execute("DELETE FROM schema_migration WHERE version=2")
    connection.commit()
    connection.close()
    # Reopening must migrate once, retain v1 data, and backfill the cache.
    migration_progress = []
    connection = open_database(database, on_migration=migration_progress.append)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("SELECT count(*) FROM schema_migration").fetchone()[0] == 8
        assert connection.execute("SELECT root_path_display FROM root").fetchone()[0] == str(discover_namespace(source).path)
        assert connection.execute("SELECT count(*) FROM path").fetchone()[0] == preserved_path_count
        summary = connection.execute(
            """SELECT ds.subtree_files, ds.subtree_logical_size,
                      ds.self_completed_at_ns, ds.subtree_complete_through_ns
               FROM directory_summary ds JOIN path_with_display p ON p.id=ds.path_id
               WHERE p.path_display=?""",
            (str(source),),
        ).fetchone()
        assert dict(summary) == {
            "subtree_files": 1,
            "subtree_logical_size": 4,
            "self_completed_at_ns": summary["self_completed_at_ns"],
            "subtree_complete_through_ns": summary["self_completed_at_ns"],
        }
        assert summary["self_completed_at_ns"] is not None
        indexes = {row["name"] for row in connection.execute("PRAGMA index_list('path')")}
        assert "idx_path_root_parent_live" in indexes
        assert migration_progress[0].phase == "started"
        assert migration_progress[0].version == 2
        backfill = [event for event in migration_progress if event.phase == "backfill"]
        assert backfill[0].completed == 0
        assert backfill[-1].completed == backfill[-1].total
        assert migration_progress[-1].phase == "completed"
    finally:
        connection.close()


@pytest.mark.scan
@pytest.mark.query
def test_ls_skips_recursive_checks_after_cancellation_and_keeps_cached_usage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    connection, source = make_connection(tmp_path)
    completed = source / "a-completed"
    active = source / "b-active"
    untouched = source / "c-untouched"
    for directory in (completed, active, untouched):
        directory.mkdir()
        (directory / "payload.bin").write_bytes(directory.name.encode())
    real_scandir = os.scandir

    def sorted_scandir(path):
        return iter(sorted(real_scandir(path), key=lambda entry: entry.name))

    interrupted = False

    def interrupt_active(progress) -> None:
        nonlocal interrupted
        if progress.phase == "scanning" and progress.current_directory == active.name and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    try:
        monkeypatch.setattr("flatlas.scanning.os.scandir", sorted_scandir)
        scan_directory(connection, source)
        try:
            scan_directory(connection, source, on_progress=interrupt_active)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("scan should be cancelled in the active branch")

        monkeypatch.setattr("flatlas.scanning.os.scandir", real_scandir)
        assert disk_usage(connection, scope=completed)[0]["status"] == "ok"
        cached_usage = connection.execute(
            """SELECT subtree_files, subtree_logical_size, subtree_complete_through_ns
               FROM directory_summary ds JOIN path_with_display p ON p.id=ds.path_id
               WHERE p.path_display=?""",
            (str(completed),),
        ).fetchone()

        def fail_recursive_check(*args):
            raise AssertionError("ls must not inspect indexed descendants")

        monkeypatch.setattr("flatlas.queries._subtree_completed_in_scan", fail_recursive_check)
        statements = []
        connection.set_trace_callback(statements.append)
        connection.execute("PRAGMA query_only = ON")
        rows = list_child_directories(connection, scope=source)
        connection.set_trace_callback(None)
        assert not any("WITH RECURSIVE" in statement.upper() for statement in statements)
        statuses = {
            Path(row["path"]).name: row["status"]
            for row in rows
        }
        assert statuses == {
            completed.name: "ok",
            active.name: "part",
            untouched.name: "ok",
        }
        completed_row = next(row for row in rows if Path(row["path"]).name == completed.name)
        assert completed_row["files"] == cached_usage["subtree_files"]
        assert completed_row["logical_size"] == cached_usage["subtree_logical_size"]
        assert completed_row["complete_through_ns"] == cached_usage["subtree_complete_through_ns"]
        assert list_child_directories(connection, scope=completed)[0]["status"] == "ok"
    finally:
        connection.close()


@pytest.mark.db
@pytest.mark.cli
@pytest.mark.output
def test_cli_reports_pending_migration_progress_to_stderr(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite"
    connection, source = make_connection(tmp_path)
    (source / "payload.bin").write_bytes(b"payload")
    scan_directory(connection, source)
    connection.execute("DROP TABLE directory_summary")
    connection.execute("DELETE FROM schema_migration WHERE version=2")
    connection.commit()
    connection.close()

    result = CliRunner().invoke(app, ["du", str(source), "--format", "jsonl", "--db", str(database)])

    assert result.exit_code == 0
    assert _records(result.stdout)[0]["logical_size"] == len(b"payload")
    assert "database migration 2 required: cache directory usage and scan freshness" in result.stderr
    assert "Migrating database 2:" in result.stderr
    assert "database migration 2 completed" in result.stderr


@pytest.mark.db
@pytest.mark.scan
@pytest.mark.safety
def test_refresh_cutoff_migration_keeps_existing_scans(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite"
    connection, source = make_connection(tmp_path)
    scan_directory(connection, source)
    scan_count = connection.execute("SELECT count(*) FROM scan").fetchone()[0]
    connection.execute("ALTER TABLE scan DROP COLUMN refresh_before_ns")
    connection.execute("DELETE FROM schema_migration WHERE version=3")
    connection.commit()
    connection.close()

    progress = []
    connection = open_database(database, on_migration=progress.append)
    try:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info('scan')")}
        assert "refresh_before_ns" in columns
        assert connection.execute("SELECT count(*) FROM scan").fetchone()[0] == scan_count
        assert connection.execute("SELECT count(*) FROM schema_migration").fetchone()[0] == 8
        assert [(event.version, event.phase) for event in progress] == [(3, "started"), (3, "completed")]
    finally:
        connection.close()


@pytest.mark.scan
@pytest.mark.query
def test_directory_summary_records_partial_and_complete_scan_freshness(tmp_path: Path, monkeypatch) -> None:
    connection, source = make_connection(tmp_path)
    nested = source / "nested"
    nested.mkdir()
    (nested / "payload.bin").write_bytes(b"payload")
    real_scandir = os.scandir

    def fail_nested(path):
        if Path(path) == nested:
            raise PermissionError("injected summary coverage failure")
        return real_scandir(path)

    try:
        monkeypatch.setattr("flatlas.scanning.os.scandir", fail_nested)
        assert scan_directory(connection, source)["status"] == "partial"
        rows = {
            row["path_display"]: row
            for row in connection.execute(
                """SELECT p.path_display, ds.subtree_files, ds.subtree_logical_size,
                          ds.self_completed_at_ns, ds.subtree_complete_through_ns
                   FROM directory_summary ds JOIN path_with_display p ON p.id=ds.path_id"""
            )
        }
        assert rows[str(source)]["self_completed_at_ns"] is not None
        assert rows[str(source)]["subtree_complete_through_ns"] is None
        assert rows[str(nested)]["self_completed_at_ns"] is None
        assert rows[str(nested)]["subtree_complete_through_ns"] is None
        assert rows[str(source)]["subtree_files"] == 0
        assert rows[str(source)]["subtree_logical_size"] == 0

        monkeypatch.setattr("flatlas.scanning.os.scandir", real_scandir)
        assert scan_directory(connection, source)["status"] == "completed"
        completed = connection.execute(
            """SELECT ds.subtree_files, ds.subtree_logical_size,
                      ds.self_completed_at_ns, ds.subtree_complete_through_ns
               FROM directory_summary ds JOIN path_with_display p ON p.id=ds.path_id
               WHERE p.path_display=?""",
            (str(source),),
        ).fetchone()
        assert completed["subtree_files"] == 1
        assert completed["subtree_logical_size"] == 7
        assert completed["self_completed_at_ns"] is not None
        assert completed["subtree_complete_through_ns"] is not None
        nested_last_success = connection.execute(
            """SELECT ds.self_completed_at_ns FROM directory_summary ds
               JOIN path_with_display p ON p.id=ds.path_id WHERE p.path_display=?""",
            (str(nested),),
        ).fetchone()[0]

        monkeypatch.setattr("flatlas.scanning.os.scandir", fail_nested)
        assert scan_directory(connection, source)["status"] == "partial"
        invalidated = connection.execute(
            """SELECT p.path_display, ds.self_completed_at_ns, ds.subtree_complete_through_ns
               FROM directory_summary ds JOIN path_with_display p ON p.id=ds.path_id
               WHERE p.path_display IN (?, ?) ORDER BY p.path_display""",
            (str(source), str(nested)),
        ).fetchall()
        by_path = {row["path_display"]: row for row in invalidated}
        assert by_path[str(source)]["self_completed_at_ns"] is not None
        assert by_path[str(source)]["subtree_complete_through_ns"] == nested_last_success
        assert by_path[str(nested)]["self_completed_at_ns"] == nested_last_success
        assert by_path[str(nested)]["subtree_complete_through_ns"] == nested_last_success
    finally:
        connection.close()


@pytest.mark.scan
def test_refresh_before_scans_old_directories_and_preserves_fresh_subtrees(tmp_path: Path) -> None:
    connection, source = make_connection(tmp_path)
    fresh = source / "fresh"
    old = source / "old"
    fresh.mkdir()
    old.mkdir()
    fresh_file = fresh / "fresh.bin"
    old_file = old / "old.bin"
    fresh_file.write_bytes(b"fresh")
    old_file.write_bytes(b"old")
    cutoff = 2_000_000_000
    try:
        scan_directory(connection, source)
        connection.execute(
            """UPDATE directory_summary
               SET self_completed_at_ns=?, subtree_complete_through_ns=?
               WHERE path_id=(SELECT id FROM path_with_display WHERE path_display=?)""",
            (cutoff + 100, cutoff - 100, str(source)),
        )
        connection.execute(
            """UPDATE directory_summary
               SET self_completed_at_ns=?, subtree_complete_through_ns=?
               WHERE path_id=(SELECT id FROM path_with_display WHERE path_display=?)""",
            (cutoff + 100, cutoff + 100, str(fresh)),
        )
        connection.execute(
            """UPDATE directory_summary
               SET self_completed_at_ns=?, subtree_complete_through_ns=?
               WHERE path_id=(SELECT id FROM path_with_display WHERE path_display=?)""",
            (cutoff - 100, cutoff - 100, str(old)),
        )
        connection.commit()
        fresh_summary = connection.execute(
            "SELECT ds.* FROM directory_summary ds JOIN path_with_display p ON p.id=ds.path_id WHERE p.path_display=?",
            (str(fresh),),
        ).fetchone()
        fresh_file.unlink()
        old_file.unlink()

        result = scan_directory(connection, source, refresh_before_ns=cutoff)

        assert result["status"] == "completed"
        assert result["refresh_before_ns"] == cutoff
        assert result["stats"]["summary_scope"] == "affected"
        assert connection.execute(
            "SELECT ds.* FROM directory_summary ds WHERE ds.path_id=?", (fresh_summary["path_id"],),
        ).fetchone() == fresh_summary
        assert disk_usage(connection, scope=source)[0]["logical_size"] == len(b"fresh")
        states = {
            row["path_display"]: row["state"]
            for row in connection.execute(
                "SELECT path_display, state FROM path_with_display WHERE path_display IN (?, ?)",
                (str(fresh_file), str(old_file)),
            )
        }
        assert states[str(fresh_file)] == "present"
        assert states[str(old_file)] == "deleted"
        scan = connection.execute(
            "SELECT refresh_before_ns FROM scan ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert scan["refresh_before_ns"] == cutoff
        freshness = connection.execute(
            """SELECT ds.self_completed_at_ns
               FROM directory_summary ds JOIN path_with_display p ON p.id=ds.path_id
               WHERE p.path_display=?""",
            (str(old),),
        ).fetchone()[0]
        assert freshness > cutoff
    finally:
        connection.close()


@pytest.mark.scan
@pytest.mark.safety
def test_partial_refresh_never_confirms_missing_entries(tmp_path: Path, monkeypatch) -> None:
    connection, source = make_connection(tmp_path)
    first = source / "first"
    blocked = source / "blocked"
    first.mkdir()
    blocked.mkdir()
    vanished = first / "vanished.bin"
    vanished.write_bytes(b"gone")
    changed = first / "changed.bin"
    changed.write_bytes(b"x")
    cutoff = 2_000_000_000
    real_scandir = os.scandir
    try:
        scan_directory(connection, source)
        connection.execute(
            "UPDATE directory_summary SET self_completed_at_ns=?, subtree_complete_through_ns=?",
            (cutoff - 1, cutoff - 1),
        )
        connection.commit()
        vanished.unlink()
        changed.write_bytes(b"longer")

        def fail_one_directory(path):
            if Path(path) == blocked:
                raise PermissionError("injected refresh failure")
            return real_scandir(path)

        monkeypatch.setattr("flatlas.scanning.os.scandir", fail_one_directory)
        result = scan_directory(connection, source, refresh_before_ns=cutoff)

        assert result["status"] == "partial"
        assert result["stats"]["summary_scope"] == "affected"
        assert result["paths_deleted"] == 0
        assert disk_usage(connection, scope=source)[0]["logical_size"] == len(b"gonelonger")
        assert connection.execute(
            "SELECT state FROM path_with_display WHERE path_display=?", (str(vanished),)
        ).fetchone()["state"] == "present"
    finally:
        connection.close()


@pytest.mark.scan
@pytest.mark.safety
def test_refresh_falls_back_to_parent_when_indexed_child_disappears(tmp_path: Path) -> None:
    connection, source = make_connection(tmp_path)
    removed = source / "removed"
    removed.mkdir()
    payload = removed / "payload.bin"
    payload.write_bytes(b"gone")
    cutoff = 2_000_000_000
    try:
        scan_directory(connection, source)
        connection.execute(
            """UPDATE directory_summary
               SET self_completed_at_ns=?, subtree_complete_through_ns=?
               WHERE path_id=(SELECT id FROM path_with_display WHERE path_display=?)""",
            (cutoff + 100, cutoff - 100, str(source)),
        )
        connection.execute(
            """UPDATE directory_summary
               SET self_completed_at_ns=?, subtree_complete_through_ns=?
               WHERE path_id=(SELECT id FROM path_with_display WHERE path_display=?)""",
            (cutoff - 100, cutoff - 100, str(removed)),
        )
        connection.commit()
        payload.unlink()
        removed.rmdir()

        result = scan_directory(connection, source, refresh_before_ns=cutoff)

        assert result["status"] == "completed"
        states = {
            row["path_display"]: row["state"]
            for row in connection.execute(
                "SELECT path_display, state FROM path_with_display WHERE path_display IN (?, ?)",
                (str(removed), str(payload)),
            )
        }
        assert states == {str(removed): "deleted", str(payload): "deleted"}
    finally:
        connection.close()


@pytest.mark.scan
def test_reappeared_directory_does_not_reuse_previous_success_time(tmp_path: Path, monkeypatch) -> None:
    connection, source = make_connection(tmp_path)
    nested = source / "nested"
    nested.mkdir()
    real_scandir = os.scandir
    try:
        scan_directory(connection, source)
        nested.rmdir()
        scan_directory(connection, source)
        nested.mkdir()

        def fail_reappeared(path):
            if Path(path) == nested:
                raise PermissionError("injected reappearance failure")
            return real_scandir(path)

        monkeypatch.setattr("flatlas.scanning.os.scandir", fail_reappeared)
        assert scan_directory(connection, source)["status"] == "partial"
        summary = connection.execute(
            """SELECT ds.self_completed_at_ns, ds.subtree_complete_through_ns
               FROM directory_summary ds JOIN path_with_display p ON p.id=ds.path_id
               WHERE p.path_display=?""",
            (str(nested),),
        ).fetchone()
        assert summary["self_completed_at_ns"] is None
        assert summary["subtree_complete_through_ns"] is None
    finally:
        connection.close()


@pytest.mark.scan
@pytest.mark.query
def test_subtree_scan_refreshes_cached_ancestor_totals_without_rescanning_ancestor(tmp_path: Path) -> None:
    connection, source = make_connection(tmp_path)
    nested = source / "nested"
    nested.mkdir()
    payload = nested / "payload.bin"
    payload.write_bytes(b"a")
    sibling = source / "sibling"
    sibling.mkdir()
    (sibling / "payload.bin").write_bytes(b"two")
    try:
        scan_directory(connection, source)
        root_before = connection.execute(
            """SELECT ds.self_completed_at_ns FROM directory_summary ds
               JOIN path_with_display p ON p.id=ds.path_id WHERE p.path_display=?""",
            (str(source),),
        ).fetchone()[0]
        sibling_before = connection.execute(
            "SELECT ds.* FROM directory_summary ds JOIN path_with_display p ON p.id=ds.path_id WHERE p.path_display=?",
            (str(sibling),),
        ).fetchone()

        payload.write_bytes(b"four")
        scan_directory(connection, nested)
        root_after = connection.execute(
            """SELECT ds.subtree_files, ds.subtree_logical_size, ds.self_completed_at_ns
               FROM directory_summary ds JOIN path_with_display p ON p.id=ds.path_id
               WHERE p.path_display=?""",
            (str(source),),
        ).fetchone()
        assert root_after["subtree_files"] == 2
        assert root_after["subtree_logical_size"] == 7
        assert root_after["self_completed_at_ns"] == root_before
        assert disk_usage(connection, scope=source)[0]["logical_size"] == 7
        assert connection.execute(
            "SELECT * FROM directory_summary WHERE path_id=?", (sibling_before["path_id"],),
        ).fetchone() == sibling_before
    finally:
        connection.close()


@pytest.mark.cli
def test_cli_main_renders_domain_errors_without_traceback(monkeypatch, capsys) -> None:
    def fail() -> None:
        raise errors.FlatlasError("path is not currently indexed")

    monkeypatch.setattr(cli, "app", fail)
    assert cli.main() == 2
    captured = capsys.readouterr()
    assert captured.err == "error: path is not currently indexed\n"
    assert "Traceback" not in captured.err


@pytest.mark.scan
@pytest.mark.safety
def test_scan_records_unicode_entries_and_does_not_follow_symlinks(tmp_path: Path) -> None:
    connection, source = make_connection(tmp_path)
    try:
        nested = source / "资料" / "深层"
        nested.mkdir(parents=True)
        document = nested / "报告.txt"
        document.write_text("内容", encoding="utf-8")
        (source / "empty").mkdir()
        link = source / "linked"
        try:
            link.symlink_to(nested, target_is_directory=True)
        except OSError:
            link = None

        result = scan_directory(connection, source)
        assert result["status"] == "completed"
        rows = {
            row["path_display"]: row
            for row in connection.execute("SELECT path_display, entry_kind, logical_size FROM path_with_display")
        }
        assert rows[str(document)]["entry_kind"] == "file"
        assert rows[str(document)]["logical_size"] == len("内容".encode())
        assert rows[str(source / "empty")]["entry_kind"] == "directory"
        if link is not None:
            assert rows[str(link)]["entry_kind"] == "symlink"
            assert not any(path.startswith(f"{link}{os.sep}") for path in rows)
    finally:
        connection.close()


@pytest.mark.scan
@pytest.mark.hash
@pytest.mark.safety
def test_completed_scan_confirms_directory_deletion_and_reappearance_stales_hash(tmp_path: Path, external_file_allocation) -> None:
    connection, source = make_connection(tmp_path)
    removed = source / "removed"
    removed.mkdir()
    tracked = removed / "tracked.bin"
    try:
        tracked.write_bytes(b"same")
        (source / "peer.bin").write_bytes(b"same")
        scan_directory(connection, source)
        ensure_duplicate_hashes(connection, source)
        tracked.unlink()
        removed.rmdir()
        result = scan_directory(connection, source)
        assert result["status"] == "completed"
        deleted = connection.execute(
            "SELECT state, deleted_by_scope_id, deleted_at_ns FROM path_with_display WHERE path_display=?",
            (str(tracked),),
        ).fetchone()
        assert deleted["state"] == "deleted"
        assert deleted["deleted_by_scope_id"] is not None
        assert deleted["deleted_at_ns"] is not None
        assert connection.execute(
            "SELECT state FROM path_with_display WHERE path_display=?", (str(removed),)
        ).fetchone()["state"] == "deleted"

        removed.mkdir()
        tracked.write_bytes(b"changed")
        scan_directory(connection, source)
        restored = connection.execute(
            """SELECT p.state, p.deleted_by_scope_id, p.deleted_at_ns, h.state AS hash_state
            FROM path_with_display p JOIN file_hash h ON h.path_id=p.id WHERE p.path_display=?""",
            (str(tracked),),
        ).fetchone()
        assert dict(restored) == {
            "state": "present",
            "deleted_by_scope_id": None,
            "deleted_at_ns": None,
            "hash_state": "stale",
        }
    finally:
        connection.close()

    result = CliRunner().invoke(app, ["scan", "--format", "json", str(source / "missing"), "--db", str(tmp_path / "index.sqlite")])
    assert result.exit_code == 1
    assert "path does not exist" in str(result.exception)


@pytest.mark.scan
@pytest.mark.safety
def test_partial_or_cancelled_scan_never_confirms_missing_paths(tmp_path: Path, monkeypatch) -> None:
    connection, source = make_connection(tmp_path)
    survivor = source / "survivor.bin"
    try:
        survivor.write_bytes(b"keep")
        vanished = source / "vanished.bin"
        vanished.write_bytes(b"gone")
        scan_directory(connection, source)
        vanished.unlink()
        real_scandir = os.scandir
        monkeypatch.setattr("flatlas.scanning.os.scandir", lambda _path: (_ for _ in ()).throw(OSError("injected")))
        assert scan_directory(connection, source)["status"] == "partial"
        assert connection.execute(
            "SELECT state FROM path_with_display WHERE path_display=?", (str(vanished),)
        ).fetchone()["state"] == "present"

        monkeypatch.setattr("flatlas.scanning.os.scandir", real_scandir)
        monkeypatch.setattr("flatlas.scanning._observation", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            scan_directory(connection, source)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("scan should propagate cancellation")
        assert connection.execute(
            "SELECT state FROM path_with_display WHERE path_display=?", (str(vanished),)
        ).fetchone()["state"] == "present"
    finally:
        connection.close()


@pytest.mark.scan
@pytest.mark.safety
def test_first_scan_interrupt_saves_observed_paths_and_cached_totals(tmp_path: Path) -> None:
    connection, source = make_connection(tmp_path)
    payload = source / "payload.bin"
    payload.write_bytes(b"saved")
    interrupted = False

    def interrupt_once(progress) -> None:
        nonlocal interrupted
        if progress.phase == "scanning" and progress.files_seen == 1 and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    try:
        try:
            scan_directory(connection, source, on_progress=interrupt_once)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("scan should propagate cancellation after saving")

        scan = connection.execute(
            "SELECT status, files_seen, dirs_seen FROM scan ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert dict(scan) == {"status": "cancelled", "files_seen": 1, "dirs_seen": 1}
        assert connection.execute(
            "SELECT state FROM path_with_display WHERE path_display=?", (str(payload),)
        ).fetchone()["state"] == "present"
        summary = connection.execute(
            """SELECT ds.subtree_files, ds.subtree_logical_size, ds.self_completed_at_ns
               FROM directory_summary ds JOIN path_with_display p ON p.id=ds.path_id
               WHERE p.path_display=?""",
            (str(source),),
        ).fetchone()
        assert dict(summary) == {
            "subtree_files": 1,
            "subtree_logical_size": 5,
            "self_completed_at_ns": None,
        }
    finally:
        connection.close()


@pytest.mark.scan
def test_resume_uses_original_start_and_skips_completed_directories(tmp_path: Path, monkeypatch) -> None:
    connection, source = make_connection(tmp_path)
    first = source / "a"
    remaining = source / "b"
    first.mkdir()
    remaining.mkdir()
    (first / "first.bin").write_bytes(b"first")
    (remaining / "remaining.bin").write_bytes(b"remaining")
    real_scandir = os.scandir

    def sorted_scandir(path):
        return iter(sorted(real_scandir(path), key=lambda entry: entry.name))

    interrupted = False

    def interrupt_before_remaining(progress) -> None:
        nonlocal interrupted
        if progress.phase == "scanning" and progress.current_directory == "b" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    try:
        monkeypatch.setattr("flatlas.scanning.os.scandir", sorted_scandir)
        try:
            scan_directory(connection, source, on_progress=interrupt_before_remaining)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("scan should be cancelled before the remaining directory")

        cancelled = connection.execute(
            "SELECT id, status, started_at_ns, refresh_before_ns FROM scan ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert cancelled["status"] == "cancelled"
        assert cancelled["refresh_before_ns"] is None
        selection = resume_selection(connection, source)
        assert selection.scan_id == cancelled["id"]
        assert selection.refresh_before_ns == cancelled["started_at_ns"]

        scanned = {first: 0, remaining: 0}

        def count_scandir(path):
            directory = Path(path)
            if directory in scanned:
                scanned[directory] += 1
            return real_scandir(path)

        monkeypatch.setattr("flatlas.scanning.os.scandir", count_scandir)
        result = scan_directory(connection, source, refresh_before_ns=selection.refresh_before_ns)

        assert result["status"] == "completed"
        assert scanned == {first: 0, remaining: 1}
        resumed = connection.execute(
            "SELECT refresh_before_ns FROM scan ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert resumed["refresh_before_ns"] == cancelled["started_at_ns"]
        explicit = resume_selection_from_scan(connection, cancelled["id"])
        assert explicit.scan_id == cancelled["id"]
        assert explicit.refresh_before_ns == cancelled["started_at_ns"]
        assert explicit.scope == source
    finally:
        connection.close()


@pytest.mark.scan
def test_resume_preserves_existing_refresh_cutoff(tmp_path: Path) -> None:
    connection, source = make_connection(tmp_path)
    (source / "payload.bin").write_bytes(b"payload")
    cutoff = 1
    interrupted = False

    def interrupt_once(progress) -> None:
        nonlocal interrupted
        if progress.phase == "scanning" and progress.files_seen == 1 and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    try:
        try:
            scan_directory(
                connection,
                source,
                refresh_before_ns=cutoff,
                on_progress=interrupt_once,
            )
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("refresh scan should be cancelled")

        assert resume_selection(connection, source).refresh_before_ns == cutoff
    finally:
        connection.close()


@pytest.mark.scan
@pytest.mark.cli
def test_cli_rejects_resume_with_refresh_before(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["scan", "--format", "json", str(tmp_path), "--resume", "--refresh-before", "7d", "--db", str(tmp_path / "index.sqlite")],
    )

    assert result.exit_code == 2
    assert "--resume, --resume-from, and --refresh-before" in result.output
    assert "mutually" in result.output


@pytest.mark.scan
@pytest.mark.cli
def test_cli_resumes_from_explicit_scan_id(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite"
    connection, source = make_connection(tmp_path)
    (source / "payload.bin").write_bytes(b"payload")
    interrupted = False

    def interrupt_once(progress) -> None:
        nonlocal interrupted
        if progress.phase == "scanning" and progress.files_seen == 1 and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    try:
        try:
            scan_directory(connection, source, on_progress=interrupt_once)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("scan should be cancelled")
        cancelled = connection.execute(
            "SELECT id, started_at_ns FROM scan ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()

    result = CliRunner().invoke(
        app,
        ["scan", "--format", "json", "--resume-from", str(cancelled["id"]), "--db", str(database)],
    )

    assert result.exit_code == 0
    summary = json.loads(result.stdout)
    assert summary["status"] == "completed"
    assert summary["refresh_before_ns"] == cancelled["started_at_ns"]


@pytest.mark.scan
@pytest.mark.cli
def test_cli_rejects_path_with_resume_from(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["scan", "--format", "json", str(tmp_path), "--resume-from", "1", "--db", str(tmp_path / "index.sqlite")],
    )

    assert result.exit_code == 2
    assert "PATH must be omitted with --resume-from" in result.output


@pytest.mark.scan
@pytest.mark.query
def test_scan_audit_queries_filter_scopes_and_include_errors(tmp_path: Path, monkeypatch) -> None:
    connection, source = make_connection(tmp_path)
    first = source / "first"
    second = source / "second"
    first.mkdir()
    second.mkdir()
    (first / "first.bin").write_bytes(b"first")
    (second / "second.bin").write_bytes(b"second")
    real_scandir = os.scandir
    try:
        root_scan = scan_directory(connection, source)
        first_scan = scan_directory(connection, first)

        def fail_second(path):
            if Path(path) == second:
                raise PermissionError("injected scan audit failure")
            return real_scandir(path)

        monkeypatch.setattr("flatlas.scanning.os.scandir", fail_second)
        second_scan = scan_directory(connection, second)
        monkeypatch.setattr("flatlas.scanning.os.scandir", real_scandir)

        rows = list_scans(connection, scope=second, limit=20)
        assert [row["scan_id"] for row in rows] == [second_scan["scan_id"], root_scan["scan_id"]]
        assert first_scan["scan_id"] not in {row["scan_id"] for row in rows}
        assert rows[0]["status"] == "partial"
        assert rows[0]["scope"] == str(second)
        assert rows[0]["error_count"] == 1

        details = scan_info(connection, second_scan["scan_id"])
        assert details["status"] == "partial"
        assert details["scopes"][0]["scope"] == str(second)
        assert details["scopes"][0]["error_count"] == 1
        assert details["errors"][0]["operation"] == "enumerate"
        assert details["errors"][0]["message"] == "injected scan audit failure"
        assert details["errors"][0]["path"] == str(second)

        offline = source.with_name("source-offline")
        source.rename(offline)
        assert [row["scan_id"] for row in list_scans(connection, scope=second, limit=20)] == [
            second_scan["scan_id"], root_scan["scan_id"],
        ]
    finally:
        connection.close()


@pytest.mark.scan
@pytest.mark.query
@pytest.mark.cli
@pytest.mark.output
def test_scan_audit_cli_outputs_table_and_json(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite"
    connection, source = make_connection(tmp_path)
    try:
        result = scan_directory(connection, source)
    finally:
        connection.close()

    runner = CliRunner()
    scans_result = runner.invoke(app, ["scans", str(source), "--db", str(database)])
    assert scans_result.exit_code == 0
    assert scans_result.stdout.splitlines()[0].split() == [
        "ID", "STATUS", "STARTED", "FINISHED", "FILES", "DIRS", "ERRORS", "SCOPE",
    ]
    assert str(result["scan_id"]) in scans_result.stdout
    assert str(source) in scans_result.stdout

    json_result = runner.invoke(
        app,
        ["scans", str(source), "--format", "jsonl", "--db", str(database)],
    )
    assert json_result.exit_code == 0
    assert _records(json_result.stdout)[0]["scan_id"] == result["scan_id"]

    info_result = runner.invoke(
        app,
        ["scan-info", "--format", "json", str(result["scan_id"]), "--db", str(database)],
    )
    assert info_result.exit_code == 0
    details = json.loads(info_result.stdout)
    assert details["scan_id"] == result["scan_id"]
    assert details["scopes"][0]["scope"] == str(source)
    assert details["errors"] == []


@pytest.mark.scan
@pytest.mark.safety
def test_second_interrupt_during_cancel_save_keeps_only_checkpoints(tmp_path: Path) -> None:
    connection, source = make_connection(tmp_path)
    payload = source / "payload.bin"
    payload.write_bytes(b"discarded")
    interruptions = 0

    def interrupt_twice(progress) -> None:
        nonlocal interruptions
        if progress.phase == "scanning" and progress.files_seen == 1 and interruptions == 0:
            interruptions += 1
            raise KeyboardInterrupt
        if progress.phase == "saving" and interruptions == 1:
            interruptions += 1
            raise KeyboardInterrupt

    try:
        try:
            scan_directory(connection, source, on_progress=interrupt_twice)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("second interrupt should abort saving")

        assert interruptions == 2
        # The uncommitted batch (payload) is discarded; the scan ends as cancelled
        # with the counts of its last checkpoint and confirms no deletion.
        scan = connection.execute("SELECT status, files_seen, finished_at_ns FROM scan").fetchone()
        assert (scan["status"], scan["files_seen"]) == ("cancelled", 0)
        assert scan["finished_at_ns"] is not None
        assert connection.execute(
            "SELECT count(*) FROM path_with_display WHERE path_display=?", (str(payload),)
        ).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM path WHERE state='deleted'").fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.scan
def test_directory_summary_refresh_reads_directory_data_in_batches(tmp_path: Path) -> None:
    connection, source = make_connection(tmp_path)
    for index in range(25):
        child = source / f"child-{index}"
        child.mkdir()
        (child / "payload.bin").write_bytes(b"x")
    try:
        scan_directory(connection, source)
        root = connection.execute(
            "SELECT id, root_id FROM path_with_display WHERE path_display=?", (str(source),)
        ).fetchone()
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        refresh_directory_summaries(connection, int(root["root_id"]), int(root["id"]))
        connection.set_trace_callback(None)

        reads = [
            statement for statement in statements
            if statement.lstrip().upper().startswith(("SELECT", "WITH"))
        ]
        assert len(reads) == 3
        assert disk_usage(connection, scope=source)[0]["logical_size"] == 25
    finally:
        connection.close()


@pytest.mark.query
@pytest.mark.hash
@pytest.mark.safety
def test_index_queries_and_cached_duplicates_work_after_source_goes_offline(tmp_path: Path, external_file_allocation) -> None:
    connection, source = make_connection(tmp_path)
    try:
        (source / "a.bin").write_bytes(b"same")
        (source / "b.bin").write_bytes(b"same")
        scan_directory(connection, source)
        ensure_duplicate_hashes(connection, source)
        offline = tmp_path / "offline"
        source.rename(offline)
        assert [Path(hit.path) for hit in find_paths(connection, str(source), parse_find_expression([]))] == [
            source,
            source / "a.bin",
            source / "b.bin",
        ]
        assert disk_usage(connection, scope=source)[0]["logical_size"] == 8
        assert [row["path"] for row in largest_files(connection, scope=source)] == [
            str(source / "a.bin"), str(source / "b.bin"),
        ]
        assert ensure_duplicate_hashes(connection, source).content_files_read == 0
        assert duplicate_groups(connection, scope=source)[0]["count"] == 2
    finally:
        connection.close()


@pytest.mark.query
@pytest.mark.hash
@pytest.mark.plan
@pytest.mark.output
@pytest.mark.safety
def test_json_csv_output_and_read_only_commands_preserve_user_files(tmp_path: Path, external_file_allocation) -> None:
    database = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    source.mkdir()
    nested = source / "nested"
    nested.mkdir()
    (source / "a.bin").write_bytes(b"same")
    (nested / "b.bin").write_bytes(b"same")
    before = {path.relative_to(source): (path.read_bytes(), path.stat().st_mtime_ns) for path in source.rglob("*") if path.is_file()}
    connection = open_database(database)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
    finally:
        connection.close()

    runner = CliRunner()
    for command in ("du", "ls", "largest", "dupes"):
        arguments = [command, str(source), "--format", "jsonl", "--db", str(database)]
        result = runner.invoke(app, arguments)
        assert result.exit_code == 0, result.output
        assert all(record["type"] for record in _records(result.stdout))
        csv_result = runner.invoke(app, [command, str(source), "--format", "csv", "--db", str(database)])
        assert csv_result.exit_code == 0, csv_result.output
        list(csv.reader(csv_result.stdout.splitlines()))

    for command in ("roots", "df"):
        result = runner.invoke(app, [command, "--format", "jsonl", "--db", str(database)])
        assert result.exit_code == 0
        assert _records(result.stdout)
    plan = runner.invoke(app, ["plan", "create", "--root", str(source), "--db", str(database)])
    assert plan.exit_code == 0
    assert runner.invoke(app, ["plan", "show", plan.stdout.strip(), "--db", str(database)]).exit_code == 0
    exported = tmp_path / "dupes.json"
    assert runner.invoke(
        app, ["dupes", "--format", "jsonl", "--output", str(exported), str(source), "--db", str(database)]
    ).exit_code == 0
    assert [record["type"] for record in _records(exported.read_text(encoding="utf-8"))] == ["hash", "group"]
    assert "apply" not in {command.name for command in app.registered_commands}
    after = {path.relative_to(source): (path.read_bytes(), path.stat().st_mtime_ns) for path in source.rglob("*") if path.is_file()}
    assert after == before
