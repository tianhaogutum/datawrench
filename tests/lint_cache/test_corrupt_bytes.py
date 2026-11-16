"""Status corruption byte histograms reuse the same validation and inventory."""
import json
import os
import time
from pathlib import Path

import pytest

from datawrench.lint_cache.cache import Cache, MAX_ENTRY, MAX_HISTORY
from datawrench.lint_cache.cli import main
from datawrench.lint_cache.fingerprint import InputError
from .test_clear import snapshot
from .test_corrupt_counts import CODES, ZERO_COUNTS
from .test_gc_budget import seed
from .test_status import ZERO


@pytest.mark.parametrize("details,reasons", [(False, False), (True, False), (True, True)])
def test_bytes_share_single_pass_and_match_details(project, capsys, monkeypatch, details, reasons):
    cache = Cache(project / "bytes")
    valid = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    expired = seed(cache, project / "bad.sql", "b" * 64, 1)
    orphan = seed(cache, project / "gone.sql", "3" * 64, 1)
    orphan[0].unlink()
    target = project / "PRIVATE-target.sql"
    target.write_text("PRIVATE SQL and raw inputs")
    expected = {p: None for p in (*valid, *expired, orphan[1])}
    denied, no_json = set(), set()
    for prefix, suffix, limit in (("", ".json", MAX_ENTRY), (".inputs-", "", MAX_HISTORY)):
        for token, payload, reason in (("c", "{PRIVATE", "invalid_data"),
                                        ("d", "{}", "unreadable")):
            path = cache.directory / (prefix + token * 64 + suffix)
            path.write_text(payload)
            expected[path] = reason
            if reason == "unreadable":
                denied.add(path)
        large = cache.directory / (prefix + "e" * 64 + suffix)
        with large.open("wb") as handle:
            handle.truncate(limit + 1)
        expected[large] = "oversized"
        denied.add(large)  # Size takes precedence over I/O errors.
        no_json.add(large)
        for token, destination in (("0", target), ("1", project / "absent"), ("2", project)):
            path = cache.directory / (prefix + token * 64 + suffix)
            path.symlink_to(destination)
            expected[path] = "non_regular"
            no_json.add(path)
        fifo = cache.directory / (prefix + "f" * 64 + suffix)
        os.mkfifo(fifo)
        expected[fifo] = "non_regular"
        no_json.add(fifo)
        assert fifo.lstat().st_size == 0
    # Different byte totals for each kind catch aliasing or swapped histograms.
    extra = cache.directory / ("9" * 64 + ".json")
    extra.write_text("PRIVATE different length")
    expected[extra] = "invalid_data"
    for name in ("unknown.json", ".entry-partial", "A" * 64 + ".json"):
        (cache.directory / name).write_text("PRIVATE ignored")
    nested = cache.directory / "ordinary"
    nested.mkdir()
    (nested / ("8" * 64 + ".json")).write_text("PRIVATE nested")
    sizes = {p: p.lstat().st_size for p in expected}
    expected_bytes = {group: {code: sum(sizes[p] for p, reason in expected.items()
                                      if reason == code and p.name.startswith(".inputs-") == history)
                              for code in CODES}
                      for group, history in (("entries", False), ("histories", True))}
    before = snapshot(project)
    original_open, original_load = os.open, json.load
    original_lstat, original_stat = Path.lstat, Path.stat
    opens, loads, stats, stat_calls = [], [], [], []

    def checked_open(path, flags, *args, **kwargs):
        candidate = Path(path)
        if candidate in expected:
            opens.append(candidate)
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
        assert candidate != target
        if candidate in denied:
            raise PermissionError("PRIVATE raw error /secrets/query.sql")
        return original_open(path, flags, *args, **kwargs)

    def checked_load(handle):
        path = Path(os.readlink(f"/proc/self/fd/{handle.fileno()}"))
        assert path not in no_json
        loads.append(path)
        return original_load(handle)

    def checked_lstat(path):
        if path in expected:
            stats.append(path)
        return original_lstat(path)

    def checked_stat(path, *args, **kwargs):
        if path in expected:
            stat_calls.append(path)
        if kwargs.get("follow_symlinks", True):
            assert path not in expected and path != target  # No target stat traversal.
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", checked_open)
        patch.setattr(json, "load", checked_load)
        patch.setattr(Path, "lstat", checked_lstat)
        patch.setattr(Path, "stat", checked_stat)
        result = cache.status(details=details, corrupt_reasons=reasons,
                              corrupt_counts=True, corrupt_bytes=True)
        assert sorted(stats) == sorted(stat_calls) == sorted(opens) == sorted(expected)
        assert sorted(loads) == sorted(expected.keys() - denied - no_json)
        assert result["corrupt_bytes"] == expected_bytes
        assert all(type(n) is int and n >= 0 for group in result["corrupt_bytes"].values() for n in group.values())
        assert result["corrupt_counts"]["entries"] == {
            "non_regular": 4, "oversized": 1, "unreadable": 1, "invalid_data": 2}
        assert result["corrupt_counts"]["histories"] == {
            "non_regular": 4, "oversized": 1, "unreadable": 1, "invalid_data": 1}
        assert {k: v for k, v in result.items() if k != "corrupt_bytes"} == cache.status(
            details=details, corrupt_reasons=reasons, corrupt_counts=True)
        reason_details = cache.status(details=True, corrupt_reasons=True)["details"]
        for kind, group in (("entry", "entries"), ("history", "histories")):
            assert {code: sum(item["bytes"] for item in reason_details
                              if item["kind"] == kind and item["corrupt_reason"] == code)
                    for code in CODES} == expected_bytes[group]
        flags = ["--corrupt-counts", "--corrupt-bytes"] + (["--details"] if details else [])
        if reasons:
            flags.append("--corrupt-reasons")
        command = ["status", "--cache-dir", str(cache.directory), *flags]
        assert main(command) == 0
        first = capsys.readouterr()
        assert first.err == "" and json.loads(first.out) == result
        assert "PRIVATE" not in first.out and str(project) not in first.out
        if details:
            fields = {"name", "kind", "bytes", "corrupt"} | ({"corrupt_reason"} if reasons else set())
            assert all(set(item) == fields for item in result["details"])
        else:
            assert set(result) == set(ZERO) | {"corrupt_counts", "corrupt_bytes"}
            assert all(p.name not in first.out for p in expected)
        assert main(command) == 0
        assert capsys.readouterr() == first
    assert snapshot(project) == before


