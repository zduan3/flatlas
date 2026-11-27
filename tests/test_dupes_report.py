"""Per-invocation hash diagnostics and the CSV report sidecar."""

import csv
import errno
import json
import os
from contextlib import closing
from pathlib import Path
from threading import Barrier, get_ident
from unittest.mock import Mock

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

    def fail_full(candidate, *, full, timer):
        if full and Path(candidate["path_display"]).name == "b":
            raise OSError(errno.EIO, "injected full read failure")
        return original(candidate, full=full, timer=timer)

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
    assert summary["stats"]["phases"]["full_hash"]["calls"] == 2
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


def test_timing_in_table_jsonl_and_report(tmp_path: Path) -> None:
    scope = tmp_path / "scope"
    scope.mkdir()
    for name in ("a", "b"):
        (scope / name).write_bytes(b"x" * (hashing.QUICK_SAMPLE_BYTES * 3))
    db = tmp_path / "index.sqlite"
    with closing(open_database(db)) as connection:
        register_namespace(connection, discover_namespace(scope))
        scan_directory(connection, scope)

    report = tmp_path / "report.jsonl"
    command = ["dupes", str(scope), "--report", str(report), "--db", str(db)]
    runner = CliRunner()
    first = runner.invoke(app, [*command, "--format", "jsonl"])
    assert first.exit_code == 0, first.output
    summary, group = map(json.loads, first.stdout.splitlines())
    assert group["type"] == "group" and group["inodes"] == 2
    assert json.loads(report.read_text(encoding="utf-8")) == summary
    stats = summary["stats"]
    phases = stats["phases"]
    assert stats["unaccounted_s"] >= 0
    assert stats["wall_s"] >= max(phase["s"] for phase in phases.values())
    assert phases["quick_hash"]["calls"] == phases["full_hash"]["calls"] == 2
    assert phases["quick_hash"]["phases"]["open"]["calls"] == 2
    assert phases["quick_hash"]["phases"]["fstat"]["calls"] == 4
    assert phases["writeback_lock"]["calls"] == 1
    assert phases["db_write"]["calls"] == 4
    assert phases["commit"]["calls"] == 1

    cached = runner.invoke(app, command)
    assert cached.exit_code == 0, cached.output
    cached_summary = json.loads(report.read_text(encoding="utf-8"))
    assert cached_summary["cache_hits"] == 2
    assert cached_summary["content_files_read"] == 0
    cached_stats = cached_summary["stats"]
    assert {"index_open", "candidates", "result_groups", "index_close"} <= cached_stats["phases"].keys()
    assert {"quick_hash", "full_hash", "writeback_lock", "db_write"}.isdisjoint(cached_stats["phases"])
    assert f"Timing: {cached_stats['wall_s']:.3f} s total" in cached.stdout
    assert f"result_groups: {cached_stats['phases']['result_groups']['s']:.3f} s · 1 call" in cached.stdout


