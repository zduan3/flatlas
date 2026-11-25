"""Typer command line interface for the read-only MVP."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import sqlite3
import sys
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Self

import typer

from flatlas.config import (
    command_default,
    configured_database,
    get_default,
    list_defaults,
    resolve_database,
    set_default,
)
from flatlas.db import (
    MigrationProgress,
    open_database,
    open_database_readonly,
)
from flatlas.duplicate_filters import DuplicateFilters
from flatlas.errors import FlatlasError, PathError
from flatlas.findexpr import is_expression_start, parse_find_expression
from flatlas.hashing import (
    HashProgress,
    HashSummary,
    ensure_duplicate_hashes,
)
from flatlas.output import (
    OBJECT_FORMATS,
    ROW_FORMATS,
    check_format,
    write_object,
    write_records,
)
from flatlas.paths import (
    discover_namespace,
    display_path,
    get_root,
    register_namespace,
    remove_deleted_descendants,
    remove_indexed_subtree,
    remove_indexed_subtrees,
    scope_path,
    shared_path_resolution,
)
from flatlas.planning import (
    create_dry_run_plan,
    list_plans,
    plan_operations,
)
from flatlas.queries import (
    FindHit,
    disk_usage,
    duplicate_groups,
    filesystem_usage,
    find_paths,
    hardlink_usage,
    indexed_scope_ids,
    largest_files,
    list_child_directories,
    list_roots,
    list_scans,
    scan_info,
)
from flatlas.records import (
    ENTRY_FIELDS,
    FILESYSTEM_FIELDS,
    FREED_FIELDS,
    GROUP_FIELDS,
    NEW,
    OK,
    OPERATION_FIELDS,
    PART,
    PLAN_FIELDS,
    ROOT_FIELDS,
    SCAN_FIELDS,
)
from flatlas.refresh import parse_refresh_before
from flatlas.scanning import (
    ScanProgress,
    resume_selection,
    resume_selection_from_scan,
    scan_directory,
)
from flatlas.version import current_version

app = typer.Typer(help="Persistent, read-only filesystem indexing and duplicate analysis.", no_args_is_help=True)
db_option = Annotated[Path | None, typer.Option("--db", help="Override the global index database for this command.")]
min_size_option = Annotated[int, typer.Option("--min-size", min=0, help="Minimum logical file size in bytes (inclusive); applied before hashing.")]
max_size_option = Annotated[int | None, typer.Option("--max-size", min=0, help="Maximum logical file size in bytes (inclusive); applied before hashing.")]
min_count_option = Annotated[int, typer.Option("--min-count", min=2, help="Minimum number of paths in a full-hash group.")]
min_savings_option = Annotated[int, typer.Option("--min-savings", min=0, help="Minimum theoretical logical savings per group in bytes (inclusive).")]
group_limit_option = Annotated[int | None, typer.Option("--limit", "-n", min=1, help="Maximum groups after filtering and sorting by theoretical savings.")]
table_format_option = Annotated[
    str, typer.Option("--format", help="Output format: table, jsonl (one JSON record per line), or csv.")
]
object_format_option = Annotated[str, typer.Option("--format", help="Output format: table or json.")]
output_option = Annotated[Path | None, typer.Option("--output", help="Write output to this file instead of stdout.")]
human_readable_option = Annotated[
    bool, typer.Option("-h", "--human-readable", help="Show table sizes in GNU -h style (1024-based, rounded up).")
]

def _version_callback(value: bool) -> None:
    if value:
        typer.echo(current_version().render())
        raise typer.Exit()


@app.callback()
def app_callback(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show the current version and exit."),
    ] = False,
    no_config: Annotated[
        bool,
        typer.Option(
            "--no-config",
            help="Ignore saved command defaults (like FLATLAS_NO_CONFIG=1); the index location still applies.",
        ),
    ] = False,
) -> None:
    """Configure top-level flatlas options.

    FLATLAS_DB selects the index like --db; FLATLAS_NO_CONFIG=1 ignores saved command defaults.
    """
    ctx.obj = {"no_config": no_config}


def _command_default(ctx: typer.Context, key: str) -> str | None:
    """A saved command default unless --no-config or FLATLAS_NO_CONFIG says to ignore it."""
    if (ctx.find_root().obj or {}).get("no_config"):
        return None
    return command_default(key)


def _database(explicit: Path | None, *, create: bool = False) -> Path:
    return resolve_database(explicit, create=create)


def _parse_refresh_before(value: str, *, current_ns: int | None = None) -> int:
    try:
        return parse_refresh_before(value, current_ns=current_ns)
    except FlatlasError as exc:
        raise typer.BadParameter(str(exc)) from exc


config_app = typer.Typer(
    help="Manage persistent settings: the default index directory and command defaults.", no_args_is_help=True,
)
app.add_typer(config_app, name="config")


def _change_setting(key: str, value: str | None) -> None:
    previous = configured_database() if key == "index.dir" else None
    set_default(key, value)
    if key != "index.dir":
        return
    current = configured_database()
    if previous is None or current is None or previous == current:
        return
    if previous.exists() and not current.exists():
        typer.echo(
            f"note: the default index is now {current}, which does not exist yet; to keep the existing index, "
            f"move {previous} (with any -wal/-shm files) there, otherwise run 'flatlas init'.",
            err=True,
        )


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Save a setting, for example: scan.refresh-before 7d, or index.dir /path/to/dir."""
    _change_setting(key, value)


@config_app.command("get")
def config_get(key: str) -> None:
    """Print a saved default; print nothing when it is unset."""
    value = get_default(key)
    if value is not None:
        typer.echo(value)


@config_app.command("unset")
def config_unset(key: str) -> None:
    """Remove a saved setting and restore the built-in behavior."""
    _change_setting(key, None)


