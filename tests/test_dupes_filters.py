"""Duplicate filters preserve candidate I/O bounds, coverage and export contracts."""

import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas import db, hashing, paths, queries, scanning
from flatlas.cli import app, main
from flatlas.duplicate_filters import DuplicateFilters
from flatlas.errors import FlatlasError
from flatlas.records import GROUP_FIELDS

pytestmark = [pytest.mark.hash, pytest.mark.query]


def _records(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines()]


@pytest.fixture
def indexed(tmp_path: Path, monkeypatch):
    scope = tmp_path / "tree" / "source"
    nested = scope / "nested"
    nested.mkdir(parents=True)
    for prefix, size, count in (("small", 4, 2), ("medium", 8, 3), ("large", 16, 2), ("other", 8, 2)):
        for index in range(count):
            parent = nested if index == 2 else scope
            (parent / f"{prefix}-{index}").write_bytes(prefix[:1].encode() * size)
    (scope / "unique").write_bytes(b"u" * 12)
    (scope / "zero").write_bytes(b"m" * 8)
    (scope / "empty-a").touch()
    (scope / "empty-b").touch()
    outside = scope.parent / "outside"
    outside.mkdir()
    (outside / "medium-outside").write_bytes(b"m" * 8)
    monkeypatch.setattr(
        "flatlas.scanning.allocated_size",
        lambda path, _metadata: 0 if path.name == "zero" else None if path.name == "medium-1" else 4096,
    )
    database = tmp_path / "index.sqlite"
    with db.open_database(database) as connection:
        root_id = paths.register_namespace(connection, paths.discover_namespace(scope))
        scanning.scan_directory(connection, scope.parent)
    return scope, database, root_id


@pytest.mark.parametrize("cached", [False, True])
def test_size_filters_bound_reads_and_cached_groups(indexed, monkeypatch, cached) -> None:
    scope, database, _ = indexed
    with db.open_database(database) as connection:
        if cached:
            hashing.ensure_duplicate_hashes(connection, scope.parent)
        before_paths = [tuple(row) for row in connection.execute("SELECT * FROM path ORDER BY id")]
        before_scopes = [tuple(row) for row in connection.execute("SELECT * FROM scan_scope ORDER BY id")]
        before_hashes = [tuple(row) for row in connection.execute("SELECT * FROM file_hash ORDER BY path_id")]
        reads = []
        digest = hashing._validated_digest

        def observe(candidate, *, full):
            reads.append(Path(candidate["path_display"]).name)
            return digest(candidate, full=full)

        monkeypatch.setattr(hashing, "_validated_digest", observe)
        filters = DuplicateFilters(min_size=8, max_size=8)
        summary = hashing.ensure_duplicate_hashes(connection, scope, filters=filters)
        assert summary.complete
        assert summary.candidate_files == 5
        assert summary.candidate_bytes == 40
        assert summary.content_files_read == (0 if cached else 5)
        assert sorted(reads) == ([] if cached else ["medium-0", "medium-1", "medium-2", "other-0", "other-1"])
        groups = queries.duplicate_groups(connection, scope=scope, filters=filters)
        assert [(group["count"], group["theoretical_savings"]) for group in groups] == [(3, 16), (2, 8)]
        assert all(group["logical_size"] == 8 for group in groups)
        assert all("outside" not in path["path_display"] for group in groups for path in group["paths"])
        assert [tuple(row) for row in connection.execute("SELECT * FROM path ORDER BY id")] == before_paths
        assert [tuple(row) for row in connection.execute("SELECT * FROM scan_scope ORDER BY id")] == before_scopes
        if cached:
            assert [tuple(row) for row in connection.execute("SELECT * FROM file_hash ORDER BY path_id")] == before_hashes


@pytest.mark.parametrize("filters,expected", [
    (DuplicateFilters(), [(16, 2), (8, 3), (8, 2), (4, 2)]),
    (DuplicateFilters(min_count=3), [(8, 3)]),
    (DuplicateFilters(min_savings=16), [(16, 2), (8, 3)]),
    (DuplicateFilters(min_savings=17), []),
    (DuplicateFilters(limit=1), [(16, 2)]),
    (DuplicateFilters(min_count=3, limit=1), [(8, 3)]),
    (DuplicateFilters(min_size=0, max_size=0), []),
    (DuplicateFilters(min_size=9, max_size=15), []),
])
def test_group_filters_follow_full_grouping_and_stable_sort(indexed, filters, expected) -> None:
    scope, database, _ = indexed
    with db.open_database(database) as connection:
        hashing.ensure_duplicate_hashes(connection, scope)
        groups = queries.duplicate_groups(connection, scope=scope, filters=filters)
        assert [(group["logical_size"], group["count"]) for group in groups] == expected
        assert all(len(group["paths"]) == group["count"] for group in groups)


