"""Typer command line interface for the read-only MVP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from flatlas.config import resolve_database
from flatlas.core import (
    create_dry_run_plan,
    discover_namespace,
    disk_usage,
    duplicate_groups,
    export_rows,
    get_root,
    largest_files,
    list_directory_usage,
    list_roots,
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
) -> None:
    if fmt in {"json", "csv"}:
        _print(rows, fmt, output)
        return
    if fmt != "table":
        raise FlatlasError("format must be table, json or csv")

    show_allocated = any(row["allocated_size"] is not None for row in rows)
    headers = ["TYPE"] if include_kind else []
    headers.extend(["LOGICAL_BYTES", "FILES"])
    if show_allocated:
        headers.append("ALLOCATED_BYTES")
    headers.append("PATH")
    lines = ["\t".join(headers)]
    for row in rows:
        fields = [str(row["entry_kind"])] if include_kind else []
        fields.extend([str(row["logical_size"]), str(row["files"])])
        if show_allocated:
            allocated = row["allocated_size"]
            fields.append("-" if allocated is None else str(allocated))
        fields.append(str(row["path"]))
        lines.append("\t".join(fields))
    rendered = "\n".join(lines) + "\n"
    if output is None:
        typer.echo(rendered, nl=False)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8", newline="")


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


@app.command("roots")
def roots(db: db_option = None, format: Annotated[str, typer.Option("--format")] = "json") -> None:
    """List globally registered filesystem namespaces."""
    connection = open_database(_database(db))
    try:
        _print(list_roots(connection), format)
    finally:
        connection.close()


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
    path: Annotated[Path | None, typer.Argument(help="Optional indexed directory scope.")] = None,
    format: Annotated[str, typer.Option("--format", help="Output format: table, json, or csv.")] = "table",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db: db_option = None,
) -> None:
    """Summarize indexed file count and sizes in a du-like table."""
    connection = open_database(_database(db))
    try:
        _print_usage(disk_usage(connection, scope=path), format, output)
    finally:
        connection.close()


@app.command("ls")
def ls_command(
    path: Annotated[Path, typer.Argument(help="Indexed directory whose direct children are listed.")] = Path("."),
    format: Annotated[str, typer.Option("--format", help="Output format: table, json, or csv.")] = "table",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db: db_option = None,
) -> None:
    """List indexed child files and directories with recursive size summaries."""
    connection = open_database(_database(db))
    try:
        _print_usage(list_directory_usage(connection, scope=path), format, output, include_kind=True)
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


@app.command("duplicates")
def duplicates(
    format: Annotated[str, typer.Option("--format")] = "json",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    db: db_option = None,
) -> None:
    """List duplicate groups confirmed with a full BLAKE3 hash."""
    connection = open_database(_database(db))
    try:
        _print(duplicate_groups(connection), format, output)
    finally:
        connection.close()


export_app = typer.Typer(help="Export read-only query results.")
app.add_typer(export_app, name="export")


@export_app.command("duplicates")
def export_duplicates(
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



