"""Aggregate corruption categories are opt-in and disclose no managed names."""
import json
import os
import time
from pathlib import Path

import pytest

from datawrench.lint_cache.cache import Cache, MARKER, MAX_ENTRY, MAX_HISTORY
from datawrench.lint_cache.cli import main
from datawrench.lint_cache.fingerprint import InputError
from .test_clear import snapshot
from .test_gc_budget import seed
from .test_status import ZERO

CODES = ("non_regular", "oversized", "unreadable", "invalid_data")
ZERO_COUNTS = {kind: dict.fromkeys(CODES, 0) for kind in ("entries", "histories")}


@pytest.mark.parametrize("details,reasons", [(False, False), (True, False), (True, True)])
def test_counts_share_pass_precedence_and_existing_schemas(project, capsys, monkeypatch, details, reasons):
    cache = Cache(project / "counts")
    valid = seed(cache, project / "clean.sql", "a" * 64, time.time_ns())
    orphan = seed(cache, project / "bad.sql", "b" * 64, 1)
    orphan[0].unlink()
    (project / "bad.sql").unlink()
    outside = project / "PRIVATE-source.sql"
    outside.write_text("PRIVATE SQL and raw inputs")
    denied, no_json = set(), set()
    expected = {valid[0]: None, valid[1]: None, orphan[1]: None}
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
        denied.add(large)  # Size must win over an open failure.
        no_json.add(large)
        for token, target in (("0", outside), ("1", project / "absent"), ("2", project)):
            path = cache.directory / (prefix + token * 64 + suffix)
            path.symlink_to(target)
            expected[path] = "non_regular"
            no_json.add(path)
        fifo = cache.directory / (prefix + "f" * 64 + suffix)
        os.mkfifo(fifo)
        expected[fifo] = "non_regular"
        no_json.add(fifo)
    # Asymmetry detects swapped kinds or shared histogram dictionaries.
    extra = cache.directory / ("9" * 64 + ".json")
    extra.write_text("{}")
    expected[extra] = "unreadable"
    denied.add(extra)
    extra = cache.directory / (".inputs-" + "9" * 64)
    extra.write_text("null")
    expected[extra] = "invalid_data"
    for name in ("unknown.json", ".entry-interrupted", "A" * 64 + ".json"):
        (cache.directory / name).write_text("PRIVATE ignored")
    nested = cache.directory / "ordinary"
    nested.mkdir()
    (nested / ("8" * 64 + ".json")).write_text("PRIVATE nested")
    before = snapshot(project)
    original_open, original_load = os.open, json.load
    opens, loads = [], []

    def checked_open(path, flags, *args, **kwargs):
        candidate = Path(path)
        if candidate in expected:
            opens.append(candidate)
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
        if candidate in denied:
            raise PermissionError("PRIVATE validator /secrets/query.sql")
        return original_open(path, flags, *args, **kwargs)

    def checked_load(handle):
        path = Path(os.readlink(f"/proc/self/fd/{handle.fileno()}"))
        assert path not in no_json
        loads.append(path)
        return original_load(handle)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", checked_open)
        patch.setattr(json, "load", checked_load)
        result = cache.status(details=details, corrupt_reasons=reasons, corrupt_counts=True)
        assert sorted(opens) == sorted(expected)  # Exactly one validation open per name.
        assert sorted(loads) == sorted(expected.keys() - denied - no_json)
        counts = result["corrupt_counts"]
        assert counts == {
            "entries": {"non_regular": 4, "oversized": 1, "unreadable": 2, "invalid_data": 1},
            "histories": {"non_regular": 4, "oversized": 1, "unreadable": 1, "invalid_data": 2}}
        assert sum(counts["entries"].values()) == result["corrupt"] == 8
        assert sum(counts["histories"].values()) == result["history_corrupt"] == 8
        assert {k: v for k, v in result.items() if k != "corrupt_counts"} == cache.status(
            details=details, corrupt_reasons=reasons)
        aggregate = cache.status()
        assert set(aggregate) == set(ZERO)
        assert {k: v for k, v in result.items() if k in ZERO} == aggregate
        reason_details = cache.status(details=True, corrupt_reasons=True)["details"]
        for kind, group, total in (("entry", "entries", "corrupt"),
                                   ("history", "histories", "history_corrupt")):
            assert {code: sum(item["kind"] == kind and item["corrupt_reason"] == code
                              for item in reason_details) for code in CODES} == counts[group]
            assert sum(item["kind"] == kind and item["corrupt"] for item in reason_details) == result[total]
        assert sum(item["bytes"] for item in reason_details) == result["managed_total_bytes"]
        assert cache.collect(dry_run=True)["bytes_before"] == result["managed_total_bytes"]
        assert cache.plan_clear()["managed_total_bytes"] == result["managed_total_bytes"]
        flags = ["--corrupt-counts"] + (["--details"] if details else [])
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
            assert set(result) == set(ZERO) | {"corrupt_counts"}
            assert all(p.name not in first.out for p in expected)
        assert main(command) == 0
        assert capsys.readouterr() == first
    assert snapshot(project) == before


@pytest.mark.parametrize("state", ["missing", "empty", "marked", "valid"])
@pytest.mark.parametrize("details,reasons", [(False, False), (True, False), (True, True)])
def test_zero_categories_are_present_and_caches_unchanged(project, capsys, state, details, reasons):
    cache = Cache(project / state)
    if state == "empty":
        cache.directory.mkdir()
    elif state == "marked":
        cache.prepare(create=True)
    elif state == "valid":
        seed(cache, project / "clean.sql", "a" * 64, 1)
    before = snapshot(project)
    expected = {**cache.status(details=details, corrupt_reasons=reasons), "corrupt_counts": ZERO_COUNTS}
    assert cache.status(details=details, corrupt_reasons=reasons, corrupt_counts=True) == expected
    flags = ["--corrupt-counts"] + (["--details"] if details else [])
    if reasons:
        flags.append("--corrupt-reasons")
    assert main(["status", "--cache-dir", str(cache.directory), *flags]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == expected
    assert snapshot(project) == before


def test_counts_do_not_relax_reason_option_requirement(project, capsys, monkeypatch):
    cache = Cache(project / "absent")
    before = snapshot(project)

    def unexpected(*args, **kwargs):
        pytest.fail("invalid combination must fail before inspection")

    monkeypatch.setattr(Cache, "prepare", unexpected)
    with pytest.raises(InputError, match="requires --details"):
        cache.status(corrupt_counts=True, corrupt_reasons=True)
    assert main(["status", "--cache-dir", str(cache.directory),
                 "--corrupt-counts", "--corrupt-reasons"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "requires --details" in captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("kind", ["entry", "history"])
@pytest.mark.parametrize("failure", ["directory", "lstat", "enumeration"])
@pytest.mark.parametrize("flags", [["--corrupt-counts"], ["--corrupt-counts", "--details", "--corrupt-reasons"]])
def test_counts_keep_inventory_refusals_separate(project, capsys, monkeypatch, kind, failure, flags):
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
        assert main(["status", "--cache-dir", str(cache.directory), *flags]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert snapshot(project) == before


@pytest.mark.parametrize("unsafe", ["unmarked", "cache_link", "marker_link", "invalid_marker"])
def test_counts_refuse_unsafe_cache_without_changes(project, capsys, unsafe):
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
    assert main(["status", "--cache-dir", str(cache.directory), "--corrupt-counts"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert snapshot(project) == before