def test_size_filters_apply_to_root_and_global_queries(indexed) -> None:
    scope, database, root_id = indexed
    with db.open_database(database) as connection:
        hashing.ensure_duplicate_hashes(connection, scope.parent)
        filters = DuplicateFilters(min_size=8, max_size=8, min_count=3, limit=1)
        for kwargs in ({}, {"root_id": root_id}):
            groups = queries.duplicate_groups(connection, filters=filters, **kwargs)
            assert len(groups) == 1
            assert groups[0]["logical_size"] == 8
            assert groups[0]["count"] == 4


@pytest.mark.parametrize("filters", [DuplicateFilters(max_size=0), DuplicateFilters(min_size=9, max_size=15)])
def test_empty_candidate_filters_do_not_read_or_create_hashes(indexed, monkeypatch, filters) -> None:
    scope, database, _ = indexed
    monkeypatch.setattr(hashing, "_validated_digest", lambda *_args, **_kwargs: pytest.fail("no candidates to read"))
    with db.open_database(database) as connection:
        summary = hashing.ensure_duplicate_hashes(connection, scope, filters=filters)
        assert summary.complete and summary.candidate_files == summary.content_files_read == 0
        assert queries.duplicate_groups(connection, scope=scope, filters=filters) == []
        assert connection.execute("SELECT count(*) FROM file_hash").fetchone()[0] == 0


def test_equal_savings_and_size_use_digest_tie_breaker(indexed) -> None:
    scope, database, _ = indexed
    (scope / "other-2").write_bytes(b"o" * 8)
    with db.open_database(database) as connection:
        scanning.scan_directory(connection, scope)
        hashing.ensure_duplicate_hashes(connection, scope)
        filters = DuplicateFilters(min_size=8, max_size=8, min_count=3)
        groups = queries.duplicate_groups(connection, scope=scope, filters=filters)
        assert len(groups) == 2
        assert [group["digest"] for group in groups] == sorted(group["digest"] for group in groups)
        assert queries.duplicate_groups(connection, scope=scope, filters=DuplicateFilters(8, 8, 3, limit=1)) == groups[:1]


@pytest.mark.scan
@pytest.mark.safety
def test_local_rescan_combines_new_and_existing_hashes_under_filters(indexed) -> None:
    scope, database, _ = indexed
    filters = DuplicateFilters(min_size=8, max_size=8, min_count=3)
    with db.open_database(database) as connection:
        hashing.ensure_duplicate_hashes(connection, scope)
        (scope / "nested" / "medium-2").unlink()
        (scope / "nested" / "other-2").write_bytes(b"o" * 8)
        assert scanning.scan_directory(connection, scope / "nested")["status"] == "completed"
        summary = hashing.ensure_duplicate_hashes(connection, scope, filters=filters)
        assert summary.complete and summary.content_files_read == 1
        groups = queries.duplicate_groups(connection, scope=scope, filters=filters)
        assert len(groups) == 1 and groups[0]["count"] == 3
        assert {Path(path["path_display"]).name for path in groups[0]["paths"]} == {"other-0", "other-1", "other-2"}
        deleted = [row[0] for row in connection.execute("SELECT path_display FROM path_with_display WHERE state='deleted'")]
        assert deleted == [str(scope / "nested" / "medium-2")]
        assert connection.execute("SELECT state FROM path_with_display WHERE path_display=?", (str(scope / "small-0"),)).fetchone()[0] == "present"


@pytest.mark.safety
def test_group_filter_does_not_hide_hash_errors_or_change_coverage(indexed, monkeypatch) -> None:
    scope, database, _ = indexed
    digest = hashing._validated_digest

    def fail_one(candidate, *, full):
        if Path(candidate["path_display"]).name == "other-0":
            raise OSError("injected read error")
        return digest(candidate, full=full)

    monkeypatch.setattr(hashing, "_validated_digest", fail_one)
    with db.open_database(database) as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM scan_scope ORDER BY id")]
    result = CliRunner().invoke(app, [
        "dupes", str(scope), "--db", str(database), "--format", "jsonl",
        "--min-size", "8", "--max-size", "8", "--min-count", "3", "--limit", "1",
    ])
    assert result.exit_code == 0, result.output
    hash_record, group, issue = _records(result.stdout)
    assert hash_record["complete"] is False and hash_record["errors"] == 1
    assert group["type"] == "group" and group["count"] == 3
    assert issue == {
        "type": "hash_error", "path": str(scope / "other-0"), "stage": "quick",
        "status": "failed", "error_code": None, "message": "injected read error",
    }
    assert "incomplete" in result.stderr
    with db.open_database(database) as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM scan_scope ORDER BY id")] == before
        assert connection.execute("SELECT count(*) FROM path WHERE state='deleted'").fetchone()[0] == 0


