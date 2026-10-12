"""Read-only platform allocation metadata (Windows uses Explorer's size-on-disk basis)."""

from __future__ import annotations

import ctypes
import os
import sys
from functools import cache
from pathlib import Path
from typing import Any


@cache
def _windows_api() -> Any:
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    dword_pointer = ctypes.POINTER(ctypes.c_uint32)
    api.GetCompressedFileSizeW.argtypes = [ctypes.c_wchar_p, dword_pointer]
    api.GetCompressedFileSizeW.restype = ctypes.c_uint32
    api.GetVolumePathNameW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    api.GetVolumePathNameW.restype = ctypes.c_int
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
    ctypes.set_last_error(0)
    low = api.GetCompressedFileSizeW(str(path.absolute()), ctypes.byref(high))
    error = ctypes.get_last_error()
    if low == 0xFFFFFFFF and error:
        raise ctypes.WinError(error)
    return (high.value << 32) | low


def _cluster_size(path: Path) -> int:
    api = _windows_api()
    volume = ctypes.create_unicode_buffer(32768)
    if not api.GetVolumePathNameW(str(path.absolute()), volume, len(volume)):
        raise ctypes.WinError(ctypes.get_last_error())
    sectors, bytes_per_sector, free, total = (ctypes.c_uint32() for _ in range(4))
    if not api.GetDiskFreeSpaceW(
        volume.value, ctypes.byref(sectors), ctypes.byref(bytes_per_sector),
        ctypes.byref(free), ctypes.byref(total),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    size = sectors.value * bytes_per_sector.value
    if size <= 0:
        raise OSError("Windows reported an invalid allocation unit size")
    return size


def _has_no_extents(path: Path) -> bool:
    """Resident data has no external extents; do not infer residency from length."""
    api = _windows_api()
    # FILE_READ_ATTRIBUTES, share read/write/delete, OPEN_EXISTING,
    # FILE_FLAG_OPEN_REPARSE_POINT: never open a replacement reparse target.
    handle = api.CreateFileW(str(path.absolute()), 0x80, 7, None, 3, 0x200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = (ctypes.c_uint32 * 2)()  # FILE_ATTRIBUTE_TAG_INFO
        if not api.GetFileInformationByHandleEx(handle, 9, attributes, ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
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
        error = ctypes.get_last_error()
        if error == 38:  # ERROR_HANDLE_EOF: no extents starting at VCN 0
            return True
        if error == 234:  # ERROR_MORE_DATA: additional extents, residency excluded
            return False
        raise ctypes.WinError(error)
    finally:
        api.CloseHandle(handle)


def allocated_size(path: Path, metadata: os.stat_result) -> int | None:
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
    if metadata.st_size == 0 or _has_no_extents(path):
        return 0
    cluster = _cluster_size(path)
    return ((metadata.st_size + cluster - 1) // cluster) * cluster
