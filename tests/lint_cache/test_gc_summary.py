"""Name-free GC summaries project the same plan, without another content read."""
import json
import os
import time
from pathlib import Path

import pytest

from datawrench.lint_cache.cache import Cache, ENTRY, HISTORY
from datawrench.lint_cache.cli import main
from datawrench.lint_cache.fingerprint import InputError
from .test_clear import snapshot
from .test_gc_budget import seed, size


CODES = ("corrupt", "source_missing", "expired", "entry_missing", "entry_removed", "over_budget")


def counts(plan):
    result = {kind: dict.fromkeys(CODES, 0) for kind in ("entries", "histories")}
    for item in plan:
        result["entries" if item["kind"] == "entry" else "histories"][item["reason"]] += 1
    return result


def summary_of(report):
    return {key: value for key, value in report.items() if key != "plan"} | {
        "planned_reasons": counts(report["plan"])}


@pytest.fixture
def mixed(project, monkeypatch):
    cache = Cache(project / "private-cache")
    now = time.time_ns()
    monkeypatch.setattr("datawrench.lint_cache.cache.time.time", lambda: now / 10**9)

    def pair(token, age=0):
        source = project / ("private-source-" + token + ".sql")
        source.write_text("SELECT 'sensitive SQL';\n")
        return source, seed(cache, source, token * 64, now - age)

    _, expired = pair("a", 40 * 86400 * 10**9)
    missing_source, missing = pair("b")
    missing_source.unlink()
    _, orphan = pair("c")
    orphan[0].unlink()
    _, older = pair("d", 2 * 10**9)
    _, newer = pair("e", 10**9)
    _, old_history = pair("f")
    os.utime(old_history[1], ns=(now - 40 * 86400 * 10**9,) * 2)
    target = project / "private-target"
    target.write_text("sensitive target must never be opened")
    garbage = []
    for prefix, suffix in (("", ".json"), (".inputs-", "")):
        broken = cache.directory / (prefix + "0" * 64 + suffix)
        broken.write_text("{sensitive malformed content")
        link = cache.directory / (prefix + "1" * 64 + suffix)
        link.symlink_to(target)
        fifo = cache.directory / (prefix + "2" * 64 + suffix)
        os.mkfifo(fifo)
        garbage.extend((broken, link, fifo))
    for name in ("unknown.json", ".entry-interrupted"):
        (cache.directory / name).write_text("unmanaged")
    (cache.directory / "unknown-link").symlink_to(project, target_is_directory=True)
    nested = cache.directory / "ordinary"
    nested.mkdir()
    (nested / ("9" * 64 + ".json")).write_text("unmanaged nested")
    expected = {*expired, *missing, orphan[1], *older, old_history[1], *garbage}
    keep = {*newer, old_history[0]}
    return cache, size(keep), expected, keep


def test_summary_mixed_matches_plan_single_read_and_exact_apply(project, mixed, monkeypatch, capsys):
    cache, budget, expected, keep = mixed
    before = snapshot(project)
    default = cache.collect(max_bytes=budget, dry_run=True)
    assert {item["name"] for item in default["plan"]} == {p.name for p in expected}
    wanted = summary_of(default)
    assert wanted["planned_reasons"] == {
        "entries": dict(zip(CODES, (3, 1, 1, 0, 0, 1))),
        "histories": dict(zip(CODES, (3, 1, 1, 1, 2, 0)))}
    reads = []
    original_entry, original_history, original_open = Cache._read, Cache._read_history, os.open

    def entry_read(self, path):
        reads.append(path.name)
        return original_entry(self, path)

    def history_read(self, path):
        reads.append(path.name)
        return original_history(self, path)

    def read_only_open(path, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        if ENTRY.fullmatch(Path(path).name) or HISTORY.fullmatch(Path(path).name):
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)

    def no_write(*args, **kwargs):
        raise AssertionError("dry-run must not write/unlink")

    with monkeypatch.context() as patch:
        patch.setattr(Cache, "_read", entry_read)
        patch.setattr(Cache, "_read_history", history_read)
        patch.setattr(os, "open", read_only_open)
        patch.setattr(Cache, "_write", no_write)
        patch.setattr(Cache, "_remove", no_write)
        assert cache.collect(max_bytes=budget, dry_run=True, summary=True) == wanted
    assert sorted(reads) == sorted(p.name for p in [*cache.entries(), *cache.histories()])
    assert sum(wanted["planned_reasons"]["entries"].values()) == wanted["planned_entries"] == 6
    assert sum(wanted["planned_reasons"]["histories"].values()) == wanted["planned_histories"] == 8
    assert wanted["removed"] == wanted["histories_removed"] == 0
    assert wanted["bytes_before"] == cache.status()["managed_total_bytes"]
    assert wanted["bytes_after"] == budget
    command = ["gc", "--summary", "--cache-dir", str(cache.directory), "--max-bytes", str(budget)]
    outputs = []
    for _ in range(2):
        assert main([*command, "--dry-run"]) == 0
        captured = capsys.readouterr()
        assert captured.err == "" and json.loads(captured.out) == wanted
        outputs.append(captured.out)
    assert outputs[0] == outputs[1]
    assert snapshot(project) == before
    assert main(command) == 0
    captured = capsys.readouterr()
    applied = json.loads(captured.out)
    assert captured.err == ""
    assert applied == wanted | {"dry_run": False, "removed": 6, "histories_removed": 8}
    for output in (*outputs, captured.out):
        assert all(p.name not in output for p in expected | keep)
        assert str(project) not in output and "private" not in output and "sensitive" not in output
        assert "plan" not in json.loads(output)
    assert set(cache.entries() + cache.histories()) == keep
    after = snapshot(project)
    changed = {".", str(cache.directory.relative_to(project))}
    removed = {str(p.relative_to(project)) for p in expected}
    assert after == {name: value for name, value in before.items() if name not in removed | changed} | {
        name: after[name] for name in changed}
    assert cache.status()["managed_total_bytes"] == budget
    assert cache.collect(max_bytes=budget, dry_run=True, summary=True)["planned_reasons"] == counts([])
    assert type(cache.gc(max_bytes=0)) is int
    assert cache.status()["managed_total_bytes"] == 0


