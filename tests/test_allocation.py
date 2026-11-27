import ctypes
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from flatlas import allocation, scanning
from flatlas.cli import app
from flatlas.db import open_database
from flatlas.paths import (
    discover_namespace,
    register_namespace,
)
from flatlas.queries import (
    disk_usage,
    list_child_directories,
    scan_info,
)
from flatlas.scanning import scan_directory

pytestmark = pytest.mark.allocation


@pytest.mark.parametrize("size,expected", [(0, 0), (1, 4096), (4096, 4096), (5000, 8192), (2**32 + 1, 2**32 + 4096)])
def test_windows_regular_file_rounds_to_volume_cluster(monkeypatch, size, expected) -> None:
    monkeypatch.setattr(allocation, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(allocation, "_cluster_size", lambda _path: 4096)
    monkeypatch.setattr(allocation, "_has_no_extents", lambda _path: False)
    metadata = cast(os.stat_result, SimpleNamespace(st_size=size, st_file_attributes=0))
    phases = []

    @contextmanager
    def phase(name):
        phases.append(name)
        yield

    assert allocation.allocated_size(Path("file"), metadata, phase=phase) == expected
    assert phases == (["extents", "cluster_size"] if size else [])


@pytest.mark.parametrize("attributes", [0x200, 0x800, 0xA00])
def test_windows_sparse_and_compressed_sizes_are_not_rounded(monkeypatch, attributes) -> None:
    monkeypatch.setattr(allocation, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(allocation, "_compressed_size", lambda _path: 123)
    monkeypatch.setattr(allocation, "_cluster_size", lambda _path: pytest.fail("must not round"))
    monkeypatch.setattr(allocation, "_has_no_extents", lambda _path: pytest.fail("must not query compressed extents"))
    metadata = cast(os.stat_result, SimpleNamespace(st_size=9999, st_file_attributes=attributes))
    assert allocation.allocated_size(Path("file"), metadata) == 123


def test_nonempty_resident_file_has_zero_allocation(monkeypatch) -> None:
    monkeypatch.setattr(allocation, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(allocation, "_has_no_extents", lambda _path: True)
    monkeypatch.setattr(allocation, "_cluster_size", lambda _path: pytest.fail("resident data must not round"))
    metadata = cast(os.stat_result, SimpleNamespace(st_size=5, st_file_attributes=0))
    assert allocation.allocated_size(Path("file"), metadata) == 0


@pytest.mark.parametrize("result,error,expected", [(True, 0, False), (False, 38, True), (False, 234, False), (False, 5, None), (False, 50, None)])
def test_extent_query_results_and_handle_cleanup(monkeypatch, result, error, expected) -> None:
    closed = []

    def create(_path, access, share, _security, disposition, flags, _template):
        assert (access, share, disposition, flags) == (0x80, 7, 3, 0x200000)
        return 123

    def attributes(handle, kind, output, _size):
        assert (handle, kind) == (123, 9)
        output[0] = 0
        return True

    def query(handle, code, starting, _input_size, _output, _output_size, _returned, _overlapped):
        assert handle == 123 and code == 0x90073
        assert ctypes.cast(starting, ctypes.POINTER(ctypes.c_int64))[0] == 0
        return result

    api = SimpleNamespace(CreateFileW=create, GetFileInformationByHandleEx=attributes, DeviceIoControl=query, CloseHandle=closed.append)
    monkeypatch.setattr(allocation, "_windows_api", lambda: api)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "extent query failed"), raising=False)
    if expected is None:
        with pytest.raises(OSError):
            allocation._has_no_extents(Path("file"))
    else:
        assert allocation._has_no_extents(Path("file")) is expected
    assert closed == [123]


@pytest.mark.parametrize("failure", ["open", "attributes", "reparse", "directory", "compressed", "sparse"])
def test_extent_query_open_errors_and_replacement_types(monkeypatch, failure) -> None:
    closed = []

    def attributes(_handle, _kind, output, _size):
        output[0] = {"reparse": 0x400, "directory": 0x10, "compressed": 0x800, "sparse": 0x200}.get(failure, 0)
        return failure != "attributes"

    api = SimpleNamespace(
        CreateFileW=lambda *_args: ctypes.c_void_p(-1).value if failure == "open" else 123,
        GetFileInformationByHandleEx=attributes,
        DeviceIoControl=lambda *_args: pytest.fail("must not query extents after failure"),
        CloseHandle=closed.append,
    )
    monkeypatch.setattr(allocation, "_windows_api", lambda: api)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "API failed"), raising=False)
    with pytest.raises(OSError):
        allocation._has_no_extents(Path("file"))
    assert closed == ([] if failure == "open" else [123])


