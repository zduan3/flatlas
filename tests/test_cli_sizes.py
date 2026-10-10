import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from flatlas import cli


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
    return rows


@pytest.mark.parametrize("command", ["ls", "du"])
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
    assert "ALLOC" in header
    assert "(B)" not in lines[0]
    expected = ["-", "0", "1023", "1K", "1.5K", "1M", "1G", "1T", "1024T", "1M"]
    for line, size in zip(lines[1:], expected, strict=True):
        fields = line.split()
        assert fields[header.index(size_header)] == size
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
    assert "ALLOC(B)" in raw_header
    for line, row in zip(raw.stdout.splitlines()[1:], usage_rows, strict=True):
        assert line.split()[raw_header.index(f"{size_header}(B)")] == cli._format_size(row["logical_size"])


@pytest.mark.parametrize("command", ["ls", "du"])
@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_human_readable_preserves_machine_output(command, fmt, usage_rows, tmp_path: Path) -> None:
    runner = CliRunner()
    arguments = [command, "--format", fmt]
    raw = runner.invoke(cli.app, arguments)
    human = runner.invoke(cli.app, [*arguments, "-h"])
    assert raw.exit_code == human.exit_code == 0
    assert raw.stdout == human.stdout
    if fmt == "json":
        assert json.loads(human.stdout) == usage_rows
    else:
        rows = list(csv.DictReader(human.stdout.splitlines()))
        assert rows[4]["logical_size"] == rows[4]["allocated_size"] == "1536"
        assert rows[0]["logical_size"] == ""

    output = tmp_path / f"usage.{fmt}"
    exported = runner.invoke(cli.app, [*arguments, "-h", "--output", str(output)])
    assert exported.exit_code == 0, exported.output
    assert output.read_bytes().decode("utf-8") == raw.stdout


@pytest.mark.parametrize("command", ["ls", "du"])
def test_usage_human_readable_keeps_long_help(command) -> None:
    result = CliRunner().invoke(cli.app, [command, "--help"])
    assert result.exit_code == 0
    assert "human-readable" in result.stdout
    assert "1024" in result.stdout
