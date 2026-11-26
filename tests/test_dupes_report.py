"""Per-invocation hash diagnostics and the CSV report sidecar."""

import csv
import errno
import json
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas import hashing
from flatlas.cli import app
from flatlas.db import open_database
from flatlas.errors import FlatlasError
from flatlas.paths import discover_namespace, register_namespace
from flatlas.scanning import scan_directory

pytestmark = [pytest.mark.hash, pytest.mark.output, pytest.mark.safety]


def test_csv_report_includes_failed_full_hash_even_without_groups(tmp_path: Path, monkeypatch) -> None:
    scope = tmp_path / "scope"
    scope.mkdir()
    size = hashing.QUICK_SAMPLE_BYTES * 2 + 4096
    for name in ("a", "b"):
        (scope / name).write_bytes(b"x" * size)
    db = tmp_path / "index.sqlite"
    with closing(open_database(db)) as connection:
        register_namespace(connection, discover_namespace(scope))
        scan_directory(connection, scope)

    original = hashing._validated_digest

    def fail_full(candidate, *, full):
        if full and Path(candidate["path_display"]).name == "b":
            raise OSError(errno.EIO, "injected full read failure")
        return original(candidate, full=full)

    report = tmp_path / "report.jsonl"
    command = ["dupes", str(scope), "--format", "csv", "--report", str(report), "--db", str(db)]
    runner = CliRunner()
    with monkeypatch.context() as patch:
        patch.setattr(hashing, "_validated_digest", fail_full)
        failed = runner.invoke(app, command)
    assert failed.exit_code == 0, failed.output
    assert list(csv.DictReader(failed.stdout.splitlines())) == []
    summary, issue = map(json.loads, report.read_text(encoding="utf-8").splitlines())
    assert summary["complete"] is False
    assert summary["cache_hits"] == 0
    assert summary["content_bytes_read"] == 4 * hashing.QUICK_SAMPLE_BYTES + size
    assert issue["stage"] == "full" and issue["status"] == "failed"
    assert issue["error_code"] == errno.EIO and issue["path"] == str(scope / "b")

    retried = runner.invoke(app, command)
    assert retried.exit_code == 0, retried.output
    assert len(list(csv.DictReader(retried.stdout.splitlines()))) == 1
    [summary] = map(json.loads, report.read_text(encoding="utf-8").splitlines())
    assert summary["complete"] is True
    assert summary["cache_hits"] == 1
    assert summary["content_bytes_read"] == 2 * hashing.QUICK_SAMPLE_BYTES + size

    cached = runner.invoke(app, command)
    assert cached.exit_code == 0, cached.output
    [summary] = map(json.loads, report.read_text(encoding="utf-8").splitlines())
    assert summary["cache_hits"] == 2
    assert summary["content_bytes_read"] == 0


def test_report_rejects_output_and_index_paths(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    open_database(db).close()
    output = tmp_path / "output.csv"
    runner = CliRunner()
    for report in (output, db, Path(f"{db}-wal")):
        with pytest.raises(FlatlasError, match="--report must differ"):
            runner.invoke(
                app, ["dupes", "--db", str(db), "--output", str(output), "--report", str(report)],
                catch_exceptions=False,
            )
    assert not output.exists()
    open_database(db).close()


def test_jsonl_distinguishes_changed_candidate_from_read_failure(tmp_path: Path) -> None:
    scope = tmp_path / "scope"
    scope.mkdir()
    changed = scope / "changed"
    changed.write_bytes(b"x" * 16384)
    (scope / "peer").write_bytes(b"x" * 16384)
    db = tmp_path / "index.sqlite"
    with closing(open_database(db)) as connection:
        register_namespace(connection, discover_namespace(scope))
        scan_directory(connection, scope)
    changed.write_bytes(b"different")
    result = CliRunner().invoke(app, ["dupes", str(scope), "--format", "jsonl", "--db", str(db)])
    assert result.exit_code == 0, result.output
    summary, issue = map(json.loads, result.stdout.splitlines())
    assert summary["complete"] is False
    assert summary["changed_files"] == 1 and summary["errors"] == 0
    assert issue["type"] == "hash_error" and issue["status"] == "changed"
    assert issue["path"] == str(changed) and issue["stage"] == "quick"
    assert issue["error_code"] is None