def test_posix_blocks_and_unknown_remain_distinct(monkeypatch) -> None:
    monkeypatch.setattr(allocation, "sys", SimpleNamespace(platform="linux"))
    for metadata, expected in [(SimpleNamespace(st_blocks=0), 0), (SimpleNamespace(st_blocks=16), 8192), (SimpleNamespace(), None)]:
        assert allocation.allocated_size(Path("file"), metadata) == expected


@pytest.mark.parametrize("low,high,error,expected", [
    (0, 0, 0, 0), (3, 2, 0, (2 << 32) + 3),
    (0xFFFFFFFF, 1, 0, (1 << 32) + 0xFFFFFFFF), (0xFFFFFFFF, 0, 5, None),
])
def test_compressed_size_64_bit_and_error_sentinel(monkeypatch, low, high, error, expected) -> None:
    last_error = [99]

    def query(_path, pointer):
        assert last_error[0] == 0
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0] = high
        last_error[0] = error
        return low

    monkeypatch.setattr(allocation, "_windows_api", lambda: SimpleNamespace(GetCompressedFileSizeW=query))
    monkeypatch.setattr(ctypes, "set_last_error", lambda value: last_error.__setitem__(0, value), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error[0], raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "API failed"), raising=False)
    if expected is None:
        with pytest.raises(OSError):
            allocation._compressed_size(Path("file"))
    else:
        assert allocation._compressed_size(Path("file")) == expected


@pytest.mark.parametrize("failure", [None, "volume", "disk", "zero"])
def test_cluster_uses_actual_volume_and_propagates_failures(monkeypatch, failure) -> None:
    def volume(_path, buffer, _length):
        buffer.value = "C:\\mounted-volume\\"
        return failure != "volume"

    def disk(root, sectors, sector_bytes, _free, _total):
        assert root == "C:\\mounted-volume\\"
        ctypes.cast(sectors, ctypes.POINTER(ctypes.c_uint32))[0] = 0 if failure == "zero" else 8
        ctypes.cast(sector_bytes, ctypes.POINTER(ctypes.c_uint32))[0] = 512
        return failure != "disk"

    monkeypatch.setattr(allocation, "_windows_api", lambda: SimpleNamespace(GetVolumePathNameW=volume, GetDiskFreeSpaceW=disk))
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "API failed"), raising=False)
    if failure is None:
        assert allocation._cluster_size(Path("file")) == 4096
    else:
        with pytest.raises(OSError):
            allocation._cluster_size(Path("file"))