@pytest.mark.parametrize("short_read", [False, True])
def test_small_hash_reads_validated_size_and_checks_final_stat(tmp_path: Path, monkeypatch, short_read: bool) -> None:
    scope = tmp_path / "scope"
    scope.mkdir()
    size = hashing.QUICK_SAMPLE_BYTES * 2
    for name in ("a", "b"):
        (scope / name).write_bytes(b"x" * size)
    db = tmp_path / "index.sqlite"
    with closing(open_database(db)) as connection:
        register_namespace(connection, discover_namespace(scope))
        scan_directory(connection, scope)

    requests = []
    original = hashing.path_from_key

    class Reader:
        def __init__(self, path: Path, mode: str) -> None:
            self.path = path
            self.stream = path.open(mode)

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            self.stream.close()

        def fileno(self) -> int:
            return self.stream.fileno()

        def read(self, count: int) -> bytes:
            requests.append(count)
            return self.stream.read(count - 1 if short_read and self.path.name == "a" else count)

    def wrap_path(root, key):
        path = original(root, key)
        return Mock(open=lambda mode: Reader(path, mode))

    monkeypatch.setattr(hashing, "path_from_key", wrap_path)
    result = CliRunner().invoke(app, ["dupes", str(scope), "--db", str(db), "--format", "jsonl"])
    assert result.exit_code == 0, result.output
    summary, record = map(json.loads, result.stdout.splitlines())
    assert requests == [size, size]
    phases = summary["stats"]["phases"]["quick_hash"]["phases"]
    assert phases["read"]["calls"] == 2 and phases["fstat"]["calls"] == 4
    if short_read:
        assert not summary["complete"] and summary["errors"] == 1
        assert summary["content_bytes_read"] == 2 * size - 1
        assert record["type"] == "hash_error" and record["error_code"] == errno.EIO
        assert record["status"] == "failed" and record["path"] == str(scope / "a")
    else:
        assert summary["complete"] and summary["content_bytes_read"] == 2 * size
        assert record["type"] == "group" and record["inodes"] == 2


def test_quick_workers_only_parallelize_quick_reads(tmp_path: Path, monkeypatch) -> None:
    scope = tmp_path / "scope"
    scope.mkdir()
    for name in ("a", "b"):
        (scope / name).write_bytes(b"x" * (hashing.QUICK_SAMPLE_BYTES * 3))
    db = tmp_path / "index.sqlite"
    with closing(open_database(db)) as connection:
        register_namespace(connection, discover_namespace(scope))
        scan_directory(connection, scope)

    main_thread = get_ident()
    quick_threads = set()
    both_readers = Barrier(2)
    original = hashing._validated_digest
    original_save = hashing._save_hash

    def read(candidate, *, full, timer):
        if full:
            assert get_ident() == main_thread
        else:
            assert get_ident() != main_thread
            both_readers.wait(timeout=5)
            quick_threads.add(get_ident())
        return original(candidate, full=full, timer=timer)

    def save(*args, **kwargs):
        assert get_ident() == main_thread
        return original_save(*args, **kwargs)

    monkeypatch.setattr(hashing, "_validated_digest", read)
    monkeypatch.setattr(hashing, "_save_hash", save)
    result = CliRunner().invoke(app, [
        "dupes", str(scope), "--db", str(db), "--format", "jsonl", "--quick-workers", "2",
    ])
    assert result.exit_code == 0, result.output
    summary, group = map(json.loads, result.stdout.splitlines())
    assert len(quick_threads) == 2
    assert summary["complete"] and summary["content_files_read"] == 2
    assert summary["content_bytes_read"] == hashing.QUICK_SAMPLE_BYTES * 10
    assert summary["stats"]["phases"]["quick_hash"]["phases"]["open"]["calls"] == 2
    assert group["inodes"] == 2


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("stage", ["quick", "same_basis", "promotion", "full", "full_error"])
def test_hash_write_cannot_overwrite_a_newer_scan(tmp_path: Path, monkeypatch, workers: int, stage: str) -> None:
    scope = tmp_path / "scope"
    first = scope / "a"
    second = scope / "b"
    size = 3 * hashing.QUICK_SAMPLE_BYTES if stage.startswith("full") else 16384
    uses_cache = stage in {"promotion", "full", "full_error"}
    for directory in (first, second):
        directory.mkdir(parents=True)
        (directory / "file").write_bytes(b"x" * size)
    db = tmp_path / "index.sqlite"
    original = hashing._validated_digest

    def rescan() -> None:
        path = first / "file"
        if stage != "same_basis":
            previous = path.stat()
            path.write_bytes(b"y" * size)
            os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
        with closing(open_database(db)) as writer:
            assert scan_directory(writer, first)["status"] == "completed"

    def read(candidate, *, full, timer):
        digest = original(candidate, full=full, timer=timer)
        if Path(candidate["path_display"]).parent == first and stage != "promotion":
            rescan()
            if stage == "full_error":
                raise OSError(errno.EIO, "injected read failure")
        return digest

    def progress(update: hashing.HashProgress) -> None:
        if stage == "promotion" and update.phase == "candidates":
            rescan()

    with closing(open_database(db)) as connection:
        register_namespace(connection, discover_namespace(scope))
        scan_directory(connection, scope)
        if uses_cache:
            assert hashing.ensure_duplicate_hashes(connection, scope).complete
            with connection:
                connection.execute(
                    "UPDATE file_hash SET state='quick_ready', full_algorithm=NULL, full_digest=NULL, full_verification=NULL",
                )
        monkeypatch.setattr(hashing, "_validated_digest", read)
        timer = hashing.DupesTimer()
        summary = hashing.ensure_duplicate_hashes(connection, scope, quick_workers=workers, on_progress=progress, timer=timer)
        hashes = connection.execute(
            "SELECT p.path_display, h.state FROM file_hash h JOIN path_with_display p ON p.id=h.path_id ORDER BY p.path_display",
        ).fetchall()
        expected = [(str(second / "file"), "full_ready")]
        if uses_cache:
            expected.insert(0, (str(first / "file"), "stale"))
        assert [tuple(row) for row in hashes] == expected
    assert not summary.complete
    assert (summary.changed_files, summary.errors) == ((0, 1) if stage == "full_error" else (1, 0))
    assert summary.issues[0].path == str(first / "file")
    phases = timer.as_dict()["phases"]
    assert phases["writeback_lock"]["calls"] == 1 and phases["db_write"]["calls"] == 2


