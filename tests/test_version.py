import pytest

from flatlas import version

pytestmark = pytest.mark.version


def test_detect_version_falls_back_without_git(monkeypatch) -> None:
    monkeypatch.setattr(version, "_run_git", lambda *_arguments: None)

    assert version._detect_version() == version.VersionInfo(version="0.2.0")


def test_detect_version_reports_clean_commit_and_date(monkeypatch) -> None:
    responses = {
        ("rev-parse", "--short=12", "HEAD"): "123456789abc",
        ("show", "-s", "--format=%cs", "HEAD"): "2026-09-16",
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(version, "_run_git", lambda *arguments: responses[arguments])

    detected = version._detect_version()

    assert detected.render() == "flatlas 0.2.0 (123456789abc, 2026-09-16)"


def test_detect_version_marks_dirty_worktree(monkeypatch) -> None:
    responses = {
        ("rev-parse", "--short=12", "HEAD"): "123456789abc",
        ("show", "-s", "--format=%cs", "HEAD"): "2026-09-16",
        ("status", "--porcelain"): " M src/flatlas/cli.py",
    }
    monkeypatch.setattr(version, "_run_git", lambda *arguments: responses[arguments])

    detected = version._detect_version()

    assert detected.render() == "flatlas 0.2.0 (123456789abc, 2026-09-16, dirty)"
