import json
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flatlas import cli, config, refresh
from flatlas.db import open_database
from flatlas.errors import FlatlasError

pytestmark = pytest.mark.cli


@pytest.fixture
def locations(tmp_path: Path, monkeypatch) -> config.UserPaths:
    paths = config.UserPaths(tmp_path / "配置", tmp_path / "data 'quoted'")
    monkeypatch.setattr(config, "user_paths", lambda: paths)
    return paths


def test_set_get_unset_before_init_preserves_settings_and_comments(locations: config.UserPaths) -> None:
    runner = CliRunner()
    assert runner.invoke(cli.app, ["config", "get", "scan.refresh-before"]).stdout == ""
    assert not locations.config_dir.exists()
    assert runner.invoke(cli.app, ["config", "set", "scan.refresh-before", "7d"]).exit_code == 0
    assert not locations.data_dir.exists()
    assert runner.invoke(cli.app, ["config", "get", "scan.refresh-before"]).stdout == "7d\n"
    with locations.config_file.open("a", encoding="utf-8") as stream:
        stream.write('\n# keep this comment\n[future]\noption = "keep"\n')
    database = config.resolve_database(create=True)
    assert database == locations.default_database.resolve()
    assert config.get_default("scan.refresh-before") == "7d"
    assert config.resolve_database() == database
    assert runner.invoke(cli.app, ["config", "set", "scan.refresh-before", "12h"]).exit_code == 0
    assert config.resolve_database() == database
    assert runner.invoke(cli.app, ["config", "unset", "scan.refresh-before"]).exit_code == 0
    assert config.get_default("scan.refresh-before") is None
    assert runner.invoke(cli.app, ["config", "unset", "scan.refresh-before"]).exit_code == 0
    text = locations.config_file.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert tomllib.loads(text)["future"]["option"] == "keep"


def test_unset_missing_default_does_not_create_config(locations: config.UserPaths) -> None:
    config.set_default("scan.refresh-before", None)
    assert not locations.config_dir.exists()


@pytest.mark.parametrize("options", [[], ["-a"], ["--all"]])
def test_list_without_config_is_read_only(locations: config.UserPaths, options: list[str]) -> None:
    result = CliRunner().invoke(cli.app, ["config", "list", *options])
    assert result.exit_code == 0, result.exception
    assert result.stdout == ("index.dir = <unset>\nscan.refresh-before = <unset>\n" if options else "")
    assert not locations.config_dir.exists()
    assert not locations.data_dir.exists()


@pytest.mark.parametrize("options", [[], ["-a"], ["--all"]])
def test_list_shows_saved_defaults_and_leaves_config_unchanged(
    locations: config.UserPaths, options: list[str],
) -> None:
    config.set_default("scan.refresh-before", "7d")
    with locations.config_file.open("a", encoding="utf-8") as stream:
        stream.write('\n[future]\noption = "keep"\n')
    before = locations.config_file.read_bytes()
    result = CliRunner().invoke(cli.app, ["config", "list", *options])
    assert result.exit_code == 0, result.exception
    assert result.stdout == ("index.dir = <unset>\n" if options else "") + "scan.refresh-before = 7d\n"
    assert locations.config_file.read_bytes() == before
    assert not locations.data_dir.exists()
    config.set_default("scan.refresh-before", None)
    result = CliRunner().invoke(cli.app, ["config", "list", *options])
    assert result.exit_code == 0, result.exception
    assert result.stdout == ("index.dir = <unset>\nscan.refresh-before = <unset>\n" if options else "")


