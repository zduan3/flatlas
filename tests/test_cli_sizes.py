import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from flatlas import cli
from flatlas.core import (
    discover_namespace,
    open_database,
    register_namespace,
    scan_directory,
)


@pytest.fixture
def usage_rows(monkeypatch):
    sizes = [None, 0, 1023, 1024, 1536, 1024**2, 1024**3, 1024**4, 1024**5, 1024**2 - 1]
    rows = [
        {
            "path": f"entry-{index}",
            "name": f"entry-{index}",
            "entry_kind": "directory" if index % 2 else "file",
            "status": "unscanned" if size is None else "scanned",
            "logical_size": size,
            "allocated_size": size,
            "files": None if size is None else 1536,
            "subtree_complete_through_ns": None,
        }
        for index, size in enumerate(sizes)
    ]
    monkeypatch.setattr(cli, "_database", lambda *args, **kwargs: Path("unused.sqlite"))
    monkeypatch.setattr(cli, "_open_database", lambda *args: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "disk_usage", lambda *args, **kwargs: rows)
    monkeypatch.setattr(cli, "list_child_directories", lambda *args, **kwargs: rows)
    monkeypatch.setattr(cli, "largest_files", lambda *args, **kwargs: rows)
    return rows


@pytest.mark.parametrize("command", ["ls", "du", "largest"])
@pytest.mark.parametrize("option", ["-h", "--human-readable"])
def test_human_readable_usage_table(command, option, usage_rows, tmp_path: Path) -> None:
    runner = CliRunner()
    arguments = [command, option]
    result = runner.invoke(cli.app, arguments)
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    header = lines[0].split()
    size_header = "LOGICAL" if command == "ls" else "SIZE"
    assert size_header in header
    if command == "largest":
        assert header == ["SIZE", "PATH"]
    else:
        assert "ALLOC" in header
    assert "(B)" not in lines[0]
    expected = ["-", "0", "1023", "1K", "1.5K", "1M", "1G", "1T", "1024T", "1M"]
    for line, size in zip(lines[1:], expected, strict=True):
        fields = line.split()
        assert fields[header.index(size_header)] == size
        if command != "largest":
            assert fields[header.index("ALLOC")] == size
            assert fields[header.index("N")] == ("-" if size == "-" else "1536")

    output = tmp_path / "usage.txt"
    exported = runner.invoke(cli.app, [*arguments, "--output", str(output)])
    assert exported.exit_code == 0, exported.output
    assert exported.stdout == ""
    assert output.read_text(encoding="utf-8") == result.stdout

    raw = runner.invoke(cli.app, [command])
    assert raw.exit_code == 0, raw.output
    raw_header = raw.stdout.splitlines()[0].split()
    assert f"{size_header}(B)" in raw_header
    if command == "largest":
        assert raw_header == ["SIZE(B)", "PATH"]
    else:
        assert "ALLOC(B)" in raw_header
    for line, row in zip(raw.stdout.splitlines()[1:], usage_rows, strict=True):
        assert line.split()[raw_header.index(f"{size_header}(B)")] == cli._format_size(row["logical_size"])


@pytest.mark.parametrize("command", ["ls", "du", "largest"])
@pytest.mark.parametrize("fmt", ["json", "csv"])
@pytest.mark.parametrize("option", ["-h", "--human-readable"])
def test_human_readable_preserves_machine_output(command, fmt, option, usage_rows, tmp_path: Path) -> None:
    runner = CliRunner()
    arguments = [command, "--format", fmt]
    raw = runner.invoke(cli.app, arguments)
    human = runner.invoke(cli.app, [*arguments, option])
    assert raw.exit_code == human.exit_code == 0
    assert raw.stdout == human.stdout
    if fmt == "json":
        assert json.loads(human.stdout) == usage_rows
    else:
        rows = list(csv.DictReader(human.stdout.splitlines()))
        assert rows[4]["logical_size"] == rows[4]["allocated_size"] == "1536"
        assert rows[0]["logical_size"] == ""

    output = tmp_path / f"usage.{fmt}"
    exported = runner.invoke(cli.app, [*arguments, option, "--output", str(output)])
    assert exported.exit_code == 0, exported.output
    assert output.read_bytes().decode("utf-8") == raw.stdout


@pytest.mark.parametrize("command", ["ls", "du", "largest"])
def test_usage_human_readable_keeps_long_help(command) -> None:
    result = CliRunner().invoke(cli.app, [command, "--help"])
    assert result.exit_code == 0
    assert "human-readable" in result.stdout
    assert "1024" in result.stdout


@pytest.mark.parametrize("rows", [
    [],
    [{"path": "file.bin", "logical_size": 1536, "allocated_size": None}],
    [{"path": "file.bin", "logical_size": 1536, "allocated_size": 4096}],
])
@pytest.mark.parametrize("options", [[], ["-h"], ["--human-readable"]])
def test_largest_table_without_allocated_column(rows, options, usage_rows, monkeypatch) -> None:
    monkeypatch.setattr(cli, "largest_files", lambda *args, **kwargs: rows)
    result = CliRunner().invoke(cli.app, ["largest", *options])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0].split() == ["SIZE" if options else "SIZE(B)", "PATH"]
    assert len(result.stdout.splitlines()) == len(rows) + 1
    if rows:
        assert result.stdout.splitlines()[1].split() == ["1.5K" if options else "1536", "file.bin"]


@pytest.mark.parametrize("option", ["-h", "--human-readable"])
def test_largest_human_readable_preserves_index_query(option, tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    scope = source / "inside"
    nested = scope / "nested"
    nested.mkdir(parents=True)
    (source / "outside.bin").write_bytes(b"x" * 4096)
    (scope / "small.bin").write_bytes(b"x" * 1023)
    (scope / "a.bin").write_bytes(b"x" * 1536)
    (nested / "b.bin").write_bytes(b"x" * 1536)
    deleted = scope / "deleted.bin"
    deleted.write_bytes(b"x" * 3072)
    database = tmp_path / "index.sqlite"
    connection = open_database(database)
    try:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        deleted.unlink()
        scan_directory(connection, scope)
    finally:
        connection.close()

    runner = CliRunner()
    arguments = ["largest", str(scope), "--db", str(database)]
    raw = runner.invoke(cli.app, [*arguments, "--format", "json"])
    human = runner.invoke(cli.app, [*arguments, option, "--format", "json"])
    assert raw.exit_code == human.exit_code == 0
    assert raw.stdout == human.stdout
    expected_paths = ["a.bin", str(Path("nested") / "b.bin"), "small.bin"]
    assert [row["path"] for row in json.loads(human.stdout)] == expected_paths
    table = runner.invoke(cli.app, [*arguments, option, "-n", "2"])
    assert table.exit_code == 0, table.output
    lines = table.stdout.splitlines()
    assert len(lines) == 3
    for line, path in zip(lines[1:], expected_paths[:2], strict=True):
        assert line.split()[0] == "1.5K"
        assert line.endswith(path)

    monkeypatch.chdir(scope)
    defaulted = runner.invoke(cli.app, ["largest", option, "--limit", "1", "--db", str(database)])
    assert defaulted.exit_code == 0, defaulted.output
    assert len(defaulted.stdout.splitlines()) == 2
    assert defaulted.stdout.splitlines()[1].endswith("a.bin")
    file_scope = runner.invoke(cli.app, ["largest", "small.bin", option, "--db", str(database)])
    assert file_scope.exit_code == 0, file_scope.output
    assert file_scope.stdout.splitlines()[1].split()[0] == "1023"
    assert file_scope.stdout.splitlines()[1].endswith(".")
