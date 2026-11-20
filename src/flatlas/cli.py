"""Typer command line interface for the read-only MVP."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from flatlas.config import get_default, list_defaults, resolve_database, set_default
from flatlas.core import (
    HashProgress,
    HashSummary,
    MigrationProgress,
    ScanProgress,
    create_dry_run_plan,
    discover_namespace,
    disk_usage,
    duplicate_groups,
    ensure_duplicate_hashes,
    export_rows,
    filesystem_usage,
    get_root,
    largest_files,
    list_child_directories,
    list_plans,
    list_roots,
    list_scans,
    open_database,
    plan_operations,
    query_paths,
    register_namespace,
    remove_indexed_subtree,
    resume_selection,
    resume_selection_from_scan,
    scan_directory,
    scan_info,
)
from flatlas.duplicate_filters import DuplicateFilters
from flatlas.errors import FlatlasError
from flatlas.refresh import parse_refresh_before
from flatlas.version import current_version

app = typer.Typer(help="Persistent, read-only filesystem indexing and duplicate analysis.", no_args_is_help=True)
db_option = Annotated[Path | None, typer.Option("--db", help="Override the global index database for this command.")]
min_size_option = Annotated[int, typer.Option("--min-size", min=0, help="Minimum logical file size in bytes (inclusive); applied before hashing.")]
max_size_option = Annotated[int | None, typer.Option("--max-size", min=0, help="Maximum logical file size in bytes (inclusive); applied before hashing.")]
min_count_option = Annotated[int, typer.Option("--min-count", min=2, help="Minimum number of paths in a full-hash group.")]
min_savings_option = Annotated[int, typer.Option("--min-savings", min=0, help="Minimum theoretical logical savings per group in bytes (inclusive).")]
group_limit_option = Annotated[int | None, typer.Option("--limit", "-n", min=1, help="Maximum groups after filtering and sorting by theoretical savings.")]

def _version_callback(value: bool) -> None:
    if value:
        typer.echo(current_version().render())
        raise typer.Exit()


@app.callback()
def app_callback(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show the current version and exit."),
    ] = False,
) -> None:
    """Configure top-level flatlas options."""


def _database(explicit: Path | None, *, create: bool = False) -> Path:
    return resolve_database(explicit, create=create)


def _parse_refresh_before(value: str, *, current_ns: int | None = None) -> int:
    try:
        return parse_refresh_before(value, current_ns=current_ns)
    except FlatlasError as exc:
        raise typer.BadParameter(str(exc)) from exc


config_app = typer.Typer(help="Manage persistent defaults for commands.", no_args_is_help=True)
app.add_typer(config_app, name="config")


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Save a default, for example: scan.refresh-before 7d."""
    set_default(key, value)


@config_app.command("get")
def config_get(key: str) -> None:
    """Print a saved default; print nothing when it is unset."""
    value = get_default(key)
    if value is not None:
        typer.echo(value)


@config_app.command("unset")
def config_unset(key: str) -> None:
    """Remove a saved default and restore the built-in behavior."""
    set_default(key, None)


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


def _open_database(location: Path) -> Any:
    progress = _MigrationProgressDisplay(enabled=sys.stderr.isatty())
    try:
        return open_database(location, on_migration=progress.update)
    finally:
        progress.close()


def _print(rows: object, fmt: str = "json", output: Path | None = None) -> None:
    if not isinstance(rows, list):
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return
    rendered = export_rows(rows, fmt, output)
    if output is None:
        typer.echo(rendered, nl=False)


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


