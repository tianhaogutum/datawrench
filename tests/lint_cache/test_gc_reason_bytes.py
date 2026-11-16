"""Opt-in name-free byte sums project the existing GC plan and inventory."""
import json
import os
import time
from pathlib import Path

import pytest

from datawrench.lint_cache.cache import Cache
from datawrench.lint_cache.cli import main
from datawrench.lint_cache.fingerprint import InputError
from .test_clear import snapshot
from .test_gc_budget import seed
from .test_gc_summary import CODES, counts, mixed, summary_of


def byte_sums(plan):
    result = {kind: dict.fromkeys(CODES, 0) for kind in ("entries", "histories")}
    for item in plan:
        result["entries" if item["kind"] == "entry" else "histories"][item["reason"]] += item["bytes"]
    return result


def with_bytes(report):
    return summary_of(report) | {"planned_reason_bytes": byte_sums(report["plan"])}


def test_reason_bytes_mixed_same_reads_stats_precedence_and_exact_apply(project, mixed, monkeypatch, capsys):
    cache, budget, expected, keep = mixed
    # Each overlapping item must contribute once, using existing precedence.
    old = time.time_ns() - 50 * 86400 * 10**9
    overlaps = [cache.directory / (token * 64 + ".json") for token in ("0", "b")]
    overlaps += [cache.directory / (".inputs-" + "0" * 64)]
    overlaps += [cache._history_path(project / ("private-source-" + token + ".sql"))
                 for token in ("a", "b", "c")]
    for path in overlaps:
        os.utime(path, ns=(old, old))
    before = snapshot(project)
    trace = []

    def tracked(original, name):
        def call(*args, **kwargs):
            trace.append((name, tuple(str(a) for a in args)))
            return original(*args, **kwargs)
        return call

    with monkeypatch.context() as patch:
        for owner, name in ((Path, "stat"), (Path, "lstat"), (os, "fstat"), (os, "open"),
                            (Cache, "_read"), (Cache, "_read_history")):
            patch.setattr(owner, name, tracked(getattr(owner, name), name))
        default = cache.collect(max_bytes=budget, dry_run=True)
        default_trace = trace[:]
        trace.clear()
        wanted = with_bytes(default)
        assert cache.collect(max_bytes=budget, dry_run=True, summary=True, reason_bytes=True) == wanted
        assert trace == default_trace  # No new stat/read pass or target traversal.
    assert {item["name"] for item in default["plan"]} == {p.name for p in expected}
    assert counts(default["plan"])["entries"] == dict(zip(CODES, (3, 1, 1, 0, 0, 1)))
    assert counts(default["plan"])["histories"] == dict(zip(CODES, (3, 1, 1, 1, 2, 0)))
    assert sum(sum(group.values()) for group in wanted["planned_reason_bytes"].values()) == (
        wanted["bytes_before"] - wanted["bytes_after"])
    assert all(type(value) is int and value >= 0 for group in wanted["planned_reason_bytes"].values()
               for value in group.values())
    assert cache.collect(max_bytes=budget, dry_run=True, summary=True) == summary_of(default)
    command = ["gc", "--summary", "--reason-bytes", "--cache-dir", str(cache.directory),
               "--max-bytes", str(budget)]
    outputs = []
    for _ in range(2):
        assert main([*command, "--dry-run"]) == 0
        captured = capsys.readouterr()
        assert captured.err == "" and json.loads(captured.out) == wanted
        outputs.append(captured.out)
    assert outputs[0] == outputs[1] and snapshot(project) == before
    assert main(command) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    applied = json.loads(captured.out)
    assert applied == wanted | {"dry_run": False, "removed": 6, "histories_removed": 8}
    for output in (*outputs, captured.out):
        assert all(path.name not in output for path in expected | keep)
        assert str(project) not in output and "private" not in output and "sensitive" not in output
        assert "plan" not in json.loads(output)
    assert set(cache.entries() + cache.histories()) == keep
    after = snapshot(project)
    changed = {".", str(cache.directory.relative_to(project))}
    removed = {str(p.relative_to(project)) for p in expected}
    assert after == {n: v for n, v in before.items() if n not in removed | changed} | {
        n: after[n] for n in changed}
    assert cache.status()["managed_total_bytes"] == wanted["bytes_after"]
    assert type(cache.gc(max_bytes=0)) is int


