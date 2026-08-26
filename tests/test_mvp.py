import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from flatlas.cli import app
from flatlas.core import (
    create_dry_run_plan,
    discover_namespace,
    disk_usage,
    duplicate_groups,
    list_child_directories,
    open_database,
    plan_operations,
    register_namespace,
    scan_directory,
)


def make_connection(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    connection = open_database(tmp_path / "index.sqlite")
    register_namespace(connection, discover_namespace(source))
    return connection, source


def test_init_registration_does_not_scan_and_roots_has_df_alias(tmp_path: Path, monkeypatch) -> None:
    connection, _ = make_connection(tmp_path)
    try:
        assert connection.execute("SELECT count(*) FROM scan").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM path").fetchone()[0] == 0
    finally:
        connection.close()


def test_full_scan_detects_duplicates_and_builds_immutable_plan(tmp_path: Path) -> None:
    connection, source = make_connection(tmp_path)
    try:
        (source / "left.bin").write_bytes(b"same payload")
        (source / "right.bin").write_bytes(b"same payload")
        result = scan_directory(connection, source)
        assert result["status"] == "completed"
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


def test_subtree_scan_can_match_existing_index_and_complete_scope_marks_deletion(tmp_path: Path) -> None:
    connection, source = make_connection(tmp_path)
    try:
        (source / "old.bin").write_bytes(b"shared")
        scan_directory(connection, source)
        incoming = source / "incoming"
        incoming.mkdir()
        (incoming / "new.bin").write_bytes(b"shared")
        scan_directory(connection, incoming)
        assert duplicate_groups(connection)[0]["count"] == 2
        (incoming / "new.bin").unlink()
        scan_directory(connection, incoming)
        row = connection.execute("SELECT state FROM path WHERE path_display=?", (str(incoming / "new.bin"),)).fetchone()
        assert row["state"] == "deleted"
        old = connection.execute("SELECT state FROM path WHERE path_display=?", (str(source / "old.bin"),)).fetchone()
        assert old["state"] == "present"
    finally:
        connection.close()


def test_du_defaults_to_headered_summary_with_relative_path(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.bin").write_bytes(b"a")
    (source / "two.bin").write_bytes(b"bc")
    connection = open_database(database)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source, hash_mode="none")
        rows = disk_usage(connection, scope=source, relative_to=tmp_path)
        assert rows[0]["path"] == "source"
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
    assert lines[0].lstrip().startswith("SIZE(B)  N")
    assert lines[0].endswith("PATH")
    assert lines[1].lstrip().startswith("3  2")
    assert lines[1].endswith("  source")
    assert lines[0].index("PATH") == lines[1].index("source")

    json_result = runner.invoke(app, ["du", str(source), "--format", "json", "--db", str(database)])
    assert json_result.exit_code == 0
    assert json.loads(json_result.stdout)[0]["path"] == "source"

    multiple_result = runner.invoke(
        app,
        ["du", "source/one.bin", "source/two.bin", "--format", "json", "--db", str(database)],
    )
    assert multiple_result.exit_code == 0
    assert [row["logical_size"] for row in json.loads(multiple_result.stdout)] == [1, 2]

    help_result = runner.invoke(app, ["du", "--help"])
    assert help_result.exit_code == 0
    assert "SIZE(B)=logical bytes" in help_result.stdout
    assert "N=file count" in help_result.stdout
    assert "ALLOC(B)=allocated bytes" in help_result.stdout


def test_ls_marks_live_child_directory_scan_statuses(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "index.sqlite"
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
        scan_directory(connection, source, hash_mode="none")

        real_scandir = __import__("os").scandir

        def fail_nested(path):
            if Path(path) == nested:
                raise PermissionError("injected ls coverage failure")
            return real_scandir(path)

        with monkeypatch.context() as partial_scan:
            partial_scan.setattr("flatlas.core.os.scandir", fail_nested)
            assert scan_directory(connection, nested, hash_mode="none")["status"] == "partial"

        empty.rmdir()
        (source / "new").mkdir()
        rows = list_child_directories(connection, scope=source)
        by_name = {row["name"]: row for row in rows}
        assert list(by_name) == ["complete", "empty", "nested", "new"]
        assert by_name["complete"]["status"] == "scanned"
        assert by_name["complete"]["logical_size"] == 4
        assert by_name["empty"]["status"] == "missing"
        assert by_name["empty"]["logical_size"] is None
        assert by_name["nested"]["status"] == "incomplete"
        assert by_name["nested"]["files"] == 1
        assert by_name["new"]["status"] == "unscanned"
        assert by_name["new"]["files"] is None
        empty_state = connection.execute(
            "SELECT state FROM path WHERE path_display=?",
            (str(empty),),
        ).fetchone()["state"]
        assert empty_state == "present"
    finally:
        connection.close()


    monkeypatch.setattr(
        "flatlas.core.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=10 * 1024, used=4 * 1024, free=6 * 1024),
    )
    database = tmp_path / "index.sqlite"
    runner = CliRunner()
    roots_result = runner.invoke(app, ["roots", "--db", str(database)])
    df_result = runner.invoke(app, ["df", "--db", str(database)])
    assert roots_result.exit_code == 0
    assert df_result.exit_code == 0
    assert roots_result.stdout == df_result.stdout
    assert roots_result.stdout.splitlines()[0].split() == [
        "FILESYSTEM",
        "1K-BLOCKS",
        "USED",
        "AVAILABLE",
        "USE%",
        "MOUNTED",
        "ON",
    ]
    assert "10" in roots_result.stdout
    assert "4" in roots_result.stdout
    assert "6" in roots_result.stdout
    assert "40%" in roots_result.stdout

    json_result = runner.invoke(app, ["df", "--format", "json", "--db", str(database)])
    row = json.loads(json_result.stdout)[0]
    assert list(row)[:5] == ["id", "root_path_display", "platform", "namespace_kind", "enabled"]
    assert row["blocks_1k"] == 10
    assert row["used_1k"] == 4
    assert row["available_1k"] == 6
    assert row["use_percent"] == 40

    def unavailable(_path):
        raise OSError("injected unavailable filesystem")

    monkeypatch.setattr("flatlas.core.shutil.disk_usage", unavailable)
    unavailable_result = runner.invoke(app, ["roots", "--format", "json", "--db", str(database)])
    unavailable_row = json.loads(unavailable_result.stdout)[0]
    assert unavailable_row["blocks_1k"] is None
    assert unavailable_row["used_1k"] is None
    assert unavailable_row["available_1k"] is None
    assert unavailable_row["use_percent"] is None
    assert unavailable_row["status"] == "unavailable"

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["ls", "source", "--db", str(database)])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert "\t" not in result.stdout
    assert lines[0].split()[:3] == ["S", "SIZE(B)", "N"]
    name_column = lines[0].index("NAME")
    assert {line[name_column:] for line in lines[1:]} == {"complete", "empty", "nested", "new"}
    assert {line.split()[0] for line in lines[1:]} == {"ok", "new", "part", "gone"}

    help_result = CliRunner().invoke(app, ["ls", "--help"])
    assert help_result.exit_code == 0
    assert "ok=scanned" in help_result.stdout
    assert "new=unscanned" in help_result.stdout
    assert "part=incomplete" in help_result.stdout
    assert "gone=missing" in help_result.stdout
    assert "SIZE(B)=indexed logical bytes" in help_result.stdout


def test_dupes_scopes_grouped_output_to_path_and_defaults_to_cwd(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "index.sqlite"
    source = tmp_path / "source"
    source.mkdir()
    inside = source / "inside"
    inside.mkdir()
    (source / "small-a.bin").write_bytes(b"small")
    (source / "small-b.bin").write_bytes(b"small")
    (inside / "large-a.bin").write_bytes(b"larger payload")
    (inside / "large-b.bin").write_bytes(b"larger payload")
    (source / "large-c.bin").write_bytes(b"larger payload")
    connection = open_database(database)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
    finally:
        connection.close()

    runner = CliRunner()
    result = runner.invoke(app, ["dupes", str(inside), "--db", str(database)])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0] == "1 duplicate group · 1 redundant file · 14 B theoretical savings"
    assert lines[2] == "[1] 14 B × 2 files · 14 B theoretical savings"
    assert lines[3:] == [
        "    large-a.bin",
        "    large-b.bin",
    ]

    absolute_result = runner.invoke(app, ["dupes", str(inside), "--absolute", "--db", str(database)])
    assert absolute_result.exit_code == 0
    assert f"    {inside / 'large-a.bin'}" in absolute_result.stdout
    assert f"    {inside / 'large-b.bin'}" in absolute_result.stdout

    monkeypatch.chdir(inside)
    json_result = runner.invoke(app, ["dupes", "--format", "json", "--db", str(database)])
    assert json_result.exit_code == 0
    json_groups = json.loads(json_result.stdout)
    assert [group["theoretical_savings"] for group in json_groups] == [14]
    assert [path["path_display"] for path in json_groups[0]["paths"]] == [
        "large-a.bin",
        "large-b.bin",
    ]

    old_command = runner.invoke(app, ["duplicates", "--db", str(database)])
    assert old_command.exit_code != 0
