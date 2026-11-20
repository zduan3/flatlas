"""Lazy metadata for Lustre roots: detection at registration and use during scans."""

import os
import sys
from pathlib import Path

import pytest

from flatlas import lazystat, mounts, paths, scanning
from flatlas.core import (
    discover_namespace,
    open_database,
    register_namespace,
    scan_directory,
)

pytestmark = pytest.mark.scan

linux_statx = pytest.mark.skipif(not lazystat.available(), reason="statx requires Linux glibc >= 2.28")


def write_mounts(tmp_path: Path, *lines: str) -> Path:
    table = tmp_path / "mounts"
    table.write_text("\n".join(lines) + "\n")
    return table


def test_filesystem_type_matches_exact_mount_point_and_last_entry_wins(tmp_path: Path) -> None:
    table = write_mounts(
        tmp_path,
        "srv:/fs1 /lustre/fs1 nfs rw 0 0",
        "srv:/fs1 /lustre/fs1 lustre rw,flock 0 0",
        "tmpfs /lustre/fs1/sub tmpfs rw 0 0",
        r"tmpfs /mnt/with\040space ext4 rw 0 0",
    )
    assert mounts.filesystem_type(Path("/lustre/fs1"), table) == "lustre"
    assert mounts.filesystem_type(Path("/lustre"), table) is None
    assert mounts.filesystem_type(Path("/mnt/with space"), table) == "ext4"
    assert mounts.filesystem_type(Path("/x"), tmp_path / "missing") is None


@pytest.mark.db
def test_registration_records_and_refreshes_filesystem_type(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    connection = open_database(tmp_path / "index.sqlite")
    namespace = discover_namespace(source)
    try:
        monkeypatch.setattr(paths, "filesystem_type", lambda _mount: None)
        root_id = register_namespace(connection, namespace)
        assert connection.execute("SELECT filesystem_type FROM root WHERE id=?", (root_id,)).fetchone()[0] is None

        # Re-running init on a root created before detection fills the type in.
        monkeypatch.setattr(paths, "filesystem_type", lambda _mount: "lustre")
        assert register_namespace(connection, namespace) == root_id
        assert connection.execute("SELECT filesystem_type FROM root WHERE id=?", (root_id,)).fetchone()[0] == "lustre"

        # An undetectable type never erases a recorded one.
        monkeypatch.setattr(paths, "filesystem_type", lambda _mount: None)
        register_namespace(connection, namespace)
        assert connection.execute("SELECT filesystem_type FROM root WHERE id=?", (root_id,)).fetchone()[0] == "lustre"
    finally:
        connection.close()


@linux_statx
@pytest.mark.safety
def test_lazy_lstat_matches_lstat_fields_the_scanner_reads(tmp_path: Path) -> None:
    file = tmp_path / "file.bin"
    file.write_bytes(b"x" * 5000)
    (tmp_path / "dir").mkdir()
    link = tmp_path / "link"
    link.symlink_to(file)
    for path in (file, tmp_path / "dir", link):
        lazy, exact = lazystat.lazy_lstat(path), os.lstat(path)
        for field in (
            "st_mode", "st_dev", "st_ino", "st_nlink", "st_uid", "st_gid",
            "st_size", "st_blocks", "st_mtime_ns", "st_ctime_ns",
        ):
            assert getattr(lazy, field) == getattr(exact, field), (path, field)
        # Hash validation compares against os.fstat, which has no birth time on Linux.
        assert getattr(lazy, "st_birthtime_ns", None) == getattr(exact, "st_birthtime_ns", None)
    with pytest.raises(FileNotFoundError):
        lazystat.lazy_lstat(tmp_path / "missing")


def test_lazy_lstat_falls_back_without_statx(tmp_path: Path, monkeypatch) -> None:
    file = tmp_path / "file.bin"
    file.write_bytes(b"data")
    monkeypatch.setattr(lazystat, "_statx", None)
    assert lazystat.lazy_lstat(file) == file.lstat()


@pytest.mark.parametrize("fs_type, expect_lazy", [("lustre", True), ("ext4", False), (None, False)])
def test_scan_uses_lazy_metadata_only_for_lustre_roots(tmp_path: Path, monkeypatch, fs_type, expect_lazy) -> None:
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "file.bin").write_bytes(b"data")
    monkeypatch.setattr(paths, "filesystem_type", lambda _mount: fs_type)
    connection = open_database(tmp_path / "index.sqlite")
    register_namespace(connection, discover_namespace(source))
    lazy_calls: list[Path] = []
    real_lazy = lazystat.lazy_lstat

    def counting_lazy(path: Path):
        lazy_calls.append(path)
        return real_lazy(path)

    monkeypatch.setattr(scanning, "lazy_lstat", counting_lazy)
    try:
        result = scan_directory(connection, source)
        assert result["status"] == "completed"
        assert result["files_seen"] == 1
        version = connection.execute("SELECT scanner_version FROM scan ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert version.endswith(scanning.LAZY_SCANNER_SUFFIX) is expect_lazy
        assert bool(lazy_calls) is expect_lazy
        if expect_lazy:
            assert source / "nested" / "file.bin" in lazy_calls
    finally:
        connection.close()


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc/self/mounts")
def test_filesystem_type_reads_live_mount_table() -> None:
    root_type = mounts.filesystem_type(Path("/"))
    assert root_type is None or isinstance(root_type, str)


@linux_statx
@pytest.mark.safety
def test_lazy_lstat_rechecks_regular_files_reporting_size_without_blocks(tmp_path: Path, monkeypatch) -> None:
    file = tmp_path / "file.bin"
    file.write_bytes(b"x" * 5000)
    exact = file.lstat()
    real = lazystat._statx

    def placeholder_statx(dirfd, path, flags, mask, ref):
        result = real(dirfd, path, flags, mask, ref)
        ref._obj.stx_size = 4096  # what Lustre returned without a size-on-MDT record
        ref._obj.stx_blocks = 0
        return result

    monkeypatch.setattr(lazystat, "_statx", placeholder_statx)
    observed = lazystat.lazy_lstat(file)
    assert (observed.st_size, observed.st_blocks) == (exact.st_size, exact.st_blocks)
