"""shared_path_resolution: memoized parent resolution must equal Path.resolve()."""

import os
from pathlib import Path

import pytest

import flatlas.paths as paths_module
from flatlas.paths import scope_path, shared_path_resolution

pytestmark = [pytest.mark.query, pytest.mark.safety]


@pytest.fixture
def links(tmp_path: Path) -> Path:
    """real/{a/b/f, c/}, up -> real/a/b, rel -> ../real/c (from real/a), loop1 <-> loop2."""
    real = tmp_path / "real"
    (real / "a" / "b").mkdir(parents=True)
    (real / "c").mkdir()
    (real / "a" / "b" / "f").write_bytes(b"x")
    try:
        (tmp_path / "up").symlink_to(real / "a" / "b", target_is_directory=True)
        (real / "a" / "rel").symlink_to(Path("..") / "c", target_is_directory=True)
        (tmp_path / "loop1").symlink_to(tmp_path / "loop2")
        (tmp_path / "loop2").symlink_to(tmp_path / "loop1")
    except OSError:
        pytest.skip("symlink creation unavailable")
    return tmp_path


def _cases(base: Path) -> list[str]:
    return [
        str(base / "real" / "a" / "b" / "f"),
        str(base / "up" / "f"),
        str(base / "up"),
        str(base / "up") + os.sep,
        str(base / "real" / "a" / "rel"),
        str(base / "real" / "a" / "rel" / "missing" / "deeper"),
        str(base / "up" / ".."),
        str(base / "real" / "a" / "rel" / ".." / "c"),
        str(base / "nope" / "x"),
        str(base / "loop1" / "x"),
        str(base / "real" / "." / "a"),
        str(base),
    ]


@pytest.mark.parametrize("follow", [False, True])
def test_shared_resolution_matches_plain_resolution(links: Path, monkeypatch, follow: bool) -> None:
    monkeypatch.chdir(links / "real" / "a")
    cases = [*_cases(links), "rel", "rel/../b", "b/f", "."]
    expected = [_outcome(case, follow) for case in cases]
    # Windows Path.resolve() may return the unresolved path for this loop.
    if os.name != "nt":
        assert any(isinstance(item, tuple) for item in expected), "the loop case must raise"
    with shared_path_resolution():
        # Twice, so the second round is served from the memo.
        assert [_outcome(case, follow) for case in cases] == expected
        assert [_outcome(case, follow) for case in cases] == expected


def _outcome(case: str, follow: bool) -> Path | tuple[str, str]:
    try:
        return scope_path(case, follow=follow)
    except RuntimeError as exc:
        return type(exc).__name__, str(exc)


def test_shared_resolution_resolves_each_prefix_once(links: Path, monkeypatch) -> None:
    calls: list[str] = []
    original = os.path.islink

    def counting(path: str) -> bool:
        calls.append(os.fspath(path))
        return original(path)

    monkeypatch.setattr(paths_module.os.path, "islink", counting)
    names = [str(links / "real" / "a" / "b" / f"f{index}") for index in range(50)]
    with shared_path_resolution():
        for name in names:
            scope_path(name, follow=False)
    assert len(calls) == len(set(calls))
    assert len(calls) <= len(Path(names[0]).parent.parts)