def _print_usage(
    rows: list[dict[str, object]],
    fmt: str,
    output: Path | None,
    *,
    include_kind: bool = False,
    include_status: bool = False,
    name_column: bool = False,
    logical_header: bool = False,
    include_scanned_through: bool = False,
    human_readable: bool = False,
) -> None:
    if fmt in {"json", "csv"}:
        _print(rows, fmt, output)
        return
    if fmt != "table":
        raise FlatlasError("format must be table, json or csv")

    show_allocated = any(row["allocated_size"] is not None for row in rows)
    headers = ["T"] if include_kind else []
    if include_status:
        headers.append("S")
    size_header = "LOGICAL" if logical_header else "SIZE"
    headers.extend([size_header if human_readable else f"{size_header}(B)", "N"])
    if show_allocated:
        headers.append("ALLOC" if human_readable else "ALLOC(B)")
    if include_scanned_through:
        headers.append("SCANNED-THROUGH")
    listing_time_ns = time.time_ns() if include_scanned_through else None
    value_column = "name" if name_column else "path"
    headers.append(value_column.upper())
    table_rows: list[list[str]] = []
    for row in rows:
        fields = [str(row["entry_kind"])[0]] if include_kind else []
        if include_status:
            fields.append({"scanned": "ok", "unscanned": "new", "incomplete": "part", "missing": "gone"}[str(row["status"])])
        fields.extend([
            _format_size(row["logical_size"], human_readable=human_readable),
            "-" if row["files"] is None or (include_kind and row["entry_kind"] == "file") else str(row["files"]),
        ])
        if show_allocated:
            fields.append(_format_size(row["allocated_size"], human_readable=human_readable))
        if include_scanned_through:
            scanned_through = row["subtree_complete_through_ns"]
            fields.append(
                _format_ls_timestamp_ns(
                    scanned_through if isinstance(scanned_through, int) else None,
                    now_ns=listing_time_ns,
                )
            )
        fields.append(str(row[value_column]))
        table_rows.append(fields)

    leading_columns = int(include_kind) + int(include_status)
    numeric_columns = set(range(leading_columns, len(headers) - 1 - int(include_scanned_through)))
    rendered = _render_table(headers, table_rows, numeric_columns)
    if output is None:
        typer.echo(rendered, nl=False)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8", newline="")


def _print_largest_files(
    rows: list[dict[str, object]],
    fmt: str,
    output: Path | None,
    *,
    human_readable: bool = False,
) -> None:
    if fmt in {"json", "csv"}:
        _print(rows, fmt, output)
        return
    if fmt != "table":
        raise FlatlasError("format must be table, json or csv")

    headers = ["SIZE" if human_readable else "SIZE(B)", "PATH"]
    table_rows = [
        [_format_size(row["logical_size"], human_readable=human_readable), str(row["path"])]
        for row in rows
    ]

    rendered = _render_table(headers, table_rows, set(range(len(headers) - 1)))
    if output is None:
        typer.echo(rendered, nl=False)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8", newline="")


def _display_width(value: str) -> int:
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


def _print_scans(rows: list[dict[str, Any]], fmt: str) -> None:
    if fmt != "table":
        _print(rows, fmt)
        return
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
            str(row["scope"] or f"key:{row['scope_path_key_hex']}"),
        ]
        for row in rows
    ]
    typer.echo(_render_table(headers, values, {0, 4, 5, 6}), nl=False)


def _counted(count: int, noun: str) -> str:
    if count == 1:
        return f"{count} {noun}"
    plural = f"{noun[:-1]}ies" if noun.endswith("y") else f"{noun}s"
    return f"{count} {plural}"


class _ScanProgressDisplay:
    def __init__(self, *, enabled: bool, interval: float = 0.1) -> None:
        self.enabled = enabled
        self.interval = interval
        self.last_update = 0.0
        self.last_phase: str | None = None
        self.width = 0

    def update(self, progress: ScanProgress) -> None:
        if not self.enabled:
            return
        current = time.monotonic()
        if progress.phase == self.last_phase and current - self.last_update < self.interval:
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
            terminal_width = _terminal_columns()
            line_width = max(terminal_width - 1, 0)
            directory_width = max(line_width - _display_width(line) - _display_width(separator), 0)
            if directory_width:
                line += separator + _truncate_display(progress.current_directory, directory_width)
            line = _truncate_display(line, line_width)
        typer.echo(f"\r\x1b[2K{line}", err=True, nl=False)
        self.width = max(self.width, _display_width(line))
        self.last_phase = progress.phase
        self.last_update = current

    def close(self) -> None:
        if self.enabled and self.width:
            typer.echo(err=True)


def _stderr_is_tty() -> bool:
    return sys.stderr.isatty()


class _HashProgressDisplay:
    def __init__(self, *, enabled: bool, interval: float = 0.1) -> None:
        self.enabled = enabled
        self.interval = interval
        self.last_update = 0.0
        self.last_phase: str | None = None
        self.width = 0

    def update(self, progress: HashProgress) -> None:
        if not self.enabled:
            return
        current = time.monotonic()
        if progress.phase == self.last_phase and current - self.last_update < self.interval:
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
        typer.echo(f"\r{line.ljust(self.width)}", err=True, nl=False)
        self.width = max(self.width, len(line))
        self.last_phase = progress.phase
        self.last_update = current

    def close(self) -> None:
        if self.enabled and self.width:
            typer.echo(err=True)


def _duplicate_display_paths(
    groups: list[dict[str, Any]],
    scope: Path,
    *,
    absolute: bool,
) -> list[dict[str, Any]]:
    if absolute:
        return groups
    base = scope.expanduser().resolve()
    return [
        {
            **group,
            "paths": [
                {
                    **path,
                    "path_display": os.path.relpath(path["path_display"], base),
                }
                for path in group["paths"]
            ],
        }
        for group in groups
    ]


