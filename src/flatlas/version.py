"""Package and source-control version reporting."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from flatlas import __version__


@dataclass(frozen=True)
class VersionInfo:
    version: str
    commit: str | None = None
    commit_date: str | None = None
    dirty: bool = False

    def render(self) -> str:
        if self.commit is None:
            return f"flatlas {self.version}"

        details = [self.commit]
        if self.commit_date is not None:
            details.append(self.commit_date)
        if self.dirty:
            details.append("dirty")
        return f"flatlas {self.version} ({', '.join(details)})"


def _run_git(*arguments: str) -> str | None:
    project_root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=project_root,
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _detect_version() -> VersionInfo:
    commit = _run_git("rev-parse", "--short=12", "HEAD")
    if not commit:
        return VersionInfo(version=__version__)

    commit_date = _run_git("show", "-s", "--format=%cs", "HEAD")
    status = _run_git("status", "--porcelain")
    return VersionInfo(
        version=__version__,
        commit=commit,
        commit_date=commit_date or None,
        dirty=bool(status),
    )


@lru_cache
def current_version() -> VersionInfo:
    return _detect_version()
