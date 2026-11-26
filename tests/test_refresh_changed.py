"""Directory timestamp refresh through the scan CLI and persistent index."""

import json
import os
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas import scanning
from flatlas.cli import app
from flatlas.db import open_database
from flatlas.paths import discover_namespace, register_namespace, remove_indexed_subtree
from flatlas.queries import disk_usage, scan_info

pytestmark = [pytest.mark.scan, pytest.mark.safety]


def test_refresh_lists_changed_descendants_and_preserves_skipped_freshness(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    unchanged = source / "unchanged"
    unchanged.mkdir()
    growing = unchanged / "growing"
    growing.write_bytes(b"x")
    gone = nested / "gone"
    gone.write_bytes(b"x")
    db = tmp_path / "index.sqlite"

    with closing(open_database(db)) as connection:
        register_namespace(connection, discover_namespace(source))
        scan = scanning.scan_directory(connection, source)
        old_freshness = disk_usage(connection, scope=unchanged)[0]["complete_through_ns"]
        growing.write_bytes(b"longer")
        gone.rename(nested / "new")
        mtime = nested.stat().st_mtime_ns + 2_000_000_000
        os.utime(nested, ns=(mtime, mtime))

        real_scandir = os.scandir
        listed: list[Path] = []

        def scandir(path):
            listed.append(Path(path))
            return real_scandir(path)

        with monkeypatch.context() as patch:
            patch.setattr(scanning.os, "scandir", scandir)
            result = CliRunner().invoke(
                app, ["scan", str(source), "--db", str(db), "--refresh-changed", "--format", "json"]
            )
        assert result.exit_code == 0, result.output
        report = json.loads(result.stdout)
        assert listed == [nested]
        assert report["refresh_changed"] is True
        assert report["stats"]["directories_skipped"] == 2
        assert report["paths_deleted"] == 1
        details = scan_info(connection, report["scan_id"])
        assert details["refresh_changed"] is True
        assert details["scopes"][0]["status"] == "completed"
        assert disk_usage(connection, scope=unchanged)[0]["complete_through_ns"] == old_freshness
        assert disk_usage(connection, scope=growing)[0]["logical_size"] == 1
        assert disk_usage(connection, scope=nested / "new")[0]["status"] == "ok"
        assert connection.execute(
            "SELECT last_seen_scan_id FROM path WHERE name_raw=?", (b"growing",)
        ).fetchone()[0] == scan["scan_id"]
        scanning.scan_directory(connection, source)
        assert disk_usage(connection, scope=growing)[0]["logical_size"] == len(b"longer")


def test_directory_error_preserves_old_entries_and_refresh_retries(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    gone = source / "gone"
    gone.touch()
    with closing(open_database(tmp_path / "index.sqlite")) as connection:
        register_namespace(connection, discover_namespace(source))
        scanning.scan_directory(connection, source)
        gone.unlink()
        mtime = source.stat().st_mtime_ns + 2_000_000_000
        os.utime(source, ns=(mtime, mtime))
        real_scandir = os.scandir

        def denied(path):
            if Path(path) == source:
                raise PermissionError("injected readdir failure")
            return real_scandir(path)

        with monkeypatch.context() as patch:
            patch.setattr(scanning.os, "scandir", denied)
            partial = scanning.scan_directory(connection, source, refresh_changed=True)
        assert partial["status"] == "partial"
        assert partial["paths_deleted"] == 0
        assert connection.execute(
            "SELECT state FROM path WHERE name_raw=?", (b"gone",)
        ).fetchone()[0] == "present"
        retried = scanning.scan_directory(connection, source, refresh_changed=True)
        assert retried["paths_deleted"] == 1


def test_unseeded_directory_and_manual_index_removal_require_listing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    item = source / "item"
    item.touch()
    with closing(open_database(tmp_path / "index.sqlite")) as connection:
        register_namespace(connection, discover_namespace(source))
        initial = scanning.scan_directory(connection, source, refresh_changed=True)
        assert initial["stats"]["directories_skipped"] == 0
        remove_indexed_subtree(connection, item)
        refreshed = scanning.scan_directory(connection, source, refresh_changed=True)
        assert refreshed["stats"]["directories_skipped"] == 0
        assert disk_usage(connection, scope=item)[0]["status"] == "ok"


def test_refresh_confirms_descendants_missing_when_directory_becomes_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    old_directory = source / "old"
    old_directory.mkdir(parents=True)
    child = old_directory / "old_child"
    child.touch()
    with closing(open_database(tmp_path / "index.sqlite")) as connection:
        register_namespace(connection, discover_namespace(source))
        scanning.scan_directory(connection, source)
        child.unlink()
        old_directory.rmdir()
        old_directory.touch()
        mtime = source.stat().st_mtime_ns + 2_000_000_000
        os.utime(source, ns=(mtime, mtime))

        refreshed = scanning.scan_directory(connection, source, refresh_changed=True)
        assert refreshed["paths_deleted"] == 1
        assert connection.execute(
            "SELECT state FROM path WHERE name_raw=?", (b"old_child",)
        ).fetchone()[0] == "deleted"
