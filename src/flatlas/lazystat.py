"""Lazy metadata for filesystems whose exact stat is expensive (Lustre).

On Lustre a plain ``lstat`` of a regular file asks the object storage targets
for the current size (a "glimpse"), which costs milliseconds per entry and
dominates full scans. ``statx`` with ``AT_STATX_DONT_SYNC`` instead returns the
size and times the metadata server already holds (lazy size-on-MDT), which is
exact for files that are not being written and may lag for files that are.

Only the scanner uses this, and only for roots recorded as Lustre. Content
hashing still validates its basis with ``fstat`` on the opened file, so a lagging
lazy value can only make a hash stale, never produce a false duplicate.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat as stat_module
import sys
from dataclasses import dataclass
from pathlib import Path

LUSTRE = "lustre"

_AT_FDCWD = -100
_AT_SYMLINK_NOFOLLOW = 0x100
_AT_STATX_DONT_SYNC = 0x4000
_STATX_BASIC_STATS = 0x7FF


class _Timestamp(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_int64), ("tv_nsec", ctypes.c_uint32), ("_reserved", ctypes.c_int32)]


class _Statx(ctypes.Structure):
    _fields_ = [
        ("stx_mask", ctypes.c_uint32),
        ("stx_blksize", ctypes.c_uint32),
        ("stx_attributes", ctypes.c_uint64),
        ("stx_nlink", ctypes.c_uint32),
        ("stx_uid", ctypes.c_uint32),
        ("stx_gid", ctypes.c_uint32),
        ("stx_mode", ctypes.c_uint16),
        ("_spare0", ctypes.c_uint16),
        ("stx_ino", ctypes.c_uint64),
        ("stx_size", ctypes.c_uint64),
        ("stx_blocks", ctypes.c_uint64),
        ("stx_attributes_mask", ctypes.c_uint64),
        ("stx_atime", _Timestamp),
        ("stx_btime", _Timestamp),
        ("stx_ctime", _Timestamp),
        ("stx_mtime", _Timestamp),
        ("stx_rdev_major", ctypes.c_uint32),
        ("stx_rdev_minor", ctypes.c_uint32),
        ("stx_dev_major", ctypes.c_uint32),
        ("stx_dev_minor", ctypes.c_uint32),
        ("_spare2", ctypes.c_uint64 * 14),
    ]


@dataclass(frozen=True)
class LazyStat:
    """The subset of ``os.stat_result`` the scanner reads.

    ``st_birthtime_ns`` is deliberately absent: Linux ``os.stat`` does not
    report it, and hash validation compares against ``os.fstat``.
    """

    st_mode: int
    st_dev: int
    st_ino: int
    st_nlink: int
    st_uid: int
    st_gid: int
    st_size: int
    st_blocks: int
    st_mtime_ns: int
    st_ctime_ns: int


_statx = None
if sys.platform.startswith("linux"):
    try:
        _statx = ctypes.CDLL(None, use_errno=True).statx
        _statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_Statx)]
        _statx.restype = ctypes.c_int
    except AttributeError:  # glibc older than 2.28
        _statx = None


def available() -> bool:
    return _statx is not None


def _ns(stamp: _Timestamp) -> int:
    return stamp.tv_sec * 1_000_000_000 + stamp.tv_nsec


def lazy_lstat(path: Path) -> os.stat_result | LazyStat:
    """``lstat`` that does not force the filesystem to resynchronise size.

    Falls back to ``Path.lstat`` when ``statx`` is unavailable.
    """
    if _statx is None:
        return path.lstat()
    buffer = _Statx()
    encoded = os.fsencode(path)
    if _statx(_AT_FDCWD, encoded, _AT_SYMLINK_NOFOLLOW | _AT_STATX_DONT_SYNC, _STATX_BASIC_STATS, ctypes.byref(buffer)):
        code = ctypes.get_errno()
        if code == errno.ENOSYS:
            return path.lstat()
        raise OSError(code, os.strerror(code), str(path))
    if stat_module.S_ISREG(buffer.stx_mode) and buffer.stx_size > 0 and buffer.stx_blocks == 0:
        # Without a size-on-MDT record (``lfs getsom`` reports ENODATA) Lustre
        # answers DONT_SYNC with placeholder values: on the measured system such
        # files reported 0 blocks and a wrong size (4096 for git objects).
        # Pay for an exact lstat on these few entries instead of indexing
        # wrong sizes; genuinely sparse or data-on-MDT files cost the same.
        return path.lstat()
    return LazyStat(
        st_mode=buffer.stx_mode,
        st_dev=os.makedev(buffer.stx_dev_major, buffer.stx_dev_minor),
        st_ino=buffer.stx_ino,
        st_nlink=buffer.stx_nlink,
        st_uid=buffer.stx_uid,
        st_gid=buffer.stx_gid,
        st_size=buffer.stx_size,
        st_blocks=buffer.stx_blocks,
        st_mtime_ns=_ns(buffer.stx_mtime),
        st_ctime_ns=_ns(buffer.stx_ctime),
    )
