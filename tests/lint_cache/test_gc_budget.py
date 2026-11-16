"""Real lint records plus small synthetic caches exercise bounded maintenance."""
import json
import os
import time

import pytest

from datawrench.lint_cache.cache import Cache, COMPONENTS, MARKER
from datawrench.lint_cache.cli import main
from datawrench.lint_cache.engine import lint
from datawrench.lint_cache.fingerprint import InputError, digest
from .test_engine import stable


def seed(cache, source, key, written):
    cache.prepare(create=True)
    cache.put(key, source, {"violations": [], "statistics": {}, "timings": {}})
    cache.remember(key, source, {name: digest(name) for name in COMPONENTS})
    entry, history = cache.directory / (key + ".json"), cache._history_path(source)
    for path in (entry, history):
        os.utime(path, ns=(written, written))
    return entry, history


def snapshot(root):
    # Include inode/mtime so even an identical atomic rewrite would be detected.
    return {p.name: ((p.lstat().st_ino, p.lstat().st_mtime_ns, p.lstat().st_mode,
                      p.lstat().st_size), os.readlink(p) if p.is_symlink() else p.read_bytes())
            for p in root.iterdir() if not p.is_dir() or p.is_symlink()}


def size(paths):
    return sum(p.lstat().st_size for p in paths)


