"""Per-entry metadata and SQLite costs of scanning, and termination handling."""

import os
import signal
import sys
from pathlib import Path

import pytest

from flatlas import cli, scanning
from flatlas.core import (
    discover_namespace,
    open_database,
    register_namespace,
    scan_directory,
)
from flatlas.db import CACHE_SIZE_KIB
from flatlas.paths import parent_key

pytestmark = pytest.mark.scan

posix_signals = pytest.mark.skipif(sys.platform == "win32", reason="POSIX termination signals")


def make_connection(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    connection = open_database(tmp_path / "index.sqlite")
    register_namespace(connection, discover_namespace(source))
    return connection, source


@pytest.fixture
def restore_signals():
    names = [name for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)]
    saved = {name: signal.getsignal(getattr(signal, name)) for name in names}
    yield
    for name, handler in saved.items():
        signal.signal(getattr(signal, name), handler)


@pytest.mark.safety
def test_observation_classifies_each_entry_from_one_lstat(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "file.bin").write_bytes(b"data")
    (tmp_path / "dir").mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(tmp_path / "dir", target_is_directory=True)
    except OSError:
        link = None
    namespace = discover_namespace(tmp_path)
    calls: list[Path] = []
    real_lstat = Path.lstat

    def counting_lstat(self: Path):
        calls.append(self)
        return real_lstat(self)

    def forbidden(*_args, **_kwargs):
        pytest.fail("kind must come from the single lstat result")

    monkeypatch.setattr(Path, "lstat", counting_lstat)
    monkeypatch.setattr(Path, "is_dir", forbidden)
    monkeypatch.setattr(Path, "is_file", forbidden)
    monkeypatch.setattr(scanning.os.path, "islink", forbidden)

    expected = {tmp_path / "file.bin": "file", tmp_path / "dir": "directory"}
    if link is not None:
        expected[link] = "symlink"
    for path, kind in expected.items():
        calls.clear()
        observed = scanning._observation(namespace, path)
        assert observed.kind == kind
        assert calls == [path]
    if link is not None:
        assert scanning._observation(namespace, link).symlink_target == os.fsencode(str(tmp_path / "dir"))


def test_scan_links_parents_without_per_entry_key_lookup(tmp_path: Path, monkeypatch) -> None:
    connection, source = make_connection(tmp_path)
    (source / "a" / "b").mkdir(parents=True)
    (source / "a" / "b" / "deep.bin").write_bytes(b"deep")
    (source / "a" / "top.bin").write_bytes(b"top")
    lookups: list[bytes] = []
    real_parent_id = scanning._parent_id

    def counting_parent_id(connection_, root_id, key):
        lookups.append(key)
        return real_parent_id(connection_, root_id, key)

    monkeypatch.setattr(scanning, "_parent_id", counting_parent_id)
    try:
        for refresh in (None, scanning.now_ns()):
            lookups.clear()
            result = scan_directory(connection, source, refresh_before_ns=refresh)
            assert result["status"] == "completed"
            # Only the namespace root, whose parent is NULL, still resolves by key.
            assert lookups == [b""]
        rows = connection.execute("SELECT id, parent_path_id, path_key FROM path").fetchall()
        by_key = {bytes(row["path_key"]): row["id"] for row in rows}
        for row in rows:
            parent = parent_key(bytes(row["path_key"]))
            assert row["parent_path_id"] == (None if parent is None else by_key[parent])
    finally:
        connection.close()


@pytest.mark.safety
def test_kind_changes_still_clear_summaries_and_stale_hashes(tmp_path: Path) -> None:
    connection, source = make_connection(tmp_path)
    target = source / "shifting"
    target.mkdir()
    (target / "inner.bin").write_bytes(b"inner")
    try:
        scan_directory(connection, source)
        path_id = connection.execute(
            "SELECT id FROM path WHERE path_display=?", (str(target),)
        ).fetchone()["id"]
        assert connection.execute("SELECT 1 FROM directory_summary WHERE path_id=?", (path_id,)).fetchone()

        (target / "inner.bin").unlink()
        target.rmdir()
        target.write_bytes(b"now a file")
        scan_directory(connection, source)
        assert connection.execute("SELECT 1 FROM directory_summary WHERE path_id=?", (path_id,)).fetchone() is None
        row = connection.execute("SELECT entry_kind FROM path WHERE id=?", (path_id,)).fetchone()
        assert row["entry_kind"] == "file"

        connection.execute(
            """INSERT INTO file_hash(path_id, state, quick_algorithm, quick_digest, quick_sample_bytes,
               basis_device, basis_inode, basis_size, basis_mtime_ns)
               SELECT id, 'quick_ready', 'blake3', X'00', 65536, device, inode, logical_size, mtime_ns
               FROM path WHERE id=?""",
            (path_id,),
        )
        connection.commit()
        target.unlink()
        target.mkdir()
        scan_directory(connection, source)
        assert connection.execute("SELECT state FROM file_hash WHERE path_id=?", (path_id,)).fetchone()["state"] == "stale"
        assert connection.execute("SELECT 1 FROM directory_summary WHERE path_id=?", (path_id,)).fetchone()
    finally:
        connection.close()


@pytest.mark.db
def test_open_database_raises_page_cache_ceiling(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "index.sqlite")
    try:
        assert connection.execute("PRAGMA cache_size").fetchone()[0] == -CACHE_SIZE_KIB
    finally:
        connection.close()


@posix_signals
@pytest.mark.cli
def test_termination_signals_raise_keyboard_interrupt(restore_signals) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
    cli._route_termination_signals()
    for signum in (signal.SIGTERM, signal.SIGHUP):
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signum)  # the handler runs before the next bytecode


@posix_signals
@pytest.mark.cli
def test_ignored_hangup_stays_ignored(restore_signals) -> None:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    cli._route_termination_signals()
    assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN


@posix_signals
@pytest.mark.safety
def test_sigterm_during_scan_saves_observed_paths_as_cancelled(tmp_path: Path, restore_signals) -> None:
    connection, source = make_connection(tmp_path)
    payload = source / "payload.bin"
    payload.write_bytes(b"saved")
    (source / "later").mkdir()
    (source / "later" / "unseen.bin").write_bytes(b"unseen")
    cli._route_termination_signals()
    sent = False

    def terminate_once(progress) -> None:
        nonlocal sent
        if progress.phase == "scanning" and progress.files_seen == 1 and not sent:
            sent = True
            os.kill(os.getpid(), signal.SIGTERM)

    try:
        with pytest.raises(KeyboardInterrupt):
            scan_directory(connection, source, on_progress=terminate_once)
        scan = connection.execute("SELECT status, files_seen FROM scan ORDER BY id DESC LIMIT 1").fetchone()
        assert scan["status"] == "cancelled"
        assert scan["files_seen"] == 1
        # Cancellation never confirms deletions.
        assert connection.execute("SELECT count(*) FROM path WHERE state='deleted'").fetchone()[0] == 0
    finally:
        connection.close()