@pytest.mark.parametrize("state", ["missing", "empty", "marked", "live"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_summary_empty_reasons_no_creation_or_rewrite(project, capsys, state, dry_run):
    cache = Cache(project / state)
    if state == "empty":
        cache.directory.mkdir()
    elif state == "marked":
        cache.prepare(create=True)
    elif state == "live":
        seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    before = snapshot(project)
    default = cache.collect(dry_run=dry_run)
    expected = summary_of(default)
    assert expected["planned_reasons"] == counts([])
    assert cache.collect(dry_run=dry_run, summary=True) == expected
    assert main(["gc", "--summary", "--cache-dir", str(cache.directory),
                 *(["--dry-run"] if dry_run else [])]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == expected
    assert snapshot(project) == before


@pytest.mark.parametrize("kind", ["entry", "history"])
@pytest.mark.parametrize("failure", ["directory", "inventory"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_summary_preflight_refusal(project, capsys, monkeypatch, kind, failure, dry_run):
    cache = Cache(project / "refusal")
    pair = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    pair[0].write_text("{")  # Would be removed, if not preflighted.
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
        with pytest.raises((InputError, PermissionError)):
            cache.collect(max_bytes=0, dry_run=dry_run, summary=True)
        assert main(["gc", "--summary", "--cache-dir", str(cache.directory), "--max-bytes", "0",
                     *(["--dry-run"] if dry_run else [])]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert ("subdirectory" if failure == "directory" else "inventory denial") in captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("dry_run", [False, True])
def test_summary_sensitive_validation_errors_are_only_corrupt(project, monkeypatch, capsys, dry_run):
    cache = Cache(project / "sensitive")
    seed(cache, project / "clean.sql", "a" * 64, time.time_ns())

    def denied(*args):
        raise PermissionError("private-source/SELECT secret/raw error")

    monkeypatch.setattr(Cache, "_read", denied)
    monkeypatch.setattr(Cache, "_read_history", denied)
    assert main(["gc", "--summary", "--cache-dir", str(cache.directory),
                 *(["--dry-run"] if dry_run else [])]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert captured.err == ""
    assert report["planned_reasons"] == {kind: dict.fromkeys(CODES, 0) | {"corrupt": 1}
                                          for kind in ("entries", "histories")}
    assert "private" not in captured.out and "secret" not in captured.out and "raw error" not in captured.out
    assert "plan" not in report


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("options", [{"max_age_days": -1}, {"max_age_days": float("nan")},
                                     {"max_bytes": -1}, {"max_bytes": True}])
def test_summary_invalid_limits_before_preparation(project, dry_run, options):
    cache = Cache(project / "absent")
    with pytest.raises(InputError):
        cache.collect(**options, dry_run=dry_run, summary=True)
    assert not cache.directory.exists()


def test_summary_partial_unlink_failure_no_success_output(project, capsys, monkeypatch):
    cache = Cache(project / "partial")
    first, history = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    later = cache.directory / ("b" * 64 + ".json")
    later.write_text("{")
    os.utime(later, ns=(first.stat().st_mtime_ns + 10**9,) * 2)
    original = Cache._remove

    def denied(self, path):
        if path == later:
            raise PermissionError("synthetic unlink denial")
        return original(self, path)

    monkeypatch.setattr(Cache, "_remove", denied)
    assert main(["gc", "--summary", "--cache-dir", str(cache.directory), "--max-bytes", "0"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "unlink denial" in captured.err
    assert not first.exists() and not history.exists() and later.exists()