@config_app.command("list")
def config_list(
    all_options: Annotated[
        bool, typer.Option("--all", "-a", help="Include all supported keys, showing <unset> for unset defaults."),
    ] = False,
) -> None:
    """List saved command defaults as key = value."""
    for key, value in list_defaults(include_unset=all_options).items():
        typer.echo(f"{key} = {value if value is not None else '<unset>'}")


class _MigrationProgressDisplay:
    def __init__(self, *, enabled: bool, interval: float = 0.1) -> None:
        self.enabled = enabled
        self.interval = interval
        self.last_update = 0.0
        self.last_percent = -1
        self.width = 0
        self.active = False

    def update(self, progress: MigrationProgress) -> None:
        if progress.phase == "started":
            typer.echo(
                f"database migration {progress.version} required: {progress.description}",
                err=True,
            )
            self.active = True
            return
        if progress.phase == "completed":
            if self.enabled and self.width:
                typer.echo(err=True)
            typer.echo(f"database migration {progress.version} completed", err=True)
            self.active = False
            self.width = 0
            return

        line = f"Migrating database {progress.version}: {progress.completed}/{progress.total} directories"
        if self.enabled:
            current = time.monotonic()
            if progress.completed != progress.total and current - self.last_update < self.interval:
                return
            typer.echo(f"\r\x1b[2K{line}", err=True, nl=False)
            self.width = max(self.width, _display_width(line))
            self.last_update = current
            return

        percent = 100 if progress.total == 0 else progress.completed * 100 // progress.total
        milestone = min(100, percent // 10 * 10)
        if milestone > self.last_percent:
            typer.echo(f"{line} ({percent}%)", err=True)
            self.last_percent = milestone

    def close(self) -> None:
        if self.enabled and self.active and self.width:
            typer.echo(err=True)
        self.active = False
        self.width = 0


def _open_database(location: Path, *, write: bool = True) -> Any:
    progress = _MigrationProgressDisplay(enabled=_stderr_is_tty())
    try:
        return open_database(location, on_migration=progress.update)
    except sqlite3.OperationalError as exc:
        if write or not location.is_file():
            raise FlatlasError(f"cannot open index database for writing: {location}: {exc}") from exc
    finally:
        progress.close()
    # Query commands still work where the index is readable but not writable.
    return open_database_readonly(location)


@contextmanager
def _connection(db: Path | None, *, create: bool = False, write: bool = True) -> Iterator[Any]:
    """Open the index; ``write=False`` marks query commands, which may fall back to read-only."""
    connection = _open_database(_database(db, create=create), write=write)
    try:
        if not write:
            # One read transaction for the whole query: every autocommit
            # statement would take and drop the WAL/database locks again, and
            # on Lustre mounted with flock each lock is a metadata-server round
            # trip (du over 3000 paths: 5.7 s -> 0.5 s). It also gives the
            # command one consistent snapshot. Query commands never write.
            connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def _print_object(value: dict[str, Any], fmt: str = "table", output: Path | None = None) -> None:
    """A single result: ``key: value`` lines, or one JSON object."""
    check_format(fmt, OBJECT_FORMATS)
    if fmt == "json":
        write_object(value, output)
        return

    def scalar(item: object) -> str:
        if isinstance(item, (dict, list)):
            return json.dumps(item, ensure_ascii=False, default=str)
        return "-" if item is None else str(item)

    _emit("".join(f"{key}: {scalar(item)}\n" for key, item in value.items()), output)


def _emit(rendered: str, output: Path | None) -> None:
    """Write rendered table/text output to stdout or to ``output``."""
    if output is None:
        typer.echo(rendered, nl=False)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8", newline="")


def _print_machine(
    rows: Iterable[dict[str, Any]], fmt: str, fields: Sequence[str], output: Path | None = None
) -> bool:
    """Write JSON Lines/CSV records and return True; return False for ``table``."""
    check_format(fmt, ROW_FORMATS)
    if fmt == "table":
        return False
    write_records(rows, fmt, fields, output)
    return True


def _format_size(value: object, *, human_readable: bool = False) -> str:
    """Format a byte count for tables; ``None`` (unknown) is ``-``.

    Human-readable sizes follow GNU coreutils (``ls -lh``, ``du -h``, ``df -h``):
    powers of 1024, always rounded up so a size is never understated, one decimal
    below 10, and plain bytes below 1 KiB.
    """
    if value is None:
        return "-"
    if not human_readable or not isinstance(value, int) or value < 1024:
        return str(value)
    for exponent, unit in enumerate("KMGTPE", start=1):
        scale = 1024**exponent
        tenths = -(-value * 10 // scale)
        if tenths < 100:
            return f"{tenths // 10}.{tenths % 10}{unit}"
        whole = -(-value // scale)
        if whole < 1024 or unit == "E":
            return f"{whole}{unit}"
    raise AssertionError("size unit must be selected")


_DU_SORT_KEYS = {
    "size": "logical_size", "alloc": "allocated_size", "files": "files",
    "entries": "entries", "freed": "freed_allocated_size",
}


def _usage_total(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """GNU ``du -c`` style total: per-path sums; unknown if any row is unknown."""
    def summed(key: str) -> int | None:
        values = [row[key] for row in rows]
        return None if any(value is None for value in values) else sum(values)

    statuses = {row["status"] for row in rows}
    status = OK if statuses == {OK} else NEW if statuses == {NEW} else PART
    return {
        "type": "total", "path": None, "kind": None, "status": status,
        "logical_size": summed("logical_size"), "allocated_size": summed("allocated_size"),
        "files": summed("files"), "entries": summed("entries"),
        "mtime_ns": None, "complete_through_ns": None, "depth": None,
    }


def _relative_display(path: str, base: Path) -> str:
    try:
        return os.path.relpath(path, base)
    except ValueError:
        # Windows cannot express a relative path across drive letters.
        return path


def _group_copies(group: dict[str, Any]) -> str:
    """``3 files`` or, with hardlink aliases, ``3 files (2 inodes)``."""
    files = _counted(int(group["count"]), "file")
    return files if group["count"] == group["inodes"] else f"{files} ({_counted(int(group['inodes']), 'inode')})"


def _print_usage(
    rows: list[dict[str, Any]],
    fmt: str,
    output: Path | None,
    *,
    display: Callable[[dict[str, Any]], str],
    fields: Sequence[str] = ENTRY_FIELDS,
    include_kind: bool = False,
    include_status: bool = False,
    name_column: bool = False,
    logical_header: bool = False,
    include_scanned_through: bool = False,
    include_freed: bool = False,
    include_entries: bool = False,
    human_readable: bool = False,
) -> None:
    if _print_machine(rows, fmt, fields, output):
        return

    show_allocated = any(row["allocated_size"] is not None for row in rows)
    headers = ["T"] if include_kind else []
    if include_status:
        headers.append("S")
    size_header = "LOGICAL" if logical_header else "SIZE"
    headers.extend([size_header if human_readable else f"{size_header}(B)", "N"])
    if include_entries:
        headers.append("E")
    if show_allocated:
        headers.append("ALLOC" if human_readable else "ALLOC(B)")
    # UNIQ/FREED follow ALLOC when allocation is known, else logical size.
    freed_basis = "allocated" if show_allocated else "logical"
    if include_freed:
        headers.extend(["UNIQ", "FREED"] if human_readable else ["UNIQ(B)", "FREED(B)"])
    if include_scanned_through:
        headers.append("SCANNED-THROUGH")
    listing_time_ns = time.time_ns() if include_scanned_through else None
    headers.append("NAME" if name_column else "PATH")
    table_rows: list[list[str]] = []
    for row in rows:
        fields = [str(row["kind"])[0]] if include_kind else []
        if include_status:
            fields.append(str(row["status"]))
        fields.extend([
            _format_size(row["logical_size"], human_readable=human_readable),
            "-" if row["files"] is None or (include_kind and row["kind"] == "file") else str(row["files"]),
        ])
        if include_entries:
            fields.append("-" if row.get("entries") is None else str(row["entries"]))
        if show_allocated:
            fields.append(_format_size(row["allocated_size"], human_readable=human_readable))
        if include_freed:
            fields.extend(
                _format_size(row.get(f"{kind}_{freed_basis}_size"), human_readable=human_readable)
                for kind in ("unique", "freed")
            )
        if include_scanned_through:
            # A subtree property; a file's own coverage is its parent's.
            scanned_through = row["complete_through_ns"] if row["kind"] == "dir" else None
            fields.append(
                _format_ls_timestamp_ns(
                    scanned_through if isinstance(scanned_through, int) else None,
                    now_ns=listing_time_ns,
                )
            )
        fields.append(display(row))
        table_rows.append(fields)

    leading_columns = int(include_kind) + int(include_status)
    numeric_columns = set(range(leading_columns, len(headers) - 1 - int(include_scanned_through)))
    rendered = _render_table(headers, table_rows, numeric_columns)
    _emit(rendered, output)


def _print_largest_files(
    rows: list[dict[str, Any]],
    fmt: str,
    output: Path | None,
    *,
    base: Path,
    human_readable: bool = False,
) -> None:
    if _print_machine(rows, fmt, ENTRY_FIELDS, output):
        return

    headers = ["SIZE" if human_readable else "SIZE(B)", "PATH"]
    table_rows = [
        [_format_size(row["logical_size"], human_readable=human_readable), _relative_display(row["path"], base)]
        for row in rows
    ]

    rendered = _render_table(headers, table_rows, set(range(len(headers) - 1)))
    _emit(rendered, output)


def _display_width(value: str) -> int:
    if value.isascii():
        # Every ASCII character is one column under the rule below; paths
        # nearly always are, and the per-character lookups cost ~5 s on a
        # 30k-row du table.
        return len(value)
    return sum(
        0 if unicodedata.combining(character) else 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
        for character in value
    )


def _truncate_display(value: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if _display_width(value) <= max_width:
        return value

    ellipsis = "…"
    prefix_width = max_width - _display_width(ellipsis)
    if prefix_width < 0:
        return ""

    used_width = 0
    prefix: list[str] = []
    for character in value:
        character_width = _display_width(character)
        if used_width + character_width > prefix_width:
            break
        prefix.append(character)
        used_width += character_width
    return f"{''.join(prefix)}{ellipsis}"


def _terminal_columns() -> int:
    try:
        return os.get_terminal_size(sys.stderr.fileno()).columns
    except (AttributeError, OSError, ValueError):
        return shutil.get_terminal_size(fallback=(80, 24)).columns


def _pad_display(value: str, width: int, *, right: bool) -> str:
    padding = " " * (width - _display_width(value))
    return f"{padding}{value}" if right else f"{value}{padding}"


def _render_table(headers: list[str], rows: list[list[str]], numeric_columns: set[int]) -> str:
    all_rows = [headers, *rows]
    widths = [max(_display_width(row[index]) for row in all_rows) for index in range(len(headers))]
    lines = [
        "  ".join(
            _pad_display(value, widths[index], right=index in numeric_columns)
            for index, value in enumerate(row)
        ).rstrip()
        for row in all_rows
    ]
    return "\n".join(lines) + "\n"


def _format_iec_size(size: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"]
    value = float(size)
    unit = units[0]
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{size} B"
    return f"{value:.2f}".rstrip("0").rstrip(".") + f" {unit}"


def _format_timestamp_ns(value: int | None) -> str:
    if value is None:
        return "-"
    return datetime.fromtimestamp(value / 1_000_000_000).astimezone().isoformat(timespec="seconds")


def _format_ls_timestamp_ns(value: int | None, *, now_ns: int | None = None) -> str:
    if value is None:
        return "-"
    now = time.time_ns() if now_ns is None else now_ns
    local = datetime.fromtimestamp(value / 1_000_000_000).astimezone()
    month_and_day = f"{local.strftime('%b')} {local.day:2}"
    if now - (31_556_952 // 2) * 1_000_000_000 < value < now:
        return f"{month_and_day} {local:%H:%M}"
    return f"{month_and_day}  {local.year}"


def _print_scans(rows: list[dict[str, Any]], fmt: str, *, limit: int | None) -> None:
    if _print_machine(rows, fmt, SCAN_FIELDS):
        return
    rows = rows[:50 if limit is None else limit]
    headers = ["ID", "STATUS", "STARTED", "FINISHED", "FILES", "DIRS", "ERRORS", "SCOPE"]
    values = [
        [
            str(row["scan_id"]),
            str(row["status"]),
            _format_timestamp_ns(row["started_at_ns"]),
            _format_timestamp_ns(row["finished_at_ns"]),
            str(row["files_seen"]),
            str(row["dirs_seen"]),
            str(row["errors_seen"]),
            str(row["scope"] or f"key:{row['scope_path_key_hex']}")
            + (" (shallow)" if row.get("max_depth") == 0 else ""),
        ]
        for row in rows
    ]
    typer.echo(_render_table(headers, values, {0, 4, 5, 6}), nl=False)


def _counted(count: int, noun: str) -> str:
    if count == 1:
        return f"{count} {noun}"
    plural = f"{noun[:-1]}ies" if noun.endswith("y") else f"{noun}s"
    return f"{count} {plural}"


class _ProgressLine:
    """A single self-overwriting stderr status line, throttled per phase.

    Subclasses build the text; this class owns throttling, clearing, truncation
    to the terminal width and the final newline. Use as a context manager.
    """

    def __init__(self, *, enabled: bool, interval: float = 0.1) -> None:
        self.enabled = enabled
        self.interval = interval
        self.last_update = 0.0
        self.now = 0.0
        self.last_phase: str | None = None
        self.width = 0

    def _due(self, phase: str) -> bool:
        if not self.enabled:
            return False
        self.now = time.monotonic()
        return phase != self.last_phase or self.now - self.last_update >= self.interval

    def _show(self, line: str, phase: str) -> None:
        line = _truncate_display(line, max(_terminal_columns() - 1, 0))
        typer.echo(f"\r\x1b[2K{line}", err=True, nl=False)
        self.width = max(self.width, _display_width(line))
        self.last_phase = phase
        self.last_update = self.now

    def close(self) -> None:
        if self.enabled and self.width:
            typer.echo(err=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class _ScanProgressDisplay(_ProgressLine):
    def update(self, progress: ScanProgress) -> None:
        if not self._due(progress.phase):
            return
        phase = {
            "scanning": "Scanning",
            "saving": "Saving observed data",
            "completed": "Completed",
            "partial": "Partial",
            "cancelled": "Cancelled; observed data saved",
        }[progress.phase]
        line = (
            f"{phase}: {_counted(progress.files_seen, 'file')} · "
            f"{progress.dirs_seen} dirs · "
            f"{_format_iec_size(progress.logical_bytes_seen)}"
        )
        if progress.current_directory is not None:
            separator = " · "
            line_width = max(_terminal_columns() - 1, 0)
            directory_width = max(line_width - _display_width(line) - _display_width(separator), 0)
            if directory_width:
                line += separator + _truncate_display(progress.current_directory, directory_width)
        self._show(line, progress.phase)


def _stderr_is_tty() -> bool:
    return sys.stderr.isatty()


class _HashProgressDisplay(_ProgressLine):
    def update(self, progress: HashProgress) -> None:
        if not self._due(progress.phase):
            return
        if progress.phase == "candidates":
            line = f"Candidates: {_counted(progress.total_files, 'file')} · {_format_iec_size(progress.total_bytes)}"
        elif progress.phase in {"quick", "full"}:
            label = "Quick hash" if progress.phase == "quick" else "Full hash"
            line = (
                f"{label}: {progress.completed_files}/{progress.total_files} files · "
                f"{_format_iec_size(progress.completed_bytes)}/{_format_iec_size(progress.total_bytes)}"
            )
        elif progress.phase == "cached":
            line = f"Hashing: cached · {_counted(progress.total_files, 'candidate')}"
        elif progress.phase == "incomplete":
            line = f"Hashing: incomplete · {_counted(progress.total_files, 'candidate')}"
        else:
            line = f"Hashing: complete · {_counted(progress.total_files, 'candidate')}"
        self._show(line, progress.phase)


def _print_duplicate_groups(
    groups: list[dict[str, Any]],
    fmt: str,
    output: Path | None,
    hash_summary: HashSummary,
    *,
    base: Path | None,
) -> None:
    """``base`` relativizes table paths; machine output always has absolute paths."""
    check_format(fmt, ROW_FORMATS)
    if fmt != "table":
        records = [
            {"type": "group", **{key: group[key] for key in GROUP_FIELDS[1:-1]},
             "paths": [path["path_display"] for path in group["paths"]]}
            for group in groups
        ]
        if fmt == "jsonl":
            # The hash summary first: it says whether the groups are complete.
            records = [{"type": "hash", **hash_summary.as_dict()}, *records]
        write_records(records, fmt, GROUP_FIELDS, output)
        return
    if base is not None:
        groups = [
            {**group, "paths": [{**path, "path_display": _relative_display(path["path_display"], base)}
                                for path in group["paths"]]}
            for group in groups
        ]

    redundant_files = sum(int(group["inodes"]) - 1 for group in groups)
    theoretical_savings = sum(int(group["theoretical_savings"]) for group in groups)
    lines = [
        (
            f"{_counted(len(groups), 'duplicate group')} · "
            f"{_counted(redundant_files, 'redundant file')} · "
            f"{_format_iec_size(theoretical_savings)} theoretical savings"
        )
    ]
    for index, group in enumerate(groups, start=1):
        lines.extend(
            [
                "",
                (
                    f"[{index}] {_format_iec_size(int(group['logical_size']))} × {_group_copies(group)} · "
                    f"{_format_iec_size(int(group['theoretical_savings']))} theoretical savings"
                    + (" · has links outside this group" if group["external_links"] else "")
                ),
                *(f"    {path['path_display']}" for path in group["paths"]),
            ]
        )
    if not hash_summary.complete:
        lines.extend(
            [
                "",
                (
                    f"Incomplete: {_counted(hash_summary.changed_files, 'changed file')} · "
                    f"{_counted(hash_summary.errors, 'error')}"
                ),
            ]
        )
    rendered = "\n".join(lines) + "\n"
    _emit(rendered, output)


def _warn_incomplete_hashes(summary: HashSummary) -> None:
    if not summary.complete:
        typer.echo(
            f"warning: duplicate analysis incomplete: {summary.changed_files} changed files, {summary.errors} errors",
            err=True,
        )


def _print_df(rows: list[dict[str, object]], fmt: str, *, human_readable: bool, print_type: bool) -> None:
    if _print_machine(rows, fmt, FILESYSTEM_FIELDS):
        return

    def size(blocks_1k: object) -> str:
        if human_readable and isinstance(blocks_1k, int):
            return _format_size(blocks_1k * 1024, human_readable=True)
        return _format_size(blocks_1k)

    headers = ["Filesystem", *(["Type"] if print_type else []),
               "Size" if human_readable else "1K-blocks", "Used",
               "Avail" if human_readable else "Available", "Use%", "Mounted on"]
    table_rows = [
        [
            str(row["filesystem"]),
            *([str(row["fstype"] or "-")] if print_type else []),
            size(row["blocks_1k"]),
            size(row["used_1k"]),
            size(row["available_1k"]),
            "-" if row["use_percent"] is None else f"{row['use_percent']}%",
            str(row["mounted_on"]),
        ]
        for row in rows
    ]
    first_number = 2 if print_type else 1
    typer.echo(_render_table(headers, table_rows, set(range(first_number, first_number + 4))), nl=False)


def _print_roots(rows: list[dict[str, Any]], fmt: str, *, human_readable: bool) -> None:
    if _print_machine(rows, fmt, ROOT_FIELDS):
        return
    table_rows = [
        [
            str(row["id"]),
            row["filesystem_type"] or "-",
            row["metadata"],
            _format_size(row["files"]),
            _format_size(row["logical_size"], human_readable=human_readable),
            "-" if row["last_scan_id"] is None else str(row["last_scan_id"]),
            row["last_scan_status"] or "-",
            _format_ls_timestamp_ns(row["last_scan_finished_at_ns"] or row["last_scan_started_at_ns"]),
            row["path"],
        ]
        for row in rows
    ]
    headers = ["ID", "TYPE", "METADATA", "FILES", "LOGICAL" if human_readable else "LOGICAL(B)",
               "SCAN", "STATUS", "SCANNED", "ROOT"]
    typer.echo(_render_table(headers, table_rows, {0, 3, 4, 5}), nl=False)


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="A path on the filesystem/volume to register.")] = Path("."),
    format: object_format_option = "table",
    db: db_option = None,
) -> None:
    """Initialize global configuration and register a filesystem without scanning it."""
    check_format(format, OBJECT_FORMATS)
    with _connection(db, create=True) as connection:
        namespace = discover_namespace(path)
        root_id = register_namespace(connection, namespace)
    if format == "json":
        write_object({"root_id": root_id, "path": display_path(namespace.path)})
    else:
        typer.echo(f"registered filesystem {namespace.path} as root {root_id}; no files were scanned")


@app.command("roots")
def roots(
    db: db_option = None,
    format: table_format_option = "table",
    human_readable: human_readable_option = False,
) -> None:
    """Show registered namespaces as recorded in the index, without touching the filesystem."""
    with _connection(db, write=False) as connection:
        rows = list_roots(connection)
    _print_roots(rows, format, human_readable=human_readable)


@app.command("df")
def df_command(
    files: Annotated[
        list[Path] | None,
        typer.Argument(help="Report the filesystem containing each FILE; defaults to all registered namespaces."),
    ] = None,
    db: db_option = None,
    format: table_format_option = "table",
    human_readable: human_readable_option = False,
    print_type: Annotated[bool, typer.Option("-T", "--print-type", help="Print the filesystem type.")] = False,
) -> None:
    """Show live filesystem capacity in the style of GNU df."""
    if files:
        rows = filesystem_usage(files=files)
    else:
        with _connection(db, write=False) as connection:
            rows = filesystem_usage(connection)
    _print_df(rows, format, human_readable=human_readable, print_type=print_type)


@app.command()
def scan(
    ctx: typer.Context,
    path: Annotated[
        Path | None,
        typer.Argument(help="Directory to scan; omit when using --resume-from."),
    ] = None,
    refresh_before: Annotated[
        str | None,
        typer.Option(
            "--refresh-before",
            help="Only refresh directories last completely scanned before this ISO 8601 time or duration (for example 7d); defaults to scan.refresh-before in config.",
        ),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Continue the latest incomplete scan for this exact directory scope."),
    ] = False,
    resume_from: Annotated[
        int | None,
        typer.Option("--resume-from", min=1, help="Continue an incomplete scan ID using its recorded scope."),
    ] = None,
    full: Annotated[
        bool, typer.Option("--full", help="Fully scan the scope, overriding the configured refresh default."),
    ] = False,
    shallow: Annotated[
        bool,
        typer.Option(
            "--shallow",
            help="Re-list only PATH's direct entries and confirm the missing ones; subdirectories are not entered.",
        ),
    ] = False,
    format: object_format_option = "table",
    db: db_option = None,
) -> None:
    """Scan metadata for a complete namespace or one subtree and update the persistent index."""
    if shallow and (full or resume or resume_from is not None or refresh_before is not None):
        raise typer.BadParameter("--shallow is mutually exclusive with --full, --resume, --resume-from, and --refresh-before")
    if sum((resume, resume_from is not None, refresh_before is not None)) > 1:
        raise typer.BadParameter("--resume, --resume-from, and --refresh-before are mutually exclusive")
    if full and (resume or resume_from is not None or refresh_before is not None):
        raise typer.BadParameter("--full is mutually exclusive with --resume, --resume-from, and --refresh-before")
    if resume_from is not None and path is not None:
        raise typer.BadParameter("PATH must be omitted with --resume-from; the scan scope is read from the record")
    if resume_from is None and path is None:
        raise typer.BadParameter("PATH is required unless --resume-from is used")
    check_format(format, OBJECT_FORMATS)
    cutoff = None
    if not (resume or resume_from is not None or full or shallow):
        value = refresh_before if refresh_before is not None else _command_default(ctx, "scan.refresh-before")
        cutoff = _parse_refresh_before(value) if value is not None else None
    with _connection(db) as connection, _ScanProgressDisplay(enabled=_stderr_is_tty()) as progress:
        scan_path = path
        if resume_from is not None:
            selection = resume_selection_from_scan(connection, resume_from)
            scan_path = selection.scope
            cutoff = selection.refresh_before_ns
        elif resume:
            assert path is not None
            cutoff = resume_selection(connection, path).refresh_before_ns
        assert scan_path is not None
        result = scan_directory(
            connection, scan_path, refresh_before_ns=cutoff, shallow=shallow,
            # Without a terminal the line is never drawn; skip the per-entry callback.
            on_progress=progress.update if progress.enabled else None,
        )
    _print_object(result, format)


@app.command("scans")
def scans_command(
    path: Annotated[Path | None, typer.Argument(help="Optional path whose related scan scopes are listed.")] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", min=1, help="Keep the newest N scopes; the table shows 50 by default.")
    ] = None,
    format: table_format_option = "table",
    db: db_option = None,
) -> None:
    """List scan and scope audit records without accessing file contents."""
    check_format(format, ROW_FORMATS)
    with _connection(db, write=False) as connection:
        rows = list_scans(connection, scope=path, limit=limit if limit is not None or format != "table" else 50)
    _print_scans(rows, format, limit=limit)


@app.command("scan-info")
def scan_info_command(
    scan_id: Annotated[int, typer.Argument(min=1, help="Scan ID to inspect.")],
    format: object_format_option = "table",
    db: db_option = None,
) -> None:
    """Show one scan with its stage timings, scopes and recorded errors."""
    check_format(format, OBJECT_FORMATS)
    with _connection(db, write=False) as connection:
        result = scan_info(connection, scan_id)
    _print_object(result, format)


@app.command("rm")
def rm(
    paths: Annotated[
        list[Path] | None, typer.Argument(help="Indexed paths to remove recursively from the index only.")
    ] = None,
    files0_from: Annotated[
        str | None,
        typer.Option(
            "--files0-from", metavar="FILE",
            help="Read NUL-separated PATHs from FILE ('-' for stdin), e.g. the list just deleted on disk.",
        ),
    ] = None,
    deleted: Annotated[
        bool,
        typer.Option("--deleted", help="Purge only confirmed deleted descendants of one present indexed directory."),
    ] = False,
    format: object_format_option = "table",
    db: db_option = None,
) -> None:
    """Remove indexed path subtrees; never checks or changes filesystem entries.

    A plain 'rm PATH' immediately removes its entire indexed subtree. Run
    'flatlas du PATH' first to check its indexed size.

    Several PATHs or --files0-from remove their subtrees in one transaction and
    print the totals. A PATH that is not indexed is reported and skipped, and
    the exit status is then 1.
    """
    check_format(format, OBJECT_FORMATS)
    if deleted:
        if files0_from is not None or paths is None or len(paths) != 1:
            raise FlatlasError("--deleted requires exactly one PATH and cannot be combined with --files0-from")
        with _connection(db) as connection:
            result = remove_deleted_descendants(connection, paths[0])
        _print_object(result, format)
        return
    if files0_from is not None:
        if paths:
            raise FlatlasError("--files0-from cannot be combined with PATH arguments")
        names, failed = _files0_names(files0_from)
        targets = [Path(name) for name in names]
    else:
        if not paths:
            raise FlatlasError("rm needs a PATH or --files0-from")
        targets, failed = list(paths), False
    with _connection(db) as connection:
        if files0_from is None and len(targets) == 1:
            _print_object(remove_indexed_subtree(connection, targets[0]), format)
            return

        def report(_value: Path, exc: FlatlasError) -> None:
            nonlocal failed
            typer.echo(f"error: {exc}", err=True)
            failed = True

        _print_object(remove_indexed_subtrees(connection, targets, on_error=report), format)
    if failed:
        raise typer.Exit(1)

@app.command("du")
def du(
    paths: Annotated[
        list[str] | None,
        typer.Argument(help="Optional file or directory scopes; defaults to the current directory."),
    ] = None,
    dereference_args: Annotated[
        bool,
        typer.Option(
            "--dereference-args", "-D", "-H",
            help="Follow symlinks given as PATH arguments (GNU du -D/-H); off by default.",
        ),
    ] = False,
    freed: Annotated[
        bool,
        typer.Option("--freed", help="Add UNIQ (each hardlinked inode once) and FREED (released if deleted)."),
    ] = False,
    total: Annotated[bool, typer.Option("--total", "-c", help="Add a total row; with --freed, over the union.")] = False,
    max_depth: Annotated[
        int, typer.Option("--max-depth", "-d", min=0, help="Also list subdirectories down to N levels below each PATH.")
    ] = 0,
    sort: Annotated[
        str | None,
        typer.Option("--sort", help="Sort rows descending by size, alloc, files, entries or freed (needs --freed)."),
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit", "-n", min=1, help="Keep the first N rows after sorting.")] = None,
    files0_from: Annotated[
        str | None,
        typer.Option(
            "--files0-from", metavar="FILE",
            help="Read NUL-separated PATHs from FILE ('-' for stdin), e.g. from 'flatlas find ... -print0'.",
        ),
    ] = None,
    human_readable: human_readable_option = False,
    format: table_format_option = "table",
    output: output_option = None,
    db: db_option = None,
) -> None:
    """Summarize indexed usage. S: ok=scanned, new=unscanned, part=incomplete.

    SIZE(B)=logical bytes; N=file count; E=entries of any kind incl. the directory (inodes);
    ALLOC(B)=allocated bytes; hardlinks count per path.
    A PATH that fails is reported on stderr; the others are still printed, then exit 1.
    """
    sort_key = _DU_SORT_KEYS.get(sort) if sort is not None else None
    if sort is not None and (sort_key is None or (sort == "freed" and not freed)):
        raise FlatlasError("--sort must be size, alloc, files, entries, or freed with --freed")
    check_format(format, ROW_FORMATS)
    rows: list[dict[str, Any]] = []
    failed = False
    if files0_from is not None:
        if paths:
            raise FlatlasError("--files0-from cannot be combined with PATH arguments")
        scopes, failed = _files0_names(files0_from)
    else:
        scopes = paths or ["."]
    with _connection(db, write=False) as connection, shared_path_resolution():
        for path in scopes:
            try:
                rows.extend(disk_usage(
                    connection, scope=path, follow_symlink=dereference_args, freed=freed, max_depth=max_depth,
                ))
            except FlatlasError as exc:
                typer.echo(f"error: {exc}", err=True)
                failed = True
        total_row = None
        if total and rows:
            total_row = _usage_total([row for row in rows if row["depth"] == 0])
            if freed:
                ids = indexed_scope_ids(connection, list(scopes), follow_symlink=dereference_args)
                total_row.update(hardlink_usage(connection, ids))
        if sort_key is not None:
            # Descending; unknown values last.
            rows.sort(key=lambda row: (row[sort_key] is None, -(row[sort_key] or 0)))
        if limit is not None:
            rows = rows[:limit]
        if total_row is not None:
            rows.append(total_row)
        if rows or not failed:
            base = Path.cwd()
            _print_usage(
                rows, format, output, include_status=True, include_entries=True,
                human_readable=human_readable, include_freed=freed,
                fields=(*ENTRY_FIELDS, *FREED_FIELDS) if freed else ENTRY_FIELDS,
                display=lambda row: "total" if row["type"] == "total" else _relative_display(row["path"], base),
            )
    if failed:
        raise typer.Exit(1)


def _files0_names(source: str) -> tuple[list[str], bool]:
    """Valid names from ``--files0-from``; like GNU du, bad names are reported with their position and skipped."""
    names = []
    failed = False
    for number, name in enumerate(_read_files0(source), start=1):
        if not name:
            typer.echo(f"error: {source}:{number}: invalid zero-length file name", err=True)
            failed = True
        elif source == "-" and name == "-":
            typer.echo("error: when reading file names from stdin, no file name of '-' allowed", err=True)
            failed = True
        else:
            names.append(name)
    return names, failed


def _read_files0(source: str) -> list[str]:
    """NUL-separated names as GNU ``du --files0-from`` reads them; the final NUL is optional.

    Empty names are kept so the caller can report them by position.
    """
    data = sys.stdin.buffer.read() if source == "-" else Path(source).read_bytes()
    names = data.split(b"\0")
    if names and names[-1] == b"":
        names.pop()
    return [os.fsdecode(name) for name in names]


@app.command(
    "find",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True, "allow_interspersed_args": False},
)
def find_command(
    ctx: typer.Context,
    dereference_args: Annotated[
        bool,
        typer.Option("-H", help="Follow symlinks given as start points (GNU find -H); off by default."),
    ] = False,
    format: Annotated[
        str | None,
        typer.Option("--format", help="Print entry records as table, jsonl or csv instead of GNU-style paths."),
    ] = None,
    # Long form only: click would read a short -h out of expression tokens such as -maxdepth.
    human_readable: Annotated[
        bool, typer.Option("--human-readable", help="Show table sizes in GNU -h style (1024-based, rounded up).")
    ] = False,
    output: output_option = None,
    db: db_option = None,
) -> None:
    """Find indexed entries with a GNU find expression; reads only the index.

    Usage: flatlas find [--db DB] [-H] [--format F] [PATH...] [EXPRESSION]. Options go before PATH.
    Supported: -name -iname -path -ipath -type -size -mtime -maxdepth -mindepth -prune -print -print0
    -true -false ! -not -a -and -o -or ( ), plus -entries [+-]N and -status ok,new,part. -size and
    -entries use a directory's indexed subtree totals. --format prints the entry record of every
    printed path (absolute paths, sizes, entries, status). A start point that fails is reported on
    stderr; the others are still printed, then exit 1.
    """
    if format is not None:
        check_format(format, ROW_FORMATS)
    arguments = list(ctx.args)
    split = next((index for index, token in enumerate(arguments) if is_expression_start(token)), len(arguments))
    starts = arguments[:split] or ["."]
    expression = parse_find_expression(arguments[split:])
    failed = False

    def hits(connection: Any) -> Iterator[FindHit]:
        nonlocal failed
        for start in starts:
            try:
                yield from find_paths(
                    connection, start, expression, follow_symlink=dereference_args, records=format is not None
                )
            except FlatlasError as exc:
                sys.stdout.flush()
                typer.echo(f"error: {exc}", err=True)
                failed = True

    with _connection(db, write=False) as connection, shared_path_resolution():
        if format is None:
            with contextlib.ExitStack() as stack:
                stream = sys.stdout.buffer if output is None else stack.enter_context(_open_output(output))
                for hit in hits(connection):
                    stream.write(os.fsencode(hit.path) + hit.terminator.encode())
                stream.flush()
        elif format == "table":
            found = list(hits(connection))
            printed = {id(hit.record): hit.path for hit in found}
            _print_usage(
                [hit.record for hit in found if hit.record is not None], format, output,
                display=lambda row: printed[id(row)], include_kind=True, include_status=True,
                include_entries=True, human_readable=human_readable,
            )
        else:
            write_records(
                (hit.record for hit in hits(connection) if hit.record is not None), format, ENTRY_FIELDS, output
            )
    if failed:
        raise typer.Exit(1)


def _open_output(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("wb")


@app.command("ls")
def ls_command(
    path: Annotated[Path, typer.Argument(help="Directory whose live direct children are listed.")] = Path("."),
    human_readable: human_readable_option = False,
    format: table_format_option = "table",
    output: output_option = None,
    db: db_option = None,
) -> None:
    """List live direct entries. S: ok=scanned, new=unscanned, part=incomplete, gone=missing.

    LOGICAL(B)=file bytes or indexed directory bytes; N=file count; ALLOC(B)=allocated bytes.
    SCANNED-THROUGH=oldest successful directory scan in an indexed subtree, not a snapshot.
    """
    check_format(format, ROW_FORMATS)
    with _connection(db, write=False) as connection:
        _print_usage(
            list_child_directories(connection, scope=path),
            format,
            output,
            display=lambda row: os.path.basename(row["path"]),
            include_kind=True,
            include_status=True,
            logical_header=True,
            name_column=True,
            include_scanned_through=True,
            human_readable=human_readable,
        )


@app.command("largest")
def largest(
    path: Annotated[Path, typer.Argument(help="Indexed file or directory scope.")] = Path("."),
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", min=1, help="Keep the N largest; the table shows 10 by default.")
    ] = None,
    human_readable: human_readable_option = False,
    format: table_format_option = "table",
    output: output_option = None,
    db: db_option = None,
) -> None:
    """List the largest currently indexed files."""
    check_format(format, ROW_FORMATS)
    if limit is None and format == "table":
        limit = 10
    with _connection(db, write=False) as connection, shared_path_resolution():
        rows = largest_files(connection, scope=path, limit=limit)
        base = scope_path(path)
    _print_largest_files(rows, format, output, base=base, human_readable=human_readable)


@app.command("dupes")
def dupes(
    path: Annotated[Path, typer.Argument(help="Indexed directory whose duplicate groups are listed.")] = Path("."),
    absolute: Annotated[
        bool, typer.Option("--absolute", help="Show absolute paths in the table; machine output always does.")
    ] = False,
    format: table_format_option = "table",
    output: output_option = None,
    db: db_option = None,
    min_size: min_size_option = 0,
    max_size: max_size_option = None,
    min_count: min_count_option = 2,
    min_savings: min_savings_option = 0,
    limit: group_limit_option = None,
) -> None:
    """List full-hash duplicate groups within an indexed directory."""
    filters = DuplicateFilters(min_size, max_size, min_count, min_savings, limit)
    check_format(format, ROW_FORMATS)
    with _connection(db) as connection, _HashProgressDisplay(enabled=_stderr_is_tty()) as hash_progress:
        hash_summary = ensure_duplicate_hashes(connection, path, on_progress=hash_progress.update, filters=filters)
        groups = duplicate_groups(connection, scope=path, filters=filters)
    _warn_incomplete_hashes(hash_summary)
    _print_duplicate_groups(groups, format, output, hash_summary, base=None if absolute else scope_path(path))


plan_app = typer.Typer(help="Create and inspect immutable dry-run plans; never applies filesystem changes.")
app.add_typer(plan_app, name="plan")


@plan_app.command("create")
def plan_create(
    root_path: Annotated[Path, typer.Option("--root", help="Registered filesystem whose duplicate groups will be planned.")],
    db: db_option = None,
) -> None:
    """Create an immutable hardlink dry-run plan without changing any file."""
    with _connection(db) as connection:
        root = get_root(connection, discover_namespace(root_path))
        if root is None:
            raise FlatlasError(f"filesystem is not registered; run 'flatlas init {root_path}' first")
        plan_id = create_dry_run_plan(connection, int(root["id"]))
    typer.echo(plan_id)


@plan_app.command("show")
def plan_show(
    plan_id: Annotated[str | None, typer.Argument(help="Plan ID to inspect; omit to list plan summaries.")] = None,
    format: table_format_option = "table",
    output: output_option = None,
    db: db_option = None,
) -> None:
    """Show one plan's operations, or a summary of every plan."""
    check_format(format, ROW_FORMATS)
    with _connection(db, write=False) as connection:
        rows = plan_operations(connection, plan_id) if plan_id is not None else list_plans(connection)
    fields = OPERATION_FIELDS if plan_id is not None else PLAN_FIELDS
    if _print_machine(rows, format, fields, output):
        return
    columns = [field for field in fields if field != "type"]
    _emit(
        _render_table(
            [column.upper() for column in columns],
            [["-" if row[column] is None else str(row[column]) for column in columns] for row in rows],
            set(),
        ),
        output,
    )


def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def _route_termination_signals() -> None:
    """Treat SIGTERM and SIGHUP like Ctrl-C.

    Batch schedulers send SIGTERM before killing a job, and closing a terminal
    or a dropped remote session sends SIGHUP. Their default action ends the
    process at once, so a running scan rolls back and hashing loses the work
    since its last batch commit. Raising KeyboardInterrupt instead reuses the
    existing cancellation paths that persist observed work. A signal that the
    parent already ignores (for example under ``nohup``) stays ignored.
    """
    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is None or signal.getsignal(signum) is signal.SIG_IGN:
            continue
        signal.signal(signum, _raise_keyboard_interrupt)


def main() -> int:
    """Run the CLI while rendering domain errors without a Python traceback."""
    _route_termination_signals()
    try:
        app()
    except PathError as exc:
        typer.echo(f"error: {exc}", err=True)
        return 1
    except FlatlasError as exc:
        typer.echo(f"error: {exc}", err=True)
        return 2
    return 0

if __name__ == "__main__":
    main()



