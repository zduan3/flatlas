"""Duplicate eligibility uses indexed allocation, including cached results."""

import csv
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas import allocation, hashing
from flatlas.cli import app
from flatlas.core import (
    discover_namespace,
    duplicate_groups,
    open_database,
    register_namespace,
    scan_directory,
)
from flatlas.hashing import ensure_duplicate_hashes

pytestmark = [pytest.mark.hash, pytest.mark.allocation]


@pytest.mark.parametrize("cached", [False, True])
def test_dupes_filters_zero_allocation_before_grouping_and_hashing(tmp_path: Path, monkeypatch, cached: bool) -> None:
    source = tmp_path / "source"
    source.mkdir()
    allocations = {
        "zero-a": 0, "zero-b": 0,
        "mixed-zero": 0, "mixed-positive": 4096, "mixed-unknown": None,
        "singleton-zero": 0, "singleton-positive": 4096,
    }
    for name in allocations:
        content = b"resident" if name.startswith("zero") else b"mixed" if name.startswith("mixed") else b"singleton"
        (source / name).write_bytes(content)
    database = tmp_path / "index.sqlite"
    monkeypatch.setattr("flatlas.scanning.allocated_size", lambda path, _metadata: 4096 if cached else allocations[path.name])
    connection = open_database(database)
    try:
        root_id = register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        if cached:
            assert ensure_duplicate_hashes(connection, source).candidate_files == 7
            monkeypatch.setattr("flatlas.scanning.allocated_size", lambda path, _metadata: allocations[path.name])
            scan_directory(connection, source)
        before_hashes = [tuple(row) for row in connection.execute("SELECT * FROM file_hash ORDER BY path_id")]
        before_scopes = [tuple(row) for row in connection.execute("SELECT * FROM scan_scope ORDER BY id")]
        read_paths = []
        real_digest = hashing._validated_digest

        def digest(candidate, *, full):
            read_paths.append(Path(candidate["path_display"]).name)
            return real_digest(candidate, full=full)

        monkeypatch.setattr(hashing, "_validated_digest", digest)
        summary = ensure_duplicate_hashes(connection, source)
        assert summary.complete
        assert summary.candidate_files == 2
        assert summary.candidate_bytes == 10
        assert sorted(read_paths) == ([] if cached else ["mixed-positive", "mixed-unknown"])
        for groups in (
            duplicate_groups(connection),
            duplicate_groups(connection, scope=source),
            duplicate_groups(connection, root_id=root_id),
        ):
            assert len(groups) == 1
            assert groups[0]["count"] == 2
            assert groups[0]["theoretical_savings"] == 5
            assert [Path(row["path_display"]).name for row in groups[0]["paths"]] == ["mixed-positive", "mixed-unknown"]
        assert [tuple(row) for row in connection.execute("SELECT * FROM scan_scope ORDER BY id")] == before_scopes
        assert connection.execute("SELECT count(*) FROM path WHERE state='deleted'").fetchone()[0] == 0
        if cached:
            assert [tuple(row) for row in connection.execute("SELECT * FROM file_hash ORDER BY path_id")] == before_hashes
        else:
            assert connection.execute("SELECT count(*) FROM file_hash").fetchone()[0] == 2
    finally:
        connection.close()

    runner = CliRunner()
    for format in ("table", "json", "csv"):
        result = runner.invoke(app, ["dupes", str(source), "--format", format, "--db", str(database)])
        assert result.exit_code == 0, result.output
        assert "mixed-positive" in result.stdout
        assert "mixed-unknown" in result.stdout
        assert "zero" not in result.stdout
        assert "singleton" not in result.stdout
        if format == "json":
            assert json.loads(result.stdout)["hash"]["candidate_files"] == 2
        elif format == "csv":
            assert next(iter(csv.DictReader(result.stdout.splitlines())))["count"] == "2"
    output = tmp_path / "dupes.json"
    result = runner.invoke(app, ["export", "dupes", str(source), "--output", str(output), "--db", str(database)])
    assert result.exit_code == 0, result.output
    assert json.loads(output.read_text(encoding="utf-8"))["groups"][0]["count"] == 2


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows resident file metadata")
def test_native_resident_duplicates_are_skipped(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in ("a", "b"):
        file = source / name
        file.write_bytes(b"hello")
        if not allocation._has_no_extents(file):
            pytest.skip("this filesystem stored the fixture outside its file record")
    database = tmp_path / "index.sqlite"
    with open_database(database) as connection:
        register_namespace(connection, discover_namespace(source))
        assert scan_directory(connection, source)["status"] == "completed"
    result = CliRunner().invoke(app, ["dupes", str(source), "--format", "json", "--db", str(database)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["hash"]["candidate_files"] == 0
    assert payload["hash"]["content_files_read"] == 0
    assert payload["groups"] == []
    with open_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM file_hash").fetchone()[0] == 0
