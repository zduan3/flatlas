"""Read-only lookups in the live mount table (Linux ``/proc/self/mounts``)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

PROC_MOUNTS = Path("/proc/self/mounts")


@dataclass(frozen=True)
class MountEntry:
    source: str
    fstype: str


def _unescape(value: str) -> str:
    # /proc mount tables escape space, tab, newline and backslash as \ooo.
    out, index = [], 0
    while index < len(value):
        if value[index] == "\\" and index + 3 < len(value) and value[index + 1:index + 4].isdigit():
            out.append(chr(int(value[index + 1:index + 4], 8)))
            index += 4
        else:
            out.append(value[index])
            index += 1
    return "".join(out)


def mount_entry(mount_point: Path, mounts: Path = PROC_MOUNTS) -> MountEntry | None:
    """Return the entry mounted exactly at ``mount_point``.

    The last matching line wins because later mounts shadow earlier ones.
    Returns ``None`` off Linux or when the table is unreadable or has no entry.
    """
    if mounts == PROC_MOUNTS and not sys.platform.startswith("linux"):
        return None
    try:
        lines = mounts.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    except OSError:
        return None
    target = str(mount_point)
    found = None
    for line in lines:
        fields = line.split()
        if len(fields) >= 3 and _unescape(fields[1]) == target:
            found = MountEntry(_unescape(fields[0]), fields[2])
    return found


def filesystem_type(mount_point: Path, mounts: Path = PROC_MOUNTS) -> str | None:
    entry = mount_entry(mount_point, mounts)
    return None if entry is None else entry.fstype