def _print_duplicate_groups(
    groups: list[dict[str, Any]],
    fmt: str,
    output: Path | None,
    hash_summary: HashSummary,
) -> None:
    if fmt == "json":
        rendered = json.dumps(
            {"hash": hash_summary.as_dict(), "groups": groups},
            ensure_ascii=False,
            indent=2,
            default=str,
        ) + "\n"
        if output is None:
            typer.echo(rendered, nl=False)
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8", newline="")
        return
    if fmt == "csv":
        rows = [{**hash_summary.as_dict(), **group} for group in groups]
        _print(rows, fmt, output)
        return
    if fmt != "table":
        raise FlatlasError("format must be table, json or csv")

    redundant_files = sum(int(group["count"]) - 1 for group in groups)
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
                    f"[{index}] {_format_iec_size(int(group['logical_size']))} × {group['count']} files · "
                    f"{_format_iec_size(int(group['theoretical_savings']))} theoretical savings"
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
    if output is None:
        typer.echo(rendered, nl=False)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8", newline="")


def _warn_incomplete_hashes(summary: HashSummary) -> None:
    if not summary.complete:
        typer.echo(
            f"warning: duplicate analysis incomplete: {summary.changed_files} changed files, {summary.errors} errors",
            err=True,
        )


def _print_df(rows: list[dict[str, object]], fmt: str, *, human_readable: bool, print_type: bool) -> None:
    if fmt in {"json", "csv"}:
        _print(rows, fmt)
        return
    if fmt != "table":
        raise FlatlasError("format must be table, json or csv")

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
            *([str(row["type"] or "-")] if print_type else []),
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
    if fmt in {"json", "csv"}:
        _print(rows, fmt)
        return
    if fmt != "table":
        raise FlatlasError("format must be table, json or csv")
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
            row["root_path_display"],
        ]
        for row in rows
    ]
    headers = ["ID", "TYPE", "METADATA", "FILES", "LOGICAL" if human_readable else "LOGICAL(B)",
               "SCAN", "STATUS", "SCANNED", "ROOT"]
    typer.echo(_render_table(headers, table_rows, {0, 3, 4, 5}), nl=False)


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="A path on the filesystem/volume to register.")] = Path("."),
    db: db_option = None,
) -> None:
    """Initialize global configuration and register a filesystem without scanning it."""
    database = _database(db, create=True)
    connection = _open_database(database)
    try:
        namespace = discover_namespace(path)
        root_id = register_namespace(connection, namespace)
    finally:
        connection.close()
    typer.echo(f"registered filesystem {namespace.path} as root {root_id}; no files were scanned")


@app.command("roots")
def roots(
    db: db_option = None,
    format: Annotated[str, typer.Option("--format")] = "table",
    human_readable: Annotated[
        bool, typer.Option("-h", "--human-readable", help="Show indexed sizes in 1024-based K, M, G, T units.")
    ] = False,
) -> None:
    """Show registered namespaces as recorded in the index, without touching the filesystem."""
    connection = _open_database(_database(db))
    try:
        rows = list_roots(connection)
    finally:
        connection.close()
    _print_roots(rows, format, human_readable=human_readable)


@app.command("df")
def df_command(
    files: Annotated[
        list[Path] | None,
        typer.Argument(help="Report the filesystem containing each FILE; defaults to all registered namespaces."),
    ] = None,
    db: db_option = None,
    format: Annotated[str, typer.Option("--format")] = "table",
    human_readable: Annotated[
        bool, typer.Option("-h", "--human-readable", help="Print sizes in powers of 1024 (GNU df -h style).")
    ] = False,
    print_type: Annotated[bool, typer.Option("-T", "--print-type", help="Print the filesystem type.")] = False,
) -> None:
    """Show live filesystem capacity in the style of GNU df."""
    if files:
        rows = filesystem_usage(files=files)
    else:
        connection = _open_database(_database(db))
        try:
            rows = filesystem_usage(connection)
        finally:
            connection.close()
    _print_df(rows, format, human_readable=human_readable, print_type=print_type)


