"""User-global configuration and database discovery."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

from flatlas.errors import FlatlasError


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


def resolve_database(explicit: Path | None = None, *, create: bool = False) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    locations = user_paths()
    if locations.config_file.exists():
        try:
            config = tomllib.loads(locations.config_file.read_text(encoding="utf-8"))
            return Path(config["database"]).expanduser()
        except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
            raise FlatlasError(f"invalid Flatlas configuration: {locations.config_file}") from exc
    if not create:
        raise FlatlasError("Flatlas is not initialized; run 'flatlas init [PATH]' first.")
    locations.config_dir.mkdir(parents=True, exist_ok=True)
    locations.data_dir.mkdir(parents=True, exist_ok=True)
    database = locations.default_database.resolve()
    escaped = str(database).replace("\\", "\\\\")
    locations.config_file.write_text(
        f'format_version = 1\ndatabase = "{escaped}"\n', encoding="utf-8"
    )
    return database
