"""Read-only platform allocation metadata (Windows uses Explorer's size-on-disk basis)."""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from functools import cache
from pathlib import Path
from typing import Any, Protocol, cast

from flatlas.lazystat import LazyStat
from flatlas.paths import filesystem_path


class _WindowsCtypes(Protocol):
    """Windows-only ctypes entry points, also replaced by POSIX test doubles."""

    def WinDLL(self, name: str, *, use_last_error: bool) -> Any: ...
    def set_last_error(self, value: int) -> int: ...
    def get_last_error(self) -> int: ...
    def WinError(self, code: int) -> OSError: ...


_windows_ctypes = cast(_WindowsCtypes, ctypes)


@cache
def _windows_api() -> Any:
    api = _windows_ctypes.WinDLL("kernel32", use_last_error=True)
    dword_pointer = ctypes.POINTER(ctypes.c_uint32)
    api.GetCompressedFileSizeW.argtypes = [ctypes.c_wchar_p, dword_pointer]
    api.GetCompressedFileSizeW.restype = ctypes.c_uint32
    api.GetVolumePathNameW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    api.GetVolumePathNameW.restype = ctypes.c_int
    api.GetVolumeNameForVolumeMountPointW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    api.GetVolumeNameForVolumeMountPointW.restype = ctypes.c_int
    api.GetDiskFreeSpaceW.argtypes = [ctypes.c_wchar_p, *([dword_pointer] * 4)]
    api.GetDiskFreeSpaceW.restype = ctypes.c_int
    api.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    api.CreateFileW.restype = ctypes.c_void_p
    api.GetFileInformationByHandleEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    api.GetFileInformationByHandleEx.restype = ctypes.c_int
    api.DeviceIoControl.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, dword_pointer, ctypes.c_void_p,
    ]
    api.DeviceIoControl.restype = ctypes.c_int
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.CloseHandle.restype = ctypes.c_int
    return api


def _compressed_size(path: Path) -> int:
    api = _windows_api()
    high = ctypes.c_uint32()
    # 0xffffffff is also a valid low DWORD; only a nonzero last error means failure.
    _windows_ctypes.set_last_error(0)
    low = api.GetCompressedFileSizeW(str(filesystem_path(path.absolute())), ctypes.byref(high))
    error = _windows_ctypes.get_last_error()
    if low == 0xFFFFFFFF and error:
        raise _windows_ctypes.WinError(error)
    return (high.value << 32) | low


def _volume_path(path: Path) -> str:
    api = _windows_api()
    volume = ctypes.create_unicode_buffer(32768)
    if not api.GetVolumePathNameW(str(filesystem_path(path.absolute())), volume, len(volume)):
        raise _windows_ctypes.WinError(_windows_ctypes.get_last_error())
    return volume.value


def _volume_guid(mount_point: str) -> str | None:
    volume = ctypes.create_unicode_buffer(50)  # \\?\Volume{GUID}\ and terminating NUL
    if not _windows_api().GetVolumeNameForVolumeMountPointW(mount_point, volume, len(volume)):
        return None  # Some paths, such as network shares, have no local volume GUID.
    return volume.value


def _volume_cluster_size(volume: str) -> int:
    api = _windows_api()
    sectors, bytes_per_sector, free, total = (ctypes.c_uint32() for _ in range(4))
    if not api.GetDiskFreeSpaceW(
        volume, ctypes.byref(sectors), ctypes.byref(bytes_per_sector),
        ctypes.byref(free), ctypes.byref(total),
    ):
        raise _windows_ctypes.WinError(_windows_ctypes.get_last_error())
    size = sectors.value * bytes_per_sector.value
    if size <= 0:
        raise OSError("Windows reported an invalid allocation unit size")
    return size


def _cluster_size(path: Path) -> int:
    return _volume_cluster_size(_volume_path(path))


def _volume_device(volume: str) -> int:
    return Path(volume).lstat().st_dev


