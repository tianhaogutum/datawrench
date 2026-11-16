"""Name-free full-clear previews project the same inventory without content reads."""
import json
import os
import time
from pathlib import Path

import pytest

from datawrench.lint_cache.cache import Cache, MARKER
from datawrench.lint_cache.cli import main
from datawrench.lint_cache.fingerprint import InputError
from .test_clear import snapshot
from .test_clear_preview import EMPTY
from .test_gc_budget import seed


def summary_of(report):
    return {key: value for key, value in report.items() if key != "plan"}


def test_summary_mixed_matches_default_without_reads_or_writes(project, monkeypatch, capsys):
    cache = Cache(project / "private-cache")
    seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    target = project / "private-target"
    target.mkdir()
    (target / "secret.sql").write_text("SELECT 'sensitive SQL';\n")
    for prefix, suffix in (("", ".json"), (".inputs-", "")):
        (cache.directory / (prefix + "b" * 64 + suffix)).write_text("{private malformed SQL")
        for token, destination in (("c", target / "secret.sql"), ("d", target),
                                   ("e", project / "absent-target")):
            (cache.directory / (prefix + token * 64 + suffix)).symlink_to(destination)
        os.mkfifo(cache.directory / (prefix + "f" * 64 + suffix))
    for name in ("unknown.json", ".entry-interrupted", "A" * 64 + ".json", ".inputs-short"):
        (cache.directory / name).write_text("unmanaged")
    (cache.directory / "unknown-link").symlink_to(target, target_is_directory=True)
    nested = cache.directory / "ordinary"
    nested.mkdir()
    (nested / ("a" * 64 + ".json")).write_text("unmanaged nested")
    before = snapshot(project)
    owned = {*cache.entries(), *cache.histories()}
    default = cache.plan_clear()
    expected = summary_of(default)
    assert expected == {"dry_run": True, "removed": 0, "histories_removed": 0,
                        "planned_entries": 6, "planned_histories": 6,
                        "managed_total_bytes": sum(p.lstat().st_size for p in owned)}
    original_open = os.open
    opens = []

    def marker_or_directory_only(path, flags, *args, **kwargs):
        opens.append(Path(path))
        assert Path(path) in (cache.directory, cache.directory / MARKER)
        assert flags & os.O_NOFOLLOW
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return original_open(path, flags, *args, **kwargs)

    def no_read_or_write(*args, **kwargs):
        raise AssertionError("preview must not read managed contents/targets or mutate")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", marker_or_directory_only)
        for method in ("_read", "_read_history", "_write", "_remove"):
            patch.setattr(Cache, method, no_read_or_write)
        assert cache.plan_clear(summary=True) == expected
        assert cache.plan_clear(summary=False) == default
        outputs = []
        for _ in range(2):
            assert main(["clear", "--dry-run", "--summary", "--cache-dir", str(cache.directory)]) == 0
            captured = capsys.readouterr()
            assert captured.err == "" and json.loads(captured.out) == expected
            assert all(p.name not in captured.out for p in owned)
            assert all(s not in captured.out for s in (str(project), "private", "SELECT", "sensitive"))
            outputs.append(captured.out)
    assert opens and outputs[0] == outputs[1]
    assert snapshot(project) == before

    # Full clear still uses its original JSON and result-only integer contract.
    assert main(["clear", "--cache-dir", str(cache.directory)]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == {"removed": 6}
    assert not any(p.exists() or p.is_symlink() for p in owned)
    after = snapshot(project)
    changed = {"private-cache"}
    removed = {str(p.relative_to(project)) for p in owned}
    assert after == {k: v for k, v in before.items() if k not in removed | changed} | {
        k: after[k] for k in changed}
    assert type(cache.clear()) is int
    assert cache.clear() == 0


@pytest.mark.parametrize("state", ["missing", "empty", "marked"])
def test_summary_zero_schema_no_creation_or_marking(project, capsys, state):
    cache = Cache(project / state)
    if state == "empty":
        cache.directory.mkdir()
    elif state == "marked":
        cache.prepare(create=True)
    before = snapshot(project)
    assert cache.plan_clear(summary=True) == summary_of(EMPTY)
    assert cache.plan_clear() == cache.plan_clear(summary=False) == EMPTY
    assert main(["clear", "--summary", "--dry-run", "--cache-dir", str(cache.directory)]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == summary_of(EMPTY)
    assert snapshot(project) == before


@pytest.mark.parametrize("kind", ["entry", "history"])
@pytest.mark.parametrize("failure", ["directory", "inventory"])
def test_summary_preflight_failure_has_no_stdout_or_mutation(project, monkeypatch, capsys, kind, failure):
    cache = Cache(project / "refusal")
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
        with pytest.raises((InputError, PermissionError)):
            cache.plan_clear(summary=True)
        assert main(["clear", "--dry-run", "--summary", "--cache-dir", str(cache.directory)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert ("subdirectory" if failure == "directory" else "inventory denial") in captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("unsafe", ["unmarked", "cache_link", "marker_link", "invalid_marker"])
def test_summary_unsafe_cache_refusal(project, capsys, unsafe):
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
    with pytest.raises(InputError):
        cache.plan_clear(summary=True)
    assert main(["clear", "--summary", "--dry-run", "--cache-dir", str(cache.directory)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("state", ["missing", "empty", "marked", "unmarked"])
def test_summary_requires_dry_run_before_preparation(project, capsys, monkeypatch, state):
    cache = Cache(project / state)
    if state == "empty":
        cache.directory.mkdir()
    elif state == "marked":
        seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    elif state == "unmarked":
        cache.directory.mkdir()
        (cache.directory / "keep").write_text("untouched")
    before = snapshot(project)

    def no_prepare(*args, **kwargs):
        raise AssertionError("invalid combination must be rejected before prepare")

    monkeypatch.setattr(Cache, "prepare", no_prepare)
    assert main(["clear", "--summary", "--cache-dir", str(cache.directory)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "--summary requires --dry-run" in captured.err
    assert snapshot(project) == before
