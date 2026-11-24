"""The machine output contract: record fields, CSV headers, limits and exit codes."""

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas.cli import app, main
from flatlas.db import open_database
from flatlas.hashing import ensure_duplicate_hashes
from flatlas.paths import discover_namespace, register_namespace
from flatlas.planning import create_dry_run_plan
from flatlas.records import (
    ENTRY_FIELDS,
    FILESYSTEM_FIELDS,
    FREED_FIELDS,
    GROUP_FIELDS,
    HASH_FIELDS,
    PLAN_FIELDS,
    ROOT_FIELDS,
    SCAN_FIELDS,
)
from flatlas.scanning import scan_directory

pytestmark = [pytest.mark.output, pytest.mark.cli, pytest.mark.query]


@pytest.fixture
def indexed(tmp_path: Path):
    source = tmp_path / "source"
    (source / "d").mkdir(parents=True)
    # Keep the duplicate payload out of NTFS resident storage.
    payload = b"same" * 2048
    (source / "d" / "a.bin").write_bytes(payload)
    (source / "b.bin").write_bytes(payload)
    db = tmp_path / "index.sqlite"
    connection = open_database(db)
    try:
        root_id = register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        ensure_duplicate_hashes(connection, source)
        create_dry_run_plan(connection, root_id)
    finally:
        connection.close()
    return db, source


def _run(*args: str) -> list[dict]:
    result = CliRunner().invoke(app, [*args])
    assert result.exit_code == 0, result.output
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_entry_records_share_one_field_order(indexed) -> None:
    db, source = indexed
    for command in (["du", "-d", "1"], ["ls"], ["largest"]):
        records = _run(*command, str(source), "--format", "jsonl", "--db", str(db))
        assert records and all(tuple(record) == ENTRY_FIELDS for record in records), command
        assert all(Path(record["path"]).is_absolute() for record in records)
    *rows, total = _run("du", "--freed", "-c", str(source), "--format", "jsonl", "--db", str(db))
    assert tuple(rows[0]) == (*ENTRY_FIELDS, *FREED_FIELDS)
    assert (total["type"], total["path"]) == ("total", None)
    [file_record] = _run("du", str(source / "b.bin"), "--format", "jsonl", "--db", str(db))
    assert (file_record["kind"], file_record["files"], file_record["entries"]) == ("file", 1, 1)
    assert file_record["mtime_ns"] is not None and file_record["status"] == "ok"


@pytest.mark.parametrize("fmt", ["jsonl", "csv"])
def test_find_machine_stdout_uses_utf8_with_a_legacy_encoding(indexed, monkeypatch, fmt: str) -> None:
    db, source = indexed
    unicode_path = source / "归档-½.txt"
    unicode_path.write_bytes(b"data")
    connection = open_database(db)
    try:
        scan_directory(connection, source)
    finally:
        connection.close()

    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    result = subprocess.run(
        [
            sys.executable, "-c", "from flatlas.cli import main; raise SystemExit(main())",
            "--no-config", "find", "--db", str(db), "--format", fmt, str(source), "-name", unicode_path.name,
        ],
        capture_output=True, check=True,
    )
    assert b"\r\n" not in result.stdout
    lines = result.stdout.decode("utf-8").splitlines()
    [record] = map(json.loads, lines) if fmt == "jsonl" else csv.DictReader(lines)
    assert record["path"] == str(unicode_path)


@pytest.mark.parametrize(("command", "fields", "record_type"), [
    (["du"], ENTRY_FIELDS, "entry"), (["ls"], ENTRY_FIELDS, "entry"), (["largest"], ENTRY_FIELDS, "entry"),
    (["dupes"], GROUP_FIELDS, "group"), (["scans"], SCAN_FIELDS, "scan"), (["roots"], ROOT_FIELDS, "root"),
    (["df"], FILESYSTEM_FIELDS, "filesystem"), (["plan", "show"], PLAN_FIELDS, "plan"),
])
def test_csv_header_is_the_record_fields(indexed, command, fields, record_type) -> None:
    db, source = indexed
    path = [str(source)] if command[0] in {"du", "ls", "largest", "dupes"} else []
    result = CliRunner().invoke(app, [*command, *path, "--format", "csv", "--db", str(db)])
    assert result.exit_code == 0, result.output
    reader = csv.DictReader(result.stdout.splitlines())
    assert tuple(reader.fieldnames or ()) == fields
    rows = list(reader)
    assert rows and {row["type"] for row in rows} == {record_type}


def test_dupes_jsonl_starts_with_the_hash_summary(indexed) -> None:
    db, source = indexed
    summary, *groups = _run("dupes", str(source), "--format", "jsonl", "--db", str(db))
    assert tuple(summary) == HASH_FIELDS and summary["type"] == "hash"
    assert [tuple(group) for group in groups] == [GROUP_FIELDS]
    assert groups[0]["paths"] == [str(source / "b.bin"), str(source / "d" / "a.bin")]


def test_machine_formats_have_no_implicit_limit(indexed) -> None:
    db, source = indexed
    connection = open_database(db)
    try:
        for _ in range(51):
            scan_directory(connection, source, shallow=True)
    finally:
        connection.close()
    assert len(_run("scans", "--format", "jsonl", "--db", str(db))) == 52
    assert len(_run("scans", "-n", "3", "--format", "jsonl", "--db", str(db))) == 3
    table = CliRunner().invoke(app, ["scans", "--db", str(db)])
    assert len(table.stdout.splitlines()) == 1 + 50


@pytest.mark.parametrize(("arguments", "code"), [
    (["largest", "missing"], 1),
    (["ls", "missing"], 1),
    (["du", "--format", "json"], 2),
    (["scan-info", "1", "--format", "jsonl"], 2),
])
def test_exit_codes_separate_path_failures_from_usage_errors(indexed, monkeypatch, capsys, arguments, code) -> None:
    db, source = indexed
    monkeypatch.chdir(source)
    monkeypatch.setattr(sys, "argv", ["flatlas", *arguments, "--db", str(db)])
    assert main() == code
    assert capsys.readouterr().err.startswith("error: ")