@pytest.mark.parametrize("state", ["missing", "empty", "marked", "live"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_reason_bytes_zero_schema_and_no_creation(project, capsys, state, dry_run):
    cache = Cache(project / state)
    if state == "empty":
        cache.directory.mkdir()
    elif state == "marked":
        cache.prepare(create=True)
    elif state == "live":
        seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    before = snapshot(project)
    default = cache.collect(dry_run=dry_run)
    wanted = with_bytes(default)
    assert wanted["planned_reason_bytes"] == byte_sums([])
    assert cache.collect(dry_run=dry_run, summary=True, reason_bytes=True) == wanted
    assert cache.collect(dry_run=dry_run, summary=True) == summary_of(default)
    assert main(["gc", "--summary", "--reason-bytes", "--cache-dir", str(cache.directory),
                 *(["--dry-run"] if dry_run else [])]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == wanted
    assert snapshot(project) == before


@pytest.mark.parametrize("dry_run", [False, True])
def test_reason_bytes_requires_summary_before_prepare(project, monkeypatch, capsys, dry_run):
    cache = Cache(project / "missing")
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid combination reached preparation")
    monkeypatch.setattr(Cache, "prepare", forbidden)
    with pytest.raises(InputError, match="--reason-bytes requires --summary"):
        cache.collect(reason_bytes=True, dry_run=dry_run)
    assert main(["gc", "--reason-bytes", "--cache-dir", str(cache.directory),
                 *(["--dry-run"] if dry_run else [])]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "--reason-bytes requires --summary" in captured.err
    assert not cache.directory.exists()


@pytest.mark.parametrize("kind", ["entry", "history"])
@pytest.mark.parametrize("failure", ["directory", "inventory"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_reason_bytes_preflight_no_mutation(project, monkeypatch, capsys, kind, failure, dry_run):
    cache = Cache(project / "refusal")
    pair = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    pair[0].write_text("{")
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
            cache.collect(max_bytes=0, summary=True, reason_bytes=True, dry_run=dry_run)
        assert main(["gc", "--summary", "--reason-bytes", "--cache-dir", str(cache.directory),
                     "--max-bytes", "0", *(["--dry-run"] if dry_run else [])]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert ("subdirectory" if failure == "directory" else "inventory denial") in captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("dry_run", [False, True])
def test_reason_bytes_validator_errors_not_disclosed(project, monkeypatch, capsys, dry_run):
    cache = Cache(project / "sensitive")
    seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    def denied(*args):
        raise PermissionError("private-source/SELECT secret/raw error")
    monkeypatch.setattr(Cache, "_read", denied)
    monkeypatch.setattr(Cache, "_read_history", denied)
    expected = with_bytes(cache.collect(dry_run=True))
    assert main(["gc", "--summary", "--reason-bytes", "--cache-dir", str(cache.directory),
                 *(["--dry-run"] if dry_run else [])]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == expected | {"dry_run": dry_run,
        "removed": 0 if dry_run else 1, "histories_removed": 0 if dry_run else 1}
    assert all(token not in captured.out for token in ("private", "secret", "raw error", str(project)))


@pytest.mark.parametrize("options", [{"max_age_days": -1}, {"max_age_days": float("nan")},
                                     {"max_bytes": -1}, {"max_bytes": True}])
def test_reason_bytes_invalid_limits_before_prepare(project, monkeypatch, options):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid limit reached preparation")
    monkeypatch.setattr(Cache, "prepare", forbidden)
    with pytest.raises(InputError):
        Cache(project / "absent").collect(**options, summary=True, reason_bytes=True)


def test_reason_bytes_partial_unlink_no_success_json(project, monkeypatch, capsys):
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
    assert main(["gc", "--summary", "--reason-bytes", "--cache-dir", str(cache.directory),
                 "--max-bytes", "0"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "unlink denial" in captured.err
    assert not first.exists() and not history.exists() and later.exists()