def test_cluster_cache_resolves_each_directory_and_reuses_volume_aliases(monkeypatch) -> None:
    located = []
    queried = []

    def mount_point(path):
        located.append(path)
        return {"base": "drive", "mounted": "nested mount", "alias": "second mount"}[path.parent.name]

    def cluster_size(volume):
        queried.append(volume)
        return {"first guid": 4096, "second guid": 65536}[volume]

    monkeypatch.setattr(allocation, "_volume_path", mount_point)
    monkeypatch.setattr(allocation, "_volume_guid", lambda mount: "first guid" if mount == "drive" else "second guid")
    monkeypatch.setattr(allocation, "_volume_device", lambda volume: 1 if volume == "first guid" else 2)
    monkeypatch.setattr(allocation, "_volume_cluster_size", cluster_size)
    cache = allocation.WindowsAllocationCache()
    base = Path("base")
    mounted = base / "mounted"
    alias = base / "alias"
    assert cache.cluster_size(base / "a", 1) == 4096
    assert cache.cluster_size(base / "b", 1) == 4096
    assert cache.cluster_size(mounted / "a", 2) == 65536
    assert cache.cluster_size(mounted / "b", 2) == 65536
    assert cache.cluster_size(alias / "a", 2) == 65536
    assert located == [base / "a", mounted / "a", alias / "a"]
    assert queried == ["first guid", "second guid"]

    monkeypatch.setattr(allocation, "_volume_path", lambda _path: "nested mount")
    assert cache.cluster_size(base / "c", 2) == 65536  # The directory now resolves to another device.


