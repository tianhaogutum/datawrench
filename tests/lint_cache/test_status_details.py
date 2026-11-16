"""Opt-in status details identify structural corruption without exposing data."""
import json
import os
import time
from pathlib import Path

import pytest

from datawrench.lint_cache.cache import Cache, MARKER
from datawrench.lint_cache.cli import main
from .test_clear import snapshot
from .test_gc_budget import seed
from .test_status import ZERO


def test_details_identify_corruption_and_match_aggregate_once(project, capsys, monkeypatch):
    cache = Cache(project / "details")
    valid = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    orphan = seed(cache, project / "bad.sql", "b" * 64,
                  time.time_ns() - 40 * 86400 * 10**9)
    orphan[0].unlink()
    (project / "bad.sql").unlink()  # Source/entry existence is not structural validity.
    corrupt = []
    outside = project / "outside-private"
    outside.write_text("private data must not appear in output")
    for prefix, suffix in (("", ".json"), (".inputs-", "")):
        broken = cache.directory / (prefix + "c" * 64 + suffix)
        broken.write_text("{")
        corrupt.append(broken)
        for token, target in (("d", outside), ("e", project / "absent"), ("f", project)):
            link = cache.directory / (prefix + token * 64 + suffix)
            link.symlink_to(target)
            corrupt.append(link)
        fifo = cache.directory / (prefix + "0" * 64 + suffix)
        os.mkfifo(fifo)
        corrupt.append(fifo)
    for name in ("unknown.json", ".entry-interrupted", "A" * 64 + ".json"):
        (cache.directory / name).write_text("unmanaged")
    nested = cache.directory / "ordinary-directory"
    nested.mkdir()
    (nested / ("a" * 64 + ".json")).write_text("unmanaged nested result")
    expected_details = sorted(
        [{"name": p.name, "kind": kind, "bytes": p.lstat().st_size, "corrupt": bad}
         for paths, kind in ((cache.entries(), "entry"), (cache.histories(), "history"))
         for p in paths for bad in [p in corrupt]], key=lambda item: item["name"])
    aggregate = {"entries": 6, "bytes": sum(p.lstat().st_size for p in [valid[0], *corrupt[:5]]),
                 "corrupt": 5, "histories": 7,
                 "history_bytes": sum(p.lstat().st_size for p in [valid[1], orphan[1], *corrupt[5:]]),
                 "history_corrupt": 5,
                 "managed_total_bytes": sum(item["bytes"] for item in expected_details)}
    expected = {**aggregate, "details": expected_details}
    before = snapshot(project)
    original_entry, original_history = Cache._read, Cache._read_history
    reads = []

    def entry_read(self, path):
        reads.append(path.name)
        return original_entry(self, path)

    def history_read(self, path):
        reads.append(path.name)
        return original_history(self, path)

    with monkeypatch.context() as patch:
        patch.setattr(Cache, "_read", entry_read)
        patch.setattr(Cache, "_read_history", history_read)
        assert cache.status(details=True) == expected
    assert sorted(reads) == [item["name"] for item in expected_details]
    assert cache.status() == cache.status(details=False) == aggregate
    assert main(["status", "--cache-dir", str(cache.directory), "--details"]) == 0
    first = capsys.readouterr()
    assert first.err == "" and json.loads(first.out) == expected
    assert main(["status", "--cache-dir", str(cache.directory), "--details"]) == 0
    assert capsys.readouterr() == first
    assert main(["status", "--cache-dir", str(cache.directory)]) == 0
    assert json.loads(capsys.readouterr().out) == aggregate
    assert cache.plan_clear()["managed_total_bytes"] == aggregate["managed_total_bytes"]
    assert cache.collect(dry_run=True)["bytes_before"] == aggregate["managed_total_bytes"]
    assert snapshot(project) == before


@pytest.mark.parametrize("state", ["missing", "empty", "marked"])
def test_details_empty_cache_does_not_create_or_rewrite(project, capsys, state):
    cache = Cache(project / state)
    if state == "empty":
        cache.directory.mkdir()
    elif state == "marked":
        cache.prepare(create=True)
    before = snapshot(project)
    assert cache.status(details=True) == {**ZERO, "details": []}
    assert main(["status", "--cache-dir", str(cache.directory), "--details"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == {**ZERO, "details": []}
    assert snapshot(project) == before


@pytest.mark.parametrize("kind", ["entry", "history"])
@pytest.mark.parametrize("failure", ["directory", "inventory"])
def test_details_preflight_error_has_no_partial_json(project, capsys, monkeypatch, kind, failure):
    cache = Cache(project / "details")
    seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    name = "f" * 64 + ".json" if kind == "entry" else ".inputs-" + "f" * 64
    path = cache.directory / name
    if failure == "directory":
        path.mkdir()
        (path / "keep").write_text("untouched")
    else:
        path.write_text("untouched")
    before = snapshot(project)
    original = Path.lstat

    def denied(candidate):
        if candidate == path and failure == "inventory":
            raise PermissionError("synthetic inventory denial")
        return original(candidate)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", denied)
        assert main(["status", "--cache-dir", str(cache.directory), "--details"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert ("subdirectory" if failure == "directory" else "inventory denial") in captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("kind", ["entry", "history"])
def test_details_read_failure_is_corrupt_without_exposing_error(project, capsys, monkeypatch, kind):
    cache = Cache(project / "details")
    pair = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    before = snapshot(project)

    def denied(*args):
        raise PermissionError("private error must not be disclosed")

    monkeypatch.setattr(Cache, "_read" if kind == "entry" else "_read_history", denied)
    assert main(["status", "--cache-dir", str(cache.directory), "--details"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and "private" not in captured.out
    result = json.loads(captured.out)
    flags = {item["name"]: item["corrupt"] for item in result.pop("details")}
    assert flags == {pair[0].name: kind == "entry", pair[1].name: kind == "history"}
    assert result == cache.status()
    assert snapshot(project) == before


@pytest.mark.parametrize("unsafe", ["unmarked", "cache_link", "marker_link", "invalid_marker"])
def test_details_refuse_unsafe_caches_without_changes(project, capsys, unsafe):
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
    elif unsafe == "invalid_marker":
        (cache.directory / MARKER).write_text("invalid")
    before = snapshot(project)
    assert main(["status", "--cache-dir", str(cache.directory), "--details"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert snapshot(project) == before
