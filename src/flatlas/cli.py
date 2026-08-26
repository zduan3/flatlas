"""Typer command line interface for the read-only MVP."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Annotated, Any

import typer

from flatlas.config import resolve_database
from flatlas.core import (
    create_dry_run_plan,
    discover_namespace,
    disk_usage,
    duplicate_groups,
    export_rows,
    filesystem_usage,
    get_root,
    largest_files,
    list_child_directories,
    open_database,
    plan_operations,
    query_paths,
    register_namespace,
    scan_directory,
)
from flatlas.errors import FlatlasError

app = typer.Typer(help="Persistent, read-only filesystem indexing and duplicate analysis.", no_args_is_help=True)
db_option = Annotated[Path | None, typer.Option("--db", help="Override the global index database for this command.")]


def _database(explicit: Path | None, *, create: bool = False) -> Path:
    return resolve_database(explicit, create=create)


def _print(rows: object, fmt: str = "json", output: Path | None = None) -> None:
    if not isinstance(rows, list):
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return
    rendered = export_rows(rows, fmt, output)
    if output is None:
        typer.echo(rendered, nl=False)


def _print_usage(
    rows: list[dict[str, object]],
    fmt: str,
    output: Path | None,
    *,
    include_kind: bool = False,
    include_status: bool = False,
    name_column: bool = False,
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
    headers.extend(["SIZE(B)", "N"])
    if show_allocated:
        headers.append("ALLOC(B)")
    value_column = "name" if name_column else "path"
    headers.append(value_column.upper())
    table_rows: list[list[str]] = []
    for row in rows:
        fields = [str(row["entry_kind"])[0]] if include_kind else []
        if include_status:
            fields.append({"scanned": "ok", "unscanned": "new", "incomplete": "part", "missing": "gone"}[str(row["status"])])
        fields.extend(["-" if row[key] is None else str(row[key]) for key in ("logical_size", "files")])
        if show_allocated:
            allocated = row["allocated_size"]
            fields.append("-" if allocated is None else str(allocated))
        fields.append(str(row[value_column]))
        table_rows.append(fields)

    leading_columns = int(include_kind) + int(include_status)
    numeric_columns = set(range(leading_columns, len(headers) - 1))
    rendered = _render_table(headers, table_rows, numeric_columns)
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


def _print_duplicate_groups(
    groups: list[dict[str, Any]],
    fmt: str,
    output: Path | None,
) -> None:
    if fmt in {"json", "csv"}:
        _print(groups, fmt, output)
        return
    if fmt != "table":
        raise FlatlasError("format must be table, json or csv")

    redundant_files = sum(int(group["count"]) - 1 for group in groups)
    theoretical_savings = sum(int(group["theoretical_savings"]) for group in groups)
    lines = [
        (
            f"{len(groups)} duplicate groups · {redundant_files} redundant files · "
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
    rendered = "\n".join(lines) + "\n"
    if output is None:
        typer.echo(rendered, nl=False)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8", newline="")


def _print_filesystems(rows: list[dict[str, object]], fmt: str) -> None:
    if fmt in {"json", "csv"}:
        _print(rows, fmt)
        return
    if fmt != "table":
        raise FlatlasError("format must be table, json or csv")
    table_rows = [
        [
            str(row["filesystem"]),
            "-" if row["blocks_1k"] is None else str(row["blocks_1k"]),
            "-" if row["used_1k"] is None else str(row["used_1k"]),
            "-" if row["available_1k"] is None else str(row["available_1k"]),
            "-" if row["use_percent"] is None else f"{row['use_percent']}%",
            str(row["mounted_on"]),
        ]
        for row in rows
    ]
    typer.echo(
        _render_table(
            ["FILESYSTEM", "1K-BLOCKS", "USED", "AVAILABLE", "USE%", "MOUNTED ON"],
            table_rows,
            {1, 2, 3, 4},
        ),
        nl=False,
    )


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="A path on the filesystem/volume to register.")] = Path("."),
    db: db_option = None,
) -> None:
    """Initialize global configuration and register a filesystem without scanning it."""
    database = _database(db, create=True)
    connection = open_database(database)
    try:
        namespace = discover_namespace(path)
        root_id = register_namespace(connection, namespace)
    finally:
        connection.close()
    typer.echo(f"registered filesystem {namespace.path} as root {root_id}; no files were scanned")


def _show_filesystems(db: Path | None, fmt: str) -> None:
    connection = open_database(_database(db))
    try:
        _print_filesystems(filesystem_usage(connection), fmt)
    finally:
        connection.close()


@app.command("roots")
def roots(db: db_option = None, format: Annotated[str, typer.Option("--format")] = "table") -> None:
    """Show registered filesystems in a GNU df-like table."""
    _show_filesystems(db, format)


@app.command("df")
def df_command(db: db_option = None, format: Annotated[str, typer.Option("--format")] = "table") -> None:
    """Alias for roots; show registered filesystem capacity and usage."""
    _show_filesystems(db, format)


@app.command()
def scan(
    path: Annotated[Path, typer.Argument(help="Directory to scan; it must be on a registered filesystem.")],
    hash_mode: Annotated[str, typer.Option("--hash", help="Hash stage: none, quick, or full.")] = "full",
    db: db_option = None,
) -> None:
    """Scan a complete namespace or one subtree and update the persistent index."""
    connection = open_database(_database(db))
    try:
        result = scan_directory(connection, path, hash_mode=hash_mode)
        _print(result)
    finally:
        connection.close()


@app.command("paths")
def paths_command(
    path: Annotated[Path | None, typer.Argument(help="Optional indexed directory scope.")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1)] = 500,
    format: Annotated[str, typer.Option("--format")] = "json",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db: db_option = None,
) -> None:
    """Browse current indexed paths; it remains available while sources are offline."""
    connection = open_database(_database(db))
    try:
        _print(query_paths(connection, scope=path, limit=limit), format, output)
    finally:
        connection.close()


@app.command("du")
def du(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="Optional indexed file or directory scopes."),
    ] = None,
    format: Annotated[str, typer.Option("--format", help="Output format: table, json, or csv.")] = "table",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db: db_option = None,
) -> None:
    """Summarize indexed usage. SIZE(B)=logical bytes; N=file count; ALLOC(B)=allocated bytes."""
    connection = open_database(_database(db))
    try:
        rows = disk_usage(connection) if not paths else [row for path in paths for row in disk_usage(connection, scope=path)]
        _print_usage(rows, format, output)
    finally:
        connection.close()


@app.command("ls")
def ls_command(
    path: Annotated[Path, typer.Argument(help="Directory whose live direct children are listed.")] = Path("."),
    format: Annotated[str, typer.Option("--format", help="Output format: table, json, or csv.")] = "table",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db: db_option = None,
) -> None:
    """List live child directories. S: ok=scanned, new=unscanned, part=incomplete, gone=missing.

    SIZE(B)=indexed logical bytes; N=indexed file count; ALLOC(B)=indexed allocated bytes.
    """
    connection = open_database(_database(db))
    try:
        _print_usage(
            list_child_directories(connection, scope=path),
            format,
            output,
            include_status=True,
            name_column=True,
        )
    finally:
        connection.close()


@app.command("largest")
def largest(
    limit: Annotated[int, typer.Option("--limit", min=1)] = 50,
    format: Annotated[str, typer.Option("--format")] = "json",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db: db_option = None,
) -> None:
    """List the largest currently indexed files."""
    connection = open_database(_database(db))
    try:
        _print(largest_files(connection, limit=limit), format, output)
    finally:
        connection.close()


@app.command("dupes")
def dupes(
    format: Annotated[str, typer.Option("--format", help="Output format: table, json, or csv.")] = "table",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db: db_option = None,
) -> None:
    """List duplicate groups confirmed with a full BLAKE3 hash."""
    connection = open_database(_database(db))
    try:
        _print_duplicate_groups(duplicate_groups(connection), format, output)
    finally:
        connection.close()


export_app = typer.Typer(help="Export read-only query results.")
app.add_typer(export_app, name="export")


@export_app.command("dupes")
def export_dupes(
    output: Annotated[Path, typer.Option("--output")],
    format: Annotated[str, typer.Option("--format")] = "json",
    db: db_option = None,
) -> None:
    connection = open_database(_database(db))
    try:
        export_rows(duplicate_groups(connection), format, output)
    finally:
        connection.close()
    typer.echo(output)


plan_app = typer.Typer(help="Create and inspect immutable dry-run plans; never applies filesystem changes.")
app.add_typer(plan_app, name="plan")


@plan_app.command("create")
def plan_create(
    root_path: Annotated[Path, typer.Option("--root", help="Registered filesystem whose duplicate groups will be planned.")],
    db: db_option = None,
) -> None:
    """Create an immutable hardlink dry-run plan without changing any file."""
    connection = open_database(_database(db))
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
    plan_id: Annotated[str, typer.Argument()],
    format: Annotated[str, typer.Option("--format")] = "json",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db: db_option = None,
) -> None:
    connection = open_database(_database(db))
    try:
        _print(plan_operations(connection, plan_id), format, output)
    finally:
        connection.close()


def main() -> int:
    """Run the CLI while rendering domain errors without a Python traceback."""
    try:
        app()
    except FlatlasError as exc:
        typer.echo(f"error: {exc}", err=True)
        return 2
    return 0

if __name__ == "__main__":
    main()



