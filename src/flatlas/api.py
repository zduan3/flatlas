"""Read-only Python access to the index using the CLI's machine records."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from flatlas.config import resolve_database
from flatlas.db import open_database_readonly
from flatlas.errors import FlatlasError
from flatlas.findexpr import parse_find_expression
from flatlas.queries import (
    disk_usage,
    find_paths,
    largest_files,
    list_roots,
    list_scans,
    scan_info,
)


class Index:
    """A read-only query session obtained from :func:`open_index`."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def find(
        self,
        path: str | Path = ".",
        expression: Sequence[str] = (),
        *,
        follow_symlink: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Yield entry records for the find expression's print actions."""
        if isinstance(expression, (str, bytes)):
            raise FlatlasError("find expression must be a sequence of tokens, not a string")
        parsed = parse_find_expression(list(expression))
        target = os.fspath(path)

        def _iterate() -> Iterator[dict[str, Any]]:
            for hit in find_paths(self._connection, target, parsed, follow_symlink=follow_symlink, records=True):
                if hit.record is not None:
                    yield hit.record

        return _iterate()

    def du(
        self,
        path: str | Path = ".",
        *,
        follow_symlink: bool = False,
        freed: bool = False,
        max_depth: int = 0,
    ) -> list[dict[str, Any]]:
        """Summarize one path and its indexed subdirectories up to max_depth."""
        return disk_usage(self._connection, scope=path, follow_symlink=follow_symlink, freed=freed, max_depth=max_depth)

    def largest(self, path: str | Path = ".", *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return the largest present files under one indexed path."""
        return largest_files(self._connection, scope=Path(path), limit=limit)

    def roots(self) -> list[dict[str, Any]]:
        """Return the registered namespaces."""
        return list_roots(self._connection)

    def scans(self, path: str | Path | None = None, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return newest scan scopes first, optionally filtered by path."""
        return list_scans(self._connection, scope=None if path is None else Path(path), limit=limit)

    def scan_info(self, scan_id: int) -> dict[str, Any]:
        """Return one scan's details, including its scopes and errors."""
        return scan_info(self._connection, scan_id)


@contextmanager
def open_index(db: str | Path | None = None) -> Iterator[Index]:
    """Reuse one read-only connection to an existing, current-schema index.

    With no explicit ``db``, use the CLI's default database resolution. Consume
    any find iterators before leaving the context.
    """
    location = resolve_database(None if db is None else Path(db))
    with closing(open_database_readonly(location)) as connection:
        connection.execute("BEGIN")
        yield Index(connection)