def test_budget_plan_dry_run_and_exact_boundary(project, capsys):
    cache = Cache(project / "budget")
    now = time.time_ns()
    older = seed(cache, project / "clean.sql", "a" * 64, now - 2_000_000_000)
    newer = seed(cache, project / "bad.sql", "b" * 64, now - 1_000_000_000)
    budget = size(newer)
    before = snapshot(cache.directory)
    command = ["gc", "--cache-dir", str(cache.directory), "--max-bytes", str(budget)]
    assert main([*command, "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] and plan["removed"] == plan["histories_removed"] == 0
    assert plan["bytes_before"] == size((*older, *newer))
    assert plan["bytes_after"] == budget
    assert [(p["name"], p["reason"]) for p in plan["plan"]] == [
        (older[1].name, "entry_removed"), (older[0].name, "over_budget")]
    assert snapshot(cache.directory) == before
    assert main([*command, "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out) == plan
    assert main(command) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["plan"] == plan["plan"]
    assert applied["removed"] == applied["histories_removed"] == 1
    assert all(not p.exists() for p in older)
    assert all(p.exists() for p in newer)
    assert cache.collect(max_bytes=budget)["plan"] == []  # exactly at budget
    assert cache.gc(max_bytes=0) == 1  # existing integer API remains usable
    assert set(p.name for p in cache.directory.iterdir()) == {MARKER}


def test_expired_missing_corrupt_before_budget_and_safe_targets(project):
    cache = Cache(project / "budget")
    now = time.time_ns()
    expired = seed(cache, project / "clean.sql", "a" * 64, now - 40 * 86400 * 10**9)
    missing_source = project / "deleted.sql"
    missing_source.write_text("SELECT 1;\n")
    missing = seed(cache, missing_source, "b" * 64, now)
    missing_source.unlink()
    keep = seed(cache, project / "bad.sql", "c" * 64, now)
    corrupt = cache.directory / ("d" * 64 + ".json")
    corrupt.write_text("{")
    outside = project / "outside"
    outside.write_text("untouched")
    link = cache.directory / ("e" * 64 + ".json")
    link.symlink_to(outside)
    history_link = cache.directory / (".inputs-" + "e" * 64)
    history_link.symlink_to(outside)
    fifo = cache.directory / ("f" * 64 + ".json")
    os.mkfifo(fifo)
    unknown = cache.directory / "unrecognized.json"
    unknown.write_text("keep" * 10000)
    unrelated_link = cache.directory / "outside-link"
    unrelated_link.symlink_to(project, target_is_directory=True)
    report = cache.collect(max_bytes=size(keep), dry_run=True)
    reasons = {p["name"]: p["reason"] for p in report["plan"]}
    assert reasons[expired[0].name] == "expired"
    assert reasons[missing[0].name] == "source_missing"
    assert reasons[corrupt.name] == reasons[link.name] == reasons[fifo.name] == "corrupt"
    assert report["bytes_after"] == size(keep)
    assert outside.read_text() == "untouched" and link.is_symlink() and fifo.exists()
    applied = cache.collect(max_bytes=size(keep))
    assert applied["plan"] == report["plan"]
    assert applied["removed"] == 5 and applied["histories_removed"] == 3
    assert all(p.exists() for p in keep)
    assert outside.read_text() == "untouched"
    assert unknown.read_text() == "keep" * 10000
    assert unrelated_link.is_symlink()
    assert not link.is_symlink() and not history_link.is_symlink()


def test_equal_timestamps_evict_by_filename_not_creation_order(project):
    cache = Cache(project / "ties")
    now = time.time_ns()
    # Reverse insertion order proves tie-breaking is independent of directory order.
    newer = seed(cache, project / "bad.sql", "b" * 64, now)
    older = seed(cache, project / "clean.sql", "a" * 64, now)
    report = cache.collect(max_bytes=size(newer))
    assert report["bytes_after"] == size(newer)
    assert not older[0].exists() and newer[0].exists()


def test_pruned_lint_matches_no_cache(project):
    paths = ["clean.sql", "bad.sql", "parse.sql"]
    lint(paths)
    cache = Cache(project / ".sqlfluff_cache")
    entries = list(cache.directory.glob("*.json"))
    # Keep just the newest result and its metadata; at least one real hit remains.
    newest = max(entries, key=lambda p: (p.stat().st_mtime_ns, p.name))
    history = next(p for p in cache.histories()
                   if json.loads(p.read_text())["key"] == newest.stem)
    budget = size((newest, history))
    assert cache.collect(max_bytes=budget)["removed"] == 2
    result = lint(paths)
    assert (result.stats.hits, result.stats.misses) == (1, 2)
    assert stable(result.records) == stable(lint(paths, no_cache=True).records)
    assert result.exit_code == 1


@pytest.mark.parametrize("budget", [-1, 1.5, True])
def test_invalid_api_budget_never_changes_files(project, budget):
    cache = Cache(project / "invalid")
    seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    before = snapshot(cache.directory)
    with pytest.raises(InputError, match="nonnegative integer"):
        cache.collect(max_bytes=budget)
    assert snapshot(cache.directory) == before


@pytest.mark.parametrize("budget", ["-1", "1.5", "nan"])
def test_invalid_cli_budget_exits_two(project, capsys, budget):
    with pytest.raises(SystemExit) as error:
        main(["gc", "--max-bytes", budget])
    assert error.value.code == 2
    assert "--max-bytes" in capsys.readouterr().err
    assert not (project / ".sqlfluff_cache").exists()


def test_dry_run_missing_and_unmarked_cache(project):
    cache = Cache(project / "absent")
    report = cache.collect(max_bytes=0, dry_run=True)
    assert report["bytes_before"] == report["bytes_after"] == 0 and report["plan"] == []
    assert not cache.directory.exists()
    cache.directory.mkdir()
    unknown = cache.directory / "keep"
    unknown.write_text("unmarked")
    with pytest.raises(InputError, match="unmarked"):
        cache.collect(max_bytes=0, dry_run=True)
    assert unknown.read_text() == "unmarked"


@pytest.mark.parametrize("dry_run", [False, True])
def test_subdirectory_error_preflights_before_any_removal(project, dry_run):
    cache = Cache(project / "preflight")
    entry, _ = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    entry.write_text("{")
    directory = cache.directory / ("f" * 64 + ".json")
    directory.mkdir()
    (directory / "keep").write_text("untouched")
    with pytest.raises(InputError, match="subdirectory"):
        cache.collect(max_bytes=0, dry_run=dry_run)
    assert entry.read_text() == "{"
    assert (directory / "keep").read_text() == "untouched"


def test_history_garbage_frees_budget_without_evicting_live_result(project):
    cache = Cache(project / "history-budget")
    now = time.time_ns()
    keep = seed(cache, project / "clean.sql", "a" * 64, now)
    orphan = seed(cache, project / "bad.sql", "b" * 64, now)
    orphan[0].unlink()
    os.utime(keep[1], ns=(now - 40 * 86400 * 10**9,) * 2)
    corrupt = cache.directory / (".inputs-" + "c" * 64)
    corrupt.write_text("{")
    report = cache.collect(max_bytes=size((keep[0],)), dry_run=True)
    assert report["bytes_after"] == size((keep[0],))
    assert {p["name"]: p["reason"] for p in report["plan"]} == {
        keep[1].name: "expired", orphan[1].name: "entry_missing", corrupt.name: "corrupt"}
    applied = cache.collect(max_bytes=size((keep[0],)))
    assert applied["removed"] == 0 and applied["histories_removed"] == 3
    assert keep[0].exists()


def test_age_only_dry_run_and_application_failure_output(project, capsys, monkeypatch):
    cache = Cache(project / ".sqlfluff_cache")
    entry, _ = seed(cache, project / "clean.sql", "a" * 64,
                    time.time_ns() - 40 * 86400 * 10**9)
    before = snapshot(cache.directory)
    assert main(["gc", "--dry-run"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["max_bytes"] is None and report["bytes_after"] == 0
    assert report["removed"] == 0 and snapshot(cache.directory) == before

    def denied(*args):
        raise PermissionError("synthetic permission failure")

    monkeypatch.setattr(Cache, "_remove", denied)
    assert main(["gc"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "permission failure" in captured.err
    assert entry.exists()