@pytest.mark.parametrize("fmt", ["table", "jsonl", "csv"])
@pytest.mark.cli
@pytest.mark.output
def test_cli_and_export_share_filters_and_paths(indexed, tmp_path: Path, fmt) -> None:
    scope, database, _ = indexed
    runner = CliRunner()
    options = ["--db", str(database), "--format", fmt, "--min-size", "8", "--max-size", "8",
               "--min-count", "3", "--min-savings", "16", "-n", "1"]
    result = runner.invoke(app, ["dupes", str(scope), *options])
    assert result.exit_code == 0, result.output
    assert "medium-0" in result.stdout
    assert "small" not in result.stdout and "large" not in result.stdout and "other" not in result.stdout
    if fmt == "jsonl":
        hash_record, *groups = _records(result.stdout)
        assert [hash_record["type"], *(group["type"] for group in groups)] == ["hash", "group"]
        assert hash_record["candidate_files"] == 5
        assert groups[0]["count"] == 3
        # Machine output is always absolute; --absolute only affects the table.
        assert Path(groups[0]["paths"][0]).is_absolute()
    elif fmt == "csv":
        rows = list(csv.DictReader(result.stdout.splitlines()))
        assert len(rows) == 1 and rows[0]["theoretical_savings"] == "16"
    else:
        assert "1 duplicate group" in result.stdout and "2 redundant files" in result.stdout
    output = tmp_path / f"groups.{fmt}"
    exported = runner.invoke(app, ["dupes", str(scope), *options, "--absolute", "--output", str(output)])
    assert exported.exit_code == 0, exported.output
    assert "medium-0" in output.read_text(encoding="utf-8")
    if fmt == "table":
        assert str(scope) in output.read_text(encoding="utf-8")
    if fmt == "jsonl":
        hash_record, *groups = _records(output.read_text(encoding="utf-8"))
        assert hash_record["content_files_read"] == 0
        assert Path(groups[0]["paths"][0]).is_absolute()


@pytest.mark.parametrize("fmt", ["table", "jsonl", "csv"])
@pytest.mark.cli
@pytest.mark.output
def test_empty_filtered_export_preserves_existing_format(indexed, tmp_path: Path, fmt) -> None:
    scope, database, _ = indexed
    output = tmp_path / "empty-output"
    result = CliRunner().invoke(app, [
        "dupes", str(scope), "--db", str(database), "--format", fmt,
        "--max-size", "0", "--output", str(output),
    ])
    assert result.exit_code == 0, result.output
    text = output.read_text(encoding="utf-8")
    if fmt == "jsonl":
        # Still says how complete the (empty) result is.
        assert _records(text) == [{"type": "hash", **_records(text)[0], "candidate_files": 0}]
    elif fmt == "csv":
        # The header keeps the schema visible even with no rows.
        assert text == ",".join(GROUP_FIELDS) + "\n"
    else:
        assert "0 duplicate groups" in text


@pytest.mark.parametrize("options", [
    ["--min-size", "-1"], ["--max-size", "-1"], ["--min-count", "1"],
    ["--min-savings", "-1"], ["--limit", "0"],
    ["--min-size", "9", "--max-size", "8"],
    ["--min-size", str(2**63)], ["--max-size", str(2**63)],
])
@pytest.mark.parametrize("to_file", [False, True])
@pytest.mark.cli
def test_invalid_filters_fail_before_opening_database(options, to_file, tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("flatlas.cli._open_database", lambda *_args, **_kwargs: pytest.fail("must validate before opening database"))
    command = ["dupes", "--output", str(tmp_path / "unused")] if to_file else ["dupes"]
    monkeypatch.setattr("sys.argv", ["flatlas", *command, *options])
    try:
        code = main()
    except SystemExit as exc:
        code = exc.code
    assert code == 2
    assert "Traceback" not in capsys.readouterr().err
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("kwargs", [
    {"min_size": -1}, {"max_size": -1}, {"min_size": 10, "max_size": 9},
    {"min_count": 1}, {"min_savings": -1}, {"limit": 0},
])
def test_core_filters_reject_invalid_values(kwargs) -> None:
    with pytest.raises(FlatlasError):
        DuplicateFilters(**kwargs)
