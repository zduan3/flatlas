"""scan --shallow re-lists one directory and confirms only its direct deletions."""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas.cli import app
from flatlas.db import open_database
from flatlas.errors import FlatlasError
from flatlas.paths import (
    discover_namespace,
    register_namespace,
)
from flatlas.queries import disk_usage
from flatlas.scanning import (
    resume_selection,
    resume_selection_from_scan,
    scan_directory,
)

pytestmark = [pytest.mark.scan, pytest.mark.safety]


@pytest.fixture
def indexed(tmp_path: Path):
    """source/{keep/{a,deep/b}, gone/{c,deeper/d}, top.bin} fully scanned."""
    source = tmp_path / "source"
    for relative in ("keep/a", "keep/deep/b", "gone/c", "gone/deeper/d", "top.bin"):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
    connection = open_database(tmp_path / "index.sqlite")
    register_namespace(connection, discover_namespace(source))
    assert scan_directory(connection, source)["status"] == "completed"
    yield connection, source
    connection.close()


def _state(connection, path: Path) -> str | None:
    row = connection.execute("SELECT state FROM path WHERE path_display=?", (str(path),)).fetchone()
    return None if row is None else row["state"]


def test_shallow_scan_confirms_direct_deletions_with_descendants(indexed) -> None:
    connection, source = indexed
    shutil.rmtree(source / "gone")
    (source / "top.bin").unlink()
    (source / "keep" / "a").unlink()  # one level down: not observed by a shallow scan of source
    result = scan_directory(connection, source, shallow=True)
    assert result["status"] == "completed"
    assert (result["files_seen"], result["dirs_seen"]) == (0, 2)  # source itself and keep
    assert result["stats"]["phases"]["readdir"]["calls"] == 1
    for path in ("gone", "gone/c", "gone/deeper/d", "top.bin"):
        assert _state(connection, source / path) == "deleted", path
    assert _state(connection, source / "keep" / "a") == "present"
    [total] = disk_usage(connection, scope=source)
    assert (total["files"], total["logical_size"]) == (2, 20)  # keep/a (stale) and keep/deep/b


def test_shallow_scan_does_not_claim_subdirectories_are_fresh(indexed) -> None:
    connection, source = indexed
    (source / "fresh").mkdir()
    (source / "fresh" / "e").write_bytes(b"e")
    (source / "new.bin").write_bytes(b"n")
    scan_directory(connection, source, shallow=True)
    [fresh] = disk_usage(connection, scope=source / "fresh")
    assert fresh["status"] == "new"  # listed as an entry, never enumerated
    [new_file] = disk_usage(connection, scope=source / "new.bin")
    assert new_file["status"] == "ok"
    [keep] = disk_usage(connection, scope=source / "keep")
    assert keep["status"] == "ok"  # still from the earlier full scan
    # A later full scan of the new directory makes it current.
    scan_directory(connection, source / "fresh")
    [fresh] = disk_usage(connection, scope=source / "fresh")
    assert (fresh["status"], fresh["files"]) == ("ok", 1)


def test_shallow_scans_are_not_resumed(indexed) -> None:
    connection, source = indexed
    shallow_id = scan_directory(connection, source, shallow=True)["scan_id"]
    with pytest.raises(FlatlasError, match="shallow"):
        resume_selection_from_scan(connection, shallow_id)
    # --resume looks past the shallow scan to the last full scan of the scope (completed here).
    with pytest.raises(FlatlasError, match=r"scan 1 .* is completed"):
        resume_selection(connection, source)


def test_cli_shallow_flag_and_exclusions(indexed, tmp_path: Path) -> None:
    _connection, source = indexed
    db = str(tmp_path / "index.sqlite")
    result = CliRunner().invoke(app, ["scan", "--format", "json", "--shallow", str(source), "--db", db])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "completed"
    for extra in (["--full"], ["--resume"], ["--refresh-before", "1d"]):
        rejected = CliRunner().invoke(app, ["scan", "--format", "json", "--shallow", *extra, str(source), "--db", db])
        assert rejected.exit_code != 0
