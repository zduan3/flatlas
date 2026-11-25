"""Scans record timings, exact-lstat fallbacks and committed deletions."""

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas import lazystat, paths, scanning
from flatlas.cli import app
from flatlas.db import now_ns, open_database
from flatlas.paths import (
    discover_namespace,
    register_namespace,
)
from flatlas.queries import scan_info
from flatlas.scanning import scan_directory

pytestmark = [pytest.mark.scan]


def _tree(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "a.bin").write_bytes(b"x" * 9020)
    (source / "b.bin").write_bytes(b"y" * 10)
    return source


def test_scan_reports_and_persists_phase_timing(tmp_path: Path) -> None:
    source = _tree(tmp_path)
    connection = open_database(tmp_path / "index.sqlite")
    try:
        register_namespace(connection, discover_namespace(source))
        result = scan_directory(connection, source)
        stats = result["stats"]
        assert result["paths_deleted"] == stats["paths_deleted"] == 0
        assert {"readdir", "observe", "db_write", "mark_missing", "summaries"} <= set(stats["phases"])
        assert stats["phases"]["observe"]["calls"] == 3  # nested, a.bin, b.bin
        assert stats["phases"]["readdir"]["calls"] == 2
        assert stats["exact_stat_fallbacks"] == 0
        assert stats["wall_s"] >= sum(phase["s"] for phase in stats["phases"].values()) - 0.01
        assert scan_info(connection, result["scan_id"])["stats"] == stats
    finally:
        connection.close()

    info = CliRunner().invoke(app, ["scan-info", "--format", "json", str(result["scan_id"]), "--db", str(tmp_path / "index.sqlite")])
    assert info.exit_code == 0, info.output
    assert '"exact_stat_fallbacks": 0' in info.stdout


@pytest.mark.safety
@pytest.mark.parametrize("mode", ["full", "shallow", "refresh"])
def test_paths_deleted_counts_only_new_tombstones(tmp_path: Path, mode: str) -> None:
    source = _tree(tmp_path)
    database = tmp_path / "index.sqlite"
    (source / "keep").mkdir()
    for name in ("old.bin", "later.bin"):
        (source / "keep" / name).write_bytes(b"data")
    with closing(open_database(database)) as connection:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        (source / "keep" / "old.bin").unlink()
        assert scan_directory(connection, source)["paths_deleted"] == 1
        shutil.rmtree(source / "nested")
        (source / "b.bin").unlink()
        (source / "keep" / "later.bin").unlink()
        cutoff = now_ns() if mode == "refresh" else None
        result = scan_directory(connection, source, shallow=mode == "shallow", refresh_before_ns=cutoff)
        expected = 3 if mode == "shallow" else 4
        assert result["paths_deleted"] == result["stats"]["paths_deleted"] == expected
        assert scan_info(connection, result["scan_id"])["stats"]["paths_deleted"] == expected
        cutoff = now_ns() if mode == "refresh" else None
        retry = scan_directory(connection, source, shallow=mode == "shallow", refresh_before_ns=cutoff)
        assert retry["paths_deleted"] == 0


@pytest.mark.cli
@pytest.mark.safety
def test_scan_cli_reports_partial_zero_and_keeps_legacy_stats_unknown(tmp_path: Path, monkeypatch) -> None:
    source = _tree(tmp_path)
    database = tmp_path / "index.sqlite"
    with closing(open_database(database)) as connection:
        register_namespace(connection, discover_namespace(source))
        old_id = scan_directory(connection, source)["scan_id"]
        with connection:
            connection.execute("UPDATE scan SET stats_json=? WHERE id=?", (json.dumps({"wall_s": 1}), old_id))
        (source / "b.bin").unlink()
        scandir = scanning.os.scandir

        def deny_nested(path):
            if Path(path) == source / "nested":
                raise PermissionError("injected")
            return scandir(path)

        with monkeypatch.context() as patch:
            patch.setattr(scanning.os, "scandir", deny_nested)
            result = CliRunner().invoke(app, ["scan", str(source), "--format", "json", "--db", str(database)])
        assert result.exit_code == 0, result.output
        output = json.loads(result.stdout)
        assert output["status"] == "partial"
        assert output["paths_deleted"] == output["stats"]["paths_deleted"] == 0
        assert connection.execute("SELECT count(*) FROM path WHERE state='deleted'").fetchone()[0] == 0
        assert scan_info(connection, output["scan_id"])["stats"]["paths_deleted"] == 0
        assert scan_info(connection, old_id)["stats"] == {"wall_s": 1}

        result = CliRunner().invoke(app, ["scan", str(source), "--db", str(database)])
        assert result.exit_code == 0, result.output
        assert "paths_deleted: 1\n" in result.stdout


