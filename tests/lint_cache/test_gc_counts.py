"""GC reports planned counts separately from actual removals, including history."""
import json
import os
import time
from pathlib import Path

import pytest

from datawrench.lint_cache.cache import Cache
from datawrench.lint_cache.cli import main
from .test_clear import snapshot
from .test_gc_budget import seed, size


def test_gc_counts_mixed_plan_budget_and_apply(project, capsys, monkeypatch):
    cache = Cache(project / "counts")
    now = time.time_ns()
    expired = seed(cache, project / "clean.sql", "a" * 64, now - 40 * 86400 * 10**9)
    deleted_source = project / "deleted.sql"
    deleted_source.write_text("SELECT 1;\n")
    missing = seed(cache, deleted_source, "b" * 64, now)
    deleted_source.unlink()
    orphan_source = project / "orphan.sql"
    orphan_source.write_text("SELECT 1;\n")
    orphan = seed(cache, orphan_source, "c" * 64, now)
    orphan[0].unlink()
    older = seed(cache, project / "bad.sql", "d" * 64, now - 2_000_000_000)
    newer = seed(cache, project / "parse.sql", "e" * 64, now - 1_000_000_000)
    outside = project / "private"
    outside.write_text("private SQL/input must not appear in plans")
    garbage = []
    for prefix, suffix in (("", ".json"), (".inputs-", "")):
        broken = cache.directory / (prefix + "0" * 64 + suffix)
        broken.write_text("{")
        link = cache.directory / (prefix + "1" * 64 + suffix)
        link.symlink_to(outside)
        fifo = cache.directory / (prefix + "2" * 64 + suffix)
        os.mkfifo(fifo)
        garbage.extend([broken, link, fifo])
    for name in ("unknown.json", ".entry-interrupted"):
        (cache.directory / name).write_text("unmanaged")
    nested = cache.directory / "ordinary"
    nested.mkdir()
    (nested / ("f" * 64 + ".json")).write_text("unmanaged nested")
    budget = size(newer)
    expected = {*expired, *missing, orphan[1], *older, *garbage}
    before = snapshot(project)
    # One validation per managed name; planned counts need no second pass over data.
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
        plan = cache.collect(max_bytes=budget, dry_run=True)
    assert sorted(reads) == sorted(p.name for p in [*cache.entries(), *cache.histories()])
    assert plan["planned_entries"] == 6
    assert plan["planned_histories"] == 7
    assert plan["removed"] == plan["histories_removed"] == 0
    assert {item["name"] for item in plan["plan"]} == {p.name for p in expected}
    reasons = {item["name"]: item["reason"] for item in plan["plan"]}
    assert reasons[older[0].name] == "over_budget"
    assert reasons[older[1].name] == "entry_removed"
    assert reasons[orphan[1].name] == "entry_missing"
    assert plan["bytes_after"] == budget
    details = cache.status(details=True)
    assert plan["bytes_before"] == details["managed_total_bytes"]
    assert sum(item["bytes"] for item in details["details"]) == plan["bytes_before"]
    assert sum(item["bytes"] for item in plan["plan"]) == plan["bytes_before"] - budget
    command = ["gc", "--cache-dir", str(cache.directory), "--max-bytes", str(budget)]
    assert main([*command, "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == plan
    assert "private" not in captured.out and str(project) not in captured.out
    assert snapshot(project) == before
    assert main(command) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    applied = json.loads(captured.out)
    assert applied == {**plan, "dry_run": False, "removed": 6, "histories_removed": 7}
    assert set(cache.entries() + cache.histories()) == set(newer)
    assert all(not p.exists() and not p.is_symlink() for p in expected)
    after = snapshot(project)
    changed = {".", str(cache.directory.relative_to(project))}
    removed = {str(p.relative_to(project)) for p in expected}
    assert after == {name: value for name, value in before.items() if name not in removed | changed} | {
        name: after[name] for name in changed}
    again = cache.collect(max_bytes=budget, dry_run=True)
    assert again["planned_entries"] == again["planned_histories"] == 0
    assert again["plan"] == []
    assert cache.gc(max_bytes=0) == 1  # Compatibility API still returns result-only int.


@pytest.mark.parametrize("state", ["missing", "empty", "marked", "live"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_gc_counts_empty_plan_does_not_create_or_rewrite(project, capsys, state, dry_run):
    cache = Cache(project / state)
    if state == "empty":
        cache.directory.mkdir()
    elif state == "marked":
        cache.prepare(create=True)
    elif state == "live":
        seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    before = snapshot(project)
    expected = {"dry_run": dry_run, "max_age_days": 30, "max_bytes": None,
                "bytes_before": cache.status()["managed_total_bytes"],
                "bytes_after": cache.status()["managed_total_bytes"],
                "removed": 0, "histories_removed": 0, "plan": [],
                "planned_entries": 0, "planned_histories": 0}
    assert cache.collect(dry_run=dry_run) == expected
    assert main(["gc", "--cache-dir", str(cache.directory), *(["--dry-run"] if dry_run else [])]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == expected
    assert snapshot(project) == before


@pytest.mark.parametrize("kind", ["entry", "history"])
@pytest.mark.parametrize("failure", ["directory", "inventory"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_gc_counts_preflight_error_without_partial_output(project, capsys, monkeypatch,
                                                        kind, failure, dry_run):
    cache = Cache(project / "counts")
    seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    path = cache.directory / ("f" * 64 + ".json" if kind == "entry" else ".inputs-" + "f" * 64)
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
        assert main(["gc", "--cache-dir", str(cache.directory), "--max-bytes", "0",
                     *(["--dry-run"] if dry_run else [])]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert ("subdirectory" if failure == "directory" else "inventory denial") in captured.err
    assert snapshot(project) == before
