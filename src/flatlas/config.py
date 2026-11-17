"""User-global configuration and database discovery."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from platformdirs import user_config_dir, user_data_dir
from tomlkit import TOMLDocument
from tomlkit.exceptions import ParseError

from flatlas.errors import FlatlasError
from flatlas.refresh import parse_refresh_before

_SUPPORTED_KEYS = ("scan.refresh-before",)


@dataclass(frozen=True)
class UserPaths:
    config_dir: Path
    data_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def default_database(self) -> Path:
        return self.data_dir / "index.sqlite"


def user_paths() -> UserPaths:
    return UserPaths(
        Path(user_config_dir("flatlas", appauthor=False)),
        Path(user_data_dir("flatlas", appauthor=False))
    )


def _read_config(locations: UserPaths) -> TOMLDocument:
    try:
        return tomlkit.parse(locations.config_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return tomlkit.document()
    except (OSError, UnicodeError, ParseError) as exc:
        raise FlatlasError(f"invalid Flatlas configuration: {locations.config_file}") from exc


def _write_config(locations: UserPaths, config: TOMLDocument) -> None:
    temporary: Path | None = None
    try:
        locations.config_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=locations.config_dir, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(tomlkit.dumps(config))
        os.replace(temporary, locations.config_file)
    except OSError as exc:
        raise FlatlasError(f"cannot write Flatlas configuration: {locations.config_file}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _check_key(key: str) -> None:
    if key not in _SUPPORTED_KEYS:
        raise FlatlasError(f"unknown configuration key: {key}; supported key: scan.refresh-before")


def get_default(key: str) -> str | None:
    """Read a command default without creating configuration or a database."""
    _check_key(key)
    config = _read_config(user_paths()).unwrap()
    section = config.get("scan", {})
    if not isinstance(section, dict):
        raise FlatlasError("invalid Flatlas configuration: scan must be a table")
    value = section.get("refresh-before")
    if value is not None:
        if not isinstance(value, str):
            raise FlatlasError("invalid Flatlas configuration: scan.refresh-before must be a string")
        parse_refresh_before(value)
    return value


def list_defaults(*, include_unset: bool = False) -> dict[str, str | None]:
    """List saved command defaults, optionally including supported unset keys."""
    defaults = {key: get_default(key) for key in sorted(_SUPPORTED_KEYS)}
    return {key: value for key, value in defaults.items() if include_unset or value is not None}


def set_default(key: str, value: str | None) -> None:
    """Set or unset a supported default, preserving other settings and comments."""
    _check_key(key)
    if value is not None:
        parse_refresh_before(value)
    locations = user_paths()
    config = _read_config(locations)
    if "scan" not in config:
        if value is None:
            return
        config["scan"] = tomlkit.table()
    section = config["scan"]
    if not isinstance(section, dict):
        raise FlatlasError("invalid Flatlas configuration: scan must be a table")
    if value is None:
        section.pop("refresh-before", None)
    else:
        section["refresh-before"] = value.strip()
    if "format_version" not in config:
        config["format_version"] = 1
    _write_config(locations, config)


def resolve_database(explicit: Path | None = None, *, create: bool = False) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    locations = user_paths()
    config = _read_config(locations)
    if "database" in config:
        database_value = config["database"]
        if not isinstance(database_value, str) or not database_value:
            raise FlatlasError(f"invalid Flatlas configuration: {locations.config_file}")
        return Path(database_value).expanduser()
    if not create:
        raise FlatlasError("Flatlas is not initialized; run 'flatlas init [PATH]' first.")
    database = locations.default_database.resolve()
    try:
        locations.data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FlatlasError(f"cannot create Flatlas data directory: {locations.data_dir}: {exc}") from exc
    if "format_version" not in config:
        config["format_version"] = 1
    config["database"] = str(database)
    _write_config(locations, config)
    return database