@pytest.mark.parametrize("options", [[], ["-a"]])
def test_list_reports_invalid_config(locations: config.UserPaths, options: list[str], monkeypatch, capsys) -> None:
    locations.config_dir.mkdir()
    locations.config_file.write_text("[scan\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["flatlas", "config", "list", *options])
    assert cli.main() == 2
    assert capsys.readouterr().err.startswith("error: invalid Flatlas configuration:")


@pytest.mark.parametrize("value", ["7d", "12h", "30m", "30s", "2w", "1970-01-01T00:00:01Z"])
def test_supported_values_are_stored_as_strings(locations: config.UserPaths, value: str) -> None:
    config.set_default("scan.refresh-before", value)
    assert config.get_default("scan.refresh-before") == value


@pytest.mark.parametrize("value", ["bad", "0d", "-7d", "", "9999-01-01T00:00:00Z", "999999999999999999999w"])
def test_invalid_setting_is_an_error_without_changing_file(
    locations: config.UserPaths, value: str, monkeypatch, capsys,
) -> None:
    config.set_default("scan.refresh-before", "7d")
    before = locations.config_file.read_bytes()
    monkeypatch.setattr(sys, "argv", ["flatlas", "config", "set", "scan.refresh-before", "--", value])
    assert cli.main() == 2
    assert capsys.readouterr().err.startswith("error: ")
    assert locations.config_file.read_bytes() == before


@pytest.mark.parametrize("action", ["get", "set", "unset"])
def test_unknown_keys_are_rejected(locations: config.UserPaths, action: str) -> None:
    args = ["config", action, "scan.typo"] + (["7d"] if action == "set" else [])
    result = CliRunner().invoke(cli.app, args)
    assert isinstance(result.exception, FlatlasError)
    assert "unknown configuration key" in str(result.exception)
    assert not locations.config_dir.exists()


@pytest.mark.parametrize("text", ['[scan\n', '[scan]\nrefresh-before = 7\n', 'scan = "bad"\n'])
def test_invalid_config_is_reported(locations: config.UserPaths, text: str) -> None:
    locations.config_dir.mkdir()
    locations.config_file.write_text(text, encoding="utf-8")
    with pytest.raises(FlatlasError, match="invalid Flatlas configuration"):
        config.get_default("scan.refresh-before")


def test_failed_replace_keeps_old_config(locations: config.UserPaths, monkeypatch) -> None:
    config.set_default("scan.refresh-before", "7d")
    before = locations.config_file.read_bytes()

    def denied(*_args):
        raise PermissionError("injected")

    monkeypatch.setattr(config.os, "replace", denied)
    with pytest.raises(FlatlasError, match="cannot write Flatlas configuration"):
        config.set_default("scan.refresh-before", "12h")
    assert locations.config_file.read_bytes() == before
    assert list(locations.config_dir.iterdir()) == [locations.config_file]


@pytest.mark.db
def test_explicit_database_still_overrides_config(locations: config.UserPaths, tmp_path: Path) -> None:
    locations.config_dir.mkdir()
    locations.config_file.write_text("invalid [", encoding="utf-8")
    explicit = tmp_path / "explicit.sqlite"
    assert config.resolve_database(explicit) == explicit.resolve()


@pytest.mark.scan
@pytest.mark.safety
def test_scan_default_override_full_and_unset_preserve_fresh_paths(
    locations: config.UserPaths, tmp_path: Path,
) -> None:
    runner = CliRunner()
    source = tmp_path / "source"
    source.mkdir()
    (source / "old.txt").write_text("payload", encoding="utf-8")
    database = tmp_path / "index.sqlite"
    options = ["--db", str(database)]
    assert runner.invoke(cli.app, ["init", str(source), *options]).exit_code == 0
    assert runner.invoke(cli.app, ["scan", "--format", "json", str(source), *options]).exit_code == 0
    (source / "old.txt").unlink()
    config.set_default("scan.refresh-before", "7d")
    result = runner.invoke(cli.app, ["scan", "--format", "json", str(source), *options])
    assert result.exit_code == 0, result.exception
    summary = json.loads(result.stdout)
    assert summary["refresh_before_ns"] is not None
    connection = open_database(database)
    try:
        assert connection.execute("SELECT state FROM path WHERE entry_kind='file'").fetchone()[0] == "present"
    finally:
        connection.close()
    result = runner.invoke(cli.app, ["scan", "--format", "json", str(source), "--refresh-before", "1970-01-01T00:00:01Z", *options])
    assert result.exit_code == 0, result.exception
    assert json.loads(result.stdout)["refresh_before_ns"] == 1_000_000_000
    result = runner.invoke(cli.app, ["scan", "--format", "json", str(source), "--full", *options])
    assert result.exit_code == 0, result.exception
    assert json.loads(result.stdout)["refresh_before_ns"] is None
    connection = open_database(database)
    try:
        assert connection.execute("SELECT state FROM path WHERE entry_kind='file'").fetchone()[0] == "deleted"
    finally:
        connection.close()
    config.set_default("scan.refresh-before", None)
    result = runner.invoke(cli.app, ["scan", "--format", "json", str(source), *options])
    assert result.exit_code == 0, result.exception
    assert json.loads(result.stdout)["refresh_before_ns"] is None


@pytest.mark.scan
@pytest.mark.parametrize("resume_args", [["--resume"], ["--resume-from", "1"]])
def test_resume_uses_saved_cutoff_instead_of_config(
    locations: config.UserPaths, tmp_path: Path, resume_args: list[str],
) -> None:
    runner = CliRunner()
    source = tmp_path / "source"
    source.mkdir()
    database = tmp_path / "index.sqlite"
    options = ["--db", str(database)]
    assert runner.invoke(cli.app, ["init", str(source), *options]).exit_code == 0
    result = runner.invoke(cli.app, ["scan", "--format", "json", str(source), "--refresh-before", "1970-01-01T00:00:01Z", *options])
    assert result.exit_code == 0, result.exception
    connection = open_database(database)
    try:
        connection.execute("UPDATE scan SET status='cancelled'")
        connection.execute("UPDATE scan_scope SET status='cancelled'")
        connection.commit()
    finally:
        connection.close()
    config.set_default("scan.refresh-before", "7d")
    path_args = [str(source)] if resume_args == ["--resume"] else []
    result = runner.invoke(cli.app, ["scan", "--format", "json", *path_args, *resume_args, *options])
    assert result.exit_code == 0, result.exception
    assert json.loads(result.stdout)["refresh_before_ns"] == 1_000_000_000


@pytest.mark.scan
@pytest.mark.parametrize("args", [["--refresh-before", "7d"], ["--resume"], ["--resume-from", "1"]])
def test_full_rejects_conflicting_options(locations: config.UserPaths, tmp_path: Path, args: list[str]) -> None:
    result = CliRunner().invoke(cli.app, ["scan", "--format", "json", str(tmp_path), "--full", *args])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


@pytest.mark.scan
def test_relative_default_is_resolved_for_each_scan(locations: config.UserPaths, tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    source = tmp_path / "source"
    source.mkdir()
    database = tmp_path / "index.sqlite"
    options = ["--db", str(database)]
    assert runner.invoke(cli.app, ["init", str(source), *options]).exit_code == 0
    config.set_default("scan.refresh-before", "7d")
    for now in (1_800_000_000_000_000_000, 1_800_000_001_000_000_000):
        monkeypatch.setattr(refresh.time, "time_ns", lambda current=now: current)
        result = runner.invoke(cli.app, ["scan", "--format", "json", str(source), *options])
        assert result.exit_code == 0, result.exception
        assert json.loads(result.stdout)["refresh_before_ns"] == now - 604800 * 1_000_000_000


@pytest.mark.db
def test_index_dir_sets_the_default_database_location(locations: config.UserPaths, tmp_path: Path) -> None:
    index_dir = tmp_path / "per-cluster" / "flatlas"
    runner = CliRunner()
    assert runner.invoke(cli.app, ["config", "set", "index.dir", str(index_dir)]).exit_code == 0
    assert runner.invoke(cli.app, ["config", "get", "index.dir"]).stdout == f"{index_dir}\n"
    with pytest.raises(FlatlasError, match="not initialized"):
        config.resolve_database()
    assert not index_dir.exists()

    source = tmp_path / "source"
    source.mkdir()
    result = runner.invoke(cli.app, ["init", str(source)])
    assert result.exit_code == 0, result.output
    assert (index_dir / "index.sqlite").is_file()
    assert config.resolve_database() == index_dir / "index.sqlite"
    # The location comes from index.dir, so init does not pin a database path.
    assert "database" not in tomllib.loads(locations.config_file.read_text(encoding="utf-8"))
    assert not locations.data_dir.exists()


@pytest.mark.db
def test_index_dir_overrides_the_pinned_database_and_reports_moves(
    locations: config.UserPaths, tmp_path: Path,
) -> None:
    pinned = config.resolve_database(create=True)
    open_database(pinned).close()
    index_dir = tmp_path / "moved"
    runner = CliRunner()
    result = runner.invoke(cli.app, ["config", "set", "index.dir", str(index_dir)])
    assert result.exit_code == 0, result.output
    assert str(pinned) in result.stderr and str(index_dir / "index.sqlite") in result.stderr
    assert config.resolve_database(require_existing=False) == index_dir / "index.sqlite"

    index_dir.mkdir()
    pinned.rename(index_dir / "index.sqlite")
    assert config.resolve_database() == index_dir / "index.sqlite"
    result = runner.invoke(cli.app, ["config", "unset", "index.dir"])
    assert result.exit_code == 0, result.output
    assert "note:" in result.stderr
    assert config.resolve_database() == pinned


@pytest.mark.parametrize("value", ["", "relative/dir", "   "])
def test_index_dir_must_be_absolute(locations: config.UserPaths, value: str) -> None:
    with pytest.raises(FlatlasError, match="index.dir"):
        config.set_default("index.dir", value)
    assert not locations.config_dir.exists()


def test_index_dir_accepts_home_relative_paths(locations: config.UserPaths, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("USERPROFILE" if sys.platform == "win32" else "HOME", str(tmp_path))
    config.set_default("index.dir", "~/indexes")
    assert config.get_default("index.dir") == "~/indexes"
    assert config.resolve_database(require_existing=False) == tmp_path / "indexes" / "index.sqlite"
