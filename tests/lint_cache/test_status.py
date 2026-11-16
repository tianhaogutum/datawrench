"""Status measures all owned bytes without repairing or following managed paths."""
import json
import os
import time
from pathlib import Path

import pytest

from datawrench.lint_cache.cache import Cache, MARKER
from datawrench.lint_cache.cli import main
from .test_gc_budget import seed, size, snapshot


ZERO = {"entries": 0, "bytes": 0, "corrupt": 0, "histories": 0,
        "history_bytes": 0, "history_corrupt": 0, "managed_total_bytes": 0}


def test_mixed_status_matches_gc_accounting(project, capsys):
    cache = Cache(project / "mixed")
    valid = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    # A structurally valid orphan/expired history still occupies managed bytes.
    orphan = seed(cache, project / "bad.sql", "b" * 64,
                  time.time_ns() - 40 * 86400 * 10**9)
    orphan[0].unlink()
    broken_entry = cache.directory / ("c" * 64 + ".json")
    broken_history = cache.directory / (".inputs-" + "d" * 64)
    broken_entry.write_text("{")
    broken_history.write_text("null")
    for name in ("unknown.json", ".entry-interrupted", "A" * 64 + ".json",
                 ".inputs-short", ".inputs-" + "e" * 64 + ".json"):
        (cache.directory / name).write_text("unmanaged" * 1000)
    nested = cache.directory / "ordinary-directory"
    nested.mkdir()
    (nested / ("f" * 64 + ".json")).write_text("ignored")
    before = snapshot(cache.directory)

    assert main(["status", "--cache-dir", str(cache.directory)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    entry_bytes = size((valid[0], broken_entry))
    history_bytes = size((valid[1], orphan[1], broken_history))
    assert result == {"entries": 2, "bytes": entry_bytes, "corrupt": 1,
                      "histories": 3, "history_bytes": history_bytes, "history_corrupt": 1,
                      "managed_total_bytes": entry_bytes + history_bytes}
    assert result == cache.status()
    assert result["managed_total_bytes"] == cache.collect(dry_run=True)["bytes_before"]
    assert snapshot(cache.directory) == before
    assert (nested / ("f" * 64 + ".json")).read_text() == "ignored"


@pytest.mark.parametrize("state", ["missing", "empty", "marked", "history_only", "result_only"])
def test_status_empty_and_single_kind(project, state):
    cache = Cache(project / state)
    expected = dict(ZERO)
    if state == "empty":
        cache.directory.mkdir()
    elif state == "marked":
        cache.prepare(create=True)
    elif state in ("history_only", "result_only"):
        entry, history = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
        if state == "history_only":
            entry.unlink()
            expected.update(histories=1, history_bytes=size((history,)),
                            managed_total_bytes=size((history,)))
        else:
            history.unlink()
            expected.update(entries=1, bytes=size((entry,)), managed_total_bytes=size((entry,)))
    before = snapshot(cache.directory) if cache.directory.exists() else None
    assert cache.status() == expected
    assert cache.collect(dry_run=True)["bytes_before"] == expected["managed_total_bytes"]
    if before is None:
        assert not cache.directory.exists()
    else:
        assert snapshot(cache.directory) == before


def test_managed_symlinks_and_fifos_are_corrupt_without_following(project):
    cache = Cache(project / "special")
    cache.prepare(create=True)
    outside = project / "outside"
    outside.write_bytes(b"private" * 10000)
    entries, histories = [], []
    for prefix, suffix, paths in (("", ".json", entries), (".inputs-", "", histories)):
        for token, target in (("a", outside), ("b", project / "absent"), ("c", project)):
            path = cache.directory / (prefix + token * 64 + suffix)
            path.symlink_to(target)
            paths.append(path)
        fifo = cache.directory / (prefix + "d" * 64 + suffix)
        os.mkfifo(fifo)
        paths.append(fifo)
    # Inode/mtime/link snapshots avoid opening the FIFO or reading outside data.
    def special_snapshot():
        return {p: ((p.lstat().st_ino, p.lstat().st_mtime_ns, p.lstat().st_mode,
                     p.lstat().st_size), os.readlink(p) if p.is_symlink() else None)
                for p in [*entries, *histories]}

    before = special_snapshot()
    assert cache.status() == {"entries": 4, "bytes": size(entries), "corrupt": 4,
                              "histories": 4, "history_bytes": size(histories),
                              "history_corrupt": 4,
                              "managed_total_bytes": size([*entries, *histories])}
    assert cache.collect(dry_run=True)["bytes_before"] == size([*entries, *histories])
    assert special_snapshot() == before
    assert outside.read_bytes() == b"private" * 10000


@pytest.mark.parametrize("kind", ["entry", "history"])
def test_status_refuses_managed_directories_without_partial_json(project, capsys, kind):
    cache = Cache(project / "directories")
    cache.prepare(create=True)
    name = "a" * 64 + ".json" if kind == "entry" else ".inputs-" + "a" * 64
    nested = cache.directory / name
    nested.mkdir()
    (nested / "keep").write_text("untouched")
    assert main(["status", "--cache-dir", str(cache.directory)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "subdirectory" in captured.err
    assert (nested / "keep").read_text() == "untouched"


@pytest.mark.parametrize("unsafe", ["unmarked", "cache_link", "marker_link"])
def test_status_refuses_unsafe_cache(project, capsys, unsafe):
    cache = Cache(project / "unsafe")
    cache.directory.mkdir()
    keep = cache.directory / "keep"
    keep.write_text("untouched")
    if unsafe == "cache_link":
        link = project / "link"
        link.symlink_to(cache.directory, target_is_directory=True)
        cache = Cache(link)
    elif unsafe == "marker_link":
        (cache.directory / MARKER).symlink_to(keep)
    assert main(["status", "--cache-dir", str(cache.directory)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert keep.read_text() == "untouched"


def test_status_inventory_io_error_has_no_partial_json(project, capsys, monkeypatch):
    cache = Cache(project / "denied")
    entry, _ = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    original = Path.lstat

    def denied(path):
        if path == entry:
            raise PermissionError("synthetic inventory denial")
        return original(path)

    monkeypatch.setattr(Path, "lstat", denied)
    assert main(["status", "--cache-dir", str(cache.directory)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "inventory denial" in captured.err
