"""dupes treats hardlink aliases as one copy and reads each inode once."""

import os
from contextlib import closing
from pathlib import Path
from threading import BoundedSemaphore, get_ident, local

import pytest

from flatlas import hashing
from flatlas.db import open_database
from flatlas.hashing import ensure_duplicate_hashes
from flatlas.paths import (
    discover_namespace,
    register_namespace,
)
from flatlas.planning import (
    create_dry_run_plan,
    plan_operations,
)
from flatlas.queries import duplicate_groups
from flatlas.scanning import scan_directory

pytestmark = [
    pytest.mark.hash,
    pytest.mark.skipif(not hasattr(os, "link") or os.name != "posix", reason="needs POSIX hardlinks"),
]

PAYLOAD = b"same content " * 8


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """``scope/a`` = ``scope/b`` (one inode) and ``scope/c`` hold the same bytes; ``scope/d`` differs;
    ``scope/e`` = ``scope/f`` are aliases with no other copy; ``outside/g`` links to ``c``."""
    scope = tmp_path / "source" / "scope"
    scope.mkdir(parents=True)
    (tmp_path / "source" / "outside").mkdir()
    (scope / "a").write_bytes(PAYLOAD)
    os.link(scope / "a", scope / "b")
    (scope / "c").write_bytes(PAYLOAD)
    (scope / "d").write_bytes(b"x" * len(PAYLOAD))
    (scope / "e").write_bytes(b"only aliases" * 50)
    os.link(scope / "e", scope / "f")
    os.link(scope / "c", tmp_path / "source" / "outside" / "g")
    return scope


def _index(tmp_path: Path, source: Path):
    connection = open_database(tmp_path / "index.sqlite")
    register_namespace(connection, discover_namespace(source))
    scan_directory(connection, source)
    return connection


@pytest.mark.parametrize("quick_workers", [1, 4])
def test_aliases_count_as_one_copy_and_are_read_once(tmp_path: Path, tree: Path, quick_workers: int) -> None:
    connection = _index(tmp_path, tree.parent)
    try:
        summary = ensure_duplicate_hashes(connection, tree, quick_workers=quick_workers)
        # e/f have no other same-size inode, so they are not candidates; b reuses a's read.
        assert summary.candidate_files == 4
        assert summary.content_files_read == 3
        [group] = duplicate_groups(connection, scope=tree)
        assert (group["count"], group["inodes"]) == (3, 2)
        assert group["theoretical_savings"] == len(PAYLOAD)
        assert sorted(Path(path["path_display"]).name for path in group["paths"]) == ["a", "b", "c"]
    finally:
        connection.close()


def test_quick_alias_reads_use_the_same_worker_slots(tmp_path: Path, tree: Path, monkeypatch) -> None:
    held = local()
    readers = []
    main_thread = get_ident()
    original = hashing._validated_digest

    class TrackedSlots:
        def __init__(self) -> None:
            self.capacity = BoundedSemaphore(2)

        def __enter__(self) -> None:
            self.capacity.acquire()
            held.slots = self

        def __exit__(self, *_exc: object) -> None:
            held.slots = None
            self.capacity.release()

    slots = TrackedSlots()

    def make_slots(workers):
        assert workers == 2
        return slots

    def read(candidate, *, full, timer):
        assert not full and held.slots is slots
        readers.append(get_ident())
        return original(candidate, full=full, timer=timer)

    monkeypatch.setattr(hashing, "Semaphore", make_slots)
    monkeypatch.setattr(hashing, "_validated_digest", read)
    with closing(_index(tmp_path, tree.parent)) as connection:
        summary = ensure_duplicate_hashes(connection, tree, quick_workers=2)
    assert summary.complete and summary.content_files_read == len(readers) == 3
    assert readers.count(main_thread) == 1


@pytest.mark.parametrize("after_commit", [False, True])
def test_rescanned_inode_hash_is_not_reused_by_a_sibling_alias(tmp_path: Path, monkeypatch, after_commit: bool) -> None:
    scope = tmp_path / "source"
    a = scope / "a" / "file"
    other = scope / "b" / "file"
    alias = scope / "c" / "file"
    for directory in (a.parent, other.parent, alias.parent):
        directory.mkdir(parents=True)
    a.write_bytes(b"x" * 16384)
    other.write_bytes(b"x" * 16384)
    os.link(a, alias)
    db = tmp_path / "index.sqlite"
    original = hashing._validated_digest

    def rescan() -> None:
        previous = a.stat()
        a.write_bytes(b"y" * 16384)
        os.utime(a, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
        with closing(open_database(db)) as writer:
            assert scan_directory(writer, a.parent)["status"] == "completed"

    def read(candidate, *, full, timer):
        digest = original(candidate, full=full, timer=timer)
        if not after_commit and Path(candidate["path_display"]) == a:
            rescan()
        return digest

    def progress(update: hashing.HashProgress) -> None:
        if after_commit and update.phase == "quick" and update.completed_files == 1:
            rescan()

    with closing(_index(tmp_path, scope)) as connection:
        monkeypatch.setattr(hashing, "_validated_digest", read)
        summary = ensure_duplicate_hashes(connection, scope, quick_workers=2, commit_batch=1, on_progress=progress)
        assert duplicate_groups(connection, scope=scope) == []
    expected = {str(alias)} if after_commit else {str(a), str(alias)}
    assert summary.changed_files == len(expected) and summary.errors == 0
    assert {issue.path for issue in summary.issues} == expected


def test_links_outside_the_scope_are_flagged(tmp_path: Path, tree: Path) -> None:
    connection = _index(tmp_path, tree.parent)
    try:
        ensure_duplicate_hashes(connection, tree.parent)
        [scoped] = duplicate_groups(connection, scope=tree)
        assert scoped["external_links"] is True  # c is also outside/g
        [whole] = duplicate_groups(connection, scope=tree.parent)
        assert (whole["count"], whole["inodes"], whole["external_links"]) == (4, 2, False)
    finally:
        connection.close()


def test_plan_skips_aliases_of_the_canonical_file(tmp_path: Path, tree: Path) -> None:
    connection = _index(tmp_path, tree.parent)
    try:
        ensure_duplicate_hashes(connection, tree.parent)
        root_id = connection.execute("SELECT id FROM root").fetchone()["id"]
        operations = plan_operations(connection, create_dry_run_plan(connection, root_id))
        # Lexical order makes outside/g canonical; its alias c is skipped, a and b need relinking.
        assert sorted(Path(op["replacement_path"]).name for op in operations) == ["a", "b"]
        assert {Path(op["canonical_path"]).name for op in operations} == {"g"}
    finally:
        connection.close()
