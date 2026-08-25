import json
from pathlib import Path

from typer.testing import CliRunner

from flatlas.cli import app
from flatlas.core import (
    create_dry_run_plan,
    discover_namespace,
    disk_usage,
    duplicate_groups,
    list_directory_usage,
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


def test_init_registration_does_not_scan(tmp_path: Path) -> None:
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


def test_ls_lists_direct_children_and_summarizes_directories(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "index.sqlite"
    connection, source = make_connection(tmp_path)
    try:
        empty = source / "empty"
        empty.mkdir()
        nested = source / "nested"
        nested.mkdir()
        deeper = nested / "deeper"
        deeper.mkdir()
        (nested / "child.bin").write_bytes(b"ab")
        (deeper / "grandchild.bin").write_bytes(b"cde")
        (source / "loose.bin").write_bytes(b"wxyz")
        scan_directory(connection, source, hash_mode="none")

        rows = list_directory_usage(connection, scope=source, relative_to=tmp_path)
        by_path = {row["path"]: row for row in rows}
        assert list(by_path) == [
            str(Path("source") / "empty"),
            str(Path("source") / "loose.bin"),
            str(Path("source") / "nested"),
        ]
        assert by_path[str(Path("source") / "empty")]["files"] == 0
        assert by_path[str(Path("source") / "loose.bin")]["entry_kind"] == "file"
        assert by_path[str(Path("source") / "loose.bin")]["logical_size"] == 4
        assert by_path[str(Path("source") / "nested")]["entry_kind"] == "directory"
        assert by_path[str(Path("source") / "nested")]["files"] == 2
        assert by_path[str(Path("source") / "nested")]["logical_size"] == 5
    finally:
        connection.close()

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["ls", "source", "--db", str(database)])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert "\t" not in result.stdout
    assert lines[0].lstrip().startswith("T  SIZE(B)  N")
    path_column = lines[0].index("PATH")
    assert all(line[path_column:].startswith("source") for line in lines[1:])
    assert {line[0] for line in lines[1:]} == {"d", "f"}

    help_result = CliRunner().invoke(app, ["ls", "--help"])
    assert help_result.exit_code == 0
    assert "d=directory, f=file" in help_result.stdout
    assert "size columns match du" in help_result.stdout
