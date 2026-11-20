"""roots reads the index only; df reports live capacity like GNU df."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from flatlas import cli, paths, queries
from flatlas.cli import app
from flatlas.core import (
    discover_namespace,
    open_database,
    register_namespace,
    scan_directory,
)
from flatlas.mounts import MountEntry

pytestmark = [pytest.mark.cli, pytest.mark.output, pytest.mark.query]


@pytest.fixture
def indexed(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    monkeypatch.setattr(paths, "filesystem_type", lambda _mount: "lustre")
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.bin").write_bytes(b"data")
    database = tmp_path / "index.sqlite"
    connection = open_database(database)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
    finally:
        connection.close()
    return database, source


def test_roots_reports_recorded_state_without_touching_the_filesystem(indexed, monkeypatch) -> None:
    database, _source = indexed

    def forbidden(*_args, **_kwargs):
        pytest.fail("roots must not query the filesystem")

    monkeypatch.setattr(queries.shutil, "disk_usage", forbidden)
    monkeypatch.setattr(queries, "mount_entry", forbidden)
    runner = CliRunner()
    table = runner.invoke(app, ["roots", "--db", str(database)])
    assert table.exit_code == 0, table.output
    header, row = table.stdout.splitlines()[:2]
    assert header.split() == ["ID", "TYPE", "METADATA", "FILES", "LOGICAL(B)", "SCAN", "STATUS", "SCANNED", "ROOT"]
    assert row.split()[:7] == ["1", "lustre", "lazy", "1", "4", "1", "completed"]

    [record] = json.loads(runner.invoke(app, ["roots", "--format", "json", "--db", str(database)]).stdout)
    assert record["filesystem_type"] == "lustre"
    assert record["metadata"] == "lazy"
    assert (record["files"], record["logical_size"]) == (1, 4)
    assert record["last_scan_status"] == "completed"


def test_roots_marks_unscanned_and_non_lustre_roots(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "filesystem_type", lambda _mount: None)
    database = tmp_path / "index.sqlite"
    connection = open_database(database)
    try:
        register_namespace(connection, discover_namespace(tmp_path))
    finally:
        connection.close()
    [record] = json.loads(CliRunner().invoke(app, ["roots", "--format", "json", "--db", str(database)]).stdout)
    assert record["metadata"] == "exact"
    assert record["files"] is None and record["last_scan_id"] is None


def test_df_uses_gnu_headers_use_percent_and_mount_source(indexed, monkeypatch) -> None:
    database, _source = indexed
    queried: list[Path] = []

    def fake_usage(path):
        queried.append(Path(path))
        return SimpleNamespace(total=10 * 1024, used=4 * 1024, free=5 * 1024)

    monkeypatch.setattr(queries.shutil, "disk_usage", fake_usage)
    monkeypatch.setattr(queries, "mount_entry", lambda _mount: MountEntry("server:/fs1", "lustre"))
    runner = CliRunner()
    table = runner.invoke(app, ["df", "-T", "--db", str(database)])
    assert table.exit_code == 0, table.output
    header, row = table.stdout.splitlines()[:2]
    assert header.split() == ["Filesystem", "Type", "1K-blocks", "Used", "Available", "Use%", "Mounted", "on"]
    # GNU df: Use% = ceil(used / (used + available)), here 4 / 9.
    assert row.split()[:6] == ["server:/fs1", "lustre", "10", "4", "5", "45%"]

    [record] = json.loads(runner.invoke(app, ["df", "--format", "json", "--db", str(database)]).stdout)
    assert record["filesystem"] == "server:/fs1"
    assert record["type"] == "lustre"
    assert record["use_percent"] == 45
    assert record["status"] == "ok"


def test_df_file_arguments_query_the_file_and_need_no_index(tmp_path: Path, monkeypatch) -> None:
    queried: list[Path] = []

    def fake_usage(path):
        queried.append(Path(path))
        return SimpleNamespace(total=2048, used=1024, free=1024)

    monkeypatch.setattr(queries.shutil, "disk_usage", fake_usage)
    target = tmp_path / "dir"
    target.mkdir()
    missing_db = tmp_path / "never-created.sqlite"
    result = CliRunner().invoke(app, ["df", "--format", "json", "--db", str(missing_db), str(target)])
    assert result.exit_code == 0, result.output
    [record] = json.loads(result.stdout)
    # statfs goes to the FILE itself, as GNU df does, so directory quotas apply.
    assert queried == [target]
    assert record["mounted_on"] == str(discover_namespace(target).path)
    assert not missing_db.exists()


def test_df_unavailable_capacity_is_unknown(indexed, monkeypatch) -> None:
    database, _source = indexed

    def unavailable(_path):
        raise OSError("injected unavailable filesystem")

    monkeypatch.setattr(queries.shutil, "disk_usage", unavailable)
    runner = CliRunner()
    [record] = json.loads(runner.invoke(app, ["df", "--format", "json", "--db", str(database)]).stdout)
    assert [record[key] for key in ("blocks_1k", "used_1k", "available_1k", "use_percent")] == [None] * 4
    assert record["status"] == "unavailable"
    row = runner.invoke(app, ["df", "-h", "--db", str(database)]).stdout.splitlines()[1].split()
    assert row[1:5] == ["-", "-", "-", "-"]


@pytest.mark.parametrize(
    "blocks_1k, expected",
    [(0, "0"), (1, "1.0K"), (10, "10K"), (1536, "1.5M"), (1537, "1.6M"), (1048575, "1.0G"), (10 * 1024**3 + 1, "11T")],
)
def test_human_sizes_round_up_like_gnu(blocks_1k: int, expected: str) -> None:
    assert cli._format_size(blocks_1k * 1024, human_readable=True) == expected