def test_parallel_interrupt_commits_consumed_hashes(tmp_path: Path) -> None:
    scope = tmp_path / "scope"
    scope.mkdir()
    for name in ("a", "b", "c"):
        (scope / name).write_bytes(b"x" * 16384)
    db = tmp_path / "index.sqlite"

    def interrupt(progress: hashing.HashProgress) -> None:
        if progress.phase == "quick" and progress.completed_files == 1:
            raise KeyboardInterrupt

    with closing(open_database(db)) as connection:
        register_namespace(connection, discover_namespace(scope))
        scan_directory(connection, scope)
        with pytest.raises(KeyboardInterrupt):
            hashing.ensure_duplicate_hashes(connection, scope, quick_workers=2, on_progress=interrupt)
    with closing(open_database(db)) as connection:
        summary = hashing.ensure_duplicate_hashes(connection, scope, quick_workers=2)
    assert summary.complete and summary.cache_hits == 1 and summary.content_files_read == 2


def test_count_changes_uses_final_rowcount_for_hash_writes(tmp_path: Path, monkeypatch) -> None:
    scope = tmp_path / "scope"
    scope.mkdir()
    for name in ("a", "b", "c"):
        (scope / name).write_bytes(b"x" * (hashing.QUICK_SAMPLE_BYTES * 3))
    original = hashing._validated_digest

    def read(candidate, *, full, timer):
        if Path(candidate["path_display"]).name == "c":
            raise OSError(errno.EIO, "injected quick read failure")
        return original(candidate, full=full, timer=timer)

    monkeypatch.setattr(hashing, "_validated_digest", read)
    with closing(open_database(tmp_path / "index.sqlite")) as connection:
        register_namespace(connection, discover_namespace(scope))
        scan_directory(connection, scope)
        connection.execute("PRAGMA count_changes=ON")
        timer = hashing.DupesTimer()
        summary = hashing.ensure_duplicate_hashes(connection, scope, quick_workers=2, commit_batch=1, timer=timer)
        states = connection.execute("SELECT state, count(*) FROM file_hash GROUP BY state ORDER BY state").fetchall()
        assert [tuple(row) for row in states] == [("failed", 1), ("full_ready", 2)]
    assert summary.changed_files == 0 and summary.errors == 1
    assert timer.as_dict()["phases"]["commit"]["calls"] == 6


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