@app.command()
def scan(
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
    db: db_option = None,
) -> None:
    """Scan metadata for a complete namespace or one subtree and update the persistent index."""
    if sum((resume, resume_from is not None, refresh_before is not None)) > 1:
        raise typer.BadParameter("--resume, --resume-from, and --refresh-before are mutually exclusive")
    if full and (resume or resume_from is not None or refresh_before is not None):
        raise typer.BadParameter("--full is mutually exclusive with --resume, --resume-from, and --refresh-before")
    if resume_from is not None and path is not None:
        raise typer.BadParameter("PATH must be omitted with --resume-from; the scan scope is read from the record")
    if resume_from is None and path is None:
        raise typer.BadParameter("PATH is required unless --resume-from is used")
    cutoff = None
    if not (resume or resume_from is not None or full):
        value = refresh_before if refresh_before is not None else get_default("scan.refresh-before")
        cutoff = _parse_refresh_before(value) if value is not None else None
    connection = _open_database(_database(db))
    progress = _ScanProgressDisplay(enabled=_stderr_is_tty())
    try:
        scan_path = path
        if resume_from is not None:
            selection = resume_selection_from_scan(connection, resume_from)
            scan_path = selection.scope
            cutoff = selection.refresh_before_ns
        elif resume:
            assert path is not None
            cutoff = resume_selection(connection, path).refresh_before_ns
        assert scan_path is not None
        result = scan_directory(connection, scan_path, refresh_before_ns=cutoff, on_progress=progress.update)
    finally:
        progress.close()
        connection.close()
    _print(result)


@app.command("scans")
def scans_command(
    path: Annotated[Path | None, typer.Argument(help="Optional path whose related scan scopes are listed.")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1)] = 50,
    format: Annotated[str, typer.Option("--format", help="Output format: table, json, or csv.")] = "table",
    db: db_option = None,
) -> None:
    """List scan and scope audit records without accessing file contents."""
    if format not in {"table", "json", "csv"}:
        raise FlatlasError("format must be table, json or csv")
    connection = _open_database(_database(db))
    try:
        rows = list_scans(connection, scope=path, limit=limit)
    finally:
        connection.close()
    _print_scans(rows, format)


@app.command("scan-info")
def scan_info_command(
    scan_id: Annotated[int, typer.Argument(min=1, help="Scan ID to inspect.")],
    db: db_option = None,
) -> None:
    """Show one scan with its scopes and recorded errors as JSON."""
    connection = _open_database(_database(db))
    try:
        result = scan_info(connection, scan_id)
    finally:
        connection.close()
    _print(result)


@app.command("paths")
def paths_command(
    path: Annotated[Path | None, typer.Argument(help="Optional indexed directory scope.")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1)] = 500,
    format: Annotated[str, typer.Option("--format")] = "json",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db: db_option = None,
) -> None:
    """Browse current indexed paths; it remains available while sources are offline."""
    connection = _open_database(_database(db))
    try:
        _print(query_paths(connection, scope=path, limit=limit), format, output)
    finally:
        connection.close()


@app.command("rm")
def rm(
    path: Annotated[Path, typer.Argument(help="Indexed path to remove recursively from the index only.")],
    db: db_option = None,
) -> None:
    """Remove an indexed path subtree; never checks or changes filesystem entries."""
    connection = _open_database(_database(db))
    try:
        _print(remove_indexed_subtree(connection, path))
    finally:
        connection.close()

@app.command("du")
def du(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="Optional file or directory scopes; defaults to the current directory."),
    ] = None,
    human_readable: Annotated[
        bool, typer.Option("-h", "--human-readable", help="Show table sizes in 1024-based K, M, G, T units.")
    ] = False,
    format: Annotated[str, typer.Option("--format", help="Output format: table, json, or csv.")] = "table",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db: db_option = None,
) -> None:
    """Summarize indexed usage. S: ok=scanned, new=unscanned, part=incomplete.

    SIZE(B)=logical bytes; N=file count; ALLOC(B)=allocated bytes.
    """
    connection = _open_database(_database(db))
    try:
        rows = [row for path in (paths or [Path(".")]) for row in disk_usage(connection, scope=path)]
        _print_usage(rows, format, output, include_status=True, human_readable=human_readable)
    finally:
        connection.close()


@app.command("ls")
def ls_command(
    path: Annotated[Path, typer.Argument(help="Directory whose live direct children are listed.")] = Path("."),
    human_readable: Annotated[
        bool, typer.Option("-h", "--human-readable", help="Show table sizes in 1024-based K, M, G, T units.")
    ] = False,
    format: Annotated[str, typer.Option("--format", help="Output format: table, json, or csv.")] = "table",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db: db_option = None,
) -> None:
    """List live direct entries. S: ok=scanned, new=unscanned, part=incomplete, gone=missing.

    LOGICAL(B)=file bytes or indexed directory bytes; N=file count; ALLOC(B)=allocated bytes.
    SCANNED-THROUGH=oldest successful directory scan in an indexed subtree, not a snapshot.
    """
    connection = _open_database(_database(db))
    try:
        _print_usage(
            list_child_directories(connection, scope=path),
            format,
            output,
            include_kind=True,
            include_status=True,
            logical_header=True,
            name_column=True,
            include_scanned_through=True,
            human_readable=human_readable,
        )
    finally:
        connection.close()