@pytest.mark.parametrize("state", ["missing", "empty", "marked", "valid"])
@pytest.mark.parametrize("details,reasons", [(False, False), (True, False), (True, True)])
def test_zero_bytes_and_existing_schemas(project, capsys, state, details, reasons):
    cache = Cache(project / state)
    if state == "empty":
        cache.directory.mkdir()
    elif state == "marked":
        cache.prepare(create=True)
    elif state == "valid":
        seed(cache, project / "clean.sql", "a" * 64, 1)
    before = snapshot(project)
    expected = {**cache.status(details=details, corrupt_reasons=reasons, corrupt_counts=True),
                "corrupt_bytes": ZERO_COUNTS}
    assert cache.status(details=details, corrupt_reasons=reasons,
                        corrupt_counts=True, corrupt_bytes=True) == expected
    flags = ["--corrupt-counts", "--corrupt-bytes"] + (["--details"] if details else [])
    if reasons:
        flags.append("--corrupt-reasons")
    assert main(["status", "--cache-dir", str(cache.directory), *flags]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == expected
    assert snapshot(project) == before


@pytest.mark.parametrize("details,reasons,counts,error", [
    (False, False, False, "--corrupt-bytes requires --corrupt-counts"),
    (True, False, False, "--corrupt-bytes requires --corrupt-counts"),
    (True, True, False, "--corrupt-bytes requires --corrupt-counts"),
    (False, True, True, "--corrupt-reasons requires --details"),
    (False, True, False, "--corrupt-reasons requires --details")])
def test_invalid_combinations_fail_before_prepare(project, capsys, monkeypatch, details, reasons, counts, error):
    cache = Cache(project / "absent")
    before = snapshot(project)

    def unexpected(*args, **kwargs):
        pytest.fail("invalid combination must fail before preparation")

    monkeypatch.setattr(Cache, "prepare", unexpected)
    with pytest.raises(InputError, match=error):
        cache.status(details=details, corrupt_reasons=reasons, corrupt_counts=counts, corrupt_bytes=True)
    flags = ["--corrupt-bytes"]
    for enabled, flag in ((details, "--details"), (reasons, "--corrupt-reasons"), (counts, "--corrupt-counts")):
        if enabled:
            flags.append(flag)
    assert main(["status", "--cache-dir", str(cache.directory), *flags]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and error in captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("kind", ["entry", "history"])
@pytest.mark.parametrize("failure", ["directory", "lstat", "enumeration"])
@pytest.mark.parametrize("details", [False, True])
def test_bytes_keep_preflight_failures_separate(project, capsys, monkeypatch, kind, failure, details):
    cache = Cache(project / "refusals")
    seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    path = cache.directory / (("f" * 64 + ".json") if kind == "entry" else ".inputs-" + "f" * 64)
    if failure == "directory":
        path.mkdir()
        (path / "keep").write_text("untouched")
    else:
        path.write_text("untouched")
    before = snapshot(project)
    original = Path.lstat

    def denied(candidate):
        if candidate == path:
            raise PermissionError("inventory denial")
        return original(candidate)

    def enumeration_denied(*args):
        raise OSError("inventory enumeration denial")

    with monkeypatch.context() as patch:
        if failure == "lstat":
            patch.setattr(Path, "lstat", denied)
        elif failure == "enumeration":
            patch.setattr(Cache, "entries" if kind == "entry" else "histories", enumeration_denied)
        flags = ["--corrupt-counts", "--corrupt-bytes"]
        if details:
            flags += ["--details", "--corrupt-reasons"]
        assert main(["status", "--cache-dir", str(cache.directory), *flags]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("unsafe", ["unmarked", "cache_link", "marker_link", "invalid_marker"])
def test_bytes_refuse_unsafe_cache(project, capsys, unsafe):
    from datawrench.lint_cache.cache import MARKER

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
    assert main(["status", "--cache-dir", str(cache.directory), "--corrupt-counts", "--corrupt-bytes"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert snapshot(project) == before
