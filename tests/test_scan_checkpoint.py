"""Scans checkpoint short transactions; killed scans keep them and are resumed."""

import json
import socket
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from flatlas import scanning
from flatlas.db import open_database
from flatlas.errors import FlatlasError
from flatlas.paths import (
    discover_namespace,
    register_namespace,
)
from flatlas.queries import (
    disk_usage,
    scan_info,
)
from flatlas.scanning import (
    resume_selection,
    scan_directory,
)

pytestmark = [pytest.mark.scan, pytest.mark.safety]


@pytest.fixture
def every_entry(monkeypatch):
    """Checkpoint before every entry so tests see each batch boundary."""
    monkeypatch.setattr(scanning, "CHECKPOINT_INTERVAL_S", 0.0)


def _tree(tmp_path: Path, directories: int = 2) -> tuple[Path, Path]:
    source = tmp_path / "source"
    for index in range(directories):
        (source / f"d{index}").mkdir(parents=True)
        (source / f"d{index}" / "f.bin").write_bytes(b"x" * (index + 1))
    database = tmp_path / "index.sqlite"
    connection = open_database(database)
    register_namespace(connection, discover_namespace(source))
    connection.close()
    return source, database


def _reader(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    return connection


def test_checkpoints_are_visible_to_other_connections(tmp_path: Path, every_entry) -> None:
    source, database = _tree(tmp_path)
    seen: dict = {}

    def inspect(progress) -> None:
        if progress.phase != "scanning" or progress.files_seen != 2 or seen:
            return
        reader = _reader(database)
        try:
            seen["scan"] = dict(reader.execute(
                "SELECT status, files_seen, heartbeat_at_ns, owner_json FROM scan"
            ).fetchone())
            # The first directory finished and its summary was committed with it.
            seen["done"] = reader.execute(
                """SELECT ds.subtree_files FROM directory_summary ds JOIN path p ON p.id=ds.path_id
                   WHERE p.parent_path_id=(SELECT id FROM path WHERE path_display=?)
                     AND ds.last_completed_scan_id=(SELECT id FROM scan)""",
                (str(source),),
            ).fetchall()
        finally:
            reader.close()

    connection = open_database(database)
    try:
        result = scan_directory(connection, source, on_progress=inspect)
    finally:
        connection.close()
    assert seen["scan"]["status"] == "running"
    assert seen["scan"]["files_seen"] == 1  # the second file is not committed yet
    assert seen["scan"]["heartbeat_at_ns"] is not None
    assert json.loads(seen["scan"]["owner_json"])["host"] == socket.gethostname()
    assert [row["subtree_files"] for row in seen["done"]] == [1]
    assert result["status"] == "completed" and result["stats"]["checkpoints"] >= 3


KILLED_SCAN = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    from flatlas import scanning
    from flatlas.db import open_database
    from flatlas.scanning import scan_directory
    scanning.CHECKPOINT_INTERVAL_S = 0.0
    def die(progress):
        if progress.phase == "scanning" and progress.files_seen == 3:
            os._exit(9)  # like SIGKILL: no cleanup, no commit
    scan_directory(open_database(Path(sys.argv[1])), Path(sys.argv[2]), on_progress=die)
    """
)


def test_killed_scan_keeps_checkpoints_and_resumes_from_them(tmp_path: Path) -> None:
    source, database = _tree(tmp_path, directories=5)
    connection = open_database(database)
    try:
        scan_directory(connection, source)
        (source / "d4" / "f.bin").unlink()
    finally:
        connection.close()

    killed = subprocess.run([sys.executable, "-c", KILLED_SCAN, str(database), str(source)], check=False)
    assert killed.returncode == 9

    connection = open_database(database)
    try:
        killed_scan = connection.execute("SELECT id, status, files_seen FROM scan ORDER BY id DESC").fetchone()
        assert killed_scan["status"] == "running" and killed_scan["files_seen"] >= 2
        assert connection.execute("SELECT count(*) FROM path WHERE state='deleted'").fetchone()[0] == 0

        selection = resume_selection(connection, source)  # the owner is gone: finalize and resume
        assert selection.scan_id == killed_scan["id"]
        info = scan_info(connection, killed_scan["id"])
        assert info["status"] == "cancelled" and "abandoned" in info["note"]
        assert info["scopes"][0]["status"] == "cancelled"

        resumed = scan_directory(connection, source, refresh_before_ns=selection.refresh_before_ns)
        assert resumed["status"] == "completed"
        # Directories finished before the kill are pruned: only the scope and the rest are listed.
        assert resumed["stats"]["phases"]["readdir"]["calls"] <= 1 + 5 - 2
        [total] = disk_usage(connection, scope=source)
        assert total["status"] == "ok"
    finally:
        connection.close()


def test_resume_refuses_a_scan_whose_owner_is_alive(tmp_path: Path, every_entry) -> None:
    source, database = _tree(tmp_path)
    refused: list[str] = []

    def try_resume(progress) -> None:
        if progress.phase == "scanning" and progress.files_seen == 1 and not refused:
            other = open_database(database)
            try:
                with pytest.raises(FlatlasError, match="still running") as error:
                    resume_selection(other, source)
                refused.append(str(error.value))
            finally:
                other.close()

    connection = open_database(database)
    try:
        assert scan_directory(connection, source, on_progress=try_resume)["status"] == "completed"
    finally:
        connection.close()
    assert refused


def test_owner_liveness_rules(tmp_path: Path) -> None:
    now = 10**18
    fresh, stale = now - 1, now - scanning.ABANDONED_AFTER_NS
    here = scanning.scan_owner()
    finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, check=True)
    dead = json.dumps(here | {"pid": int(finished.stdout)})
    elsewhere = json.dumps(here | {"host": here["host"] + "-other"})
    assert scanning.owner_is_alive(json.dumps(here), fresh, now=now)
    assert not scanning.owner_is_alive(json.dumps(here), stale, now=now)
    assert scanning.owner_is_alive(elsewhere, fresh, now=now)  # cannot probe another host
    assert not scanning.owner_is_alive(elsewhere, stale, now=now)
    assert scanning.owner_is_alive(None, fresh, now=now)
    if here["pid_ns"]:
        assert not scanning.owner_is_alive(dead, fresh, now=now)
        other_namespace = json.dumps(json.loads(dead) | {"pid_ns": "pid:[0]"})
        assert scanning.owner_is_alive(other_namespace, fresh, now=now)


def test_scan_finalized_by_another_process_stops_without_saving(tmp_path: Path, every_entry) -> None:
    source, database = _tree(tmp_path)
    connection = open_database(database)

    def take_over(progress) -> None:
        # Another writer only gets the lock between a checkpoint and the next write.
        if progress.phase == "scanning" and progress.files_seen == 1 and not connection.in_transaction:
            other = _reader(database)
            with other:
                other.execute("UPDATE scan SET status='cancelled', note='taken over'")
            other.close()

    try:
        with pytest.raises(scanning.ScanTakenOverError):
            scan_directory(connection, source, on_progress=take_over)
        row = connection.execute("SELECT status, note FROM scan").fetchone()
        assert (row["status"], row["note"]) == ("cancelled", "taken over")
    finally:
        connection.close()


def test_interrupt_while_saving_a_complete_scan_confirms_nothing(tmp_path: Path) -> None:
    source, database = _tree(tmp_path)
    connection = open_database(database)
    try:
        scan_directory(connection, source)
        (source / "d1" / "f.bin").unlink()

        def interrupt_saving(progress) -> None:
            if progress.phase == "saving":
                raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            scan_directory(connection, source, on_progress=interrupt_saving)
        latest = connection.execute("SELECT status FROM scan ORDER BY id DESC").fetchone()
        assert latest["status"] == "cancelled"
        assert connection.execute(
            "SELECT state FROM path WHERE path_display=?", (str(source / "d1" / "f.bin"),)
        ).fetchone()["state"] == "present"
    finally:
        connection.close()


def test_migration_adds_checkpoint_columns_and_keeps_scans(tmp_path: Path) -> None:
    source, database = _tree(tmp_path)
    connection = open_database(database)
    scan_id = scan_directory(connection, source)["scan_id"]
    connection.close()

    raw = sqlite3.connect(database)
    for column in ("logical_bytes_seen", "heartbeat_at_ns", "owner_json"):
        raw.execute(f"ALTER TABLE scan DROP COLUMN {column}")
    raw.execute("DELETE FROM schema_migration WHERE version=7")
    raw.commit()
    raw.close()

    connection = open_database(database)
    try:
        info = scan_info(connection, scan_id)
        assert (info["files_seen"], info["logical_bytes_seen"], info["owner"]) == (2, 0, None)
    finally:
        connection.close()


def test_unexpected_error_while_saving_marks_the_scan_failed(tmp_path: Path, monkeypatch) -> None:
    source, database = _tree(tmp_path)

    def broken_refresh(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected")

    monkeypatch.setattr(scanning, "refresh_directory_summaries", broken_refresh)
    connection = open_database(database)
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            scan_directory(connection, source)
        assert connection.execute("SELECT status FROM scan").fetchone()["status"] == "failed"
        assert connection.execute("SELECT status FROM scan_scope").fetchone()["status"] == "failed"
    finally:
        connection.close()