@app.command("largest")
def largest(
    path: Annotated[Path, typer.Argument(help="Indexed file or directory scope.")] = Path("."),
    limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 10,
    human_readable: Annotated[
        bool, typer.Option("-h", "--human-readable", help="Show table sizes in 1024-based K, M, G, T units.")
    ] = False,
    format: Annotated[str, typer.Option("--format", help="Output format: table, json, or csv.")] = "table",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db: db_option = None,
) -> None:
    """List the largest currently indexed files."""
    connection = _open_database(_database(db))
    try:
        _print_largest_files(
            largest_files(connection, scope=path, limit=limit), format, output, human_readable=human_readable
        )
    finally:
        connection.close()


@app.command("dupes")
def dupes(
    path: Annotated[Path, typer.Argument(help="Indexed directory whose duplicate groups are listed.")] = Path("."),
    absolute: Annotated[bool, typer.Option("--absolute", help="Show absolute paths instead of paths relative to PATH.")] = False,
    format: Annotated[str, typer.Option("--format", help="Output format: table, json, or csv.")] = "table",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db: db_option = None,
    min_size: min_size_option = 0,
    max_size: max_size_option = None,
    min_count: min_count_option = 2,
    min_savings: min_savings_option = 0,
    limit: group_limit_option = None,
) -> None:
    """List full-hash duplicate groups within an indexed directory."""
    filters = DuplicateFilters(min_size, max_size, min_count, min_savings, limit)
    connection = _open_database(_database(db))
    hash_progress = _HashProgressDisplay(enabled=_stderr_is_tty())
    try:
        hash_summary = ensure_duplicate_hashes(connection, path, on_progress=hash_progress.update, filters=filters)
        groups = _duplicate_display_paths(duplicate_groups(connection, scope=path, filters=filters), path, absolute=absolute)
    finally:
        hash_progress.close()
        connection.close()
    _warn_incomplete_hashes(hash_summary)
    _print_duplicate_groups(groups, format, output, hash_summary)


export_app = typer.Typer(help="Export read-only query results.")
app.add_typer(export_app, name="export")


@export_app.command("dupes")
def export_dupes(
    output: Annotated[Path, typer.Option("--output")],
    path: Annotated[Path, typer.Argument(help="Indexed directory whose duplicate groups are exported.")] = Path("."),
    absolute: Annotated[bool, typer.Option("--absolute", help="Show absolute paths instead of paths relative to PATH.")] = False,
    format: Annotated[str, typer.Option("--format")] = "json",
    db: db_option = None,
    min_size: min_size_option = 0,
    max_size: max_size_option = None,
    min_count: min_count_option = 2,
    min_savings: min_savings_option = 0,
    limit: group_limit_option = None,
) -> None:
    dupes(
        path=path, absolute=absolute, format=format, output=output, db=db,
        min_size=min_size, max_size=max_size, min_count=min_count,
        min_savings=min_savings, limit=limit,
    )
    typer.echo(output)


plan_app = typer.Typer(help="Create and inspect immutable dry-run plans; never applies filesystem changes.")
app.add_typer(plan_app, name="plan")


@plan_app.command("create")
def plan_create(
    root_path: Annotated[Path, typer.Option("--root", help="Registered filesystem whose duplicate groups will be planned.")],
    db: db_option = None,
) -> None:
    """Create an immutable hardlink dry-run plan without changing any file."""
    connection = _open_database(_database(db))
    try:
        root = get_root(connection, discover_namespace(root_path))
        if root is None:
            raise FlatlasError(f"filesystem is not registered; run 'flatlas init {root_path}' first")
        plan_id = create_dry_run_plan(connection, int(root["id"]))
    finally:
        connection.close()
    typer.echo(plan_id)


@plan_app.command("show")
def plan_show(
    plan_id: Annotated[str | None, typer.Argument(help="Plan ID to inspect; omit to list plan summaries.")] = None,
    format: Annotated[str, typer.Option("--format")] = "json",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db: db_option = None,
) -> None:
    connection = _open_database(_database(db))
    try:
        rows = plan_operations(connection, plan_id) if plan_id is not None else list_plans(connection)
        _print(rows, format, output)
    finally:
        connection.close()


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
    except FlatlasError as exc:
        typer.echo(f"error: {exc}", err=True)
        return 2
    return 0

if __name__ == "__main__":
    main()



