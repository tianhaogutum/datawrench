"""Full-clear previews enumerate owned names without reading or changing them."""
import json
import os
import time
from pathlib import Path

import pytest

from datawrench.lint_cache.cache import Cache, ENTRY, MARKER
from datawrench.lint_cache.cli import main
from .test_clear import snapshot
from .test_gc_budget import seed


EMPTY = {"dry_run": True, "removed": 0, "histories_removed": 0,
         "planned_entries": 0, "planned_histories": 0, "managed_total_bytes": 0, "plan": []}


def test_preview_matches_full_clear_without_reads_or_writes(project, capsys, monkeypatch):
    cache = Cache(project / "preview")
    seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    outside = project / "outside"
    outside.mkdir()
    (outside / "private").write_text("untouched")
    outside_before = snapshot(outside)
    for prefix, suffix in (("", ".json"), (".inputs-", "")):
        (cache.directory / (prefix + "b" * 64 + suffix)).write_text("{")
        for token, target in (("c", outside / "private"), ("d", outside),
                              ("e", project / "absent")):
            (cache.directory / (prefix + token * 64 + suffix)).symlink_to(target)
        os.mkfifo(cache.directory / (prefix + "f" * 64 + suffix))
    for name in ("unknown.json", ".entry-interrupted", "A" * 64 + ".json",
                 ".inputs-short", ".inputs-" + "a" * 64 + ".json"):
        (cache.directory / name).write_text("unmanaged")
    (cache.directory / "unknown-link").symlink_to(outside)
    nested = cache.directory / "ordinary-directory"
    nested.mkdir()
    (nested / ("a" * 64 + ".json")).write_text("unmanaged nested result")
    owned = sorted([*cache.entries(), *cache.histories()])
    plan = [{"name": p.name, "kind": "entry" if ENTRY.fullmatch(p.name) else "history",
             "bytes": p.lstat().st_size} for p in owned]
    expected = {"dry_run": True, "removed": 0, "histories_removed": 0,
                "planned_entries": 6, "planned_histories": 6,
                "managed_total_bytes": sum(item["bytes"] for item in plan), "plan": plan}
    before = snapshot(cache.directory)

    def no_read(*args):
        raise AssertionError("preview must not read managed contents")

    monkeypatch.setattr(Cache, "_read", no_read)
    monkeypatch.setattr(Cache, "_read_history", no_read)
    assert cache.plan_clear() == expected
    assert main(["clear", "--cache-dir", str(cache.directory), "--dry-run"]) == 0
    first = capsys.readouterr()
    assert first.err == "" and json.loads(first.out) == expected
    assert main(["clear", "--cache-dir", str(cache.directory), "--dry-run"]) == 0
    assert capsys.readouterr() == first
    assert snapshot(cache.directory) == before
    assert snapshot(outside) == outside_before

    assert main(["clear", "--cache-dir", str(cache.directory)]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == {"removed": 6}
    assert all(not p.exists() and not p.is_symlink() for p in owned)
    after = snapshot(cache.directory)
    after.pop(".")  # Deleting owned children changes only the parent directory metadata.
    assert after == {k: v for k, v in before.items() if k not in {p.name for p in owned} | {"."}}
    assert snapshot(outside) == outside_before
    assert not (project / "absent").exists()
    assert cache.clear() == 0  # Existing integer API remains unchanged.


@pytest.mark.parametrize("state", ["missing", "empty", "marked"])
def test_empty_preview_does_not_create_or_rewrite(project, capsys, state):
    cache = Cache(project / state)
    if state == "empty":
        cache.directory.mkdir()
    elif state == "marked":
        cache.prepare(create=True)
    before = snapshot(project)
    assert cache.plan_clear() == EMPTY
    assert main(["clear", "--cache-dir", str(cache.directory), "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == EMPTY
    assert snapshot(project) == before


@pytest.mark.parametrize("kind", ["entry", "history"])
def test_preview_refuses_managed_directories_without_partial_json(project, capsys, kind):
    cache = Cache(project / "preview")
    seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    name = "f" * 64 + ".json" if kind == "entry" else ".inputs-" + "f" * 64
    nested = cache.directory / name
    nested.mkdir()
    (nested / "keep").write_text("untouched")
    before = snapshot(project)
    assert main(["clear", "--cache-dir", str(cache.directory), "--dry-run"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "subdirectory" in captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("kind", ["entry", "history"])
def test_preview_inventory_failure_has_no_partial_json(project, capsys, monkeypatch, kind):
    cache = Cache(project / "preview")
    pair = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    denied_path = pair[0 if kind == "entry" else 1]
    before = snapshot(project)
    original = Path.lstat

    def denied(path):
        if path == denied_path:
            raise PermissionError("synthetic inventory denial")
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", denied)
        assert main(["clear", "--cache-dir", str(cache.directory), "--dry-run"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "inventory denial" in captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("unsafe", ["unmarked", "cache_link", "marker_link", "invalid_marker"])
def test_preview_refuses_unsafe_caches_without_changes(project, capsys, unsafe):
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
    assert main(["clear", "--cache-dir", str(cache.directory), "--dry-run"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert snapshot(project) == before