class WindowsAllocationCache:
    """Reuse volume locations for siblings and cluster sizes by volume GUID during one scan."""

    def __init__(self) -> None:
        self._directories: dict[str, str] = {}
        self._clusters: dict[str, tuple[int, int]] = {}

    def cluster_size(self, path: Path, device: int) -> int:
        directory = str(path.parent)
        volume = self._directories.get(directory)
        if volume is not None:
            volume_device, cluster_size = self._clusters[volume]
            if volume_device == device:
                return cluster_size

        # A child directory is resolved independently: a mount point can be
        # on a different volume despite sharing the drive letter and prefix.
        self._directories.pop(directory, None)
        mount_point = _volume_path(path)
        volume = _volume_guid(mount_point)
        if volume is None:
            return _volume_cluster_size(mount_point)
        geometry = self._clusters.get(volume)
        if geometry is None:
            try:
                # The file or an ancestor may have changed since lstat. Check
                # the GUID root before sharing its geometry with other files.
                if _volume_device(volume) == device:
                    geometry = self._clusters[volume] = (device, _volume_cluster_size(volume))
            except OSError:
                pass
        if geometry is None or geometry[0] != device:
            return _volume_cluster_size(mount_point)
        self._directories[directory] = volume
        return geometry[1]


def _has_no_extents(path: Path) -> bool:
    """Resident data has no external extents; do not infer residency from length."""
    api = _windows_api()
    # FILE_READ_ATTRIBUTES, share read/write/delete, OPEN_EXISTING,
    # FILE_FLAG_OPEN_REPARSE_POINT: never open a replacement reparse target.
    handle = api.CreateFileW(str(filesystem_path(path.absolute())), 0x80, 7, None, 3, 0x200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise _windows_ctypes.WinError(_windows_ctypes.get_last_error())
    try:
        attributes = (ctypes.c_uint32 * 2)()  # FILE_ATTRIBUTE_TAG_INFO
        if not api.GetFileInformationByHandleEx(handle, 9, attributes, ctypes.sizeof(attributes)):
            raise _windows_ctypes.WinError(_windows_ctypes.get_last_error())
        if attributes[0] & (0x400 | 0x10 | 0x800 | 0x200):
            raise OSError("File type changed during allocation query")
        starting_vcn = ctypes.c_int64(0)
        output = ctypes.create_string_buffer(32)  # header and one extent
        returned = ctypes.c_uint32()
        success = api.DeviceIoControl(
            handle, 0x90073, ctypes.byref(starting_vcn), ctypes.sizeof(starting_vcn),
            output, ctypes.sizeof(output), ctypes.byref(returned), None,
        )
        if success:
            return False
        error = _windows_ctypes.get_last_error()
        if error == 38:  # ERROR_HANDLE_EOF: no extents starting at VCN 0
            return True
        if error == 234:  # ERROR_MORE_DATA: additional extents, residency excluded
            return False
        raise _windows_ctypes.WinError(error)
    finally:
        api.CloseHandle(handle)


def allocated_size(
    path: Path, metadata: os.stat_result | LazyStat, *,
    phase: Callable[[str], AbstractContextManager[None]] | None = None,
    cache: WindowsAllocationCache | None = None,
) -> int | None:
    """Called only for ordinary files, never for symlinks or reparse points.

    API errors propagate so the caller can record incomplete metadata coverage.
    Ordinary Windows files with extents use cluster-rounded logical length; this is an
    Explorer-style estimate, not a measurement of uniquely reclaimable storage.
    """
    if sys.platform != "win32":
        return metadata.st_blocks * 512 if hasattr(metadata, "st_blocks") else None
    attributes = getattr(metadata, "st_file_attributes", 0)
    if attributes & (0x800 | 0x200):  # FILE_ATTRIBUTE_COMPRESSED | FILE_ATTRIBUTE_SPARSE_FILE
        return _compressed_size(path)
    if metadata.st_size == 0:
        return 0
    with phase("extents") if phase is not None else nullcontext():
        if _has_no_extents(path):
            return 0
    with phase("cluster_size") if phase is not None else nullcontext():
        device = getattr(metadata, "st_dev", 0)
        cluster = cache.cluster_size(path, device) if cache is not None and device else _cluster_size(path)
    return ((metadata.st_size + cluster - 1) // cluster) * cluster
