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

DATABASE_NAME = "index.sqlite"
# Scripts and sandboxes select an index or ignore personal defaults without
# writing the user configuration.
DATABASE_ENV = "FLATLAS_DB"
NO_CONFIG_ENV = "FLATLAS_NO_CONFIG"
_FALSE_WORDS = {"", "0", "false", "no", "off"}


@dataclass(frozen=True)
class UserPaths:
    config_dir: Path
    data_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def default_database(self) -> Path:
        return self.data_dir / DATABASE_NAME


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


def _validate_index_dir(value: str) -> None:
    if not value.strip():
        raise FlatlasError("index.dir must not be empty")
    if not Path(value.strip()).expanduser().is_absolute():
        raise FlatlasError(f"index.dir must be an absolute path: {value}")


def _validate_refresh_before(value: str) -> None:
    parse_refresh_before(value)


# Supported keys as "table.name" -> validator. index.dir chooses where the
# default index lives; the others are command defaults.
_VALIDATORS = {
    "index.dir": _validate_index_dir,
    "scan.refresh-before": _validate_refresh_before,
}


def _split_key(key: str) -> tuple[str, str]:
    if key not in _VALIDATORS:
        supported = ", ".join(sorted(_VALIDATORS))
        raise FlatlasError(f"unknown configuration key: {key}; supported keys: {supported}")
    table, name = key.split(".", 1)
    return table, name


def _configured(config: dict, key: str) -> str | None:
    table, name = _split_key(key)
    section = config.get(table, {})
    if not isinstance(section, dict):
        raise FlatlasError(f"invalid Flatlas configuration: {table} must be a table")
    value = section.get(name)
    if value is not None:
        if not isinstance(value, str):
            raise FlatlasError(f"invalid Flatlas configuration: {key} must be a string")
        _VALIDATORS[key](value)
    return value


def get_default(key: str) -> str | None:
    """Read a saved setting without creating configuration or a database."""
    _split_key(key)
    return _configured(_read_config(user_paths()).unwrap(), key)


def defaults_disabled() -> bool:
    """Whether ``FLATLAS_NO_CONFIG`` asks commands to ignore saved command defaults."""
    return os.environ.get(NO_CONFIG_ENV, "").strip().lower() not in _FALSE_WORDS


def command_default(key: str) -> str | None:
    """A saved command default as commands apply it; ``None`` under ``FLATLAS_NO_CONFIG``.

    Only command defaults are ignored: the index location still comes from the
    configuration, so scripts find the same index.
    """
    return None if defaults_disabled() else get_default(key)


def list_defaults(*, include_unset: bool = False) -> dict[str, str | None]:
    """List saved settings, optionally including supported unset keys."""
    defaults = {key: get_default(key) for key in sorted(_VALIDATORS)}
    return {key: value for key, value in defaults.items() if include_unset or value is not None}


def set_default(key: str, value: str | None) -> None:
    """Set or unset a supported setting, preserving other settings and comments."""
    table, name = _split_key(key)
    if value is not None:
        _VALIDATORS[key](value)
    locations = user_paths()
    config = _read_config(locations)
    if table not in config:
        if value is None:
            return
        config[table] = tomlkit.table()
    section = config[table]
    if not isinstance(section, dict):
        raise FlatlasError(f"invalid Flatlas configuration: {table} must be a table")
    if value is None:
        section.pop(name, None)
    else:
        section[name] = value.strip()
    if "format_version" not in config:
        config["format_version"] = 1
    _write_config(locations, config)


def configured_database() -> Path | None:
    """Where the configuration points the default index, without checking it exists."""
    try:
        return resolve_database(create=False, require_existing=False)
    except FlatlasError:
        return None


def resolve_database(
    explicit: Path | None = None, *, create: bool = False, require_existing: bool = True,
) -> Path:
    """Locate the index: ``--db``, ``FLATLAS_DB``, ``index.dir``, then the path pinned by ``init``.

    ``FLATLAS_DB`` behaves exactly like ``--db``. ``index.dir`` is an explicit
    choice, so a missing file there means "not initialized" rather than
    silently starting an empty index.
    """
    if explicit is None and (from_environment := os.environ.get(DATABASE_ENV, "").strip()):
        explicit = Path(from_environment)
    if explicit is not None:
        return explicit.expanduser().resolve()
    locations = user_paths()
    config = _read_config(locations)
    index_dir = _configured(config.unwrap(), "index.dir")
    if index_dir is not None:
        directory = Path(index_dir.strip()).expanduser()
        database = directory / DATABASE_NAME
        if create:
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise FlatlasError(f"cannot create Flatlas index directory: {directory}: {exc}") from exc
        elif require_existing and not database.exists():
            raise FlatlasError(f"Flatlas is not initialized at {database} (index.dir); run 'flatlas init [PATH]' first.")
        return database
    if "database" in config:
        database_value = config["database"]
        if not isinstance(database_value, str) or not database_value:
            raise FlatlasError(f"invalid Flatlas configuration: {locations.config_file}")
        return Path(database_value).expanduser()
    if not create:
        if not require_existing:
            return locations.default_database
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