@pytest.mark.parametrize("fallback", ["unknown device", "no GUID", "GUID query fails", "volume changed"])
def test_cluster_cache_fallback_keeps_per_file_queries(monkeypatch, fallback) -> None:
    located = []

    def mount_point(path):
        located.append(path)
        return "mount"

    def cluster_size(volume):
        if volume != "mount":
            raise OSError("GUID path is unavailable")
        return 8192

    monkeypatch.setattr(allocation, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(allocation, "_has_no_extents", lambda _path: False)
    monkeypatch.setattr(allocation, "_volume_path", mount_point)
    monkeypatch.setattr(allocation, "_volume_guid", lambda _mount: None if fallback == "no GUID" else "guid")
    monkeypatch.setattr(allocation, "_volume_device", lambda _volume: 2 if fallback == "volume changed" else 1)
    monkeypatch.setattr(allocation, "_volume_cluster_size", cluster_size)
    cache = allocation.WindowsAllocationCache()
    device = 0 if fallback == "unknown device" else 1
    metadata = cast(os.stat_result, SimpleNamespace(st_size=5000, st_dev=device))
    files = [Path("directory") / name for name in ("a", "b")]
    assert [allocation.allocated_size(path, metadata, cache=cache) for path in files] == [8192, 8192]
    assert located == files


@pytest.mark.scan
@pytest.mark.safety
def test_allocation_failure_preserves_metadata_and_prevents_false_deletion(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source"
    nested = source / "nested"
    outside = source / "outside"
    nested.mkdir(parents=True)
    outside.mkdir()
    target = nested / "file"
    target.write_bytes(b"a")
    missing = nested / "missing"
    missing.write_bytes(b"b")
    (outside / "untouched").write_bytes(b"c")
    monkeypatch.setattr(scanning, "allocated_size", lambda _path, _metadata, **_kwargs: 4096)
    with open_database(tmp_path / "index.sqlite") as connection:
        register_namespace(connection, discover_namespace(source))
        scan_directory(connection, source)
        old_freshness = connection.execute(
            "SELECT self_completed_at_ns FROM directory_summary JOIN path_with_display p ON p.id=path_id WHERE path_display=?",
            (str(nested),),
        ).fetchone()[0]
        missing.unlink()
        target.write_bytes(b"changed")

        def fail(path, _metadata, **_kwargs):
            if path == target:
                raise OSError(5, "allocation query failed")
            return 4096

        monkeypatch.setattr(scanning, "allocated_size", fail)
        result = scan_directory(connection, nested)
        assert result["status"] == "partial"
        assert result["files_seen"] == 1
        audit = scan_info(connection, result["scan_id"])
        assert audit["errors_seen"] == 1
        assert audit["errors"][0]["operation"] == "stat"
        assert "allocation query failed" in audit["errors"][0]["message"]
        rows = connection.execute("SELECT path_display, state, logical_size, allocated_size FROM path_with_display WHERE entry_kind='file'").fetchall()
        indexed = {row["path_display"]: row for row in rows}
        assert all(row["state"] == "present" for row in rows)
        assert indexed[str(target)]["logical_size"] == 7
        assert indexed[str(target)]["allocated_size"] is None
        assert indexed[str(outside / "untouched")]["allocated_size"] == 4096
        assert disk_usage(connection, scope=nested)[0]["allocated_size"] is None
        assert disk_usage(connection, scope=source)[0]["allocated_size"] is None
        assert connection.execute(
            "SELECT self_completed_at_ns FROM directory_summary JOIN path_with_display p ON p.id=path_id WHERE path_display=?",
            (str(nested),),
        ).fetchone()[0] == old_freshness
        live = list_child_directories(connection, scope=nested)
        assert next(row for row in live if Path(row["path"]).name == "file")["allocated_size"] is None
        monkeypatch.setattr(scanning, "allocated_size", lambda _path, _metadata, **_kwargs: 4096)
        assert scan_directory(connection, nested)["status"] == "completed"
        assert disk_usage(connection, scope=source)[0]["allocated_size"] == 8192


@pytest.mark.safety
def test_allocation_is_never_queried_for_directories_or_symlinks(tmp_path, monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("special entries must not query allocation")

    monkeypatch.setattr(scanning, "allocated_size", forbidden)
    namespace = discover_namespace(tmp_path)
    assert scanning._observation(namespace, tmp_path).allocated_size is None
    link = tmp_path / "link"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    assert scanning._observation(namespace, link).allocated_size is None


@pytest.mark.safety
def test_reparse_file_is_not_queried(tmp_path, monkeypatch) -> None:
    namespace = discover_namespace(tmp_path)
    monkeypatch.setattr(Path, "lstat", lambda _path: SimpleNamespace(st_mode=0o100644, st_file_attributes=0x400))
    monkeypatch.setattr(scanning.os.path, "islink", lambda _path: False)
    monkeypatch.setattr(scanning, "allocated_size", lambda *_args, **_kwargs: pytest.fail("must not follow reparse"))
    observed = scanning._observation(namespace, tmp_path / "reparse")
    assert observed.kind == "reparse"
    assert observed.allocated_size is None


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows allocation APIs")
def test_native_windows_scan_and_ls_du_outputs(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "文件"
    nested = source / "nested"
    nested.mkdir(parents=True)
    file = nested / "数据.bin"
    file.write_bytes(b"x" * 5000)
    (nested / "empty").touch()
    expected = allocation.allocated_size(file, file.lstat())
    assert expected is not None and expected > 0
    assert allocation.allocated_size(nested / "empty", (nested / "empty").lstat()) == 0
    sibling = nested / "second.bin"
    sibling.write_bytes(b"x" * 5000)
    located = []
    volume_path = allocation._volume_path

    def locate(path):
        located.append(path)
        return volume_path(path)

    monkeypatch.setattr(allocation, "_volume_path", locate)
    database = tmp_path / "index.sqlite"
    with open_database(database) as connection:
        register_namespace(connection, discover_namespace(source))
        for scans in (1, 2):
            assert scan_directory(connection, source)["status"] == "completed"
            assert disk_usage(connection, scope=source)[0]["allocated_size"] == 2 * expected
            assert list_child_directories(connection, scope=source)[0]["allocated_size"] == 2 * expected
            assert len(located) == scans and all(path.parent == nested for path in located)
    runner = CliRunner()
    for command in ["ls", "du"]:
        result = runner.invoke(app, [command, str(source), "--db", str(database)])
        assert result.exit_code == 0, result.output
        assert "ALLOC(B)" in result.stdout
        assert str(2 * expected) in result.stdout


@pytest.mark.scan
@pytest.mark.safety
@pytest.mark.skipif(sys.platform != "win32", reason="native Windows junctions")
def test_scan_does_not_resolve_volumes_through_junction_entries(tmp_path: Path, monkeypatch) -> None:
    winapi = pytest.importorskip("_winapi")
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (target / "outside.bin").write_bytes(b"x" * 5000)
    try:
        winapi.CreateJunction(str(target), str(source / "junction"))
    except OSError as exc:
        pytest.skip(f"junction creation unavailable: {exc}")

    def unexpected_lookup(_path):
        pytest.fail("must not look up a volume for a junction or its descendants")

    monkeypatch.setattr(allocation, "_volume_path", unexpected_lookup)
    with open_database(tmp_path / "index.sqlite") as connection:
        register_namespace(connection, discover_namespace(source))
        result = scan_directory(connection, source)
        assert result["status"] == "completed"
        assert result["files_seen"] == 0
        row = connection.execute("SELECT entry_kind, allocated_size FROM path WHERE name_raw=?", (b"junction",)).fetchone()
        assert tuple(row) == ("reparse", None)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows sparse/compression controls")
@pytest.mark.parametrize("sparse", [True, False])
def test_native_windows_sparse_and_compressed_files(tmp_path: Path, sparse: bool) -> None:
    if sys.platform != "win32":
        pytest.skip("native Windows sparse/compression controls")
    import msvcrt

    source = tmp_path / "source"
    source.mkdir()
    file = source / "payload.bin"
    api: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    api.DeviceIoControl.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
    ]
    api.DeviceIoControl.restype = ctypes.c_int
    with file.open("w+b") as stream:
        if not sparse:
            stream.write(b"x" * (1024 * 1024))
            stream.flush()
        mode = ctypes.c_uint16(1)  # COMPRESSION_FORMAT_DEFAULT
        returned = ctypes.c_uint32()
        success = api.DeviceIoControl(
            msvcrt.get_osfhandle(stream.fileno()), 0x900C4 if sparse else 0x9C040,
            None if sparse else ctypes.byref(mode), 0 if sparse else ctypes.sizeof(mode),
            None, 0, ctypes.byref(returned), None,
        )
        if not success:
            pytest.skip(f"filesystem does not permit this control: {ctypes.get_last_error()}")
        if sparse:
            stream.seek(8 * 1024 * 1024)
            stream.write(b"end")
    metadata = file.lstat()
    assert metadata.st_file_attributes & (0x200 if sparse else 0x800)
    size = allocation.allocated_size(file, metadata)
    assert size == allocation._compressed_size(file)
    assert size is not None and 0 < size < metadata.st_size
    with open_database(tmp_path / "index.sqlite") as connection:
        register_namespace(connection, discover_namespace(source))
        assert scan_directory(connection, source)["status"] == "completed"
        assert disk_usage(connection, scope=source)[0]["allocated_size"] == size


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows resident file metadata")
def test_native_resident_file_zero_survives_scan_summaries_and_cli(tmp_path: Path) -> None:
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    file = nested / "tiny.txt"
    file.write_bytes(b"hello")
    if not allocation._has_no_extents(file):
        pytest.skip("this filesystem stored the fixture outside its file record")
    assert file.stat().st_size == 5
    assert allocation.allocated_size(file, file.lstat()) == 0
    database = tmp_path / "index.sqlite"
    with open_database(database) as connection:
        register_namespace(connection, discover_namespace(source))
        assert scan_directory(connection, source)["status"] == "completed"
        for scope in (source, nested, file):
            usage = disk_usage(connection, scope=scope)[0]
            assert usage["logical_size"] == 5
            assert usage["allocated_size"] == 0
        assert list_child_directories(connection, scope=source)[0]["allocated_size"] == 0
    runner = CliRunner()
    for command in ("ls", "du"):
        result = runner.invoke(app, [command, str(source), "--db", str(database)])
        assert result.exit_code == 0, result.output
        assert "ALLOC(B)" in result.stdout
        result = runner.invoke(app, [command, str(source), "--db", str(database), "--format", "jsonl"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout.splitlines()[0])["allocated_size"] == 0