@pytest.mark.safety
def test_scan_setup_failure_reports_zero_deletions(tmp_path: Path, monkeypatch) -> None:
    source = _tree(tmp_path)
    with closing(open_database(tmp_path / "index.sqlite")) as connection:
        register_namespace(connection, discover_namespace(source))

        def deny_ancestors(*args, **kwargs):
            raise PermissionError("injected")

        monkeypatch.setattr(scanning, "_ensure_ancestors", deny_ancestors)
        result = scan_directory(connection, source)
        assert result["status"] == "failed"
        assert result["paths_deleted"] == result["stats"]["paths_deleted"] == 0
        assert scan_info(connection, result["scan_id"])["stats"]["paths_deleted"] == 0


@pytest.mark.skipif(not lazystat.available(), reason="statx requires Linux glibc >= 2.28")
def test_scan_counts_exact_lstat_fallbacks_on_lazy_roots(tmp_path: Path, monkeypatch) -> None:
    source = _tree(tmp_path)
    monkeypatch.setattr(paths, "filesystem_type", lambda _mount: "lustre")
    real = lazystat._statx

    def unflushed_blocks(dirfd, path, flags, mask, ref):
        result = real(dirfd, path, flags, mask, ref)
        if path.endswith(b"a.bin"):
            ref._obj.stx_blocks = 1
        return result

    monkeypatch.setattr(lazystat, "_statx", unflushed_blocks)
    connection = open_database(tmp_path / "index.sqlite")
    try:
        register_namespace(connection, discover_namespace(source))
        stats = scan_directory(connection, source)["stats"]
        assert stats["exact_stat_fallbacks"] == 1
    finally:
        connection.close()


def test_migration_adds_stats_column_and_keeps_old_scans(tmp_path: Path) -> None:
    source = _tree(tmp_path)
    database = tmp_path / "index.sqlite"
    connection = open_database(database)
    register_namespace(connection, discover_namespace(source))
    scan_id = scan_directory(connection, source)["scan_id"]
    connection.close()

    raw = sqlite3.connect(database)
    raw.execute("ALTER TABLE scan DROP COLUMN stats_json")
    raw.execute("DELETE FROM schema_migration WHERE version=5")
    raw.commit()
    raw.close()

    connection = open_database(database)
    try:
        info = scan_info(connection, scan_id)
        assert info["stats"] is None and info["files_seen"] == 2
    finally:
        connection.close()


@pytest.mark.cli
def test_scan_without_a_terminal_passes_no_progress_callback(tmp_path: Path, monkeypatch) -> None:
    from flatlas import cli

    source = tmp_path / "source"
    source.mkdir()
    (source / "f").write_bytes(b"x")
    db = tmp_path / "index.sqlite"
    assert CliRunner().invoke(app, ["init", str(source), "--db", str(db)]).exit_code == 0
    seen: list[object] = []
    original = cli.scan_directory

    def recording(*args, **kwargs):
        seen.append(kwargs.get("on_progress"))
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "scan_directory", recording)
    monkeypatch.setattr(cli, "_stderr_is_tty", lambda: False)
    result = CliRunner().invoke(app, ["scan", "--format", "json", str(source), "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert seen == [None]
