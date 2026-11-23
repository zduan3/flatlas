"""Scans record per-phase timing and exact-lstat fallbacks in scan.stats_json."""

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas import lazystat, paths
from flatlas.cli import app
from flatlas.core import (
    discover_namespace,
    open_database,
    register_namespace,
    scan_directory,
    scan_info,
)

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
        assert {"readdir", "observe", "db_write", "mark_missing", "summaries"} <= set(stats["phases"])
        assert stats["phases"]["observe"]["calls"] == 3  # nested, a.bin, b.bin
        assert stats["phases"]["readdir"]["calls"] == 2
        assert stats["exact_stat_fallbacks"] == 0
        assert stats["wall_s"] >= sum(phase["s"] for phase in stats["phases"].values()) - 0.01
        assert scan_info(connection, result["scan_id"])["stats"] == stats
    finally:
        connection.close()

    info = CliRunner().invoke(app, ["scan-info", str(result["scan_id"]), "--db", str(tmp_path / "index.sqlite")])
    assert info.exit_code == 0, info.output
    assert '"exact_stat_fallbacks": 0' in info.stdout


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
